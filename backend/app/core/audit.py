"""Audit logging helper.

record_audit(...) inserts one append-only AuditLog row. Callers pass the
Request so IP and user-agent are captured. Never raises into the caller — an
audit failure must not break the action being audited, though it is logged.
"""
import json
import logging

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog

logger = logging.getLogger("karibu.audit")


def _client_ip(request: Request | None) -> str | None:
    if not request:
        return None
    # X-Forwarded-For (set by Nginx) wins; fall back to the socket peer.
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else None


async def record_audit(
    db: AsyncSession,
    *,
    action: str,
    summary: str,
    actor_id: str | None = None,
    actor_email: str | None = None,
    restaurant_id: str | None = None,
    restaurant_name: str | None = None,
    target_id: str | None = None,
    detail: dict | None = None,
    request: Request | None = None,
    commit: bool = False,
) -> None:
    """Insert an audit row. Set commit=True only if the caller isn't already
    committing in the same transaction (most callers commit themselves)."""
    try:
        ua = request.headers.get("user-agent") if request else None
        entry = AuditLog(
            action=action,
            summary=summary[:400],
            actor_id=actor_id,
            actor_email=actor_email,
            restaurant_id=restaurant_id,
            restaurant_name=restaurant_name,
            target_id=target_id,
            detail=json.dumps(detail) if detail else None,
            ip_address=_client_ip(request),
            user_agent=(ua[:300] if ua else None),
        )
        db.add(entry)
        if commit:
            await db.commit()
    except Exception:
        logger.exception("Failed to write audit entry action=%s", action)
        if commit:
            await db.rollback()
