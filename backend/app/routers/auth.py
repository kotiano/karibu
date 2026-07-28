"""Authentication routes: register, login, refresh, me, email verification."""
import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Request
from sqlalchemy import select

from app.core.audit import record_audit
from app.core.config import settings
from app.core.database import get_db
from app.core.dependencies import CurrentUser, DbDep
from app.core.limiter import limiter
from app.core.passwords import password_problem
from app.core.security import (
    APIError,
    create_access_token,
    create_refresh_token,
    decode_token,
    generate_otp_code,
    hash_otp_code,
    token_claims,
    verify_otp_code,
)
from app.core.serializers import subscription_dict
from app.models import AuditAction, Restaurant, Subscription, User, UserRole
from app.schemas.auth import (
    LoginRequest,
    RegisterRequest,
    UpdateProfileRequest,
    UserOut,
    VerifyEmailRequest,
)
from app.schemas.common import ok
from app.services import billing, email as email_service

router = APIRouter(prefix="/api/auth", tags=["auth"])
logger = logging.getLogger("karibu.auth")


def _new_otp() -> tuple[str, str, datetime]:
    """Return (plaintext code, hash to store, expiry). Only the hash is persisted."""
    code = generate_otp_code()
    expires = datetime.utcnow() + timedelta(minutes=settings.EMAIL_OTP_MINUTES)
    return code, hash_otp_code(code), expires


def _set_otp(user: User) -> str:
    code, code_hash, expires = _new_otp()
    user.email_token = code_hash
    user.email_token_expires = expires
    user.email_token_attempts = 0
    return code


async def _send_confirmation_code(user: User, code: str) -> bool:
    """Email the code. Returns whether it was actually delivered to SMTP."""
    subject, html, text = email_service.confirm_email_code(user.full_name, code)
    sent = await email_service.send_email(user.email, subject, html, text)
    if not sent:
        logger.error(
            "Verification code for %s could not be emailed — the account "
            "cannot log in until a code is delivered (POST /auth/resend-confirmation)",
            user.email,
        )
    return sent


def _issue_tokens(user: User) -> dict:
    extra = token_claims(user)
    return {
        "access_token": create_access_token(user.id, extra),
        "refresh_token": create_refresh_token(user.id, extra),
    }


@router.post("/register", status_code=201)
@limiter.limit(settings.RATELIMIT_LOGIN)
async def register(request: Request, body: RegisterRequest, db: DbDep):
    """Register a restaurant owner: creates the restaurant + a 14-day trial."""
    email = body.email.strip().lower()

    problem = password_problem(body.password)
    if problem:
        raise APIError(problem, status=422, errors={"password": problem})

    exists = await db.execute(select(User).where(User.email == email))
    if exists.scalar_one_or_none():
        raise APIError("An account with this email already exists", status=409)

    restaurant = Restaurant(name=body.restaurant_name.strip())
    db.add(restaurant)
    await db.flush()

    user = User(
        full_name=body.full_name.strip(),
        email=email,
        phone=body.phone,
        role=UserRole.OWNER,
        branch_name=body.branch_name or "Main Branch",
        restaurant_id=restaurant.id,
    )
    user.set_password(body.password)
    # Email starts unconfirmed; login is blocked until the code is verified.
    code = _set_otp(user)
    db.add(user)

    billing_phone = body.billing_phone or body.phone
    subscription = await billing.start_trial(db, restaurant, billing_phone)

    await record_audit(
        db,
        action=AuditAction.USER_REGISTERED,
        summary=f"{email} registered restaurant '{restaurant.name}'",
        actor_email=email,
        restaurant_id=restaurant.id,
        restaurant_name=restaurant.name,
        request=request,
    )

    await db.commit()
    await db.refresh(user)

    # Fire the verification code email (console-logged in dev).
    sent = await _send_confirmation_code(user, code)

    # No login tokens — the account must verify its email code first.
    # `email_sent` lets the client tell "check your inbox" apart from "we
    # couldn't send it", instead of stranding the user on a screen waiting for
    # a code that is never coming. The account and its code are already
    # committed, so /auth/resend-confirmation recovers it once mail is healthy.
    return ok(
        {
            "email": user.email,
            "confirmation_required": True,
            "email_sent": sent,
        },
        message=(
            "Almost there — check your email for a verification code, "
            "then confirm it in the app. Your 14-day free trial is ready."
        )
        if sent
        else (
            "Your account is ready, but we couldn't send the verification "
            "email just now. Tap resend in a moment to get your code."
        ),
    )


@router.post("/login")
@limiter.limit(settings.RATELIMIT_LOGIN)
async def login(request: Request, body: LoginRequest, db: DbDep):
    """Authenticate with email + password."""
    email = body.email.strip().lower()

    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()

    # Uniform error + always hash to avoid leaking which emails exist.
    if not user:
        User().set_password(body.password)
        raise APIError("Invalid email or password", status=401)

    # Account-level lockout: stops brute force even from rotating IPs.
    now = datetime.utcnow()
    if user.locked_until and now < user.locked_until:
        raise APIError(
            "Too many failed attempts. Try again in a few minutes.", status=429
        )

    if not user.check_password(body.password):
        user.failed_logins += 1
        if user.failed_logins >= settings.LOGIN_LOCKOUT_THRESHOLD:
            user.locked_until = now + timedelta(minutes=settings.LOGIN_LOCKOUT_MINUTES)
            user.failed_logins = 0
        await db.commit()
        raise APIError("Invalid email or password", status=401)
    if not user.is_active:
        raise APIError("This account has been disabled", status=403)

    # Email ownership must be confirmed before first login. Platform admins
    # (created via CLI) are exempt.
    if not user.email_confirmed and not user.is_platform_admin:
        raise APIError(
            "Please verify your email first. Check your inbox for the code.",
            status=403,
            errors={"email_confirmation": "required"},
        )

    # Successful login clears any lockout state.
    if user.failed_logins or user.locked_until:
        user.failed_logins = 0
        user.locked_until = None

    await record_audit(
        db,
        action=AuditAction.USER_LOGIN,
        summary=f"{user.email} signed in",
        actor_id=user.id,
        actor_email=user.email,
        restaurant_id=user.restaurant_id,
        request=request,
    )
    await db.commit()

    sub_res = await db.execute(
        select(Subscription).where(Subscription.restaurant_id == user.restaurant_id)
    )
    sub = sub_res.scalar_one_or_none()

    return ok(
        {
            "user": UserOut.model_validate(user).model_dump(),
            "subscription": subscription_dict(sub) if sub else None,
            "tokens": _issue_tokens(user),
        },
        message="Signed in successfully",
    )


@router.post("/verify-email")
@limiter.limit("10/minute")
async def verify_email(request: Request, body: VerifyEmailRequest, db: DbDep):
    """Confirm an email with the 6-digit code sent at signup (or resend).

    Verified in-app rather than via a clicked link. Wrong attempts are counted
    on the account itself — after EMAIL_OTP_MAX_ATTEMPTS the code is dead
    regardless of the caller's IP, closing the "just try from another IP"
    brute-force gap that a pure per-IP rate limit would leave open.
    """
    email = body.email.strip().lower()
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()

    generic_invalid = APIError(
        "Invalid or expired code", status=400, errors={"code": "invalid"}
    )

    if not user:
        raise generic_invalid
    if user.email_confirmed:
        return ok(message="Your email is already confirmed — you can sign in.")
    if (
        not user.email_token
        or not user.email_token_expires
        or datetime.utcnow() > user.email_token_expires
    ):
        raise APIError(
            "That code has expired. Request a new one.",
            status=400,
            errors={"code": "expired"},
        )
    if user.email_token_attempts >= settings.EMAIL_OTP_MAX_ATTEMPTS:
        raise APIError(
            "Too many incorrect attempts. Request a new code.",
            status=429,
            errors={"code": "locked"},
        )

    if not verify_otp_code(body.code, user.email_token):
        user.email_token_attempts += 1
        await db.commit()
        raise generic_invalid

    user.email_confirmed = True
    user.email_token = None
    user.email_token_expires = None
    user.email_token_attempts = 0
    await record_audit(
        db,
        action=AuditAction.EMAIL_CONFIRMED,
        summary=f"{user.email} confirmed their email",
        actor_id=user.id,
        actor_email=user.email,
        restaurant_id=user.restaurant_id,
        request=request,
    )
    await db.commit()

    sub_res = await db.execute(
        select(Subscription).where(Subscription.restaurant_id == user.restaurant_id)
    )
    sub = sub_res.scalar_one_or_none()

    # Verifying the code proves email ownership just as signing in would, so
    # issue a session immediately instead of forcing a separate login step.
    return ok(
        {
            "user": UserOut.model_validate(user).model_dump(),
            "subscription": subscription_dict(sub) if sub else None,
            "tokens": _issue_tokens(user),
        },
        message="Email confirmed — you're in!",
    )


@router.post("/resend-confirmation")
@limiter.limit("3/minute")
async def resend_confirmation(request: Request, body: LoginRequest, db: DbDep):
    """Re-send the verification code. Takes email+password so only the account
    owner can trigger it (and we don't leak whether an email exists)."""
    email = body.email.strip().lower()
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()

    # Always return the same response regardless, to avoid email enumeration.
    generic = ok(message="If that account exists and is unconfirmed, a new code is on its way.")
    if not user or not user.check_password(body.password):
        return generic
    if user.email_confirmed:
        return generic

    code = _set_otp(user)
    await db.commit()
    await _send_confirmation_code(user, code)
    return generic


@router.post("/refresh")
async def refresh(request: Request, db: DbDep):
    """Exchange a valid refresh token for a fresh access token."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise APIError("Authorization token required", status=401)
    payload = decode_token(auth.split(" ", 1)[1])
    if payload.get("type") != "refresh":
        raise APIError("Invalid token type", status=401)

    user = await db.get(User, payload.get("sub"))
    if not user or not user.is_active:
        raise APIError("Account not found", status=401)
    if payload.get("tv") != user.token_version:
        raise APIError("Session expired, please sign in again", status=401)

    token = create_access_token(user.id, token_claims(user))
    return ok({"access_token": token})


@router.get("/me")
async def me(user: CurrentUser):
    return ok({"user": UserOut.model_validate(user).model_dump()})


@router.patch("/me")
async def update_me(body: UpdateProfileRequest, user: CurrentUser, db: DbDep):
    if body.full_name is not None:
        user.full_name = body.full_name
    if body.phone is not None:
        user.phone = body.phone
    if body.avatar_url is not None:
        user.avatar_url = body.avatar_url
    if body.branch_name is not None:
        user.branch_name = body.branch_name
    if body.password:
        # Changing a password requires proving knowledge of the current one —
        # a valid session token alone must not be enough to reset it.
        if not body.current_password:
            raise APIError(
                "Current password is required to set a new one",
                status=422,
                errors={"current_password": "required"},
            )
        if not user.check_password(body.current_password):
            raise APIError(
                "Current password is incorrect",
                status=403,
                errors={"current_password": "incorrect"},
            )
        problem = password_problem(body.password)
        if problem:
            raise APIError(problem, status=422, errors={"password": problem})
        user.set_password(body.password)
        user.token_version += 1  # invalidate old tokens

    await db.commit()
    await db.refresh(user)
    return ok({"user": UserOut.model_validate(user).model_dump()}, message="Profile updated")
