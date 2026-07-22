"""Create database tables (dev/bootstrap).

Used by the `migrate` service in docker-compose to initialise the schema before
the app replicas start. For real schema evolution, replace this with Alembic
(`alembic upgrade head`) — the `alembic` dependency is already included.

    python create_tables.py
"""
import asyncio

import app.models  # noqa: F401 — registers all tables on the metadata
from app.core.database import Base, engine


async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print("Tables created (or already present).")


if __name__ == "__main__":
    asyncio.run(main())
