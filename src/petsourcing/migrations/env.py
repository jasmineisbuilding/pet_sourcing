"""Alembic environment. Driven programmatically from petsourcing.migrate (no alembic.ini)."""

from alembic import context

from petsourcing.models import Base

config = context.config
target_metadata = Base.metadata


def run_migrations_online() -> None:
    connectable = config.attributes.get("connection")
    if connectable is None:
        from sqlalchemy import create_engine

        connectable = create_engine(config.get_main_option("sqlalchemy.url"))
        with connectable.connect() as connection:
            _run(connection)
            connection.commit()
    else:
        _run(connectable)


def _run(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,  # SQLite cannot ALTER most things; batch mode rebuilds tables
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


run_migrations_online()
