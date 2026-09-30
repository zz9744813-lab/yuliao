# -*- coding: utf-8 -*-
"""segments.work_id 索引命名对齐真库（2026-09-30）。

真库现状是手工建的 `idx_segments_work_id`（578s / +1.57 GiB）；模型此前用
`index=True`，SQLAlchemy 默认名 `ix_segments_work_id` 与真库**不同名** ⇒
任何 `create_all` / autogenerate 对账会在 45 GB 库上多建一个 ~1.5 GiB 的
重复索引。本文件钉死三件事：
  1. 模型声明里 work_id 上**恰好一个**索引，名字 == idx_segments_work_id；
  2. create_all 出来的库里没有 ix_segments_work_id；
  3. 该索引仍能服务 `WHERE work_id = ?`（EQP 实测；空表优化器可能选别的
     计划 ⇒ 退化为断言索引存在且列序正确，不造假）。
"""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

from sqlalchemy import create_engine, inspect

from app.db import Base
from app.models import Segment

REAL_DB_INDEX_NAME = "idx_segments_work_id"
LEGACY_DEFAULT_NAME = "ix_segments_work_id"


def _work_id_indexes():
    return [ix for ix in Segment.__table__.indexes
            if any(c.name == "work_id" for c in ix.columns)]


def test_model_declares_exactly_one_named_index_on_work_id():
    ixs = _work_id_indexes()
    assert len(ixs) == 1, (
        f"work_id 上应恰好一个索引，实际: {[ix.name for ix in ixs]}")
    assert ixs[0].name == REAL_DB_INDEX_NAME
    assert [c.name for c in ixs[0].columns] == ["work_id"]


def _create_all_to_tmpdb() -> Path:
    db = Path(tempfile.mkdtemp(prefix="lg_idxname_")) / "align.db"
    engine = create_engine(f"sqlite:///{db.as_posix()}")
    Base.metadata.create_all(engine)
    engine.dispose()
    return db


def test_create_all_has_no_legacy_named_index():
    db = _create_all_to_tmpdb()
    with sqlite3.connect(db) as conn:
        names = {row[1] for row in conn.execute("PRAGMA index_list('segments')")}
    assert REAL_DB_INDEX_NAME in names
    assert LEGACY_DEFAULT_NAME not in names, (
        f"create_all 又生成了默认名 {LEGACY_DEFAULT_NAME}："
        f"会与真库 {REAL_DB_INDEX_NAME} 并存成重复索引")
    # ORM 元数据视角同口径复核（防 create_all 之外的读者）
    insp = inspect(create_engine(f"sqlite:///{db.as_posix()}"))
    idx_names = {ix["name"] for ix in insp.get_indexes("segments")}
    assert LEGACY_DEFAULT_NAME not in idx_names
    assert REAL_DB_INDEX_NAME in idx_names


def test_index_serves_work_id_equality_lookup():
    db = _create_all_to_tmpdb()
    with sqlite3.connect(db) as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        for i in range(200):
            conn.execute(
                "INSERT INTO segments (id, work_id, ordinal, text, n_sentences,"
                " n_chars, role, seg_version, created_at)"
                " VALUES (?, ?, ?, ?, 0, 0, NULL, 1, 'x')",
                (f"SEG{i:05d}", "WORK_A" if i % 2 else "WORK_B",
                 i, "正文"))
        conn.commit()
        plan = " ".join(
            row[3] for row in conn.execute(
                "EXPLAIN QUERY PLAN SELECT id FROM segments WHERE work_id = ?",
                ("WORK_A",)))
        if REAL_DB_INDEX_NAME in plan:
            assert "USING INDEX" in plan
        else:
            # 优化器对这份自建小样本选了别的计划：只核事实，不凑断言。
            cols = [row[2] for row in conn.execute(
                f"PRAGMA index_info('{REAL_DB_INDEX_NAME}')")]
            assert cols == ["work_id"], f"索引存在但列序异常: {cols}"
