"""条件门空转盘查器（**只读**，2026-09-27 派工 lg-conditions-gate-idle）。

要回答的问题只有一个：`app/knowledge_query.py::_condition_pipeline`（固定顺序
第二步）这条条件门，在**真库现况**下有没有行可走；`required_matches` /
`good_when_matches` 这两个可解释分量还能不能被抬高。

派工背景（主控真库直读，非推测）：`strategy_conditions` 行数 = 0，
`expression_strategies_v2` 8 行（全 `status='hypothesis'`）。⇒ 条件门对全部
8 条策略**空转**：循环体一次都不进，分量恒为 0，`excluded_bad_when` /
`excluded_required_*` 两类拒绝理由在真库上永远不出现。这**不等于**「条件都
满足」，而是「条件面没有数据」——两者必须分得开，否则任何拿分量做依据的裁定
都在读一个空集。本盘查器就是把这两件事分开摆平的仪器：它只报读数，不裁定。

只读承诺在**连接层**成立，不靠自觉：
- 全部读数走 `sqlite3.connect("file:...?mode=ro", uri=True)`（与仓内
  `strategy_evidence_audit` / `stats_projection_drift` 同一字面口径）；
- 本文件**只发 SELECT/PRAGMA**（PRAGMA 限于 `sqlite_master` 的表名清单读取），
  零 DDL、零 DML；不调 `db.init_db()`、不建表、不改任何一行数据；
- 不调 `db.session()` / SQLAlchemy 引擎：`app.knowledge_query` 只被**导入取
  常量与判据**用（导入链不建库文件，见交付报告 §④），数据面一律直读 sqlite3。

**单源复用既有口径，不另写一套**：合格集判据从 `app.knowledge_query` 取
`eligible_statuses(version)`（按版本分桶的 status 合格集）与
`ELIGIBLE_OBSERVATION`（观察层合格集），报告里以**函数名 + 行号**记下复用
位置；条件行的分桶口径逐字对照 `_condition_pipeline`：
- `required_rows` = `kind='good_when'` ∧ `required`（pipeline 里
  `c.kind != "good_when"` 先 `continue`，故 required 只在 good_when 上被读）；
- `good_when_rows` = `kind='good_when'` ∧ ¬`required`（喂
  `comps["good_when_matches"]` 的那一半）；
- `neutral_when_rows` = `kind='neutral_when'`（pipeline 显式 `continue`，
  **不计正支持**，故不进任何可达上限）；
- `bad_when_rows` = `kind='bad_when'`（硬排除来源）。
两个上限因此**是算出来的**（= 各自行数），不是写死的常量：夹具空库得 0，
插 3 行的夹具得 1。

「可达上限」= 分量在该策略上**理论上**能到的最大值（取全部谓词都 true 的
那次调用），「现状值」= 当次读数所能确定的数：空转策略恒 0；**有行**策略的
取值取决于当次 `semantic_requirements`（本盘查器零 LLM、不跑查询），不是静态
读数，故报 `null` 而不是编一个数——宁可空着也不假造分量。

输出确定：同一库两次运行 JSON 逐字一致（`generated_at` 之外）；策略排序键写死
为 `strategy_key` → `version` → `id`，按 BINARY 序（Python 逐码点比较，与
SQLite BINARY 字节序在 UTF-8 下等价，故排序不依赖 schema 里的 COLLATE）。

用法：
    python scripts/conditions_gate_idle.py [--db PATH] [--out report.json]

退出码：0 = 盘查完成（**有没有空转都是 0**，空转是读数不是故障）；
2 = 跑不起来（库不存在 / mode=ro 打不开 / 表缺失 / 判据源导不进 / 报告写不出）。
"""
from __future__ import annotations

import argparse
import datetime
import json
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT, ROOT / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

TOOL = "conditions_gate_idle"
SCHEMA = "conditions_gate_idle/v1"
TASK = ("条件门空转读数（只读）：strategy_conditions 行数逐策略盘查 + "
        "required_matches/good_when_matches 可达上限；不裁定、不改数据")

# 库路径：缺省 <repo>/data/language_genome.db（工作树/源码检出内），
# 该处不存在时回主检出同名相对路径；--db 覆盖。
MAIN_CHECKOUT_DB = "F:/agi/language-genome/data/language_genome.db"
REPO_DB = ROOT / "data" / "language_genome.db"

NEEDED_TABLES = ("expression_strategies_v2", "strategy_conditions")

# 复用的判据/常量（名字 → 报告里要记的行号）。line 在运行时从源码实读，
# 不写死数字：源码挪行时报告会跟着动，不会留一条过期行号。
REUSED_CRITERIA = {
    "eligible_statuses": "criteria",
    "ELIGIBLE_OBSERVATION": "criteria",
    "_condition_pipeline": "counting_semantics",
}

# `_condition_pipeline` 只认这三种 kind（bad_when 先判，其余非 good_when
# 一律 continue）；契约外 kind 同样不进任何上限，单列计数以免被静默吞掉。
KIND_BUCKETS = ("required", "good_when", "bad_when", "neutral_when", "other")

CURRENT_NOTE = ("空转策略恒 0；有行策略的取值取决于当次 semantic_requirements，"
               "本盘查器不跑查询（非静态读数）故报 null")


def default_db() -> Path:
    """缺省库路径：本树 data/ 优先，其次主检出 data/，都没有则回本树 data/。

    回退只影响「没给 --db」这一种情形；两个候选都在报告里如实标出来源，
    不让读数建立在「猜对了哪个库」上。
    """
    if REPO_DB.exists():
        return REPO_DB
    if Path(MAIN_CHECKOUT_DB).exists():
        return Path(MAIN_CHECKOUT_DB)
    return REPO_DB


def db_source(db_path: Path) -> str:
    p = Path(db_path)
    if p == REPO_DB and not REPO_DB.exists():
        return "<repo>/data/language_genome.db（缺省，未找到）"
    if p == REPO_DB:
        return "<repo>/data/language_genome.db（缺省）"
    if p == Path(MAIN_CHECKOUT_DB):
        return f"{MAIN_CHECKOUT_DB}（缺省回落：主检出）"
    return "--db"


# ------------------------------------------------------------ 只读连接层
def ro_connect(db_path: Path) -> sqlite3.Connection:
    """mode=ro 只读连接：任何写尝试在 SQLite 层直接抛异常。"""
    return sqlite3.connect(f"file:{Path(db_path).resolve().as_posix()}?mode=ro",
                           uri=True)


def require_tables(con: sqlite3.Connection) -> None:
    have = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    missing = [t for t in NEEDED_TABLES if t not in have]
    if missing:
        raise RuntimeError(f"库里缺表：{missing}")


# ------------------------------------------------- 判据源（单源，不复刻）
def load_knowledge_query():
    """导入 `app.knowledge_query` 取判据。

    导不进就报错退出（返回码 2），**不**在本脚本里另写一套 status/observation
    合格集兜底——那正是派工禁止的「另写一套口径」。判据缺席时宁可不出报告，
    也不出一份口径来源不明的读数。

    返回的是**模块**而不是解包后的名字：判据一律走 `kq.<名>` 属性读取，
    这样上游改判据（含测试用 monkeypatch 换返回值）本盘查器会跟着变，
    不会在 import 期把值拷走。
    """
    try:
        from app import knowledge_query as kq
    except Exception as exc:                       # noqa: BLE001
        raise RuntimeError(
            f"判据源 app.knowledge_query 导不进：{type(exc).__name__}: {exc}") from exc
    for name in REUSED_CRITERIA:
        if not hasattr(kq, name):
            raise RuntimeError(f"判据源 app.knowledge_query 缺 {name}")
    return kq


def definition_lines(kq) -> dict:
    """复用符号在 `app/knowledge_query.py` 里的定义行号（实读，不写死）。"""
    try:
        src = Path(inspect_source_file(kq)).read_text(encoding="utf-8")
    except Exception:                              # noqa: BLE001
        return {name: None for name in REUSED_CRITERIA}
    out = {}
    for name in REUSED_CRITERIA:
        pat = re.compile(rf"^(?:def\s+{re.escape(name)}\b|{re.escape(name)}\s*[=:])")
        out[name] = next((i for i, ln in enumerate(src.splitlines(), 1)
                          if pat.search(ln)), None)
    return out


def inspect_source_file(mod):
    import inspect
    return inspect.getsourcefile(mod)


# ------------------------------------------------------------ 读数
def _required_flag(v) -> bool:
    """`required` 列按真值读（SQLite 侧存 0/1；NULL/缺值一律非 required）。"""
    return bool(v)


def bucket_of(kind, required) -> str:
    """条件行 → 计数桶（逐字对照 `_condition_pipeline` 的分派顺序）。"""
    if kind == "good_when":
        return "required" if _required_flag(required) else "good_when"
    if kind in ("bad_when", "neutral_when"):
        return kind
    return "other"


def read_strategies(con: sqlite3.Connection) -> list[tuple]:
    return list(con.execute(
        "SELECT id, strategy_key, version, status, observation_status, scope "
        "FROM expression_strategies_v2"))


def read_condition_agg(con: sqlite3.Connection) -> list[tuple]:
    """一次扫描拿到 (strategy_id, kind, required) → 行数 的全部分组。"""
    return list(con.execute(
        "SELECT strategy_id, kind, required, COUNT(*) FROM strategy_conditions "
        "GROUP BY strategy_id, kind, required"))


def read_total_condition_rows(con: sqlite3.Connection) -> int:
    return int(con.execute("SELECT COUNT(*) FROM strategy_conditions")
               .fetchone()[0])


def criteria_row_sort_key() -> list[str]:
    """写死的排序键（报告与测试共用一处定义，防两处漂移）。"""
    return ["strategy_key", "version", "strategy_id"]


def _sort_key(row: tuple):
    """写死的排序键：strategy_key → version → id；键与 id 都按 BINARY 逐码点比。"""
    sid, key, version = row[0], row[1] or "", row[2]
    try:
        ver_key = (0, int(version))
    except (TypeError, ValueError):               # 契约外 version 不猜口径
        ver_key = (1, str(version))
    return (key, ver_key, sid or "")


def strategy_row(row: tuple, counts: dict, kq) -> dict:
    sid, key, version, status, obs, scope = row
    required_rows = counts.get("required", 0)
    good_when_rows = counts.get("good_when", 0)
    bad_when_rows = counts.get("bad_when", 0)
    neutral_rows = counts.get("neutral_when", 0)
    other_rows = counts.get("other", 0)
    condition_rows = (required_rows + good_when_rows + bad_when_rows
                      + neutral_rows + other_rows)
    is_idle = condition_rows == 0
    status_ok = status in kq.eligible_statuses(version)
    obs_ok = obs in kq.ELIGIBLE_OBSERVATION
    return {
        "strategy_id": sid,
        "strategy_key": key,
        "version": version,
        "status": status,
        "observation_status": obs,
        "scope": scope,
        "status_eligible": bool(status_ok),
        "observation_eligible": bool(obs_ok),
        "eligible": bool(status_ok and obs_ok),
        "condition_rows": condition_rows,
        "required_rows": required_rows,
        "good_when_rows": good_when_rows,
        "bad_when_rows": bad_when_rows,
        "neutral_when_rows": neutral_rows,
        "other_kind_rows": other_rows,
        "good_when_rows_total": required_rows + good_when_rows,
        "is_idle": bool(is_idle),
        # 可达上限 = 相应行数（分量只由这些行抬起）
        "required_matches_max": required_rows,
        "good_when_matches_max": good_when_rows,
        # 现状：空转恒 0；有行时非静态读数 ⇒ null（不假造分量）
        "required_matches_now": 0 if is_idle else None,
        "good_when_matches_now": 0 if is_idle else None,
    }


def build_report(db_path: Path, now: datetime.datetime | None = None,
                 db_path_source: str = "--db") -> dict:
    kq = load_knowledge_query()
    now = now or datetime.datetime.now(datetime.timezone.utc)
    db_path = Path(db_path)
    con = ro_connect(db_path)
    try:
        require_tables(con)
        strat_rows = read_strategies(con)
        agg_rows = read_condition_agg(con)
        total_rows = read_total_condition_rows(con)
    finally:
        con.close()

    strat_rows.sort(key=_sort_key)
    known = {r[0] for r in strat_rows}
    per: dict = {}
    orphan: dict = {}
    for sid, kind, required, n in agg_rows:
        n = int(n)
        if sid not in known:
            orphan[sid] = orphan.get(sid, 0) + n
            continue
        bucket = per.setdefault(sid, {})
        b = bucket_of(kind, required)
        bucket[b] = bucket.get(b, 0) + n

    rows = [strategy_row(r, per.get(r[0], {}), kq) for r in strat_rows]
    versions = sorted({str(r[2]) for r in strat_rows})
    idle = [r for r in rows if r["is_idle"]]
    with_rows = [r for r in rows if not r["is_idle"]]
    req_total = sum(r["required_rows"] for r in rows)
    gw_total = sum(r["good_when_rows"] for r in rows)
    bw_total = sum(r["bad_when_rows"] for r in rows)
    nw_total = sum(r["neutral_when_rows"] for r in rows)
    other_total = sum(r["other_kind_rows"] for r in rows)

    def _component(max_reachable: int) -> dict:
        return {
            "max_reachable": max_reachable,
            "current": 0 if not with_rows else None,
            "idle_strategies_known_zero": len(idle),
            "query_dependent_strategies": len(with_rows),
            "current_note": CURRENT_NOTE,
        }

    return {
        "tool": TOOL,
        "schema": SCHEMA,
        "task": TASK,
        "readonly": True,
        "open_mode": "sqlite uri mode=ro",
        "db_path": str(db_path),
        "db_path_source": db_path_source,
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "reuse": {
            "source": "app/knowledge_query.py",
            "note": ("合格集判据与条件分桶口径均取自该模块，本脚本不另写一套；"
                     "行号在运行时实读"),
            "symbols": [
                {"name": n, "role": REUSED_CRITERIA[n],
                 "definition_line": definition_lines(kq).get(n)}
                for n in REUSED_CRITERIA
            ],
        },
        "criteria": {
            "eligible_status_sets": {v: sorted(kq.eligible_statuses(v))
                                     for v in versions},
            "eligible_observation": sorted(kq.ELIGIBLE_OBSERVATION),
            "required_rows_counts": "kind='good_when' AND required",
            "good_when_rows_counts": "kind='good_when' AND NOT required",
            "bad_when_rows_counts": "kind='bad_when'",
            "neutral_when_rows_counts": "kind='neutral_when'",
            "neutral_when_counts_toward": "neither（_condition_pipeline 显式 "
                                          "continue，neutral_when 不计正支持）",
            "max_reachable_semantics": "各分量在「全部谓词皆 true」那次调用里"
                                       "的取值上界 = 相应行数",
            "current_semantics": CURRENT_NOTE,
            "row_sort_key": criteria_row_sort_key(),
            "row_sort_order": "BINARY",
        },
        "strategies": rows,
        "summary": {
            "n_strategies": len(rows),
            "n_idle": len(idle),
            "n_with_rows": len(with_rows),
            "total_condition_rows": total_rows,
            "condition_rows_attributed": sum(r["condition_rows"] for r in rows),
            "orphan_condition_rows": sum(orphan.values()),
            "orphan_condition_strategy_ids": sorted(k for k in orphan if k),
            "n_eligible": sum(1 for r in rows if r["eligible"]),
            "n_eligible_idle": sum(1 for r in rows
                                   if r["eligible"] and r["is_idle"]),
            "kind_rows": {
                "good_when_required": req_total,
                "good_when_optional": gw_total,
                "bad_when": bw_total,
                "neutral_when": nw_total,
                "other": other_total,
            },
            "required_matches": _component(req_total),
            "good_when_matches": _component(gw_total),
            # 拒绝理由「可不可达」= 有没有相应行的行可走；有无行可走
            # 与「条件都满足」是**两件事**，这里分列。
            "rejection_reasons_reachable": {
                "excluded_bad_when": bw_total > 0,
                "excluded_required_false": req_total > 0,
                "excluded_required_unknown": req_total > 0,
                "note": ("可达=存在相应行的条件；实际是否触发取决于当次 "
                         "semantic_requirements，本盘查器不跑查询"),
            },
        },
    }


# ------------------------------------------------------------ 输出
def summary_line(summary: dict) -> str:
    return (f"n_strategies={summary['n_strategies']} / "
            f"n_idle={summary['n_idle']} / "
            f"n_with_rows={summary['n_with_rows']} / "
            f"total_condition_rows={summary['total_condition_rows']} / "
            f"n_eligible={summary['n_eligible']}")


def impact_line(summary: dict) -> str:
    req, gw = summary["required_matches"], summary["good_when_matches"]
    reach = summary["rejection_reasons_reachable"]
    return (f"required_matches: max_reachable={req['max_reachable']} "
            f"current={req['current']} | "
            f"good_when_matches: max_reachable={gw['max_reachable']} "
            f"current={gw['current']} | "
            f"excluded_bad_when 可达={reach['excluded_bad_when']} "
            f"excluded_required_* 可达="
            f"{reach['excluded_required_false']}")


def render(rep: dict) -> list[str]:
    s = rep["summary"]
    lines = [f"[{TOOL}] db={rep['db_path']} 来源={rep['db_path_source']} "
             f"({rep['open_mode']}, 只读零写入)",
             "  复用判据（单源，不另写一套）："]
    for sym in rep["reuse"]["symbols"]:
        lines.append(f"    - {rep['reuse']['source']}::{sym['name']} "
                     f"行 {sym['definition_line']}（{sym['role']}）")
    lines.append(f"  合格 status 集={rep['criteria']['eligible_status_sets']} "
                 f"合格 observation={rep['criteria']['eligible_observation']}")
    for r in rep["strategies"]:
        lines.append(
            f"  - {r['strategy_key']} v{r['version']} id={r['strategy_id']} "
            f"status={r['status']} observation={r['observation_status']} "
            f"合格={r['eligible']} "
            f"condition_rows={r['condition_rows']} "
            f"(required={r['required_rows']} good_when={r['good_when_rows']} "
            f"bad_when={r['bad_when_rows']} "
            f"neutral_when={r['neutral_when_rows']} "
            f"other={r['other_kind_rows']}) "
            f"is_idle={r['is_idle']}")
    if s["orphan_condition_rows"]:
        lines.append(f"  ! 孤儿条件行（strategy_id 不在 v2 表内）"
                     f"{s['orphan_condition_rows']} 行："
                     f"{s['orphan_condition_strategy_ids']}")
    lines.append(f"  {summary_line(s)} n_eligible_idle={s['n_eligible_idle']} "
                 f"orphan_condition_rows={s['orphan_condition_rows']}")
    lines.append(f"  {impact_line(s)}")
    if s["n_idle"] == s["n_strategies"]:
        lines.append("  判读：条件面无数据 ⇒ 条件门对全部策略空转（分量恒 0）。"
                     "这**不等于**「条件都满足」，别把空集读成通过。")
    return lines


def dump_json(rep: dict) -> str:
    """落盘文本：UTF-8 / ensure_ascii=False / 末尾换行；两次运行逐字一致。"""
    return json.dumps(rep, ensure_ascii=False, indent=1) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="条件门空转盘查（只读；不写库、不改任何数据）")
    ap.add_argument("--db", default=None,
                    help=f"sqlite 库路径（缺省 {default_db()}）")
    ap.add_argument("--out", default="", help="把完整报告写成 JSON 文件")
    a = ap.parse_args(argv)
    db_path = Path(a.db) if a.db else default_db()
    source = db_source(db_path) if a.db is None else "--db"
    if not db_path.exists():
        print(f"[{TOOL}] 库不存在：{db_path}", file=sys.stderr)
        return 2
    try:
        rep = build_report(db_path, db_path_source=source)
    except Exception as exc:                      # noqa: BLE001
        # 跑不起来（mode=ro 打不开 / 缺表 / 判据源导不进）一律返回码 2，
        # **不**把「没跑成」误报成「条件面干净」。
        print(f"[{TOOL}] 盘查未完成：{type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 2
    for line in render(rep):
        print(line, flush=True)
    if a.out:
        try:
            Path(a.out).write_text(dump_json(rep), encoding="utf-8")
        except OSError as exc:
            print(f"[{TOOL}] 报告写不出 {a.out}：{exc}", file=sys.stderr)
            return 2
        print(f"[{TOOL}] 报告已写出 {a.out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
