"""K5 晋升三级门逐条解释器（lg-fix-promotion-gate-explain，2026-09-26 派工）。

回答审计 P0-1/P0-2 的 PARTIAL 缺口：`scripts/k5_promotion_wire_probe.py` 已能
机械回答「过门对数可不可核」，但**不解释每条策略卡在哪一级门**。本脚本对每条
策略逐级报出判词，把「8 条策略为何进不了 K3」从断言变成逐条可核证据。

**单源复用纪律（硬约束）**：判据本体**一律取自** `app/knowledge_query.py`，
本文件**不另写一套判据**——
- 第 1 级 status 门：`KQ.eligible_statuses(version)`（按版本分桶的合格集）；
- 第 2 级 observation 门：`KQ.ELIGIBLE_OBSERVATION`；
- 第 3 级 证据门：`KQ._evidence_for` 的 `evidence_count` 与 `stripped` 拒因
  （拒因**分类**只做字符串归并，判词本体是 `_evidence_for` 产出的字面量）；
- 条件管道（随附判词，不单独编号）：`KQ._condition_pipeline`；
- 第 4 级 scope 门：`KQ._scope_matches` 的判词（`UNCERTAIN` →
  `excluded_scope_uncertain`，属哪一类拒因由该判词本身给出）；
- 证据侧同口径常量一并报出以供对账：`KQ.ELIGIBLE_INSTANCE_STATUS` /
  `KQ.DEFAULT_EXCLUDED_SOURCE_TYPES` / `KQ.DEFAULT_ALLOWED_TEXT_VERSIONS`。

**逐字单源自证**（本脚本自带的硬对账，不靠人工比对）：同 policy 现场复跑一次
`KQ.query_knowledge`（只读），逐条比对本解释器的 `blocked_at/reason` 与库侧
`selected/rejected` 落点——不一致即 `library_crosscheck.n_disagree > 0`
（测试对这条钉死）。

**明确不做**（任务书硬约束，逐条写进 JSON `discipline` 与报告正文）：
- 不裁定任何 `status` / `scope`（只报既有门对该行的判词，不给升格意见）；
- 不改任何一行数据（无 commit/add/DDL；连接层 `mode=ro`）；
- 不给「一键升格」建议（那是策略审查席的事）；
- 不放宽任何既有门（只读复用，不传任何 `source_policy` 加严/放宽参数）。

纪律（与 `k5_promotion_wire_probe.py` 同款）：
- 真库 `mode=ro`：`sqlite3.connect("file:...?mode=ro", uri=True)` 作
  SQLAlchemy engine 的 creator——只读承诺在**连接层**成立，不靠自觉；
- 零模型调用、零 git 写；唯一允许的写 = `--json-out` / `--doc-out` 报告本身；
- 库不可读 → `db_available=false` + 如实原因，**不猜**任何判词。

用法：
    python scripts/k5_promotion_gate_explain.py --print-only   # 只打印，不写盘
    python scripts/k5_promotion_gate_explain.py                 # 默认写报告文档
    python scripts/k5_promotion_gate_explain.py --db <真库> --json-out r.json
    python scripts/k5_promotion_gate_explain.py --book-id WK-x  # 带查询作品复跑

默认写文档模式只重写文档里 DOC_BEGIN/DOC_END 标记之间的**机器区**（人写的
叙述段在标记之外原样保留），因此本脚本对同一库复跑是幂等的，不会抹掉人工
叙述。
"""
from __future__ import annotations

import argparse
import datetime
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # 工作树根
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from app import knowledge_query as KQ                   # noqa: E402
from app.models import ExpressionStrategyV2            # noqa: E402

EXPLAIN = "k5_promotion_gate_explain"
SCHEMA = "k5_promotion_gate_explain/v1"
TASK = "lg-fix-promotion-gate-explain（K5 晋升三级门逐条解释：只报判词，" \
       "不裁定 status/scope，不改数据）"
DEFAULT_DOC_REL = "docs/K5晋升三级门逐条解释_20260926.md"
DOC_BEGIN = ("<!-- BEGIN: k5_promotion_gate_explain "
             "(machine-generated) -->")
DOC_END = ("<!-- END: k5_promotion_gate_explain "
           "(machine-generated) -->")
# 库的候选路径（按序取第一个存在的）：工作树自身 → 主仓（worktree 检出
# 不带 data/ 时仍能只读实测；主控在主仓 cwd 复跑时命中第一条）。
DB_CANDIDATE_REL = "data/language_genome.db"
DB_FALLBACK_ABS = "F:/agi/language-genome/data/language_genome.db"

# 固定判定顺序（与 KQ.query_knowledge 的执行顺序逐字对齐：候选筛
# status+observation → ①证据 → ②条件 → ③范围）。blocked_at 取**第一条**
# 不过的级（不跳级猜）。
GATE_ORDER = ("gate1_status", "gate2_observation", "gate3_evidence",
              "gate_condition", "gate4_scope")
GATE_TITLE = {
    "gate1_status": "第 1 级 status 门",
    "gate2_observation": "第 2 级 observation 门",
    "gate3_evidence": "第 3 级 证据门",
    "gate_condition": "条件管道（KQ._condition_pipeline，随附判词）",
    "gate4_scope": "第 4 级 scope 门",
}
# 拒因分类标签（**只归并不判定**）：键 = `_evidence_for` 的 stripped 判词
# 首段；值为 (英文键, 中文说明)。未登记的键落 `unknown_*` 并显式报出
# （fail-visible：分类表漏项不许静默吞掉）。
STRIP_CATEGORY = {
    "no_registry": ("no_registry", "来源未登记（work_sources 无该 work 行）"),
    "benchmark_source": ("benchmark_source",
                         "基准段剔除（segments.role=benchmark，基准上下文泄漏）"),
    "excluded_source_type": ("excluded_source_type",
                             "来源类型被排除（fixture/synthetic/commentary 等，"
                             "附具体类型）"),
    "excluded_use": ("excluded_use", "license 禁用用途（license_purposes 撞 "
                                     "excluded_uses）"),
    "text_version": ("text_version", "文本版本不在允许集（附具体版本）"),
    "mirror_dedup": ("mirror_dedup", "同（根作品, span）区间重复/镜像，不计独立"),
    "cross_work": ("cross_work", "跨作品（require_same_book 生效时不保留）"),
    "query_book_no_registry": ("query_book_no_registry",
                               "查询作品无登记行（require_same_book 无从比对）"),
}


# ------------------------------------------------------------ 只读连接层
def _ro_connect(db_path: Path):
    """真库一律 mode=ro 只读。**只读承诺在连接层成立**（不是靠自觉）。"""
    return sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True,
                           check_same_thread=False)


def open_ro_session(db_path: Path):
    """只读 SQLAlchemy session（engine 的 creator 直接给 mode=ro 连接）。

    不用 `app.db` 的全局 engine（那个按 LG_DATABASE_URL 走**可写**连接）——
    本解释器的所有查询都挂在这个只读 engine 上。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    eng = create_engine("sqlite://",
                        creator=lambda: _ro_connect(db_path), future=True)
    return sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)()


def resolve_db(explicit: str, repo_root: Path) -> tuple[Path, list[str]]:
    """库路径解析：--db 覆盖 → <repo>/data/... → 候选绝对路径。返回
    (选中路径, 候选清单)；候选全缺时选中路径仍返回首选（由调用方报
    「不可读」而不是崩）。"""
    cands: list[Path] = []
    if explicit:
        cands.append(Path(explicit))
    cands.append(Path(repo_root) / DB_CANDIDATE_REL)
    cands.append(Path(DB_FALLBACK_ABS))
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
    return datetime.datetime.now(datetime.timezone.utc).astimezone(
    ).isoformat(timespec="seconds")


# ---------------------------------------------------- 拒因分类（只归并）
def classify_stripped(stripped) -> dict:
    """`_evidence_for` 的 stripped 列表 → 分类计数。

    判词本体来自库（`f"{id}:{reason}"`），本函数**只做字符串归并**：
    键 = 判词首段（`excluded_source_type:fixture` → 键 `excluded_source_type`、
    附值 `fixture`）。未登记的判词落 `unknown:<判词首段>` 并单独报出，
    绝不静默归入已知类。"""
    by_cat: Counter = Counter()
    by_val: Counter = Counter()
    unknown: Counter = Counter()
    for entry in stripped:
        reason = entry.split(":", 1)[1] if ":" in entry else entry
        head, _, val = reason.partition(":")
        key = STRIP_CATEGORY.get(head, (f"unknown:{head}", "未登记判词"))
        by_cat[key[0]] += 1
        by_val[f"{key[0]}:{val}" if val else key[0]] += 1
        if head not in STRIP_CATEGORY:
            unknown[head] += 1
    return {"total": len(stripped),
            "by_category": dict(sorted(by_cat.items())),
            "by_category_value": dict(sorted(by_val.items())),
            "category_labels": {k: v[1] for k, v in STRIP_CATEGORY.items()},
            "unknown_categories": dict(sorted(unknown.items())),
            "unknown_categories_note": "分类表未收录的判词——fail-visible，"
                                       "不许静默归入已知类" if unknown else None,
            "raw": list(stripped)}


# ---------------------------------------------------- 逐策略逐级判词
def explain_strategy(s, st, policy: dict) -> dict:
    """单条策略的逐级判词。每一级都**照样求值并报出**（即使上一级已不过），
    `blocked_at` 单列「第一条不过的级」——解释器要的是可核证据，不是短路日志。"""
    eligible = KQ.eligible_statuses(st.version)
    g1_pass = st.status in eligible
    g1 = {"gate": "gate1_status", "order": 1, "name_cn": GATE_TITLE["gate1_status"],
          "source_symbol": f"app/knowledge_query.py:eligible_statuses("
                           f"{st.version!r})",
          "value": st.status, "expected": sorted(eligible),
          "pass": g1_pass,
          "reason": "status_in_eligible_set" if g1_pass
                    else f"status_not_eligible:{st.status}",
          "reason_source": "explainer（判据单源=eligible_statuses；"
                           "库在 query_knowledge 候选筛内联过滤，不产出判词）"}

    obs_ok = st.observation_status in KQ.ELIGIBLE_OBSERVATION
    g2 = {"gate": "gate2_observation", "order": 2,
          "name_cn": GATE_TITLE["gate2_observation"],
          "source_symbol": "app/knowledge_query.py:ELIGIBLE_OBSERVATION",
          "value": st.observation_status,
          "expected": sorted(KQ.ELIGIBLE_OBSERVATION),
          "pass": obs_ok,
          "reason": "observation_in_eligible_set" if obs_ok
                    else f"observation_not_eligible:{st.observation_status}",
          "reason_source": "explainer（判据单源=ELIGIBLE_OBSERVATION）"}

    refs, ev_count, stripped = KQ._evidence_for(s, st.id, policy)
    g3 = {"gate": "gate3_evidence", "order": 3,
          "name_cn": GATE_TITLE["gate3_evidence"],
          "source_symbol": "app/knowledge_query.py:_evidence_for",
          "pass": ev_count > 0,
          "reason": "pass" if ev_count > 0 else "excluded_no_evidence",
          "reason_source": "library（_evidence_for 的 evidence_count=0；"
                           "判词字面量同 query_knowledge 的淘汰项）",
          "evidence_count": ev_count, "n_refs": len(refs),
          "root_works": sorted({r["canonical_work"] for r in refs}),
          "instances_considered": len(refs) + len(stripped),
          "instances_considered_note": "= len(refs)+len(stripped)（_evidence_for "
                                       "每个实例二选一归入 refs 或 stripped）",
          "stripped": classify_stripped(stripped)}

    reason_c, comps, uncertain = KQ._condition_pipeline(
        s, st.id, dict(policy.get("semantic_requirements") or {}))
    gcond = {"gate": "gate_condition", "order": 4,
             "name_cn": GATE_TITLE["gate_condition"],
             "source_symbol": "app/knowledge_query.py:_condition_pipeline",
             "pass": reason_c is None,
             "reason": reason_c or "pass",
             "reason_source": "library（_condition_pipeline 的拒绝理由原样）",
             "components": comps, "n_uncertain": len(uncertain),
             "uncertain": uncertain}

    scope_verdict = KQ._scope_matches(s, st, policy)
    g4 = {"gate": "gate4_scope", "order": 5,
          "name_cn": GATE_TITLE["gate4_scope"],
          "source_symbol": "app/knowledge_query.py:_scope_matches",
          "value": st.scope, "scope_ids": list(st.scope_ids or []),
          "pass": scope_verdict == "pass", "reason": scope_verdict,
          "reason_source": "library（_scope_matches 判词原样，未改写）",
          "reject_category": (None if scope_verdict == "pass" else
                              scope_verdict[len("excluded_scope"):]
                              .lstrip("_") or "unspecified")}

    gates = {"gate1_status": g1, "gate2_observation": g2, "gate3_evidence": g3,
             "gate_condition": gcond, "gate4_scope": g4}
    blocked_at = next((k for k in GATE_ORDER if not gates[k]["pass"]), None)
    return {
        "strategy_id": st.id, "strategy_key": st.strategy_key,
        "version": st.version, "legacy_strategy_id": st.legacy_strategy_id,
        "abstract_operation": st.abstract_operation,
        "gates": gates,
        "blocked_at": blocked_at,
        "blocked_reason": None if blocked_at is None
                          else gates[blocked_at]["reason"],
        "all_gates_pass": blocked_at is None,
    }


# ------------------------------------------- 单源自证：复跑 query_knowledge
def _library_outcome(qr: dict) -> tuple[dict, set]:
    """query_knowledge 落点索引：key → 'selected' | 'rejected:<reason>'。"""
    out: dict[str, str] = {}
    for e in qr.get("selected") or []:
        out[e["strategy_key"]] = "selected"
    for r in qr.get("rejected") or []:
        k = r.get("strategy_key")
        if k is not None and k not in out:
            out[k] = f"rejected:{r.get('reason')}"
    return out, {r.get("strategy_key") for r in qr.get("rejected") or []
                 if r.get("reason") == "excluded_duplicate_lower_version"
                 and r.get("strategy_key") in out}


def library_crosscheck(s, policy: dict, explained: list[dict]) -> dict:
    """同 policy 现场复跑 `KQ.query_knowledge`（只读），逐条比对本解释器
    的落点。**这是「判词单源」的字面自证**：解释器说卡在哪，库自己也得
    说卡在同一处。对不上就如实报 disagree（不藏）。"""
    qr: dict
    err = None
    try:
        qr = KQ.query_knowledge(policy, s)
    except Exception as exc:                      # 对账失败即事实，不崩
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "rows": [], "n_agree": 0, "n_disagree": 0, "mismatches": []}
    idx, dedup_ambiguous = _library_outcome(qr)
    rows, mismatches = [], []
    for item in explained:
        key = item["strategy_key"]
        g1 = item["gates"]["gate1_status"]["pass"]
        g2 = item["gates"]["gate2_observation"]["pass"]
        if not (g1 and g2):
            expect = "candidate_filtered_out"     # 根本不进候选集
        else:
            g3 = item["gates"]["gate3_evidence"]
            gc = item["gates"]["gate_condition"]
            g4 = item["gates"]["gate4_scope"]
            if not g3["pass"]:
                expect = f"rejected:{g3['reason']}"
            elif not gc["pass"]:
                expect = f"rejected:{gc['reason']}"
            elif not g4["pass"]:
                expect = f"rejected:{g4['reason']}"
            else:
                expect = "selected"
        actual = idx.get(key, "candidate_filtered_out")
        note = None
        if key in dedup_ambiguous:
            note = ("同 key 多版本被 excluded_duplicate_lower_version 归并，"
                    "按 key 不可区分——本行不计入对账")
            agree = True
        else:
            agree = (actual == expect)
            if not agree:
                mismatches.append({"strategy_key": key, "explainer": expect,
                                   "library": actual})
        rows.append({"strategy_key": key, "explainer": expect,
                     "library": actual, "agree": agree, "note": note})
    return {"ok": err is None, "error": err,
            "query_knowledge_status": qr.get("status"),
            "query_knowledge_budget": qr.get("budget"),
            "selected_keys": sorted(k for k, v in idx.items()
                                    if v == "selected"),
            "rejected": qr.get("rejected"),
            "rows": rows, "n_agree": sum(1 for r in rows if r["agree"]),
            "n_disagree": sum(1 for r in rows if not r["agree"]),
            "mismatches": mismatches,
            "note": "判词单源自证：解释器落点 vs 库侧 query_knowledge 落点，"
                    "逐条对照（不一致即 n_disagree>0）"}


# ---------------------------------------------------------- 汇总与判据
def _sql_in(col: str, values) -> str:
    """单源常量 → SQL IN 列表（值全部来自 app.knowledge_query，不手抄）。"""
    return f"IN ({','.join(chr(39) + str(v) + chr(39) for v in sorted(values))})"


def summarize(explained: list[dict], policy: dict) -> dict:
    """逐级汇总计数：分布 / 卡点 / 拒因 / 证据净剩。"""
    blocked = Counter(i["blocked_at"] or "none(all_pass)" for i in explained)
    reason_by_gate: dict[str, Counter] = {}
    for i in explained:
        for g in GATE_ORDER:
            reason_by_gate.setdefault(g, Counter())[
                i["gates"][g]["reason"]] += 1
    ev_total = sum(i["gates"]["gate3_evidence"]["evidence_count"]
                   for i in explained)
    strip_cat: Counter = Counter()
    strip_val: Counter = Counter()
    for i in explained:
        g3 = i["gates"]["gate3_evidence"]
        strip_cat.update(g3["stripped"]["by_category"])
        strip_val.update(g3["stripped"]["by_category_value"])
    roots = sorted({r for i in explained
                    for r in i["gates"]["gate3_evidence"]["root_works"]})
    return {
        "n_strategies": len(explained),
        "by_status": dict(sorted(Counter(
            i["gates"]["gate1_status"]["value"] for i in explained).items())),
        "by_observation_status": dict(sorted(Counter(
            i["gates"]["gate2_observation"]["value"] for i in explained).items())),
        "by_scope": dict(sorted(Counter(
            i["gates"]["gate4_scope"]["value"] for i in explained).items())),
        "pass_per_gate": {g: sum(1 for i in explained
                                 if i["gates"][g]["pass"]) for g in GATE_ORDER},
        "blocked_at_counts": dict(sorted(blocked.items())),
        "blocked_reason_per_gate": {g: dict(sorted(c.items()))
                                    for g, c in reason_by_gate.items()},
        "evidence": {
            "net_evidence_intervals": ev_total,
            "instances_considered": sum(
                i["gates"]["gate3_evidence"]["instances_considered"]
                for i in explained),
            "stripped_total": sum(strip_cat.values()),
            "stripped_by_category": dict(sorted(strip_cat.items())),
            "stripped_by_category_value": dict(sorted(strip_val.items())),
            "distinct_root_works": len(roots),
            "root_works": roots,
            "n_strategies_with_net_evidence": sum(
                1 for i in explained
                if i["gates"]["gate3_evidence"]["evidence_count"] > 0)},
        "n_all_gates_pass": sum(1 for i in explained if i["all_gates_pass"]),
        "n_beyond_status_gate": sum(
            1 for i in explained
            if i["gates"]["gate1_status"]["pass"]
            and i["gates"]["gate2_observation"]["pass"]),
    }


def closure_criteria(explained: list[dict]) -> list[dict]:
    """可机械执行的收口判据清单（**只列判据与当前读数，不裁定、不建议**）。

    每条附：判词陈述 / 阈值 / 当前值 / 机械判定（阈值是否达成）/ 只读 SQL
    或调用式。阈值一律取存在性下界 1（**存在性下界是占位，不是拍板值**——
    真正阈值属策略审查席的裁定范围，本报告不越界代裁）。"""
    st_vals = {i["gates"]["gate1_status"]["value"] for i in explained}
    obs_vals = {i["gates"]["gate2_observation"]["value"] for i in explained}
    eligible = KQ.eligible_statuses(None)
    n_elig_status = sum(1 for i in explained
                        if i["gates"]["gate1_status"]["pass"])
    n_elig_obs = sum(1 for i in explained
                     if i["gates"]["gate2_observation"]["pass"])
    n_promoted_scoped = sum(
        1 for i in explained
        if i["gates"]["gate1_status"]["value"] != "hypothesis"
        and i["gates"]["gate4_scope"]["value"] != "UNCERTAIN")
    n_net_ev = sum(1 for i in explained
                   if i["gates"]["gate3_evidence"]["evidence_count"] > 0)
    n_no_bench_strip = sum(
        1 for i in explained
        if i["gates"]["gate3_evidence"]["instances_considered"] > 0
        and i["gates"]["gate3_evidence"]["stripped"]["by_category"].get(
            "benchmark_source", 0) == 0)
    n_scope_pass = sum(1 for i in explained
                       if i["gates"]["gate4_scope"]["pass"])
    n_all = sum(1 for i in explained if i["all_gates_pass"])
    n_net_segments = sum(i["gates"]["gate3_evidence"]["evidence_count"]
                         for i in explained)
    n_roots = len(sorted({r for i in explained
                          for r in i["gates"]["gate3_evidence"]["root_works"]}))
    n_bench = sum(i["gates"]["gate3_evidence"]["stripped"]["by_category"].get(
        "benchmark_source", 0) for i in explained)
    n_strip = sum(i["gates"]["gate3_evidence"]["stripped"]["total"]
                  for i in explained)
    ev_call = ("python scripts/k5_promotion_gate_explain.py --print-only"
               " → strategies[].gates.gate3_evidence.evidence_count"
               "（= KQ._evidence_for 的第 2 个返回值）")
    strip_call = ("python scripts/k5_promotion_gate_explain.py --print-only"
                  " → strategies[].gates.gate3_evidence.stripped"
                  ".by_category（= KQ._evidence_for 第 3 个返回值的归并）")
    scope_call = ("python scripts/k5_promotion_gate_explain.py --print-only"
                  " → strategies[].gates.gate4_scope.reason"
                  "（= KQ._scope_matches 的返回值）")
    items = [
        ("G1", "至少 1 张 status 落在 eligible_statuses(version) 的卡",
         1, n_elig_status,
         f"SELECT COUNT(*) FROM expression_strategies_v2 WHERE status "
         f"{_sql_in('status', eligible)}",
         f"判据集合现场读出 kq.eligible_statuses(None) = {sorted(eligible)}；"
         f"库内 status 取值实测 {sorted(st_vals)}"),
        ("G2", "至少 1 张卡 status<>'hypothesis' 且 scope<>'UNCERTAIN'",
         1, n_promoted_scoped,
         "SELECT COUNT(*) FROM expression_strategies_v2 WHERE "
         "status<>'hypothesis' AND scope<>'UNCERTAIN'",
         "两列均为既有行取值；本报告只读不裁定、不代改"),
        ("G3", "至少 1 张卡 observation_status 落在 ELIGIBLE_OBSERVATION",
         1, n_elig_obs,
         "SELECT COUNT(*) FROM expression_strategies_v2 WHERE "
         "observation_status "
         f"{_sql_in('observation_status', KQ.ELIGIBLE_OBSERVATION)}",
         f"判据集合现场读出 kq.ELIGIBLE_OBSERVATION = "
         f"{sorted(KQ.ELIGIBLE_OBSERVATION)}；库内 observation_status 取值"
         f"实测 {sorted(obs_vals)}"),
        ("G4", "至少 1 条策略 _evidence_for 的 evidence_count ≥ 1（证据净剩非空）",
         1, n_net_ev, ev_call,
         "该计数无纯 SQL 等价（区间按 (canonical_work_id, span) 聚合去重），"
         "口径只存在于 _evidence_for 内——只可调用式核对"),
        ("G5", "非 benchmark 且过来源闸的净剩段数 ≥ 2（去重后独立根作品口径）",
         2, n_roots, "见 G5-SQL（只读；与 _evidence_for 的去重口径并用）",
         f"净剩段数实测 {n_net_segments}、去重后独立根作品数实测 {n_roots}"),
        ("G6", "至少 1 条策略不被 benchmark_source 剔空（剥离后仍有候选实例）",
         1, n_no_bench_strip, strip_call,
         f"benchmark_source 剔除实测 {n_bench} 条（占被剔除实例总数 "
         f"{n_strip} 的 "
         f"{'—' if not n_strip else format(100.0 * n_bench / n_strip, '.1f')}"
         f"%）——本轮主拒因"),
        ("G7", "至少 1 条策略 scope 判词为 pass（_scope_matches）",
         1, n_scope_pass, scope_call,
         "scope 需先有非 UNCERTAIN 取值；判词只报不改"),
        ("G8", "至少 1 条策略五级全通（blocked_at 为空）",
         1, n_all,
         "python scripts/k5_promotion_gate_explain.py --print-only"
         " → summary.n_all_gates_pass",
         f"全通条数实测 {n_all}"),
    ]
    out = [{"id": cid, "statement": stmt, "target": target,
            "current": current, "satisfied": current >= target,
            "executable": sql, "note": note,
            "target_is_placeholder": True,
            "target_note": "存在性下界 1（或 2）为占位阈值——真实阈值属策略"
                           "审查席裁定范围，本报告只给机械读数，不代裁"}
           for cid, stmt, target, current, sql, note in items]
    out.append({
        "id": "G5-SQL",
        "statement": "只读 SQL：非 benchmark、status∈ELIGIBLE_INSTANCE_STATUS、"
                     "来源类型不在排除集、文本版本在允许集的实例行数"
                     "（按策略参数化 strategy_id）",
        "target": None, "current": None, "satisfied": None,
        "executable": (
            "SELECT COUNT(*) FROM strategy_instances si "
            "JOIN segments sg ON sg.id = si.segment_id "
            "JOIN work_sources ws ON ws.work_id = si.work_id "
            f"WHERE si.status {_sql_in('si.status', KQ.ELIGIBLE_INSTANCE_STATUS)} "
            "AND (sg.role IS NULL OR sg.role <> 'benchmark') "
            "AND ws.source_type NOT "
            f"{_sql_in('ws.source_type', KQ.DEFAULT_EXCLUDED_SOURCE_TYPES)} "
            "AND si.text_version "
            f"{_sql_in('si.text_version', KQ.DEFAULT_ALLOWED_TEXT_VERSIONS)} "
            "AND si.strategy_id = :strategy_id"),
        "note": "值域全部由 app.knowledge_query 常量按运行期展开（本脚本生成，"
                "未手抄）；本 SQL 不含镜像去重，跨根作品去重口径以 _evidence_for "
                "为准（G5 的达标判定用后者）",
        "target_is_placeholder": True,
        "target_note": "只读核对式，非阈值判据"})
    return out


# ------------------------------------------------------------ 组装报告
def single_source_manifest() -> dict:
    """单源复用清单：判据本体全部来自 app.knowledge_query（可对账）。"""
    return {
        "module": "app/knowledge_query.py",
        "symbols": [
            {"gate": "gate1_status", "symbol": "eligible_statuses(version)",
             "kind": "function", "used_as": "第 1 级 status 门判据"},
            {"gate": "gate2_observation", "symbol": "ELIGIBLE_OBSERVATION",
             "kind": "constant", "used_as": "第 2 级 observation 门判据"},
            {"gate": "gate3_evidence", "symbol": "_evidence_for(s, id, policy)",
             "kind": "function",
             "used_as": "第 3 级证据门的 evidence_count 与 stripped 判词"},
            {"gate": "gate_condition", "symbol": "_condition_pipeline(s, id, req)",
             "kind": "function", "used_as": "条件管道判词（随附，不另编号）"},
            {"gate": "gate4_scope", "symbol": "_scope_matches(s, strategy, policy)",
             "kind": "function", "used_as": "第 4 级 scope 门判词"},
            {"gate": "evidence_side", "symbol": "ELIGIBLE_INSTANCE_STATUS",
             "kind": "constant", "used_as": "证据侧 status 口径（对账用）"},
            {"gate": "evidence_side", "symbol": "DEFAULT_EXCLUDED_SOURCE_TYPES",
             "kind": "constant", "used_as": "来源类型排除集（对账用）"},
            {"gate": "evidence_side", "symbol": "DEFAULT_ALLOWED_TEXT_VERSIONS",
             "kind": "constant", "used_as": "文本版本允许集（对账用）"},
            {"gate": "crosscheck", "symbol": "query_knowledge(policy, s)",
             "kind": "function",
             "used_as": "单源自证：现场复跑并逐条比对落点"},
        ],
        "local_criteria_written": "none（本文件不定义任何合格集/判据；"
                                  "所有门判据均引用上面的符号）",
        "local_compositions": [
            "gate1/gate2 的 reject 理由串（库在候选筛里内联过滤，不产出判词）",
            "stripped 判词的分类归并（只归并字符串，不改判词）",
        ],
    }


def build_report(repo_root: Path, db_path: Path, policy: dict) -> dict:
    """主组装：只读 session → 逐条判词 → 汇总 → 单源对账 → 判据清单。"""
    from sqlalchemy import text
    db_avail, db_err, s = False, None, None
    explained: list[dict] = []
    cross: dict = {"ok": False, "error": "库未打开", "rows": [],
                   "n_agree": 0, "n_disagree": 0, "mismatches": []}
    try:
        s = open_ro_session(db_path)
        s.execute(text("SELECT 1")).fetchone()   # 真开一次只读连接（fail-visible）
        rows = (s.query(ExpressionStrategyV2)
                .order_by(ExpressionStrategyV2.strategy_key,
                          ExpressionStrategyV2.id).all())
        explained = [explain_strategy(s, st, policy) for st in rows]
        cross = library_crosscheck(s, policy, explained)
        db_avail = True
    except Exception as exc:                  # 库不可读 = 证据不足，不猜
        db_err = f"{type(exc).__name__}: {exc}"
    finally:
        if s is not None:
            s.close()
    summary = summarize(explained, policy) if explained else {
        "n_strategies": 0, "blocked_at_counts": {},
        "evidence": {"net_evidence_intervals": None,
                     "stripped_by_category": None}}
    return {
        "schema": SCHEMA,
        "explain": EXPLAIN,
        "task": TASK,
        "generated_at": _now_iso(),
        "repo_root": portable_path(repo_root, repo_root),
        "db_path": portable_path(db_path, repo_root),
        "db_mode": "ro",                       # 恒为只读档（无写入档）
        "db_opened": db_avail,
        "db_available": db_avail,
        "db_error": db_err,
        "policy": policy,
        "gate_order": list(GATE_ORDER),
        "gate_titles": GATE_TITLE,
        "single_source": single_source_manifest(),
        "constants_as_read": {
            "ELIGIBLE_STATUS_DEFAULT": sorted(KQ.eligible_statuses(None)),
            "ELIGIBLE_STATUS_BY_VERSION": {
                v: sorted(s_) for v, s_ in
                KQ.ELIGIBLE_STATUS_BY_VERSION.items()},
            "ELIGIBLE_OBSERVATION": sorted(KQ.ELIGIBLE_OBSERVATION),
            "ELIGIBLE_INSTANCE_STATUS": sorted(KQ.ELIGIBLE_INSTANCE_STATUS),
            "DEFAULT_EXCLUDED_SOURCE_TYPES": sorted(
                KQ.DEFAULT_EXCLUDED_SOURCE_TYPES),
            "DEFAULT_ALLOWED_TEXT_VERSIONS": sorted(
                KQ.DEFAULT_ALLOWED_TEXT_VERSIONS),
        },
        "strategies": explained,
        "summary": summary,
        "library_crosscheck": cross,
        "closure_criteria": closure_criteria(explained) if explained else [],
        "closure_criteria_note": "只列机械判据与当前读数；阈值是存在性占位，"
                                 "不裁定任何 status/scope，不给升格建议",
        "discipline": {
            "db_mode": "ro", "db_writes": 0, "model_calls": 0, "git_writes": 0,
            "no_status_or_scope_adjudication": True,
            "no_one_click_promotion_advice": True,
            "no_gate_relaxation": True,
            "criteria_single_sourced_from": "app/knowledge_query.py",
            "forbid": ["裁定 status/scope", "改任何一行数据",
                       "一键升格建议", "放宽任何既有门", "触碰原始语料"],
        },
    }


# ------------------------------------------------------------ 文档渲染
def _md_table(headers: list[str], rows: list[list[str]]) -> str:
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join("---" for _ in headers) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(out)


def render_doc(report: dict, policy: dict) -> str:
    """机器区渲染：逐策略逐级判词表 + 汇总 + 收口判据清单 + 单源清单。"""
    L: list[str] = [DOC_BEGIN, ""]
    L.append(f"<!-- 本段由 scripts/k5_promotion_gate_explain.py 生成于 "
             f"{report['generated_at']}；库 {report['db_path']}（"
             f"db_mode={report['db_mode']}，opened={report['db_opened']}）"
             f"；勿手改，复跑即重生成 -->")
    L.append("")
    L.append(f"### M1 逐策略逐级判词（{report['summary'].get('n_strategies')} 条）")
    L.append("")
    if report["db_opened"] is False:
        L.append(f"**库不可读（{report['db_error']}）——本节无读数，不猜。**")
        L.append("")
        return "\n".join(L + [DOC_END, ""])
    L.append("门序（与 `KQ.query_knowledge` 执行顺序对齐）："
             + " → ".join(f"`{g}`" for g in report["gate_order"])
             + "；`卡在哪一级` 取**第一条不过的级**。")
    L.append("")
    head = ["strategy_key", "version", "①status", "②obs", "③证据净剩",
            "③拒因分类计数", "条件判词", "④scope 判词", "卡在哪一级"]
    rows = []
    for i in report["strategies"]:
        g1, g2 = i["gates"]["gate1_status"], i["gates"]["gate2_observation"]
        g3, gc = i["gates"]["gate3_evidence"], i["gates"]["gate_condition"]
        g4 = i["gates"]["gate4_scope"]
        sv = g3["stripped"]["by_category_value"]
        sv_txt = ("—（无实例）" if not sv
                  else "、".join(f"`{k}`={v}" for k, v in sv.items()))
        rows.append([i["strategy_key"], i["version"],
                     f"`{g1['value']}` {'✅' if g1['pass'] else '❌'}",
                     f"`{g2['value']}` {'✅' if g2['pass'] else '❌'}",
                     f"{g3['evidence_count']} 段/区间"
                     f"（根作品 {g3['root_works'] or '—'}）",
                     sv_txt,
                     f"`{gc['reason']}`",
                     f"`{g4['reason']}`（scope={g4['value']}）",
                     f"**{i['blocked_at'] or '全通'}**"])
    L.append(_md_table(head, rows))
    L.append("")
    L.append("### M2 汇总计数（机器读数，逐项可复算）")
    L.append("")
    s = report["summary"]
    L.append(_md_table(["项", "值"], [
        ["策略总数 n_strategies", s["n_strategies"]],
        ["status 分布", json.dumps(s["by_status"], ensure_ascii=False)],
        ["observation_status 分布",
         json.dumps(s["by_observation_status"], ensure_ascii=False)],
        ["scope 分布", json.dumps(s["by_scope"], ensure_ascii=False)],
        ["过第 1/2 级（候选筛）条数", s["n_beyond_status_gate"]],
        ["各级通过条数",
         json.dumps(s["pass_per_gate"], ensure_ascii=False)],
        ["卡点分布（blocked_at）",
         json.dumps(s["blocked_at_counts"], ensure_ascii=False)],
        ["五级全通条数", s["n_all_gates_pass"]],
        ["证据：净剩区间数", s["evidence"]["net_evidence_intervals"]],
        ["证据：考虑实例数", s["evidence"]["instances_considered"]],
        ["证据：被剔除实例数", s["evidence"]["stripped_total"]],
        ["证据：拒因分类计数",
         json.dumps(s["evidence"]["stripped_by_category"],
                    ensure_ascii=False)],
        ["证据：拒因分类×取值",
         json.dumps(s["evidence"]["stripped_by_category_value"],
                    ensure_ascii=False)],
        ["证据：去重后独立根作品数", s["evidence"]["distinct_root_works"]],
        ["证据：净剩非空的策略条数", s["evidence"]["n_strategies_with_net_evidence"]],
    ]))
    L.append("")
    labels = (report["strategies"][0]["gates"]["gate3_evidence"]["stripped"]
              ["category_labels"] if report["strategies"] else {})
    if labels:
        L.append("**拒因分类口径**（`_evidence_for` 产出的判词 → 分类；"
                 "**只归并不判定**，判词本体逐字来自库）：")
        L.append("")
        L.append(_md_table(["分类键", "说明"],
                          [[f"`{k}`", v] for k, v in labels.items()]))
        L.append("")
    unknown = {i["strategy_key"]: i["gates"]["gate3_evidence"]["stripped"]
               ["unknown_categories"] for i in report["strategies"]
               if i["gates"]["gate3_evidence"]["stripped"]
               ["unknown_categories"]}
    L.append(f"- 分类表未收录的判词（fail-visible，不静默归并）："
             + ("**无**" if not unknown else json.dumps(unknown,
                                                      ensure_ascii=False)))
    L.append("")
    L.append("### M3 可机械执行的收口判据清单（只给读数与核对式，**不裁定**）")
    L.append("")
    L.append(_md_table(["id", "判词", "阈值", "当前读数", "机械判定", "只读 SQL / 调用式"],
                      [[c["id"], c["statement"], c["target"], c["current"],
                        ("达标" if c["satisfied"] else "未达标")
                        if c["satisfied"] is not None else "（核对式）",
                        f"`{c['executable']}`" if len(c["executable"]) < 160
                        else f"`{c['executable'][:157]}…`（完整式见 JSON "
                             f"closure_criteria）"]
                       for c in report["closure_criteria"]]))
    L.append("")
    L.append(f"> 阈值说明：{report['closure_criteria'][0]['target_note'] if report['closure_criteria'] else ''}")
    L.append("")
    L.append("### M4 单源复用清单（判据本体全部取自 `app/knowledge_query.py`）")
    L.append("")
    L.append(_md_table(["门", "复用符号", "类型", "用途"],
                      [[c["gate"], f"`{c['symbol']}`", c["kind"], c["used_as"]]
                       for c in report["single_source"]["symbols"]]))
    L.append("")
    L.append(f"- 本地另写的判据：{report['single_source']['local_criteria_written']}")
    L.append(f"- 本地仅做组合（不改判据本体）："
             + "；".join(report["single_source"]["local_compositions"]))
    L.append("")
    L.append("### M5 逐字单源自证（复跑 `KQ.query_knowledge` 对账）")
    L.append("")
    cc = report["library_crosscheck"]
    if cc.get("error"):
        L.append(f"**对账未成立：{cc['error']}——不猜。**")
    else:
        L.append(f"- `query_knowledge.status` = `{cc.get('query_knowledge_status')}`；"
                 f"一致 {cc['n_agree']} 条 / 不一致 {cc['n_disagree']} 条。")
        L.append("")
        L.append(_md_table(["strategy_key", "解释器落点", "库侧落点", "一致"],
                          [[r["strategy_key"], f"`{r['explainer']}`",
                            f"`{r['library']}`", "✅" if r["agree"] else "❌"]
                           for r in cc.get("rows") or []]))
    L.append("")
    L.append("### M6 本次运行参数与纪律")
    L.append("")
    L.append(_md_table(["项", "值"], [
        ["db_path", f"`{report['db_path']}`"],
        ["db_mode", f"`{report['db_mode']}`"],
        ["db_opened", str(report["db_opened"])],
        ["policy（本次判词所依据的查询 policy）",
         "`" + json.dumps(policy, ensure_ascii=False,
                          sort_keys=True) + "`"],
        ["零库写 / 零模型调用 / 零 git 写", "0 / 0 / 0"],
        ["裁定 status 或 scope", "未做（`discipline.no_status_or_scope_"
                                 "adjudication=true`）"],
        ["一键升格建议", "未给（属策略审查席）"],
    ]))
    L.append("")
    L.append(DOC_END)
    return "\n".join(L) + "\n"


def write_doc(path: Path, region: str) -> str:
    """只重写机器区（DOC_BEGIN..DOC_END）；叙述段（标记之外）原样保留。
    文件不存在 → 写「标题 + 机器区」骨架。幂等：同库复跑内容不变。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    old = path.read_text(encoding="utf-8") if path.exists() else None
    if old and DOC_BEGIN in old and DOC_END in old:
        pre = old.split(DOC_BEGIN)[0]
        post = old.split(DOC_END, 1)[1]
        new = pre + region.rstrip("\n") + post
        action = "machine-region-refreshed"
    else:
        new = (f"# K5 晋升三级门逐条解释（{report_title_hint()}）\n\n"
               f"{region}")
        action = "skeleton-created"
    path.write_text(new, encoding="utf-8")
    return action


def report_title_hint() -> str:
    return "机器生成；叙述段见标记之外"


# ------------------------------------------------------------------ CLI
def build_policy(book_id: str, policy_json: str) -> dict:
    """policy 构造：默认空 policy（= 无查询作品，与主控 2026-09-26 真库
    实测口径逐字一致）；--book-id / --policy-json 只做加严侧输入，
    **不放宽任何服务端硬拦**（knowledge_query 对 source_policy 是并集/
    交集封底，调用方传什么都换不掉默认排除集）。"""
    policy: dict = {}
    if policy_json:
        policy = json.loads(Path(policy_json).read_text(encoding="utf-8"))
    if book_id:
        policy["book_id"] = book_id
    return policy


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo-root", default=str(ROOT), dest="repo_root",
                    help="仓库根（缺省=脚本自身推导的仓库根）")
    ap.add_argument("--db", default="", help="真库路径覆盖（缺省 <repo>/"
                    "data/language_genome.db；再缺则候选绝对路径）")
    ap.add_argument("--book-id", default="", dest="book_id",
                    help="查询作品 id（缺省空 policy——与主控实测口径一致）")
    ap.add_argument("--policy-json", default="", dest="policy_json",
                    help="policy JSON 文件覆盖（只可加严，不放宽服务端硬拦）")
    ap.add_argument("--print-only", action="store_true", dest="print_only",
                    help='只打印 JSON，零写盘（--json-out/--doc-out 一并忽略）')
    ap.add_argument("--json-out", default="", dest="json_out",
                    help="JSON 落盘路径（\"\"＝不落盘）")
    ap.add_argument("--doc-out", default="", dest="doc_out",
                    help=f"报告文档落盘路径（缺省 <repo>/{DEFAULT_DOC_REL}；"
                         '"--print-only" 忽略本项）')
    a = ap.parse_args(argv)
    repo_root = Path(a.repo_root)
    db_path, cands = resolve_db(a.db, repo_root)
    policy = build_policy(a.book_id, a.policy_json)
    report = build_report(repo_root, db_path, policy)
    report["db_candidates"] = cands
    text = json.dumps(report, ensure_ascii=False, indent=1)
    print(text)
    if a.print_only:
        print("[k5_promotion_gate_explain] --print-only：零写盘（仅 stdout）",
              file=sys.stderr)
        return 0
    if a.json_out:
        jp = Path(a.json_out)
        jp.parent.mkdir(parents=True, exist_ok=True)
        jp.write_text(text, encoding="utf-8")
        print(f"[k5_promotion_gate_explain] JSON 已写 {jp}", file=sys.stderr)
    doc_path = Path(a.doc_out) if a.doc_out else repo_root / DEFAULT_DOC_REL
    action = write_doc(doc_path, render_doc(report, policy))
    print(f"[k5_promotion_gate_explain] 报告文档已写 {doc_path}"
          f"（{action}）", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
