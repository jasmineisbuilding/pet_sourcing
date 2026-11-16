"""Schema migrations (Alembic), driven from code so there is no alembic.ini to keep in sync.

- ``upgrade(engine)``: bring a database to the latest schema. Safe to run repeatedly.
- Databases created before migrations existed (tables present, no
  ``alembic_version``) are stamped at the first revision, then upgraded.
- ``revision(message)``: autogenerate a new migration from changes in models.py.
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Engine, inspect

from . import config

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
BASELINE_REVISION = "0001"


def _config(engine: Engine) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", str(engine.url))
    return cfg


def current_revision(engine: Engine) -> str | None:
    with engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


def upgrade(engine: Engine, target: str = "head") -> None:
    cfg = _config(engine)
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        tables = set(inspect(conn).get_table_names())
        if "opportunity" in tables and "alembic_version" not in tables:
            command.stamp(cfg, BASELINE_REVISION)  # pre-migration database
        command.upgrade(cfg, target)


def revision(message: str, engine: Engine | None = None) -> None:
    from .db import make_engine

    engine = engine or make_engine(config.db_path())
    upgrade(engine)
    cfg = _config(engine)
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.revision(cfg, message=message, autogenerate=True)
