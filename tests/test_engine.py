"""实验引擎（任务 12）回归测试。

覆盖五条验收口径：
1. 阶段顺序与跳过：跑通全 7 阶段且按状态机顺序落 stats；
2. 续跑幂等：整套跑两遍，产品行数与阶段计数不重复累加；
3. 产品级幂等：--force 强制重跑单阶段，也不重复计数；
4. 失败传播：阶段抛错 → stats 记 failed → 原样上抛 → 下游阶段不再静默执行 →
   修复后可续跑；
5. stats 结构 + llm_calls 归账（token/失败率只能来自 llm_calls，不另记账）；
6. dry-run 零副作用（不发调用、不建数据），CLI 子进程直跑验收命令。
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

from app import corpus, db, engine, experiments
from app.main import app
from app.models import (Candidate, Experiment, Frame, JudgeRun, LlmCall,
                        ReportFile, ResidualSem, ReviewItem, Segment)

SEED_TEXT = (
    "天擦黑的时候他进了院子。院门没闩，他一推就开。屋里点着灯，人影晃了一下。\n"
    "\"回来了？\"她问。他没有答，把斗笠摘下来挂在门后。\n"
    "桌上摆着饭菜，还冒着热气。他坐下来，先喝了一口汤。汤是温的。\n"
    "她坐在对面看他的动作，始终没有再开口。窗外有虫鸣，一声长一声短。\n"
)


def _mkexp(**kw) -> str:
    db.init_db()
    with db.session() as s:
        corpus.add_work(s, title="引擎测试书", text=SEED_TEXT * 4, source="test:engine")
        exp = experiments.create_experiment(s, {
            "n_segments": 2,
            "granularities": ["S"],
            "recon_models": ["mA"],
            "temperatures": [0.5],
            "samples_per_pair": 1,
            "adversarial_k": 2,
            "concurrency": 2,
            **kw,
        })
        return exp.id


def _snapshot(exp_id: str) -> dict:
    with db.session() as s:
        return {
            "frames": s.query(Frame).filter_by(experiment_id=exp_id).count(),
            "candidates": s.query(Candidate).filter_by(experiment_id=exp_id).count(),
            "judges": s.query(JudgeRun).filter_by(experiment_id=exp_id).count(),
            "sem_residuals": s.query(ResidualSem)
                .filter(ResidualSem.candidate_id.in_(
                    [c.id for c in s.query(Candidate)
                     .filter_by(experiment_id=exp_id).all()])).count(),
            "llm_calls": s.query(LlmCall).filter_by(experiment_id=exp_id).count(),
        }


# ── ① 全流程：阶段顺序与产物 ──────────────────────────────────

def test_full_run_stage_order_and_products():
    eid = _mkexp()
    out = engine.run(eid)
    # 阶段必须按状态机顺序全部落账且 done
    assert list(out["stages"].keys()) == engine.ENGINE_STAGES
    assert all(out["stages"][n]["status"] == "done" for n in engine.ENGINE_STAGES)
    assert out["stage_order"] == engine.ENGINE_STAGES
    assert out["current_stage"] == "report"
    # mock 口径下的确定性产品数：2 段 × 1 粒度 × 2 抽取器 = 4 帧（2 primary）
    # → 2 候选 → 2×3 评委判定 + 2 段 human naturalness = 8
    snap = _snapshot(eid)
    assert snap["frames"] == 4, snap
    assert snap["candidates"] == 2, snap
    assert snap["judges"] == 8, snap
    with db.session() as s:
        kinds = {r.kind for r in s.query(ReportFile).filter_by(experiment_id=eid)}
        assert {"calibration_md", "calibration_json"} <= kinds
        # 评审队列在 judge 阶段生成（幂等 refresh），表可查
        assert s.query(ReviewItem).filter_by(experiment_id=eid).count() >= 0


# ── ② 续跑幂等：整套跑两遍不重复计数 ─────────────────────────

def test_resume_full_twice_no_double_counting():
    eid = _mkexp()
    r1 = engine.run(eid)
    snap1 = _snapshot(eid)
    rec1 = {n: dict(r1["stages"][n]) for n in engine.ENGINE_STAGES}

    r2 = engine.run(eid)          # 第二趟：全阶段应命中阶段级幂等，直接跳过
    snap2 = _snapshot(eid)

    assert snap1 == snap2, "重跑一遍不许多出任何产品行 / 调用"
    for n in engine.ENGINE_STAGES:
        for k in ("attempted", "ok", "failed", "skipped", "tokens",
                  "llm_calls", "llm_failed"):
            assert r1["stages"][n][k] == r2["stages"][n][k], (n, k)
        # 阶段级幂等：已 done 的阶段直接跳过，不重新执行（runs 不涨）
        assert r2["stages"][n]["runs"] == r1["stages"][n]["runs"], n


# ── ②b 产品级幂等：强制重跑 extract 也不重复 ─────────────────

def test_force_rerun_extract_no_duplicates():
    eid = _mkexp()
    engine.run(eid)
    before = _snapshot(eid)
    out = engine.run(eid, "extract", force=True)
    after = _snapshot(eid)
    assert before == after, "强制重跑 extract 不许新增 frame/调用"
    assert out["stages"]["extract"]["status"] == "done"
    assert out["stages"]["extract"]["skipped"] == before["frames"]


# ── ③ 失败传播 + 失败后续跑 ──────────────────────────────────

def test_failure_propagation_and_resume(monkeypatch):
    eid = _mkexp()
    monkeypatch.setitem(engine.STAGE_IMPL["extract"], "body",
                        lambda s, exp: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(engine.EngineStageError) as ei:
        engine.run(eid)
    assert "boom" in str(ei.value)
    assert isinstance(ei.value, engine.EngineError)   # CLI 按这一族捕获并退 1

    with db.session() as s:
        exp = s.get(Experiment, eid)
        rec = exp.stats["engine"]["stages"]
        assert rec["extract"]["status"] == "failed"
        assert "RuntimeError" in rec["extract"]["error"]
        assert rec["plan"]["status"] == "done"
        # 失败之后的阶段不许静默执行：下游一个都没落账
        assert "reconstruct" not in rec and "judge" not in rec and "report" not in rec
        assert exp.status == "failed"
        assert exp.error and "extract" in exp.error

    # 修复后可从失败点续跑：done 的阶段跳过，failed 的 extract 重试
    monkeypatch.undo()
    out = engine.run(eid)
    assert all(out["stages"][n]["status"] == "done" for n in engine.ENGINE_STAGES)
    assert out["stages"]["extract"]["runs"] >= 2


def test_prereq_missing_raises_not_silent():
    eid = _mkexp()
    with pytest.raises(engine.EngineError):
        engine.run(eid, "reconstruct")     # 没 frame 就要 reconstruct → 必须报错


def test_unknown_stage_rejected():
    eid = _mkexp()
    with pytest.raises(engine.EngineError):
        engine.run(eid, "plan,no_such_stage")
    # 逗号带空格的子集要能正常解析（run 的入口就是 _resolve）
    assert engine._resolve("plan, extract") == ["plan", "extract"]


# ── ④ stats 结构 + llm_calls 归账 ────────────────────────────

def test_stats_structure_and_llm_attribution():
    eid = _mkexp()
    # source_check 对已有 integrity 的段幂等跳过（不发调用）。本测试的「llm_calls
    # 归账 > 0」断言不能依赖「create_experiment 恰好抽到没校勘过的段」——全库采样
    # 的选中段在其它测试文件写过 integrity 时（跨文件耦合）会全部跳过、静默归零。
    # 显式清空本实验选中段的 integrity，保证 source_check 必然产生真实调用。
    with db.session() as s:
        ids = s.get(Experiment, eid).config["segment_ids"]
        for seg in s.query(Segment).filter(Segment.id.in_(ids)).all():
            seg.integrity = None
        s.commit()
    out = engine.run(eid)
    need = {"status", "attempted", "ok", "failed", "skipped", "seconds",
            "tokens", "llm_calls", "llm_failed", "failure_rate", "runs", "finished_at"}
    for n in engine.ENGINE_STAGES:
        rec = out["stages"][n]
        assert need <= set(rec), (n, set(rec))
        assert 0.0 <= rec["failure_rate"] <= 1.0
        assert rec["seconds"] >= 0 and rec["runs"] >= 1
        assert rec["finished_at"]

    with db.session() as s:
        rows = s.query(LlmCall).filter_by(experiment_id=eid).all()
    ext = [r for r in rows if r.purpose.startswith("extract")
           or r.purpose.startswith("propositions")]
    jdg = [r for r in rows if r.purpose.startswith("judge")]
    src = [r for r in rows if r.purpose.startswith("source_integrity")]

    # 阶段 token / 失败计数必须等于 llm_calls 按实验+purpose 聚合的结果（不另记账）
    assert out["stages"]["extract"]["llm_calls"] == len(ext) > 0
    assert out["stages"]["extract"]["tokens"] == sum(r.tokens_in + r.tokens_out for r in ext)
    assert out["stages"]["judge"]["llm_calls"] == len(jdg) > 0
    assert out["stages"]["judge"]["tokens"] == sum(r.tokens_in + r.tokens_out for r in jdg)
    assert out["stages"]["source_check"]["llm_calls"] == len(src) > 0
    # plan / report 按定义不发生 LLM 调用
    assert out["stages"]["plan"]["tokens"] == 0
    assert out["stages"]["report"]["tokens"] == 0
    # 引擎总量账 = 该实验全部调用的 token
    assert out["llm_totals"]["tokens"] == sum(r.tokens_in + r.tokens_out for r in rows)
    assert out["llm_totals"]["llm_calls"] == len(rows)


# ── ⑤ dry-run：零调用、零写入 ────────────────────────────────

def test_plan_run_dry_has_no_side_effects():
    eid = _mkexp()
    before = _snapshot(eid)
    with db.session() as s:
        stats_before = s.get(Experiment, eid).stats
    plan = engine.plan_run(eid, ["plan", "judge"])
    after = _snapshot(eid)
    assert before == after, "dry-run 不许发调用/建数据"
    with db.session() as s:
        assert s.get(Experiment, eid).stats == stats_before, "dry-run 不许写 stats"
    assert set(plan["stages"]) == {"plan", "judge"}
    assert plan["stages"]["plan"]["action"] == "run"
    assert plan["stages"]["judge"]["action"] == "blocked"     # 还没有候选
    assert "reconstruct" in plan["stages"]["judge"]["reason"]


def test_plan_run_unknown_experiment():
    plan = engine.plan_run("EXP-NO-SUCH", None)
    assert plan["exists"] is False
    assert list(plan["stages"].keys()) == engine.ENGINE_STAGES


# ── ⑥ CLI（子进程直跑）───────────────────────────────────────

def test_cli_dry_run_json():
    r = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "run_experiment.py"),
         "--exp", "EXP-ENGINE-CLI", "--dry-run", "--json"],
        capture_output=True, text=True, encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"}, cwd=str(ROOT), timeout=180)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["experiment"] == "EXP-ENGINE-CLI"
    assert out["dry_run"] is True and out["exists"] is False
    assert list(out["stages"].keys()) == engine.ENGINE_STAGES


def test_cli_mock_end_to_end():
    eid = _mkexp()
    r = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "run_experiment.py"),
         "--exp", eid, "--mock", "--json"],
        capture_output=True, text=True, encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"}, cwd=str(ROOT), timeout=300)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["ok"] is True
    assert list(out["engine"]["stages"].keys()) == engine.ENGINE_STAGES
    assert all(v["status"] == "done" for v in out["engine"]["stages"].values())
    with db.session() as s:
        assert s.get(Experiment, eid).status == "done"


# ── ⑦ API 接线：响应字段兼容（只加法）+ /stages ──────────────

client = TestClient(app)


def test_api_run_and_stages():
    eid = _mkexp()
    resp = client.post(f"/experiments/{eid}/run", json={})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    # 响应字段与旧管线兼容：status/id 必在；stages 是加法
    assert data["status"] == "started" and data["id"] == eid
    assert data["stages"] == engine.ENGINE_STAGES

    for _ in range(200):                      # mock 模式秒级；轮询到收尾
        st = client.get(f"/experiments/{eid}/stages").json()
        if st["status"] in ("done", "failed"):
            break
        time.sleep(0.1)
    assert st["status"] == "done", st
    assert st["stage_order"] == engine.ENGINE_STAGES
    assert st["stages"]["report"]["status"] == "done"
    assert st["stages"]["source_check"]["status"] == "done"

    r404 = client.get("/experiments/EXP-NOPE/stages")
    assert r404.status_code == 404
    r404b = client.post("/experiments/EXP-NOPE/run", json={})
    assert r404b.status_code == 404
