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
                 "segments": {"integrity": "TEXT", "role": "TEXT", "seg_version": "INTEGER DEFAULT 1",
                 "text_clean": "TEXT"},
                 "works": {"anchors": "TEXT", "v2_of": "TEXT"}}
    with engine.begin() as conn:
        for table, cols in additions.items():
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
