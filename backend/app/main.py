"""FastAPI application entry point.

Assembles the app the way the Flask factory did: exception handlers that emit the
same JSON envelope, CORS, security headers, rate limiting, the background billing
scheduler (in the lifespan), and all routers. Interactive docs at /docs.
"""
import contextlib
import logging
import os

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from starlette.exceptions import HTTPException as StarletteHTTPException
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.core.config import settings
from app.core.database import Base, engine
from app.core.limiter import limiter
from app.core.security import APIError
from app.routers import admin, analytics, auth, billing, debts, menu, orders

logger = logging.getLogger("karibu")


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    # Fail fast if we're in production with placeholder secrets — refusing to
    # boot beats booting exploitable.
    settings.assert_production_ready()

    # Dev convenience: create tables if they don't exist. In production use
    # Alembic migrations instead.
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    scheduler = _maybe_start_scheduler()
    try:
        yield
    finally:
        if scheduler:
            scheduler.shutdown(wait=False)


app = FastAPI(
    title="Karibu POS API",
    version="1.0.0",
    description="Multi-tenant restaurant POS with M-Pesa subscription billing.",
    lifespan=lifespan,
)

# --- Rate limiting ----------------------------------------------------------
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# --- Trust the reverse proxy's forwarded headers -----------------------------
# Both deployment targets put exactly one trusted proxy directly in front of
# the app — nginx in docker-compose (the app containers are only `expose`d,
# never published), or the platform's own edge load balancer on Render/etc.
# Nothing else can reach the app directly, so trusting the immediate peer's
# X-Forwarded-For/-Proto is safe. Without this, Gunicorn/Uvicorn never
# rewrites request.client (it only trusts 127.0.0.1 by default), so it's
# always the proxy's IP — silently breaking the M-Pesa callback IP allowlist
# (every real Safaricom callback would 403) and collapsing the per-IP rate
# limiter into one shared bucket for every caller.
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")

# --- CORS -------------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list or ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- Compression -------------------------------------------------------------
# GZip large responses (analytics payloads, order lists). ~5-10x smaller on
# the wire for JSON, which matters a lot on Kenyan mobile data connections.
app.add_middleware(GZipMiddleware, minimum_size=1024)

# --- Host header guard --------------------------------------------------------
# Rejects requests whose Host isn't ours (cache-poisoning / password-reset
# poisoning class). "*" disables it for dev; set ALLOWED_HOSTS in production.
if settings.ALLOWED_HOSTS.strip() != "*":
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts_list
    )


# --- Request body size limit --------------------------------------------------
@app.middleware("http")
async def limit_body_size(request: Request, call_next):
    """Reject oversized bodies before they're read (memory-exhaustion guard).

    Nginx enforces this at the edge too; this covers direct-to-app access.
    """
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > settings.MAX_BODY_BYTES:
        return JSONResponse(
            status_code=413,
            content={"success": False, "message": "Request body too large"},
        )
    return await call_next(request)


# --- Security headers --------------------------------------------------------
# --- Security headers --------------------------------------------------------
@app.middleware("http")
async def security_headers(request: Request, call_next):
    try:
        response = await call_next(request)
    except RuntimeError as exc:
        # Starlette's BaseHTTPMiddleware raises "No response returned." when the
        # client goes away mid-request. Common on hosts that sleep: the app times
        # out during a cold start and disconnects before we finish. Nothing is
        # actually wrong, so don't emit a 500 and a stack trace for it.
        if "No response returned" in str(exc) and await request.is_disconnected():
            return Response(status_code=204)
        raise
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-XSS-Protection"] = "0"
    response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
    response.headers.setdefault("Cache-Control", "no-store")
    if settings.FORCE_HTTPS:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


# --- Exception handlers → uniform JSON envelope -----------------------------
@app.exception_handler(APIError)
async def handle_api_error(request: Request, exc: APIError):
    body = {"success": False, "message": exc.message}
    if exc.errors is not None:
        body["errors"] = exc.errors
    return JSONResponse(status_code=exc.status, content=body)


@app.exception_handler(RequestValidationError)
async def handle_validation(request: Request, exc: RequestValidationError):
    # Collapse Pydantic errors into a field->message map like the Flask API.
    errors = {}
    for err in exc.errors():
        loc = [p for p in err["loc"] if p not in ("body", "query", "path")]
        field = ".".join(str(p) for p in loc) or "body"
        errors[field] = err["msg"]
    return JSONResponse(
        status_code=422,
        content={"success": False, "message": "Validation failed", "errors": errors},
    )


@app.exception_handler(StarletteHTTPException)
async def handle_http(request: Request, exc: StarletteHTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"success": False, "message": exc.detail},
    )


@app.exception_handler(Exception)
async def handle_unexpected(request: Request, exc: Exception):
    logger.exception("Unhandled exception: %s", exc)
    message = str(exc) if settings.ENV == "development" else "Internal server error"
    return JSONResponse(status_code=500, content={"success": False, "message": message})


# --- Health -----------------------------------------------------------------
@app.get("/api/health", tags=["health"])
async def health():
    return {"success": True, "message": "Karibu POS API is running", "data": {"status": "ok"}}


# --- Routers ----------------------------------------------------------------
app.include_router(admin.router)
app.include_router(auth.router)
app.include_router(billing.router)
app.include_router(debts.router)
app.include_router(menu.router)
app.include_router(orders.router)
app.include_router(analytics.router)


# --- Scheduler --------------------------------------------------------------
def _maybe_start_scheduler():
    """Start the billing sweep if enabled. Run in exactly one instance in a
    load-balanced deployment (ENABLE_SCHEDULER=true there, false elsewhere)."""
    if not settings.ENABLE_SCHEDULER:
        return None
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
    except ImportError:
        logger.warning("APScheduler not installed — billing sweep disabled")
        return None

    from app.core.database import AsyncSessionLocal
    from app.services import billing as billing_service

    scheduler = AsyncIOScheduler()

    async def _sweep():
        async with AsyncSessionLocal() as db:
            try:
                stats = await billing_service.run_billing_sweep(db)
                if stats.get("charged") or stats.get("stale_failed"):
                    logger.info("Billing sweep: %s", stats)
            except Exception as exc:
                logger.exception("Billing sweep crashed: %s", exc)

    scheduler.add_job(_sweep, "interval", minutes=15, id="billing_sweep")
    scheduler.start()
    logger.info("Billing scheduler started")
    return scheduler
