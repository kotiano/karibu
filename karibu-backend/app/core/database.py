"""Async database engine, session factory, and the request-scoped session dep.

Uses SQLAlchemy 2.0 async. `get_db` yields a session per request and is injected
into routes via Depends. Row locking (SELECT ... FOR UPDATE) is used in the
billing service on Postgres; on SQLite it's a no-op but the correctness
guarantees are backed by the unique index regardless.
"""
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings

# SQLite needs check_same_thread off for the async driver; Postgres uses a pool.
_connect_args = {"check_same_thread": False} if settings.is_sqlite else {}

# Tuned pool for Postgres: enough persistent connections to serve steady load
# without reconnect latency, overflow headroom for spikes, and recycling so
# stale connections (dropped by firewalls/PG restarts) never serve a request.
_pool_kwargs = (
    {}
    if settings.is_sqlite
    else {
        "pool_size": settings.DB_POOL_SIZE,
        "max_overflow": settings.DB_MAX_OVERFLOW,
        "pool_recycle": settings.DB_POOL_RECYCLE_SECONDS,
        "pool_timeout": 30,
    }
)

engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    pool_pre_ping=not settings.is_sqlite,
    connect_args=_connect_args,
    **_pool_kwargs,
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


class Base(DeclarativeBase):
    """Declarative base for all models."""


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Yield a database session for the lifetime of a request."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
