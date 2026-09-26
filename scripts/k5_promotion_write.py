"""K5 晋升写侧执行器（S4，2026-09-26 派工）——唯一允许改 status 的入口，默认 `--dry-run`。

补的是 `lg_k4_minimal_loop_20260926.md` 判定的「唯一真阻断」：非基准合格段下界
4,213（≥1）而 K3 合格集 = 0，8 条策略全为 `hypothesis`+`scope=UNCERTAIN`，status
与账面证据量零耦合 = **写侧从不运行**（`scripts/k5_promotion_write.py` 当时不存在）。
本件把「有没有执行器」从断言变成可机械验证的程序：判词逐条可核、写路径单事务单行、
审计行不可变。

## 判词单源（与 `k5_promotion_gate_explain.py` 同款纪律）

- 逐级门判词**一律取自** `scripts/k5_promotion_gate_explain.py::explain_strategy()`
  （其本体再引 `app/knowledge_query.py` 的 `eligible_statuses` /
  `ELIGIBLE_OBSERVATION` / `_evidence_for` / `_condition_pipeline` /
  `_scope_matches`）——本文件不另写一套门判据；
- 净剩证据与 stripped 判词调 `KQ._evidence_for`（解释器读的同一函数）；
- `src_ok` 严格布尔调 `scripts/source_check.py::parse_src_ok`（唯一支笔）；
- 复审标记取 `app/knowledge_extract.py::REVIEW_MARKER_NEW_DEF`（不新造标记位）；
- 库路径解析 / 只读连接 / policy 构造复用 `GE.resolve_db` /
  `GE.open_ro_session` / `GE.build_policy`。
- 本地只写**库里不存在、写侧固有**的两件东西：晋升级迁规则（阶梯 + 相邻性）与
  scope 推导规则（带版本号）。

## 晋升阶梯（硬契约 4「禁跳级」）

阶梯 = `hypothesis → observed → replicated → verified`，一次事务只许走**一级**。
每级落到具体列取值，全部是 `app/knowledge.py` 既有词表里的值（不新造枚举）：
`status` 列只在最后一级（→`verified`）真的变化，中间两级动的是 `observation_status`
——方案 §4.3 明令**不得**把 scope/观察/效果三类字段压成一条线性等级，所以本件的
「级」是**晋升位次**，落到列上仍按 §4.3 分开记录。跳级输入（`--to` 跨 ≥2 级、
非阶梯值、降级、契约外列取值）一律 `NO-PROMOTE`，原因里点名被跨过的级。

各级的机械前置（全部是证据的函数，不是运维的输入）：

| 目标级 | 前置（只读求值） |
|---|---|
| `observed` | 净剩证据 ≥1（`_evidence_for`）且其中 ≥1 条 `src_ok=true` 且 ≥1 条已按新口径复审 |
| `replicated` | 上一条 + 过闸证据的**独立根作品** ≥2（§4.3「独立来源复现」） |
| `verified` | 上一条 + 当前已在 `replicated` 级（相邻性保证）+ 事务后 `scope` 非 `UNCERTAIN` |

**gate2/gate4/条件管道不作晋升前置**（如实声明的取舍）：`gate4_scope` 与
`gate_condition` 的真假依赖**查询侧 policy**（`book_id`/`semantic_requirements`），
拿它们当晋升前置等于让任意调用方的 book_id 反向锁死晋升。本件只把**行内可判**的
条件当输入前置（证据净剩、src_ok、复审、独立复现、scope 可推导为确定值），并在这
同一次事务里把 `scope` 从 `UNCERTAIN` 推到证据支持的下一级（W3「scope 与 status
同事务一致变更」）。判词里仍**逐条报出**解释器的 gate1..gate4 原文供对账。

## scope 推导（带规则版本，S4 判红第 5 条）

`SCOPE_RULE_VERSION = "scope-derive-1"`：证据 work 必须全部在 `work_sources` 登记
（缺登记 ⇒ 无从推导，拒）；≥1 部 → `WORK`；≥2 部且作者 id 唯一非空 → `AUTHOR`；
≥2 部且题材交集非空 → `GENRE`；`GLOBAL` **永不**推导（§4.3 只预留契约）。
scope 同样逐级（`UNCERTAIN → WORK → AUTHOR → GENRE`）：推导结果跨 ≥2 级时
**截断到下一级**并在判词记 `scope_capped`——截断只发生在「证据支持得更远」的
超出场景，方向永远逐级，不放宽门槛。库里已是 `GLOBAL` 的行本件不写（fail-visible 拒）。

## 写侧纪律（硬契约 1/3/5 + W4/W5/U2）

- 缺省（不带 `--commit`）即 `--dry-run`：判定连接是 `mode=ro`，进程**不打开**任何
  可写连接 ⇒ 零写入；
- `--commit` 必须显式给出**恰好 1 条** `--strategy` 且 `--reviewer` 非空：选择集 ≠1
  条直接 rc=1 拒（`批量升格禁止`，U2「批量升格在任何读数下都不解冻」）；
- 一次 `--commit` = `BEGIN IMMEDIATE` → 单行 CAS `UPDATE`（WHERE 带旧值，rowcount
  必须 ==1，否则整体回滚）→ 同事务 INSERT **1** 行审计 → `COMMIT`；任一步抛异常
  即 `ROLLBACK`，两表都无变化；
- 审计行 append-only：建表同时建 `BEFORE UPDATE`/`BEFORE DELETE` 的 `RAISE(ABORT)`
  触发器；本文件**没有**任何 UPDATE/DELETE `promotion_audits` 的代码路径（测试用
  源码 grep 钉死）；只读校验入口 = `verify_promotion_audits()`。

用法：
    python scripts/k5_promotion_write.py                          # = --dry-run，真库逐条判词
    python scripts/k5_promotion_write.py --dry-run --strategy ESV2-x --to observed
    python scripts/k5_promotion_write.py --commit --strategy ESV2-x --to observed --reviewer R
    python scripts/k5_promotion_write.py --verify-audits
退出码：0=无拒绝；**2=至少一条 `NO-PROMOTE`（拒绝语义）**；1=用法/运行错误。
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # 工作树根
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import k5_promotion_gate_explain as GE                 # noqa: E402  判词单源
import source_check as sc                              # noqa: E402  parse_src_ok
from app import knowledge_extract as KE                # noqa: E402  REVIEW_MARKER
from app import knowledge_query as KQ                  # noqa: E402
from app.models import (ExpressionStrategyV2, Segment,  # noqa: E402
                        StrategyInstance, WorkSource)

TOOL = "k5_promotion_write"
SCHEMA = "k5_promotion_write/v1"
TASK = "K5 晋升写侧执行器（S4）：默认 --dry-run，单事务单行晋升 + 不可变审计行"
GATE_VERSION = "k5_promotion_write/v1"
SCOPE_RULE_VERSION = "scope-derive-1"
PROMOTE = "PROMOTE"
NO_PROMOTE = "NO-PROMOTE"
EXIT_OK, EXIT_ERROR, EXIT_REFUSED = 0, 1, 2
AUDIT_TABLE = "promotion_audits"

# 晋升阶梯（一次事务只走一级）与每级落地的列取值 (status, observation_status)
LADDER = ("hypothesis", "observed", "replicated", "verified")
LADDER_COLUMNS = {
    "hypothesis": ("hypothesis", "hypothesis"),
    "observed": ("hypothesis", "observed"),
    "replicated": ("hypothesis", "replicated"),
    "verified": ("verified", "replicated"),
}
SCOPE_LADDER = ("UNCERTAIN", "WORK", "AUTHOR", "GENRE")     # GLOBAL 永不推导/写入
MIN_REVIEWED = 1            # W2(ii)：新口径复审 ≥1 条
MIN_INDEPENDENT_ROOTS = 2   # §4.3 replicated：独立来源复现
DEFAULT_REVIEWER_DRY = "—（dry-run 不写审计行）"

AUDIT_COLUMNS = (
    "audit_id", "tool", "gate_version", "strategy_id", "strategy_key",
    "strategy_version", "from_status", "to_status", "status_column_from",
    "status_column_to", "observation_from", "observation_to", "scope_from",
    "scope_to", "scope_ids", "scope_basis", "scope_rule_version",
    "evidence_ref", "evidence_count", "reviewer", "ts", "policy_sha256",
    "columns_written")
AUDIT_INTEGER_COLUMNS = frozenset({"strategy_version", "evidence_count"})
AUDIT_DDL = "CREATE TABLE IF NOT EXISTS {} (\n  {}\n)".format(
    AUDIT_TABLE, ",\n  ".join(
        [f"{AUDIT_COLUMNS[0]} TEXT PRIMARY KEY"] + [
            f"{c} {'INTEGER' if c in AUDIT_INTEGER_COLUMNS else 'TEXT'} NOT NULL"
            for c in AUDIT_COLUMNS[1:]]))
AUDIT_IMMUTABLE_DDL = (
    f"CREATE TRIGGER IF NOT EXISTS {AUDIT_TABLE}_no_update BEFORE UPDATE ON"
    f" {AUDIT_TABLE} BEGIN SELECT RAISE(ABORT,"
    f" '{AUDIT_TABLE} is append-only: UPDATE blocked'); END;",
    f"CREATE TRIGGER IF NOT EXISTS {AUDIT_TABLE}_no_delete BEFORE DELETE ON"
    f" {AUDIT_TABLE} BEGIN SELECT RAISE(ABORT,"
    f" '{AUDIT_TABLE} is append-only: DELETE blocked'); END;",
)


class PromotionRefused(Exception):
    """判词为 NO-PROMOTE（拒绝语义，不是崩溃）。"""


class PromotionGuardError(Exception):
    """事务内实读与 CAS 前置不符（影响行数 ≠1）——整体回滚。"""


# ------------------------------------------------------------ 连接层
def open_ro_session(db_path: Path):
    """判定一律走只读连接（复用解释器的 mode=ro 入口，只读在连接层成立）。"""
    return GE.open_ro_session(db_path)


def open_write_connection(db_path: Path) -> sqlite3.Connection:
    """**只有 `--commit` 路径**允许调用：可写连接 + 手工事务控制。"""
    return sqlite3.connect(Path(db_path).as_posix(), isolation_level=None,
                           timeout=30.0)


def ensure_audit_schema(con: sqlite3.Connection) -> None:
    """建审计表/append-only 触发器（DDL only：不改任何数据行、不改既有表结构）。"""
    con.execute(AUDIT_DDL)
    for trig in AUDIT_IMMUTABLE_DDL:
        con.execute(trig)


# ------------------------------------------------- 证据事实（单源求值）
def _src_ok_true(integrity) -> bool:
    """`segments.integrity` → 严格布尔 `src_ok is True`（判定调 source_check 唯一支笔）。"""
    try:
        d = json.loads(integrity)
    except (TypeError, ValueError):
        return False
    if not isinstance(d, dict):
        return False
    return sc.parse_src_ok(d.get("src_ok")) is True


def evidence_facts(s, strategy_id: str, policy: dict) -> dict:
    """净剩证据 → 再按 `src_ok` 严格 true 与新口径复审收窄的**可用证据**集合。"""
    refs, count, stripped = KQ._evidence_for(s, strategy_id, policy)
    ids = [r["instance_id"] for r in refs]
    inst = {i.id: i for i in (s.query(StrategyInstance)
                              .filter(StrategyInstance.id.in_(ids)).all()
                              if ids else [])}
    seg_ids = sorted({i.segment_id for i in inst.values()})
    integ = dict(s.query(Segment.id, Segment.integrity)
                 .filter(Segment.id.in_(seg_ids)).all()) if seg_ids else {}
    src_ok_ids = [i for i in ids if _src_ok_true(integ.get(inst[i].segment_id))]
    reviewed_ids = [i for i in src_ok_ids
                    if (inst[i].reviewer_version or "")
                    == KE.REVIEW_MARKER_NEW_DEF]
    ok = set(reviewed_ids)
    return {
        "evidence_count": count,
        "instance_ids": ids,
        "stripped": GE.classify_stripped(stripped)["by_category"],
        "src_ok_ids": src_ok_ids,
        "reviewed_ids": reviewed_ids,
        "roots": sorted({r["canonical_work"] for r in refs
                         if r["instance_id"] in ok}),
        "works": sorted({inst[i].work_id for i in reviewed_ids}),
    }


# ------------------------------------------------------- scope 推导
def scope_candidates(s, work_ids: list[str]) -> tuple[list[tuple], str]:
    """证据作品 → 由低到高的候选 scope（各带 ids 与依据）；`GLOBAL` 永不出现。

    返回 (candidates, why_none)：缺登记行/无作品 ⇒ 空候选 + 原因（不猜）。"""
    if not work_ids:
        return [], "no_evidence_works"
    regs = {r.work_id: r for r in s.query(WorkSource)
            .filter(WorkSource.work_id.in_(work_ids)).all()}
    missing = sorted({w for w in work_ids if w not in regs})
    if missing:
        return [], f"no_registry:{','.join(missing)}"
    out: list[tuple] = [(
        "WORK", sorted(work_ids),
        f"{SCOPE_RULE_VERSION}: 证据覆盖 {len(work_ids)} 部登记作品")]
    authors = sorted({(regs[w].author_id or "") for w in work_ids})
    if len(work_ids) >= 2 and len(authors) == 1 and authors[0]:
        out.append(("AUTHOR", [authors[0]],
                    f"{SCOPE_RULE_VERSION}: {len(work_ids)} 部证据作品同一作者 "
                    f"{authors[0]}"))
    genres = [set(regs[w].genre_ids or []) for w in work_ids]
    common = sorted(set.intersection(*genres))
    if len(work_ids) >= 2 and common:
        out.append(("GENRE", common,
                    f"{SCOPE_RULE_VERSION}: {len(work_ids)} 部证据作品共有题材 "
                    f"{','.join(common)}"))
    return out, ""


def scope_step(current: str, derived: str) -> tuple[str, str | None]:
    """scope 也只走一级 → (本次写入值, 截断说明|None)。derived 不高于当前即不变。"""
    ci, di = SCOPE_LADDER.index(current), SCOPE_LADDER.index(derived)
    if di <= ci:
        return current, None
    if di == ci + 1:
        return derived, None
    nxt = SCOPE_LADDER[ci + 1]
    return nxt, f"derived={derived} 跨 {di - ci} 级，截断到下一级 {nxt}" \
        f"（scope 同样禁跳级，规则 {SCOPE_RULE_VERSION}）"


# ------------------------------------------------------------ 阶梯
def level_of(status: str, observation_status: str) -> str:
    """既有行列取值 → 当前晋升级；读不出来返回空串（调用方 fail-visible 拒）。"""
    if status == "verified":
        return "verified"
    if observation_status in ("hypothesis", "observed", "replicated"):
        return observation_status
    return ""


def line_of(v: dict) -> str:
    """唯一判词渲染：`--dry-run` 与 `--commit` 打的是同一串（硬契约 2）。"""
    head = f"{v['decision']} [{v['index']}/{v['total']}]"
    base = (f"{head} strategy={v['strategy_id']} key={v['strategy_key']} "
            f"level={v['level']}")
    ev = (f"evidence_count={v['evidence']['evidence_count']} "
          f"src_ok={len(v['evidence']['src_ok_ids'])} "
          f"reviewed={len(v['evidence']['reviewed_ids'])} "
          f"roots={v['evidence']['roots'] or '—'} "
          f"stripped={v['evidence']['stripped'] or '—'}")
    if v["decision"] == PROMOTE:
        p = v["plan"]
        capped = (f" (capped: {p['scope_capped']})"
                  if p.get("scope_capped") else "")
        return (f"{base} {PROMOTE}: instance={p['strategy_id']}"
                f", from={p['from_status']}, to={p['to_status']}"
                f", status_column={p['status_column_from']}->{p['status_column_to']}"
                f", observation={p['observation_from']}->{p['observation_to']}"
                f", scope: {p['scope_from']} -> {p['scope_to']}{capped}"
                f" [rule={SCOPE_RULE_VERSION}]"
                f", evidence_ids={p['evidence_ref']}"
                f", gate_version={p['gate_version']}, reviewer={p['reviewer']}"
                f" | {ev} | blocked_at={v['blocked_at']}")
    gates = " ".join(f"{g}={v['gates'][g]}" for g in GE.GATE_ORDER)
    note = f" note={v['note']}" if v.get("note") else ""
    return (f"{base} {NO_PROMOTE}: reason={v['reason']} "
            f"requested={v['requested'] or 'next'} "
            f"skipped={v['skipped'] or '—'}{note} | {ev} | {gates}"
            f" | blocked_at={v['blocked_at']}")


# ------------------------------------------------ 判词（唯一产出点）
def evaluate(s, st: ExpressionStrategyV2, policy: dict, requested: str | None,
             reviewer: str = DEFAULT_REVIEWER_DRY,
             index: int = 1, total: int = 1) -> dict:
    """**唯一的晋升判词函数**：`--dry-run` 与 `--commit` 都只经过这里。

    门级判词逐字取自 `GE.explain_strategy`；本函数只叠加写侧固有的**变迁规则**
    （阶梯相邻 + 各级证据前置 + scope 逐级推导）。"""
    explain = GE.explain_strategy(s, st, policy)
    gates = {g: explain["gates"][g]["reason"] for g in GE.GATE_ORDER}
    ev = evidence_facts(s, st.id, policy)
    level = level_of(st.status, st.observation_status)
    v = {"tool": TOOL, "strategy_id": st.id, "strategy_key": st.strategy_key,
         "version": st.version, "status": st.status,
         "observation_status": st.observation_status, "scope": st.scope,
         "scope_ids": list(st.scope_ids or []), "level": level,
         "requested": requested, "skipped": None, "note": None,
         "gates": gates, "blocked_at": explain["blocked_at"],
         "evidence": ev, "decision": NO_PROMOTE, "reason": None, "plan": None,
         "index": index, "total": total}

    def refuse(reason: str, skipped=None, note=None) -> dict:
        v["reason"], v["skipped"], v["note"] = reason, skipped, note
        v["line"] = line_of(v)
        return v

    # ── 变迁规则：先验输入合法性（跳级/降级/契约外取值）─────────────
    if not level:
        return refuse(f"off_ladder:observation_status={st.observation_status}"
                      "（行列取值不在阶梯词表内，fail-visible 不猜）")
    if st.scope not in SCOPE_LADDER:
        return refuse(f"off_ladder:scope={st.scope}"
                      "（GLOBAL 只预留契约；写侧不推导也不覆盖契约外取值）")
    li = LADDER.index(level)
    if requested is None:
        ti = li + 1
    elif requested not in LADDER:
        return refuse(f"off_ladder:to={requested}")
    else:
        ti = LADDER.index(requested)
    if ti >= len(LADDER):
        return refuse(f"already_top:{level}（verified 是本阶梯顶，效果层/范围层"
                      "推广不属本件职权）")
    if ti == li:
        return refuse(f"no_step:{level}（目标级即当前级）")
    if ti < li:
        return refuse(f"not_promotion:{level}->{requested}"
                      "（降级/横向不是晋升；retired/superseded 走人审）")
    if ti - li > 1:
        return refuse(f"skip_ladder:{level}->{requested}"
                      f"（跨 {ti - li} 级，禁跳级）", list(LADDER[li + 1:ti]))
    target = LADDER[ti]
    v["target"] = target

    # ── 硬契约 2：证据为空即拒（判词字面量取自库/解释器）───────────
    if ev["evidence_count"] == 0:
        return refuse(gates["gate3_evidence"],
                      note="净剩 0 段：晋升无输入，问题在抽取侧不在闸")
    if not ev["src_ok_ids"]:
        return refuse(f"no_src_ok_evidence:净剩 {ev['evidence_count']} 段，但没有"
                      "一条 segments.integrity.src_ok 是严格 true")
    if len(ev["reviewed_ids"]) < MIN_REVIEWED:
        return refuse(f"no_review_new_def:过 src_ok 的证据里 reviewer_version=="
                      f"{KE.REVIEW_MARKER_NEW_DEF} 的有 "
                      f"{len(ev['reviewed_ids'])} 条（须 ≥{MIN_REVIEWED}）")
    if target == "replicated" and len(ev["roots"]) < MIN_INDEPENDENT_ROOTS:
        return refuse(f"single_root_no_replication:独立根作品 {len(ev['roots'])} 部"
                      f"（须 ≥{MIN_INDEPENDENT_ROOTS}，§4.3 独立来源复现）")

    cands, why_none = scope_candidates(s, ev["works"])
    if not cands:
        return refuse(f"scope_undeterminable:{why_none}")
    scope_to, capped = scope_step(st.scope, cands[-1][0])
    payload = next(((ids, basis) for (lv, ids, basis) in cands if lv == scope_to),
                   None)
    if payload is None:
        scope_ids = list(st.scope_ids or [])
        basis = f"{SCOPE_RULE_VERSION}: scope 不变（{st.scope}）"
    else:
        scope_ids, basis = payload
    if target == "verified" and scope_to == "UNCERTAIN":
        return refuse("scope_still_uncertain（verified 级要求事务后 scope 确定）")

    status_to, obs_to = LADDER_COLUMNS[target]
    plan = {
        "strategy_id": st.id, "strategy_key": st.strategy_key,
        "from_status": level, "to_status": target,
        "status_column_from": st.status, "status_column_to": status_to,
        "observation_from": st.observation_status, "observation_to": obs_to,
        "scope_from": st.scope, "scope_to": scope_to, "scope_ids": scope_ids,
        "scope_basis": basis, "scope_capped": capped,
        "evidence_ref": list(ev["reviewed_ids"]),
        "evidence_count": len(ev["reviewed_ids"]),
        "gate_version": GATE_VERSION, "reviewer": reviewer,
        "policy_sha256": KQ.policy_sha256(policy),
    }
    v["decision"] = PROMOTE
    v["reason"] = "step_preconditions_met"
    v["plan"] = plan
    v["note"] = (f"scope_capped: {capped}" if capped else None)
    v["line"] = line_of(v)
    return v


# -------------------------------------------------------- 写侧（单事务）
def audit_id_for(plan: dict) -> str:
    """审计行主键 = 变迁负载规范化哈希（同一次变迁不可重复入账，重放天然撞键）。"""
    payload = KQ.canonical_json({k: plan[k] for k in (
        "strategy_id", "from_status", "to_status", "scope_to", "evidence_ref",
        "gate_version")})
    return "PAUD-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).astimezone(
        ).isoformat(timespec="seconds")


def commit_promotion(db_path: Path, verdict: dict) -> dict:
    """一次事务：CAS UPDATE 1 行 + INSERT 1 行审计；任一步异常整体回滚。"""
    plan, st = verdict["plan"], verdict
    con = open_write_connection(db_path)
    ts = _now_iso()
    try:
        ensure_audit_schema(con)
        con.execute("BEGIN IMMEDIATE")
        try:
            cur = con.execute(
                "UPDATE expression_strategies_v2 SET status=?,"
                " observation_status=?, scope=?, scope_ids=?, scope_basis=?"
                " WHERE id=? AND status=? AND observation_status=? AND scope=?",
                (plan["status_column_to"], plan["observation_to"],
                 plan["scope_to"], KQ.canonical_json(plan["scope_ids"]),
                 plan["scope_basis"], plan["strategy_id"],
                 st["status"], st["observation_status"], st["scope"]))
            if cur.rowcount != 1:
                raise PromotionGuardError(
                    f"影响行数={cur.rowcount}（必须 1）——行已被并发改变或 CAS 失配")
            con.execute(
                f"INSERT INTO {AUDIT_TABLE} ("
                + ", ".join(AUDIT_COLUMNS) + ") VALUES ("
                + ",".join("?" * len(AUDIT_COLUMNS)) + ")",
                (audit_id_for(plan), TOOL, plan["gate_version"],
                 plan["strategy_id"], plan["strategy_key"], st["version"],
                 plan["from_status"], plan["to_status"],
                 plan["status_column_from"], plan["status_column_to"],
                 plan["observation_from"], plan["observation_to"],
                 plan["scope_from"], plan["scope_to"],
                 KQ.canonical_json(plan["scope_ids"]), plan["scope_basis"],
                 SCOPE_RULE_VERSION, KQ.canonical_json(plan["evidence_ref"]),
                 plan["evidence_count"], plan["reviewer"], ts,
                 plan["policy_sha256"],
                 KQ.canonical_json({"status": plan["status_column_to"],
                                    "observation_status":
                                        plan["observation_to"],
                                    "scope": plan["scope_to"]})))
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        return {"committed": True, "audit_id": audit_id_for(plan), "ts": ts,
                "strategy_id": plan["strategy_id"],
                "from_status": plan["from_status"],
                "to_status": plan["to_status"], "rows_changed": 1,
                "audit_rows": 1}
    finally:
        con.close()


# ------------------------------------------- 审计行只读校验（硬契约 5）
def verify_promotion_audits(db_path: Path) -> dict:
    """审计行的**只读**校验：连接层 `mode=ro`，本函数无任何 UPDATE/DELETE 路径。"""
    con = GE._ro_connect(Path(db_path))
    out = {"tool": TOOL, "db_mode": "ro", "ok": True, "n_audits": 0,
           "n_strategies_verified": 0, "violations": [],
           "note": "append-only 审计行的只读核对：逐级相邻 / evidence_ref 非空且"
                   "逐条可回溯（存在、非 benchmark、来源类型未被排除、实例 status"
                   " 合格、已复审、src_ok 严格 true）/ 签认与时刻与门版本齐备 / "
                   "策略行当前列取值不回退"}
    try:
        rows = con.execute(
            f"SELECT {', '.join(AUDIT_COLUMNS)} FROM {AUDIT_TABLE}"
            " ORDER BY rowid").fetchall()
        out["n_strategies_verified"] = con.execute(
            "SELECT COUNT(*) FROM expression_strategies_v2 WHERE status = ?",
            ("verified",)).fetchone()[0]
    except sqlite3.OperationalError as exc:      # 表不存在 = 零审计行，不是错误
        out["note"] += f"（本库尚无 {AUDIT_TABLE} 表：{type(exc).__name__}）"
        con.close()
        return out
    out["n_audits"] = len(rows)
    for r in rows:
        a = dict(zip(AUDIT_COLUMNS, r))
        v: list[str] = []
        if not str(a["gate_version"]).strip():
            v.append("missing_gate_version")
        if not str(a["reviewer"]).strip():
            v.append("missing_reviewer")
        if not str(a["scope_rule_version"]).strip():
            v.append("missing_scope_rule_version")
        if a["from_status"] not in LADDER or a["to_status"] not in LADDER:
            v.append("off_ladder_recorded")
        elif LADDER.index(a["to_status"]) - LADDER.index(a["from_status"]) != 1:
            v.append(f"non_adjacent_step:{a['from_status']}"
                     f"->{a['to_status']}")
        if a["scope_from"] in SCOPE_LADDER and a["scope_to"] in SCOPE_LADDER and \
                SCOPE_LADDER.index(a["scope_to"]) < \
                SCOPE_LADDER.index(a["scope_from"]):
            v.append(f"scope_regression:{a['scope_from']}->{a['scope_to']}")
        try:
            ids = json.loads(a["evidence_ref"])
        except (TypeError, ValueError):
            ids = None
        if not isinstance(ids, list) or not ids:
            v.append("empty_evidence_ref")
            ids = []
        if not isinstance(a["evidence_count"], int) or \
                a["evidence_count"] != len(ids):
            v.append("evidence_count_mismatch")
        try:
            datetime.datetime.fromisoformat(a["ts"])
        except (TypeError, ValueError):
            v.append("bad_ts")
        for iid in ids:
            e = con.execute(
                "SELECT si.status, si.reviewer_version, sg.role, sg.integrity,"
                " ws.id AS reg, ws.source_type FROM strategy_instances si"
                " LEFT JOIN segments sg ON sg.id = si.segment_id"
                " LEFT JOIN work_sources ws ON ws.work_id = si.work_id"
                " WHERE si.id = ?", (iid,)).fetchone()
            if e is None:
                v.append(f"evidence_missing:{iid}")
                continue
            ist, iver, role, integ, reg, stype = e
            if reg is None:
                v.append(f"evidence_no_registry:{iid}")
            if role == "benchmark":
                v.append(f"evidence_benchmark:{iid}")
            if stype in tuple(KQ.DEFAULT_EXCLUDED_SOURCE_TYPES):
                v.append(f"evidence_excluded_source_type:{iid}")
            if ist not in tuple(KQ.ELIGIBLE_INSTANCE_STATUS):
                v.append(f"evidence_instance_status:{iid}")
            if iver != KE.REVIEW_MARKER_NEW_DEF:
                v.append(f"evidence_unreviewed:{iid}")
            if not _src_ok_true(integ):
                v.append(f"evidence_src_ok_not_true:{iid}")
        row = con.execute(
            "SELECT status, observation_status FROM expression_strategies_v2"
            " WHERE id = ?", (a["strategy_id"],)).fetchone()
        if row is None:
            v.append(f"strategy_missing:{a['strategy_id']}")
        else:
            cur = level_of(row[0], row[1])
            if a["to_status"] in LADDER and cur in LADDER and \
                    LADDER.index(cur) < LADDER.index(a["to_status"]):
                v.append(f"state_regression:{a['strategy_id']}:{cur}"
                         f"<{a['to_status']}")
            if cur == a["to_status"] and (
                    row[0] != a["status_column_to"]
                    or row[1] != a["observation_to"]):
                v.append(f"column_mismatch:{a['strategy_id']}:"
                         f"({row[0]},{row[1]})!=({a['status_column_to']},"
                         f"{a['observation_to']})")
        if v:
            out["violations"].append({"audit_id": a["audit_id"],
                                      "strategy_key": a["strategy_key"],
                                      "kinds": v})
    out["ok"] = not out["violations"]
    con.close()
    return out


# ------------------------------------------------------------ 组装/CLI
def select_strategies(s, wanted: list[str]) -> list[ExpressionStrategyV2]:
    rows = (s.query(ExpressionStrategyV2)
            .order_by(ExpressionStrategyV2.strategy_key,
                      ExpressionStrategyV2.id).all())
    if not wanted:
        return rows
    keys = {k.strip() for k in wanted if k.strip()}
    picked = [r for r in rows if r.id in keys or r.strategy_key in keys]
    missed = keys - {r.id for r in picked} - {r.strategy_key for r in picked}
    if missed:
        raise KeyError(f"策略不存在：{','.join(sorted(missed))}")
    return picked


def build_report(db_path: Path, repo_root: Path, policy: dict,
                 wanted: list[str], requested_to: str | None,
                 reviewer: str, mode: str) -> dict:
    """判定阶段（永远只读）：逐条 `evaluate` → 报告。写侧在 `main` 里另行执行。"""
    s = open_ro_session(db_path)
    try:
        rows = select_strategies(s, wanted)
        verdicts = [evaluate(s, st, policy, requested_to, reviewer, i, len(rows))
                    for i, st in enumerate(rows, 1)]
    finally:
        s.close()
    n_promote = sum(1 for x in verdicts if x["decision"] == PROMOTE)
    return {
        "schema": SCHEMA, "tool": TOOL, "task": TASK, "generated_at": _now_iso(),
        "repo_root": GE.portable_path(repo_root, repo_root),
        "db_path": GE.portable_path(db_path, repo_root),
        "mode": mode,
        "db_mode": "ro" if mode == "dry-run" else "ro(判定)+rw(仅写侧事务)",
        "writable_connections_opened_during_judgement": 0,
        "policy": policy, "requested_to": requested_to, "reviewer": reviewer,
        "ladder": list(LADDER), "ladder_columns": dict(LADDER_COLUMNS),
        "scope_ladder": list(SCOPE_LADDER),
        "scope_rule_version": SCOPE_RULE_VERSION, "gate_version": GATE_VERSION,
        "min_reviewed": MIN_REVIEWED,
        "min_independent_roots": MIN_INDEPENDENT_ROOTS,
        "gate_order": list(GE.GATE_ORDER),
        "n_strategies": len(verdicts), "n_promote": n_promote,
        "n_no_promote": len(verdicts) - n_promote,
        "verdicts": [{k: x[k] for k in (
            "strategy_id", "strategy_key", "version", "level", "requested",
            "skipped", "note", "decision", "reason", "gates", "blocked_at",
            "evidence", "status", "observation_status", "scope", "line")}
            for x in verdicts],
        "discipline": {
            "db_writes_during_judgement": 0,
            "planned_writes": 0 if mode == "dry-run" else n_promote,
            "model_calls": 0, "git_writes": 0,
            "criteria_single_sourced_from":
                "scripts/k5_promotion_gate_explain.py + app/knowledge_query.py"
                "（门判词）/ source_check.parse_src_ok（src_ok）/"
                "knowledge_extract.REVIEW_MARKER_NEW_DEF（复审标记）",
            "local_rules_written": [
                "晋升级迁规则 LADDER/LADDER_COLUMNS + 相邻性判定（库里不存在、"
                "写侧固有）", f"scope 推导规则 {SCOPE_RULE_VERSION}"],
            "batch_promotion": "禁止：--commit 只接受恰好 1 条 --strategy"
                               "（W4 影响行数=1 / U2 批量升格任何读数下不解冻）",
            "audit_rows": "append-only：触发器阻断 UPDATE/DELETE + 本文件无该代码路径",
            "unfreeze_note": "本件让 S4 从「无执行器」变成「有执行器」；S4 判绿"
                             "另需 ≥1 条真写（K3 合格集 0→≥1），那要求 S1/S2 先"
                             "转绿且本执行器经独立验收（W7）。解冻前只准 --dry-run。"},
    }


def print_report(rep: dict) -> None:
    print(f"[{TOOL}] mode={rep['mode']} db={rep['db_path']} "
          f"db_mode={rep['db_mode']} gate_version={rep['gate_version']} "
          f"ladder={'->'.join(rep['ladder'])} "
          f"policy={KQ.canonical_json(rep['policy'])}")
    for v in rep["verdicts"]:
        print(v["line"])
    print(f"[{TOOL}] 判定 {rep['n_strategies']} 条："
          f"{PROMOTE}={rep['n_promote']} {NO_PROMOTE}={rep['n_no_promote']}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo-root", default=str(ROOT), dest="repo_root")
    ap.add_argument("--db", default="", help="库路径覆盖（缺省同解释器：本仓 "
                    "data/language_genome.db → 主仓候选绝对路径）")
    ap.add_argument("--book-id", default="", dest="book_id")
    ap.add_argument("--policy-json", default="", dest="policy_json")
    ap.add_argument("--strategy", action="append", default=[], dest="strategy",
                    help="策略 id 或 strategy_key（可重复）；--commit 只接受 1 条")
    ap.add_argument("--to", default=None, dest="to",
                    help=f"目标晋升级，取 {LADDER} 之一；缺省=下一级（跳级即拒）")
    ap.add_argument("--dry-run", action="store_true", dest="dry_run",
                    help="显式声明只判不写（缺省行为即 dry-run）")
    ap.add_argument("--commit", action="store_true",
                    help="真写：单事务、单行、单条策略；不带本项一律零写入")
    ap.add_argument("--reviewer", default="", help="--commit 必填的签认身份")
    ap.add_argument("--verify-audits", action="store_true", dest="verify_audits",
                    help="只做审计行只读校验，不判定不写")
    ap.add_argument("--json", action="store_true", dest="as_json",
                    help="额外打印完整 JSON 报告")
    ap.add_argument("--json-out", default="", dest="json_out",
                    help="JSON 报告落盘路径（\"\"＝不落盘；只写这一个文件，不碰库）")
    a = ap.parse_args(argv)
    repo_root = Path(a.repo_root)
    if a.dry_run and a.commit:
        print(f"[{TOOL}] --dry-run 与 --commit 互斥", file=sys.stderr)
        return EXIT_ERROR
    mode = "commit" if a.commit else "dry-run"
    db_path, _cands = GE.resolve_db(a.db, repo_root)
    if not Path(db_path).exists():
        print(f"[{TOOL}] 库不可读（路径不存在）：{db_path}", file=sys.stderr)
        return EXIT_ERROR
    if a.verify_audits:
        try:
            out = verify_promotion_audits(db_path)
        except sqlite3.Error as exc:
            print(f"[{TOOL}] 校验失败：{type(exc).__name__}: {exc}",
                  file=sys.stderr)
            return EXIT_ERROR
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return EXIT_OK if out["ok"] else EXIT_ERROR
    try:
        policy = GE.build_policy(a.book_id, a.policy_json)
        rep = build_report(db_path, repo_root, policy, a.strategy, a.to,
                           a.reviewer.strip() or DEFAULT_REVIEWER_DRY, mode)
    except KeyError as exc:
        print(f"[{TOOL}] 用法错误：{exc}", file=sys.stderr)
        return EXIT_ERROR
    except sqlite3.Error as exc:
        print(f"[{TOOL}] 库读取失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_ERROR
    print_report(rep)
    if a.as_json:
        print(json.dumps(rep, ensure_ascii=False, indent=1))
    if a.json_out:
        jp = Path(a.json_out)
        jp.parent.mkdir(parents=True, exist_ok=True)
        jp.write_text(json.dumps(rep, ensure_ascii=False, indent=1),
                      encoding="utf-8")
    rc = EXIT_REFUSED if rep["n_no_promote"] else EXIT_OK
    if mode == "dry-run":
        print(f"[{TOOL}] dry-run：判定连接 mode=ro、未打开可写连接 ⇒ 零写入"
              + (f"（{rep['n_no_promote']} 条 {NO_PROMOTE}，exit={EXIT_REFUSED}）"
                 if rep["n_no_promote"] else ""))
        return rc
    # ── 真写档：批量升格红线先拦（W4/U2）──────────────────────────
    if not a.reviewer.strip():
        print(f"[{TOOL}] 用法错误：--commit 必须带非空 --reviewer（无签认不写）",
              file=sys.stderr)
        return EXIT_ERROR
    if len(rep["verdicts"]) != 1:
        print(f"[{TOOL}] 批量升格禁止：--commit 只接受恰好 1 条 --strategy"
              f"（实得 {len(rep['verdicts'])} 条）", file=sys.stderr)
        return EXIT_ERROR
    v = rep["verdicts"][0]
    if v["decision"] != PROMOTE:
        print(f"[{TOOL}] {NO_PROMOTE}：拒绝写入（reason={v['reason']}）",
              file=sys.stderr)
        return EXIT_REFUSED
    return _execute_commit(db_path, rep, a.reviewer.strip())


def _execute_commit(db_path: Path, rep: dict, reviewer: str) -> int:
    """写侧执行：判定结果不改判（`evaluate` 是唯一判词产出处），只把签认身份
    填进审计字段并重渲染同一判词。"""
    s = open_ro_session(db_path)          # 重取一次 ORM 行对象（判定连接的行快照）
    try:
        st = s.query(ExpressionStrategyV2).filter_by(
            id=rep["verdicts"][0]["strategy_id"]).one()
        verdict = evaluate(s, st, rep["policy"], rep["requested_to"], reviewer)
    finally:
        s.close()
    if verdict["decision"] != PROMOTE:
        print(f"[{TOOL}] {NO_PROMOTE}：复检拒绝写入（reason={verdict['reason']}）",
              file=sys.stderr)
        return EXIT_REFUSED
    print(f"[{TOOL}] 复检判词（与 dry-run 同一函数产出）：{verdict['line']}")
    try:
        res = commit_promotion(Path(db_path), verdict)
    except (PromotionGuardError, sqlite3.Error) as exc:
        print(f"[{TOOL}] 写入失败已回滚：{type(exc).__name__}: {exc}"
              f" —— 两张表均无变化", file=sys.stderr)
        return EXIT_ERROR
    print(f"[{TOOL}] COMMITTED {res['audit_id']} "
          f"{res['from_status']}->{res['to_status']} "
          f"rows_changed={res['rows_changed']} audit_rows={res['audit_rows']}")
    integ = verify_promotion_audits(Path(db_path))
    print(f"[{TOOL}] 审计行只读校验：n_audits={integ['n_audits']} ok={integ['ok']}")
    return EXIT_OK if integ["ok"] else EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
