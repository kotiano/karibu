"""Google Play Developer API client for subscription verification.

WHY THIS EXISTS AT ALL: the app can report "the user bought a subscription", but
a client claim is not evidence — anyone can POST that. Entitlement is granted
only after Google itself confirms the purchase token, which is what this module
does. Never activate a subscription from client-supplied state.

Two responsibilities:

1. VERIFY — ask Google what a purchase token actually represents
   (purchases.subscriptionsv2.get). Returns the real state, expiry and whether
   it is still paying.

2. ACKNOWLEDGE — tell Google we granted entitlement. This is not optional:
   **Google automatically refunds and revokes any purchase not acknowledged
   within three days.** A working verify path with a missing acknowledge looks
   perfect in testing and silently refunds every real customer 72 hours later.

Auth is the OAuth2 service-account flow done directly over httpx rather than
pulling in google-auth + google-api-python-client: it is a signed JWT exchanged
for a bearer token, roughly forty lines, and avoids two large dependencies in a
service that already has httpx and python-jose.
"""
import json
import logging
import time
from datetime import datetime, timezone

import httpx
from jose import jwt

from app.core.config import settings

logger = logging.getLogger("karibu.googleplay")

_TOKEN_URL = "https://oauth2.googleapis.com/token"
_SCOPE = "https://www.googleapis.com/auth/androidpublisher"
_API = "https://androidpublisher.googleapis.com/androidpublisher/v3"

# Cached bearer token: (token, expires_at_epoch).
_token_cache: tuple[str, float] | None = None


class GooglePlayError(Exception):
    """Raised when Google rejects a request or is unreachable."""


class PurchaseNotFound(GooglePlayError):
    """Google has no record of this purchase token."""


def is_configured() -> bool:
    return bool(
        settings.GOOGLE_PLAY_PACKAGE_NAME and settings.google_play_credentials
    )


# --- Auth -------------------------------------------------------------------
async def _access_token() -> str:
    """Exchange a service-account JWT for a bearer token, cached until expiry."""
    global _token_cache
    if _token_cache and _token_cache[1] > time.time() + 60:
        return _token_cache[0]

    creds = settings.google_play_credentials
    if not creds:
        raise GooglePlayError("GOOGLE_PLAY_SERVICE_ACCOUNT_JSON is not configured")

    now = int(time.time())
    assertion = jwt.encode(
        {
            "iss": creds["client_email"],
            "scope": _SCOPE,
            "aud": _TOKEN_URL,
            "iat": now,
            "exp": now + 3600,
        },
        creds["private_key"],
        algorithm="RS256",
    )

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                _TOKEN_URL,
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                    "assertion": assertion,
                },
            )
            body = resp.json()
    except httpx.HTTPError as exc:
        raise GooglePlayError(f"Could not reach Google's token endpoint: {exc}") from exc

    if resp.status_code >= 400 or "access_token" not in body:
        # The usual cause is the service account not being granted access in
        # Play Console, which returns a perfectly valid-looking 401 here.
        raise GooglePlayError(
            f"Google refused the service-account credentials "
            f"(HTTP {resp.status_code}): {body.get('error_description') or body}"
        )

    token = body["access_token"]
    _token_cache = (token, time.time() + int(body.get("expires_in", 3600)))
    return token


async def _request(method: str, path: str, **kwargs) -> dict:
    token = await _access_token()
    url = f"{_API}/applications/{settings.GOOGLE_PLAY_PACKAGE_NAME}{path}"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.request(
                method, url, headers={"Authorization": f"Bearer {token}"}, **kwargs
            )
    except httpx.HTTPError as exc:
        raise GooglePlayError(f"Google Play API request failed: {exc}") from exc

    if resp.status_code == 404:
        raise PurchaseNotFound("Google has no record of this purchase")
    if resp.status_code == 410:
        # Token no longer valid — the subscription is long gone.
        raise PurchaseNotFound("This purchase token has expired at Google")
    if resp.status_code >= 400:
        raise GooglePlayError(
            f"Google Play API returned HTTP {resp.status_code}: {resp.text[:300]}"
        )
    return resp.json() if resp.content else {}


# --- Subscription state -----------------------------------------------------
def _parse_rfc3339(value: str | None) -> datetime | None:
    """Google returns RFC3339 with a Z; the DB columns are naive UTC."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


# subscriptionsv2 states that still entitle the user to the product. PAUSED is
# deliberately excluded: the user keeps the subscription but has explicitly
# stopped paying for a period, and should not have access during it.
_ENTITLED_STATES = {
    "SUBSCRIPTION_STATE_ACTIVE",
    "SUBSCRIPTION_STATE_IN_GRACE_PERIOD",
    "SUBSCRIPTION_STATE_CANCELED",  # cancelled but paid through to expiry
}


async def get_subscription(purchase_token: str) -> dict:
    """Fetch the true state of a purchase token.

    Returns a normalised dict:
      {state, entitled, expiry, product_id, acknowledged, linked_token, raw}
    """
    data = await _request(
        "GET", f"/purchases/subscriptionsv2/tokens/{purchase_token}"
    )

    state = data.get("subscriptionState", "")
    line_items = data.get("lineItems") or []
    # The last line item carries the current plan and its expiry.
    current = line_items[-1] if line_items else {}

    return {
        "state": state,
        "entitled": state in _ENTITLED_STATES,
        "expiry": _parse_rfc3339(current.get("expiryTime")),
        "product_id": current.get("productId"),
        # "acknowledgementState" is ACKNOWLEDGED / PENDING.
        "acknowledged": data.get("acknowledgementState")
        == "ACKNOWLEDGEMENT_STATE_ACKNOWLEDGED",
        # Set when this purchase replaced an earlier one (upgrade, resubscribe).
        "linked_token": data.get("linkedPurchaseToken"),
        "test": bool(data.get("testPurchase")),
        "raw": data,
    }


async def acknowledge(purchase_token: str) -> None:
    """Confirm to Google that entitlement was granted.

    MUST happen within three days of purchase. Google refunds and revokes
    anything still unacknowledged after that, silently, long after the code
    looked like it worked in testing.
    """
    await _request(
        "POST",
        f"/purchases/subscriptions/{settings.GOOGLE_PLAY_PRODUCT_ID}"
        f"/tokens/{purchase_token}:acknowledge",
        json={},
    )
    logger.info("Acknowledged Play purchase %s", purchase_token[:16] + "…")


# --- Real-time Developer Notifications --------------------------------------
def decode_rtdn(envelope: dict) -> dict | None:
    """Pull the notification out of a Pub/Sub push envelope.

    Google wraps RTDNs in a Pub/Sub message whose `data` is base64 JSON. Returns
    None for anything that isn't a subscription notification (test pings and
    one-time-product notifications both arrive on the same topic).
    """
    import base64

    message = envelope.get("message") or {}
    raw = message.get("data")
    if not raw:
        return None
    try:
        payload = json.loads(base64.b64decode(raw).decode("utf-8"))
    except Exception:
        logger.warning("Undecodable RTDN payload")
        return None

    # Google sends this when you first wire the topic up. Acknowledge it and
    # move on, or Pub/Sub retries it forever.
    if "testNotification" in payload:
        logger.info("Received Google Play test notification — topic is wired up")
        return {"test": True}

    sub = payload.get("subscriptionNotification")
    if not sub:
        return None
    return {
        "test": False,
        "purchase_token": sub.get("purchaseToken"),
        "product_id": sub.get("subscriptionId"),
        "notification_type": sub.get("notificationType"),
        "package": payload.get("packageName"),
    }
