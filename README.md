# Karibu POS

An intelligent, multi-tenant point-of-sale SaaS for Kenyan restaurants — take
orders, run the kitchen, record M-Pesa / cash / card payments, and track sales
in real time. Sold as a subscription: **KSh 500/month via M-Pesa, after a 14-day
free trial**.

Built as a **React Native (Expo + TypeScript + NativeWind)** app talking to a
**Python FastAPI + PostgreSQL (async)** REST API, designed to run **load-balanced** behind
Nginx with defense-in-depth security.

```
karibu-pos/
├── backend/       FastAPI (async) — auth, tenancy, menu, orders, payments, analytics, billing
├── backend-flask/ Original Flask implementation (kept for reference)
├── frontend/    Expo React Native app
└── deploy/      Nginx + docker-compose (load-balanced production stack)
```

---

## What's inside

**POS**
- Menu (categories, items, availability), ordering with live 16% VAT,
  kitchen status flow, split M-Pesa/cash/card payments, analytics dashboard.

**Multi-tenant SaaS**
- Every restaurant is an isolated tenant; staff share one subscription.
- Owner signup creates the restaurant + a 14-day trial automatically.

**Subscription billing (M-Pesa STK Push)**
- KSh 500/month, 14-day trial, automatic renewal, dunning with retries, then
  suspension. Built to **never double-charge** (see `BILLING.md` + `SECURITY.md`).

**Load balancing**
- Stateless app → run N replicas behind Nginx; Redis-backed rate limiting shared
  across them; billing jobs run in exactly one instance. See `deploy/`.

**Security (defense-in-depth)**
- JWT with token versioning, password hashing, login rate limiting, tenant
  isolation, subscription gating, verified idempotent payment callbacks, HTTPS +
  security headers, non-root containers. Full, honest write-up in `SECURITY.md`.

---

## Quick start (development)

### 1. Backend

```bash
cd backend
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                    # then edit secrets
python seed.py                                          # tables + demo data
uvicorn app.main:app --reload                           # http://localhost:8000
```

Interactive API docs (Swagger UI) are at **http://localhost:8000/docs**.

Demo login (from the seed):

```
demo@karibupos.co.ke  /  karibu12345
```

The demo restaurant has an **active** subscription so you get full access
immediately. New signups start a **14-day trial**. Runs on local **SQLite** with
zero setup; point `DATABASE_URL` at Postgres for production.

M-Pesa works **without real keys** in dev — the STK push is simulated. To
complete a simulated payment, POST a callback (see `BILLING.md`).

### 2. Frontend

```bash
cd frontend
npm install
npm start            # Expo — press i (iOS), a (Android), or scan the QR
```

Point the app at your API in `frontend/app.json` → `expo.extra.apiUrl`. On a
physical device use your machine's LAN IP, not `localhost`.

---

## Production (load-balanced)

```bash
cd deploy
# set SECRET_KEY, JWT_SECRET_KEY, MPESA_CALLBACK_SECRET, DB_PASSWORD, and
# (for real payments) the MPESA_* Daraja credentials in your environment/.env
docker compose -f docker-compose.prod.yml up --build
```

This brings up Postgres, Redis, **3 app replicas + 1 scheduler**, and Nginx
(TLS termination + load balancing). Drop your certs in `deploy/certs/` and set
`server_name` in `deploy/nginx.conf`. Details in `deploy/` and `SECURITY.md`.

---

## API overview

Envelope on every response: `{ "success", "message", "data" }`.
All routes except `/auth/*`, `/health`, and the M-Pesa callback require
`Authorization: Bearer <token>`. POS routes also require an active subscription
(else `402`).

| Area | Method | Path |
|------|--------|------|
| Auth | POST | `/api/auth/register` (owner + restaurant + trial) |
| Auth | POST | `/api/auth/login` · `/api/auth/refresh` |
| Auth | GET/PATCH | `/api/auth/me` |
| Billing | GET | `/api/billing/subscription` |
| Billing | POST | `/api/billing/pay` (idempotent charge) |
| Billing | GET | `/api/billing/charges` |
| Billing | POST | `/api/billing/callback/<secret>` (Safaricom) |
| Menu | GET | `/api/menu/categories` · `/api/menu/items` |
| Menu | POST/PATCH/DELETE | `/api/menu/items` (manager+) |
| Orders | GET/POST | `/api/orders` |
| Orders | PATCH | `/api/orders/:id/status` |
| Orders | POST | `/api/orders/:id/payments` |
| Analytics | GET | `/api/analytics/dashboard` · `/api/analytics/sales` |

Full payloads in `backend/README.md`; billing detail in `BILLING.md`.

---

## Documentation

- `SECURITY.md` — security posture, billing integrity, and an honest list of what
  is and isn't protected.
- `BILLING.md` — subscription lifecycle, dunning, M-Pesa integration, testing.
- `backend/README.md` — API payloads and backend structure.
- `frontend/DESIGN_NOTES.md` — the UI revamp rationale.
