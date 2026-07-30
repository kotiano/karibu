"""Async Paystack client (M-Pesa mobile money charges).

Replaces the direct Safaricom Daraja integration. Daraja's production
credentials require a registered company (certificate of incorporation, a
corporate paybill/till); Paystack accepts unregistered "Starter" merchants and
resells M-Pesa collection on top, so the same STK-push UX is reachable without
the company paperwork.

The customer experience is unchanged: POST /charge with a phone number pushes an
STK prompt to that handset, the customer enters their PIN, and Paystack posts a
`charge.success` webhook. Only the wire protocol differs.

Two things about Paystack differ from Daraja and shape the code below:

- Amounts are in the currency's *subunit* — cents for KES — which is already
  how money is stored everywhere in this app, so no conversion is needed.
- Webhooks are authenticated with an HMAC-SHA512 signature over the raw request
  body rather than a secret in the URL path, so verify_webhook() must be handed
  the exact bytes received (see routers/billing.py).

As with the Daraja client, when no credentials are configured charge_mobile_money
returns a simulated reference so the whole billing flow stays exercisable in
development without live keys.
"""
import hashlib
import hmac
import logging
import time

import httpx

from app.core.config import settings

logger = logging.getLogger("karibu.paystack")

_BASE_URL = "https://api.paystack.co"

# Paystack's transaction states. Only these two are final; everything else means
# "the customer is still being asked something", and we let the charge sit in
# PROCESSING until the webhook arrives or the stale-charge sweep times it out.
# Failing early on an unrecognised state would be the dangerous mistake: the
# customer may still complete the payment on their handset seconds later.
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
# The expected state for M-Pesa: "complete the authorization on your phone".
STATUS_PAY_OFFLINE = "pay_offline"
# States that need an interactive follow-up call we deliberately don't make —
# they shouldn't occur for mobile money, but log loudly if they ever do.
_INTERACTIVE_STATUSES = {"send_otp", "send_pin", "send_phone", "send_birthday", "open_url"}


class PaystackError(Exception):
    """Raised when Paystack returns an error or is unreachable."""


def is_configured() -> bool:
    return bool(settings.PAYSTACK_SECRET_KEY)


def normalize_phone(phone: str) -> str:
    """07XXXXXXXX / 2547XXXXXXXX → Paystack's preferred +2547XXXXXXXX.

    Paystack recommends the E.164 form with the country code; Daraja wanted a
    bare 2547… instead, which is why this differs from the old normalizer.
    """
    p = phone.strip().replace(" ", "").replace("-", "").replace("+", "")
    if p.startswith("0"):
        p = "254" + p[1:]
    elif p.startswith("7") or p.startswith("1"):
        p = "254" + p
    return "+" + p


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}",
        "Content-Type": "application/json",
    }


def verify_webhook(raw_body: bytes, signature: str | None) -> bool:
    """Check a webhook's x-paystack-signature header.

    HMAC-SHA512 of the *raw* request body, keyed with the secret key. It must be
    the bytes as received — re-serialising the parsed JSON reorders keys and
    changes whitespace, which produces a different digest and rejects every
    legitimate webhook.
    """
    if not signature or not settings.PAYSTACK_SECRET_KEY:
        return False
    expected = hmac.new(
        settings.PAYSTACK_SECRET_KEY.encode(), raw_body, hashlib.sha512
    ).hexdigest()
    # compare_digest, not ==, so response timing doesn't leak how many leading
    # hex characters matched.
    return hmac.compare_digest(expected, signature)


async def _post(path: str, payload: dict) -> dict:
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{_BASE_URL}{path}", json=payload, headers=_headers()
            )
            body = response.json()
    except httpx.HTTPError as exc:
        raise PaystackError(f"Paystack request failed: {exc}") from exc
    except ValueError as exc:  # non-JSON body (gateway error page)
        raise PaystackError(
            f"Paystack returned a non-JSON response (HTTP {response.status_code})"
        ) from exc

    # `status` here is whether the API call itself succeeded — NOT the state of
    # the transaction, which lives in data.status. Conflating the two is the
    # classic Paystack integration bug.
    if not body.get("status"):
        raise PaystackError(body.get("message") or "Paystack rejected the request")
    return body.get("data") or {}


async def charge_mobile_money(
    *,
    email: str,
    phone: str,
    amount_cents: int,
    currency: str = "KES",
    metadata: dict | None = None,
) -> dict:
    """Push an M-Pesa STK prompt to `phone`.

    Returns {reference, status, display_text, simulated}. `status` is Paystack's
    transaction state — callers should treat only "success"/"failed" as final
    and wait for the webhook otherwise.
    """
    if not is_configured():
        fake_ref = f"sim_{int(time.time() * 1000)}"
        return {
            "reference": fake_ref,
            "status": STATUS_PAY_OFFLINE,
            "display_text": "Simulated charge (no Paystack keys configured)",
            "simulated": True,
        }

    payload = {
        "email": email,
        # Already in subunits — Paystack wants cents for KES, which is how
        # amount_cents is stored, so this passes straight through.
        "amount": amount_cents,
        "currency": currency,
        "mobile_money": {"phone": normalize_phone(phone), "provider": "mpesa"},
    }
    if metadata:
        payload["metadata"] = metadata

    data = await _post("/charge", payload)

    reference = data.get("reference")
    if not reference:
        raise PaystackError("Paystack accepted the charge but returned no reference")

    status = (data.get("status") or "").lower()
    if status in _INTERACTIVE_STATUSES:
        logger.warning(
            "Paystack asked for an interactive step (%s) on mobile money charge "
            "%s — this flow does not answer those, so the charge will time out. "
            "Check the mobile money channel setup on the Paystack account.",
            status, reference,
        )

    return {
        "reference": reference,
        "status": status,
        "display_text": data.get("display_text") or "",
        "simulated": False,
    }


async def verify_transaction(reference: str) -> dict:
    """Fetch a transaction's current state, for reconciling a missed webhook."""
    if not is_configured():
        raise PaystackError("Paystack is not configured")
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                f"{_BASE_URL}/transaction/verify/{reference}", headers=_headers()
            )
            body = response.json()
    except httpx.HTTPError as exc:
        raise PaystackError(f"Paystack verify failed: {exc}") from exc

    if not body.get("status"):
        raise PaystackError(body.get("message") or "Paystack verify rejected")
    return body.get("data") or {}


def extract_receipt(data: dict) -> str | None:
    """Best-effort M-Pesa confirmation code out of a transaction payload.

    Paystack doesn't surface the Safaricom receipt in a single documented field
    for mobile money, so try the places it has been seen and fall back to
    Paystack's own reference — which is always present and is what their
    dashboard and support both key off, so it's a useful thing to show the user
    either way.
    """
    authorization = data.get("authorization") or {}
    for candidate in (
        authorization.get("receipt_number"),
        authorization.get("receiver_bank_account_number"),
        data.get("receipt_number"),
    ):
        if candidate:
            return str(candidate)[:40]
    reference = data.get("reference")
    return str(reference)[:40] if reference else None
