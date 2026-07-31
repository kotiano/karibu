"""JWT helpers (create/decode) and the API error type.

Tokens embed the same claims as the Flask version: role, branch, rid (tenant),
tv (token version), plus a `type` of access/refresh so refresh tokens can't be
used as access tokens.
"""
import hashlib
import hmac
import secrets as _secrets
from datetime import datetime, timedelta, timezone

from jose import JWTError, jwt

from app.core.config import settings


class APIError(Exception):
    """Raise anywhere to return a controlled JSON error via the handler."""

    def __init__(self, message: str, status: int = 400, errors: dict | None = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.errors = errors


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _base_claims(user_id: str, token_type: str, lifetime: timedelta) -> dict:
    return {
        "sub": user_id,
        "type": token_type,
        "iss": settings.JWT_ISSUER,       # who minted it
        "aud": settings.JWT_AUDIENCE,     # who it's for
        "jti": _secrets.token_hex(8),     # unique id (audit/troubleshooting)
        "iat": _now(),
        "exp": _now() + lifetime,
    }


def create_access_token(user_id: str, extra: dict) -> str:
    payload = {
        **_base_claims(user_id, "access", timedelta(minutes=settings.JWT_ACCESS_MINUTES)),
        **extra,
    }
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def create_refresh_token(user_id: str, extra: dict) -> str:
    payload = {
        **_base_claims(user_id, "refresh", timedelta(days=settings.JWT_REFRESH_DAYS)),
        **extra,
    }
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    """Decode + verify a JWT (signature, expiry, issuer, audience).

    Raises APIError(401) on any problem. Validating iss/aud means a token
    minted by anything else that happens to share a secret can't be replayed
    against this API.
    """
    try:
        return jwt.decode(
            token,
            settings.JWT_SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
            issuer=settings.JWT_ISSUER,
            audience=settings.JWT_AUDIENCE,
        )
    except JWTError as exc:
        raise APIError("Invalid or expired token", status=401) from exc


def token_claims(user) -> dict:
    """Standard extra claims embedded in both token types."""
    return {
        "role": user.role,
        "branch": user.branch_name,
        "rid": user.restaurant_id,
        "tv": user.token_version,
    }


# --- Email verification codes ------------------------------------------------
# A 6-digit OTP sent by email at signup, in place of a clickable link — the
# --- Email confirmation link tokens ----------------------------------------
# The web app confirms an email by clicking a link, not by typing a code. That
# changes the threat model completely: a 6-digit code has a million
# possibilities and needs an attempt cap to survive, whereas this is 256 bits of
# entropy and cannot be guessed, so no attempt counter is needed and a longer
# expiry is safe.
#
# Only the sha256 hash is stored. A database leak must not hand the attacker a
# working confirmation link, and sha256 hex is exactly 64 characters — the width
# the email_token column already has.
def generate_link_token() -> str:
    """A URL-safe token to embed in a confirmation link. Never stored as-is."""
    return _secrets.token_urlsafe(32)


def hash_link_token(token: str) -> str:
    return hashlib.sha256(token.strip().encode()).hexdigest()


def verify_link_token(token: str, token_hash: str | None) -> bool:
    if not token_hash or not token:
        return False
    # Constant-time, so response timing can't reveal a partial match.
    return hmac.compare_digest(hash_link_token(token), token_hash)
