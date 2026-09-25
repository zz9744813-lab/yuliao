"""K4 三场验收 harness（lg-k4-accept-harness，2026-09-25 派工）。

只读账本/收据/世界目录，产出「三场逐场验收报告」：场景 → 臂 → 结果 →
依据（收据字段/退出码/世界库逐行）→ 判定。对「诚实失败」给**机械判据**，
不靠人读日志下结论：

判定矩阵（机械，逐行给依据）：
- **通过**：prose[(scene,arm)].status == "committed"（依据：收据 usage
  与 job 状态）。
- **诚实失败**（机械判据三连）：
  ① 失败码 ∈ {call_budget_exhausted, rewrite_budget_exhausted}（闸门
    拒绝码），且 failure.rollback_failed 为假（回滚干净）；
  ② 无产出：prose[(scene,arm)] 不存在 committed 行；
  ③ 收据/世界库**自洽**：
     - call_budget_exhausted ⇒ 该 job 的 calls 行数 == budget.max_calls
       且全部 succeeded、job.status != 'committed'；
     - rewrite_budget_exhausted ⇒ 失败串冒号后带非空核验错误清单
       （validate_review errors），calls 全 succeeded，
       calls 行数 == (max_rewrites+1)*2 或 ≤ max_calls，
       job.status != 'committed'。
- **口径缺陷未定**：失败码不在已知闸集合 / rollback_failed=true /
  自洽性破（如声称预算拒但 calls < max_calls，或失败与 committed 产出
  同现，或存在 failed 调用行）。
- **证据不足**：收据缺（文件不存在 / 场-臂无任何记录 / 世界库不可读
  导致自洽不可核）。**缺证据绝不判「通过」**（测试红线）。
- **连锁跳过**：skipped 条目（单列，随 parent 失败定性，不算独立判）。

硬约束：真库与世界库一律 mode=ro 只读；零模型调用；无写路径；
不合并不推送。

用法：
    python scripts/k4_accept_report.py --repo-root F:/agi/language-genome \
        --out <report.json>
"""
from __future__ import annotations

import argparse
import datetime
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DOC = "docs/K4验收定性_20260925.md（本脚本产出）"
HONEST_GATE_CODES = {"call_budget_exhausted", "rewrite_budget_exhausted"}
VERDICTS = ("pass", "honest_failure", "defect_undetermined",
            "insufficient_evidence", "cascade_skip")


def _ro_connect(path: Path):
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)


def latest_artifact(repo_root: Path) -> Path | None:
    cands = sorted(Path(repo_root).glob("out_k4_3*/k4_paired.json"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    return cands[0] if cands else None


def _row(scene, arm, result, verdict, basis, note=""):
    return {"scene": scene, "arm": arm, "result": result, "verdict": verdict,
            "basis": basis, "note": note}


def _job_for(con, scene: str, arm: str):
    """世界库中找该 (scene, arm) 的 job 行（idem 尾缀 -A/-B）。"""
    rows = con.execute(
        "SELECT id, idem, status, request FROM jobs WHERE scene=? "
        "AND idem LIKE ?", (scene, f"%-{arm}")).fetchall()
    return rows[0] if rows else None


def _self_consistent(con, job_id: str, code: str, failure_error: str,
                     basis: list[str]) -> tuple[bool, str]:
    """闸拒绝码与世界库逐行核对（机械自洽）。返回 (ok, 不自洽说明)。"""
    req = None
    jstatus = calls_n = None
    row = con.execute("SELECT status, request FROM jobs WHERE id=?",
                      (job_id,)).fetchone()
    if row is None:
        return False, f"jobs[{job_id}] 缺行"
    jstatus, req_raw = row
    req = json.loads(req_raw)
    budget = req.get("budget") or {}
    max_calls = int(budget.get("max_calls", 0))
    max_rewrites = int(budget.get("max_rewrites", 2))
    rows = con.execute(
        "SELECT status FROM calls WHERE job=?", (job_id,)).fetchall()
    calls_n = len(rows)
    n_failed_calls = sum(1 for (st,) in rows if st != "succeeded")
    basis += [f"世界库 job={job_id}：status={jstatus}，"
              f"calls={calls_n}（max_calls={max_calls}，"
              f"max_rewrites={max_rewrites}），非 succeeded 调用行={n_failed_calls}"]
    if jstatus == "committed":
        return False, "job 状态为 committed，与失败记录矛盾"
    if n_failed_calls:
        return False, f"存在非 succeeded 调用行 ×{n_failed_calls}（非干净闸拒）"
    if code == "call_budget_exhausted":
        if calls_n != max_calls:
            return False, (f"声称预算闸拒但 calls={calls_n} ≠ "
                           f"max_calls={max_calls}")
        basis.append(f"自洽：calls == max_calls（{calls_n}=={max_calls}）"
                     "且全部 succeeded ⇒ 预算闸在下次调用前拒 ⇒ 诚实")
        return True, ""
    if code == "rewrite_budget_exhausted":
        err_list = failure_error.split(":", 1)[1].strip() \
            if ":" in failure_error else ""
        if not err_list:
            return False, "rewrite_budget_exhausted 冒号后无核验错误清单"
        if calls_n > max_calls:
            return False, (f"calls={calls_n} > max_calls={max_calls}（越闸）")
        expected_rounds_calls = (max_rewrites + 1) * 2
        basis.append(f"自洽：核验错误清单非空（{err_list[:60]}…），"
                     f"calls={calls_n} ≤ max_calls（改稿轮全用尽的常规形="
                     f"{expected_rounds_calls}）⇒ 诚实")
        return True, ""
    return False, f"未知闸码 {code}"


def classify(artifact_path: Path | None) -> dict:
    """三场逐场判定。artifact 缺 ⇒ 全部证据不足（红线：不判通过）。"""
    scenes = ["s1", "s2", "s3"]
    rows: list[dict] = []
    world_con: dict[str, sqlite3.Connection] = {}
    art = None
    if artifact_path is not None and artifact_path.exists():
        art = json.loads(artifact_path.read_text(encoding="utf-8"))
    if art is None:
        for sc in scenes:
            for arm in ("A", "B"):
                rows.append(_row(sc, arm, "no_artifact",
                                 "insufficient_evidence",
                                 [f"收据不存在：{artifact_path}"],
                                 "缺收据 ⇒ 证据不足，绝不判通过"))
        return {"artifact": None, "rows": rows,
                "summary": _summary(rows)}
    a = art.get("artifacts") or {}
    prose = {(p["scene"], p["arm"]): p for p in (a.get("prose") or [])}
    failures = {(f["scene"], f["arm"]): f
                for f in (a.get("failures") or [])}
    skips = {(s["scene"], s["arm"]): s for s in (a.get("skipped") or [])}
    receipts = {(r["scene"], r["arm"]): r
                for r in (a.get("receipts") or [])}
    wd = art.get("worlds_dir")
    # live 留库的世界目录：arm1=A、arm2=B（工厂序）
    for arm, n in (("A", 1), ("B", 2)):
        p = Path(wd or "") / f"arm{n}" / "k4.sqlite"
        if wd and p.exists():
            world_con[arm] = _ro_connect(p)

    for sc in scenes:
        for arm in ("A", "B"):
            key = (sc, arm)
            if key in prose and prose[key].get("status") == "committed":
                r = receipts.get(key) or {}
                u = r.get("usage") or {}
                rows.append(_row(sc, arm, "committed", "pass", [
                    f"prose.status=committed；receipt.job_id={r.get('job_id')}",
                    f"usage：calls={u.get('calls')}，tokens={u.get('tokens')}，"
                    f"verifier_invalid_retries="
                    f"{u.get('verifier_invalid_retries')}",
                    f"live={art.get('live')}，channel_changed="
                    f"{art.get('channel_changed')}"]))
                continue
            if key in skips:
                s = skips[key]
                rows.append(_row(sc, arm, "skipped", "cascade_skip", [
                    f"skipped_after={s.get('skipped_after')}；"
                    f"reason={s.get('reason')}"],
                    "连锁跳过随 parent 失败定性，不算独立判"))
                continue
            if key in failures:
                f = failures[key]
                err = f.get("error") or ""
                code = err.split(":", 1)[0]
                basis = [f"failures[{sc},{arm}].error={err[:80]}",
                         f"error_type={f.get('error_type')}，"
                         f"rollback_failed={f.get('rollback_failed')}"]
                if f.get("rollback_failed"):
                    rows.append(_row(sc, arm, "failed",
                                     "defect_undetermined", basis,
                                     "回滚亦失败（rollback_failed=true）"
                                     "——非干净闸拒，留半成品态，判口径缺陷未定"))
                    continue
                if code not in HONEST_GATE_CODES:
                    rows.append(_row(sc, arm, "failed",
                                     "defect_undetermined", basis,
                                     f"失败码 {code} 不在已知闸集合 "
                                     f"{sorted(HONEST_GATE_CODES)}"))
                    continue
                con = world_con.get(arm)
                if con is None:
                    rows.append(_row(sc, arm, "failed",
                                     "insufficient_evidence", basis,
                                     "世界库不可读（worlds_dir 缺/已清）——"
                                     "自洽性不可核 ⇒ 证据不足，不判诚实失败"))
                    continue
                job = _job_for(con, sc, arm)
                if job is None:
                    rows.append(_row(sc, arm, "failed",
                                     "insufficient_evidence", basis,
                                     "世界库中无该 (scene,arm) 的 job 行——"
                                     "自洽性不可核"))
                    continue
                ok, why_not = _self_consistent(con, job[0], code, err, basis)
                if ok:
                    rows.append(_row(sc, arm, "failed", "honest_failure",
                                    basis, "机械判据三连成立：闸拒绝码 + "
                                    "无产出 + 世界库自洽"))
                else:
                    rows.append(_row(sc, arm, "failed",
                                     "defect_undetermined", basis,
                                     f"自洽性破：{why_not}"))
                continue
            rows.append(_row(sc, arm, "no_record", "insufficient_evidence",
                             ["收据中无该 (scene,arm) 的任何记录"],
                             "无 prose/failure/skip 记录 ⇒ 证据不足"))
    for con in world_con.values():
        con.close()
    return {"artifact": str(artifact_path), "live": art.get("live"),
            "worlds_dir": wd, "rows": rows, "summary": _summary(rows)}


def _summary(rows: list[dict]) -> dict:
    s = {v: 0 for v in VERDICTS}
    for r in rows:
        s[r["verdict"]] += 1
    s["total"] = len(rows)
    return s


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo-root", default="F:/agi/language-genome",
                    dest="repo_root")
    ap.add_argument("--artifact", default="",
                    help="三场收据（缺省自动取最新 out_k4_3*/k4_paired.json）")
    ap.add_argument("--out", default="", help="报告 JSON 落盘（可选）")
    a = ap.parse_args()
    repo = Path(a.repo_root)
    art = Path(a.artifact) if a.artifact else latest_artifact(repo)
    report = classify(art)
    report["mode"] = "read_only"
    report["generated_at"] = datetime.datetime.now().isoformat(
        timespec="seconds")
    text = json.dumps(report, ensure_ascii=False, indent=1)
    print(text)
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(text, encoding="utf-8")
        print(f"[k4_accept_report] 报告已写 {a.out}")


if __name__ == "__main__":
    main()
