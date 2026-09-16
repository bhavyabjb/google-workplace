# Alembic's entrypoint script. Wires Alembic up to our actual SQLAlchemy models/engine
# so `alembic revision --autogenerate` can diff the DB against app/db/models.py, and
# `alembic upgrade head` applies migrations using our real DATABASE_URL (from .env)
# rather than the placeholder in alembic.ini.

from logging.config import fileConfig
# fileConfig: sets up Python logging from the [loggers]/[handlers] sections of alembic.ini.

from alembic import context
# context: Alembic's global object representing the current migration run
# (gives us the configured URL, and run_migrations_online/offline entrypoints).

from app.config import get_settings
# Reuse our own typed settings instead of duplicating a DB URL in alembic.ini.

from app.db.session import Base
# Base: the declarative base every model inherits. Importing app.db.models below
# registers all tables onto Base.metadata, which is what autogenerate diffs against.
import app.db.models  # noqa: F401  (import side-effect: registers tables on Base.metadata)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Point Alembic at our real database, overriding the placeholder from alembic.ini.
config.set_main_option("sqlalchemy.url", get_settings().database_url)

# This is what `--autogenerate` compares the live DB schema against.
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Generate SQL without a live DB connection (`alembic upgrade head --sql`)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Connect to the real database and apply migrations directly (the common case)."""
    from sqlalchemy import engine_from_config, pool

    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,  # short-lived connection just for running migrations
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
