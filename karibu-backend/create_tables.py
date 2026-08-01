"""Create database tables (dev/bootstrap).

Used by the `migrate` service in docker-compose to initialise the schema before
the app replicas start. For real schema evolution, replace this with Alembic
(`alembic upgrade head`) — the `alembic` dependency is already included.

    python create_tables.py
"""
import asyncio

import app.models  # noqa: F401 — registers all tables on the metadata
from app.core.config import settings
from app.core.database import Base, engine


def _refuse_in_production(what: str, instead: str) -> None:
    """These scripts predate the app having a real database.

    ENV is the only thing separating the dev database from the live one, which
    is exactly the kind of difference that is obvious only afterwards.
    """
    if settings.ENV == "production":
        raise SystemExit(
            f"REFUSING TO RUN: ENV=production. {what} {instead}"
        )


async def main():
    _refuse_in_production(
        "Alembic owns the production schema.",
        "Use `alembic upgrade head` — create_all does not stamp "
        "alembic_version, so the next migration would fail with "
        "'relation already exists'.",
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print("Tables created (or already present).")


if __name__ == "__main__":
    asyncio.run(main())
