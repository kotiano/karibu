"""Rate limiting via slowapi (the FastAPI-native analog of Flask-Limiter).

Keyed on the real client IP. Storage is memory:// in dev; set RATELIMIT_STORAGE_URI
to redis://… in production so the limit is shared across load-balanced workers.
"""
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.core.config import settings

limiter = Limiter(
    key_func=get_remote_address,
    storage_uri=settings.RATELIMIT_STORAGE_URI,
    default_limits=[settings.RATELIMIT_DEFAULT],
)
