"""大规模 Dream 的正例与负例（`scripts/dream_scale.py`）。

一次提交 N 场 × 双臂，**真起子进程**、**真建每臂独立 SQLite 世界库**，但零真实模型
调用：合成 writer/verifier 在臂子进程内就地构造，不连网关、不读凭据（`tokens_total`
恒为 0）。产物全落 pytest 临时目录，不碰任何真库（真库护栏复用调度层既有判定）。

覆盖任务书要求的六组负例：
  · 缺臂（批次矩阵少一臂 ⇒ 连汇总都造不出来）；
  · 预算耗尽（单臂硬闸 ⇒ 只记该臂、整批判负、已烧调用照实入账）；
  · 超时（强杀 + 回收 ⇒ 整批判负）；
  · 重放（同一批次重放 ⇒ 不再烧调用，收据复用）；
  · 越界并发（上界只取 `app.limits.MAX_CONCURRENCY` 单一真源，响亮报错）；
  · 真模型通道未开双开关即拒（`offline_mode="gateway"` + `LG_DREAM_SCALE_LIVE=1`）。
反向验证（硬要求）：`test_one_failing_scene_rejects_the_whole_batch` 与
`test_scale_summary_never_claims_ok_with_an_incomplete_scene` 钉死
「一场失败 / 一臂缺结果 ⇒ 整批 verdict=reject、ok=false」。
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

import dream_scale as ds  # noqa: E402
import runtime_parallel as rp  # noqa: E402


# ── 装配（真跑世界与计划一律取自脚本本体，不另造第二套） ────────────────────

def arm(summary, job_id):
    return next(a for a in summary["arms"] if a["job_id"] == job_id)


def scene(summary, scene_id):
    return next(s for s in summary["scene_matrix"] if s["scene_id"] == scene_id)


def read_only(db_path):
    """只读开一臂的世界库：测试自己也不许拿到写连接。"""
    with sqlite3.connect(f"{Path(db_path).as_uri()}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        return db


def fake_arm(job_id, arm_name, scene_name, *, status="committed", **over):
    """仅供汇总层单测用的臂记录（不启动任何子进程）。"""
    record = {"job_id": job_id, "arm": arm_name, "scene_id": scene_name,
              "world_dir": f"/tmp/{job_id}", "db_path": f"/tmp/{job_id}/runtime.sqlite",
              "prose_path": f"/tmp/{job_id}/prose.md", "pid": 4242, "exit_code":
              0 if status == "committed" else 2, "timed_out": False, "reclaimed": None,
              "wall_ms": 1200, "kernel_ms": 900, "status": status, "calls": 2, "tokens": 0,
              "call_duration_ms": 40, "revision": 1 if status == "committed" else None,
              "commit_id": "commit-x" if status == "committed" else None,
              "text_hash": "a" * 64, "reused": False,
              "result_verified": status == "committed",
              "error": None if status == "committed" else status,
              "stdout_tail": "", "stderr_tail": ""}
    record.update(over)
    return record


def fake_base(records, **over):
    base = {"schema": rp.SCHEMA, "runtime_version": "scene-pilot/1", "workdir": "/tmp/run",
            "wall_ms": 3000, "max_workers": 2, "timeout_s": 30.0,
            "jobs_submitted": len(records), "jobs_committed": sum(
                1 for r in records if r["status"] == "committed"),
            "jobs_failed": sum(1 for r in records if r["status"] != "committed"),
            "call_duration_ms_total": 80, "parallelism_measured": 2, "parallel_ok": True,
            "parallel_ok_reason": "measured=2,expected=2,floor=2", "concurrency_bound_ok": True,
            "reclaim_problems": [], "rejected_at": None, "rejected_jobs": [],
            "launched": len(records), "arms": records}
    base.update(over)
    return base


def scale_summary(base, matrix, **over):
    kwargs = {"matrix": matrix, "batch_id": "batch-test", "book_id": "dream-scale-offline",
              "workdir": Path("/tmp/run"), "wall_ms": 3000, "max_workers": 2, "timeout_s": 30.0,
              "max_batch_calls": 200, "calls_cap_max": 40, "offline_mode": "synthetic",
              "arm_names": ["A", "B"]}
    kwargs.update(over)
    return ds.assemble_scale_summary(base, **kwargs)


# ── 正例：十场 × 双臂的规模证据 ───────────────────────────────────────────

def test_ten_scenes_x_two_arms_commits_twenty_arms_with_scale_evidence(tmp_path):
    """一次提交 10 场 × 双臂：20 臂全部提交，场数/调用台账/耗时/实测并发逐项对上。

    这是「大规模」的规模证据本体：零真实模型调用（tokens 恒 0），每臂独立子进程 +
    独立世界目录 + 独立世界库，实测峰值并发 ≥ 2。
    """
    summary = ds.run_scale(out=tmp_path / "run", scenes=10, max_workers=8, arm_delay_s=0.15,
                           max_batch_calls=80)

    assert summary["ok"] is True and summary["verdict"] == "accept"
    assert summary["complete"] is True and summary["coverage_ok"] is True
    assert summary["rejected_at"] is None and summary["rejected_jobs"] == []
    assert summary["scenes"] == 10 and summary["arms_per_scene"] == ["A", "B"]
    assert summary["jobs_submitted"] == 20 and summary["jobs_committed"] == 20
    assert summary["jobs_failed"] == 0 and summary["failed_job_ids"] == []
    assert summary["scenes_committed"] == 10
    # 每臂正好 2 次调用（writer.0 + verifier.0），离线通道不产生真实 token。
    assert summary["calls_total"] == 40 and summary["tokens_total"] == 0
    assert summary["reused_jobs"] == []
    # 整批调用上限硬闸：结构上限 10×2×4=80 恰好等于批次上限，跑完台账落在闸内。
    assert summary["calls_cap"] == {"max_batch_calls": 80, "planned_max": 80,
                                    "spent": 40, "ok": True}
    # 墙钟与实测并发。
    assert summary["wall_ms"] > 0 and summary["arms_wall_ms"] > 0
    assert summary["parallel_ok"] is True
    assert 2 <= summary["parallelism_measured"] <= 8
    assert summary["concurrency_bound_ok"] is True
    assert summary["launched"] == 20 and summary["reclaim_problems"] == []
    assert summary["throughput_offline"]["arms_per_minute"] > 0
    assert summary["live_channel_enabled"] is False and summary["offline_mode"] == "synthetic"

    # 逐场逐臂：10 场 × 2 臂，全部 committed，各自带提交收据与独立世界库。
    assert len(summary["scene_matrix"]) == 10
    dirs, dbs, commits, hashes = set(), set(), 0, []
    for index in range(1, 11):
        row = scene(summary, f"scene-{index:03d}")
        assert row["scene_ok"] is True and row["expected_arms"] == 2
        assert row["committed_arms"] == 2 and row["calls"] == 4
        for entry in row["arms"]:
            assert entry["status"] == "committed" and entry["error"] is None
            assert entry["calls"] == 2 and entry["tokens"] == 0
            assert entry["revision"] == 1 and entry["commit_id"] and entry["text_hash"]
            assert entry["result_verified"] is True and entry["reused"] is False
            assert entry["pid"] and entry["exit_code"] == 0 and entry["timed_out"] is False
            assert entry["wall_ms"] > 0 and entry["kernel_ms"] > 0
            assert Path(entry["prose_path"]).read_text(encoding="utf-8").strip()
            dirs.add(entry["world_dir"])
            dbs.add(entry["db_path"])
            hashes.append((entry["scene_id"], entry["arm"], entry["text_hash"]))
            with read_only(entry["db_path"]) as db:
                commits += db.execute("SELECT COUNT(*) FROM commits").fetchone()[0]
                assert db.execute("SELECT revision FROM branches").fetchone()[0] == 1
    assert len(dirs) == 20 and len(dbs) == 20 and commits == 20
    # 同场两臂是同题两稿：正文哈希不同，才谈得上逐臂归因。
    for index in range(1, 11):
        pair = {h[2] for h in hashes if h[0] == f"scene-{index:03d}"}
        assert len(pair) == 2

    written = json.loads((tmp_path / "run" / ds.SUMMARY_NAME).read_text(encoding="utf-8"))
    assert written["batch_id"] == summary["batch_id"] and written["verdict"] == "accept"
    assert written["jobs_committed"] == 20
    jobs_doc = json.loads((tmp_path / "run" / ds.JOBS_NAME).read_text(encoding="utf-8"))
    assert jobs_doc["batch_id"] == summary["batch_id"] and len(jobs_doc["jobs"]) == 20
    assert (tmp_path / "run" / rp.SUMMARY_NAME).exists()


def test_serial_request_records_serial_by_request(tmp_path):
    """单场单臂（并发期望 ≤1）如实记 serial_by_request，不谎报并行。"""
    summary = ds.run_scale(out=tmp_path / "run", scenes=1, arms=["A"], max_workers=4,
                           arm_delay_s=0.05)
    assert summary["ok"] is True and summary["verdict"] == "accept"
    assert summary["scenes"] == 1 and summary["jobs_submitted"] == 1
    assert summary["calls_total"] == 2
    assert summary["parallelism_measured"] == 1
    assert summary["parallel_ok"] is True
    assert "serial_by_request" in summary["parallel_ok_reason"]


# ── 负例①：缺臂 ⇒ 连汇总都造不出来 ───────────────────────────────────────

def test_a_missing_arm_refuses_to_build_the_scale_summary(tmp_path):
    """批次矩阵少一臂 ⇒ 抛 `scene_arm_missing`：宁可不给规模结论。

    「跑了一半当全跑完」比崩溃更危险，所以批次层对覆盖率做硬校验：少一臂、重复记录、
    越权记录都直接抛。
    """
    matrix = [{"index": 1, "scene_id": "scene-001", "idempotency_key": "scene-001-v1",
               "arms": [{"arm": "A", "job_id": "scene-001-armA"},
                        {"arm": "B", "job_id": "scene-001-armB"}]}]
    one = fake_arm("scene-001-armA", "A", "scene-001")

    with pytest.raises(ds.DreamFault, match="scene_arm_missing:scene-001-armB"):
        scale_summary(fake_base([one]), matrix)
    with pytest.raises(ds.DreamFault, match="duplicate_arm_record:scene-001-armA"):
        scale_summary(fake_base([one, dict(one)]), matrix)
    with pytest.raises(ds.DreamFault, match="unsubmitted_arm_record:scene-009-armZ"):
        scale_summary(fake_base([one, fake_arm("scene-009-armZ", "Z", "scene-009")]), matrix)

    # 预检阶段整批拒跑（一个子进程都没启动）不算「缺臂」：如实记 not_launched。
    rejected = fake_base([one], rejected_at="preflight",
                         parallel_ok=False, parallel_ok_reason="rejected_before_launch",
                         rejected_jobs=[{"index": 1, "error": "duplicate_job_id:x"}])
    summary = scale_summary(rejected, matrix)
    assert summary["ok"] is False and summary["verdict"] == "reject"
    assert summary["rejected_at"] == "preflight" and summary["launched"] == 1
    lost = arm(summary, "scene-001-armB")
    assert lost["status"] == "not_launched" and lost["error"] == "rejected_before_launch"
    assert lost["calls"] == 0 and lost["result_verified"] is False
    with pytest.raises(ds.DreamFault, match="dream_scale_rejected:scene-001-armB"):
        ds.require_ok(summary, parallel=False)


def test_scale_summary_never_claims_ok_with_an_incomplete_scene():
    """汇总层（不起子进程）：任一臂不是 committed ⇒ 该场不全 ⇒ 整批 reject。"""
    matrix = [{"index": index, "scene_id": f"scene-{index:03d}",
               "idempotency_key": f"scene-{index:03d}-v1",
               "arms": [{"arm": "A", "job_id": f"scene-{index:03d}-armA"},
                        {"arm": "B", "job_id": f"scene-{index:03d}-armB"}]}
              for index in (1, 2)]
    records = [fake_arm(f"scene-{index:03d}-arm{arm}", arm, f"scene-{index:03d}")
               for index in (1, 2) for arm in ("A", "B")]
    ok_summary = scale_summary(fake_base(records), matrix)
    assert ok_summary["ok"] is True and ok_summary["verdict"] == "accept"
    assert ok_summary["scenes"] == 2 and ok_summary["scenes_committed"] == 2
    assert ds.require_ok(ok_summary) is ok_summary

    records[-1] = fake_arm("scene-002-armB", "B", "scene-002", status="missing",
                           error="arm_result_file_absent", result_verified=False,
                           calls=0, exit_code=97, text_hash=None)
    bad = scale_summary(fake_base(records), matrix)
    assert bad["ok"] is False and bad["verdict"] == "reject" and bad["complete"] is False
    assert scene(bad, "scene-002")["scene_ok"] is False
    assert bad["failed_arms"] == [{"scene_id": "scene-002", "arm": "B",
                                   "job_id": "scene-002-armB", "status": "missing",
                                   "error": "arm_result_file_absent", "calls": 0}]
    with pytest.raises(ds.DreamFault, match="dream_scale_rejected:scene-002-armB"):
        ds.require_ok(bad)


# ── 负例②：一场失败 ⇒ 整批判负（反向验证） ─────────────────────────────────

def test_one_failing_scene_rejects_the_whole_batch(tmp_path):
    """第 3 场双臂预算烧尽 ⇒ 只记该场两臂，但**整批** ok=false / verdict=reject。

    这是硬要求的反向验证：一场失败不会被汇总成整体成功；其余三场的好臂提交不受牵连，
    失败臂已烧掉的调用照实入账（止损台账不许把失败臂记成 0 调用）。
    """
    summary = ds.run_scale(out=tmp_path / "run", scenes=4, max_workers=8, arm_delay_s=0.1,
                           scene_overrides={3: {"offline": {"behavior": "hard"},
                                                "budget": {"max_calls": 4}}})

    assert summary["ok"] is False and summary["verdict"] == "reject"
    assert summary["complete"] is False
    assert summary["jobs_submitted"] == 8 and summary["jobs_committed"] == 6
    assert summary["jobs_failed"] == 2
    assert summary["failed_job_ids"] == ["scene-003-armA", "scene-003-armB"]
    assert summary["scenes_committed"] == 3
    # 失败原因原文（内核同码，不另起名）+ 已烧掉的 4 次调用。
    assert summary["failed_arms"] == [
        {"scene_id": "scene-003", "arm": "A", "job_id": "scene-003-armA",
         "status": "call_budget_exhausted", "error": "call_budget_exhausted", "calls": 4},
        {"scene_id": "scene-003", "arm": "B", "job_id": "scene-003-armB",
         "status": "call_budget_exhausted", "error": "call_budget_exhausted", "calls": 4}]
    assert summary["calls_total"] == 6 * 2 + 2 * 4
    assert summary["calls_cap"]["ok"] is True
    # 失败场两臂都不落提交，好场四场共 6 臂照常提交在各自世界库里。
    for entry in scene(summary, "scene-003")["arms"]:
        assert entry["revision"] is None and entry["commit_id"] is None
        assert entry["result_verified"] is False
        with read_only(entry["db_path"]) as db:
            assert db.execute("SELECT COUNT(*) FROM commits").fetchone()[0] == 0
            assert db.execute("SELECT revision FROM branches").fetchone()[0] == 0
    for index in (1, 2, 4):
        assert scene(summary, f"scene-{index:03d}")["scene_ok"] is True
    with pytest.raises(ds.DreamFault, match="dream_scale_rejected:scene-003-armA"):
        ds.require_ok(summary, parallel=False)


def test_per_arm_budget_exhaustion_is_recorded_only_for_that_arm(tmp_path):
    """单臂预算硬闸只废该臂：同场另一臂与第二场照常提交，整批仍判负。"""
    summary = ds.run_scale(out=tmp_path / "run", scenes=2, max_workers=4, arm_delay_s=0.1,
                           scene_overrides={1: {"arms": {"A": {"budget": {"max_calls": 3},
                                                             "offline": {"behavior": "hard"}}}}})
    burned = scene(summary, "scene-001")["arms"]
    assert [entry["status"] for entry in burned] == ["call_budget_exhausted", "committed"]
    assert burned[0]["calls"] == 3 and burned[1]["calls"] == 2
    assert scene(summary, "scene-002")["scene_ok"] is True
    assert summary["jobs_committed"] == 3 and summary["scenes_committed"] == 1
    assert summary["failed_job_ids"] == ["scene-001-armA"]
    assert summary["ok"] is False and summary["verdict"] == "reject"
    assert summary["calls_total"] == 3 + 2 + 4


def test_hard_crash_marks_its_scene_incomplete_and_rejects_the_batch(tmp_path):
    """子进程硬崩（无 result.json）⇒ 该臂 missing，该场不全，整批 reject，父进程不编结果。"""
    summary = ds.run_scale(out=tmp_path / "run", scenes=2, max_workers=4, arm_delay_s=0.05,
                           scene_overrides={1: {"arms": {"A": {"offline": {"behavior": "crash"}}}}})
    crashed = arm(summary, "scene-001-armA")
    assert crashed["status"] == "missing" and crashed["error"] == "arm_result_file_absent"
    assert crashed["exit_code"] == 97 and crashed["result_verified"] is False
    assert arm(summary, "scene-001-armB")["status"] == "committed"
    assert scene(summary, "scene-001")["scene_ok"] is False
    assert scene(summary, "scene-002")["scene_ok"] is True
    assert summary["ok"] is False and summary["verdict"] == "reject"
    assert summary["complete"] is False and summary["jobs_committed"] == 3


# ── 负例③：整批调用上限硬闸 ──────────────────────────────────────────────

def test_batch_call_cap_refuses_the_whole_batch_before_any_launch(tmp_path):
    """批次上限 `场×臂×max_calls` 超限 ⇒ 整批拒跑，一个子进程都不启动、零 SQLite。

    越界响亮报错不静默放大（费用护栏，意图错位比失败危险）；恰好等于结构上限则放行。
    """
    out = tmp_path / "run"
    with pytest.raises(ds.DreamFault, match="batch_call_cap_exceeded:80>40"):
        ds.run_scale(out=out, scenes=10, max_workers=8, max_batch_calls=40)
    assert not out.exists()
    with pytest.raises(ds.DreamFault, match="max_batch_calls_below_minimum"):
        ds.run_scale(out=out, scenes=1, max_batch_calls=0)

    exact = ds.run_scale(out=out, scenes=10, max_workers=8, arm_delay_s=0.05,
                         max_batch_calls=80)
    assert exact["ok"] is True
    assert exact["calls_cap"] == {"max_batch_calls": 80, "planned_max": 80,
                                  "spent": 40, "ok": True}


def test_require_ok_refuses_a_batch_that_overspent_its_cap():
    """台账越上限（理论上被结构上界堵死）时判定函数仍必抛，不放行。"""
    summary = {"ok": True, "parallel_ok": True, "reclaim_problems": [],
               "calls_cap": {"max_batch_calls": 10, "planned_max": 10, "spent": 12, "ok": False}}
    with pytest.raises(ds.DreamFault, match="batch_call_cap_exceeded_post_run:12>10"):
        ds.require_ok(summary)


# ── 负例④：同一批次重放不重复烧调用 ──────────────────────────────────────

def test_replaying_the_same_batch_does_not_burn_calls_again(tmp_path):
    """同一 out + 同一矩阵重放：收据复用（`reused=true`），调用台账不再增长。

    批次身份由作业矩阵摘要给出（`batch_id` 相同即同一批次）；逐臂幂等仍由内核既有
    outbox/冻结请求哈希执行——改预算的重放属幂等输入冲突，必须响亮拒绝而不是再扣一次。
    """
    out = tmp_path / "run"
    first = ds.run_scale(out=out, scenes=3, max_workers=4, arm_delay_s=0.05)
    assert first["ok"] is True and first["calls_total"] == 12

    second = ds.run_scale(out=out, scenes=3, max_workers=4, arm_delay_s=0.05)
    assert second["batch_id"] == first["batch_id"]
    assert second["ok"] is True and second["calls_total"] == 12
    assert len(second["reused_jobs"]) == 6 and second["jobs_committed"] == 6
    for record in second["arms"]:
        assert record["reused"] is True and record["calls"] == 2 and record["revision"] == 1
        with read_only(record["db_path"]) as db:
            assert db.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 2
            assert db.execute("SELECT COUNT(*) FROM commits").fetchone()[0] == 1
        assert record["commit_id"] == arm(first, record["job_id"])["commit_id"]
        assert record["text_hash"] == arm(first, record["job_id"])["text_hash"]


# ── 负例⑤：单臂超时被回收 ──────────────────────────────────────────────

def test_timed_out_arm_is_reclaimed_and_rejects_the_batch(tmp_path):
    """一臂挂死超时：强杀 + 回收（目录锁可独占取得），其它臂照常提交，整批判负。"""
    summary = ds.run_scale(out=tmp_path / "run", scenes=2, max_workers=4, timeout_s=4,
                           scene_overrides={1: {"arms": {"A": {"offline": {"behavior": "hang",
                                                                           "delay_s": 60}}}}})
    hung = arm(summary, "scene-001-armA")
    assert hung["status"] == "timeout" and hung["timed_out"] is True
    assert hung["error"] == "arm_result_file_absent"
    assert hung["reclaimed"] is True and summary["reclaim_problems"] == []
    assert hung["result_verified"] is False
    assert rp._arm_lock_free(Path(hung["world_dir"]) / rp.LOCK_NAME) is True
    # 同场另一臂与第二场照常提交（臂级失败不外溢）。
    assert arm(summary, "scene-001-armB")["status"] == "committed"
    assert scene(summary, "scene-002")["scene_ok"] is True
    assert scene(summary, "scene-001")["scene_ok"] is False
    assert summary["ok"] is False and summary["verdict"] == "reject"
    assert summary["failed_job_ids"] == ["scene-001-armA"]
    with pytest.raises(ds.DreamFault, match="dream_scale_rejected:scene-001-armA"):
        ds.require_ok(summary, parallel=False)


# ── 负例⑥：越界并发 / 非法规模参数（批次级，启动前拒跑） ──────────────────

def test_concurrency_above_single_source_cap_is_refused(monkeypatch, tmp_path):
    """并发上界只取 `app.limits.MAX_CONCURRENCY` 单一真源，越界响亮报错不静默 clamp。"""
    from app import limits
    monkeypatch.setattr(limits, "MAX_CONCURRENCY", 2)
    out = tmp_path / "run"
    with pytest.raises(rp.ParallelFault, match="max_workers_above_cap:8>2"):
        ds.run_scale(out=out, scenes=10, max_workers=8)
    assert not out.exists()
    with pytest.raises(rp.ParallelFault, match="max_workers_below_minimum"):
        ds.run_scale(out=out, scenes=1, max_workers=0)
    assert ds.run_scale(out=out, scenes=2, max_workers=2, arm_delay_s=0.05)["ok"] is True


def test_scenes_and_arms_out_of_range_are_refused(tmp_path):
    """场数/臂名/逐场覆盖/通道模式/墙钟/书号任一非法 ⇒ 批次级拒跑，零产物。"""
    out = tmp_path / "run"
    cases = [
        ({"scenes": 0}, "scenes_below_minimum:0<1"),
        ({"scenes": ds.MAX_SCENES + 1}, f"scenes_above_cap:{ds.MAX_SCENES + 1}>{ds.MAX_SCENES}"),
        ({"scenes": 1.5}, "scenes_not_an_integer"),
        ({"arms": []}, "arms_below_minimum:0<1"),
        ({"arms": ["A", "A"]}, "duplicate_arm_name"),
        ({"arms": ["a b"]}, "arm_invalid:a b"),
        ({"arms": ["A", "B", "C", "D", "E"]}, "arms_above_cap:5>4"),
        ({"scenes": 2, "scene_overrides": {"9": {"budget": {"max_calls": 2}}}},
         "scene_override_out_of_range:9:not_in_1..2"),
        ({"scenes": 2, "scene_overrides": {1: {"nope": 1}}}, "unknown_scene_override_1_keys:nope"),
        ({"offline_mode": "bogus"}, "unknown_offline_mode:bogus"),
        ({"timeout_s": 0}, "timeout_not_positive"),
        ({"arm_delay_s": -1}, "arm_delay_not_positive"),
        ({"book_id": "bad id"}, "book_id_invalid"),
        ({"budget": {"nope": 1}}, "unknown_budget_keys:nope"),
    ]
    for kwargs, message in cases:
        with pytest.raises(rp.ParallelFault, match=message):
            ds.run_scale(out=out, **kwargs)
    assert not out.exists()


def test_workdir_conflicting_with_research_database_is_refused(tmp_path):
    """真库护栏复用调度层既有判定：把真库目录当工作根、或往里写批次目录，两个方向都拒。"""
    from app import config
    db_dir = Path(config.DATABASE_URL[len("sqlite:///"):]).parent
    for workdir in (db_dir, db_dir / "dream-scale"):
        with pytest.raises(rp.ParallelFault, match="workdir_conflicts_research_database"):
            ds.run_scale(out=workdir, scenes=1, max_workers=2)
    assert list(db_dir.glob("**/dream-scale-summary.json")) == []


# ── 真模型通道：双开关且默认关闭 ──────────────────────────────────────────

def test_live_channel_needs_both_explicit_switches(monkeypatch, tmp_path):
    """真模型通道必须两个独立显式开关；缺任一都拒，且全程零网络。"""
    assert "LG_DREAM_SCALE_LIVE" not in os.environ
    assert "LG_RUNTIME_PARALLEL_LIVE" not in os.environ
    models = {"writer_model": "m-writer", "verifier_model": "m-verifier"}
    out = tmp_path / "closed"

    # 开关一（`offline_mode="gateway"` 声明）有、开关二（`LG_DREAM_SCALE_LIVE=1`）无
    # ⇒ 批次级拒跑：一个子进程都不启动，零 SQLite。
    with pytest.raises(ds.DreamFault, match="gateway_channel_requires_explicit_live_opt_in"):
        ds.run_scale(out=out, scenes=1, offline_mode="gateway", offline_overrides=models)
    assert not out.exists()
    # 默认（未声明 gateway）不读任何凭据、零真实调用：合成通道 tokens 恒 0。
    offline = ds.run_scale(out=out, scenes=1, max_workers=2, arm_delay_s=0.05)
    assert offline["ok"] is True and offline["tokens_total"] == 0
    assert offline["offline_mode"] == "synthetic" and offline["live_channel_enabled"] is False

    # 两个开关都开、但没声明 writer/verifier 模型 ⇒ 仍然批次级拒跑。
    monkeypatch.setenv("LG_DREAM_SCALE_LIVE", "1")
    with pytest.raises(ds.DreamFault, match="gateway_channel_requires_model_declaration"):
        ds.run_scale(out=out, scenes=1, offline_mode="gateway",
                     offline_overrides={"writer_model": "m-writer"})

    # 两个开关都开且声明齐了 ⇒ 批次放行，但臂子进程里内核那道开关独立生效：
    # 没给内核的 LG_RUNTIME_PARALLEL_LIVE 就逐臂拒（仍然零网络、零真实 token）。
    monkeypatch.delenv("LG_RUNTIME_PARALLEL_LIVE", raising=False)
    live = ds.run_scale(out=tmp_path / "live", scenes=1, max_workers=2,
                        offline_mode="gateway", offline_overrides=models)
    assert live["ok"] is False and live["verdict"] == "reject"
    assert live["live_channel_enabled"] is True and live["offline_mode"] == "gateway"
    assert live["tokens_total"] == 0 and live["calls_total"] == 0
    for record in live["arms"]:
        assert record["status"] == "gateway_channel_requires_explicit_live_opt_in"
        assert record["error"] == "gateway_channel_requires_explicit_live_opt_in"


# ── CLI 入口 ───────────────────────────────────────────────────────────

def test_cli_runs_a_batch_and_then_rejects_a_failing_one(tmp_path, capsys):
    """文档里写的入口可用：--scenes/--out 跑通批次；注入一场失败后退出码 1。"""
    out = tmp_path / "run"
    code = ds.main(["--scenes", "2", "--out", str(out), "--max-workers", "4",
                    "--arm-delay-s", "0.05", "--max-batch-calls", "16"])
    assert code == 0
    printed = json.loads(capsys.readouterr().out.strip().splitlines()[0])
    assert printed["ok"] is True and printed["verdict"] == "accept"
    assert printed["scenes"] == 2 and printed["jobs_committed"] == 4
    assert printed["calls_total"] == 8 and printed["arms_per_scene"] == ["A", "B"]
    assert printed["calls_total"] == json.loads(
        (out / ds.SUMMARY_NAME).read_text(encoding="utf-8"))["calls_total"]

    code = ds.main(["--scenes", "2", "--out", str(tmp_path / "bad"), "--max-workers", "4",
                    "--arm-delay-s", "0.05",
                    "--scene-json", '{"2": {"offline": {"behavior": "hard"}, '
                                    '"budget": {"max_calls": 2}}}'])
    assert code == 1
    lines = capsys.readouterr().out.strip().splitlines()
    rejected = json.loads(lines[0])
    assert rejected["ok"] is False and rejected["verdict"] == "reject"
    assert rejected["failed_job_ids"] == ["scene-002-armA", "scene-002-armB"]
    assert json.loads(lines[1])["error"].startswith("dream_scale_rejected:scene-002-armA")

    # 批次级护栏由 __main__ 收口成退出码 2；main() 本身响亮抛，不静默降级。
    with pytest.raises(ds.DreamFault, match="batch_call_cap_exceeded"):
        ds.main(["--scenes", "2", "--out", str(tmp_path / "capped"), "--max-batch-calls", "4"])
