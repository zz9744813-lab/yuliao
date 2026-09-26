#!/usr/bin/env python
"""K5 非基准供给池缺口只读盘查（2026-09-26 派工 nonbench-supply-gap；只读、零模型调用、零写库、零 git 写）。

## 要回答的问题

`python scripts/k2_extract_backfill.py --dry-run --source-scope nonbenchmark` 的主控
真跑读数：候选来源 9 → 合格来源 6 → 合格段 **4213**、可配对 **33703**，但
`would_pass_all = 0/33703`，否决原因只有 `excluded_scope_uncertain` 与
`status_not_eligible` 两条。**同时**排除来源里有真问题：
`WK-dc90993434e9`（覆汉（榴弹怕水），`human_fiction`，`corpus-v1`，35974 段）
→ `n_segments_kept: 0`，原因 `source_gate_failed:src_ok_or_text_clean`。

疑点：非基准供给池到底是被「**策略状态闸**」（K3 侧 status/scope，归策略审查席拍板）
卡住，还是被「**`src_ok`/`text_clean` 门**」（数据侧可做的活）吃掉？两条路的处理方
完全不同，必须先分开量化。本脚本对真库逐作品盘查，一次段级扫描出全部读数。

## 口径纪律

- 真库 `sqlite3` `mode=ro` 只读；任何写操作抛异常（回归钉死）；零 LLM 调用、零网络。
- **绝不另写一套口径**——全部单源复用既有常量与判据函数：
  · `scripts/k2_extract_backfill.py`：`nonbenchmark_compliant_source`（非基准白名单）、
    `SOURCE_SCOPES` / `BENCHMARK_ROLE` / `_role_passes_scope`（段 role 判定）、
    `_k3_source_gate`（K3 来源闸镜像）、`MAX_TRACE_SOURCES`（明细截断上限）；
  · `scripts/source_check.py`：`parse_src_ok`（严格布尔三态）、`needs_check`
    （非严格布尔即未校验）、`NONBENCH_EXCLUDED_SOURCE_TYPES`、`parse_work_ids`；
  · `app/knowledge_query.py`：`DEFAULT_EXCLUDED_SOURCE_TYPES`（fixture/synthetic/
    commentary）、`DEFAULT_ALLOWED_TEXT_VERSIONS`、`eligible_statuses`、
    `ELIGIBLE_OBSERVATION`（策略状态闸复算）。
  · K2「策略抽取宇宙」的 status 集合在 driver 里是内联字面量（无公开常量），
    本脚本用 AST 从 `scripts/k2_extract_backfill.py` 源码挖出并记行号
    （`k2_status_universe`），命中数≠1 即 fail-closed 不复算——同样不复制字面量。
  任一入口 import 不到 / 常量缺失 / 排除集与 K2 侧不是同一对象 ⇒ **fail-closed
  非零退出**，不带病出数。
- 段级判据不写进 SQL 字符串比较，而是把上面的 **Python 判据函数**注册成 SQLite
  确定性函数后在段级子查询里各调用一次（镜像即原函数，不存在第二支笔）；随后
  所有分布/分桶都在 Python 里对这张紧凑分类表求和——一趟段扫描，口径可逐段对账。
- 本脚本**不跑校验、不放宽任何门**：收口判据是「按现有门规则机械推算」的算术上下界
  （补齐 `src_ok` 校验后全部判 true 才可能达到的上界 / 全部判 false 的下界）。

用法：
    python scripts/k5_nonbench_supply_gap.py [--db PATH]
        [--md-out docs/K5非基准供给缺口_20260926.md]
        [--json-out PATH] [--print-only] [--work-id WK-... （可重复，只收窄）]
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
K2_SRC = ROOT / "scripts" / "k2_extract_backfill.py"
for _p in (str(ROOT), str(ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ── 单源复用既有口径（import 不到即 fail-closed 退出，绝不另写一套）──────
try:
    import k2_extract_backfill as k2b                       # noqa: E402
except Exception as _e:                                     # noqa: BLE001
    raise SystemExit(
        "无法单源复用 K2 供给口径 (scripts/k2_extract_backfill.py)："
        f"{type(_e).__name__}: {_e}。本盘查禁止另写一套口径，已 fail-closed 退出。")

try:
    import source_check as sc                               # noqa: E402
except Exception as _e:                                     # noqa: BLE001
    raise SystemExit(
        "无法单源复用源校验口径 (scripts/source_check.py)："
        f"{type(_e).__name__}: {_e}。本盘查禁止另写一套口径，已 fail-closed 退出。")

try:
    from app import knowledge_query as KQ                   # noqa: E402
    ALLOWED_TEXT_VERSIONS = KQ.DEFAULT_ALLOWED_TEXT_VERSIONS
    _ELIGIBLE_STATUSES = KQ.eligible_statuses
    _ELIGIBLE_OBSERVATION = KQ.ELIGIBLE_OBSERVATION
except Exception as _e:                                     # noqa: BLE001
    raise SystemExit(
        "无法单源复用 K3 侧常量/判据 (app.knowledge_query)："
        f"{type(_e).__name__}: {_e}。已 fail-closed 退出。")

# 排除集必须与 K2 侧是**同一个对象**（source_check 在 import 期就从 KQ 取；这里再
# 核一次，防止有人中途复制成第二个 frozenset 字面量）
EXCLUDED_SOURCE_TYPES = sc.NONBENCH_EXCLUDED_SOURCE_TYPES
if EXCLUDED_SOURCE_TYPES is not KQ.DEFAULT_EXCLUDED_SOURCE_TYPES:
    raise SystemExit("NONBENCH_EXCLUDED_SOURCE_TYPES 与 "
                     "app.knowledge_query.DEFAULT_EXCLUDED_SOURCE_TYPES 不同源，"
                     "已 fail-closed 退出（不许复制常量）。")

NONBENCHMARK_SCOPE = "nonbenchmark"
if NONBENCHMARK_SCOPE not in k2b.SOURCE_SCOPES:
    raise SystemExit(f"k2_extract_backfill.SOURCE_SCOPES 缺 {NONBENCHMARK_SCOPE!r}，"
                     "口径已漂移，fail-closed 退出。")
BENCHMARK_ROLE = k2b.BENCHMARK_ROLE
FUHAN_WORK_ID = "WK-dc90993434e9"

MAIN_REPO_DB = Path("F:/agi/language-genome/data/language_genome.db")
DOC_REL = Path("docs") / "K5非基准供给缺口_20260926.md"

# 主控 2026-09-26 K2 真跑读数（任务书背景给定）——只作对照基准，不是本脚本产物
REF_K2 = [
    ("候选来源（有段或有登记的 work 数）", "candidate_sources", 9),
    ("合格来源（有保留段的 work 数）", "qualified_sources", 6),
    ("非基准合格段", "eligible_segments", 4_213),
    (f"覆汉 {FUHAN_WORK_ID} 段总数", "fuhan_segments", 35_974),
    (f"覆汉 {FUHAN_WORK_ID} 保留段", "fuhan_kept", 0),
]

# src_ok 五态里「未校验」的三支（source_check.parse_src_ok 返回 None 的全部情形）
UNVERIFIED_STATES = ("missing", "loose", "nojson")

# 各闸分桶（顺序即判定顺序，与 k2_extract_backfill.segment_universe 同序；
# 各桶互斥、求和恒等于全库段数——逐段可对账）
BUCKET_ORDER = [
    ("no_registry", "作品无 work_sources 登记（fail-closed 整作排除）"),
    ("excluded_source_type", "来源类型命中 K2 同源排除集 fixture/synthetic/commentary"),
    ("source_type_not_compliant", "来源类型不在非基准白名单（human_fiction / production_nonbenchmark_*）"),
    ("role_benchmark", "段 role == 'benchmark'（非基准口径剔除）"),
    ("src_ok_false", "src_ok 严格布尔 false —— 判坏（补跑不可翻正）"),
    ("src_ok_unverified", "src_ok 未校验（缺键/类型不严/非法 JSON）—— 数据侧可做的活"),
    ("text_clean_empty", "text_clean 为空（清洗缺位）"),
    ("eligible", "非基准合格段（= K2 抽段宇宙，可配对供给池）"),
]


# ── 判据函数（薄封装既有入口，无独立口径）─────────────────────────────
def src_state(integrity) -> str:
    """integrity → 五态 true/false/missing/loose/nojson。

    严格布尔判定**直接调** `source_check.parse_src_ok`（唯一支笔）：JSON true→true、
    JSON false→false、其余（字符串 "false"/"true"、数字 1/0、null、缺字段）→None。
    None 再按「键在不在」细分 loose（键在但类型不严）与 missing（合法 JSON 缺键）；
    非法 JSON / 非对象 / NULL 记 nojson。后三支一律 = 未校验（`needs_check` 同语义：
    非严格布尔必须重查，存量脏值不允许被幂等永久留存）。
    """
    try:
        d = json.loads(integrity)
    except (TypeError, ValueError):
        return "nojson"
    if not isinstance(d, dict):
        return "nojson"
    v = sc.parse_src_ok(d.get("src_ok"))
    if v is True:
        return "true"
    if v is False:
        return "false"
    return "loose" if "src_ok" in d else "missing"


def text_clean_ok(text_clean) -> int:
    """`text_clean` 非空判定——与 k2_extract_backfill 段宇宙里
    `if not (seg.text_clean or "").strip()` 同表达式（Python strip，不走 SQL trim，
    免得多类空白字符判不准）。1=非空（过闸）/0=空（被吃掉）。"""
    return 1 if (text_clean or "").strip() else 0


def role_nonbench_ok(role) -> int:
    """段 role 是否属 nonbenchmark 口径——直接调 `k2b._role_passes_scope`
    （NULL 记成字面 "NULL"，与其 `_segment_roles_by_work` 同约定）。"""
    key = role if role is not None else "NULL"
    return 1 if k2b._role_passes_scope(NONBENCHMARK_SCOPE, key) else 0


def register_judges(con: sqlite3.Connection) -> None:
    """把判据注册成 SQLite 确定性函数（镜像即原函数，不复制判定逻辑）。"""
    con.create_function("src_state", 1, src_state, deterministic=True)
    con.create_function("text_clean_ok", 1, text_clean_ok, deterministic=True)
    con.create_function("role_nonbench_ok", 1, role_nonbench_ok,
                        deterministic=True)


# ── 只读访问 ──────────────────────────────────────────────────────────
def default_db() -> Path | None:
    """解析真库：env LG_DATABASE_URL → 本树 data/ → 主检出 data/。"""
    cands: list[Path] = []
    env = os.environ.get("LG_DATABASE_URL", "")
    if env.startswith("sqlite:///"):
        cands.append(Path(env.removeprefix("sqlite:///")))
    cands += [ROOT / "data" / "language_genome.db", MAIN_REPO_DB]
    for c in cands:
        if c.is_file():
            return c
    return None


def open_ro(db_path: Path) -> sqlite3.Connection:
    """只读打开：mode=ro，任何写操作都会抛异常（测试钉死）。"""
    con = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _tables(con: sqlite3.Connection) -> set[str]:
    return {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def _cols(con: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}


def json_probe(con: sqlite3.Connection) -> bool:
    """JSON1 可用性旁证（主体走 Python 判据，但缺 JSON1 说明库口径异常，照旧
    fail-closed）。"""
    try:
        typ = con.execute("SELECT json_type('{\"a\":true}','$.a')").fetchone()[0]
    except sqlite3.Error:
        return False
    return typ == "true"


REQUIRED_SCHEMA = {
    "segments": {"id", "work_id", "integrity", "role", "seg_version", "text_clean"},
    "works": {"id", "title"},
    "work_sources": {"work_id", "source_type", "text_version"},
}

# 段级分类表：一趟扫描，每段恰好 3 次判据调用（注册函数即原 Python 判据本身）。
# 聚合后行数 = work 数 × 五态 × 2 × 2 的上界，很小；此后全部算术在 Python 里做。
_SEG_CLASS_SQL = """
SELECT w, st, clean, nb, COUNT(*) AS n FROM (
  SELECT s.work_id AS w,
         src_state(s.integrity)         AS st,
         text_clean_ok(s.text_clean)    AS clean,
         role_nonbench_ok(s.role)       AS nb
    FROM segments s
) GROUP BY 1, 2, 3, 4
"""


def _registry(con: sqlite3.Connection, cols: set[str]) -> dict[str, dict]:
    """work_sources 登记行。license_purposes 列存在才读（ORM 的 JSON 列会反序列化，
    这里 json.loads 同一份原文，保持一致）。"""
    want = ["work_id", "source_type", "text_version"]
    extra = [c for c in ("license_purposes", "identity_purposes") if c in cols]
    sql = f"SELECT {', '.join(want + extra)} FROM work_sources"
    out = {}
    for r in con.execute(sql):
        d = dict(r)
        for k in extra:
            try:
                d[k] = json.loads(d[k] or "[]")
            except (TypeError, ValueError):
                d[k] = []                     # 非法 JSON 与原值都进不了授权交集
        out[r["work_id"]] = d
    return out


def _seg_versions(con: sqlite3.Connection) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for r in con.execute("SELECT s.work_id AS w,"
                         " COALESCE(CAST(s.seg_version AS TEXT),'(null)') AS v,"
                         " COUNT(*) AS n FROM segments s GROUP BY 1, 2"):
        out.setdefault(r["w"], {})[r["v"]] = r["n"]
    return out


def _k3_source_reason(reg: dict | None, st_norm: str, text_version,
                      license_purposes) -> str | None:
    """would-be 实例的 K3 **来源闸**——直接调 `k2b._k3_source_gate`（它只按属性名
    取值，这里用 SimpleNamespace 喂同名字段，不复制其判定顺序/常量）。"""
    ws = SimpleNamespace(source_type=st_norm, text_version=text_version,
                         license_purposes=license_purposes or [])
    seg = SimpleNamespace(role=None)          # 非基准段：闸内 role 判定走 nb 分支
    return k2b._k3_source_gate(seg, text_version, work_source=ws)


def _work_rows(con: sqlite3.Connection, work_filter: tuple | None) -> list[dict]:
    """逐作品盘查行 + 各闸分桶（互斥求和 = 该作品段总数）。"""
    ws_cols = _cols(con, "work_sources")
    reg = _registry(con, ws_cols)
    titles = {r["id"]: (r["title"] or "") for r in
              con.execute("SELECT id, title FROM works")}
    svers = _seg_versions(con)

    # 段级分类计数：work -> (st, clean, nb) -> n
    cls: dict[str, dict[tuple[str, int, int], int]] = {}
    for r in con.execute(_SEG_CLASS_SQL):
        cls.setdefault(r["w"], {})[(r["st"], r["clean"], r["nb"])] = r["n"]

    work_ids = set(cls) | set(reg)
    if work_filter is not None:
        wf = set(work_filter)                 # 只收窄不放宽（同 source_check --work-id）
        work_ids &= wf
        reg = {k: v for k, v in reg.items() if k in wf}

    out = []
    for wid in sorted(work_ids):
        cells = cls.get(wid, {})
        n_total = sum(cells.values())
        r = reg.get(wid)
        st = r["source_type"] if r else None
        st_norm = (st or "").strip()
        tv = r["text_version"] if r else None
        compliant = k2b.nonbenchmark_compliant_source(st)
        in_excl = st_norm in EXCLUDED_SOURCE_TYPES
        in_pool = bool(r is not None and compliant and not in_excl)

        marg = {"true": 0, "false": 0, "missing": 0, "loose": 0, "nojson": 0}
        clean_empty = bench = 0
        b = {k: 0 for k, _ in BUCKET_ORDER}
        kept = recoverable = judged_bad = 0
        for (state, clean, nb), n in cells.items():
            marg[state] += n
            if not clean:
                clean_empty += n
            if not nb:
                bench += n
            # 分桶（判定顺序 = 来源级 → role → src_ok → text_clean）
            if r is None:
                b["no_registry"] += n
            elif in_excl:
                b["excluded_source_type"] += n
            elif not compliant:
                b["source_type_not_compliant"] += n
            elif not nb:
                b["role_benchmark"] += n
            elif state == "false":
                b["src_ok_false"] += n
                if clean:
                    judged_bad += n
            elif state in UNVERIFIED_STATES:
                b["src_ok_unverified"] += n
                if clean:
                    recoverable += n
            elif not clean:
                b["text_clean_empty"] += n
            else:
                b["eligible"] += n
                kept += n
        unverified = marg["missing"] + marg["loose"] + marg["nojson"]
        out.append({
            "work_id": wid,
            "title": titles.get(wid, "(无 works 行)"),
            "source_type": st if st is not None else "(未登记)",
            "text_version": tv if tv is not None else "(未登记)",
            "seg_versions": dict(sorted(svers.get(wid, {}).items())),
            "registered": r is not None,
            "compliant_source": compliant,
            "hit_excluded_source_type": in_excl,
            "in_pool": in_pool,
            "text_version_allowed": bool(tv in ALLOWED_TEXT_VERSIONS),
            "k3_source_gate": _k3_source_reason(
                r, st_norm, tv, (r or {}).get("license_purposes")) if r else "no_registry",
            "n_segments": n_total,
            "src_true": marg["true"],
            "src_false": marg["false"],
            "src_missing": marg["missing"],
            "src_loose": marg["loose"],
            "src_nojson": marg["nojson"],
            "src_unverified": unverified,
            "src_unverified_ratio": round(unverified / n_total, 6) if n_total else 0.0,
            "text_clean_empty": clean_empty,
            "text_clean_empty_ratio": (round(clean_empty / n_total, 6)
                                       if n_total else 0.0),
            "role_benchmark_segments": bench,
            "kept_nonbenchmark": kept,
            "recoverable_if_verified": recoverable,
            "judged_bad_nonbench": judged_bad,
            "buckets": b,
        })
    return out


def k2_status_universe(path: Path = K2_SRC) -> dict:
    """K2「策略抽取宇宙」的 status 集合——driver 里是**内联字面量**
    （`ExpressionStrategyV2.status.in_(...)`，无公开常量可 import），所以从源码
    AST 挖出来用，并带行号自指；这里**不复制字面量**。命中数≠1（口径被拆成多处
    或改名）即 ok=False，策略闸 fail-closed 不复算。"""
    out = {"path": Path(path).as_posix(), "ok": False, "statuses": [],
           "line": None, "hits": 0, "why": ""}
    try:
        tree = ast.parse(Path(path).read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as e:
        out["why"] = f"{type(e).__name__}: {e}"
        return out
    found = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "in_" and len(node.args) == 1):
            continue
        chain = node.func.value
        if not (isinstance(chain, ast.Attribute) and chain.attr == "status"
                and isinstance(chain.value, ast.Name)
                and chain.value.id == "ExpressionStrategyV2"):
            continue
        try:
            val = ast.literal_eval(node.args[0])
        except ValueError:
            continue
        seq = val if isinstance(val, (tuple, list, set, frozenset)) else (val,)
        if not all(isinstance(x, str) for x in seq):
            continue
        found.append((node.lineno, sorted(seq)))
    out["hits"] = len(found)
    if len(found) != 1:
        out["why"] = (f"源码里 ExpressionStrategyV2.status.in_(字面量) 命中 "
                      f"{len(found)} 处（须恰好 1 处才无歧义）：{found}")
        return out
    out["line"], out["statuses"] = found[0]
    out["ok"] = True
    return out


def strategy_gate(con: sqlite3.Connection) -> dict:
    """策略状态闸复算（只读）：K2 抽取宇宙（status ∈ 抽取宇宙集合）里，
    按 K3 真判据 `eligible_statuses(version)` ∧ `observation_status ∈
    ELIGIBLE_OBSERVATION` 数还有几条策略的证据能被 K3 采信——这决定「可进 K3 的
    证据」是否恒 0。scope 只按 `_scope_matches` 中**不依赖按书反查登记**的两支如实
    归桶（直接读 scope 列），WORK/AUTHOR/GENRE 需登记反查，本盘查不复算（不猜）。"""
    uni = k2_status_universe()
    if not uni["ok"]:
        return {"error": "K2 抽取宇宙的 status 集合无法从 "
                         f"`{K2_SRC.as_posix()}` 源码单源取到（{uni['why']}）——"
                         "不复制字面量、不猜口径，策略闸 fail-closed 不复算。"}
    placeholders = ", ".join("?" * len(uni["statuses"]))
    try:
        dist = {r["status"] if r["status"] is not None else "(null)": r["n"]
                for r in con.execute(
                    "SELECT status, COUNT(*) AS n FROM expression_strategies_v2"
                    " GROUP BY status")}
        rows = con.execute(
            "SELECT id, strategy_key, version, status, observation_status, scope"
            f" FROM expression_strategies_v2 WHERE status IN ({placeholders})"
            " ORDER BY strategy_key, version, id", uni["statuses"]).fetchall()
    except sqlite3.Error as e:
        return {"error": f"expression_strategies_v2 不可读（fail-closed）："
                         f"{type(e).__name__}: {e}"}
    items, n_pass, scope_buckets = [], 0, {}
    for r in rows:
        ok = (r["status"] in _ELIGIBLE_STATUSES(r["version"])
              and r["observation_status"] in _ELIGIBLE_OBSERVATION)
        n_pass += int(ok)
        sk = r["scope"] if r["scope"] is not None else "(null)"
        scope_buckets[sk] = scope_buckets.get(sk, 0) + 1
        items.append({"strategy_key": r["strategy_key"], "version": r["version"],
                      "status": r["status"],
                      "observation_status": r["observation_status"],
                      "scope": sk, "status_gate_pass": ok})
    # 抽取宇宙与 K3 合格集是否**天然不相交**（两支口径按定义无交集 ⇒ status 闸
    # 对抽取宇宙恒不通过，恒 0 与供给量无关）。逐版本求交，不只看默认口径。
    eligible_union: set[str] = set()
    for ver in sorted({str(r["version"]) for r in rows} | {""}):
        eligible_union |= set(_ELIGIBLE_STATUSES(ver or None))
    disjoint = not (set(uni["statuses"]) & eligible_union)
    return {"universe_statuses": list(uni["statuses"]),
            "universe_source_line": uni["line"],
            "universe_eligible_intersection": sorted(
                set(uni["statuses"]) & eligible_union),
            "universe_disjoint_from_eligible": disjoint,
            "status_distribution_all": dict(sorted(dist.items())),
            "n_extract_universe": len(rows), "n_status_gate_pass": n_pass,
            "scope_buckets": dict(sorted(scope_buckets.items())),
            "eligible_statuses_default": sorted(_ELIGIBLE_STATUSES(None)),
            "eligible_observation": sorted(_ELIGIBLE_OBSERVATION),
            "items": items,
            "note": ("抽取宇宙集合从 scripts/k2_extract_backfill.py 源码 AST 取"
                     f"（行 {uni['line']}，不复制字面量）；status 闸用 "
                     "app.knowledge_query.eligible_statuses/ELIGIBLE_OBSERVATION "
                     "真常量复算；scope 直接读列（UNCERTAIN ⇒ "
                     "excluded_scope_uncertain、GLOBAL ⇒ 预留），WORK/AUTHOR/GENRE "
                     "需按书反查登记，本盘查不复算。")}


def build_result(db_path: Path, *, work_filter: tuple | None = None) -> dict:
    """真跑盘查（只读）。同库同参两次输出逐字节一致（无时间戳、序固定）。"""
    res: dict = {
        "tool": "k5_nonbench_supply_gap",
        "db_path": Path(db_path).as_posix(),
        "sqlite_version": sqlite3.sqlite_version,
        "readonly": True,
        "source_scope": NONBENCHMARK_SCOPE,
        "work_filter": sorted(work_filter) if work_filter else None,
        "reused_calibers": {
            "k2_extract_backfill": ["BENCHMARK_ROLE", "MAX_TRACE_SOURCES",
                                    "NONBENCHMARK_SOURCE_TYPES",
                                    "NONBENCHMARK_SOURCE_TYPE_PREFIX",
                                    "SOURCE_SCOPES", "_k3_source_gate",
                                    "_role_passes_scope",
                                    "nonbenchmark_compliant_source"],
            "source_check": ["NONBENCH_EXCLUDED_SOURCE_TYPES", "needs_check",
                             "parse_src_ok", "parse_work_ids"],
            "knowledge_query": ["DEFAULT_ALLOWED_TEXT_VERSIONS",
                                "DEFAULT_EXCLUDED_SOURCE_TYPES",
                                "ELIGIBLE_OBSERVATION", "eligible_statuses"],
            "nonbenchmark_source_types": sorted(k2b.NONBENCHMARK_SOURCE_TYPES),
            "nonbenchmark_source_type_prefix": k2b.NONBENCHMARK_SOURCE_TYPE_PREFIX,
            "excluded_source_types": sorted(EXCLUDED_SOURCE_TYPES),
            "allowed_text_versions": sorted(ALLOWED_TEXT_VERSIONS),
            "benchmark_role": BENCHMARK_ROLE,
            "eligible_statuses_default": sorted(_ELIGIBLE_STATUSES(None)),
            "k2_status_universe": k2_status_universe(),
        },
        "bucket_legend": [{"key": k, "desc": d} for k, d in BUCKET_ORDER],
        "issues": [],
    }
    con = open_ro(db_path)
    try:
        tabs = _tables(con)
        missing = []
        if not json_probe(con):
            missing.append("JSON1 扩展不可用")
        for name, cols in REQUIRED_SCHEMA.items():
            if name not in tabs:
                missing.append(f"{name} 表缺失")
            else:
                have = _cols(con, name)
                if not cols <= have:
                    missing.append(f"{name} 缺列 {sorted(cols - have)}")
        if missing:
            res["issues"].append("；".join(missing))
            res["conclusion"] = {
                "verdict": "不可盘查",
                "supply_gate": {"verdict": "未知", "who": "—"},
                "policy_gate": {"verdict": "未知", "who": "—", "reason": ""},
                "reading": "库结构不满足盘查前提，fail-closed，不出数。",
                "reason": "库结构不满足盘查前提：" + "；".join(missing)}
            return res
        register_judges(con)
        works = _work_rows(con, work_filter)
        gate = strategy_gate(con)
    finally:
        con.close()
    res["strategy_gate"] = gate

    buckets = {k: 0 for k, _ in BUCKET_ORDER}
    for w in works:
        for k in buckets:
            buckets[k] += w["buckets"][k]
    grand = {
        "n_segments": sum(w["n_segments"] for w in works),
        "src_true": sum(w["src_true"] for w in works),
        "src_false": sum(w["src_false"] for w in works),
        "src_missing": sum(w["src_missing"] for w in works),
        "src_loose": sum(w["src_loose"] for w in works),
        "src_nojson": sum(w["src_nojson"] for w in works),
        "src_unverified": sum(w["src_unverified"] for w in works),
        "text_clean_empty": sum(w["text_clean_empty"] for w in works),
        "role_benchmark": sum(w["role_benchmark_segments"] for w in works),
    }
    grand["src_unverified_ratio"] = (round(grand["src_unverified"]
                                           / grand["n_segments"], 6)
                                     if grand["n_segments"] else 0.0)
    pool = [w for w in works if w["in_pool"]]
    eligible = buckets["eligible"]
    recoverable = sum(w["recoverable_if_verified"] for w in works)
    judged_bad = sum(w["judged_bad_nonbench"] for w in works)
    res["works"] = works
    res["totals"] = {
        **grand, "buckets": buckets,
        "bucket_sum": sum(buckets.values()),
        "bucket_sum_equals_total": sum(buckets.values()) == grand["n_segments"],
        "candidate_sources": len(works),
        "pool_sources": len(pool),
        "qualified_sources": sum(1 for w in works if w["kept_nonbenchmark"] > 0),
        "eligible_segments": eligible,
        "pool_segments": sum(w["n_segments"] for w in pool),
    }
    res["projection"] = {
        "eligible_now": eligible,
        "recoverable_if_verified": recoverable,
        "eligible_upper_bound": eligible + recoverable,
        "eligible_lower_bound": eligible,
        "judged_bad_not_recoverable": judged_bad,
        "unverified_in_pool": buckets["src_ok_unverified"],
        "pool_sources_with_k3_source_gate_ok": sum(
            1 for w in pool if w["k3_source_gate"] is None),
        "note": (
            "机械推算（现有门规则，不放宽任何门）：把非基准池内 role 与 text_clean "
            "两闸都已过、只差 `src_ok` 未校验的段全部补齐源校验——若全部判 true，"
            f"非基准合格段从 {eligible:,} 涨到 {eligible + recoverable:,}（算术上界）；"
            f"若全部判 false 则仍是 {eligible:,}（下界）。`src_ok=false` 判坏的 "
            f"{judged_bad:,} 段补跑不可翻正，不进上界。上界还受「补齐后文本是否仍"
            "非空」约束，这里按补齐不改动正文计（source_check 只写 integrity，"
            "不碰 text/text_clean）。"),
    }

    ref_map = {
        "candidate_sources": res["totals"]["candidate_sources"],
        "qualified_sources": res["totals"]["qualified_sources"],
        "eligible_segments": eligible,
        "fuhan_segments": next((w["n_segments"] for w in works
                                if w["work_id"] == FUHAN_WORK_ID), None),
        "fuhan_kept": next((w["kept_nonbenchmark"] for w in works
                            if w["work_id"] == FUHAN_WORK_ID), None),
    }
    res["k2_reference"] = [{"item": item, "key": key, "ref": ref,
                            "measured": ref_map[key],
                            "consistent": ref_map[key] == ref}
                           for item, key, ref in REF_K2]

    supply = {
        "eaten_by_src_ok_unverified": buckets["src_ok_unverified"],
        "eaten_by_src_ok_false": buckets["src_ok_false"],
        "eaten_by_text_clean_empty": buckets["text_clean_empty"],
        "eaten_by_role_benchmark": buckets["role_benchmark"],
        "eaten_by_source_gate": (buckets["no_registry"]
                                 + buckets["excluded_source_type"]
                                 + buckets["source_type_not_compliant"]),
        "pool_segments": res["totals"]["pool_segments"],
        "eligible": eligible,
    }
    policy = ({"strategies_in_extract_universe": gate["n_extract_universe"],
               "status_gate_pass": gate["n_status_gate_pass"],
               "universe_statuses": gate["universe_statuses"],
               "universe_source_line": gate["universe_source_line"],
               "universe_disjoint_from_eligible":
                   gate["universe_disjoint_from_eligible"],
               "status_distribution_all": gate["status_distribution_all"],
               "scope_buckets": gate["scope_buckets"]}
              if "error" not in gate else {"error": gate["error"]})
    if "error" in policy:
        p_verdict, p_reason = "未知", policy["error"]
    elif policy["status_gate_pass"] == 0:
        p_verdict = "卡死"
        p_reason = (f"K2 抽取宇宙 {policy['strategies_in_extract_universe']} 条策略中，"
                    f"过 K3 status 闸（`eligible_statuses(version)` ∧ "
                    f"`observation_status ∈ ELIGIBLE_OBSERVATION`）的有 "
                    f"{policy['status_gate_pass']} 条；scope 分布 "
                    f"{policy['scope_buckets']}——「可进 K3 的证据恒 0」由**策略"
                    "状态/scope 闸**造成，与供给量无关（补再多段也还是 0）。"
                    + ("且抽取宇宙 status 集合 "
                       f"`{policy['universe_statuses']}` 与 K3 合格集"
                       "**按定义无交集**"
                       f"（交集={gate['universe_eligible_intersection']}，全库 status "
                       f"分布 {policy['status_distribution_all']}）：恒 0 是"
                       "**晋升没发生**（上游写侧把抽取宇宙里的行 status 升到 K3 "
                       "合格集），不是策略行缺失或数据不够——先补供给不会让恒 0"
                       "变正。"
                       if policy["universe_disjoint_from_eligible"] else ""))
    else:
        p_verdict = "未卡死"
        p_reason = (f"status 闸已有 {policy['status_gate_pass']} 条策略合格，恒 0 需"
                    "另找原因（scope 按书反查 / 预算 / 去重排序）。")
    s_verdict = "卡死" if (supply["eaten_by_src_ok_unverified"]
                           or supply["eaten_by_text_clean_empty"]) else "未卡死"
    res["attribution"] = {"supply_gate": supply, "policy_gate": policy}
    res["conclusion"] = {
        "verdict": ("两闸都有份（须分开处理）"
                    if s_verdict == "卡死" and p_verdict == "卡死"
                    else ("供给门卡死" if s_verdict == "卡死"
                          else ("策略门卡死" if p_verdict == "卡死"
                                else "两闸都未卡死"))),
        "supply_gate": {"verdict": s_verdict, "detail": supply,
                        "who": "数据侧：补跑 scripts/source_check.py（--scope nonbench）"},
        "policy_gate": {"verdict": p_verdict, "reason": p_reason,
                        "who": "策略审查席：strategy status/scope 拍板"},
        "reading": (
            f"全库 {grand['n_segments']:,} 段中，非基准池（合规人类语料，不含 "
            f"fixture/synthetic/commentary）候选 {supply['pool_segments']:,} 段；"
            f"其中 `role=='benchmark'` 剔 {supply['eaten_by_role_benchmark']:,}、"
            f"`src_ok=false` 判坏 {supply['eaten_by_src_ok_false']:,}、"
            f"`src_ok` **未校验** {supply['eaten_by_src_ok_unverified']:,}、"
            f"`text_clean` 空 {supply['eaten_by_text_clean_empty']:,}，"
            f"最终合格 {eligible:,}。补齐未校验段的源校验后算术上界 "
            f"{eligible + recoverable:,} 段（下界仍 {eligible:,}）。而"
            + ("且" if p_verdict == "卡死" else "同时")
            + f"策略侧：{p_reason}"),
    }
    return res


# ── 渲染 ──────────────────────────────────────────────────────────────
def _num(v) -> str:
    return "—" if v is None else f"{v:,}"


def render_markdown(res: dict) -> str:
    L: list[str] = []
    add = L.append
    add("# K5 非基准供给池缺口只读盘查（2026-09-26）")
    add("")
    add("- 盘查对象：`k2_extract_backfill --source-scope nonbenchmark` 的段宇宙"
        "（非基准试点供给池）——回答「为什么可进 K3 的证据恒 0」到底是"
        "**策略状态闸**卡的，还是 **`src_ok`/`text_clean` 门**吃的。")
    add(f"- 工具：`scripts/k5_nonbench_supply_gap.py`（`sqlite3` `mode=ro` **只读**、"
        "零模型调用、零写库、零 git 写；同库同参两次输出逐字节一致）。")
    add(f"- 库：`{res['db_path']}`｜SQLite {res['sqlite_version']}｜"
        f"口径：`--source-scope {res['source_scope']}`"
        + (f"｜`--work-id` 收窄 {len(res['work_filter'])} 本（只收窄不放宽）"
           if res.get("work_filter") else ""))
    rc = res["reused_calibers"]
    add("- 口径单源复用（import 不到即 fail-closed 退出，绝不另写一套）："
        "`scripts/k2_extract_backfill.py` + `scripts/source_check.py` + "
        "`app/knowledge_query.py`。")
    add(f"  - 非基准白名单：`{rc['nonbenchmark_source_types']}` ∪ 前缀 "
        f"`{rc['nonbenchmark_source_type_prefix']}`｜K2 同源排除集："
        f"`{rc['excluded_source_types']}`｜合格文本版本：`{rc['allowed_text_versions']}`｜"
        f"基准 role：`{rc['benchmark_role']}`｜K3 status 闸默认集合："
        f"`{rc['eligible_statuses_default']}`。")
    add("")
    if res["issues"]:
        add("## 0. fail-closed 记录")
        add("")
        for i in res["issues"]:
            add(f"- {i}")
        add("")
    t = res.get("totals")
    if t:
        add("## 1. 全库 `src_ok` 三态与 `text_clean` 读数（严格布尔口径）")
        add("")
        add("| 项 | 段数 |")
        add("|---|---:|")
        add(f"| 全库段总数 | {t['n_segments']:,} |")
        add(f"| `src_ok` 严格布尔 true（完好） | {t['src_true']:,} |")
        add(f"| `src_ok` 严格布尔 false（判坏） | {t['src_false']:,} |")
        add(f"| `src_ok` 未校验合计（缺键 {t['src_missing']:,} + 类型不严 "
            f"{t['src_loose']:,} + 非法 JSON {t['src_nojson']:,}） | "
            f"**{t['src_unverified']:,}**（{t['src_unverified_ratio']:.2%}） |")
        add(f"| `text_clean` 为空 | {t['text_clean_empty']:,} |")
        add(f"| `role == 'benchmark'` | {t['role_benchmark']:,} |")
        add("")
        add("「未校验」≠「判坏」：口径同 `source_check.parse_src_ok` 三态——只有 JSON "
            "`true`/`false` 算已校验；字符串 `\"false\"`/`\"true\"`、数字 1/0、`null`、"
            "缺键、非法 JSON 一律未校验（`needs_check` 要求重查）。**字符串 "
            "`\"false\"` 绝不计入「通过」**。")
        add("")
        add("## 2. 被各闸吃掉的段数分桶（互斥；求和 = 全库段数）")
        add("")
        add("| 闸 | 含义 | 吃掉段数 |")
        add("|---|---|---:|")
        for k, desc in BUCKET_ORDER:
            add(f"| `{k}` | {desc} | {t['buckets'][k]:,} |")
        add(f"| **求和** | 对账（须等于全库段数 {t['n_segments']:,}） | "
            f"**{t['bucket_sum']:,}**｜"
            f"{'一致' if t['bucket_sum_equals_total'] else '**不一致**'} |")
        add("")
        add(f"- 候选来源 {t['candidate_sources']}｜非基准池来源 {t['pool_sources']}"
            f"（池内候选段 {t['pool_segments']:,}）｜有保留段的合格来源 "
            f"{t['qualified_sources']}｜非基准合格段 **{t['eligible_segments']:,}**")
        add("")
        add("## 3. 逐作品盘查")
        add("")
        add("| work_id | 作品 | source_type | text_version | seg_version | 段总数 | "
            "`src_ok` true | false | 未校验 | 未校验占比 | `text_clean` 空率 | "
            "`role=bench` | 池内保留段 |")
        add("|---|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|")
        cap = k2b.MAX_TRACE_SOURCES
        for w in res["works"][:cap]:
            sv = ", ".join(f"v{k}:{v:,}" for k, v in w["seg_versions"].items())
            add(f"| {w['work_id']} | {w['title']} | {w['source_type']} | "
                f"{w['text_version']} | {sv or '—'} | {w['n_segments']:,} | "
                f"{w['src_true']:,} | {w['src_false']:,} | {w['src_unverified']:,} | "
                f"{w['src_unverified_ratio']:.2%} | {w['text_clean_empty_ratio']:.2%} | "
                f"{w['role_benchmark_segments']:,} | {w['kept_nonbenchmark']:,} |")
        if len(res["works"]) > cap:
            add("")
            add(f"> 明细截断至前 {cap} 行（`k2_extract_backfill.MAX_TRACE_SOURCES`），"
                f"共 {len(res['works'])} 行；局部核对用 `--work-id` 收窄。")
        add("")
        add("### 3b. 池内作品的闸链明细（各桶相加 = 该作品段总数）")
        add("")
        pool = [w for w in res["works"] if w["in_pool"]]
        add("| work_id | 段总数 | role=bench | src_ok=false | src_ok 未校验 | "
            "text_clean 空 | 保留段 | 补齐可回段 | K3 来源闸 |")
        add("|---|---:|---:|---:|---:|---:|---:|---:|---|")
        for w in pool[:cap]:
            add(f"| {w['work_id']}《{w['title']}》 | {w['n_segments']:,} | "
                f"{w['buckets']['role_benchmark']:,} | "
                f"{w['buckets']['src_ok_false']:,} | "
                f"{w['buckets']['src_ok_unverified']:,} | "
                f"{w['buckets']['text_clean_empty']:,} | "
                f"{w['kept_nonbenchmark']:,} | {w['recoverable_if_verified']:,} | "
                f"{w['k3_source_gate'] or '过闸'} |")
        if not pool:
            add("| — | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 无非基准池作品 |")
        add("")
    pj = res.get("projection")
    if pj:
        add("## 4. 收口判据：补齐 `src_ok` 校验后的机械推算（不放宽任何门）")
        add("")
        add("| 量 | 段数 |")
        add("|---|---:|")
        add(f"| 现状非基准合格段（K2 抽段宇宙） | {pj['eligible_now']:,} |")
        add(f"| 池内只差 `src_ok` 未校验（role/text_clean 两闸已过） | "
            f"{pj['recoverable_if_verified']:,} |")
        add(f"| 补齐后**算术上界**（未校验段全判 true） | "
            f"**{pj['eligible_upper_bound']:,}** |")
        add(f"| 补齐后**下界**（未校验段全判 false） | "
            f"{pj['eligible_lower_bound']:,} |")
        add(f"| `src_ok=false` 判坏段（补跑不可翻正，不进上界） | "
            f"{pj['judged_bad_not_recoverable']:,} |")
        add("")
        add(pj["note"])
        add("")
    at = res.get("attribution")
    if at:
        a, p = at["supply_gate"], at["policy_gate"]
        add("## 5. 疑点对账：策略状态闸 vs `src_ok`/`text_clean` 门")
        add("")
        add("### 5a. 供给侧（数据侧可做的活）")
        add("")
        add(f"- 非基准池候选 {a['pool_segments']:,} 段里："
            f"`role=='benchmark'` 剔 {a['eaten_by_role_benchmark']:,}、"
            f"`src_ok=false` 判坏 {a['eaten_by_src_ok_false']:,}、"
            f"`src_ok` **未校验** {a['eaten_by_src_ok_unverified']:,}、"
            f"`text_clean` 空 {a['eaten_by_text_clean_empty']:,} ⇒ 合格 "
            f"{a['eligible']:,}。")
        add(f"- 来源级另吃 {a['eaten_by_source_gate']:,} 段（无登记 / 不在非基准白名单 /"
            f" 命中 fixture·synthetic·commentary 排除集——按设计排除）。")
        add("")
        add("### 5b. 策略侧（归策略审查席拍板）")
        add("")
        if "error" in p:
            add(f"- **{p['error']}**（fail-closed，不复算策略闸）")
        else:
            add(f"- K2 抽取宇宙（status ∈ `{p['universe_statuses']}`，该集合从 "
                f"`scripts/k2_extract_backfill.py` 源码行 {p['universe_source_line']} "
                f"AST 取，不复制字面量）共 "
                f"{p['strategies_in_extract_universe']} 条策略；全库 status 分布 "
                f"`{p['status_distribution_all']}`。过 K3 status 闸"
                f"（`eligible_statuses(version)` ∧ `observation_status ∈ "
                f"{sorted(_ELIGIBLE_OBSERVATION)}`）的有 "
                f"**{p['status_gate_pass']}** 条；scope 分布 {p['scope_buckets']}。")
            if p["universe_disjoint_from_eligible"]:
                add("- **结构性结论**：抽取宇宙的 status 集合与 K3 合格集"
                    "`eligible_statuses(version)` **无交集** ⇒ `would_pass_status` "
                    "恒 0 与供给段数无关（补再多段也还是 0）；要变正只能走上游"
                    "**晋升**（写侧把行 status 升为 verified），查询/抽取侧都不放宽。")
            add(f"- 复算口径：`{res['strategy_gate']['note']}`")
        add("")
    co = res.get("conclusion", {})
    add("## 6. 结论")
    add("")
    add(f"- 归因：**{co.get('verdict', '证据不足')}**")
    sg, pg = co.get("supply_gate", {}), co.get("policy_gate", {})
    add(f"- 供给门（`src_ok`/`text_clean`）：**{sg.get('verdict', '—')}**｜处理方："
        f"{sg.get('who', '—')}")
    add(f"- 策略门（status/scope）：**{pg.get('verdict', '—')}**｜处理方："
        f"{pg.get('who', '—')}")
    add(f"- 策略门依据：{pg.get('reason', '')}")
    add("")
    add(co.get("reading", ""))
    add("")
    ref = res.get("k2_reference")
    if ref:
        add("## 7. 与主控 K2 真跑读数对照")
        add("")
        add("| 项目 | 主控 K2 读数 | 本盘查复算 | 是否一致 |")
        add("|---|---:|---:|---|")
        for row in ref:
            add(f"| {row['item']} | {_num(row['ref'])} | {_num(row['measured'])} | "
                f"{'一致' if row['consistent'] else '**不一致**'} |")
        add("")
        add("> 不一致即口径漂移，须先查因再取用本表数字（本脚本不改主控读数）。"
            "「可配对 33703」是策略×段的队列乘积（含幂等与 text_version 过滤），"
            "属 K2 驱动侧口径，本盘查**不复算**；`would_pass_all = 0/33703` 的两条"
            "否决原因由 §5b 策略状态闸只读复算独立佐证。")
        add("")
    add("## 8. 红线声明")
    add("")
    add("- 真库以 `mode=ro` 打开：零写库、零 `segments.integrity` 变更、零模型调用、"
        "零网络；未清空或改写任何原始语料。")
    add("- 未 commit / merge / push；未改主仓或其他 worktree；未改 `source_check.py`、"
        "`k2_extract_backfill.py` 与 K3 侧任何一行。")
    add("- 推算只是现有门规则下的算术上下界，**不构成放宽任何门的建议**；"
        "fixture/synthetic/commentary 与 `role=='benchmark'` 的排除按设计保留。")
    add("")
    return "\n".join(L) + "\n"


def render_stdout(res: dict) -> str:
    L = [f"[k5_nonbench_supply_gap] db={res['db_path']} "
         f"sqlite={res['sqlite_version']} (mode=ro 只读、零模型调用)"]
    if res["issues"]:
        L.append("[fail-closed] " + "；".join(res["issues"]))
    t = res.get("totals")
    if t:
        L.append(f"[src_ok 三态] 全库 {t['n_segments']:,}｜true {t['src_true']:,}"
                 f"｜false {t['src_false']:,}｜未校验 {t['src_unverified']:,}"
                 f"（缺键 {t['src_missing']:,}/不严 {t['src_loose']:,}/"
                 f"非法 {t['src_nojson']:,}，占 {t['src_unverified_ratio']:.2%}）")
        b = t["buckets"]
        L.append("[分桶] " + "｜".join(f"{k}={b[k]:,}" for k, _ in BUCKET_ORDER)
                 + f" ‖ 求和 {t['bucket_sum']:,} "
                 + ("(对账一致)" if t["bucket_sum_equals_total"] else "(**对账不一致**)"))
        L.append(f"[来源] 候选 {t['candidate_sources']}｜池 {t['pool_sources']}"
                 f"｜合格 {t['qualified_sources']}｜非基准合格段 "
                 f"{t['eligible_segments']:,}")
    pj = res.get("projection")
    if pj:
        L.append(f"[推算] 合格 {pj['eligible_now']:,} → 补齐 src_ok 校验后上界 "
                 f"{pj['eligible_upper_bound']:,}（可补 {pj['recoverable_if_verified']:,}"
                 f"｜判坏不可翻 {pj['judged_bad_not_recoverable']:,}）｜下界 "
                 f"{pj['eligible_lower_bound']:,}")
    w = next((x for x in res.get("works", []) if x["work_id"] == FUHAN_WORK_ID), None)
    if w:
        L.append(f"[覆汉 {w['work_id']}《{w['title']}》] source_type={w['source_type']}"
                 f" text_version={w['text_version']} seg_versions={w['seg_versions']}"
                 f" 段 {w['n_segments']:,}｜true {w['src_true']:,}｜"
                 f"false {w['src_false']:,}｜未校验 {w['src_unverified']:,}"
                 f"（{w['src_unverified_ratio']:.2%}）｜text_clean 空 "
                 f"{w['text_clean_empty']:,}（{w['text_clean_empty_ratio']:.2%}）｜"
                 f"保留 {w['kept_nonbenchmark']:,}｜补齐可回 "
                 f"{w['recoverable_if_verified']:,}｜K3 来源闸 "
                 f"{w['k3_source_gate'] or '过闸'}")
    else:
        L.append(f"[覆汉 {FUHAN_WORK_ID}] 本次盘查范围内无该作品段（缺库/已收窄）")
    p = res.get("attribution", {}).get("policy_gate", {})
    if "error" in p:
        L.append(f"[策略闸] {p['error']}")
    elif p:
        L.append(f"[策略闸] 抽取宇宙 status={p['universe_statuses']}"
                 f"（源码行 {p['universe_source_line']}）"
                 f" {p['strategies_in_extract_universe']} 条｜"
                 f"过 status 闸 {p['status_gate_pass']} 条｜"
                 f"与 K3 合格集{'无交集(结构性恒0)' if p['universe_disjoint_from_eligible'] else '有交集'}｜"
                 f"scope {p['scope_buckets']}")
    co = res.get("conclusion", {})
    L.append(f"[结论] {co.get('verdict', '证据不足')}｜供给门="
             f"{co.get('supply_gate', {}).get('verdict', '—')} "
             f"({co.get('supply_gate', {}).get('who', '')})｜策略门="
             f"{co.get('policy_gate', {}).get('verdict', '—')} "
             f"({co.get('policy_gate', {}).get('who', '')})")
    L.append(co.get("reading", ""))
    return "\n".join(L) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="K5 非基准供给池缺口只读盘查（零写库、零模型调用）")
    ap.add_argument("--db", default=None,
                    help="库路径（默认 env LG_DATABASE_URL → 本树 data/ → 主检出 data/）")
    ap.add_argument("--work-id", action="append", default=[],
                    help="只收窄到指定作品（可重复或逗号分隔）；结果恒 ⊆ 全量盘查，"
                         "绝不借它扩宽到不合规来源")
    ap.add_argument("--md-out", default=str(ROOT / DOC_REL),
                    help="markdown 报告输出路径（传 '' 则不写）")
    ap.add_argument("--json-out", default=None, help="JSON 输出路径（默认不写）")
    ap.add_argument("--print-only", action="store_true", help="只打印，不写任何文件")
    a = ap.parse_args(argv)

    if a.db:
        db = Path(a.db)
        if not db.is_file():
            print(f"[k5_nonbench_supply_gap] 指定的库不存在：{db}", file=sys.stderr)
            return 2
    else:
        db = default_db()
        if db is None:
            print("[k5_nonbench_supply_gap] 找不到库（LG_DATABASE_URL / 本树 data/ / "
                  f"{MAIN_REPO_DB} 均无）", file=sys.stderr)
            return 2

    wf = None
    if a.work_id:
        wf = tuple(sc.parse_work_ids(a.work_id))
        if not wf:
            print("[k5_nonbench_supply_gap] --work-id 全为空值，fail-closed 退出",
                  file=sys.stderr)
            return 2
    res = build_result(db, work_filter=wf)
    print(render_stdout(res), end="")
    if a.print_only:
        return 0
    if a.json_out:
        jp = Path(a.json_out)
        jp.parent.mkdir(parents=True, exist_ok=True)
        jp.write_text(json.dumps(res, ensure_ascii=False, sort_keys=True,
                                 indent=2) + "\n", encoding="utf-8")
        print(f"[k5_nonbench_supply_gap] 已写 {jp.as_posix()}")
    if a.md_out:
        mp = Path(a.md_out)
        mp.parent.mkdir(parents=True, exist_ok=True)
        mp.write_text(render_markdown(res), encoding="utf-8")
        print(f"[k5_nonbench_supply_gap] 已写 {mp.as_posix()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
