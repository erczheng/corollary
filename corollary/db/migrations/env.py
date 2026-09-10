"""Alembic environment.

The database URL comes from ``corollary.db.session.database_url()`` unless
alembic.ini or a caller overrides it, so ``alembic upgrade head`` and the
running engine can never open different files.

``render_as_batch`` is on because SQLite cannot ALTER a column in place;
Alembic's batch mode rebuilds the table instead. Nothing in the initial
migration needs it, but the first ALTER would fail obscurely without it.
"""

from logging.config import fileConfig

from alembic import context

from corollary.db.models import Base
from corollary.db.session import create_db_engine, database_url

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _url() -> str:
    configured = config.get_main_option("sqlalchemy.url", default=None)
    if configured:
        return configured
    return database_url()


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # create_db_engine rather than engine_from_config: it carries the WAL and
    # foreign-key pragmas, so a migration runs against the same SQLite
    # configuration the engine uses.
    connectable = create_db_engine(_url())
    try:
        with connectable.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                render_as_batch=True,
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
