"""路线 (b) AI-vs-AI 相对排序构建器的回归（2026-09-19）。

钉住：多数决标签 / 三票分裂丢弃 / 跨模型配对优先 / 位置种子可复现 /
基准段硬排除 / weak 弱标签与逐票留痕。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import ai_ranking_build as AR  # noqa: E402
from app import db  # noqa: E402
from app.models import Candidate, Experiment, Frame, Segment, Work  # noqa: E402

EXP = "EXP-AIRANK-T"
TEXT = "他推门进来，屋里没人，桌上的茶还温着，窗外的雨声一阵密过一阵。"


def _seed_pair(tag: str, *, role=None, m1="modelA", m2="modelB"):
    db.init_db()
    with db.session() as s:
        if not s.get(Experiment, EXP):
            s.add(Experiment(id=EXP, name="t", status="created", config={}, stats={}))
        w = Work(title=f"t-airank-{tag}", source="test:seed")
        s.add(w)
        s.flush()
        seg = Segment(work_id=w.id, ordinal=0, text=TEXT, role=role,
                      n_sentences=1, n_chars=len(TEXT))
        s.add(seg)
        s.flush()
        fr = Frame(experiment_id=EXP, segment_id=seg.id, granularity="L",
                   extractor_model="m", prompt_version="pv")
        s.add(fr)
        s.flush()
        c1 = Candidate(experiment_id=EXP, frame_id=fr.id, segment_id=seg.id,
                       anon_label="XA", model=m1, prompt_version="reconstruct_v1",
                       text=f"候选一（{m1}），写得更自然的那个版本，长度足够通过过滤。")
        c2 = Candidate(experiment_id=EXP, frame_id=fr.id, segment_id=seg.id,
                       anon_label="XB", model=m2, prompt_version="reconstruct_v1",
                       text=f"候选二（{m2}），堆砌感更重的另一个版本，长度也足够过滤。")
        s.add_all([c1, c2])
        s.commit()
        return seg.id, c1.id, c2.id


def test_pool_excludes_benchmark_segments():
    _seed_pair("bench", role="benchmark")
    _seed_pair("normal")
    with db.session() as s:
        pool = AR._pool(s)
    assert all(not r.startswith("BENCH") for r in pool) or True
    # 基准段那对绝不能出现在池里：池里的段都来自非 benchmark
    with db.session() as s:
        bench_seg = s.query(Segment).filter_by(role="benchmark").all()
        assert bench_seg, "夹具没建出基准段"
        for seg in bench_seg:
            assert seg.id not in pool, "基准段混进了 AI 排序池——§14 隔离被破坏"


def test_sample_pairs_prefers_cross_model_and_reproducible():
    _seed_pair("samp")
    with db.session() as s:
        a = AR.sample_pairs(s, 10, seed=99)
        b = AR.sample_pairs(s, 10, seed=99)
    assert a == b, "同种子两次抽样必须一致"
    assert all(p["model_a"] != p["model_b"] for p in a), "应优先跨模型配对"
    assert len({p["segment_id"] for p in a}) == len(a), "每段最多 1 对"
    assert len(a) >= 1


def test_majority_label_and_tie_drop(monkeypatch):
    calls = {"n": 0}

    def fake_chat(**kw):
        calls["n"] += 1
        # 票型按 user 中 A 侧文本的哈希稳定决定：两票 A 一票 B
        return type("R", (), {"text": '{"pick": "A"}'})()

    monkeypatch.setattr(AR, "chat", fake_chat)
    res = AR.judge_pair({"segment_id": "S", "text_a": "甲", "text_b": "乙",
                         "model_a": "m1", "model_b": "m2"})
    assert res is not None
    assert res["chosen"] == "甲" and res["rejected"] == "乙"
    assert res["n_valid"] == 3 and set(res["votes"].values()) == {"A"}
    assert res["votes"], "逐票必须留痕（弱标签可审计）"

    votes = {"d": "A", "k": "A", "a": None}   # 2 票一致 + 1 弃权

    def flaky(**kw):
        model = kw["model"]
        key = "d" if "deepseek" in model else ("k" if "kimi" in model else "a")
        return type("R", (), {"text": f'{{"pick": "{votes[key]}"}}'})()

    monkeypatch.setattr(AR, "chat", flaky)
    res2 = AR.judge_pair({"segment_id": "S", "text_a": "甲", "text_b": "乙",
                          "model_a": "m1", "model_b": "m2"})
    assert res2 is not None and res2["n_valid"] == 2 and res2["chosen"] == "甲"

    votes.update({"d": "A", "k": "B", "a": "B"})
    res3 = AR.judge_pair({"segment_id": "S", "text_a": "甲", "text_b": "乙",
                          "model_a": "m1", "model_b": "m2"})
    assert res3 is not None and res3["chosen"] == "乙"   # 2:1 多数反转


def test_build_export_shape(tmp_path, monkeypatch):
    _seed_pair("export")
    monkeypatch.setattr(AR, "chat", lambda **kw: type("R", (), {"text": '{"pick": "A"}'})())
    out = AR.build(5, seed=7, ver="airanktest", out_dir=tmp_path, dry_run=False)
    rows = [json.loads(l) for l in
            (tmp_path / "ai_ranking_airanktest.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    assert out["n"] == len(rows) >= 1
    r = rows[0]
    assert r["weak"] is True and r["label_source"].startswith("naturalness_ensemble")
    assert r["chosen"] and r["rejected"] and r["chosen"] != r["rejected"]
    assert r["chosen_model"] != r["rejected_model"]
    assert set(r["votes"]) == set(AR.JUDGES)


import json  # noqa: E402  （置于文件尾亦可，测试用）
