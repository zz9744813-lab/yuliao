"""大语料控制台统计只聚合所需值，不把全表实体载入 Python。"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import event

from app import console, corpus, db
from app.models import Segment, Work


def test_corpus_stats_and_integrity_keep_exact_semantics():
    db.init_db()
    with db.session() as s:
        old_console = console._corpus(s)
        before = old_console["integrity"]
        old_stats = corpus.segment_stats(s)
        w = Work(title="stats-scale-test", source="test:stats-scale")
        s.add(w)
        s.flush()
        payloads = [
            '{"src_ok": true}', '{"src_ok": false}', '{"src_ok": "false"}',
            '{"src_ok": null}', '{"src_ok_unverified": true}',
            '{"src_ok_unverified": false}', r'{"\u0073rc_ok": true}',
            '{broken', '[{"src_ok":true}]', '{}',
            '{"src_ok_unverified": {"reason":"pending"}}',
        ]
        for i, payload in enumerate(payloads):
            s.add(Segment(work_id=w.id, ordinal=i, text="一句话。", n_chars=4,
                          n_sentences=1, integrity=payload))
        s.commit()

    statements = []

    def capture(_conn, _cursor, statement, _params, _ctx, _many):
        statements.append(statement.lower())

    event.listen(db.engine, "before_cursor_execute", capture)
    try:
        with db.session() as s:
            new_console = console._corpus(s)
            after = new_console["integrity"]
            stats = corpus.segment_stats(s)
    finally:
        event.remove(db.engine, "before_cursor_execute", capture)

    assert after["src_ok"] - before["src_ok"] == 2
    assert after["src_bad"] - before["src_bad"] == 1
    assert after["src_unverified"] - before["src_unverified"] == 4
    assert after["unchecked"] - before["unchecked"] == 4
    assert stats["n_segments"] - old_stats["n_segments"] == len(payloads)
    assert stats["chars_total"] - old_stats["chars_total"] == 4 * len(payloads)
    assert new_console["roles"]["∅"] - old_console["roles"].get("∅", 0) == len(payloads)
    assert (new_console["seg_versions"]["1"]
            - old_console["seg_versions"].get("1", 0) == len(payloads))
    assert any("select integrity from segments" in q and "where instr" in q
               for q in statements)
    assert any("sum(segments.n_chars)" in q and "sum(segments.n_sentences)" in q
               for q in statements), "首屏统计必须由数据库聚合"
    assert any("group by role, seg_version" in q
               for q in statements), "深度概览应合并分类扫描"
    assert sum("group by segments.work_id" in q for q in statements) == 1, (
        "最近作品段数应批量统计，不能每本书单独 COUNT")


def test_group_counts_distinguishes_null_from_empty_role():
    db.init_db()
    with db.session() as s:
        before = console._group_counts(s, Segment.role)
        w = Work(title="group-null-test", source="test:group-null")
        s.add(w)
        s.flush()
        for ordinal, role in enumerate((None, "")):
            s.add(Segment(work_id=w.id, ordinal=ordinal, role=role,
                          text="一句话。", n_chars=4, n_sentences=1))
        s.commit()
    with db.session() as s:
        after = console._group_counts(s, Segment.role)
    assert after["∅"] - before.get("∅", 0) == 1
    assert after[""] - before.get("", 0) == 1


def test_postgres_config_fails_before_any_connection(tmp_path):
    env = dict(os.environ, PYTHONIOENCODING="utf-8",
               LG_DATABASE_URL="postgresql+psycopg://invalid/unused",
               LG_DATA_DIR=str(tmp_path / "must-not-create"))
    root = Path(__file__).resolve().parent.parent
    p = subprocess.run([sys.executable, "-c", "from app import db"], cwd=root,
                       env=env, capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    assert p.returncode != 0
    assert "PostgreSQL 迁移和脚本适配尚未完成" in p.stderr
    assert not (tmp_path / "must-not-create").exists()
