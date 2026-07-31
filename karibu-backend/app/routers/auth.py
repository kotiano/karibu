"""Authentication routes: register, login, refresh, me, email verification."""
import asyncio
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
    generate_link_token,
    hash_link_token,
    token_claims,
    verify_link_token,
)
from app.core.serializers import subscription_dict
from app.models import AuditAction, Restaurant, Subscription, User, UserRole
from app.schemas.auth import (
    ForgotPasswordRequest,
    LoginRequest,
    RegisterRequest,
    ResetPasswordRequest,
    UpdateProfileRequest,
    UserOut,
    VerifyEmailRequest,
)
from app.schemas.common import ok
from app.services import billing, email as email_service

router = APIRouter(prefix="/api/auth", tags=["auth"])
logger = logging.getLogger("karibu.auth")


def _set_confirmation_token(user: User) -> str:
    """Issue a fresh confirmation token. Returns the plaintext; stores only the
    hash, so a database leak never yields a working confirmation link."""
    token = generate_link_token()
    user.email_token = hash_link_token(token)
    user.email_token_expires = datetime.utcnow() + timedelta(
        hours=settings.EMAIL_LINK_HOURS
    )
    user.email_token_attempts = 0
    return token


async def _send_confirmation_link(user: User, code: str) -> bool:
    """Email the code, bounded by EMAIL_SIGNUP_DEADLINE_SECONDS.

    Returns whether it was actually delivered. A blocked or slow mail server
    must never hold the signup response longer than a mobile client will wait.
    """
    url = f"{settings.PUBLIC_WEB_URL.rstrip('/')}/verify?token={code}"
    subject, html, text = email_service.confirm_email_link(user.full_name, url)
    try:
        sent = await asyncio.wait_for(
            email_service.send_email(user.email, subject, html, text),
            timeout=settings.EMAIL_SIGNUP_DEADLINE_SECONDS,
        )
    except asyncio.TimeoutError:
        sent = False
        logger.error(
            "Email to %s exceeded the %ss signup deadline — returning to the "
            "client without waiting. Check the mail transport.",
            user.email, settings.EMAIL_SIGNUP_DEADLINE_SECONDS,
        )
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

    result = await db.execute(select(User).where(User.email == email))
    existing = result.scalar_one_or_none()
    if existing:
        # A still-unconfirmed account whose owner can produce the password is
        # almost always a RETRY of a signup whose response never reached the
        # client — the account was committed, then the reply was lost (slow
        # email send, flaky mobile connection). Hard-409ing that leaves the
        # user wedged: they can't log in (unconfirmed) and can't re-register
        # (409), with no hint that /resend-confirmation is the way out. Treat
        # it as a resend instead, which makes signup safely idempotent.
        if not existing.email_confirmed and existing.check_password(body.password):
            code = _set_confirmation_token(existing)
            await db.commit()
            sent = await _send_confirmation_link(existing, code)
            return ok(
                {
                    "email": existing.email,
                    "confirmation_required": True,
                    "email_sent": sent,
                },
                message=(
                    "You already started signing up — we've sent a fresh "
                    "verification code to your email."
                )
                if sent
                else (
                    "You already started signing up, but we couldn't send the "
                    "verification email just now. Tap resend in a moment."
                ),
            )
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
    code = _set_confirmation_token(user)
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
    sent = await _send_confirmation_link(user, code)

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
@limiter.limit("20/minute")
async def verify_email(request: Request, body: VerifyEmailRequest, db: DbDep):
    """Confirm an email from the link sent at signup, and sign the user in.

    The token is looked up by its HASH — the plaintext is never stored, so a
    leaked database yields no usable link. No attempt counter here, unlike the
    old 6-digit code: 256 bits of entropy is not brute-forceable, and the
    per-IP limit above is only there to stop someone hammering the endpoint.

    Signing the user in on success is the whole point of using a link. Making
    them click through and then type their password again would waste the trust
    the click already established.
    """
    token = body.token.strip()
    result = await db.execute(
        select(User).where(User.email_token == hash_link_token(token))
    )
    user = result.scalar_one_or_none()

    if not user or not verify_link_token(token, user.email_token):
        raise APIError(
            "This confirmation link isn't valid. Request a new one below.",
            status=400,
            errors={"token": "invalid"},
        )

    if user.email_token_expires and datetime.utcnow() > user.email_token_expires:
        raise APIError(
            "This confirmation link has expired. Request a new one below.",
            status=400,
            errors={"token": "expired"},
        )

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
    await db.refresh(user)

    sub_res = await db.execute(
        select(Subscription).where(Subscription.restaurant_id == user.restaurant_id)
    )
    sub = sub_res.scalar_one_or_none()

    # Same shape as /login, so the client has one code path for "I now have a
    # session" regardless of how it got one.
    return ok(
        {
            "user": UserOut.model_validate(user).model_dump(),
            "subscription": subscription_dict(sub) if sub else None,
            "tokens": _issue_tokens(user),
        },
        message="Email confirmed — welcome to Karibu POS!",
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

    code = _set_confirmation_token(user)
    await db.commit()
    await _send_confirmation_link(user, code)
    return generic


@router.post("/forgot-password")
@limiter.limit("3/minute")
async def forgot_password(request: Request, body: ForgotPasswordRequest, db: DbDep):
    """Email a password reset link.

    ENUMERATION-SAFE. The response is identical whether or not the address is
    registered, so this endpoint cannot be used to discover which of a list of
    emails has an account. That is why it does not report "no such user" even
    though that would be friendlier to the one person who mistyped their own
    address — the friendlier version hands an attacker a customer list.
    """
    email = body.email.strip().lower()
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()

    generic = ok(
        message="If that email has an account, a reset link is on its way."
    )
    if not user or not user.is_active:
        return generic

    token = generate_link_token()
    user.reset_token = hash_link_token(token)
    # ONE HOUR, not the 24 the confirmation link gets. A reset link is a live
    # credential for taking over an account; a confirmation link only activates
    # one that already belongs to whoever holds the inbox.
    user.reset_token_expires = datetime.utcnow() + timedelta(hours=1)
    await db.commit()

    url = f"{settings.PUBLIC_WEB_URL.rstrip('/')}/reset-password?token={token}"
    subject, html, text = email_service.password_reset_link(user.full_name, url)
    try:
        await asyncio.wait_for(
            email_service.send_email(user.email, subject, html, text),
            timeout=settings.EMAIL_SIGNUP_DEADLINE_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.error("Password reset email to %s timed out", user.email)
    return generic


@router.post("/reset-password")
@limiter.limit("5/minute")
async def reset_password(request: Request, body: ResetPasswordRequest, db: DbDep):
    """Set a new password from a reset link."""
    problem = password_problem(body.password)
    if problem:
        raise APIError(problem, status=422, errors={"password": problem})

    # Looked up BY the hash, so the plaintext token is never compared in the
    # database and no email is needed in the request.
    token_hash = hash_link_token(body.token)
    result = await db.execute(select(User).where(User.reset_token == token_hash))
    user = result.scalar_one_or_none()

    invalid = APIError(
        "This reset link is invalid or has expired. Request a new one.",
        status=400,
    )
    if not user or not verify_link_token(body.token, user.reset_token):
        raise invalid
    if not user.reset_token_expires or user.reset_token_expires < datetime.utcnow():
        raise invalid
    if not user.is_active:
        raise invalid

    user.set_password(body.password)
    # SINGLE USE. Cleared before commit so a replayed link fails even if the
    # same request is delivered twice.
    user.reset_token = None
    user.reset_token_expires = None
    # Reaching the inbox proves ownership of the address, so an account still
    # waiting on confirmation is confirmed here too. Otherwise someone who
    # never got the signup email but did get this one lands in a state where
    # they know their password and still cannot sign in.
    user.email_confirmed = True
    # EVERY EXISTING SESSION DIES. If the reset is happening because someone
    # else got in, leaving their access token working until it expires would
    # make the reset pointless.
    user.token_version += 1
    # A password change also clears any lockout — the brute force it was
    # protecting against is now aimed at a password that no longer exists.
    user.failed_logins = 0
    user.locked_until = None

    # Recorded in the SAME transaction as the change itself. Auditing after the
    # commit means a crash in between leaves a password changed with nothing
    # saying who changed it — which is the one question an audit log exists to
    # answer.
    await record_audit(
        db,
        action=AuditAction.PASSWORD_RESET,
        summary=f"{user.email} reset their password via an emailed link",
        actor_id=user.id,
        actor_email=user.email,
        restaurant_id=user.restaurant_id,
        request=request,
    )
    await db.commit()
    return ok(message="Password updated. Sign in with your new password.")


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
