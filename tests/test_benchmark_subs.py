"""T5 子基准回归：corruption_type（按类型）与 naturalness_pair（自然度成对）。

钉住的事：
1. 三种 kind 共用同一套闸门（_eligible_pairs）——闸门不一致 = 子基准之间不可比；
2. corruption_type：每类型一个冻结集合，条目类型与集合一一对应（含控制臂——
   身份检测里"中性改写"也是合法题）；naturalness_pair：控制臂**必须**被排除
   （中性改写不声称拉低自然度，混进"答案=人类侧"的题里是污染答案键）；
3. 同种子重建 → 同位置同答案（可复现，跨时间可比）；
4. runner 的 naturalness 任务口径（NAT 模板）落库、按答案键判分；
   未知 task 响亮报错，不静默回落（纪律④）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import benchmark_build as BB  # noqa: E402
import benchmark_run as BR  # noqa: E402
from app import db  # noqa: E402
from app.models import (BenchmarkItem, BenchmarkRun, Candidate,  # noqa: E402
                        ControlledCorruption, Experiment, Frame, Segment, Work)

EXP = "EXP-BENCH-SUB"
TEXT = "他把茶喝完才起身，屋外风声很紧，谁也没有再说话。窗纸被吹得鼓了一下。"


def _seed_pair(seg_key: str, ctype: str, *, src_ok: bool = True,
               ungrammatical: bool = False, status: str = "ok"):
    """一段基准段 + 一条指定类型的劣化变体。seg_key 区分不同段落。"""
    db.init_db()
    with db.session() as s:
        if not s.get(Experiment, EXP):
            s.add(Experiment(id=EXP, name="t-sub", status="created", config={}, stats={}))
        w = Work(title=f"t-sub-{seg_key}", source="test:seed-sub")
        s.add(w)
        s.flush()
        seg = Segment(work_id=w.id, ordinal=0, text=TEXT, text_clean=TEXT,
                      role="benchmark", n_sentences=2, n_chars=len(TEXT),
                      integrity='{"src_ok": %s}' % ("true" if src_ok else "false"))
        s.add(seg)
        s.flush()
        fr = Frame(experiment_id=EXP, segment_id=seg.id, granularity="L",
                   extractor_model="m", prompt_version="pv")
        s.add(fr)
        s.flush()
        bad = "他显然是很想离开的，所以他就把茶喝完了才起身。屋外的风声是很紧的呀。"
        cand = Candidate(experiment_id=EXP, frame_id=fr.id, segment_id=seg.id,
                         anon_label="XB", model=f"corrupt:{ctype}",
                         prompt_version="corrupt_v2", text=bad, status="ok")
        s.add(cand)
        s.flush()
        s.add(ControlledCorruption(
            experiment_id=EXP, segment_id=seg.id, frame_id=fr.id, candidate_id=cand.id,
            corruption_type=ctype, variable=f"变量-{ctype}", generator_model="g",
            prompt_version="corrupt_v2", text=bad, n_chars=len(bad),
            drift=({"ungrammatical": True} if ungrammatical else {}),
            drift_score=0.1, fact_consistent=True, drift_ok=True,
            verify_model="v", verify_pv="corrupt_verify_v3", status=status))
        s.commit()
        return seg.id


@pytest.fixture(scope="module", autouse=True)
def _seed_all():
    _seed_pair("a", "EXPLICITIZE")
    _seed_pair("b", "RHYTHM_FLATTEN")
    _seed_pair("c", "NEUTRAL_PARAPHRASE")          # 控制臂
    _seed_pair("d", "ADVERB_INFLATION", ungrammatical=True)   # 病句 → 必须被闸门拒
    _seed_pair("e", "EMOTION_LABEL", src_ok=False)            # 源坏 → 必须被闸门拒


def _items(set_id):
    with db.session() as s:
        return [(x.segment_id, x.kind, (x.meta or {}).get("corruption_type"), x.answer)
                for x in s.query(BenchmarkItem).filter_by(set_id=set_id).all()]


def test_corruption_type_sets_one_set_per_type():
    out = BB.build_corruption_type_sets("cct-sub", version=1, seed=7)
    types = {c["type"] for c in out["sets"]}
    # 病句/源坏的类型不该出现；控制臂是合法的类型题，要在
    assert types == {"EXPLICITIZE", "RHYTHM_FLATTEN", "NEUTRAL_PARAPHRASE"}
    with db.session() as s:
        sets = {st.name: st for st in s.query(BB.BenchmarkSet)
                .filter(BB.BenchmarkSet.kind == "corruption_type",
                        BB.BenchmarkSet.name.like("cct-sub-%")).all()}
    assert set(sets) == {f"cct-sub-{t}" for t in types}
    for name, st in sets.items():
        rows = _items(st.id)
        assert rows and all(r[1] == "corruption_type" for r in rows)
        assert all(r[2] == name.split("cct-sub-", 1)[1] for r in rows), \
            "集合里混进了别的类型——类型子基准失去意义"
        assert all(r[3] in ("A", "B") for r in rows)


def test_naturalness_pair_excludes_control_arm():
    out = BB.build_naturalness_pairs("nat-sub", version=1, seed=7)
    with db.session() as s:
        st = s.get(BB.BenchmarkSet, out["set_id"])
    assert st.kind == "naturalness_pair"
    assert "NEUTRAL_PARAPHRASE" in (st.spec or {}).get("excluded_types", [])
    rows = _items(st.id)
    types = {r[2] for r in rows}
    assert "NEUTRAL_PARAPHRASE" not in types, "控制臂混进了自然度基准——答案键被污染"
    assert types == {"EXPLICITIZE", "RHYTHM_FLATTEN"}
    assert all(r[1] == "naturalness_pair" for r in rows)


def test_same_seed_rebuilds_same_positions():
    a = BB.build_naturalness_pairs("nat-repro-a", version=1, seed=99)
    b = BB.build_naturalness_pairs("nat-repro-b", version=1, seed=99)
    ka = sorted((r[0], r[3], ) for r in _items(a["set_id"]))
    kb = sorted((r[0], r[3], ) for r in _items(b["set_id"]))
    assert ka == kb, "同种子两次构建位置/答案不同——冻结与可复现承诺被破坏"


def test_run_naturalness_task_scores_by_answer(monkeypatch):
    from app.gateway import ChatResult

    def fake_chat(**kw):
        assert "更自然" in kw["user"], "naturalness 任务必须用 NAT 模板（问自然度，不问身份）"
        return ChatResult(text='{"pick": "A"}', tokens_in=1, tokens_out=1, latency_ms=1)

    monkeypatch.setattr(BR, "chat", fake_chat)
    out = BB.build_naturalness_pairs("nat-run", version=1, seed=11)
    res = BR.run_set(set_id=out["set_id"], models=["fake/model"], task="naturalness", conc=1)
    assert res["fake/model"]["n"] >= 1
    with db.session() as s:
        run = s.query(BenchmarkRun).filter_by(set_id=out["set_id"],
                                              model="fake/model").one()
        n_a = s.query(BenchmarkItem).filter_by(set_id=out["set_id"], answer="A").count()
    assert run.n_correct == n_a, "答对判定必须按条目答案键，不能按固定位置"
    assert "task=naturalness" in (run.note or "")


def test_unknown_task_fails_loud():
    out = BB.build_naturalness_pairs("nat-loud", version=1, seed=13)
    with pytest.raises(SystemExit, match="未知 task"):
        BR.run_set(set_id=out["set_id"], models=["fake/model"], task="nope", conc=1)


def test_dry_run_touches_nothing():
    before = BB.scan()
    d1 = BB.build_corruption_type_sets("cct-dry", dry_run=True)
    d2 = BB.build_naturalness_pairs("nat-dry", dry_run=True)
    assert isinstance(d1["would_build"], dict) and isinstance(d2["would_build"], int)
    assert BB.scan()["sets"] == before["sets"], "dry-run 建了集合——违反零副作用承诺"


def test_human_vs_ai_builder_whitelist_and_answer():
    """hvai：只收白名单口径的自由重建候选；答案=人类侧；文本冻结。"""
    from app.config import BLIND_REVIEW_PROMPT_VERSIONS
    _seed_pair("hvai", ctype="EXPLICITIZE")     # 自建基准段+帧，不依赖共享库状态
    with db.session() as s:
        seg = s.query(Segment).filter_by(role="benchmark").filter(
            Segment.id.in_([x[0] for x in s.query(
                Frame.segment_id).distinct().all()])).first()
        assert seg is not None, "找不到带帧的基准段"
        fr = s.query(Frame).filter_by(segment_id=seg.id).first()
        for pv, status in (("reconstruct_v1", "ok"), ("recon_ctx_v1", "ok"),
                           ("recon_ctxonly_v1", "ok"), ("reconstruct_v1", "failed")):
            s.add(Candidate(experiment_id=EXP, frame_id=fr.id, segment_id=seg.id,
                            anon_label="XH", model="recon-model", temperature=0.7,
                            prompt_version=pv, status=status,
                            text=f"重建候选（{pv}/{status}），长度足够长以通过任何过滤。"))
        s.commit()
    out = BB.build_human_vs_ai("hvai-sub", version=1, seed=5)
    with db.session() as s:
        st = s.get(BB.BenchmarkSet, out["set_id"])
    assert st.kind == "human_vs_ai"
    with db.session() as s:
        items = s.query(BenchmarkItem).filter_by(set_id=out["set_id"]).all()
    assert all(i.kind == "human_vs_ai" for i in items)
    assert all((i.meta or {}).get("prompt_version") in BLIND_REVIEW_PROMPT_VERSIONS
               for i in items), "非白名单口径混进 hvai 基准"
    with db.session() as s:
        seg_text = (s.get(Segment, items[0].segment_id).text_clean
                    or s.get(Segment, items[0].segment_id).text)
    for i in items:
        human_side = i.text_a if i.answer == "A" else i.text_b
        assert human_side == seg_text, "答案键不是人类原文侧——hvai 答案键被破坏"
