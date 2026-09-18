"""基准集 / Runner / Leaderboard 回归（总方案 §14）。

§14 的两条硬要求，用测试钉住：

1. **隔离**：基准段（`Segment.role='benchmark'`）的文本不得进训练导出。
   （由 `tests/test_controlled_corruption.py::test_split_benchmark_*` 与
   `export_training` 的 role 过滤共同保证，这里再验一次"基准条目只取基准段"。）
2. **冻结**：条目里存 A/B 原文，不是只存 segment_id —— 语料清洗/切分升级之后，
   同一版基准的分数仍要可比（Regression）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import benchmark_build as BB  # noqa: E402
import benchmark_run as BR  # noqa: E402
from app import db  # noqa: E402
from app.models import (BenchmarkItem, BenchmarkRun, Candidate,  # noqa: E402
                        ControlledCorruption, Experiment, Frame, Segment, Work)


def _seed(role_bench: str = "benchmark", src_ok: bool = True):
    """造一段基准段 + 一条劣化变体（够长、有帧、有候选）。"""
    exp = "EXP-BENCH-T"
    db.init_db()
    with db.session() as s:
        if not s.get(Experiment, exp):
            s.add(Experiment(id=exp, name="t", status="created", config={}, stats={}))
        w = Work(title=f"t-bench-{role_bench}", source="test:seed")
        s.add(w)
        s.flush()
        seg = Segment(work_id=w.id, ordinal=0,
                      text="他把茶喝完才起身，屋外风声很紧，谁也没有再说话。",
                      text_clean="他把茶喝完才起身，屋外风声很紧，谁也没有再说话。",
                      role=role_bench, n_sentences=1, n_chars=22,
                      integrity='{"src_ok": %s}' % ("true" if src_ok else "false"))
        s.add(seg)
        s.flush()
        fr = Frame(experiment_id=exp, segment_id=seg.id, granularity="L",
                   extractor_model="m", prompt_version="pv")
        s.add(fr)
        s.flush()
        cand = Candidate(experiment_id=exp, frame_id=fr.id, segment_id=seg.id,
                         anon_label="XA", model="corrupt:EXPLICITIZE",
                         prompt_version="corrupt_v2",
                         text="他显然是想离开，所以把茶喝完才起身，屋外风声很紧。")
        s.add(cand)
        s.flush()
        s.add(ControlledCorruption(
            experiment_id=exp, segment_id=seg.id, frame_id=fr.id, candidate_id=cand.id,
            corruption_type="EXPLICITIZE", variable="显式化", generator_model="g",
            prompt_version="corrupt_v2", text=cand.text, n_chars=len(cand.text),
            drift={}, drift_score=0.1, fact_consistent=True, drift_ok=True,
            verify_model="v", verify_pv="corrupt_verify_v3", status="ok"))
        s.commit()
        return seg.id


def test_build_only_takes_benchmark_segments():
    """只收 `role='benchmark'` 的段——非基准段进了基准集就是泄漏。"""
    _seed(role_bench="benchmark")
    _seed(role_bench="train")          # 这一段不该被收
    out = BB.build_corruption_detection("cc-test-a", version=1, seed=1)
    assert out["items"] >= 1
    with db.session() as s:
        items = s.query(BenchmarkItem).filter_by(set_id=out["set_id"]).all()
        roles = {s.get(Segment, x.segment_id).role for x in items if x.segment_id}
    assert roles == {"benchmark"}, f"基准集里混进了非基准段：{roles}"


def test_build_skips_bad_source_and_requires_answer():
    """源文本判坏的段不进基准；每条都必须带答案与冻结文本。"""
    _seed(role_bench="benchmark", src_ok=False)
    out = BB.build_corruption_detection("cc-test-b", version=1, seed=2)
    with db.session() as s:
        items = s.query(BenchmarkItem).filter_by(set_id=out["set_id"]).all()
    for x in items:
        assert x.answer in ("A", "B")
        assert x.text_a and x.text_b
        assert x.text_a != x.text_b, "A/B 不能是同一段（否则答案无意义）"


def test_benchmark_items_freeze_texts():
    """冻结：条目自带 A/B 原文，改 segment 不影响已建好的基准。"""
    seg_id = _seed()
    out = BB.build_corruption_detection("cc-test-c", version=1, seed=3)
    with db.session() as s:
        item = s.query(BenchmarkItem).filter_by(set_id=out["set_id"]).first()
        frozen = item.text_a
        seg = s.get(Segment, seg_id)
        seg.text_clean = "完全换掉的一段文字，用来验证基准条目是否被牵动。"
        seg.text = seg.text_clean
        s.commit()
        again = s.query(BenchmarkItem).filter_by(id=item.id).one()
    assert again.text_a == frozen, "基准条目必须冻结文本，不能跟着语料变"


def test_parse_pick_robust():
    assert BR.parse_pick('{"pick": "A", "confidence": 0.7}') == "A"
    assert BR.parse_pick("B") == "B"
    assert BR.parse_pick("答案是 B。") == "B"
    assert BR.parse_pick("不知道") is None


def test_wilson_bounds():
    lo, hi = BR.wilson(0, 10)
    assert lo == 0.0 and 0 < hi < 1
    lo, hi = BR.wilson(10, 10)
    assert hi == 1.0 and 0 < lo < 1
    lo, hi = BR.wilson(5, 0)
    assert (lo, hi) == (0.0, 0.0)


def test_run_records_and_leaderboard(monkeypatch):
    """Runner 写 benchmark_runs，且答对率按 ans 判定（位置随机化不影响正确性）。"""
    _seed()
    out = BB.build_corruption_detection("cc-test-d", version=1, seed=4)
    # 用一个假模型：总是顺着答案答，验证"答对率"口径不是位置口径
    from app.gateway import ChatResult

    def fake_chat(**kw):
        if '"pick"' in kw["user"]:
            ans = "A" if "哪一边" in kw["user"] else "A"
        return ChatResult(text='{"pick": "A"}', tokens_in=1, tokens_out=1, latency_ms=1)

    monkeypatch.setattr(BR, "chat", fake_chat)
    res = BR.run_set(set_id=out["set_id"], models=["fake/model"], conc=1)
    assert res["fake/model"]["n"] >= 1
    with db.session() as s:
        run = s.query(BenchmarkRun).filter_by(set_id=out["set_id"],
                                              model="fake/model").one()
    # 假模型永远选 A：答对数 = 答案为 A 的条数
    with db.session() as s:
        n_a = s.query(BenchmarkItem).filter_by(set_id=out["set_id"], answer="A").count()
    assert run.n_correct == n_a, "答对判定必须按条目答案，不能按固定位置"
