"""多进程自动调度的正例与负例（`scripts/runtime_parallel.py`）。

真起子进程、真建每臂独立 SQLite 世界库，但**零真实模型调用**：合成 writer/verifier
在子进程内就地构造，不连网关、不读凭据；产物全落 pytest 临时目录，不碰任何真库。

覆盖任务书要求的五组：
  · 正例：3 个离线臂并行跑完，汇总计数正确；
  · 负例 ① 一臂预算超限只该臂失败、其它臂仍 committed；
  · 负例 ② 两臂同时写同一世界目录被拒（整轮拒跑，一个子进程都不启动）；
  · 负例 ③ 同 job_id 重放不重复计数（复用既有 outbox/幂等语义）；
  · 负例 ④ 单臂超时被回收且不影响其它臂；
  · 负例 ⑤ 汇总里缺臂即拒绝出结果。
反向验证（硬要求）：负例 ①④⑤ 各自钉死「一个臂失败绝不会被汇总成整体成功」。
"""
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import runtime_parallel as rp  # noqa: E402

WORLD = {
    "book_id": "book-p", "branch_id": "main", "revision": 0,
    "characters": {"lin": "林穗", "shen": "沈砚"},
    "facts": {"coins": {"value": 3, "visible_to": ["lin", "shen"]},
              "received": {"value": 0, "visible_to": ["lin", "shen"]},
              "lamp": {"value": False, "visible_to": ["lin", "shen"]},
              "shen.memory": {"value": "沈砚守过一个冬夜", "visible_to": ["shen"],
                              "reader_visible": False, "mutable": False}},
    "rules": ["离线合成世界：无超自然能力。", "只允许计划列出的持续状态变化。"],
}
KNOWLEDGE = {"schema_version": "scene-knowledge/1", "package_id": "offline-empty",
             "book_id": "book-p", "source_kind": "empty", "techniques": []}


def plan(scene_id="s1", revision=0):
    return {"book_id": "book-p", "branch_id": "main", "scene_id": scene_id,
            "idempotency_key": f"{scene_id}-v1", "expected_revision": revision, "pov": "lin",
            "goal": "离线合成臂：交付一件物品。", "style": "简洁。",
            "min_chars": 200, "max_chars": 1400,
            "events": [{"event_id": "deliver", "description": "交付一件物品。",
                        "changes": [{"fact": "coins", "before": 3, "after": 2},
                                    {"fact": "received", "before": 0, "after": 1}]}]}


def job(job_id, *, scene="s1", **over):
    spec = {"job_id": job_id, "arm": job_id[-1].upper(), "scene_id": scene, "plan": plan(scene),
            "knowledge": KNOWLEDGE, "world": WORLD}
    spec.update(over)
    return spec


def arm(summary, job_id):
    return next(a for a in summary["arms"] if a["job_id"] == job_id)


def read_only(db_path):
    """只读开一臂的世界库：测试自己也不许拿到写连接。"""
    with sqlite3.connect(f"{Path(db_path).as_uri()}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        return db


def call_rows(db_path):
    with read_only(db_path) as db:
        return [dict(r) for r in db.execute(
            "SELECT stage,status,error FROM calls ORDER BY rowid")]


# ── 正例 ────────────────────────────────────────────────────────────────

def test_three_offline_arms_run_in_parallel_with_exact_counts(tmp_path):
    """3 个离线臂并行跑完：每臂独立世界库、各自提交、汇总计数与实测并行度都对。"""
    jobs = [job(f"arm-{name}", scene=f"s{i + 1}",
                offline={"behavior": "commit", "delay_s": 0.4})
            for i, name in enumerate("abc")]
    summary = rp.run_parallel(jobs, max_workers=3, budget=None,
                              workdir=tmp_path / "run", timeout_s=60)

    assert summary["ok"] is True and summary["verdict"] == "accept"
    assert summary["complete"] is True and summary["rejected_at"] is None
    assert summary["jobs_submitted"] == 3 and summary["jobs_committed"] == 3
    assert summary["jobs_failed"] == 0 and summary["failed_job_ids"] == []
    assert summary["launched"] == 3
    # 每臂两次调用（writer.0 + verifier.0），离线通道不产生真实 token，如实记 0。
    assert summary["calls_total"] == 6 and summary["tokens_total"] == 0
    assert summary["wall_ms"] > 0 and summary["reclaim_problems"] == []
    # 并行度实测：峰值同时在跑的臂数落在 [2, max_workers]。
    assert summary["parallel_ok"] is True
    assert 2 <= summary["parallelism_measured"] <= 3
    assert summary["concurrency_bound_ok"] is True

    for name in "abc":
        rec = arm(summary, f"arm-{name}")
        assert rec["status"] == "committed" and rec["error"] is None
        assert rec["calls"] == 2 and rec["tokens"] == 0
        assert rec["duration_ms"] > 0 and rec["kernel_ms"] > 0
        assert rec["revision"] == 1 and rec["commit_id"] and rec["kernel_job_id"]
        assert rec["reused"] is False and rec["result_verified"] is True
        assert rec["audit_ok"] is True and rec["pending_projections"] == 0
        assert rec["pid"] and rec["exit_code"] == 0 and rec["timed_out"] is False
    # 三臂各在自己的世界目录与世界库里提交；正文也各落各的。
    dirs = {arm(summary, f"arm-{n}")["world_dir"] for n in "abc"}
    dbs = {arm(summary, f"arm-{n}")["db_path"] for n in "abc"}
    assert len(dirs) == 3 and len(dbs) == 3
    for name in "abc":
        rec = arm(summary, f"arm-{name}")
        with read_only(rec["db_path"]) as db:
            assert db.execute("SELECT COUNT(*) FROM commits").fetchone()[0] == 1
            assert db.execute("SELECT revision FROM branches").fetchone()[0] == 1
        assert Path(rec["prose_path"]).read_text(encoding="utf-8").strip()
    assert json.loads((tmp_path / "run" / "parallel-summary.json").read_text(encoding="utf-8"))["ok"] is True


def test_max_workers_serialises_when_asked(tmp_path):
    """请求串行（max_workers=1）时如实记 serial_by_request，不谎报并行。"""
    summary = rp.run_parallel([job("arm-a"), job("arm-b")], max_workers=1, budget=None,
                              workdir=tmp_path / "run", timeout_s=60)
    assert summary["ok"] is True
    assert summary["parallelism_measured"] == 1
    assert summary["parallel_ok"] is True
    assert "serial_by_request" in summary["parallel_ok_reason"]


# ── 负例 ①：一臂预算超限只该臂失败 ──────────────────────────────────────

def test_budget_exhaustion_fails_only_that_arm(tmp_path):
    """一臂 max_calls 超限即 call_budget_exhausted（同码），其它臂照常 committed。

    这是「一个臂失败不会被汇总成整体成功」的反向验证：整轮判负、失败臂 id 具名，
    而两臂好臂的提交与正文不受任何影响。
    """
    jobs = [job("arm-a", offline={"behavior": "commit", "delay_s": 0.3}),
            job("arm-b", offline={"behavior": "hard"}, budget={"max_calls": 4}),
            job("arm-c", offline={"behavior": "commit", "delay_s": 0.3})]
    summary = rp.run_parallel(jobs, max_workers=3, budget=None,
                              workdir=tmp_path / "run", timeout_s=60)

    bad = arm(summary, "arm-b")
    assert bad["status"] == "call_budget_exhausted"          # 与既有契约同码，不另起名
    assert bad["error"] == "call_budget_exhausted"
    assert bad["calls"] == 4 and bad["revision"] is None and bad["commit_id"] is None
    assert bad["result_verified"] is False
    # 已烧掉的 4 次调用如实入账（止损台账不许把失败臂记成 0 调用）。
    assert summary["calls_total"] == 8
    with read_only(bad["db_path"]) as db:
        assert db.execute("SELECT COUNT(*) FROM commits").fetchone()[0] == 0
        assert db.execute("SELECT revision FROM branches").fetchone()[0] == 0

    for name in "ac":
        good = arm(summary, f"arm-{name}")
        assert good["status"] == "committed" and good["revision"] == 1 and good["calls"] == 2
    assert summary["jobs_committed"] == 2 and summary["jobs_failed"] == 1
    assert summary["failed_job_ids"] == ["arm-b"]
    assert summary["ok"] is False and summary["verdict"] == "reject"
    assert summary["complete"] is False
    with pytest.raises(rp.ParallelFault, match="parallel_run_rejected:arm-b"):
        rp.require_ok(summary)


def test_rewrite_budget_exhaustion_also_stays_per_arm(tmp_path):
    """hard 问题烧满修稿额度 → rewrite_budget_exhausted，仍只废该臂。"""
    jobs = [job("arm-a", offline={"behavior": "hard"}, budget={"max_calls": 6}),
            job("arm-b")]
    summary = rp.run_parallel(jobs, max_workers=2, budget=None,
                              workdir=tmp_path / "run", timeout_s=60)
    assert arm(summary, "arm-a")["status"] == "rewrite_budget_exhausted:unresolved_hard_issue"
    assert arm(summary, "arm-a")["calls"] == 6
    assert arm(summary, "arm-b")["status"] == "committed"
    assert summary["ok"] is False


# ── 负例 ②：两臂同时写同一世界目录被拒 ──────────────────────────────────

def test_two_arms_sharing_a_world_directory_are_refused(tmp_path):
    """同一世界目录（含 `.`/`..` 别名）被两臂声明 → 整轮拒跑，一个子进程都不启动。

    共享世界目录意味着「每臂独立世界库」这条不变量已被打破，没法判断哪一臂拥有它，
    因此不启动子进程（而不是让两臂去争同一条 SQLite）。同批的无辜臂也一并拒跑。
    """
    shared = tmp_path / "shared"
    run = tmp_path / "run"
    jobs = [job("arm-1", world_dir=str(shared)),
            job("arm-2", world_dir=str(shared) + "/./"),
            job("arm-3")]
    summary = rp.run_parallel(jobs, max_workers=3, budget=None, workdir=run, timeout_s=60)

    assert summary["ok"] is False and summary["verdict"] == "reject"
    assert summary["rejected_at"] == "preflight" and summary["launched"] == 0
    assert len(summary["arms"]) == 3
    for rec in summary["arms"]:
        assert rec["status"] == "rejected"
        assert rec["error"].startswith("world_dir_contended:")
        assert rec["pid"] is None and rec["calls"] == 0 and rec["duration_ms"] == 0
    contested = {p["error"].split(":", 1)[1] for p in summary["rejected_jobs"]}
    assert len(contested) == 1
    assert [p["job_ids"] for p in summary["rejected_jobs"]] == [["arm-1", "arm-2"]]
    # 关键：拒跑不建任何世界库（除汇总外 run 目录里什么产物都没有）。
    assert list(run.rglob("*.sqlite")) == []
    assert not (run / "arm-1").exists() and not (run / "arm-3").exists()
    assert summary["parallel_ok"] is False
    with pytest.raises(rp.ParallelFault, match="parallel_run_rejected"):
        rp.require_ok(summary)


def test_duplicate_job_id_and_bad_spec_refuse_the_whole_submission(tmp_path):
    """重名 job_id / 未知键 / 非法标识符都在预检层响亮拒跑，不静默改写。"""
    run = tmp_path / "dup"
    summary = rp.run_parallel([job("arm-a"), job("arm-a")], max_workers=2, budget=None,
                              workdir=run, timeout_s=30)
    assert summary["rejected_at"] == "preflight"
    codes = [p["error"].split(":", 1)[0] for p in summary["rejected_jobs"]]
    # 重名 job_id 同时也撞了默认世界目录（workdir/<job_id>），两条问题都要报出来。
    assert codes == ["duplicate_job_id", "world_dir_contended"]
    assert len(summary["arms"]) == 1 and summary["arms"][0]["status"] == "rejected"
    assert list(run.rglob("*.sqlite")) == []

    summary = rp.run_parallel([job("arm-a", scene="s1", **{"extra_key": 1})],
                              max_workers=1, budget=None,
                              workdir=tmp_path / "unknown", timeout_s=30)
    assert summary["rejected_at"] == "preflight"
    assert summary["arms"][0]["error"].startswith("unknown_job_keys:")

    summary = rp.run_parallel([job("arm a!")], max_workers=1, budget=None,
                              workdir=tmp_path / "badid", timeout_s=30)
    assert summary["arms"][0]["error"] == "job_id_invalid"


def test_max_workers_above_single_source_cap_is_refused(monkeypatch, tmp_path):
    """并发上界只取 app.limits.MAX_CONCURRENCY 单一真源，越界响亮报错不静默 clamp。"""
    from app import limits
    monkeypatch.setattr(limits, "MAX_CONCURRENCY", 2)
    with pytest.raises(rp.ParallelFault, match="max_workers_above_cap:3>2"):
        rp.run_parallel([job("arm-a")], max_workers=3, budget=None,
                        workdir=tmp_path / "run", timeout_s=30)
    assert rp.run_parallel([job("arm-a"), job("arm-b")], max_workers=2, budget=None,
                           workdir=tmp_path / "ok", timeout_s=30)["ok"] is True
    with pytest.raises(rp.ParallelFault, match="max_workers_below_minimum"):
        rp.run_parallel([job("arm-a")], max_workers=0, budget=None,
                        workdir=tmp_path / "zero", timeout_s=30)


def test_workdir_conflicting_with_research_database_is_refused(tmp_path):
    """拒写真库所在的目录树：把真库目录当工作根、或往里写臂目录，两个方向都拒。"""
    from app import config
    db_dir = Path(config.DATABASE_URL[len("sqlite:///"):]).parent
    for workdir in (db_dir, db_dir / "arms"):
        with pytest.raises(rp.ParallelFault, match="workdir_conflicts_research_database"):
            rp.run_parallel([job("arm-a")], max_workers=1, budget=None,
                            workdir=workdir, timeout_s=30)
    assert list(db_dir.glob("**/runtime.sqlite")) == []


# ── 负例 ③：同 job_id 重放不重复计数 ────────────────────────────────────

def test_replaying_the_same_job_id_does_not_double_spend(tmp_path):
    """同一提交重放：复用既有 outbox/幂等收据，调用数与提交数都不再增长。"""
    run = tmp_path / "run"
    jobs = [job("arm-a", offline={"behavior": "commit", "delay_s": 0.2}),
            job("arm-b", offline={"behavior": "commit", "delay_s": 0.2})]
    first = rp.run_parallel(jobs, max_workers=2, budget=None, workdir=run, timeout_s=60)
    assert first["ok"] is True and first["calls_total"] == 4

    second = rp.run_parallel(jobs, max_workers=2, budget=None, workdir=run, timeout_s=60)
    assert second["ok"] is True and second["calls_total"] == 4
    for name in "ab":
        rec = arm(second, f"arm-{name}")
        assert rec["status"] == "committed" and rec["reused"] is True
        assert rec["calls"] == 2 and rec["projections"] == 0
        with read_only(rec["db_path"]) as db:
            assert db.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 2
            assert db.execute("SELECT COUNT(*) FROM commits").fetchone()[0] == 1
            assert db.execute("SELECT revision FROM branches").fetchone()[0] == 1
        assert rec["commit_id"] == arm(first, f"arm-{name}")["commit_id"]
        assert rec["text_hash"] == arm(first, f"arm-{name}")["text_hash"]

    # 改预算的重放属于幂等输入冲突（预算在冻结请求哈希里），必须响亮拒绝而不是再扣一次。
    changed = rp.run_parallel([job("arm-a", offline={"behavior": "commit", "delay_s": 0.2},
                                   budget={"max_calls": 8})],
                              max_workers=1, budget=None, workdir=run, timeout_s=60)
    assert arm(changed, "arm-a")["status"] == "idempotency_input_conflict"
    assert arm(changed, "arm-a")["calls"] == 2


# ── 负例 ④：单臂超时被回收 ──────────────────────────────────────────────

def test_timed_out_arm_is_reclaimed_and_others_survive(tmp_path):
    """一臂挂死超时：强杀 + 回收（目录锁可独占取得），其它臂照常提交。"""
    jobs = [job("arm-a", offline={"behavior": "hang", "delay_s": 60}),
            job("arm-b", offline={"behavior": "commit", "delay_s": 0.2}),
            job("arm-c", offline={"behavior": "commit", "delay_s": 0.2})]
    summary = rp.run_parallel(jobs, max_workers=3, budget=None,
                              workdir=tmp_path / "run", timeout_s=4)

    hung = arm(summary, "arm-a")
    assert hung["status"] == "timeout" and hung["timed_out"] is True
    assert hung["reclaimed"] is True and summary["reclaim_problems"] == []
    assert hung["result_verified"] is False and hung["duration_ms"] <= 20000
    assert rp._arm_lock_free(Path(hung["world_dir"]) / rp.LOCK_NAME) is True
    # 挂死前已预留的调用留在账上，且是 dispatched（未知结果）——不自动重派。
    stages = call_rows(hung["db_path"])
    assert stages and stages[0]["status"] == "dispatched"
    assert any(row["status"] in ("dispatched", "unknown") for row in stages)

    for name in "bc":
        good = arm(summary, f"arm-{name}")
        assert good["status"] == "committed" and good["revision"] == 1
    assert summary["jobs_committed"] == 2 and summary["failed_job_ids"] == ["arm-a"]
    assert summary["ok"] is False and summary["verdict"] == "reject"
    with pytest.raises(rp.ParallelFault, match="parallel_run_rejected:arm-a"):
        rp.require_ok(summary)


def test_hard_crash_without_result_file_is_reported_as_missing(tmp_path):
    """子进程硬崩（无 result.json）→ 该臂记 missing，不许替它编造成功。"""
    jobs = [job("arm-a", offline={"behavior": "crash"}), job("arm-b")]
    summary = rp.run_parallel(jobs, max_workers=2, budget=None,
                              workdir=tmp_path / "run", timeout_s=60)
    crashed = arm(summary, "arm-a")
    assert crashed["status"] == "missing" and crashed["error"] == "arm_result_file_absent"
    assert crashed["exit_code"] == 97 and crashed["timed_out"] is False
    assert crashed["result_verified"] is False
    assert arm(summary, "arm-b")["status"] == "committed"
    assert summary["ok"] is False and summary["complete"] is False


# ── 负例 ⑤：汇总里缺臂即拒绝出结果 ──────────────────────────────────────

def test_summary_with_a_missing_arm_refuses_to_be_built(tmp_path):
    """覆盖完整性是硬校验：少一臂就抛，绝不给一份「只跑了一半」的汇总。"""
    records = [{"job_id": "arm-a", "status": "committed", "calls": 2, "tokens": 0,
                "duration_ms": 5, "result_verified": True}]
    with pytest.raises(rp.ParallelFault, match="arm_result_missing:arm-b"):
        rp.assemble_summary(submitted=["arm-a", "arm-b"], records=records, wall_ms=5,
                            max_workers=2, timeout_s=1.0, workdir=tmp_path)
    with pytest.raises(rp.ParallelFault, match="duplicate_arm_record:arm-a"):
        rp.assemble_summary(submitted=["arm-a"], records=records * 2, wall_ms=5,
                            max_workers=1, timeout_s=1.0, workdir=tmp_path)
    with pytest.raises(rp.ParallelFault, match="unsubmitted_arm_record:arm-z"):
        rp.assemble_summary(submitted=["arm-a"],
                            records=records + [{"job_id": "arm-z", "status": "committed"}],
                            wall_ms=5, max_workers=1, timeout_s=1.0, workdir=tmp_path)
    summary = rp.assemble_summary(submitted=["arm-a"], records=records, wall_ms=5,
                                 max_workers=1, timeout_s=1.0, workdir=tmp_path)
    assert summary["ok"] is True and summary["complete"] is True
    assert rp.require_ok(summary) is summary


def test_require_ok_rejects_any_single_failed_arm(tmp_path):
    """反向验证：汇总里只要有一臂没提交，require_ok 就抛——不给「整体成功」留缝。"""
    jobs = [job("arm-a", offline={"behavior": "hard"}, budget={"max_calls": 2}),
            job("arm-b"), job("arm-c")]
    summary = rp.run_parallel(jobs, max_workers=3, budget=None,
                              workdir=tmp_path / "run", timeout_s=60)
    assert arm(summary, "arm-a")["status"] == "call_budget_exhausted"
    assert summary["ok"] is False
    assert summary["verdict"] == "reject"
    assert summary["jobs_committed"] == 2
    with pytest.raises(rp.ParallelFault):
        rp.require_ok(summary)
    # 好臂的提交不受同批失败臂牵连。
    with read_only(arm(summary, "arm-c")["db_path"]) as db:
        assert db.execute("SELECT COUNT(*) FROM commits").fetchone()[0] == 1


# ── 离线保证与文档入口 ─────────────────────────────────────────────────

def test_no_real_model_channel_is_reachable_without_explicit_opt_in(tmp_path):
    """live 网关通道需两个独立显式开关：默认合成通道，且本轮交付未跑过真跑。"""
    assert "LG_RUNTIME_PARALLEL_LIVE" not in os.environ
    spec = {"mode": "gateway", "writer_model": "x", "verifier_model": "y"}
    with pytest.raises(Exception) as excinfo:
        rp._build_client(spec)
    assert "gateway_channel_requires_explicit_live_opt_in" in str(excinfo.value)
    client = rp._build_client({"mode": "synthetic", "behavior": "commit"})
    assert client.models["transport"] == "offline-synthetic/1"
    reply = client.invoke(role="writer", system="s", payload={"plan": plan()},
                          max_tokens=100, timeout=1.0)
    assert reply["tokens_in"] is None and reply["tokens_out"] is None
    assert 200 <= len(json.loads(reply["text"])["text"]) <= 1400


def test_cli_runs_a_two_arm_batch_and_writes_the_summary(tmp_path):
    """文档里写的入口可用：--jobs/--out 跑两臂，退出码与汇总一致。"""
    jobs_path = tmp_path / "jobs.json"
    jobs_path.write_text(json.dumps({"budget": {"max_calls": 6},
                                     "jobs": [job("arm-a"), job("arm-b")]}),
                         encoding="utf-8")
    code = rp.main(["--jobs", str(jobs_path), "--out", str(tmp_path / "run"),
                    "--max-workers", "2", "--timeout-s", "60"])
    assert code == 0
    summary = json.loads((tmp_path / "run" / "parallel-summary.json").read_text(encoding="utf-8"))
    assert summary["ok"] is True and summary["jobs_committed"] == 2
    assert summary["calls_total"] == 4 and summary["max_workers"] == 2

    jobs_path.write_text(json.dumps([job("arm-a", offline={"behavior": "hard"},
                                         budget={"max_calls": 2})]), encoding="utf-8")
    assert rp.main(["--jobs", str(jobs_path), "--out", str(tmp_path / "run2"),
                    "--max-workers", "1", "--timeout-s", "60"]) == 1
