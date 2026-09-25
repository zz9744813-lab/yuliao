"""K2→K3 最小闭环「决策就绪」**只读**仿真（审计 P0-1 拍板用，2026-09-25）。

## 它算什么

审计 P0-1 的真实卡点是 K3 的三层闸：层1 `status`（`eligible_statuses` 只认
verified）→ 层2 证据（`_evidence_for`，基准段实例硬剔）→ 层3 `scope`
（`_scope_matches`）。8 条 legacy 策略当前**层1 就开火**，所以「可进 K3 的
生产证据恒 0」。

对 8 条 legacy 的 `status`/`scope` 语义裁定是集霸（朱十一）的权限，不是主控
的，也不是本脚本的。本脚本只把「**若拍板为 X，则收益为 Y**」算成可对账的
计数——零写库、零模型调用、不改任何真值。

| 场景 | 内存里改什么 | 期望 | 证明的事 |
|---|---|---|---|
| S0 | 什么都不改（现状） | `selected=0` | 与 K3 真身实测逐策略对齐（自校验门） |
| S1 | 只开层1：`status=verified` | `selected=0` | 层3 是真闸，单升 status 无效 |
| S2 | 只开层3：`scope=WORK`+`scope_ids` | `selected=0` | 层1 是真闸，单授 scope 无效 |
| S3 | 层1+层3 同开（含非空 `scope_basis`） | 逐策略给实例数/区间数 | 拍板后的真实收益上限 |
| S3_no_basis | 同 S3 但 `scope_basis` 为空 | 形式闸 `grant_scope_valid` 拒 | 依据缺失不得授予（§4.3） |
| S4_global | 同 S3 但 `scope=GLOBAL` | `excluded_scope_global_reserved` | GLOBAL 本阶段只预留不授予 |

## 只读纪律（三重；任一失效仍写不到库）

1. **连接层**：DB-API 一律 SQLite URI `mode=ro`（`file:<abs>?mode=ro` +
   `uri=True`）；开跑前断言该串含 `mode=ro`，不含即拒跑。
2. **会话层**：每条新连接回读 `PRAGMA query_only` 必须为 1；Session 用
   `ReadOnlySession`，其写入口（增/删/改/落盘/提交）被替换为抛
   `ReadOnlyViolation`；`autoflush=False` 保证「内存里改属性」不产生任何
   写 SQL。场景收尾按快照逐字段还原 + `expire_all()`，模拟值永不落盘。
3. **源码层**：`write_path_scan()` 扫本文件源码，出现任何 ORM/DB-API 写调用
   点即拒跑（回归测试钉死，并用合成可写片段反向钉扫描器不是死门）。

判据**一律 import 复用** `app.knowledge_query`（`eligible_statuses` /
`ELIGIBLE_OBSERVATION` / `ELIGIBLE_INSTANCE_STATUS` / `_evidence_for` /
`_scope_matches` / `query_knowledge`）与 `app.knowledge.grant_scope_valid`。
本脚本不重写任何一层判定：场景的权威结论来自 `query_knowledge` 真身整管道，
逐层探针只用于「哪一层在开火」的分桶报告。

## 用法

```
F:/Hermes/hermes-agent/venv/Scripts/python.exe scripts/k2_unlock_sim.py \
    --db F:/agi/language-genome/data/language_genome.db \
    --books WK-6e5d2623,WK-dc90993434e9,WK-dc90934e34e9 \
    --out F:/agi/_scratch/k2_unlock_sim.json
```

- 目标库绝对路径与只读证据打印在 stderr；stdout 只出 JSON（可对账）。
- 退出码：0=跑通且 S0 自校验通过；3=S0 与真身口径不符（必须查因，不许调
  参数凑数）；2=库路径/只读纪律不过（`SimError` 由 argparse 之外抛出）。
- `--books` 支持**前缀解析**：真库 `works.id` 是 `WK-`+12 位十六进制
  （见 app/ids.py），文档里手抄的短 id（`WK-6e5d2623`）与近似拼法
  （`WK-dc90934e34e9` / `WK-dc90993434e9`）由库来判，不靠记忆。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine, event, func
from sqlalchemy.orm import Session, sessionmaker

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from app import knowledge as K                                     # noqa: E402
from app import knowledge_query as KQ                              # noqa: E402
from app.models import (ExpressionStrategyV2, Segment,             # noqa: E402
                        StrategyCondition, StrategyInstance, Work,
                        WorkSource)

# 授予范围拍板必须带的「依据」文本占位——S2/S3 用它把「形式闸过」与「层3
# 语义授予」分开：真值由集霸给定，本脚本只验形式，绝不代改真值。
SIM_SCOPE_BASIS = ("仿真占位依据：仅用于计算「若拍板授予该范围」的收益上限，"
                   "非集霸裁定，不构成任何真实授予")

# 主控/审计侧点名的候选世界（含两种拼法，交给解析器对账真实 id）
DEFAULT_BOOKS = ("WK-6e5d2623", "WK-dc90993434e9", "WK-dc90934e34e9")
# 审计口径（用作自校验对账，不作为判定输入）
AUDIT_EXPECTATION = {"benchmark_instances_stripped": 82,
                     "instances_total": 83,
                     "strategies_total": 8}

# 场景表：overrides 在内存里 setattr 到策略行，`<BOOK>` 由当前 book_id 代入；
# 未列出的字段保持真库原值（＝该层不开）。
SCENARIOS: tuple[tuple[str, str, dict], ...] = (
    ("S0", "现状：零改动", {}),
    ("S1", "只开层1：status=verified，scope 保持原值",
     {"status": "verified"}),
    ("S2", "只开层3：scope=WORK+scope_ids=[book]+非空依据，status 保持原值",
     {"scope": "WORK", "scope_ids": ["<BOOK>"], "scope_basis": SIM_SCOPE_BASIS}),
    ("S3", "层1+层3 同开：verified + WORK + scope_ids=[book] + 非空依据",
     {"status": "verified", "scope": "WORK", "scope_ids": ["<BOOK>"],
      "scope_basis": SIM_SCOPE_BASIS}),
    ("S3_no_basis", "S3 但缺 scope_basis（形式闸须拒；K3 查询层不查依据）",
     {"status": "verified", "scope": "WORK", "scope_ids": ["<BOOK>"],
      "scope_basis": ""}),
    ("S4_global", "层1+scope=GLOBAL（本阶段只预留，不自动授予）",
     {"status": "verified", "scope": "GLOBAL", "scope_ids": ["<BOOK>"],
      "scope_basis": SIM_SCOPE_BASIS}),
)

# 被模拟的字段＝三层闸的全部入口（快照/还原清单）
SIMULATED_FIELDS = ("status", "scope", "scope_ids", "scope_basis")

# ── 只读纪律：源码级扫描 ────────────────────────────────────────────────
# 方法名单独列成字符串元组：源码里不出现 `.commit(` 这类字面量，扫描器
# 才不会自匹配自己的模式串。
_ORM_WRITE_METHODS = ("commit", "flush", "add", "add_all", "merge", "delete",
                      "expunge", "execute_ddl")
_WRITE_METHOD_RES = tuple(
    re.compile(r"\.\s*" + re.escape(m) + r"\s*\(") for m in _ORM_WRITE_METHODS)
# DML 只有落在执行点上才算写路径（`PRAGMA query_only` 之类不算）
_DML_EXEC_RE = re.compile(
    r"(exec_\w+|execute|executescript)\s*\([^)\n]{0,120}?"
    r"\b(insert|update|delete|drop|alter|truncate|vacuum|create\s+table)\b",
    re.IGNORECASE)
_WRITABLE_URI_RE = re.compile(r"mode=(rw|rwlwc|readwrite)\b")


def write_path_scan(source: str) -> list[str]:
    """返回源码里所有「写库调用点」命中行；[] = 无写路径。"""
    hits: list[str] = []
    for no, line in enumerate(source.splitlines(), 1):
        code = line.split("#", 1)[0]
        for rx in _WRITE_METHOD_RES:
            if rx.search(code):
                hits.append(f"{no}:orm_write:{code.strip()}")
                break
        if _DML_EXEC_RE.search(code):
            hits.append(f"{no}:dml:{code.strip()}")
        if _WRITABLE_URI_RE.search(code):
            hits.append(f"{no}:writable_uri:{code.strip()}")
    return hits


class SimError(RuntimeError):
    """仿真前置条件不成立（库不可读 / 只读纪律不过 / K3 报 unavailable）。"""


class ReadOnlyViolation(SimError):
    """只读会话被调用到写入口。"""


class ReadOnlySession(Session):
    """写入口全部抛错——模拟只许发生在内存里。"""

    def _refuse(self, what: str):
        raise ReadOnlyViolation(
            f"k2_unlock_sim 只读：拒绝 {what}()（场景模拟只在内存里做）")

    def add(self, *a, **k):
        self._refuse("add")

    def add_all(self, *a, **k):
        self._refuse("add_all")

    def delete(self, *a, **k):
        self._refuse("delete")

    def flush(self, *a, **k):
        self._refuse("flush")

    def commit(self, *a, **k):
        self._refuse("commit")

    def merge(self, *a, **k):
        self._refuse("merge")


# ── 只读连接 ────────────────────────────────────────────────────────────
def readonly_uri(path: str | Path) -> str:
    """SQLite URI 只读连接串（Windows 盘符用正斜杠，与本仓既有脚本同口径）。"""
    return f"file:{Path(path).resolve().as_posix()}?mode=ro"


def build_readonly_engine(db_path: str | Path):
    """开只读引擎并自证只读；任一不过 → SimError（拒绝在可写连接下运行）。"""
    p = Path(db_path).resolve()
    if not p.is_file():
        raise SimError(f"目标库不存在或不是文件：{p}")
    uri = readonly_uri(p)
    if "mode=ro" not in uri:
        raise SimError(f"连接串不含 mode=ro，拒绝运行：{uri}")
    print(f"[k2_unlock_sim] 目标库（只读）= {p}", file=sys.stderr, flush=True)
    print(f"[k2_unlock_sim] DB-API URI   = {uri}", file=sys.stderr, flush=True)

    def _connect():
        con = sqlite3.connect(uri, uri=True, check_same_thread=False,
                              timeout=30)
        con.execute("PRAGMA query_only=ON")
        return con

    # creator= 让 SQLAlchemy 只走上面这个 sqlite3.connect()：连接串的
    # mode=ro 由本函数担保，不依赖方言对 URI query 的解析口径。
    engine = create_engine("sqlite://", creator=_connect)

    @event.listens_for(engine, "connect")
    def _assert_query_only(dbapi_conn, _record):
        got = int(dbapi_conn.execute("PRAGMA query_only").fetchone()[0] or 0)
        if got != 1:
            raise SimError(f"PRAGMA query_only={got} != 1：非只读连接，拒跑")

    with engine.connect() as c:
        qo = int(c.exec_driver_sql("PRAGMA query_only").scalar() or 0)
        if qo != 1:
            raise SimError(f"PRAGMA query_only={qo} != 1：只读自证失败")
        journal = c.exec_driver_sql("PRAGMA journal_mode").scalar()
        n_tables = int(c.exec_driver_sql(
            "SELECT count(*) FROM sqlite_master WHERE type='table'").scalar() or 0)
    return engine, {"resolved_path": str(p), "size_bytes": p.stat().st_size,
                    "dbapi_uri": uri, "mode_ro": True, "query_only": qo,
                    "journal_mode": journal, "n_tables": n_tables}


def session_for(engine) -> ReadOnlySession:
    """只读会话：autoflush=False ⇒ 改属性不会发出任何写 SQL。"""
    return sessionmaker(bind=engine, class_=ReadOnlySession, autoflush=False,
                        expire_on_commit=False)()


# ── 台账（只读投影，不是判定）───────────────────────────────────────────
def all_strategies(s) -> list[ExpressionStrategyV2]:
    return (s.query(ExpressionStrategyV2)
            .order_by(ExpressionStrategyV2.strategy_key,
                      ExpressionStrategyV2.version).all())


def resolve_books(s, raw_ids) -> list[dict]:
    """把文档里手抄的 book id 解析成真库全量 id，并把差异如实报出。"""
    works = {wid for (wid,) in s.query(Work.id).all()}
    universe = set(works)
    universe |= {wid for (wid,) in s.query(WorkSource.work_id).all()}
    universe |= {wid for (wid,) in s.query(StrategyInstance.work_id).all()}
    out: list[dict] = []
    for raw in raw_ids:
        raw = (raw or "").strip()
        if not raw:
            continue
        exact = raw in universe
        prefix_hits = sorted(w for w in universe if w.startswith(raw))
        shorter = sorted(w for w in universe
                         if w != raw and raw.startswith(w) and len(w) >= 6)
        out.append({
            "requested": raw,
            "requested_len": len(raw),
            "exact_match": exact,
            "exists_in_works": raw in works,
            "prefix_matches": prefix_hits[:10],
            "stored_id_is_prefix_of_request": shorter[:10],
            # 真库 id 口径（app/ids.py）：WK- + 12 hex = 15 字符
            "looks_truncated_vs_schema": len(raw) < 15,
            "resolved_ids": [raw] if exact else prefix_hits[:10],
            "unresolved": not exact and not prefix_hits and not shorter})
    return out


def _bump(d: dict, key, n: int = 1) -> None:
    d[key] = d.get(key, 0) + n


def strategy_census(s, strategies) -> dict:
    """逐策略台账：三层闸入口字段 + 实例的 work_id 分布与 role 分布。"""
    inst_rows = s.query(StrategyInstance).all()
    seg_ids = {i.segment_id for i in inst_rows}
    role_map: dict[str, object] = {}
    if seg_ids:
        role_map = {sid: role for sid, role in
                    s.query(Segment.id, Segment.role)
                    .filter(Segment.id.in_(seg_ids)).all()}
    reg_ids = {wid for (wid,) in s.query(WorkSource.work_id).all()}
    cond_counts: dict[str, int] = {}
    for sid, n in (s.query(StrategyCondition.strategy_id, func.count())
                   .group_by(StrategyCondition.strategy_id).all()):
        cond_counts[str(sid)] = int(n)

    by_strategy: dict[str, dict] = {}
    role_total: dict[str, int] = {}
    status_total: dict[str, int] = {}
    for ins in inst_rows:
        d = by_strategy.setdefault(str(ins.strategy_id), _empty_inst_roll())
        d["instances"] += 1
        _bump(d["work_ids"], str(ins.work_id))
        role_key = ("<segment_missing>" if ins.segment_id not in role_map
                    else (role_map.get(ins.segment_id) or "role_null"))
        _bump(d["roles"], role_key)
        _bump(role_total, role_key)
        _bump(d["text_versions"], str(ins.text_version))
        _bump(d["instance_status"], str(ins.status))
        _bump(status_total, str(ins.status))
        _bump(d["registered_works" if ins.work_id in reg_ids
                else "unregistered_works"], str(ins.work_id))

    rows = []
    for st in strategies:
        d = by_strategy.get(str(st.id), _empty_inst_roll())
        basis = st.scope_basis or ""
        rows.append({
            "strategy_id": st.id, "strategy_key": st.strategy_key,
            "version": st.version, "status": st.status, "scope": st.scope,
            "scope_ids": list(st.scope_ids or []),
            "scope_basis_len": len(basis.strip()),
            "scope_basis_present": bool(basis.strip()),
            "observation_status": st.observation_status,
            "effect_status": st.effect_status,
            "legacy_strategy_id": st.legacy_strategy_id,
            "layer1_status_eligible": st.status in KQ.eligible_statuses(st.version),
            "layer1_observation_eligible": st.observation_status in KQ.ELIGIBLE_OBSERVATION,
            "formal_scope_valid": K.grant_scope_valid(
                st.scope, list(st.scope_ids or []), basis),
            "condition_rows": cond_counts.get(str(st.id), 0),
            **d})
    return {"n_strategies": len(strategies),
            "n_instances": len(inst_rows),
            "instances_by_status": status_total,
            "instances_by_segment_role": role_total,
            "n_condition_rows": _scalar(s, func.count(), StrategyCondition),
            "n_knowledge_packages": _count_packages(s),
            "audit_expectation": AUDIT_EXPECTATION,
            "per_strategy": rows}


def _empty_inst_roll() -> dict:
    return {"instances": 0, "work_ids": {}, "roles": {}, "text_versions": {},
            "instance_status": {}, "registered_works": {},
            "unregistered_works": {}}


def _scalar(s, fn, model) -> int:
    return int(s.query(fn).select_from(model).scalar() or 0)


def _count_packages(s) -> int:
    from app.models import KnowledgePackage
    return _scalar(s, func.count(), KnowledgePackage)


def worlds_census(s, book_ids) -> dict:
    """候选世界的登记与段落分布（K1-A 契约字段，逐本可对账）。"""
    out: dict[str, dict] = {}
    for book in book_ids:
        work = s.query(Work).filter_by(id=book).first()
        reg = s.query(WorkSource).filter_by(work_id=book).first()
        roles: dict[str, int] = {}
        for role, n in (s.query(Segment.role, func.count())
                        .filter(Segment.work_id == book)
                        .group_by(Segment.role).all()):
            _bump(roles, role or "role_null", int(n))
        inst_by_strategy: dict[str, int] = {}
        for sid, n in (s.query(StrategyInstance.strategy_id, func.count())
                       .filter(StrategyInstance.work_id == book)
                       .group_by(StrategyInstance.strategy_id).all()):
            inst_by_strategy[str(sid)] = int(n)
        out[book] = {
            "in_works": work is not None,
            "work_title": work.title if work else None,
            "work_author": work.author if work else None,
            "registered": reg is not None,
            "canonical_work_id": reg.canonical_work_id if reg else None,
            "source_type": reg.source_type if reg else None,
            "registry_text_version": reg.text_version if reg else None,
            "identity_purposes": list(reg.identity_purposes or []) if reg else None,
            "license_purposes": list(reg.license_purposes or []) if reg else None,
            "allowed_purposes": list(reg.allowed_purposes or []) if reg else None,
            "metadata_status": reg.metadata_status if reg else None,
            "segments_by_role": roles,
            "segments_total": sum(roles.values()),
            "instances_by_strategy": inst_by_strategy}
    return out


# ── 层2 证据探针（真判据）───────────────────────────────────────────────
def evidence_probes(s, strategies, policy) -> dict:
    """`_evidence_for` 逐策略：可用实例数 / 唯一(根作品,span) 区间数 / 剔除理由。

    该探针只读实例·段·登记表，与 status/scope 模拟无关（**场景不变量**），
    故全局算一次；报告里以 evidence_probe_invariant 注明。"""
    probes: dict[str, dict] = {}
    for st in strategies:
        refs, n_intervals, stripped = KQ._evidence_for(s, st.id, policy)
        buckets: dict[str, int] = {}
        for tag in stripped:
            _bump(buckets, tag.split(":", 1)[1] if ":" in tag else tag)
        spans = sorted({tuple(r["span"]) for r in refs})
        probes[str(st.id)] = {
            "instances_seen_by_gate": len(refs) + len(stripped),
            "eligible_instances": len(refs),
            "unique_root_span_intervals": n_intervals,
            "root_works": sorted({r["canonical_work"] for r in refs}),
            "spans": [list(x) for x in spans],
            "stripped": stripped,
            "stripped_buckets": buckets}
    return probes


def _bucket(stripped, suffix: str) -> int:
    return sum(1 for tag in stripped if tag.endswith(":" + suffix))


# ── 场景套用与还原（只在内存里）─────────────────────────────────────────
def snapshot(strategies) -> dict:
    return {str(st.id): {f: getattr(st, f) for f in SIMULATED_FIELDS}
            for st in strategies}


def restore(s, strategies, snap) -> None:
    """逐字段还原 + expire_all()：模拟值不离开内存，也不残留下一个场景。"""
    for st in strategies:
        orig = snap.get(str(st.id)) or {}
        for f in SIMULATED_FIELDS:
            if f in orig:
                value = orig[f]
                setattr(st, f, list(value) if isinstance(value, list) else value)
    s.expire_all()


def apply_scenario(s, strategies, spec, book) -> None:
    for st in strategies:
        for field, value in spec.items():
            if isinstance(value, list):
                value = [str(v).replace("<BOOK>", book) for v in value]
            elif isinstance(value, str):
                value = value.replace("<BOOK>", book)
            setattr(st, field, value)


def scenario_run(s, strategies, scen_id, spec, book, policy, evidence,
                 snap) -> dict:
    """套用场景 → 跑 K3 真身 `query_knowledge` → 逐策略三层分桶 → 还原。"""
    apply_scenario(s, strategies, spec, book)
    try:
        resp = KQ.query_knowledge(policy, s)
        if resp.get("status") == "unavailable":
            raise SimError(f"K3 真身报 unavailable（口径不成立，必须查因）："
                           f"{resp.get('reason')}")
        selected_ids = [e.get("strategy_id") for e in resp.get("selected") or []]
        rejected_by_key = {str(r.get("strategy_key")): str(r.get("reason"))
                           for r in (resp.get("rejected") or [])}
        per_strategy: list[dict] = []
        buckets: dict[str, int] = {}
        for st in strategies:
            if st.id in selected_ids:
                verdict = "selected"
            elif st.strategy_key in rejected_by_key:
                verdict = rejected_by_key[st.strategy_key]
            elif st.status not in KQ.eligible_statuses(st.version):
                # 真身预筛（未进管道）；桶名与 scripts/k2_extract_backfill.py
                # 的 status_not_eligible 对齐，判据取真常量，不自立口径
                verdict = "status_not_eligible"
            elif st.observation_status not in KQ.ELIGIBLE_OBSERVATION:
                verdict = "observation_not_eligible"
            else:
                verdict = "not_considered"
            ev = evidence[str(st.id)]
            basis = st.scope_basis or ""
            _bump(buckets, verdict)
            per_strategy.append({
                "strategy_id": st.id, "strategy_key": st.strategy_key,
                "version": st.version,
                "simulated_status": st.status, "simulated_scope": st.scope,
                "simulated_scope_ids": list(st.scope_ids or []),
                "simulated_scope_basis_len": len(basis.strip()),
                "layer1_status_eligible": st.status in KQ.eligible_statuses(st.version),
                "layer1_observation_eligible": st.observation_status in KQ.ELIGIBLE_OBSERVATION,
                "layer2_eligible_instances": ev["eligible_instances"],
                "layer2_unique_intervals": ev["unique_root_span_intervals"],
                "layer2_root_works": ev["root_works"],
                "layer2_benchmark_stripped": _bucket(ev["stripped"],
                                                      "benchmark_source"),
                "layer2_no_registry_stripped": _bucket(ev["stripped"],
                                                        "no_registry"),
                "layer3_scope_verdict": KQ._scope_matches(s, st, policy),
                "formal_scope_valid": K.grant_scope_valid(
                    st.scope, list(st.scope_ids or []), basis),
                "pipeline_verdict": verdict,
                "in_package": st.id in selected_ids})
        return {"scenario": scen_id, "book_id": book, "policy": policy,
                "k3_status": resp.get("status"),
                "n_selected": len(selected_ids),
                "selected_strategy_ids": selected_ids,
                "selected_strategy_keys": [
                    e.get("strategy_key") for e in resp.get("selected") or []],
                "budget": resp.get("budget"),
                "contract_negotiation": resp.get("contract_negotiation"),
                "snapshot_fingerprint": resp.get("snapshot_fingerprint"),
                "package_sha256": resp.get("package_sha256"),
                "pipeline_rejected": resp.get("rejected"),
                "pipeline_verdict_buckets": buckets,
                "nonempty_package": bool(selected_ids),
                "layer1_open_per_book": sum(
                    1 for e in per_strategy if e["layer1_status_eligible"]
                    and e["layer1_observation_eligible"]),
                "layer3_open_per_book": sum(
                    1 for e in per_strategy if e["layer3_scope_verdict"] == "pass"),
                "per_strategy": per_strategy}
    finally:
        restore(s, strategies, snap)


def discover_nonbenchmark_books(s) -> list[str]:
    """从真库自己发现「有合格实例落在非 benchmark 段上」的作品，不靠文档记忆。"""
    inst_rows = s.query(StrategyInstance).all()
    seg_ids = {i.segment_id for i in inst_rows}
    role_map: dict[str, object] = {}
    if seg_ids:
        role_map = {sid: role for sid, role in
                    s.query(Segment.id, Segment.role)
                    .filter(Segment.id.in_(seg_ids)).all()}
    found: list[str] = []
    for ins in inst_rows:
        if str(ins.status) not in KQ.ELIGIBLE_INSTANCE_STATUS:
            continue
        if ins.segment_id not in role_map:
            continue                      # 段缺失：来源不可核对，不算证据
        if role_map.get(ins.segment_id) == "benchmark":
            continue                      # 基准段被 K3 硬剔
        if str(ins.work_id) not in found:
            found.append(str(ins.work_id))
    return found


# ── 主入口 ──────────────────────────────────────────────────────────────
def sim_policy(book_id: str) -> dict:
    """与 K4 A 臂同形，但语义需求取最保守空集、预算不放宽（不放宽任何门槛）。"""
    return {"contract_version": K.PACKAGE_CONTRACT_VERSION,
            "book_id": book_id, "semantic_requirements": {},
            "limits": {"candidate_cap": KQ.CANDIDATE_CAP_MAX,
                       "context_items": 0}}


def simulate(db_path, books=None, *, out_path=None) -> dict:
    src = Path(__file__).resolve().read_text(encoding="utf-8")
    hits = write_path_scan(src)
    if hits:
        raise SimError("只读纪律：本脚本源码存在写路径，拒绝运行：\n  "
                       + "\n  ".join(hits))
    engine, conn_info = build_readonly_engine(db_path)
    s = session_for(engine)
    try:
        strategies = all_strategies(s)
        if not strategies:
            raise SimError("expression_strategies_v2 为空，无候选可仿真")
        snap = snapshot(strategies)

        resolved = resolve_books(s, books or DEFAULT_BOOKS)
        discovered = discover_nonbenchmark_books(s)
        books_final: list[str] = []
        for item in resolved:
            for b in item["resolved_ids"]:
                if b not in books_final:
                    books_final.append(b)
        for b in discovered:
            if b not in books_final:
                books_final.append(b)
        if not books_final:
            raise SimError("候选世界解析后为空——用 --books 指定真库 work_id")

        policies = {b: sim_policy(b) for b in books_final}
        evidence = evidence_probes(s, strategies, policies[books_final[0]])
        bench_stripped = sum(_bucket(v["stripped"], "benchmark_source")
                             for v in evidence.values())
        no_registry = sum(_bucket(v["stripped"], "no_registry")
                          for v in evidence.values())

        scenarios: dict[str, dict] = {}
        for scen_id, desc, spec in SCENARIOS:
            per_book = {b: scenario_run(s, strategies, scen_id, spec, b,
                                        policies[b], evidence, snap)
                        for b in books_final}
            scenarios[scen_id] = {
                "desc": desc,
                "opened_layers": list(spec.keys()) or ["(零改动)"],
                "nonempty_packages": sum(1 for v in per_book.values()
                                          if v["nonempty_package"]),
                "selected_by_book": {b: v["n_selected"]
                                      for b, v in per_book.items()},
                "max_selected_any_book": max(v["n_selected"]
                                              for v in per_book.values()),
                "layer1_open_by_book": {b: v["layer1_open_per_book"]
                                         for b, v in per_book.items()},
                "layer3_open_by_book": {b: v["layer3_open_per_book"]
                                         for b, v in per_book.items()},
                "verdict_buckets_pooled": _merge(
                    v["pipeline_verdict_buckets"] for v in per_book.values()),
                "per_book": per_book}

        deviations = []
        for b, run in scenarios["S0"]["per_book"].items():
            if run["n_selected"] != 0:
                deviations.append({"book_id": b, "n_selected": run["n_selected"],
                                   "selected": run["selected_strategy_keys"]})
            deviations += [{"book_id": b, "strategy_key": e["strategy_key"],
                            "pipeline_verdict": e["pipeline_verdict"]}
                           for e in run["per_strategy"] if e["in_package"]]

        out = {
            "tool": "scripts/k2_unlock_sim.py",
            "schema": "k2-unlock-sim/1",
            "generated_at": datetime.now(timezone.utc).isoformat(
                timespec="seconds"),
            "readonly": {"checks": ["DB-API URI 含 mode=ro",
                                     "每条连接 PRAGMA query_only 回读==1",
                                     "ReadOnlySession 写入口抛错",
                                     "autoflush=False",
                                     "场景收尾快照还原 + expire_all()",
                                     "write_path_scan() 零命中"],
                          "scan_hits": hits,
                          "evidence_probe_invariant": True, **conn_info},
            "sim_policy": {"note": ("semantic_requirements={} ⇒ 任何 required "
                                     "条件落 unknown（真身口径，不放宽）；"
                                     "candidate_cap="
                                     f"{KQ.CANDIDATE_CAP_MAX}、context_items=0、"
                                     f"contract_version={K.PACKAGE_CONTRACT_VERSION}"),
                            "scope_basis_placeholder": SIM_SCOPE_BASIS,
                            "per_book": policies},
            "census": {"strategies": strategy_census(s, strategies),
                        "worlds_requested": resolved,
                        "worlds": worlds_census(s, books_final),
                        "benchmark_instances_stripped_by_k3": bench_stripped,
                        "instances_stripped_no_registry": no_registry,
                        "audit_expectation": AUDIT_EXPECTATION,
                        "discovered_nonbenchmark_books": discovered},
            "books_evaluated": books_final,
            "scenarios": scenarios,
            "selfcheck_s0_matches_k3_truth": {
                "expected": "逐策略 selected=0（主控实测 8/8 selected=0）",
                "passed": not deviations, "deviations": deviations},
            "unlock_summary": unlock_summary(scenarios, evidence)}
    finally:
        s.close()
        engine.dispose()
    if out_path:
        Path(out_path).write_text(json.dumps(out, ensure_ascii=False, indent=1),
                                  encoding="utf-8")
        print(f"[k2_unlock_sim] JSON 已写 {Path(out_path).resolve()}",
              file=sys.stderr, flush=True)
    return out


def _merge(runs):
    merged: dict[str, int] = {}
    for r in runs:
        for k, v in r.items():
            _bump(merged, k, v)
    return merged


def unlock_summary(scenarios, evidence) -> dict:
    """拍板前后对比：出包张数 / 覆盖策略 / 仍然拿不到的东西。"""
    per_book_covered: dict[str, dict] = {}
    s3 = scenarios["S3"]["per_book"]
    for b, run in s3.items():
        per_book_covered[b] = {
            "selected_keys": [e["strategy_key"] for e in run["per_strategy"]
                              if e["in_package"]],
            "excluded_no_evidence": [
                e["strategy_key"] for e in run["per_strategy"]
                if e["pipeline_verdict"] == "excluded_no_evidence"],
            "excluded_scope": [
                {"strategy_key": e["strategy_key"],
                 "reason": e["pipeline_verdict"]}
                for e in run["per_strategy"]
                if str(e["pipeline_verdict"]).startswith("excluded_scope")],
            "other": [
                {"strategy_key": e["strategy_key"],
                 "reason": e["pipeline_verdict"]}
                for e in run["per_strategy"]
                if e["pipeline_verdict"] != "selected"
                and e["pipeline_verdict"] != "excluded_no_evidence"
                and not str(e["pipeline_verdict"]).startswith("excluded_scope")],
            "package_sha256": run["package_sha256"],
            "k3_status": run["k3_status"]}
    return {
        "packages_nonempty_by_scenario": {
            sid: scenarios[sid]["nonempty_packages"]
            for sid, *_ in SCENARIOS},
        "selected_by_book_by_scenario": {
            sid: scenarios[sid]["selected_by_book"] for sid, *_ in SCENARIOS},
        "s3_by_book": per_book_covered,
        "evidence_available_by_strategy": {
            k: {"eligible_instances": v["eligible_instances"],
                 "unique_root_span_intervals": v["unique_root_span_intervals"],
                 "root_works": v["root_works"]}
            for k, v in evidence.items()},
        "caveats": [
            "「出包」= query_knowledge 返回 matched 且 selected 非空；落库成 "
            "knowledge_packages 行须另走 K3-B 冻结流程（本仿真零写库）",
            "S3 只让**有非基准合格证据**的策略出包，其余仍 excluded_no_evidence",
            "基准段实例被 _evidence_for 硬剔，任何场景都不因升格而回流",
            "S3_no_basis 若仍在查询层命中，说明依据校验只存在于授予侧形式闸"
            "（grant_scope_valid），K3 查询层不查 scope_basis——拍板时必须写进依据",
            "本表是收益上限估算，不构成「已解锁」；status/scope 真值零改动"]}


def render_report(out: dict) -> str:
    """人读摘要（stderr）；机器对账看 stdout/--out 的 JSON。"""
    lines = [f"# K2→K3 解锁仿真（只读） {out['generated_at']}",
             f"库={out['readonly']['resolved_path']} "
             f"query_only={out['readonly']['query_only']} "
             f"mode_ro={out['readonly']['mode_ro']}",
             f"候选世界={out['books_evaluated']}",
             f"S0 自校验（逐策略 selected=0）："
             f"{'通过' if out['selfcheck_s0_matches_k3_truth']['passed'] else '不通过'}"]
    cen = out["census"]["strategies"]
    lines.append(f"策略 {cen['n_strategies']} 条 / 实例 {cen['n_instances']} 条 "
                 f"/ 条件 {cen['n_condition_rows']} 行 / "
                 f"包 {cen['n_knowledge_packages']} 行 / "
                 f"基准段硬剔 "
                 f"{out['census']['benchmark_instances_stripped_by_k3']} 条")
    for sid, sc in out["scenarios"].items():
        lines.append(f"\n## {sid} — {sc['desc']}")
        lines.append(f"  非空包 {sc['nonempty_packages']}/{len(sc['per_book'])} "
                     f"本；桶 {sc['verdict_buckets_pooled']}")
        for b, run in sc["per_book"].items():
            lines.append(f"  [{b}] selected={run['n_selected']} "
                         f"status={run['k3_status']} "
                         f"层1开={run['layer1_open_per_book']} "
                         f"层3开={run['layer3_open_per_book']}")
            for e in run["per_strategy"]:
                lines.append(
                    f"     - {e['strategy_key'][:28]:28s} "
                    f"verdict={e['pipeline_verdict']:34s} "
                    f"L1={int(e['layer1_status_eligible'])} "
                    f"L2inst={e['layer2_eligible_instances']} "
                    f"L2iv={e['layer2_unique_intervals']} "
                    f"L3={e['layer3_scope_verdict']} "
                    f"basis={e['simulated_scope_basis_len']} "
                    f"形式闸={int(e['formal_scope_valid'])}")
    return "\n".join(lines)


def default_db_path() -> str:
    env_db = (os.environ.get("LG_SIM_DB") or "").strip()
    if env_db:
        return env_db
    url = (os.environ.get("LG_DATABASE_URL") or "").strip()
    if url.startswith("sqlite:///"):
        return url[len("sqlite:///"):]
    return str(ROOT / "data" / "language_genome.db")


def _compact(out: dict) -> dict:
    keep = ("tool", "schema", "generated_at", "readonly", "sim_policy",
            "census", "books_evaluated", "selfcheck_s0_matches_k3_truth",
            "unlock_summary")
    payload = {k: out[k] for k in keep if k in out}
    payload["scenario_headline"] = {
        sid: {"nonempty_packages": sc["nonempty_packages"],
               "selected_by_book": sc["selected_by_book"],
               "buckets": sc["verdict_buckets_pooled"]}
        for sid, sc in out["scenarios"].items()}
    return payload


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=None,
                    help="目标库文件（默认按 LG_SIM_DB / LG_DATABASE_URL / "
                         "repo/data/language_genome.db 解析）")
    ap.add_argument("--books", default=None,
                    help="候选世界 id（逗号分隔，支持前缀解析）；"
                         "默认取审计点名的两个 + 真库自发现的非基准作品")
    ap.add_argument("--out", default=None, help="JSON 另写一份到该路径")
    ap.add_argument("--compact", action="store_true",
                    help="stdout 只出摘要（逐策略明细仍在场景块里省略）")
    ap.add_argument("--no-report", action="store_true",
                    help="不打印 stderr 人读摘要")
    a = ap.parse_args(argv)
    books = a.books.split(",") if a.books else None
    out = simulate(a.db or default_db_path(), books, out_path=a.out)
    if not a.no_report:
        print(render_report(out), file=sys.stderr, flush=True)
    print(json.dumps(_compact(out) if a.compact else out,
                     ensure_ascii=False, indent=1))
    passed = out["selfcheck_s0_matches_k3_truth"]["passed"]
    if not passed:
        print("[k2_unlock_sim] S0 与 K3 真身口径不符——这是仿真偏差，必须查清写进"
              "文档，不许调参数凑数：见 "
              "selfcheck_s0_matches_k3_truth.deviations", file=sys.stderr)
    return 0 if passed else 3


if __name__ == "__main__":
    sys.exit(main())
