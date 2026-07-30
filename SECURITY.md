# Security & Billing Integrity

This document explains the security posture of Karibu POS and — importantly —
what it does and does not guarantee.

## An honest word on "unbreakable"

There is no such thing as a system that is *technically impossible to attack*.
Any vendor who claims otherwise is misleading you. What a serious system can do
is make attacks **expensive, detectable, and low-payoff** through layered
controls ("defense in depth"), so that the effort required vastly outweighs what
an attacker gains, and so that the damage from any single failure is contained.

That is the standard this system is built to. Below is exactly what is in place,
and the assumptions each layer relies on.

---

## Billing integrity (no double-charging)

This is the requirement we treated most carefully, because charging a customer
twice is both a real financial harm and a trust-killer. Four independent
safeguards protect it; a bug in any one is caught by the others.

1. **One open charge per period — enforced by the database.**
   A partial unique index (`uq_open_charge_per_period`) makes it *physically
   impossible* to have two pending/processing charges for the same subscription
   and period at once. Even if two servers try simultaneously, one INSERT
   succeeds and the other is rejected by the database.

2. **Row-level locking.**
   Every charge mutation takes `SELECT … FOR UPDATE` on the subscription, so
   concurrent billing operations serialise instead of interleaving. (Postgres
   enforces this; the unique index above backs it up everywhere.)

3. **Idempotency keys.**
   Each charge attempt has a deterministic key. Retrying the *same* attempt maps
   to the existing charge row rather than creating a new one — so a network
   retry or a double-tapped "Pay now" button cannot produce a second charge.

4. **Callback idempotency + terminal states.**
   M-Pesa can deliver the same callback more than once. Each `CheckoutRequestID`
   is recorded in `processed_callbacks`; a duplicate is ignored in O(1). And once
   a charge is `success`/`failed` it is immutable — a late or repeated callback
   can never re-apply a payment or extend a paid period twice.

Supporting behaviours:
- **Stale-charge recovery.** If an STK push gets no callback (lost network),
  a sweep auto-fails it after `CHARGE_STALE_MINUTES` so the subscription isn't
  wedged and can be retried cleanly.
- **Dunning.** Failed renewals retry on a schedule (`DUNNING_RETRY_HOURS`),
  then suspend access. Retries reuse all the guarantees above.
- **Money in integer cents.** No floating-point rounding anywhere.
- **Scheduler runs in exactly one instance.** Even though the app is load
  balanced, only the instance with `ENABLE_SCHEDULER=true` runs the billing
  sweep, so periodic jobs fire once.

These properties are covered by an automated test suite that simulates duplicate
callbacks, concurrent charge attempts, dunning exhaustion, and lost callbacks.

---

## Authentication & session security

- **Password hashing** with Werkzeug's PBKDF2 (salted, one-way).
- **JWT access + refresh tokens.** Short-lived access tokens; refresh rotation.
- **Token versioning.** Each user has a `token_version`; changing a password
  bumps it, instantly invalidating every previously issued token (logout
  everywhere).
- **Uniform login errors + constant-ish timing.** The login endpoint returns the
  same message whether the email is unknown or the password is wrong, and always
  performs a hash operation, to avoid leaking which emails have accounts.
- **Brute-force protection.** Login/registration are rate limited (default 5/min
  per IP), returning `429` beyond the threshold.

## Hardening layer (added after initial build)

These were added specifically to raise the cost of the attacks that get past
basic controls:

- **Account-level login lockout.** Per-IP rate limiting alone is defeated by an
  attacker rotating IPs against one account. After 5 consecutive failures the
  *account* locks for 15 minutes (returns 429 even to the correct password),
  then auto-unlocks. Successful login resets the counter. Configurable via
  `LOGIN_LOCKOUT_THRESHOLD` / `LOGIN_LOCKOUT_MINUTES`.
- **Common-password denylist.** "password123" passes an 8-char minimum and is
  in every attacker's first hundred guesses. Registration and password change
  reject the top of the real-world breach lists plus trivial sequences.
- **JWT issuer/audience/jti.** Tokens carry and the API validates `iss` and
  `aud`, so a token minted by any other system that ever shares a secret can't
  be replayed here. Every token also carries a unique `jti` for auditability.
- **Timing-safe callback secret comparison.** The M-Pesa callback secret is
  compared with `hmac.compare_digest`, closing the byte-by-byte timing channel
  a plain `!=` leaks.
- **Production boot guard.** The app *refuses to start* in `ENV=production`
  with placeholder secrets, wildcard CORS, or wildcard hosts. Shipping with
  default secrets is one of the most common real-world breach causes; failing
  fast removes the whole class.
- **Host-header guard.** With `ALLOWED_HOSTS` set, requests with a foreign
  `Host` header are rejected (cache-poisoning / reset-poisoning class).
- **Request body cap.** Bodies over `MAX_BODY_BYTES` (default 1 MB) are
  rejected with 413 before being read — memory-exhaustion guard at the app
  layer, backing up the same limit at the Nginx edge.
- **Bounded list endpoints.** Order listing is paginated (default 50, hard cap
  200) — an unbounded list is both a performance cliff and a DoS vector.

## Platform-admin surface (/api/admin)

The one place tenant isolation is deliberately crossed — so it gets the
strictest treatment in the system:

- The `is_platform_admin` flag is **unreachable from every API** (verified by
  tests that try to set it via registration and profile update). It can only
  be set with shell access via `create_admin.py` — server access is the trust
  boundary.
- Non-admins receive **404, not 403**, on all /admin routes: probing can't
  even confirm the surface exists.
- The flag is re-read from the database on **every request**, so revoking an
  admin (which also bumps their token version) takes effect immediately, not
  at token expiry.
- The whole surface is **read-only** (GET only) — it's monitoring, not remote
  control. Write verbs return 405.

## Multi-tenant isolation

- Every tenant-scoped row carries a `restaurant_id`.
- The tenant is read from the **verified JWT** (`rid` claim), never from the
  request body — so a caller cannot ask for another restaurant's data by
  changing a parameter.
- Every query is filtered through a `scoped()` helper; cross-tenant reads,
  writes, and even existence checks return `404`. This is verified by an
  isolation test suite.

## Subscription access control

- A `subscription_required` guard returns `402 Payment Required` on all POS
  endpoints when a subscription is expired/suspended, with a machine-readable
  reason so the app can route the owner to billing.
- Billing endpoints stay reachable while suspended, so an owner can always pay
  to restore access.

## Payment webhook security

The Paystack webhook can't be behind login (Paystack calls it), so it is
protected by:
1. An **HMAC-SHA512 signature** over the raw request body, keyed with
   `PAYSTACK_SECRET_KEY` and compared with `hmac.compare_digest`. An unsigned or
   mis-signed request gets `404`. This is strictly stronger than the secret path
   segment the old Daraja callback used: the secret never travels in a URL, so
   it can't leak through proxy logs, browser history or a referrer header, and
   the signature also proves the body wasn't tampered with in transit.
2. An optional **source-IP allowlist** (`PAYSTACK_WEBHOOK_ALLOWED_IPS`),
   deliberately empty by default — Paystack's published egress IPs can change,
   and a stale allowlist would silently `403` every real webhook.
3. **Idempotent processing** — even a valid replayed webhook can't double-apply.
   This matters more than it did with Daraja: Paystack *deliberately* re-sends
   until it gets a 200 (every 3 min ×4, then hourly for 72 h), so replays are
   routine rather than exceptional.
4. It never returns application errors to Paystack (always a clean ack), so
   internal state can't be probed through it.

## Transport & headers

- **HTTPS enforced** at the edge (Nginx) with HSTS; HTTP redirects to HTTPS.
- Security headers on every response: `X-Content-Type-Options: nosniff`,
  `X-Frame-Options: DENY`, a restrictive `Content-Security-Policy`,
  `Referrer-Policy: no-referrer`, and `Cache-Control: no-store`.
- **ProxyFix** so the app sees the real client IP behind the load balancer
  (accurate rate limiting and logs).

## Infrastructure

- App runs as a **non-root** user in the container.
- **Request size limits** and **connection caps** at the edge.
- Rate-limit state in **Redis**, shared across all replicas, so a limit means the
  same thing everywhere.
- Secrets come from environment variables, never committed.

---

## What this does NOT protect against (the honest list)

- **Compromised credentials / phishing.** If an owner's password is stolen, the
  attacker can act as them. Mitigate with strong passwords and (future) 2FA.
- **A malicious or compromised payment-gateway side.** We trust Paystack's
  responses; we validate what we can (webhook signature, idempotency) but cannot
  audit their infrastructure or Safaricom's behind them.
- **Zero-day vulnerabilities** in dependencies or the OS. Mitigate by keeping
  dependencies patched (`pip-audit`, Dependabot) and monitoring.
- **A determined insider with database access.** Protect DB credentials and
  restrict access.
- **DDoS at scale.** Edge rate limiting helps against modest abuse; volumetric
  attacks need a CDN/WAF (Cloudflare, etc.) in front.

## Performance & scale posture

- All analytics aggregates run **in the database** (SUM / GROUP BY / LIMIT on
  indexed columns), not in Python — endpoints scale with result size, not row
  count. Measured on 2,500 orders: sales report 219ms → 46ms, orders list
  685ms → 20ms.
- Composite indexes on `(restaurant_id, created_at)` and
  `(restaurant_id, status)` plus FK/time indexes on payments and order items
  back the hot queries.
- GZip compression for responses over 1 KB (~5-10x smaller JSON on the wire).
- Tuned asyncpg pool (size/overflow/recycle) for Postgres; stateless app +
  Redis-backed rate limits mean replicas scale horizontally behind Nginx.

## Recommended next steps for production

1. Put a WAF/CDN (e.g. Cloudflare) in front of Nginx.
2. Add **2FA** for owner/manager accounts.
3. Wire **error monitoring** (Sentry) and **structured audit logs**.
4. Run **`pip-audit`** in CI and enable Dependabot.
5. Rotate secrets via a manager (AWS Secrets Manager / Vault), not env files.
6. Add automated **database backups** with tested restores.
7. Rotate `PAYSTACK_SECRET_KEY` from the Paystack dashboard if it is ever
   exposed — it both authorises charges and verifies webhook signatures, so a
   leak lets an attacker forge payment confirmations.
