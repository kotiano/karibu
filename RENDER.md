# Deploying the Karibu POS backend to Render

This gets the FastAPI backend running on Render with a managed Postgres database.
Render handles TLS and load-balancing, so there's no nginx/Redis to run — the
container just serves the app on Render's `$PORT`, and runs Alembic migrations on
every deploy.

Two files make this work:
- `backend/Dockerfile.render` — binds to `$PORT`, runs `alembic upgrade head`
  then starts Gunicorn.
- `render.yaml` — a blueprint that provisions the web service + Postgres and
  scaffolds the env vars.

---

## Option A — Blueprint (recommended, one step)

1. Push this project to a GitHub repo.
2. In Render: **New + → Blueprint**, connect the repo. Render reads `render.yaml`
   and creates the `karibu-api` web service **and** the `karibu-postgres`
   database, wiring `DATABASE_URL` automatically.
3. The first deploy will **fail health checks on purpose** — see
   "The hostname bootstrap" below. Do that step, and it goes green.

## Option B — Manual web service

1. **New + → Postgres**: create a database (name it `karibu_pos`). Copy its
   **Internal Connection String**.
2. **New + → Web Service**, connect the repo. Settings:
   - Runtime: **Docker**
   - Dockerfile path: `backend/Dockerfile.render`
   - Docker context: `backend`
   - Health check path: `/api/health`
3. Add the env vars from the table below.

---

## The hostname bootstrap (IMPORTANT — do this or it crash-loops)

The app **refuses to boot in production** if `ALLOWED_HOSTS` or `CORS_ORIGINS`
are unset (this is a deliberate security guard — it blocks the most common
breach cause). But you don't know your Render hostname until the service is
created. So it's a two-step:

1. Let the first deploy run and **fail** its health check. In the service's
   page, note the URL Render assigned, e.g. `karibu-api.onrender.com`.
2. Go to **Environment** and set:
   - `ALLOWED_HOSTS` = `karibu-api.onrender.com`
   - `CORS_ORIGINS` = `https://karibu-api.onrender.com`
     (for a mobile-only app you can use the Render host here; add your web
     landing-page origin too if you have one, comma-separated)
3. Save — Render redeploys, the guard passes, health check goes green.

---

## Environment variables

| Key | Value | Set by |
|---|---|---|
| `DATABASE_URL` | (Postgres connection string) | Auto (blueprint) or paste manually |
| `SECRET_KEY` | 32+ random chars | `generateValue` / `openssl rand -hex 32` |
| `JWT_SECRET_KEY` | 32+ random chars | `generateValue` / `openssl rand -hex 32` |
| `ENV` | `production` | You |
| `FORCE_HTTPS` | `true` | You |
| `ENABLE_SCHEDULER` | `true` | You (single instance runs the billing sweep) |
| `ALLOWED_HOSTS` | your `.onrender.com` host | You (after step 1 above) |
| `CORS_ORIGINS` | `https://<host>` (not `*`) | You (after step 1 above) |
| `EMAIL_PROVIDER` | `brevo` | Blueprint (already set) |
| `EMAIL_API_KEY` | Brevo API key (`xkeysib-…`) | **You — the only email secret** |
| `EMAIL_FROM` | `Karibu POS <karibupos@gmail.com>` | Blueprint (already set) |
| `PAYSTACK_SECRET_KEY` | `sk_test_…`, then `sk_live_…` | **You — the only payment secret** |
| `PAYSTACK_PUBLIC_KEY` | `pk_test_…` / `pk_live_…` | You (optional) |

**Paystack.** Start with the *test* secret key: the whole billing flow runs, no
money moves. The app refuses to boot with this unset, because an empty key
silently simulates every charge and no payment would ever arrive.

Set the webhook URL in **Paystack → Settings → API Keys & Webhooks** to
`https://<your-host>/api/billing/webhook/paystack`. Nothing secret goes in the
URL — the endpoint verifies an HMAC-SHA512 signature over the request body. Test
and live mode have separate webhook URLs; setting one does not set the other.

Leave `PAYSTACK_WEBHOOK_ALLOWED_IPS` empty. Paystack's published egress IPs can
change, and a stale allowlist 403s every real webhook — payments would succeed
while subscriptions silently never activate.

---

## Email — required, or nobody can sign up

Signup emails a **6-digit verification code** and login is blocked until it's
confirmed. If email is broken, every new owner is stranded on the "enter your
code" screen forever. The app therefore **refuses to boot in production**
without a working transport — a crash-loop with a clear log line, rather than a
silently broken signup.

> **Do not use SMTP on Render's free tier.** It is firewalled. Measured from a
> free instance:
>
> ```
> IPv4 142.251.127.109:587 -> TimeoutError: timed out
> IPv6 2a00:1450:4001:c21::6d:587 -> Network is unreachable
> ```
>
> IPv4 *timing out* rather than being refused is a firewall silently dropping
> the packets. Correct credentials fail identically — this is a network block
> outside the application, and no port or setting works around it. Port 443 is
> open, so we send over Brevo's HTTPS API instead.

Mail goes out from **karibupos@gmail.com** via Brevo. `EMAIL_PROVIDER` and
`EMAIL_FROM` are set in `render.yaml`, so there's one secret to supply.

### 1. Set up Brevo

Free tier is 300 emails/day, and unlike most providers it lets you verify a
single Gmail address as a sender without owning a domain.

1. Sign up at [brevo.com](https://www.brevo.com) — free, no card
2. **Senders, Domains & Dedicated IPs → Senders → Add a sender** →
   `karibupos@gmail.com`. Brevo sends a confirmation link; **click it**.
   Sends from an unverified address fail with HTTP 400.
3. **SMTP & API → API Keys → Generate a new API key** ("Karibu POS")
4. Copy the key — it starts with `xkeysib-` and is shown once

### 2. Set it in the Render dashboard

Environment tab → add **`EMAIL_API_KEY`** → save (this redeploys).

Delete the now-unused `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`,
`SMTP_USE_TLS` and `EMAIL_DIAGNOSE_ON_BOOT` while you're there — they're
ignored when `EMAIL_PROVIDER=brevo`.

### 3. Confirm it works

```bash
curl https://<your-host>/api/health
# → {"success":true,...,"data":{"status":"ok","email":true}}
```

Then register a throwaway account in the app. The response carries
`"email_sent": true|false`, so you get a direct answer without shell access.
If it's `false`, the **Logs** tab shows the provider's error:

- **HTTP 401/403** — the API key is wrong or revoked
- **HTTP 400** — `EMAIL_FROM` isn't a verified sender (step 1.2). Most common.

Anyone who registered while email was broken still has their account and can
recover with `POST /api/auth/resend-confirmation` (email + password).

**Deliverability:** sending as `@gmail.com` through a third party is allowed
but scores worse than a domain you own — expect some codes in spam at first.
Once `karibupos.co.ke` exists, verify the *domain* in Brevo (it supplies
DKIM/SPF records) and switch `EMAIL_FROM` to `no-reply@karibupos.co.ke`.

### Moving to a paid instance or a VPS later?

Outbound SMTP works there. Set `EMAIL_PROVIDER=smtp` plus `SMTP_HOST`/`_PORT`/
`_USER`/`_PASSWORD`/`_USE_TLS`, and set `EMAIL_DIAGNOSE_ON_BOOT=true` for one
deploy — the app logs at startup whether the host can reach the mail server:

```
[smtp-diagnose] SUCCESS — outbound SMTP works from this host.
[smtp-diagnose] BLOCKED  — cannot open a TCP connection to ...
```

### 3. Prove it actually sends

The Shell tab is **paid-only**, so on the free tier test from your own machine
instead — same code, same provider, and the Brevo API is reachable from
anywhere:

```bash
cd backend
# put EMAIL_PROVIDER=brevo and EMAIL_API_KEY=... in backend/.env first
.venv/bin/python send_test_email.py you@example.com
```

Check the inbox *and* the spam folder.

**HTTP 401/403** — the API key is wrong or revoked.
**HTTP 400** — `EMAIL_FROM` isn't a verified sender on the Brevo account
(step 1.2). This is the most common failure.

### 4. Confirm end-to-end on Render

Without shell access, use these three instead:

```bash
curl https://<your-host>/api/health
# → {"success":true,...,"data":{"status":"ok","email":true}}
```

`"email": true` means a transport is wired up. Then register a throwaway
account in the app: the register response returns `"email_sent": true|false`,
which tells you directly whether the send succeeded. If it's `false`, the
**Logs** tab shows the `Giving up sending email to…` line with the provider's
error.

Users who registered while mail was broken are recoverable: they keep their
account and can call `POST /api/auth/resend-confirmation` (email + password) to
get a fresh code once email is healthy. That covers anyone who signed up during
the SMTP outage.

---

## After the first successful deploy

Create your platform admin (the DB starts empty — restaurants self-register):

Render dashboard → your service → **Shell**:
```bash
python create_admin.py --email you@example.com --password 'a-strong-password'
```

Verify it's live:
- `https://<your-host>/api/health` → `{"success": true, ...}`
- `https://<your-host>/docs` → the API docs

---

## Migrations going forward

The Dockerfile runs `alembic upgrade head` on every deploy, so when you add a
migration (`alembic revision --autogenerate -m "..."` on your machine, committed
to the repo), it applies automatically on the next Render deploy — no data loss.

## Scaling note

The starter tier is a single instance, so the in-process rate limiter
(`memory://`) and the in-process scheduler are fine. If you scale to multiple
instances later:
- Add a Redis instance and set `RATELIMIT_STORAGE_URI` to its URL (so limits are
  shared across instances).
- Set `ENABLE_SCHEDULER=false` on the web service and run the scheduler as a
  separate Render **Background Worker** (so the billing sweep fires once, not
  once per instance).

## Free tier caveat

Render's free web services **spin down after inactivity** and cold-start on the
next request (~30s delay), and free Postgres expires after 90 days. For a real
POS that customers depend on, use at least the **starter** paid tiers.
