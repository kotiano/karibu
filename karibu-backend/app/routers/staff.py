"""Staff accounts.

Managers create the people who work for them. Two rules do most of the work
here, and both exist to protect the attribution that orders, debts, expenses
and stock movements all depend on:

  1. A NEW ACCOUNT GETS A TEMPORARY CODE AND MUST CHANGE IT. The manager reads
     the code out; the staff member sets their own password before they can do
     anything else. After that the manager cannot act as them, which is what
     makes "served by Jane" a fact rather than a suggestion.

  2. NOBODY MAY CREATE OR PROMOTE TO THEIR OWN RANK OR ABOVE. Otherwise a
     manager mints a second manager, or a cashier quietly promotes themselves,
     and the hierarchy is decorative.

Staff are DEACTIVATED, never deleted. Every order they took references them; a
delete would blank the attribution on all of it.
"""
import secrets

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, or_, select

from app.core.audit import record_audit
from app.core.dependencies import DbDep, SubscribedUser, require_roles
from app.core.security import APIError
from app.models import AuditAction, Order, Restaurant, User, UserRole
from app.schemas.common import ok
from app.schemas.staff import StaffCreate, StaffUpdate
from app.services import email as email_service

router = APIRouter(prefix="/api/staff", tags=["staff"])

MANAGERS = UserRole.MANAGERS

# Unambiguous alphabet: no O/0, I/1/l. This code gets read aloud across a
# noisy kitchen, and "was that an oh or a zero" is a support call.
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def generate_temp_code() -> str:
    """A short, speakable one-time password.

    Deliberately short — it is spoken, used once, and dies at first sign-in,
    so length is a usability cost with no security return. It still has to
    satisfy the password rules, hence the fixed suffix.
    """
    body = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(6))
    return f"{body}#k1"


def _staff_dict(u: User, orders_served: int = 0) -> dict:
    return {
        "id": u.id,
        "full_name": u.full_name,
        "email": u.email,
        "phone": u.phone,
        "role": u.role,
        "is_active": u.is_active,
        "must_change_password": u.must_change_password,
        "orders_served": orders_served,
        "created_at": u.created_at,
    }


async def _get_staff(db, manager: User, staff_id: str) -> User:
    """Scoped to the manager's restaurant, in the WHERE clause."""
    result = await db.execute(
        select(User).where(
            User.id == staff_id,
            User.restaurant_id == manager.restaurant_id,
        )
    )
    staff = result.scalar_one_or_none()
    if not staff:
        raise APIError("Staff member not found", status=404)
    return staff


def _assert_may_assign(actor: User, role: str) -> None:
    if role not in UserRole.ALL:
        raise APIError(
            f"Role must be one of: {', '.join(UserRole.ALL)}",
            status=422, errors={"role": "unknown"},
        )
    if UserRole.rank(role) >= UserRole.rank(actor.role):
        raise APIError(
            "You can only create accounts below your own level.",
            status=403, errors={"role": "too_high"},
        )


@router.get("", dependencies=[Depends(require_roles(*MANAGERS))])
async def list_staff(user: SubscribedUser, db: DbDep):
    """Everyone at this restaurant, including deactivated accounts.

    Deactivated people are listed rather than hidden: their name still appears
    against old orders, and someone reading a report needs to be able to find
    out who that was.
    """
    result = await db.execute(
        select(User)
        .where(User.restaurant_id == user.restaurant_id)
        .order_by(User.is_active.desc(), User.full_name.asc())
    )
    staff = list(result.scalars().all())

    counts_res = await db.execute(
        select(Order.server_id, func.count())
        .where(Order.restaurant_id == user.restaurant_id)
        .group_by(Order.server_id)
    )
    counts = {sid: n for sid, n in counts_res.all()}

    return ok(
        {
            "staff": [_staff_dict(u, counts.get(u.id, 0)) for u in staff],
            "roles": list(UserRole.ALL),
            # What the caller may hand out, so the client need not re-implement
            # the rank rule to build a dropdown.
            "assignable_roles": [
                r for r in UserRole.ALL if UserRole.rank(r) < UserRole.rank(user.role)
            ],
        }
    )


@router.post("", status_code=201, dependencies=[Depends(require_roles(*MANAGERS))])
async def create_staff(
    body: StaffCreate, request: Request, user: SubscribedUser, db: DbDep
):
    """Create an account and return a one-time code to read out."""
    _assert_may_assign(user, body.role)

    email = body.email.strip().lower() if body.email else None
    phone = body.phone.strip() if body.phone else None
    if not email and not phone:
        raise APIError(
            "A staff member needs an email or a phone number to sign in with.",
            status=422, errors={"phone": "required"},
        )

    # Email is unique across the whole platform; phone only within this
    # restaurant. The two checks are therefore scoped differently on purpose.
    if email:
        clash = await db.execute(select(User).where(User.email == email))
        if clash.scalar_one_or_none():
            raise APIError(
                "That email already has an account.",
                status=409, errors={"email": "taken"},
            )
    if phone:
        clash = await db.execute(
            select(User).where(
                User.restaurant_id == user.restaurant_id, User.phone == phone
            )
        )
        if clash.scalar_one_or_none():
            raise APIError(
                "Someone here already uses that phone number.",
                status=409, errors={"phone": "taken"},
            )

    code = generate_temp_code()
    staff = User(
        full_name=body.full_name.strip(),
        email=email,
        phone=phone,
        role=body.role,
        restaurant_id=user.restaurant_id,
        invited_by_id=user.id,
        must_change_password=True,
        # No inbox to confirm. The manager creating the account IS the
        # verification — they know who this person is.
        email_confirmed=True,
    )
    staff.set_password(code)
    db.add(staff)

    await record_audit(
        db,
        action=AuditAction.STAFF_CREATED,
        summary=f"{user.full_name} added {staff.full_name} as {staff.role}",
        actor_id=user.id,
        actor_email=user.email,
        restaurant_id=user.restaurant_id,
        request=request,
    )
    await db.commit()
    await db.refresh(staff)

    # Emailed as a convenience where there is an address; the code is returned
    # either way, because reading it out is the flow that always works.
    if staff.email:
        restaurant = await db.get(Restaurant, user.restaurant_id)
        subject, html, text = email_service.staff_welcome(
            staff.full_name, user.full_name,
            restaurant.name if restaurant else "Karibu POS",
            staff.email, code, f"{_web_url()}/login",
        )
        try:
            await email_service.send_email(staff.email, subject, html, text)
        except Exception:  # noqa: BLE001 — a failed email must not fail the create
            pass

    return ok(
        {"staff": _staff_dict(staff), "temp_code": code},
        message=f"{staff.full_name} added. Give them this code — it works once.",
    )


@router.patch("/{staff_id}", dependencies=[Depends(require_roles(*MANAGERS))])
async def update_staff(
    staff_id: str, body: StaffUpdate, request: Request, user: SubscribedUser, db: DbDep
):
    staff = await _get_staff(db, user, staff_id)

    if staff.id == user.id and body.is_active is False:
        raise APIError(
            "You can't deactivate your own account.",
            status=422, errors={"is_active": "self"},
        )
    if staff.id == user.id and body.role is not None and body.role != staff.role:
        raise APIError(
            "You can't change your own role.",
            status=422, errors={"role": "self"},
        )

    if body.role is not None and body.role != staff.role:
        _assert_may_assign(user, body.role)
        staff.role = body.role

    if body.full_name is not None:
        staff.full_name = body.full_name.strip()

    if body.is_active is not None and body.is_active != staff.is_active:
        staff.is_active = body.is_active
        if not body.is_active:
            # Bumping the token version ends every live session immediately.
            # Without it someone dismissed at 2pm keeps working access until
            # their access token expires.
            staff.token_version += 1
            await record_audit(
                db,
                action=AuditAction.STAFF_DEACTIVATED,
                summary=f"{user.full_name} deactivated {staff.full_name}",
                actor_id=user.id,
                actor_email=user.email,
                restaurant_id=user.restaurant_id,
                request=request,
            )

    await db.commit()
    await db.refresh(staff)
    return ok(_staff_dict(staff), message=f"{staff.full_name} updated")


@router.post("/{staff_id}/reset-password", dependencies=[Depends(require_roles(*MANAGERS))])
async def reset_staff_password(staff_id: str, user: SubscribedUser, db: DbDep):
    """Issue a fresh one-time code.

    The route back in for staff with no email, who cannot use the self-service
    reset link. Ends their live sessions too — a forgotten password and a
    borrowed phone look identical from here.
    """
    staff = await _get_staff(db, user, staff_id)
    if staff.id == user.id:
        raise APIError(
            "Use your own password reset instead.",
            status=422, errors={"staff_id": "self"},
        )
    if UserRole.rank(staff.role) >= UserRole.rank(user.role):
        raise APIError("You can't reset that account.", status=403)

    code = generate_temp_code()
    staff.set_password(code)
    staff.must_change_password = True
    staff.token_version += 1
    staff.failed_logins = 0
    staff.locked_until = None
    await db.commit()

    return ok(
        {"temp_code": code},
        message=f"New code for {staff.full_name} — it works once.",
    )


def _web_url() -> str:
    from app.core.config import settings

    return settings.PUBLIC_WEB_URL.rstrip("/")
