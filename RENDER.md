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
| `MPESA_CALLBACK_SECRET` | 16+ random chars | `generateValue` / `openssl rand -hex 24` |
| `ENV` | `production` | You |
| `FORCE_HTTPS` | `true` | You |
| `ENABLE_SCHEDULER` | `true` | You (single instance runs the billing sweep) |
| `ALLOWED_HOSTS` | your `.onrender.com` host | You (after step 1 above) |
| `CORS_ORIGINS` | `https://<host>` (not `*`) | You (after step 1 above) |
| `MPESA_ENV` | `sandbox` (until go-live) | You |

M-Pesa production vars (`MPESA_CONSUMER_KEY`, `_SECRET`, `_SHORTCODE`,
`_PASSKEY`, `MPESA_CALLBACK_URL`) — leave unset until you go live; keep
`MPESA_ENV=sandbox` so billing runs in simulation. When ready, set
`MPESA_CALLBACK_URL` to `https://<your-host>/api/billing/mpesa/callback` and
register it with Safaricom.

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
