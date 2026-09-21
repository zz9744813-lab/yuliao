"""SQLAlchemy 引擎与会话。SQLite（默认）/ PostgreSQL（LG_DATABASE_URL）双兼容。"""
from __future__ import annotations

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from . import config


class Base(DeclarativeBase):
    pass


def _make_engine():
    url = config.DATABASE_URL
    kwargs = {"future": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
    engine = create_engine(url, **kwargs)
    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _wal(dbapi_conn, _):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()
    return engine


engine = _make_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def _migrate(engine) -> None:
    """create_all 不会 alter 已有表；这里做只增列的轻量迁移。"""
    if not engine.url.get_backend_name().startswith("sqlite"):
        return
    additions = {"llm_calls": {"experiment_id": "TEXT",
                                "logical_call_id": "TEXT", "attempt_no": "INTEGER"},
                 # DDL 与模型声明同口径（四轮建议：迁移库/新建库 schema 漂移，
                 # SQLite 容忍、换后端即炸）
                 "experiments": {"run_owner": "VARCHAR(64)",
                                 "run_claimed_at": "VARCHAR(32)"},
                 # K1-A 二轮（会审）：身份/授权拆分的新列——只增列；旧
                 # allowed_purposes 列保留为遗留（模型不再读写），重跑
                 # register_work_sources.py 补齐新列
                 "work_sources": {"identity_purposes": "TEXT",
                                  "license_purposes": "TEXT",
                                  "license_basis": "TEXT"},
                 "segments": {"integrity": "TEXT", "role": "TEXT", "seg_version": "INTEGER DEFAULT 1",
                 "text_clean": "TEXT"},
                 "works": {"anchors": "TEXT", "v2_of": "TEXT"}}
    with engine.begin() as conn:
        for table, cols in additions.items():
            # 缺表跳过：建表归 create_all 全责（新表带全量列，无漂移）；
            # additions 只对**既有表**增列。旧口径「缺表 ALTER 直接炸」在
            # K1-A 增列 work_sources 后被 A07 迁移测打红——缺表不是迁移
            # 失败，是 create_all 的辖区（K1-A 二轮改定）。
            hit = conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table,)).fetchone()
            if hit is None:
                # 会审三轮建议：缺表不是失败（建表归 create_all）但**不许
                # 静默**——表名拼错时增列永远不生效，必须可见
                print(f"[_migrate] 表 {table} 不存在，跳过增列"
                      f"（新表由 create_all 带全量列建立）", flush=True)
                continue
            existing = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}
            for col, ddl in cols.items():
                if col not in existing:
                    conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")


def init_db() -> None:
    from . import models  # noqa: F401  确保表已注册

    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(engine)
    _migrate(engine)


def session() -> Session:
    return SessionLocal()
