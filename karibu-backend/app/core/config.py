"""Application settings, loaded from environment via pydantic-settings.

Mirrors the Flask config surface so .env files carry over unchanged. Money is
in integer cents everywhere; never commit real secrets (use .env).
"""
import os
from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # The dotenv path is overridable so the test suite can point it at nothing.
    # Otherwise pytest reads the developer's real .env, and the suite's result
    # depends on an untracked, machine-specific file — locking ALLOWED_HOSTS to
    # the production hostname broke 33 of 34 tests on "Invalid host header",
    # with nothing in the diff to explain why.
    model_config = SettingsConfigDict(
        env_file=os.getenv("KARIBU_ENV_FILE", ".env"), extra="ignore"
    )

    ENV: str = "development"  # development | production | testing

    # The restaurant's wall clock. Storage stays UTC; this decides where a
    # DAY starts, which is what "sales today" actually means to an owner.
    TIMEZONE: str = "Africa/Nairobi"

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

    # --- Paystack (M-Pesa collection) ----------------------------------------
    # Subscriptions are charged by pushing an M-Pesa STK prompt through
    # Paystack rather than Safaricom's Daraja API directly: Daraja production
    # access needs a registered company and its own paybill/till, while
    # Paystack onboards unregistered "Starter" merchants.
    #
    # sk_test_… exercises the full flow against Paystack's test mode; sk_live_…
    # moves real money. The same key both authenticates outbound calls and
    # verifies inbound webhook signatures, so it is strictly secret.
    PAYSTACK_SECRET_KEY: str = ""
    # Safe to expose; only needed if a client-side Paystack SDK is added later.
    PAYSTACK_PUBLIC_KEY: str = ""
    # Paystack's documented webhook source IPs. Defence in depth only — the
    # HMAC signature is what actually authenticates a webhook, and this list
    # must be cleared if Paystack ever changes it, or every callback 403s.
    PAYSTACK_WEBHOOK_ALLOWED_IPS: str = ""


    # --- Rate limiting -------------------------------------------------------
    RATELIMIT_STORAGE_URI: str = "memory://"
    RATELIMIT_DEFAULT: str = "200/minute"
    RATELIMIT_LOGIN: str = "5/minute"

    # --- Cache (optional) ------------------------------------------------------
    # Cache-aside for hot, rarely-changing reads (menu browsing) so a busy
    # restaurant's ordering traffic doesn't all hit Postgres. Empty disables
    # caching outright (dev default) — every read just misses and hits the DB,
    # so nothing behaves differently without Redis configured. Use a separate
    # logical DB (e.g. /1) from RATELIMIT_STORAGE_URI's so a flush of one never
    # touches the other.
    CACHE_URL: str = ""
    CACHE_MENU_TTL_SECONDS: int = 60

    # --- Infra ---------------------------------------------------------------
    FORCE_HTTPS: bool = False
    ENABLE_SCHEDULER: bool = True

    # --- Email ---------------------------------------------------------------
    # Two transports. Pick with EMAIL_PROVIDER:
    #
    #   "smtp"  — classic SMTP (default). Works on a VPS, Docker, locally.
    #   "brevo" / "resend" / "sendgrid" — the provider's HTTPS API.
    #
    # Use an HTTP provider when the host blocks outbound SMTP: Render's free
    # tier (and many PaaS free tiers) firewall ports 25/465/587 to curb spam,
    # so smtplib fails with "[Errno 101] Network is unreachable" no matter how
    # correct the credentials are. Port 443 is always open, so the API works
    # where SMTP cannot. Set EMAIL_API_KEY when using one of these.
    EMAIL_PROVIDER: str = "smtp"
    EMAIL_API_KEY: str = ""

    # Log an SMTP reachability verdict at startup. Set true to find out whether
    # a host actually permits outbound SMTP when you have no shell to test from
    # (Render's shell is paid-only) — the result lands in the platform's log
    # viewer. Leave off normally; it adds a few seconds to boot.
    EMAIL_DIAGNOSE_ON_BOOT: bool = False

    # If SMTP_HOST is empty, emails are printed to the console instead of sent
    # (zero-setup dev). Set these for real delivery (Gmail/Zoho/any host).
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_USE_TLS: bool = True  # STARTTLS on 587; set False + port 465 for SSL
    # Gmail rewrites From to the authenticated mailbox, so this address must
    # match SMTP_USER (or a verified "Send mail as" alias on that account) or
    # the send is refused.
    EMAIL_FROM: str = "Karibu POS <karibupos@gmail.com>"
    # Socket timeout per send attempt, and how many times to retry a failed
    # send. A signup code that fails to send locks the user out of their brand
    # new account, so one transient blip is worth retrying before giving up.
    # Keep (retries + 1) * timeout + backoff comfortably under Gunicorn's
    # --timeout (60s): the signup send runs inline in the request, so a slow
    # mail host must not turn into a killed worker. Defaults worst-case ~21s.
    SMTP_TIMEOUT_SECONDS: int = 10
    EMAIL_SEND_RETRIES: int = 1
    # Hard ceiling on how long a signup request may wait for the email to go
    # out. Retries can otherwise stack up to (retries+1)*timeout, and a mobile
    # client gives up long before that — leaving the account committed but the
    # user staring at "unable to reach server", then blocked by a 409 when they
    # retry. Past this deadline we stop waiting and report email_sent=false;
    # the code is already saved, so /resend-confirmation still recovers it.
    EMAIL_SIGNUP_DEADLINE_SECONDS: int = 12

    # Base URL of the API (used for links in outgoing emails).
    PUBLIC_API_URL: str = "http://localhost:8000"
    # Base URL of the WEB APP. Confirmation links point here, not at the API —
    # the user must land on a page that can sign them in, not on a JSON
    # response. Set this to the Vercel deployment in production; a wrong value
    # produces links that 404 and blocks every new signup.
    PUBLIC_WEB_URL: str = "http://localhost:3000"
    # How long a confirmation link stays valid. Hours rather than minutes: the
    # token carries 256 bits of entropy and cannot be guessed, so the short
    # window a 6-digit code needed does not apply — and a link that dies while
    # someone is still finding the email just blocks the signup.
    EMAIL_LINK_HOURS: int = 24

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
        # Without a secret key, charges silently run in simulation mode — every
        # SQLITE IN PRODUCTION IS SILENT DATA LOSS, not a performance choice.
        #
        # On Render, Railway, Fly and every other container host the filesystem
        # is EPHEMERAL: each deploy, restart, or wake from idle gives a brand
        # new container, and a SQLite file living on it is simply gone. The app
        # then calls create_all, gets an empty schema, and starts up looking
        # perfectly healthy — so the failure presents as "every account
        # disappeared and I have to sign up again", with nothing in the logs.
        #
        # Refusing to boot converts that into an obvious failure at deploy time,
        # which is the difference between an hour of confusion and a message
        # naming the fix.
        if self.DATABASE_URL.startswith("sqlite"):
            problems.append(
                "DATABASE_URL is SQLite, which is stored on the container's "
                "disk and is DESTROYED on every deploy, restart and wake from "
                "idle — every account and order would be lost. Provision a "
                "Postgres database and set DATABASE_URL to its "
                "postgresql+asyncpg://… URL."
            )

        # subscription would appear to bill successfully while no money moves.
        if not self.PAYSTACK_SECRET_KEY.strip():
            problems.append(
                "PAYSTACK_SECRET_KEY is empty — charges would be simulated and "
                "no real payment would ever be collected"
            )
        elif not self.PAYSTACK_SECRET_KEY.startswith(("sk_test_", "sk_live_")):
            problems.append(
                "PAYSTACK_SECRET_KEY doesn't look like a secret key (expected "
                "sk_test_… or sk_live_…) — the public pk_… key can neither "
                "authorise charges nor verify webhook signatures"
            )
        if self.CORS_ORIGINS.strip() == "*":
            problems.append("CORS_ORIGINS is '*' (set your app's real origins)")
        if self.ALLOWED_HOSTS.strip() == "*":
            problems.append("ALLOWED_HOSTS is '*' (set your API hostname)")
        provider = self.EMAIL_PROVIDER.strip().lower()
        if provider not in {"smtp", "brevo", "resend", "sendgrid"}:
            problems.append(
                f"EMAIL_PROVIDER '{self.EMAIL_PROVIDER}' is not recognised "
                f"(use smtp, brevo, resend or sendgrid)"
            )
        # Without a working transport the app silently falls back to
        # console-logging signup codes. In dev that's a convenience; in
        # production it means every new owner is permanently locked out of the
        # account they just created, with nothing in the API response to say
        # so. Fail the boot instead.
        elif self.uses_email_api:
            if not self.EMAIL_API_KEY.strip():
                problems.append(
                    f"EMAIL_PROVIDER is '{provider}' but EMAIL_API_KEY is empty "
                    f"— signup verification codes would only be logged to the "
                    f"console, locking every new user out."
                )
        elif not self.SMTP_HOST.strip():
            problems.append(
                "SMTP_HOST is empty — signup verification codes would only be "
                "logged to the console, locking every new user out. Configure "
                "SMTP_HOST/SMTP_PORT/SMTP_USER/SMTP_PASSWORD/EMAIL_FROM, or "
                "set EMAIL_PROVIDER + EMAIL_API_KEY to send over HTTPS instead "
                "(required where outbound SMTP is blocked)."
            )
        elif not self.SMTP_USER.strip() or not self.SMTP_PASSWORD.strip():
            # Every hosted provider (Gmail, Zoho, Mailgun, Brevo, SendGrid)
            # requires auth; an unauthenticated relay here is almost always a
            # half-finished config that fails on the first real send.
            problems.append(
                "SMTP_HOST is set but SMTP_USER/SMTP_PASSWORD are empty "
                "(hosted mail providers all require authentication)"
            )
        if "@" not in self.EMAIL_FROM:
            problems.append("EMAIL_FROM is not a valid sender address")
        # Gmail refuses to send as an address the authenticated account doesn't
        # own, so a mismatch here fails on the first real send rather than at
        # boot — catch it now, while the log is still being read.
        elif (
            not self.uses_email_api
            and "gmail.com" in self.SMTP_HOST.lower()
            and self.SMTP_USER.strip()
            and self.email_from_address.lower() != self.SMTP_USER.strip().lower()
        ):
            problems.append(
                f"EMAIL_FROM ({self.email_from_address}) must match SMTP_USER "
                f"({self.SMTP_USER}) for Gmail, or be an alias verified on that "
                f"account under Settings -> Accounts -> 'Send mail as'"
            )
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
    def paystack_allowed_ips(self) -> list[str]:
        return [
            ip.strip()
            for ip in self.PAYSTACK_WEBHOOK_ALLOWED_IPS.split(",")
            if ip.strip()
        ]

    @property
    def is_sqlite(self) -> bool:
        return self.DATABASE_URL.startswith("sqlite")

    @property
    def uses_email_api(self) -> bool:
        """True when sending over a provider's HTTPS API instead of SMTP."""
        return self.EMAIL_PROVIDER.strip().lower() in {"brevo", "resend", "sendgrid"}

    @property
    def email_configured(self) -> bool:
        if self.uses_email_api:
            return bool(self.EMAIL_API_KEY.strip())
        return bool(self.SMTP_HOST)

    @property
    def email_from_address(self) -> str:
        """The bare address out of EMAIL_FROM ("Name <a@b.c>" -> "a@b.c")."""
        value = self.EMAIL_FROM.strip()
        if "<" in value and ">" in value:
            return value[value.rindex("<") + 1 : value.rindex(">")].strip()
        return value

    @property
    def email_from_name(self) -> str:
        """The display name out of EMAIL_FROM ("Name <a@b.c>" -> "Name")."""
        value = self.EMAIL_FROM.strip()
        if "<" in value:
            return value[: value.index("<")].strip().strip('"') or "Karibu POS"
        return "Karibu POS"

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