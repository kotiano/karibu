"""Alembic migration environment for Karibu POS.

Reads DATABASE_URL from the app settings (so migrations hit the same DB as the
app) and targets the app's model metadata, so `--autogenerate` sees every
table. Handles the app's async driver by translating it to a sync driver for
the migration run — Alembic itself runs synchronously.
"""
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context

# Import the app's settings and metadata.
from app.core.config import settings
from app.core.database import Base
import app.models  # noqa: F401 — registers every table on Base.metadata

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _sync_url() -> str:
    """Alembic runs synchronously, so translate async drivers to sync ones."""
    url = settings.DATABASE_URL
    # Postgres: asyncpg -> psycopg2 (sync). psycopg2 is only needed for
    # migrations; add `psycopg2-binary` to requirements for prod migrations.
    url = url.replace("postgresql+asyncpg://", "postgresql://", 1)
    # SQLite: aiosqlite -> default sync sqlite driver.
    url = url.replace("sqlite+aiosqlite://", "sqlite://", 1)
    return url


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a live DB connection."""
    context.configure(
        url=_sync_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live DB connection."""
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = _sync_url()

    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,  # detect column type changes
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
