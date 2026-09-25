"""K4 验收 harness 回归（scripts/k4_accept_report.py，全离线假夹具）。

任务书两条红线：
1. **真实预算闸拒 ⇒ 必须判「诚实失败」而不是「缺陷」**——机械判据三连
   （闸拒绝码 + 无产出 + 世界库自洽：calls==max_calls 且全 succeeded）；
2. **缺收据 ⇒ 必须判「证据不足」而不是「通过」**——文件不存在 / 场-臂
   无记录 / 世界库不可读，一律证据不足。

附加钉：自洽性破（声称预算拒但 calls<max_calls）⇒ 口径缺陷未定；
rollback_failed ⇒ 口径缺陷未定；rewrite_budget_exhausted 须带非空
核验错误清单才算诚实；committed ⇒ pass 带收据依据。
"""
from __future__ import annotations

import importlib.util as _u
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_spec = _u.spec_from_file_location(
    "k4ar", ROOT / "scripts" / "k4_accept_report.py")
k4ar = _u.module_from_spec(_spec)
sys.modules["k4ar"] = k4ar
_spec.loader.exec_module(k4ar)

SCHEMA = """
CREATE TABLE jobs(
  id TEXT PRIMARY KEY, book TEXT, branch TEXT, scene TEXT,
  idem TEXT, request_hash TEXT NOT NULL, request TEXT NOT NULL,
  context TEXT NOT NULL, status TEXT NOT NULL, verified TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE calls(
  job TEXT, stage TEXT, request_hash TEXT NOT NULL, status TEXT NOT NULL,
  request TEXT NOT NULL, response TEXT, error TEXT,
  started_at TEXT NOT NULL, duration_ms INTEGER,
  PRIMARY KEY(job,stage));
"""


def _mk_world(tmp_path, arm_n, *, scene, max_calls=6, max_rewrites=2,
              n_calls=6, all_succeeded=True, job_status="running",
              call_statuses=None):
    """造一个与 store.py 同形的最小世界库（离线夹具，写 tmp_path 副本）。"""
    d = tmp_path / f"arm{arm_n}"
    d.mkdir(parents=True, exist_ok=True)
    p = d / "k4.sqlite"
    con = sqlite3.connect(p)
    con.executescript(SCHEMA)
    job_id = f"job-{scene}-{'AB'[arm_n - 1]}"
    request = json.dumps({
        "plan": {"scene_id": scene, "idempotency_key": f"k4-{scene}-"
                 f"{'AB'[arm_n - 1]}"},
        "budget": {"max_calls": max_calls, "max_rewrites": max_rewrites}})
    con.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (job_id, "WK-K4", "main", scene,
                 f"k4-{scene}-{'AB'[arm_n - 1]}", "h", request,
                 "{}", job_status, None, "t", "t"))
    statuses = call_statuses or (["succeeded"] * n_calls)
    for i, st in enumerate(statuses):
        con.execute("INSERT INTO calls VALUES (?,?,?,?,?,?,?,?,?)",
                    (job_id, f"writer.{i // 2}" if i % 2 == 0
                     else f"verifier.{i // 2}", "h", st, "{}",
                     '{"text":"x"}' if st == "succeeded" else None,
                     None if st == "succeeded" else "boom", "t", 1))
    con.commit()
    con.close()
    return p


def _artifact(tmp_path, *, worlds=True, prose=None, failures=None,
              skips=None, receipts=None):
    art = {
        "live": True, "channel_changed": True,
        "worlds_dir": str(tmp_path) if worlds else "Z:/nope",
        "artifacts": {"prose": prose or [], "packages": [],
                      "receipts": receipts or [],
                      "failures": failures or [], "skipped": skips or []},
    }
    p = tmp_path / "k4_paired.json"
    p.write_text(json.dumps(art, ensure_ascii=False), encoding="utf-8")
    return p


def _by(rows):
    return {(r["scene"], r["arm"]): r for r in rows}


def test_budget_refusal_is_honest_failure(tmp_path):
    """红线1：真实预算闸拒（calls==max_calls 全 succeeded、无产出、
    rollback 干净）⇒ 判 honest_failure 而非 defect。"""
    _mk_world(tmp_path, 1, scene="s2", n_calls=6, all_succeeded=True)
    _mk_world(tmp_path, 2, scene="s2", n_calls=6)
    art = _artifact(
        tmp_path,
        prose=[{"scene": "s1", "arm": "A", "status": "committed"},
               {"scene": "s1", "arm": "B", "status": "committed"}],
        receipts=[{"scene": "s1", "arm": "A", "job_id": "j1",
                   "usage": {"calls": 2, "tokens": 900,
                             "verifier_invalid_retries": 0}},
                  {"scene": "s1", "arm": "B", "job_id": "j2",
                   "usage": {"calls": 2, "tokens": 950,
                             "verifier_invalid_retries": 0}}],
        failures=[
            {"scene": "s2", "arm": "A", "error_type": "RuntimeFault",
             "error": "call_budget_exhausted", "rollback_failed": False,
             "rollback_error": None},
            {"scene": "s2", "arm": "B", "error_type": "RuntimeFault",
             "error": "rewrite_budget_exhausted:"
                      "missing_or_unplanned_event",
             "rollback_failed": False, "rollback_error": None}],
        skips=[{"scene": "s3", "arm": "A", "skipped_after": "s2"},
               {"scene": "s3", "arm": "B", "skipped_after": "s2"}])
    report = k4ar.classify(art)
    rows = _by(report["rows"])
    assert rows[("s1", "A")]["verdict"] == "pass"
    assert "tokens=900" in "".join(rows[("s1", "A")]["basis"])
    # 红线1 主体：
    assert rows[("s2", "A")]["verdict"] == "honest_failure", \
        rows[("s2", "A")]
    assert any("calls == max_calls" in b
               for b in rows[("s2", "A")]["basis"])
    assert rows[("s2", "B")]["verdict"] == "honest_failure"
    assert rows[("s3", "A")]["verdict"] == "cascade_skip"
    assert report["summary"]["honest_failure"] == 2


def test_missing_artifact_is_insufficient_evidence(tmp_path):
    """红线2：缺收据 ⇒ 全部证据不足，绝不判通过。"""
    report = k4ar.classify(tmp_path / "absent.json")
    assert report["artifact"] is None
    assert report["summary"]["pass"] == 0
    assert report["summary"]["insufficient_evidence"] == 6
    for r in report["rows"]:
        assert r["verdict"] == "insufficient_evidence"


def test_missing_world_store_is_insufficient_not_honest(tmp_path):
    """世界库不可读 ⇒ 自洽不可核 ⇒ 证据不足（不许凭失败码单腿判诚实）。"""
    art = _artifact(
        tmp_path, worlds=False,
        failures=[{"scene": "s2", "arm": "A",
                   "error_type": "RuntimeFault",
                   "error": "call_budget_exhausted",
                   "rollback_failed": False}])
    rows = _by(k4ar.classify(art)["rows"])
    assert rows[("s2", "A")]["verdict"] == "insufficient_evidence"


def test_inconsistent_claim_is_defect(tmp_path):
    """声称预算闸拒但 calls<max_calls（4≠6）⇒ 自洽性破 ⇒ 口径缺陷未定。"""
    _mk_world(tmp_path, 1, scene="s2", n_calls=4)
    art = _artifact(
        tmp_path,
        failures=[{"scene": "s2", "arm": "A", "error_type": "RuntimeFault",
                   "error": "call_budget_exhausted",
                   "rollback_failed": False}])
    rows = _by(k4ar.classify(art)["rows"])
    r = rows[("s2", "A")]
    assert r["verdict"] == "defect_undetermined"
    assert "≠" in r["note"] or "≠" in "".join(r["basis"])


def test_rollback_failed_is_defect(tmp_path):
    """回滚亦失败（rollback_failed=true）⇒ 非干净闸拒 ⇒ 口径缺陷未定。"""
    art = _artifact(
        tmp_path,
        failures=[{"scene": "s2", "arm": "A", "error_type": "RuntimeFault",
                   "error": "call_budget_exhausted", "rollback_failed": True,
                   "rollback_error": "RuntimeError"}])
    rows = _by(k4ar.classify(art)["rows"])
    assert rows[("s2", "A")]["verdict"] == "defect_undetermined"


def test_rewrite_exhausted_without_error_list_is_defect(tmp_path):
    """rewrite_budget_exhausted 冒号后无核验错误清单 ⇒ 不可证 ⇒ 缺陷未定。"""
    _mk_world(tmp_path, 2, scene="s2", n_calls=6)
    art = _artifact(
        tmp_path,
        failures=[{"scene": "s2", "arm": "B", "error_type": "RuntimeFault",
                   "error": "rewrite_budget_exhausted",
                   "rollback_failed": False}])
    rows = _by(k4ar.classify(art)["rows"])
    assert rows[("s2", "B")]["verdict"] == "defect_undetermined"


def test_unknown_code_is_defect(tmp_path):
    art = _artifact(
        tmp_path,
        failures=[{"scene": "s2", "arm": "A", "error_type": "RuntimeFault",
                   "error": "mysterious_fault", "rollback_failed": False}])
    rows = _by(k4ar.classify(art)["rows"])
    assert rows[("s2", "A")]["verdict"] == "defect_undetermined"
