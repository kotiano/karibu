"""FastAPI dependency-injection layer.

Replaces the Flask decorators with composable `Depends`:
  - get_current_user       → verifies JWT + token version, loads the user
  - require_roles(...)      → role-based access guard
  - get_current_restaurant → the caller's tenant (from the verified token)
  - require_subscription   → 402 unless the subscription grants access

Routes declare what they need in their signature, e.g.:
    async def endpoint(user = Depends(require_roles("owner"))): ...
"""
from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import APIError, decode_token
from app.models import Restaurant, Subscription, User

# HTTPBearer extracts the "Authorization: Bearer <token>" header and 401s if
# missing (auto_error=True), matching the Flask unauthorized behaviour.
bearer = HTTPBearer(auto_error=True)

DbDep = Annotated[AsyncSession, Depends(get_db)]


async def get_token_payload(
    creds: Annotated[HTTPAuthorizationCredentials, Depends(bearer)],
) -> dict:
    payload = decode_token(creds.credentials)
    if payload.get("type") != "access":
        raise APIError("Invalid token type", status=401)
    return payload


async def get_current_user(
    payload: Annotated[dict, Depends(get_token_payload)],
    db: DbDep,
) -> User:
    user = await db.get(User, payload.get("sub"))
    if not user or not user.is_active:
        raise APIError("Account not found or disabled", status=401)
    # Token-version check: reject tokens issued before a password change.
    if payload.get("tv") != user.token_version:
        raise APIError("Session expired, please sign in again", status=401)
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


async def get_settled_user(user: CurrentUser) -> User:
    """A user who is not mid-way through a forced password change.

    Staff created by a manager start with a temporary code and
    must_change_password set. Until they choose their own, the session may
    reach exactly one endpoint — /auth/change-password — and nothing else.

    ENFORCED HERE, NOT IN THE UI. The manager who issued the code knows it, so
    a client-side redirect would leave them able to act as that person by
    calling the API directly — which is precisely what the served-by
    attribution is supposed to rule out.
    """
    if user.must_change_password:
        raise APIError(
            "Set your own password before continuing.",
            status=403,
            errors={"must_change_password": "required"},
        )
    return user


SettledUser = Annotated[User, Depends(get_settled_user)]


def require_roles(*allowed_roles: str):
    """Dependency factory enforcing the caller holds one of the given roles.

    Depends on SettledUser rather than CurrentUser, so every role-gated route
    inherits the forced-password-change block without having to remember it.
    """

    async def _dep(user: SettledUser) -> User:
        if user.role not in allowed_roles:
            raise APIError("Insufficient permissions", status=403)
        return user

    return _dep


async def get_current_restaurant(user: CurrentUser, db: DbDep) -> Restaurant:
    restaurant = await db.get(Restaurant, user.restaurant_id)
    if not restaurant:
        raise APIError("Restaurant not found", status=401)
    return restaurant


async def require_subscription(user: SettledUser, db: DbDep) -> User:
    """Block the request (402) unless the caller's subscription grants access.

    Returns the user so routes can chain it; the tenant id is on user.restaurant_id.
    Platform admins are never gated — they may not belong to a paying tenant.
    """
    if user.is_platform_admin:
        return user
    result = await db.execute(
        select(Subscription).where(Subscription.restaurant_id == user.restaurant_id)
    )
    sub = result.scalar_one_or_none()
    if not sub or not sub.has_access:
        raise APIError(
            "Your subscription is inactive. Please renew to continue.",
            status=402,
            errors={"subscription": sub.status if sub else "none"},
        )
    return user


async def require_platform_admin(user: CurrentUser) -> User:
    """Gate for the cross-tenant /admin surface.

    Returns 404 (not 403) to non-admins so probing can't even confirm the
    admin routes exist — concealment is cheap hardening for the crown jewels.
    The flag is re-read from the DB on every request (via get_current_user),
    so revoking admin takes effect immediately, not at token expiry.
    """
    if not user.is_platform_admin:
        raise APIError("Not found", status=404)
    return user


PlatformAdmin = Annotated[User, Depends(require_platform_admin)]


# Convenience annotated deps.
SubscribedUser = Annotated[User, Depends(require_subscription)]
