# Karibu POS — Backend (FastAPI)

Async FastAPI + SQLAlchemy 2.0 (async) + PostgreSQL (SQLite in dev), with JWT
auth, multi-tenancy, and idempotent M-Pesa subscription billing.

## Why FastAPI here

- **Async M-Pesa calls** (httpx) don't block workers — the concrete win for a
  payments backend that calls an external gateway.
- **Pydantic** validates every request body/query and returns clean 422s.
- **`/docs`** — interactive OpenAPI documentation, generated automatically.

## Structure

```
backend/
├── app/
│   ├── main.py            FastAPI app: handlers, middleware, lifespan, routers
│   ├── core/
│   │   ├── config.py      pydantic-settings (env-driven)
│   │   ├── database.py    async engine + get_db dependency
│   │   ├── security.py    JWT create/decode, APIError
│   │   ├── dependencies.py  auth / roles / tenant / subscription deps
│   │   ├── limiter.py     slowapi rate limiter
│   │   └── serializers.py ORM → response dicts (computed fields)
│   ├── models/            async SQLAlchemy models
│   ├── schemas/           Pydantic request/response models
│   ├── routers/           auth, billing, menu, orders, analytics
│   └── services/          billing engine + async M-Pesa client
├── seed.py                async demo-data seeder
├── requirements.txt
└── Dockerfile
```

## Running

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python seed.py                 # create tables + demo data
uvicorn app.main:app --reload  # http://localhost:8000  (docs at /docs)

# production (async workers):
gunicorn app.main:app --worker-class uvicorn.workers.UvicornWorker \
  --workers 4 --bind 0.0.0.0:8000
```

Demo login (from seed): `demo@karibupos.co.ke` / `karibu12345`

Dev uses a local **SQLite** file (via aiosqlite) with zero setup. Set
`DATABASE_URL` to an asyncpg Postgres URL for production (plain `postgresql://`
URLs are auto-upgraded to the async driver).

## Notes on the async port

- Money is integer cents; VAT 16%.
- Response envelope `{ success, message, data }` is identical to before, so the
  existing React Native frontend works unchanged.
- The billing correctness guarantees (idempotency key, DB unique index on open
  charges, row locking, callback dedup, dunning) are preserved — they live at the
  SQLAlchemy/DB level. See `../SECURITY.md` and `../BILLING.md`.
- Implicit lazy-loading is avoided (async requires it): relationships are
  eager-loaded with `selectinload` or explicit `db.get`.
- `bcrypt` is pinned to 4.0.1 for passlib compatibility.

## Migrations

The app calls `create_all` on startup for dev convenience. For production schema
management, wire Alembic (async template) — `alembic` is already a dependency.
