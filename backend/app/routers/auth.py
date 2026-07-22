"""Authentication routes: register, login, refresh, me, email confirmation."""
import secrets
from datetime import datetime, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
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
    token_claims,
)
from app.core.serializers import subscription_dict
from app.models import AuditAction, Restaurant, Subscription, User, UserRole
from app.schemas.auth import (
    LoginRequest,
    RegisterRequest,
    UpdateProfileRequest,
    UserOut,
)
from app.schemas.common import ok
from app.services import billing, email as email_service

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _new_email_token() -> tuple[str, datetime]:
    token = secrets.token_urlsafe(32)
    expires = datetime.utcnow() + timedelta(hours=settings.EMAIL_TOKEN_HOURS)
    return token, expires


async def _send_confirmation(user: User) -> None:
    confirm_url = f"{settings.PUBLIC_API_URL}/api/auth/confirm?token={user.email_token}"
    subject, html, text = email_service.confirm_email(user.full_name, confirm_url)
    await email_service.send_email(user.email, subject, html, text)


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
    # Email starts unconfirmed; login is blocked until they click the link.
    token, expires = _new_email_token()
    user.email_token = token
    user.email_token_expires = expires
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

    # Fire the confirmation email (console-logged in dev).
    await _send_confirmation(user)

    # No login tokens — the account must confirm its email first.
    return ok(
        {
            "email": user.email,
            "confirmation_required": True,
        },
        message=(
            "Almost there — check your email to confirm your account, "
            "then sign in. Your 14-day free trial is ready."
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
            "Please confirm your email first. Check your inbox for the link.",
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


@router.get("/confirm", response_class=HTMLResponse)
async def confirm_email(token: str, db: DbDep):
    """Confirm an email via the link token. Returns a simple HTML page since
    this is opened in a browser, not the app."""
    result = await db.execute(select(User).where(User.email_token == token))
    user = result.scalar_one_or_none()

    def page(title: str, message: str, ok_state: bool) -> str:
        color = "#005C39" if ok_state else "#C0392B"
        return f"""<!doctype html><html><head><meta name="viewport"
        content="width=device-width,initial-scale=1"><title>{title}</title></head>
        <body style="margin:0;background:#FBFAF6;font-family:Arial,sans-serif;">
        <div style="max-width:460px;margin:64px auto;padding:32px;text-align:center;">
        <div style="font-size:24px;font-weight:bold;color:#005C39;">Karibu POS</div>
        <div style="height:3px;width:44px;background:#F97316;border-radius:2px;margin:12px auto 28px;"></div>
        <h1 style="font-size:22px;color:{color};">{title}</h1>
        <p style="color:#3a3a3a;font-size:15px;line-height:1.6;">{message}</p>
        </div></body></html>"""

    if not user:
        return HTMLResponse(
            page("Link invalid", "This confirmation link is invalid or already used. "
                 "Try signing in, or request a new link from the app.", False),
            status_code=400,
        )
    if user.email_confirmed:
        return HTMLResponse(
            page("Already confirmed", "Your email is already confirmed — you can "
                 "sign in from the app.", True)
        )
    if user.email_token_expires and datetime.utcnow() > user.email_token_expires:
        return HTMLResponse(
            page("Link expired", "This link has expired. Open the app and request "
                 "a new confirmation email.", False),
            status_code=400,
        )

    user.email_confirmed = True
    user.email_token = None
    user.email_token_expires = None
    await record_audit(
        db,
        action=AuditAction.EMAIL_CONFIRMED,
        summary=f"{user.email} confirmed their email",
        actor_id=user.id,
        actor_email=user.email,
        restaurant_id=user.restaurant_id,
    )
    await db.commit()
    return HTMLResponse(
        page("Email confirmed!", "Your account is now active. Head back to the "
             "Karibu POS app and sign in — your free trial is ready.", True)
    )


@router.post("/resend-confirmation")
@limiter.limit("3/minute")
async def resend_confirmation(request: Request, body: LoginRequest, db: DbDep):
    """Re-send the confirmation email. Takes email+password so only the account
    owner can trigger it (and we don't leak whether an email exists)."""
    email = body.email.strip().lower()
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()

    # Always return the same response regardless, to avoid email enumeration.
    generic = ok(message="If that account exists and is unconfirmed, a new link is on its way.")
    if not user or not user.check_password(body.password):
        return generic
    if user.email_confirmed:
        return generic

    token, expires = _new_email_token()
    user.email_token = token
    user.email_token_expires = expires
    await db.commit()
    await _send_confirmation(user)
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
