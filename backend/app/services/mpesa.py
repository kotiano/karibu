"""Async M-Pesa Daraja (STK Push) client.

Uses httpx.AsyncClient so the outbound OAuth + STK calls don't block the event
loop — the concrete async win for a payments backend that calls an external
gateway. In sandbox mode with no credentials, stk_push returns a simulated
CheckoutRequestID so the full flow is exercisable without live keys.
"""
import base64
import time
from datetime import datetime, timedelta

import httpx

from app.core.config import settings


class MpesaError(Exception):
    """Raised when Daraja returns an error or is unreachable."""


_BASE = {
    "sandbox": "https://sandbox.safaricom.co.ke",
    "production": "https://api.safaricom.co.ke",
}

# In-process token cache: {base_url: (token, expires_at_epoch)}.
_token_cache: dict[str, tuple[str, float]] = {}


def _base_url() -> str:
    return _BASE.get(settings.MPESA_ENV, _BASE["sandbox"])


def _is_configured() -> bool:
    return bool(
        settings.MPESA_CONSUMER_KEY
        and settings.MPESA_CONSUMER_SECRET
        and settings.MPESA_PASSKEY
    )


def normalize_phone(phone: str) -> str:
    """07XXXXXXXX / +2547XXXXXXXX → Daraja's 2547XXXXXXXX."""
    p = phone.strip().replace(" ", "").replace("+", "")
    if p.startswith("0"):
        p = "254" + p[1:]
    if p.startswith("7") or p.startswith("1"):
        p = "254" + p
    return p


async def _get_access_token() -> str:
    base = _base_url()
    cached = _token_cache.get(base)
    if cached and cached[1] > time.time() + 30:
        return cached[0]

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{base}/oauth/v1/generate?grant_type=client_credentials",
                auth=(settings.MPESA_CONSUMER_KEY, settings.MPESA_CONSUMER_SECRET),
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as exc:
        raise MpesaError(f"Failed to obtain M-Pesa token: {exc}") from exc

    token = data["access_token"]
    expires_in = int(data.get("expires_in", 3599))
    _token_cache[base] = (token, time.time() + expires_in)
    return token


def _password(timestamp: str) -> str:
    raw = f"{settings.MPESA_SHORTCODE}{settings.MPESA_PASSKEY}{timestamp}"
    return base64.b64encode(raw.encode()).decode()


async def stk_push(
    *,
    phone: str,
    amount_cents: int,
    account_reference: str,
    description: str,
    callback_url: str,
) -> dict:
    """Initiate an STK Push. Returns {checkout_id, merchant_id, simulated}."""
    phone = normalize_phone(phone)
    amount = max(1, round(amount_cents / 100))  # Daraja needs integer KES, min 1

    if not _is_configured():
        fake_id = f"ws_CO_SIM_{int(time.time() * 1000)}"
        return {"checkout_id": fake_id, "merchant_id": "SIM", "simulated": True}

    # Daraja expects the timestamp in East Africa Time (UTC+3), NOT UTC.
    # Sending UTC puts every request three hours out and Daraja rejects it.
    timestamp = (datetime.utcnow() + timedelta(hours=3)).strftime("%Y%m%d%H%M%S")
    # A till (Buy Goods) and a paybill send different payloads:
    #   paybill -> TransactionType CustomerPayBillOnline, PartyB = the paybill
    #   till    -> TransactionType CustomerBuyGoodsOnline, PartyB = the TILL number,
    #              while BusinessShortCode stays the STORE (head office) number.
    # The password is always built from BusinessShortCode, so it is correct for both.
    is_till = bool(settings.MPESA_TILL_NUMBER)
    payload = {
        "BusinessShortCode": settings.MPESA_SHORTCODE,
        "Password": _password(timestamp),
        "Timestamp": timestamp,
        "TransactionType": (
            "CustomerBuyGoodsOnline" if is_till else "CustomerPayBillOnline"
        ),
        "Amount": amount,
        "PartyA": phone,
        "PartyB": settings.MPESA_TILL_NUMBER if is_till else settings.MPESA_SHORTCODE,
        "PhoneNumber": phone,
        "CallBackURL": callback_url,
        "AccountReference": account_reference[:12],
        "TransactionDesc": description[:13],
    }

    try:
        token = await _get_access_token()
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                f"{_base_url()}/mpesa/stkpush/v1/processrequest",
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
            )
            data = resp.json()
    except httpx.HTTPError as exc:
        raise MpesaError(f"STK push request failed: {exc}") from exc

    if str(data.get("ResponseCode")) != "0":
        raise MpesaError(
            data.get("errorMessage") or data.get("ResponseDescription") or "STK push rejected"
        )

    return {
        "checkout_id": data["CheckoutRequestID"],
        "merchant_id": data.get("MerchantRequestID"),
        "simulated": False,
    }