"""Application settings, loaded from environment via pydantic-settings.

Mirrors the Flask config surface so .env files carry over unchanged. Money is
in integer cents everywhere; never commit real secrets (use .env).
"""
from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    ENV: str = "development"  # development | production | testing

    # --- Security ------------------------------------------------------------
    SECRET_KEY: str = "dev-secret-change-me"
    JWT_SECRET_KEY: str = "dev-jwt-secret-change-me"
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_MINUTES: int = 30
    JWT_REFRESH_DAYS: int = 30
    JWT_ISSUER: str = "karibu-pos"
    JWT_AUDIENCE: str = "karibu-pos-api"

    # Comma-separated hostnames the API will serve (Host-header attack guard).
    # "*" disables the check (dev). In production set e.g. "api.karibupos.co.ke".
    ALLOWED_HOSTS: str = "*"

    # Account-level login lockout (defeats per-IP-rotating brute force).
    LOGIN_LOCKOUT_THRESHOLD: int = 5
    LOGIN_LOCKOUT_MINUTES: int = 15

    # Reject request bodies larger than this (bytes) at the app layer.
    MAX_BODY_BYTES: int = 1_000_000  # 1 MB

    # --- Database ------------------------------------------------------------
    # Async driver URLs. Dev falls back to a local SQLite file (zero setup);
    # set DATABASE_URL to an asyncpg URL for Postgres, e.g.
    #   postgresql+asyncpg://karibu:pass@localhost:5432/karibu_pos
    DATABASE_URL: str = "sqlite+aiosqlite:///./karibu_dev.db"

    # --- CORS ----------------------------------------------------------------
    CORS_ORIGINS: str = "*"

    # --- Subscription / billing ---------------------------------------------
    SUBSCRIPTION_PRICE_CENTS: int = 49900  # KSh 499.00
    SUBSCRIPTION_CURRENCY: str = "KES"
    TRIAL_DAYS: int = 14
    BILLING_PERIOD_DAYS: int = 30
    DUNNING_RETRY_HOURS: str = "0,24,72,120"
    CHARGE_STALE_MINUTES: int = 10

    # --- M-Pesa Daraja (STK Push) -------------------------------------------
    MPESA_ENV: str = "sandbox"  # sandbox | production
    MPESA_CONSUMER_KEY: str = ""
    MPESA_CONSUMER_SECRET: str = ""
    MPESA_SHORTCODE: str = "174379"
    MPESA_PASSKEY: str = ""
    MPESA_CALLBACK_URL: str = ""
    MPESA_CALLBACK_SECRET: str = "change-me-callback"
    MPESA_CALLBACK_ALLOWED_IPS: str = ""

    # --- Rate limiting -------------------------------------------------------
    RATELIMIT_STORAGE_URI: str = "memory://"
    RATELIMIT_DEFAULT: str = "200/minute"
    RATELIMIT_LOGIN: str = "5/minute"

    # --- Infra ---------------------------------------------------------------
    FORCE_HTTPS: bool = False
    ENABLE_SCHEDULER: bool = True

    # --- Email (SMTP) --------------------------------------------------------
    # If SMTP_HOST is empty, emails are printed to the console instead of sent
    # (zero-setup dev). Set these for real delivery (Gmail/Zoho/any host).
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_USE_TLS: bool = True  # STARTTLS on 587; set False + port 465 for SSL
    EMAIL_FROM: str = "Karibu POS <no-reply@karibupos.co.ke>"

    # Base URL the confirmation link points at (the API's public URL).
    PUBLIC_API_URL: str = "http://localhost:8000"
    # How long an email-confirmation token stays valid.
    EMAIL_TOKEN_HOURS: int = 48

    # Postgres connection pool (ignored on SQLite).
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20
    DB_POOL_RECYCLE_SECONDS: int = 1800

    # --- Derived helpers -----------------------------------------------------
    @property
    def allowed_hosts_list(self) -> list[str]:
        return [h.strip() for h in self.ALLOWED_HOSTS.split(",") if h.strip()]

    def assert_production_ready(self) -> None:
        """Refuse to boot in production with placeholder secrets.

        Shipping with default secrets is one of the most common real-world
        breach causes; failing fast here removes the whole class of mistake.
        """
        if self.ENV != "production":
            return
        problems = []
        if "change-me" in self.SECRET_KEY or len(self.SECRET_KEY) < 32:
            problems.append("SECRET_KEY is default/too short (need 32+ random chars)")
        if "change-me" in self.JWT_SECRET_KEY or len(self.JWT_SECRET_KEY) < 32:
            problems.append("JWT_SECRET_KEY is default/too short (need 32+ random chars)")
        if "change-me" in self.MPESA_CALLBACK_SECRET or len(self.MPESA_CALLBACK_SECRET) < 16:
            problems.append("MPESA_CALLBACK_SECRET is default/too short (need 16+ random chars)")
        if self.CORS_ORIGINS.strip() == "*":
            problems.append("CORS_ORIGINS is '*' (set your app's real origins)")
        if self.ALLOWED_HOSTS.strip() == "*":
            problems.append("ALLOWED_HOSTS is '*' (set your API hostname)")
        if problems:
            raise RuntimeError(
                "Refusing to start in production with insecure config:\n  - "
                + "\n  - ".join(problems)
            )

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    @property
    def dunning_retry_hours(self) -> list[int]:
        return [int(h) for h in self.DUNNING_RETRY_HOURS.split(",") if h.strip() != ""]

    @property
    def callback_allowed_ips(self) -> list[str]:
        return [ip.strip() for ip in self.MPESA_CALLBACK_ALLOWED_IPS.split(",") if ip.strip()]

    @property
    def is_sqlite(self) -> bool:
        return self.DATABASE_URL.startswith("sqlite")

    @property
    def email_configured(self) -> bool:
        return bool(self.SMTP_HOST)

    @field_validator("DATABASE_URL")
    @classmethod
    def _coerce_async_driver(cls, v: str) -> str:
        # Accept plain postgres/sqlite URLs and upgrade them to async drivers,
        # so a Flask-style DATABASE_URL still works here.
        if v.startswith("postgresql://"):
            return v.replace("postgresql://", "postgresql+asyncpg://", 1)
        if v.startswith("postgres://"):
            return v.replace("postgres://", "postgresql+asyncpg://", 1)
        if v.startswith("sqlite://") and "+aiosqlite" not in v:
            return v.replace("sqlite://", "sqlite+aiosqlite://", 1)
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
