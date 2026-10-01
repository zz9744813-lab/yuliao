# -*- coding: utf-8 -*-
"""segments 表列级瘦身迁移器（离线、只动 text_clean / integrity / cleaned 三列）。

真账：主库 language_genome.db ~14.9 MB/本，冗余主体是 `text_clean` 那份与 `text`
94–99% 逐字相同的拷贝（约占行负载 33%），以及仍是 183+ 字节 JSON 形态的 2–3% integrity 行。

铁律（见 docs/SEGMENTS_SLIM.md 的逐条口径）：
  * **只允许动 `text_clean` / `integrity` / `cleaned`**；`text` / `id` / `ordinal` /
    `n_chars` / `created_at` 一行不改。
  * `text_clean` **只在 `text_clean == text` 时**归 NULL ⇒ 读侧
    `text_clean or text`（app/api.py:907、scripts/register_work_sources._work_sha256
    内容锚）与 `text_clean if text_clean is not None else text`
    （app/semantic_review.py:182/193/262）**两种口径同时不变**：被 NULL 的行其
    text_clean 本就等于 text，两种回退都落回 text；`text_clean != text`（真清洗结果、
    含空串清洗）的行**原样保留**。
  * integrity 只改**形态**：用 app.segment_integrity.pack_raw 把仍是基键 JSON 的行压成
    紧凑串；带附加键 / 取值越界 / 非 JSON 的行 pack_raw 原样返回 ⇒ `loads_any` 前后等价。

默认拒绝对活库动手：路径解析后等于 LIVE_DB_PATHS 任一，直接退出码 2，只有 `--force-live` 才继续。

用法（小副本）：
    python scripts/slim_segments.py --db <副本> --dry-run
    python scripts/slim_segments.py --db <副本> --vacuum
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import segment_integrity as si  # noqa: E402

# 活库真路径（大小写/斜杠归一后比对）——见任务 0 节的「真账」。
LIVE_DB_PATHS = (
    "D:/language-genome-data/language_genome.db",
    "F:/agi/language-genome/data/language_genome.db",
)

# 全库折算基准（任务 0 节：segments=105,283,111 行）。
FULL_DB_ROWS = 105_283_111


def _norm(path: str | os.PathLike) -> str:
    """归一比对键：realpath（解析符号链接 / junction / 8.3 短路径 / subst 盘）
    + normpath + normcase（Windows 折叠大小写）+ 正斜杠 + 小写。

    必须走 realpath 而非 abspath：abspath 不解析重解析点，符号链接/junction 指向
    活库文件时会绕过守卫直接写活库（会审 BLOCK 项）。"""
    p = os.path.normcase(os.path.normpath(os.path.realpath(str(path))))
    return p.replace("\\", "/").lower()


def is_live_db(path: str | os.PathLike) -> bool:
    """解析后的路径是否命中任一活库（归一化：realpath + 正斜杠 + 小写）。"""
    n = _norm(path)
    return n in {_norm(x) for x in LIVE_DB_PATHS}


def _col_exists(cur: sqlite3.Cursor, table: str, col: str) -> bool:
    cur.execute(f"PRAGMA table_info({table})")
    return col in {r[1] for r in cur.fetchall()}


def _scope_hi(cur: sqlite3.Cursor, limit: int | None) -> int | None:
    """--limit N 的处理上界：按 rowid 升序取前 N 行的最大 rowid；None=全库。"""
    if limit is None:
        return None
    row = cur.execute(
        "SELECT MAX(rowid) FROM (SELECT rowid FROM segments ORDER BY rowid LIMIT ?)",
        (limit,)).fetchone()
    return row[0] if row and row[0] is not None else None


def _scope_clause(hi: int | None) -> tuple[str, list]:
    return (" AND rowid <= ?" if hi is not None else ""), ([hi] if hi is not None else [])


def _iter_batches(upper: int, start_rowid: int, batch: int):
    """按 rowid 区间 [start_rowid..upper] 切批（upper 为硬上界，永不为 None ⇒ 必然收敛）。"""
    start = start_rowid
    while start <= upper:
        end = start + batch - 1
        if end > upper:
            end = upper
        yield start, end
        start = end + 1


def run(db: str, *, dry_run: bool = False, limit: int | None = None,
        vacuum: bool = False, batch: int = 20_000, force_live: bool = False) -> int:
    # 入参闸门（会审 BLOCK：--limit 0/负数会被 SQLite 当作全库或不限，--batch 0/负数
    # 会让 _iter_batches 死循环）——范围只收紧不放宽：limit/batch 必须 >= 1。
    if limit is not None and limit < 1:
        print(f"[slim] --limit={limit} 非法：必须 >= 1。"
              f"（LIMIT 0 ⇒ 子查询空 ⇒ MAX(rowid)=NULL ⇒ 会被误当作全库执行，已拒绝。）",
              file=sys.stderr)
        return 2
    if batch < 1:
        print(f"[slim] --batch={batch} 非法：必须 >= 1。", file=sys.stderr)
        return 2
    db_path = Path(db)
    if is_live_db(db_path) and not force_live:
        print(f"[slim] 活库需 --force-live：{db} 判定为活库路径，"
              f"默认拒绝对活库执行任何写操作（含 VACUUM）。", file=sys.stderr)
        return 2
    if not db_path.exists():
        print(f"[slim] 库不存在：{db}", file=sys.stderr)
        return 2

    size0 = db_path.stat().st_size
    con = sqlite3.connect(str(db_path))
    # 本轮只改 text_clean/integrity/cleaned 三列的列值，不新增/删除行、不动主键/外键，
    # 也不重建表，故无需 PRAGMA foreign_keys=OFF（会审建议：关掉反而少一道兜底，已移除）。
    cur = con.cursor()
    total_rows = cur.execute("SELECT COUNT(*) FROM segments").fetchone()[0]
    hi = _scope_hi(cur, limit)
    scope_sql, scope_params = _scope_clause(hi)  # 参数绑定，不再 f-string 拼 hi
    print(f"[slim] db={db}")
    print(f"[slim] segments 总行数={total_rows}；本轮处理范围 rowid<={hi or '∞'} "
          f"({'--limit ' + str(limit) if limit is not None else '全库'})；"
          f"文件大小={size0:,} B；bytes/row={(size0 / total_rows) if total_rows else 0:.1f}")

    # ── a. 补 cleaned 列 + 回填「已清洗」口径（非 NULL 即已清洗，含空串）──
    # 注意：cleaned 不进 ORM（app/models.py 未改），故**增量新行一律 cleaned=0**；
    # 读侧口径见 app/console.py::_cleaned_expr，用「cleaned=1 或 text_clean 非 NULL」
    # 的等价复合口径兜住增量与部分迁移（会审严重项）。本步只保证全量迁移后
    # cleaned=1 集合 == 迁移前 text_clean 非 NULL 集合。
    t = time.perf_counter()
    if not _col_exists(cur, "segments", "cleaned"):
        if dry_run:
            print("[slim][a] DRY-RUN：将 ADD COLUMN cleaned INTEGER NOT NULL DEFAULT 0")
        else:
            cur.execute("ALTER TABLE segments ADD COLUMN cleaned INTEGER NOT NULL DEFAULT 0")
            print("[slim][a] 已补列 cleaned INTEGER NOT NULL DEFAULT 0")
    pre_cleaned = cur.execute(
        f"SELECT COUNT(*) FROM segments WHERE text_clean IS NOT NULL{scope_sql}",
        scope_params).fetchone()[0]
    if dry_run:
        print(f"[slim][a] DRY-RUN：将 UPDATE cleaned=1，命中 {pre_cleaned} 行"
              f"（口径同 console「非 NULL 即已清洗」）")
    else:
        cur.execute(f"UPDATE segments SET cleaned=1 WHERE text_clean IS NOT NULL{scope_sql}",
                    scope_params)
        print(f"[slim][a] cleaned=1 回填：写 {cur.rowcount} 行 "
              f"（范围内 text_clean 非 NULL 计 {pre_cleaned}）"
              f"，耗时 {time.perf_counter() - t:.2f}s")

    # text_clean 归 NULL 预估（一次廉价聚合，dry-run 与正式跑都打印）
    t = time.perf_counter()
    tc_null_rows = cur.execute(
        f"SELECT COUNT(*), COALESCE(SUM(LENGTH(CAST(text_clean AS BLOB))),0) "
        f"FROM segments WHERE text_clean IS NOT NULL AND text_clean = text{scope_sql}",
        scope_params).fetchone()
    n_null, bytes_clean = tc_null_rows[0], tc_null_rows[1]

    # 分批区间：upper 为硬上界（hi 或全库最大 rowid），永不为 None ⇒ _iter_batches 必然收敛。
    min_rowid = cur.execute("SELECT MIN(rowid) FROM segments").fetchone()[0]
    max_rowid = cur.execute("SELECT MAX(rowid) FROM segments").fetchone()[0]
    start_rid = min_rowid if min_rowid is not None else 1
    upper = hi if hi is not None else (max_rowid if max_rowid is not None else 0)
    batches = list(_iter_batches(upper, start_rid, batch)) if upper >= start_rid else []

    def _compact_integrity(b0: int, b1: int):
        """流式处理单个 rowid 区间的 integrity：只在批内 fetch，逐行算 pack_raw 压缩差。
        返回 (executemany 的 updates, 命中行数, 节省字节)。会审 BLOCK 修正：不再一次性
        fetchall 全表、不再对 int_updates 逐批线性过滤 ⇒ 常驻内存 O(batch)、总复杂度 O(行)。"""
        rows = cur.execute(
            "SELECT rowid, integrity FROM segments "
            "WHERE integrity IS NOT NULL AND integrity NOT LIKE 'i1:%' AND rowid BETWEEN ? AND ?",
            (b0, b1)).fetchall()
        updates: list[tuple[str, int]] = []
        cnt = saved = 0
        for rid, raw in rows:
            packed = si.pack_raw(raw)
            if packed is not None and packed != raw:
                updates.append((packed, rid))
                saved += len(str(raw).encode("utf-8")) - len(packed.encode("utf-8"))
                cnt += 1
        return updates, cnt, saved

    if dry_run:
        n_int = bytes_int_saved = 0
        for b0, b1 in batches:
            _, c, sv = _compact_integrity(b0, b1)
            n_int += c
            bytes_int_saved += sv
        est_saved = int(bytes_clean) + bytes_int_saved
        print(f"[slim][预] 将归 NULL 的等值 text_clean：{n_null} 行 / {int(bytes_clean):,} B；"
              f"将紧凑化的 JSON integrity：{n_int} 行 / {bytes_int_saved:,} B；"
              f"合计预估节省 {est_saved:,} B，耗时 {time.perf_counter() - t:.2f}s")
        print("[slim][b/c/d] DRY-RUN：零写入，跳过实际 UPDATE / VACUUM")
        _report_projection(size0, size0, total_rows, est_saved, dry=True)
        con.close()
        return 0

    # ── b/c 合并单趟分批循环：每批归 NULL text_clean + 流式紧凑 integrity，逐批 commit ──
    print(f"[slim][预] 将归 NULL 的等值 text_clean：{n_null} 行 / {int(bytes_clean):,} B；"
          f"integrity 逐批流式紧凑化（见每批 [b/c] 行）")
    done_rows = ci = bytes_int_saved = 0
    for (b0, b1) in batches:
        tb = time.perf_counter()
        cur.execute(
            "UPDATE segments SET text_clean=NULL "
            "WHERE text_clean IS NOT NULL AND text_clean = text AND rowid BETWEEN ? AND ?",
            (b0, b1))
        changed = cur.rowcount
        done_rows += changed
        updates, c, sv = _compact_integrity(b0, b1)
        if updates:
            cur.executemany("UPDATE segments SET integrity=? WHERE rowid=?", updates)
        ci += c
        bytes_int_saved += sv
        con.commit()
        print(f"[slim][b/c] rowid [{b0}..{b1}]：归 NULL {changed} 行（累计 {done_rows}）；"
              f"紧凑化 {c} 行（累计 {ci}），耗时 {time.perf_counter() - tb:.2f}s")
    est_saved = int(bytes_clean) + bytes_int_saved

    # 校验：范围内不再有「等值却未归 NULL」的 text_clean（硬不变量）。
    # cleaned 计数与「迁移前 text_clean 非 NULL」的相等关系是**单次全新迁移**的性质，
    # 由 tests/test_slim_segments.py 在全新副本上钉死；重跑时 cleaned=1 会保留历史
    # （等值行上一轮已归 NULL），故此处只作透明打印、不作失败门槛。
    cleaned_cnt = cur.execute(
        f"SELECT COUNT(*) FROM segments WHERE cleaned=1{scope_sql}", scope_params).fetchone()[0]
    left_equal = cur.execute(
        f"SELECT COUNT(*) FROM segments WHERE text_clean IS NOT NULL AND text_clean = text"
        f"{scope_sql}", scope_params).fetchone()[0]
    print(f"[slim][核] 范围内 cleaned=1 计数={cleaned_cnt}（本轮迁移前 text_clean 非 NULL={pre_cleaned}，"
          f"重跑时 cleaned 保留历史故可 ≥ 该值）；残留等值 text_clean={left_equal}（应=0）；"
          f"合计预估节省 {est_saved:,} B，耗时 {time.perf_counter() - t:.2f}s")
    if left_equal != 0:
        print("[slim][!] 迁移后仍有等值 text_clean 未归 NULL——不执行 VACUUM。\n"
              "        ⚠ 半途而废态：此前各批 UPDATE 已逐批 commit，不可自动回滚；"
              "已提交批次必须按 docs/SEGMENTS_SLIM.md §4「回滚」节手工处理后再重跑。",
              file=sys.stderr)
        con.close()
        return 1

    # ── d. VACUUM（默认关：耗时且需 2 倍空间）──
    if vacuum:
        tb = time.perf_counter()
        con.commit()
        con.isolation_level = None            # VACUUM 不能在事务内执行
        con.execute("VACUUM")
        print(f"[slim][d] VACUUM 完成，耗时 {time.perf_counter() - tb:.2f}s")

    con.close()
    size1 = db_path.stat().st_size
    _report_projection(size0, size1, total_rows, est_saved, dry=False)
    return 0


def _report_projection(size0: int, size1: int, rows: int, est_saved: int, *, dry: bool) -> None:
    bpr0 = size0 / rows if rows else 0.0
    bpr1 = size1 / rows if rows else 0.0
    print("─" * 68)
    print(f"[slim][结果]{'（DRY-RUN 预估，未落盘）' if dry else ''}")
    print(f"  文件大小：前 {size0:,} B（{size0 / 2**20:.1f} MiB）"
          f" → 后 {size1:,} B（{size1 / 2**20:.1f} MiB）"
          f"，实际差 {size0 - size1:,} B"
          f"{'；未 VACUUM，物理文件可不缩，字节账看下面预估' if not dry and size1 >= size0 else ''}")
    print(f"  bytes/row：前 {bpr0:.1f} → 后 {bpr1:.1f}（本机范围实测）")
    saved_est = est_saved
    print(f"  逻辑节省字节（text_clean+integrity 载荷）：{saved_est:,} B "
          f"⇒ 折算 bytes/row 净减 {saved_est / rows if rows else 0:.1f} B")
    est_full0 = bpr0 * FULL_DB_ROWS
    est_full_after = (bpr0 - (saved_est / rows if rows else 0)) * FULL_DB_ROWS
    print(f"  按 {FULL_DB_ROWS:,} 行折算全库预估："
          f"前 {est_full0 / 2**30:.2f} GiB → 瘦身后约 {est_full_after / 2**30:.2f} GiB"
          f"（不含 VACUUM 回收的页内碎片）")
    print("─" * 68)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="segments 列级瘦身迁移（离线，只动 text_clean/integrity/cleaned）")
    ap.add_argument("--db", required=True, help="SQLite 库路径（必填）")
    ap.add_argument("--force-live", action="store_true",
                    help="显式放行活库路径；否则命中活库直接退出码 2")
    ap.add_argument("--dry-run", action="store_true", help="只统计将要改多少行/省多少字节，零写入")
    ap.add_argument("--limit", type=int, default=None, help="只处理按 rowid 升序的前 N 行（小库验证）")
    ap.add_argument("--vacuum", action="store_true", help="迁移后 VACUUM（默认关：耗时且需 2 倍空间）")
    ap.add_argument("--batch", type=int, default=20_000, help="分批 rowid 区间大小（默认 20000）")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return run(args.db, dry_run=args.dry_run, limit=args.limit,
               vacuum=args.vacuum, batch=args.batch, force_live=args.force_live)


if __name__ == "__main__":
    raise SystemExit(main())
