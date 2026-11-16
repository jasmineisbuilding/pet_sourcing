from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from . import config


def make_engine(path: Path | None = None) -> Engine:
    path = path or config.db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(config.db_url(path))

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_conn, _record):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")  # SQLite ignores FKs unless asked
        # Let SQLAlchemy, not the sqlite3 driver, control transactions, so that
        # SAVEPOINTs work (each agent tool call runs in one and rolls back on error).
        dbapi_conn.isolation_level = None

    @event.listens_for(engine, "begin")
    def _on_begin(conn):
        conn.exec_driver_sql("BEGIN")

    return engine


def init_db(engine: Engine) -> None:
    """Create or upgrade the schema via migrations (idempotent)."""
    from .migrate import upgrade

    upgrade(engine)


@contextmanager
def session_scope(engine: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
