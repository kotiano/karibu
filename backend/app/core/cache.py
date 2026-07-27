"""Cache-aside helper backed by Redis (optional).

Same fallback philosophy as email.py's console mode: if CACHE_URL is unset,
every call is a no-op — reads always miss (so callers just fall through to
the DB) and writes/deletes do nothing. Nothing behaves differently in dev
without Redis configured; production sets CACHE_URL to get the speedup.

Never raises into the caller — a cache outage must degrade to "hit the DB
every time", not break the request.
"""
import json
import logging
from typing import Any

import redis.asyncio as redis

from app.core.config import settings

logger = logging.getLogger("karibu.cache")

_client: redis.Redis | None = None


def _get_client() -> "redis.Redis | None":
    global _client
    if not settings.CACHE_URL:
        return None
    if _client is None:
        _client = redis.from_url(settings.CACHE_URL, decode_responses=True)
    return _client


async def cache_get(key: str) -> Any | None:
    client = _get_client()
    if not client:
        return None
    try:
        raw = await client.get(key)
    except Exception:
        logger.warning("Cache GET failed for %s", key, exc_info=True)
        return None
    return json.loads(raw) if raw else None


async def cache_set(key: str, value: Any, ttl: int) -> None:
    client = _get_client()
    if not client:
        return
    try:
        await client.set(key, json.dumps(value), ex=ttl)
    except Exception:
        logger.warning("Cache SET failed for %s", key, exc_info=True)


async def cache_delete(*keys: str) -> None:
    client = _get_client()
    if not client or not keys:
        return
    try:
        await client.delete(*keys)
    except Exception:
        logger.warning("Cache DELETE failed for %s", keys, exc_info=True)
