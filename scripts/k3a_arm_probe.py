"""K3-A 臂对照探针（lg-k3a-arm-probe，2026-09-27 派工）。

回答一个已被真库读数确认、但台账说不清的问题：**同一条有效 book policy 下，
「一条都查不出来」到底卡在哪一级门？**

主控 2026-09-27 用 `mode=ro` 真库 + 内存里只读放宽 status 门拿到两条读数：
- 默认门：`status=empty`、selected=0 **且 rejected=0**——8 条策略在
  `r.status in eligible_statuses(r.version)` 这一行被**静默丢弃**，根本没进
  `rejected` 台账。台账说「0 条被拒」，实际有 8 条被拒 ⇒ **台账漏报**。
- 只读放宽 status 门：`status=matched`、selected=8，每条带真实证据区间、
  2 个根作品、`evidence_cross_work=True` ⇒ 阻断 K3-A 臂的**只有 status 门**，
  证据供给本身够。

本脚本把这两侧**同一次调用并排跑出来**，出可核的对照收据（`--out` JSON）。

**单源复用纪律（硬约束）**：判据本体**一律取自** `app/knowledge_query.py`，
本文件**不另写一套判据**——
- 第 1 级 status 门：`KQ.eligible_statuses(row.version)`（`query_knowledge`
  逐字调用的那个单一可审计入口）；
- 第 2 级 observation 门：`KQ.ELIGIBLE_OBSERVATION`；
- 证据/条件/scope 三级与落点：直接跑 `KQ.query_knowledge`，不自制过滤器；
- 侧 A 连 policy 之外的任何加严/放宽参数都不传（生产口径原样）。

**只读放宽的实现口径（关键，勿改成别的做法）**：侧 B 的放宽是**进程内局部**的
——`unittest.mock.patch.object(app.knowledge_query, "eligible_statuses", ...)`，
只替换 `query_knowledge` 实际调用的那**一个**可审计入口，且是**加法**（原集合
∪ 额外 status，永不收窄）。由此：
- 模块级常量 `ELIGIBLE_STATUS` / `ELIGIBLE_STATUS_BY_VERSION` /
  `DEFAULT_ELIGIBLE_VERSION` **在任何时刻都逐字未被改动**（收据里 before/after
  两份快照逐字相等，且有 `relaxation.patch_live_during_side_b` 证明放宽确实
  生效过——防空跑）；
- 退出 `with` 上下文即恢复 `KQ.eligible_statuses` 原对象（上下文管理器语义，
  异常路径也恢复）。

**明确不做**（任务书硬约束，逐条写进 JSON `discipline`）：
- 不写库：无 DDL/DML/`commit`；连接层 `mode=ro` + `PRAGMA query_only=ON`，
  **只读承诺在连接层成立**，不靠自觉；唯一允许的写 = `--out` 报告本身；
- **不写 `expression_strategies_v2.status`**（升格归上游写侧，查询侧只读、
  绝不代为放宽）；
- 不改 `ELIGIBLE_STATUS` 的**生产默认值**（放宽只活在一次 `with` 作用域内）；
- 不裁定任何 `status`/`scope`，不给「一键升格」建议（那是策略审查席的事）；
- 缺 `--book` 时**如实报错退出**，绝不猜一个作品（book_id 猜错会让
  scope 门给出看似合理的假读数）；点名了 `--db` 而它不可读时同样如实报
  「不可读」，**不悄悄换一个库**去出读数。

用法：
    python scripts/k3a_arm_probe.py --book WK-6e5d2623
    python scripts/k3a_arm_probe.py --db <真库> --book WK-x --out r.json

`--out` 缺省（""）= 只打 stdout、零写盘；`--db` 缺省时按 `<repo>/data/
language_genome.db` → 主仓候选绝对路径取第一个存在的（worktree 检出不带
data/ 时仍能只读实测）。

退出码：0=两侧读数已产出；2=参数非法（如缺 `--book`、`--extra-status` 越
词表）；3=库不可读。
"""
from __future__ import annotations

import argparse
import datetime
import json
import sqlite3
import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent          # 工作树根
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from app import knowledge_query as KQ                   # noqa: E402
from app.models import ExpressionStrategyV2            # noqa: E402

PROBE = "k3a_arm_probe"
SCHEMA = "k3a_arm_probe/v1"
TASK = ("lg-k3a-arm-probe（K3-A 臂对照：默认门 vs 只读放宽 status 门，"
        "同 policy 并排读数 + 台账漏报对账）")
# 库的候选路径（按序取第一个存在的）：工作树自身 → 主仓（worktree 检出
# 不带 data/ 时仍能只读实测；主控在主仓 cwd 复跑时命中第一条）。
DB_CANDIDATE_REL = "data/language_genome.db"
DB_FALLBACK_ABS = "F:/agi/language-genome/data/language_genome.db"
# 侧 B 额外放进合格集的 status（**仅存活在一次 with 作用域内**）。
# 逐字取自 app.knowledge.STRATEGY_STATUS 词表里被默认口径挡掉的那一档，
# 不自造词表：见 STRATEGY_STATUS_VOCAB 的自证。
DEFAULT_EXTRA_STATUS = ("hypothesis",)
# 词表自证用：侧 B 放宽用的 status 必须是知识侧词表里的合法值（不自造）。
STRATEGY_STATUS_VOCAB = frozenset(KQ.K.STRATEGY_STATUS)
# 逐字单源自证：把两侧用的口径常量原样带进收据，便于外部对账
# （不复制字面量——全部现场从 app.knowledge_query 取）。
GATE_ORDER = ("gate1_status", "gate2_observation")


# ------------------------------------------------------------ 只读连接层
def _ro_connect(db_path: Path):
    """真库一律 mode=ro 只读。**只读承诺在连接层成立**（不是靠自觉）。"""
    return sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True,
                           check_same_thread=False)


def open_ro_session(db_path: Path):
    """只读 SQLAlchemy session（engine 的 creator 直接给 mode=ro 连接）。

    不用 `app.db` 的全局 engine（那个按 LG_DATABASE_URL 走**可写**连接）——
    本探针的所有查询都挂在这个只读 engine 上。额外打上
    `PRAGMA query_only=ON` 并把读回的 query_only 值作为**只读自证**带进收据
    （连上了不等于写不了；query_only=1 才是「写即报错」的那道闸）。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    eng = create_engine("sqlite://",
                        creator=lambda: _ro_connect(db_path), future=True)
    session = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)()
    session.connection().exec_driver_sql("PRAGMA query_only=ON")
    return session, {
        "db_mode": "ro",
        "dbapi_uri": f"file:{Path(db_path).as_posix()}?mode=ro",
        "query_only": session.connection().exec_driver_sql(
            "PRAGMA query_only").scalar(),
    }


def resolve_db(explicit: str, repo_root: Path) -> tuple[Path, list[str]]:
    """库路径解析。返回 (选中路径, 候选清单)；候选全缺时选中路径仍返回
    首选（由调用方报「不可读」而不是崩）。

    **显式 `--db` 不做候选回退**：调用方点名了一个库，它不存在就必须如实
    报「不可读」。悄悄换一个库去出读数 = 拿别的库的数当这颗的答案，比报错
    危险得多。候选链只在**没点名**时生效（工作树自身 → 主仓）。"""
    if explicit:
        p = Path(explicit)
        return p, [str(p)]
    cands = [Path(repo_root) / DB_CANDIDATE_REL, Path(DB_FALLBACK_ABS)]
    for c in cands:
        if c.exists():
            return c, [str(x) for x in cands]
    return cands[0], [str(x) for x in cands]


def portable_path(path, repo_root: Path) -> str:
    """绝对路径 → 相对仓库根的可移植写法（仓外路径不内嵌盘符）。"""
    p = Path(path)
    try:
        return p.resolve().relative_to(Path(repo_root).resolve()).as_posix()
    except (ValueError, OSError, RuntimeError):
        return f"<outside-repo>/{p.name}"


def _now_iso() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


# ------------------------------------------------- status 门（单源快照与放宽）
def gate_snapshot() -> dict:
    """status 门当前口径的**逐字快照**（收据对账用）。

    常量三项（`ELIGIBLE_STATUS` / `ELIGIBLE_STATUS_BY_VERSION` /
    `DEFAULT_ELIGIBLE_VERSION`）与入口函数 `eligible_statuses()` 的实际返回值
    一并记录——测试据此断言「放宽是局部且会恢复」。"""
    return {
        "ELIGIBLE_STATUS": sorted(KQ.ELIGIBLE_STATUS),
        "ELIGIBLE_STATUS_BY_VERSION": {
            k: sorted(v) for k, v in sorted(
                KQ.ELIGIBLE_STATUS_BY_VERSION.items())},
        "DEFAULT_ELIGIBLE_VERSION": KQ.DEFAULT_ELIGIBLE_VERSION,
        "eligible_statuses_none": sorted(KQ.eligible_statuses()),
        "eligible_statuses_by_version": {
            str(v): sorted(KQ.eligible_statuses(v)) for v in (1, 2)},
    }


def _constants_view(snap: dict) -> dict:
    """只留三项模块级常量（放宽期间它们**必须**逐字不变）。"""
    return {"ELIGIBLE_STATUS": snap["ELIGIBLE_STATUS"],
            "ELIGIBLE_STATUS_BY_VERSION": snap["ELIGIBLE_STATUS_BY_VERSION"],
            "DEFAULT_ELIGIBLE_VERSION": snap["DEFAULT_ELIGIBLE_VERSION"]}


def widened_eligible_statuses(extra_status, orig):
    """加法放宽：原口径 ∪ extra_status。**永不收窄**（原来过的仍过）。"""
    extra = frozenset(extra_status)

    def widened(version: str | int | None = None) -> frozenset[str]:
        return frozenset(orig(version)) | extra
    widened.__name__ = "eligible_statuses(只读放宽·进程内局部)"
    return widened


def _side_b(policy: dict, session, extra_status) -> tuple[dict, dict]:
    """侧 B：只读放宽 status 门后跑**同一个** policy。

    放宽实现 = `mock.patch.object(KQ, "eligible_statuses", ...)`（上下文
    管理器：正常/异常退出都恢复原对象）。**模块级常量全程一个字节都没动。**
    收据给出三件事：放宽前常量快照、作用域内 patch 是否真的生效（防空跑）、
    退出后常量与入口函数是否逐字复原。"""
    orig_fn = KQ.eligible_statuses
    before = gate_snapshot()
    extra = frozenset(extra_status)
    widened = widened_eligible_statuses(extra, orig_fn)
    with mock.patch.object(KQ, "eligible_statuses", widened):
        patch_live = KQ.eligible_statuses is widened
        during = gate_snapshot()
        resp = KQ.query_knowledge(policy, session)
    after = gate_snapshot()
    return resp, {
        "mechanism": ("unittest.mock.patch.object(app.knowledge_query, "
                      "'eligible_statuses', <加法放宽>)——只替换 query_knowledge "
                      "逐字调用的那一个可审计入口，作用域退出即恢复"),
        "extra_status": sorted(extra),
        "constants_before": _constants_view(before),
        "constants_during": _constants_view(during),
        "constants_after": _constants_view(after),
        "constants_untouched": (_constants_view(before)
                                == _constants_view(during)
                                == _constants_view(after)),
        "entry_restored": KQ.eligible_statuses is orig_fn,
        "entry_output_restored": (after["eligible_statuses_none"]
                                  == before["eligible_statuses_none"]
                                  and after["eligible_statuses_by_version"]
                                  == before["eligible_statuses_by_version"]),
        "patch_live_during_side_b": patch_live,
        "widened_sets_by_version": {
            str(v): sorted(widened(v)) for v in (1, 2)},
    }


# ------------------------------------------------------- 落点索引与逐条读数
def _landing_index(resp: dict) -> dict:
    """query_knowledge 落点索引：key → 'selected' | 'rejected:<reason>'。"""
    out: dict[str, str] = {}
    for e in resp.get("selected") or []:
        out[e["strategy_key"]] = "selected"
    for r in resp.get("rejected") or []:
        k = r.get("strategy_key")
        if k is not None and k not in out:
            out[k] = f"rejected:{r.get('reason')}"
    return out


def _side_view(resp: dict, label: str) -> dict:
    """一侧的对照读数：status / selected 条数 / rejected 条数 / budget。"""
    return {
        "side": label,
        "status": resp.get("status"),
        "reason": resp.get("reason"),
        "n_selected": len(resp.get("selected") or []),
        "n_rejected": len(resp.get("rejected") or []),
        "selected_keys": sorted(e["strategy_key"]
                                for e in resp.get("selected") or []),
        "rejected": resp.get("rejected") or [],
        "budget": resp.get("budget"),
        "snapshot_fingerprint": resp.get("snapshot_fingerprint"),
    }


def _candidate_filter_audit(session, policy: dict) -> dict:
    """候选集预筛的逐行对账：哪些行在进 `rejected` 台账**之前**就被丢了。

    逐字复刻 `query_knowledge` 候选筛那一行（`app/knowledge_query.py` 第 1 级
    status 门 + 第 2 级 observation 门），判据本体取自 KQ 的常量/入口函数——
    本文件不自造过滤器。核心读数：`status_gate` 名单（**静默丢弃**，台账
    看不见）与 `observation_gate` 名单（另一道口径，单独记账不混算）。"""
    book = policy.get("book_id") or ""
    all_rows, status_dropped, observation_dropped = [], [], []
    for r in session.query(ExpressionStrategyV2).all():
        row = {"strategy_key": r.strategy_key, "strategy_id": r.id,
               "version": r.version, "status": r.status,
               "observation_status": r.observation_status,
               "scope": r.scope, "scope_ids": list(r.scope_ids or []),
               "status_gate": r.status in KQ.eligible_statuses(r.version),
               "observation_gate": (r.observation_status
                                    in KQ.ELIGIBLE_OBSERVATION),
               "scope_gate": r.scope == "WORK" and book in (r.scope_ids or [])}
        all_rows.append(row)
        if not row["status_gate"]:
            status_dropped.append(row)
        elif not row["observation_gate"]:
            observation_dropped.append(row)
    all_rows.sort(key=lambda x: x["strategy_key"])
    for lst in (status_dropped, observation_dropped):
        lst.sort(key=lambda x: x["strategy_key"])
    return {
        "n_rows_total": len(all_rows),
        "status_gate": {
            "gated_by": "KQ.eligible_statuses(row.version)（= query_knowledge "
                        "候选筛第 1 级）",
            "n_silently_dropped": len(status_dropped),
            "keys": [x["strategy_key"] for x in status_dropped],
            "rows": status_dropped},
        "observation_gate": {
            "gated_by": "KQ.ELIGIBLE_OBSERVATION（= 第 2 级，另一口径）",
            "n_silently_dropped": len(observation_dropped),
            "keys": [x["strategy_key"] for x in observation_dropped],
            "rows": observation_dropped},
        "all_rows": all_rows,
    }


def _ledger_gap(side_a: dict, audit: dict, side_b: dict) -> dict:
    """台账漏报对账：rejected 台账 vs status 门静默丢弃名单。

    「台账说 0 条被拒」与「实际有 N 条被拒」同时成立 ⇒ 台账漏报。这条事实
    由 `test_side_a_ledger_reports_zero_rejected_while_status_gate_drops_rows`
    钉死。"""
    dropped_keys = audit["status_gate"]["keys"]
    rejected_keys = sorted({r.get("strategy_key")
                            for r in side_a.get("rejected") or []
                            if r.get("strategy_key") is not None})
    covered = sorted(set(dropped_keys) & set(rejected_keys))
    b_landing = _landing_index(side_b)
    isolated = sorted(k for k in dropped_keys if k in b_landing)
    return {
        "side_a_n_rejected": side_a["n_rejected"],
        "n_silently_dropped_by_status_gate": len(dropped_keys),
        "silently_dropped_keys": dropped_keys,
        "rejected_keys": rejected_keys,
        "status_drops_visible_in_rejected": covered,
        "ledger_covers_status_drops": bool(covered),
        "verdict": ("ledger_under_reports" if dropped_keys and not covered
                    else "ledger_consistent"),
        "status_gate_isolation": {
            "n_dropped_by_status_gate": len(dropped_keys),
            "n_appear_in_side_b_landing": len(isolated),
            "sole_blocker_is_status_gate": len(isolated) == len(dropped_keys),
            "side_b_landings": {k: b_landing[k] for k in isolated},
            "note": "被 status 门丢弃的这批行在侧 B 全部落进 selected/"
                    "rejected ⇒ 阻断本臂的只有 status 门，证据供给本身够"},
        "note": ("rejected 台账只记进候选集**之后**的拒因（证据/条件/scope/"
                 "去重）；候选筛那一行被挡掉的行根本没进台账——所以「0 条被拒」"
                 "与「实际有 N 条被拒」可以同时为真。"),
    }


def _side_b_rows(side_b: dict, audit: dict) -> list[dict]:
    """侧 B 逐条读数：每条策略的 evidence_count 与 evidence_root_works。"""
    b_landing = _landing_index(side_b)
    dropped = {x["strategy_key"]: x for x in audit["status_gate"]["rows"]}
    rows = []
    for e in side_b.get("selected") or []:
        key = e["strategy_key"]
        comps = e.get("score_components") or {}
        rows.append({
            "strategy_key": key, "strategy_id": e["strategy_id"],
            "version": e["version"], "status": e["status"],
            "observation_status": e["observation_status"],
            "scope": e["scope"],
            "evidence_count": comps.get("evidence_count"),
            "evidence_root_works": e.get("evidence_root_works") or [],
            "n_evidence_refs": len(e.get("evidence") or []),
            "evidence_cross_work": e.get("evidence_cross_work"),
            "landing": "selected",
            "dropped_by_status_gate_in_side_a": key in dropped})
    for key, landing in sorted(b_landing.items()):
        if landing == "selected":
            continue
        rows.append({"strategy_key": key, "landing": landing,
                     "dropped_by_status_gate_in_side_a": key in dropped})
    rows.sort(key=lambda x: x["strategy_key"])
    return rows


# ------------------------------------------------------------------ 主流程
def build_policy(book_id: str) -> dict:
    """policy 构造：只放查询作品本身，**不传任何 source_policy 加严/放宽
    参数**（侧 A 必须逐字是生产口径）。"""
    return {"book_id": book_id}


def run_probe(db_path: Path, book_id: str, *,
              extra_status=DEFAULT_EXTRA_STATUS,
              repo_root: Path = ROOT) -> dict:
    """两侧并排跑完并出收据。只读：连接层 mode=ro + query_only=ON，
    全程无 DDL/DML/commit；`expression_strategies_v2.status` 只读。"""
    if not book_id:
        raise ValueError("book_id 必填：缺查询作品时如实报错，不猜一个作品")
    policy = build_policy(book_id)
    report: dict = {
        "probe": PROBE, "schema": SCHEMA, "task": TASK,
        "generated_at": _now_iso(),
        "repo_root": portable_path(ROOT, repo_root),
        "db_path": portable_path(db_path, repo_root),
        "db_available": False, "db_error": None,
        "policy": policy,
        "policy_sha256": KQ.policy_sha256(policy),
        "gates": {
            "ELIGIBLE_STATUS": sorted(KQ.ELIGIBLE_STATUS),
            "ELIGIBLE_STATUS_BY_VERSION": {
                k: sorted(v) for k, v in sorted(
                    KQ.ELIGIBLE_STATUS_BY_VERSION.items())},
            "ELIGIBLE_OBSERVATION": sorted(KQ.ELIGIBLE_OBSERVATION),
            "ELIGIBLE_INSTANCE_STATUS": sorted(KQ.ELIGIBLE_INSTANCE_STATUS),
            "gate_order": list(GATE_ORDER),
            "source": "app/knowledge_query.py（现场取，脚本不复制字面量）"},
        "strategy_status_vocab": sorted(STRATEGY_STATUS_VOCAB),
    }
    if not Path(db_path).is_file():
        report["db_error"] = f"库不存在或不可读：{report['db_path']}"
        return report
    from sqlalchemy import text
    try:
        session, ro = open_ro_session(Path(db_path))
    except Exception as exc:                      # 库不可读即事实，不崩不猜
        report["db_error"] = f"{type(exc).__name__}: {exc}"
        return report
    try:
        session.execute(text("SELECT 1")).fetchone()  # 失败可见的连通性探针
        report["db_available"] = True
        report["readonly"] = ro
        # 侧 A：默认门（生产口径，不碰任何常量）
        side_a_resp = KQ.query_knowledge(policy, session)
        audit = _candidate_filter_audit(session, policy)
        # 侧 B：只读放宽 status 门（进程内局部，见 _side_b）
        side_b_resp, relaxation = _side_b(policy, session, extra_status)
        report["readonly"]["query_only_after_probe"] = \
            session.connection().exec_driver_sql("PRAGMA query_only").scalar()
    finally:
        session.close()
    side_a = _side_view(side_a_resp, "A_default_gate")
    side_b = _side_view(side_b_resp, "B_status_gate_relaxed_readonly")
    report["side_a_default_gate"] = side_a
    report["side_b_status_gate_relaxed"] = side_b
    report["candidate_filter_audit"] = audit
    report["ledger_gap"] = _ledger_gap(side_a, audit, side_b_resp)
    report["relaxation"] = relaxation
    report["side_b_rows"] = _side_b_rows(side_b_resp, audit)
    report["evidence_supply"] = {
        "n_rows": len(report["side_b_rows"]),
        "min_evidence_count": min(
            [r["evidence_count"] for r in report["side_b_rows"]
             if r.get("evidence_count") is not None] or [0]),
        "max_evidence_count": max(
            [r["evidence_count"] for r in report["side_b_rows"]
             if r.get("evidence_count") is not None] or [0]),
        "all_have_evidence": all(
            (r.get("evidence_count") or 0) > 0
            for r in report["side_b_rows"]
            if r.get("landing") == "selected"),
        "root_works_union": sorted({w for r in report["side_b_rows"]
                                    for w in (r.get("evidence_root_works")
                                              or [])})}
    report["comparison"] = {
        "side_a": {"status": side_a["status"],
                   "selected": side_a["n_selected"],
                   "rejected": side_a["n_rejected"]},
        "side_b": {"status": side_b["status"],
                   "selected": side_b["n_selected"],
                   "rejected": side_b["n_rejected"]},
        "delta_selected": side_b["n_selected"] - side_a["n_selected"],
        "readings_identical": (side_a["status"] == side_b["status"]
                               and side_a["n_selected"] == side_b["n_selected"]),
        "note": "同一 policy、同一只读 session、同一判据源（KQ）；"
                "唯一差别=status 门在内存里被进程内局部加法放宽"}
    report["discipline"] = {
        "db_mode": "ro（sqlite3 file:...?mode=ro + PRAGMA query_only=ON，"
                   "只读承诺在连接层成立）",
        "db_writes": "无（无 DDL/DML/commit；唯一写=--out 报告本身）",
        "status_column": "只读：未写 expression_strategies_v2.status",
        "production_default_changed": False,
        "gate_relaxation": relaxation["mechanism"],
        "no_promotion_advice": True,
        "zero_model_calls": True,
        "zero_git_writes": True}
    return report


# ------------------------------------------------------------------ CLI
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo-root", default=str(ROOT), dest="repo_root",
                    help="仓库根（缺省=脚本自身推导的仓库根）")
    ap.add_argument("--db", default="", help="真库路径覆盖（缺省 <repo>/"
                    "data/language_genome.db；再缺则候选绝对路径）")
    ap.add_argument("--book", default="", dest="book",
                    help="查询作品 id（**必填**，如 WK-6e5d2623；"
                         "缺则如实报错退出，绝不猜一个作品）")
    ap.add_argument("--extra-status", default=",".join(DEFAULT_EXTRA_STATUS),
                    dest="extra_status",
                    help="侧 B 额外放进合格集的 status（逗号分隔；仅活在"
                         "进程内局部 with 作用域，缺省 hypothesis）")
    ap.add_argument("--out", default="", dest="out",
                    help='JSON 落盘路径（""＝只打 stdout、零写盘）')
    a = ap.parse_args(argv)
    if not a.book.strip():
        print("[k3a_arm_probe] --book 必填：缺查询作品时如实报错退出，"
              "绝不猜一个作品（book_id 猜错会让 scope 门给出看似合理的假读数）。"
              "例：--book WK-6e5d2623", file=sys.stderr)
        return 2
    repo_root = Path(a.repo_root)
    db_path, cands = resolve_db(a.db, repo_root)
    extra = tuple(x.strip() for x in a.extra_status.split(",") if x.strip())
    if not extra:
        print("[k3a_arm_probe] --extra-status 不得为空：侧 B 必须有一个"
              "被默认口径挡掉的合法 status（词表见 strategy_status_vocab）",
              file=sys.stderr)
        return 2
    unknown = sorted(set(extra) - STRATEGY_STATUS_VOCAB)
    if unknown:
        print(f"[k3a_arm_probe] --extra-status 含词表外取值 {unknown}："
              f"合法值={sorted(STRATEGY_STATUS_VOCAB)}", file=sys.stderr)
        return 2
    report = run_probe(db_path, a.book.strip(), extra_status=extra,
                       repo_root=repo_root)
    report["db_candidates"] = cands
    text = json.dumps(report, ensure_ascii=False, indent=1)
    print(text)
    if not report["db_available"]:
        print(f"[k3a_arm_probe] 库不可读：{report['db_error']}"
              "（不猜任何读数）", file=sys.stderr)
        return 3
    if a.out:
        op = Path(a.out)
        if op.parent and not op.parent.exists():
            op.parent.mkdir(parents=True, exist_ok=True)
        op.write_text(text, encoding="utf-8")
        print(f"[k3a_arm_probe] JSON 已写 {op}", file=sys.stderr)
    else:
        print("[k3a_arm_probe] --out 缺省：零写盘（仅 stdout）", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
