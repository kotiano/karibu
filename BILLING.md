# Subscription & Billing — how it works

Karibu POS is sold as a per-restaurant SaaS subscription: **KSh 500/month**,
billed via **M-Pesa STK Push**, after a **14-day free trial**.

## Lifecycle

```
   register ──▶ TRIALING (14 days, full access, no charge)
                   │
        trial ends │ (billing sweep detects it)
                   ▼
             STK Push for KSh 500
              │              │
         success          failure
              │              │
              ▼              ▼
           ACTIVE         PAST_DUE ──retry (0h,+1d,+3d,+5d)──┐
              │              │                                │
   period ends│              └── all retries fail ──▶ SUSPENDED (access revoked)
              ▼                                            │
        renewal STK Push                          owner pays ──▶ ACTIVE
```

- **TRIALING** — created on signup. Full access until `trial_ends_at`.
- **ACTIVE** — paid and current. Renews at `current_period_end`.
- **PAST_DUE** — a charge failed; still has access during the retry window.
- **SUSPENDED** — retries exhausted; POS access blocked (billing still open).
- **CANCELLED** — owner cancelled.

## Who is billed

One subscription **per restaurant**. The owner's M-Pesa number
(`billing_phone`) receives the STK prompt; all staff share access. This is set
at signup and editable when paying.

## How charges happen

There are two triggers, both funnelling through the same idempotent
`billing.initiate_charge()`:

1. **Automatic** — a background sweep (every 15 min) finds subscriptions whose
   trial ended, period elapsed, or dunning retry is due, and charges them.
2. **Manual** — the owner taps *Subscribe now* / *Pay now* in the app
   (`POST /api/billing/pay`), e.g. to convert a trial early or clear a past-due
   balance.

Either way, Paystack pushes an M-Pesa STK prompt to the owner's phone; they
enter their PIN; Paystack posts a `charge.success` webhook; the subscription
advances to ACTIVE.

> **Why Paystack and not Daraja?** Safaricom's Daraja API needs production
> credentials that are only issued to a registered company with its own
> paybill/till. Paystack onboards unregistered "Starter" merchants and resells
> M-Pesa collection, so the owner's payment experience is identical — an STK
> prompt and a PIN — without the company paperwork. Starter accounts carry a
> lifetime collection cap until you upgrade to a registered business.

## Why it can't double-charge

See `SECURITY.md` → "Billing integrity". In short: a DB unique index allows only
one open charge per period, row locks serialise concurrent attempts, idempotency
keys collapse duplicate requests, and duplicate/late webhooks are ignored
against terminal charges. All four live in the database, not the gateway, which
is why the Daraja → Paystack swap didn't touch any of them.

## Configuration

All tunable via environment (see `backend/.env.example`):

| Variable | Default | Meaning |
|----------|---------|---------|
| `SUBSCRIPTION_PRICE_CENTS` | `50000` | KSh 500.00 |
| `TRIAL_DAYS` | `14` | Free trial length |
| `BILLING_PERIOD_DAYS` | `30` | Billing cycle |
| `DUNNING_RETRY_HOURS` | `0,24,72,120` | Retry offsets; count = attempts before suspension |
| `CHARGE_STALE_MINUTES` | `10` | Reconcile-then-fail an STK with no webhook |
| `PAYSTACK_SECRET_KEY` | — | `sk_test_…` / `sk_live_…`. Empty ⇒ charges are simulated |
| `PAYSTACK_WEBHOOK_ALLOWED_IPS` | — | Optional; leave empty (a stale list 403s real webhooks) |

## Endpoints

| Method | Path | Who | Purpose |
|--------|------|-----|---------|
| GET | `/api/billing/subscription` | any staff | Current status |
| POST | `/api/billing/pay` | owner | Charge now / retry (idempotent) |
| GET | `/api/billing/charges` | owner/manager | Payment history |
| POST | `/api/billing/webhook/paystack` | Paystack | Transaction result (signature-verified, idempotent) |

## Testing without real Paystack keys

If `PAYSTACK_SECRET_KEY` is unset, the client **simulates** the STK push and
returns a fake `sim_…` reference. The rest of the flow (charge rows, webhook
processing, state transitions) is identical, so the whole lifecycle is
exercisable in development.

Webhooks are authenticated by an HMAC-SHA512 signature over the raw body, so a
hand-rolled test webhook has to be signed:

```bash
REF="<provider_reference-from-the-charge>"
BODY="{\"event\":\"charge.success\",\"data\":{\"reference\":\"$REF\",\"gateway_response\":\"Approved\"}}"
SIG=$(printf '%s' "$BODY" | openssl dgst -sha512 -hmac "$PAYSTACK_SECRET_KEY" -r | cut -d' ' -f1)

curl -X POST http://localhost:8000/api/billing/webhook/paystack \
  -H "Content-Type: application/json" -H "x-paystack-signature: $SIG" \
  -d "$BODY"
```

Note the signature covers the bytes *as sent* — reformatting the JSON after
signing invalidates it.

For real integration, set `PAYSTACK_SECRET_KEY` and point the webhook URL in
**Paystack → Settings → API Keys & Webhooks** at a public
`/api/billing/webhook/paystack` (use ngrok in dev). The same code path goes live.

### Reconciling a lost webhook

Paystack retries a failed webhook (every 3 min ×4, then hourly for 72 h), but if
one is still never delivered the stale-charge sweep calls
`GET /transaction/verify/:reference` before failing a charge — so a payment that
really succeeded is recovered rather than being failed and billed again.
