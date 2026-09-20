"""会审①收口：corpus v2 镜像段的下游隔离（采样/建批/基准/导出侧真接上了排除）。

钉住的事（全部对应 app.models.exclude_corpus_v2_segments / is_corpus_v2_work 的实际消费方）：
1. near_dup.train_sampling_pool —— v2 段永不进 create_experiment 的采样域；
2. near_dup.split_benchmark  —— v2 段永不被划成基准段（同文双份入基准就是污染）；
3. scale_corpus.pick         —— 扩产挑段排除 v2（v2 role=None，不排就会和 v1 双份入池）；
4. goldpick_build            —— gold 指认清单只认 v1；
5. export_training           —— 训练导出闸门给出 corpus_v2 剔除原因；
6. benchmark_build           —— 建批侧即使 v2 段被误标 benchmark 也拿不到它；
7. 标记口径覆盖"回填前"的历史行（只有标题后缀、v2_of 为空）——与 v2_of 同判定。
8. controlled_corruption     —— 隐藏基准池 / role='benchmark' 两个写入侧（会审①复发路径）；
9. ai_ranking_build          —— AI-vs-AI 排序池不收 v2 镜像（同文双份=同一证据计两次）。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import benchmark_build as BB  # noqa: E402
import goldpick_build as GP  # noqa: E402
import scale_corpus as SC  # noqa: E402
import ai_ranking_build as AR  # noqa: E402
import controlled_corruption as CC  # noqa: E402
from app import db, near_dup  # noqa: E402
from app.models import (Candidate, ControlledCorruption, Experiment, Frame, Segment,  # noqa: E402
                       Work, exclude_corpus_v2_segments, is_corpus_v2_work)
from app.typo_map import V2_TITLE_SUFFIX  # noqa: E402
import export_training as ET  # noqa: E402

# 与 tests/test_goldpick.py 同款的"能过质量闸"正文（≥60 字、无水印、非番外）
TEXT = ("这是一段足够长的正文文本，用来通过最小字数与来源质量闸门检查，"
        "它继续讲述人物在夜色里的动作与对话，长度超过六十个汉字的门槛。")
EXP = "EXP-V2-ISO"
EXP_CC = "EXP-V2-ISOCC"   # 劣化/基准侧用例独占，避免与上面互相牵动计数


def _seed_pair(tag: str, *, legacy_title_only: bool = False):
    """建一本 v1 + 它的 v2 镜像（同文），返回 (v1_id, v2_id, v1_seg, v2_seg)。

    legacy_title_only=True 时 v2 只有标题后缀、v2_of 为空（回填前的历史行形态）。
    """
    db.init_db()
    with db.session() as s:
        v1 = Work(title=f"斗罗大陆（唐家三少）-{tag}", source="test:v2-iso")
        s.add(v1)
        s.flush()
        s1 = Segment(work_id=v1.id, ordinal=0, text=TEXT, text_clean=TEXT,
                     integrity='{"src_ok": true}', n_sentences=2, n_chars=len(TEXT))
        s.add(s1)
        s.flush()
        v2 = Work(title=f"{v1.title}{V2_TITLE_SUFFIX}", source=v1.source,
                  v2_of=None if legacy_title_only else v1.id)
        s.add(v2)
        s.flush()
        s2 = Segment(work_id=v2.id, ordinal=0, text=TEXT, text_clean=TEXT,
                     integrity='{"src_ok": true}', n_sentences=2, n_chars=len(TEXT))
        s.add(s2)
        s.commit()
        return v1.id, v2.id, s1.id, s2.id


def test_train_sampling_pool_excludes_v2():
    v1, v2, s1, s2 = _seed_pair("iso-pool")
    with db.session() as s:
        ids = {x.id for x in near_dup.train_sampling_pool(s, work_ids=[v1, v2])}
    assert s1 in ids, "v1 段仍可正常采样（排除逻辑不是一刀切）"
    assert s2 not in ids, "corpus v2 镜像段进了训练采样域——与 v1 同文双份入池"


def test_split_benchmark_never_marks_v2():
    v1, v2, s1, s2 = _seed_pair("iso-bm")
    out = near_dup.split_benchmark(work_id=v2, n=10)
    assert out["pool"] == 0 and out["marked"] == 0, \
        "v2 段可被划成基准段——同一内容双份入基准，基准被污染"
    out2 = near_dup.split_benchmark(work_id=v1, n=10)
    assert out2["marked"] == 1, "v1 侧基准划分照常（只关 v2 的闸）"


def test_scale_pick_excludes_v2():
    v1, v2, s1, s2 = _seed_pair("iso-pick")
    ids = SC.pick(500, seed=1, min_chars=60)
    assert s1 in ids, "v1 段应被挑中（否则这条断言没有牙）"
    assert s2 not in ids, "scale_corpus.pick 把 v2 镜像段和 v1 一起挑走=同文双份进扩产池"


def test_goldpick_excludes_v2():
    v1, v2, s1, s2 = _seed_pair("iso-gp")
    with db.session() as s:
        seg_ids = {seg.id for _w, seg in GP._eligible_segments(s)}
    assert s1 in seg_ids, "v1 段应在 gold 候选里"
    assert s2 not in seg_ids, "gold 指认清单收了 v2 镜像段——同文双份入 gold"


def test_export_gate_reports_corpus_v2():
    v1, v2, s1, s2 = _seed_pair("iso-exp")
    with db.session() as s:
        assert ET._excluded_reason(s, s.get(Segment, s2), {}) == "corpus_v2"
        assert ET._excluded_reason(s, s.get(Segment, s1), {}) == ""


def test_benchmark_pair_pool_excludes_v2_even_if_mis_marked():
    """建批侧双保险：v2 段即使被误标 role='benchmark'，也拿不到它进基准集合。"""
    db.init_db()
    with db.session() as s:
        if not s.get(Experiment, EXP):
            s.add(Experiment(id=EXP, name="t-v2-iso", status="created",
                             config={}, stats={}))
        s.commit()
    v1, v2, s1_id, s2_id = _seed_pair("iso-bb")
    with db.session() as s:
        for sid in (s1_id, s2_id):
            seg = s.get(Segment, sid)
            seg.role = "benchmark"
            fr = Frame(experiment_id=EXP, segment_id=sid, granularity="L",
                       extractor_model="m", prompt_version="pv")
            s.add(fr)
            s.flush()
            cand = Candidate(experiment_id=EXP, frame_id=fr.id, segment_id=sid,
                             anon_label="XB", model="corrupt:EXPLICITIZE",
                             prompt_version="corrupt_v2", text=TEXT + "多出来的解释。",
                             status="ok")
            s.add(cand)
            s.flush()
            s.add(ControlledCorruption(
                experiment_id=EXP, segment_id=sid, frame_id=fr.id,
                candidate_id=cand.id, corruption_type="EXPLICITIZE",
                variable="变量", generator_model="g", prompt_version="corrupt_v2",
                text=TEXT + "多出来的解释。", n_chars=len(TEXT), drift={},
                drift_score=0.1, fact_consistent=True, drift_ok=True,
                verify_model="v", verify_pv="pv", status="ok"))
        s.commit()
    with db.session() as s:
        got = {seg.id for _cc, seg in BB._eligible_pairs(s)}
    assert s1_id in got, "v1 基准段正常入集合"
    assert s2_id not in got, "v2 段带着 role=benchmark 也能进基准集合——血缘闸失效"


def test_marker_covers_legacy_title_only_rows():
    """历史行（v2_of 为空、只有标题后缀）与稳定键同口径——回填前的数据也闸得住。"""
    v1, v2, s1, s2 = _seed_pair("iso-legacy", legacy_title_only=True)
    with db.session() as s:
        assert s.get(Work, v2).v2_of is None
        assert is_corpus_v2_work(s.get(Work, v2)) and not is_corpus_v2_work(s.get(Work, v1))
        ids = {x.id for x in s.query(Segment).filter(exclude_corpus_v2_segments()).all()}
    assert s2 not in ids and s1 in ids


# ── 会审①复发路径：controlled_corruption / ai_ranking_build ───────────

def _ensure_exp(exp_id: str) -> None:
    db.init_db()
    with db.session() as s:
        if not s.get(Experiment, exp_id):
            s.add(Experiment(id=exp_id, name="t-v2-iso", status="created",
                             config={}, stats={}))
        s.commit()


def _seed_cc_row(seg_id: str, exp_id: str = EXP_CC) -> None:
    """给一个段建 L 帧 + 劣化行（split_benchmark / build_batch 续跑的输入形态）。"""
    _ensure_exp(exp_id)
    with db.session() as s:
        fr = Frame(experiment_id=exp_id, segment_id=seg_id, granularity="L",
                   extractor_model="m", prompt_version="pv")
        s.add(fr)
        s.flush()
        s.add(ControlledCorruption(
            experiment_id=exp_id, segment_id=seg_id, frame_id=fr.id,
            corruption_type="EXPLICITIZE", variable="变量", generator_model="g",
            prompt_version="corrupt_v2", text=TEXT + "多出来的解释。",
            n_chars=len(TEXT), drift={}, drift_score=0.1, fact_consistent=True,
            drift_ok=True, verify_model="v", verify_pv="pv", status="ok"))
        s.commit()


def _seed_ranking_pair(seg_id: str) -> None:
    """一个段配两个不同模型的白名单 ok 候选（ai_ranking_build._pool 的入池门槛）。"""
    with db.session() as s:
        fr = Frame(experiment_id=EXP_CC, segment_id=seg_id, granularity="L",
                   extractor_model="m", prompt_version="pv")
        s.add(fr)
        s.flush()
        for i, m in enumerate(("modelA", "modelB")):
            s.add(Candidate(experiment_id=EXP_CC, frame_id=fr.id, segment_id=seg_id,
                            anon_label=f"X{i}", model=m, prompt_version="reconstruct_v1",
                            text=f"{TEXT}第{i}版改写，措辞与节奏都不同。", status="ok"))
        s.commit()


def test_controlled_corruption_fresh_pool_excludes_v2():
    """隐藏基准池：v2 段 role=None 且从未被用过，不排就会和 v1 一起被挑去划基准。"""
    v1, v2, s1, s2 = _seed_pair("cc-fresh")
    with db.session() as s:
        got = {seg.id for seg, _f in CC.pick_fresh_segments(s, 500, seed=7, min_chars=60)}
    assert s1 in got, "v1 段应可当隐藏基准题（否则这条断言没有牙）"
    assert s2 not in got, ("controlled_corruption.pick_fresh_segments 收了 v2 镜像段——"
                           "跑一次就把 U0c 清掉的基准污染重新写入")


def test_controlled_corruption_split_benchmark_never_marks_v2():
    """role='benchmark' 写入侧：v2 段带着劣化行也不许被划成基准段，且要显形。"""
    v1, v2, s1, s2 = _seed_pair("cc-split")
    for sid in (s1, s2):
        _seed_cc_row(sid)
    out = CC.split_benchmark(n=10, exp=EXP_CC)
    assert out["v2_excluded"] == 1, f"v2 排除数未显形：{out}"
    assert out["marked"] == 1, f"只该划中 v1 那一个段：{out}"
    with db.session() as s:
        assert s.get(Segment, s1).role == "benchmark", "v1 侧基准划分照常（只关 v2 的闸）"
        assert s.get(Segment, s2).role is None, \
            "controlled_corruption.split_benchmark 把 v2 镜像段划成了基准段——复发路径未关闭"


def test_ai_ranking_pool_excludes_v2():
    v1, v2, s1, s2 = _seed_pair("ar-pool")
    for sid in (s1, s2):
        _seed_ranking_pair(sid)
    with db.session() as s:
        pool = AR._pool(s)
    assert s1 in pool, "v1 段应正常入排序池（否则这条断言没有牙）"
    assert s2 not in pool, ("ai_ranking_build._pool 收了 v2 镜像段——同一内容双份排序证据，"
                            "gold 弱标签被重复计数")
