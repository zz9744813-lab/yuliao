"""策略统计投影漂移对账器（**只读**，2026-09-27 派工）。

要回答的问题只有一个：库内 `strategy_stats` 投影行，与**当下**按
`scripts/strategy_stats_rebuild.py` 现算出来的值，差在哪。差哪些键、
哪些键值不等、快照是不是老得不能再当证据用——逐策略列明白。

只读承诺在**连接层**成立，不靠自觉：
- 投影行读取：`sqlite3.connect("file:...?mode=ro", uri=True)`；
- 现算值：调 `strategy_stats_rebuild.run(apply=False)`（dry-run 语义本身
  不插/不删/不改数据行），但在调用期间把 `app.db.SessionLocal` **临时
  改绑**到同一库的 mode=ro 只读 engine 上，`finally` 还原。绑回去的不是
  按 `LG_DATABASE_URL` 建的全局**可写** engine——否则 `app.db` 的
  `PRAGMA journal_mode=WAL` 连接钩子会真动库文件（对旧 schema 库即写）。

本脚本**不写库、不自动 rebuild**：重建是主控决策，对账器只报差。
`run(apply=False)` 的 docstring 已声明「零数据写 ≠ 零 DDL」——本脚本
全程不调 `db.init_db()`，也不 `create_all`，故连 DDL 也没有。

用法：
    python scripts/stats_projection_drift.py [--db PATH] [--out report.json]
                                            [--max-age-days N]

退出码：0 = 对账完成（**有没有漂移都是 0**，漂移是读数不是故障）；
2 = 跑不起来（库不存在 / mode=ro 打不开 / 表缺失 / 现算失败）。
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT, ROOT / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

# 真库缺省路径（主检出）；--db 覆盖。
DEFAULT_DB = Path("F:/agi/language-genome/data/language_genome.db")

REBUILD_SCRIPT = ROOT / "scripts" / "strategy_stats_rebuild.py"

# CANONICAL_EXTRAS_KEYS 的兜底副本：只在建脚本读不到（异常路径）时用；
# 正常情况下**一律以重建器里的集合为单一真值**，防两处口径漂移。
FALLBACK_EXTRAS_KEYS = ("by_root_work", "usable_evidence", "benchmark_stripped",
                        "k3_eligible_instances", "k3_eligible_root_works")


def load_rebuild():
    """按文件路径加载重建器（scripts/ 不是包，与既有测试同一加载方式）。"""
    spec = importlib.util.spec_from_file_location("spdr_ssr", REBUILD_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载重建器：{REBUILD_SCRIPT}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def canonical_extras_keys(ssr) -> tuple[str, ...]:
    keys = getattr(ssr, "CANONICAL_EXTRAS_KEYS", None)
    src = sorted(keys) if keys else sorted(FALLBACK_EXTRAS_KEYS)
    return tuple(src)


# ------------------------------------------------------------ 只读连接层
def ro_connect(db_path: Path):
    """mode=ro 只读连接：任何写操作在 SQLite 层直接抛异常。"""
    return sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro",
                           uri=True, check_same_thread=False)


@contextlib.contextmanager
def readonly_db_bind(db_path: Path):
    """把 app.db.SessionLocal **临时**改绑到本库的 mode=ro engine，退出还原。

    存在的理由：`ssr.run(apply=False)` 内部用的是 `db.session()`，而
    `app.db.engine` 在 import 期就按 `LG_DATABASE_URL` 绑成了可写连接；
    不改绑则「对账 A 库」实际算的是 B 库，且可写连接的 WAL 钩子会动文件。
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app import db as app_db

    engine = create_engine("sqlite://", future=True,
                           creator=lambda: ro_connect(db_path))
    original = app_db.SessionLocal
    app_db.SessionLocal = sessionmaker(bind=engine, autoflush=False,
                                       expire_on_commit=False)
    try:
        yield app_db.SessionLocal
    finally:
        app_db.SessionLocal = original
        # 释放只读连接（池里留着的空闲句柄不该越过本次对账的生命周期）
        engine.dispose()


# ------------------------------------------------------------ 时间/快照
def parse_snapshot(raw) -> datetime.datetime | None:
    """投影 snapshot_at 字符串 → aware UTC datetime；解析不了返回 None。

    写入方格式为 `%Y-%m-%dT%H:%M:%SZ`（UTC、无偏移）。不带偏移的历史写法
    一律按 UTC 解释（宁可按 UTC 判超龄，不把「读不懂」判成「没超龄」）。
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc)


def snapshot_age_days(raw, now: datetime.datetime) -> float | None:
    dt = parse_snapshot(raw)
    return None if dt is None else (now - dt).total_seconds() / 86400.0


# ------------------------------------------------------------ 读数
def read_projection(db_path: Path) -> list[dict]:
    """库内 strategy_stats 现值：snapshot_at + extras 键集/取值 + valid。"""
    con = ro_connect(db_path)
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(strategy_stats)")}
        if not cols:
            raise RuntimeError("库里没有 strategy_stats 表")
        need = {"strategy_id", "snapshot_at", "extras"}
        missing = need - cols
        if missing:
            raise RuntimeError(f"strategy_stats 缺列：{sorted(missing)}")
        keys = {r[0]: r[1] for r in con.execute(
            "SELECT id, strategy_key FROM expression_strategies_v2")}
        out = []
        for row in con.execute(
                "SELECT strategy_id, strategy_version, snapshot_at, valid, "
                "extras FROM strategy_stats ORDER BY strategy_id, "
                "strategy_version"):
            raw_extras = row[4]
            if isinstance(raw_extras, (dict, list)) or raw_extras is None:
                extras = raw_extras or {}
            else:
                try:
                    extras = json.loads(raw_extras)
                except (TypeError, ValueError):
                    extras = {}
                if not isinstance(extras, dict):
                    extras = {}
            out.append({
                "strategy_id": row[0],
                "strategy_version": row[1],
                "strategy_key": keys.get(row[0], "<无对应策略行>"),
                "snapshot_at": row[2],
                "valid": row[3],
                "extras": extras,
            })
        return out
    finally:
        con.close()


def recompute(db_path: Path, ssr) -> dict[str, dict]:
    """现算值：在 mode=ro 改绑下跑 `ssr.run(apply=False)`（零数据写）。"""
    with readonly_db_bind(db_path):
        rep = ssr.run(apply=False)
    if rep.get("mode") != "dry_run":
        raise RuntimeError(f"重建器未以 dry-run 返回：mode={rep.get('mode')}")
    return {r["strategy_id"]: r for r in rep["rows"]}


# ------------------------------------------------------------ 对账
def diff_rows(projection: list[dict], computed: dict[str, dict],
              expected_keys: tuple[str, ...], max_age_days: float,
              now: datetime.datetime) -> list[dict]:
    rows = []
    for p in projection:
        extras = p["extras"] or {}
        c = computed.get(p["strategy_id"])
        missing = [k for k in expected_keys if k not in extras]
        mismatch: dict[str, dict] = {}
        if c is not None:
            for k in expected_keys:
                if k in extras and k in c and extras[k] != c[k]:
                    mismatch[k] = {"projection": extras[k], "recomputed": c[k]}
        age = snapshot_age_days(p["snapshot_at"], now)
        stale = None if age is None else age > max_age_days
        valid_mismatch = (c is not None and p["valid"] != c["valid"])
        rows.append({
            "strategy_id": p["strategy_id"],
            "strategy_key": p["strategy_key"],
            "strategy_version": p["strategy_version"],
            "snapshot_at": p["snapshot_at"],
            "snapshot_age_days": None if age is None else round(age, 3),
            "stale": stale,
            "valid_projection": p["valid"],
            "valid_recomputed": None if c is None else c["valid"],
            "extras_keys": sorted(extras),
            "missing_keys": missing,
            "value_mismatch": mismatch,
            "foreign_extras_keys": sorted(set(extras) - set(expected_keys)),
            "valid_mismatch": valid_mismatch,
            "drift": bool(missing or mismatch or valid_mismatch),
        })
    return rows


def build_report(db_path: Path, max_age_days: float, ssr=None,
                 now: datetime.datetime | None = None) -> dict:
    ssr = ssr or load_rebuild()
    expected = canonical_extras_keys(ssr)
    now = now or datetime.datetime.now(datetime.timezone.utc)
    projection = read_projection(db_path)
    computed = recompute(db_path, ssr)
    rows = diff_rows(projection, computed, expected, max_age_days, now)
    projected_ids = {r["strategy_id"] for r in rows}
    return {
        "tool": "stats_projection_drift",
        "readonly": True,
        "open_mode": "sqlite uri mode=ro",
        "db_path": str(db_path),
        "expected_extras_keys": list(expected),
        "max_age_days": max_age_days,
        "checked_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "rows": rows,
        "orphan_strategy_ids": sorted(
            projected_ids - set(computed)),          # 有投影、策略已不在
        "unprojected_strategy_ids": sorted(
            set(computed) - projected_ids),           # 现算有、库里没投影行
        "summary": {
            "total": len(rows),
            "drift_rows": sum(1 for r in rows if r["drift"]),
            "missing_key_rows": sum(1 for r in rows if r["missing_keys"]),
            "value_mismatch_rows": sum(1 for r in rows if r["value_mismatch"]),
            "stale_rows": sum(1 for r in rows if r["stale"]),
            "snapshot_unparseable_rows": sum(
                1 for r in rows if r["stale"] is None),
        },
    }


def summary_line(summary: dict) -> str:
    return (f"drift_rows={summary['drift_rows']} / "
            f"missing_key_rows={summary['missing_key_rows']} / "
            f"total={summary['total']}")


def render(rep: dict) -> list[str]:
    lines = [f"[stats_projection_drift] db={rep['db_path']} "
             f"({rep['open_mode']}, 只读零写入)",
             f"  期望 extras 键={rep['expected_extras_keys']}",
             f"  max_age_days={rep['max_age_days']}"]
    for r in rep["rows"]:
        tag = "漂移" if r["drift"] else "一致"
        lines.append(f"  - {r['strategy_key']} snapshot_at={r['snapshot_at']} "
                     f"age_days={r['snapshot_age_days']} stale={r['stale']} "
                     f"valid={r['valid_projection']}|{r['valid_recomputed']} "
                     f"=> {tag}")
        if r["missing_keys"]:
            lines.append(f"      缺失键={r['missing_keys']}")
        for k, v in r["value_mismatch"].items():
            lines.append(f"      值不一致 {k}: 旧投影={v['projection']!r} "
                         f"现算={v['recomputed']!r}")
        if r["foreign_extras_keys"]:
            lines.append(f"      外来键={r['foreign_extras_keys']}")
    for sid in rep["orphan_strategy_ids"]:
        lines.append(f"  ! 孤儿投影行（策略已不在）strategy_id={sid}")
    for sid in rep["unprojected_strategy_ids"]:
        lines.append(f"  ! 未投影策略（现算有、库里无行）strategy_id={sid}")
    lines.append(f"  {summary_line(rep['summary'])} "
                 f"stale_rows={rep['summary']['stale_rows']} "
                 f"value_mismatch_rows={rep['summary']['value_mismatch_rows']}")
    return lines


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="strategy_stats 投影漂移对账（只读；不写库、不自动 rebuild）")
    ap.add_argument("--db", default=str(DEFAULT_DB),
                    help=f"sqlite 库路径（缺省 {DEFAULT_DB}）")
    ap.add_argument("--out", default="", help="把完整报告写成 JSON 文件")
    ap.add_argument("--max-age-days", type=float, default=7.0,
                    help="snapshot_at 超龄阈值（天），默认 7")
    a = ap.parse_args(argv)
    db_path = Path(a.db)
    if not db_path.exists():
        print(f"[stats_projection_drift] 库不存在：{db_path}", file=sys.stderr)
        return 2
    try:
        rep = build_report(db_path, a.max_age_days)
    except Exception as exc:                  # noqa: BLE001
        # 跑不起来（库损坏 / 表缺失 / mode=ro 被拒 / 现算异常）一律退出码 2
        # 带异常类型名，**不**把「没跑成」误报成「没漂移」。
        print(f"[stats_projection_drift] 对账未完成："
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    for line in render(rep):
        print(line, flush=True)
    if a.out:
        Path(a.out).write_text(json.dumps(rep, ensure_ascii=False, indent=1),
                               encoding="utf-8")
        print(f"[stats_projection_drift] 报告已写出 {a.out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
