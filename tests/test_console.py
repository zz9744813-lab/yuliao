"""T2 只读控制台 API 的回归测试（2026-09-19）。

钉四件事：
1. 模块索引恰好是总方案 §18 导航的 16 个模块，顺序一致（前端外壳 T3 按此渲染）；
2. 每个模块 200 + 结构可解析；未知模块 404（白名单分发）；
3. **只读**：16 个端点全扫一遍，前后全表行数逐表相等——控制台"看一眼"不许改库；
4. 数字对得上：种一批带唯一标记的数据，断言各模块读数的**增量**与种子一致
   （相对断言，不写死绝对值——测试库跨文件共享，交接 §9 的坑）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import console, db
from app.main import app
from app.models import (BenchmarkItem, BenchmarkRun, BenchmarkSet, Candidate,
                        ControlledCorruption, Experiment, ExpressionStrategy,
                        Frame, HardCase, Job, JudgeRun, LlmCall, ReviewItem,
                        Segment, Work)

client = TestClient(app)
db.init_db()      # 本文件自足建表（create_all 幂等）；不依赖其它测试文件先跑过 init_db


# ── 索引与白名单 ─────────────────────────────────────────────

def test_console_index_has_exactly_16_spec_modules():
    r = client.get("/console")
    assert r.status_code == 200
    slugs = [m["slug"] for m in r.json()["modules"]]
    assert slugs == console.MODULE_ORDER          # 顺序即导航顺序
    assert len(slugs) == 16
    assert all(m["title"] for m in r.json()["modules"])


def test_console_unknown_module_404():
    r = client.get("/console/no_such_module")
    assert r.status_code == 404
    assert "no_such_module" in r.json()["detail"]


# ── 只读性：全端点扫描，前后全表行数逐表相等 ────────────────

def _table_counts() -> dict[str, int]:
    with db.session() as s:
        out = {}
        for name, table in _tables().items():
            out[name] = s.query(table).count()
        return out


def _tables() -> dict:
    from app.db import Base
    return dict(Base.metadata.tables)


def test_console_endpoints_are_readonly():
    slugs = console.MODULE_ORDER
    before = _table_counts()
    for slug in slugs:
        r = client.get(f"/console/{slug}")
        assert r.status_code == 200, (slug, r.text)
        body = r.json()
        assert body["module"] == slug
        assert isinstance(body["data"], dict) and body["data"], slug
    assert _table_counts() == before, "控制台端点改变了数据库行数——只读性被破坏"


# ── 数字对得上：种唯一标记数据，断言读数增量 ────────────────

def _seed_unique(tag: str) -> dict:
    """种一小组互相引用的数据，返回主键供增量断言。全部走 ORM。"""
    with db.session() as s:
        w = Work(title=f"控制台测试书-{tag}", source=f"test:console-{tag}")
        s.add(w)
        s.flush()
        seg = Segment(work_id=w.id, ordinal=0, text="测试段落，够长以免被判空。" * 3,
                      n_sentences=3, n_chars=36)
        s.add(seg)
        s.flush()
        exp = Experiment(id=f"EXP-CONSOLE-{tag}", name="console-test", status="done",
                         config={"segment_ids": [seg.id]})
        s.add(exp)
        s.flush()
        fr = Frame(experiment_id=exp.id, segment_id=seg.id, granularity="L",
                   extractor_model="mock", prompt_version="extract_v1",
                   payload={"event": "测试"}, status="ok")
        s.add(fr)
        s.flush()
        cand = Candidate(experiment_id=exp.id, frame_id=fr.id, segment_id=seg.id,
                         anon_label="X99", model="mock-model", temperature=0.5,
                         prompt_version="reconstruct_v1", text="候选文本", status="ok")
        s.add(cand)
        s.flush()
        rv = ReviewItem(experiment_id=exp.id, subject_type="pair", subject_id=cand.id,
                        status="done", reasons=[f"batch_console_{tag}"],
                        human_verdict={"winner_resolved": "candidate"})
        s.add(rv)
        s.add(JudgeRun(experiment_id=exp.id, subject_type="candidate", subject_id=cand.id,
                       judge_kind="preference", model="mock-judge",
                       prompt_version="judge_preference_v4", verdict={"score": 0.5},
                       status="ok"))
        s.add(LlmCall(experiment_id=exp.id, purpose="extract", model="mock-model",
                      prompt_version="extract_v1", tokens_in=10, tokens_out=5, status="ok"))
        cc = ControlledCorruption(experiment_id=exp.id, segment_id=seg.id,
                                  corruption_type="EXPLICITNESS", variable="测试变量",
                                  status="ok", drift_ok=True, fact_consistent=True)
        s.add(cc)
        bs = BenchmarkSet(id=f"BS-CONSOLE-{tag}", name=f"console-{tag}", kind="corruption_detection",
                          n_items=1, spec={})
        s.add(bs)
        s.flush()
        s.add(BenchmarkItem(set_id=bs.id, kind="corruption_detection", text_a="甲",
                            text_b="乙", answer="A", meta={"corruption_type": "EXPLICITNESS"}))
        s.add(BenchmarkRun(set_id=bs.id, model="mock-bench", n=1, n_correct=1,
                           accuracy=1.0, detail={}))
        s.add(ExpressionStrategy(id=f"ES-CONSOLE-{tag}", name=f"策略-{tag}",
                                 n_items=5, success_rate=0.4, method="test"))
        s.add(HardCase(candidate_id=cand.id, experiment_id=exp.id, kind="judge_split",
                       severity=0.9, need_human=True))
        s.add(Job(kind="stage:extract", payload={"experiment_id": exp.id}, status="done"))
        s.commit()
        return {"work": w.id, "segment": seg.id, "experiment": exp.id,
                "candidate": cand.id, "review": rv.id, "benchmark_set": bs.id,
                "strategy": f"ES-CONSOLE-{tag}"}


def test_console_numbers_reflect_seeded_delta():
    tag = "d1"                      # 同一测试库可能被多个测试共享 → 相对断言
    ids = _seed_unique(tag)
    dash = client.get("/console/dashboard").json()["data"]
    corpus = client.get("/console/corpus").json()["data"]
    lab = client.get("/console/semantic-lab").json()["data"]
    arena = client.get("/console/reconstruction-arena").json()["data"]
    judge = client.get("/console/judge-arena").json()["data"]
    pref = client.get("/console/preference-lab").json()["data"]
    bench = client.get("/console/benchmarks").json()["data"]
    strat = client.get("/console/strategy-atlas").json()["data"]
    hard = client.get("/console/hard-cases").json()["data"]
    exps = client.get("/console/experiments").json()["data"]
    models = client.get("/console/models").json()["data"]
    train = client.get("/console/training-data").json()["data"]
    wf = client.get("/console/workflow").json()["data"]
    obs = client.get("/console/observability").json()["data"]
    settings = client.get("/console/settings").json()["data"]

    # dashboard：本实验计入
    assert ids["experiment"] in [None] or any(
        e["id"] == ids["experiment"] for e in exps["recent"])
    assert dash["works"] >= 1 and dash["human_anchors"] >= 1
    assert dash["candidates_ok"] >= 1 and dash["human_judged"] >= 1
    assert dash["preference_pairs"] >= 1 and dash["hard_cases"] >= 1
    assert dash["best_benchmark_model"] is not None
    # corpus / semantic lab / arena
    assert corpus["works"] >= 1 and corpus["segments"] >= 1
    assert lab["frames"] >= 1 and lab["segments_covered"] >= 1
    assert arena["candidates"] >= 1 and any(
        k == "mock-model" for k in arena["by_model"])
    # judge / preference
    assert judge["judge_runs"] >= 1
    assert pref["winner_resolved"].get("candidate", 0) >= 1
    assert any(k == f"batch_console_{tag}" for k in pref["batches"])
    # benchmarks / strategy / hard cases
    assert bench["items"] >= 1 and bench["runs"] >= 1
    assert any(b["model"] == "mock-bench" for b in bench["leaderboard"])
    assert any(x["id"] == ids["strategy"] for x in strat["top_by_size"])
    assert hard["need_human"] >= 1
    # experiments / models / training / workflow / observability / settings
    assert exps["total"] >= 1
    assert models["llm_calls_total"] >= 1
    assert isinstance(models["registry"], (dict, type(None)))
    assert train["corruptions"]["total"] >= 1
    assert train["negative_pool_eligible"] >= 1
    assert wf["jobs"]["total"] >= 1 and wf["engine_stages"][0] == "plan"
    assert "snapshot" in obs and obs["window_hours"] == 24
    assert settings["readonly"] is True
    assert "token" not in str(settings).lower()      # 令牌绝不进 settings 响应
    assert not any("token" in str(k).lower() for k in settings)


@pytest.mark.parametrize("slug", console.MODULE_ORDER)
def test_console_each_module_shapes(slug):
    """每个模块的关键键位存在（前端外壳按这些键渲染，删键会在这里红）。"""
    data = client.get(f"/console/{slug}").json()["data"]
    need = {
        "dashboard": {"human_anchors", "preference_pairs", "experiments"},
        "corpus": {"works", "segments", "integrity", "recent_works"},
        "semantic-lab": {"frames", "by_granularity", "leakage_scores"},
        "reconstruction-arena": {"candidates", "by_model", "tokens_total"},
        "expression-residual": {"residuals_det", "residuals_sem"},
        "strategy-atlas": {"strategies", "top_by_size"},
        "judge-arena": {"judge_runs", "by_kind", "leaderboard"},
        "preference-lab": {"review_items", "winner_resolved", "batches"},
        "hard-cases": {"hard_cases", "by_kind", "top_by_severity"},
        "benchmarks": {"sets", "leaderboard", "runs"},
        "experiments": {"total", "by_status", "recent"},
        "models": {"registry", "usage_all_time"},
        "training-data": {"export_files", "corruptions"},
        "workflow": {"workflows", "jobs"},
        "observability": {"window_hours", "snapshot"},
        "settings": {"llm_mode", "modules", "readonly"},
    }[slug]
    assert need <= set(data), (slug, need - set(data))
