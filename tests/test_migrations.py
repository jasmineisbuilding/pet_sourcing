from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect, text

from petsourcing.db import init_db, make_engine
from petsourcing.migrate import current_revision, upgrade
from petsourcing.models import Base
from petsourcing.search_index import is_index_object


def test_migrations_match_models(tmp_path):
    """If this fails, models.py changed without a migration: run `petsrc db revision -m ...`."""
    engine = make_engine(tmp_path / "m.db")
    init_db(engine)
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={
            "include_name": lambda name, type_, parent: not (type_ == "table" and is_index_object(name))
        })
        diff = compare_metadata(ctx, Base.metadata)
    assert diff == []


def test_upgrade_is_idempotent(tmp_path):
    engine = make_engine(tmp_path / "m.db")
    init_db(engine)
    rev = current_revision(engine)
    init_db(engine)
    assert current_revision(engine) == rev


def test_pre_migration_database_is_upgraded_without_data_loss(tmp_path):
    engine = make_engine(tmp_path / "old.db")
    upgrade(engine, "0001")
    with engine.begin() as conn:  # simulate a db made before migrations existed
        conn.execute(text("DROP TABLE alembic_version"))
        conn.execute(text(
            "INSERT INTO opportunity (slug, title, category_profile, target_market, stage, "
            "is_demo, created_at, updated_at) VALUES ('x','x','pet_supplies','CA','to_research',0,"
            "'2026-01-01','2026-01-01')"))
    init_db(engine)
    assert "supersede_note" in {c["name"] for c in inspect(engine).get_columns("evidence")}
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM opportunity")).scalar() == 1
