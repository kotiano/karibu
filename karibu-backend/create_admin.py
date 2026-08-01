"""Grant (or revoke) platform-admin access. THE ONLY WAY to set the flag.

The is_platform_admin flag is deliberately unreachable from any API endpoint —
if it were settable over HTTP, any auth bug would become full-platform
compromise. Server (shell) access is the trust boundary instead.

Usage:
    # Promote an existing account by email:
    python create_admin.py --email you@example.com

    # Create a brand-new admin account (attached to a "Karibu Platform HQ"
    # restaurant row, since every user needs a tenant):
    python create_admin.py --email you@example.com --password 'a-strong-one' \
        --name "Your Name"

    # Revoke:
    python create_admin.py --email you@example.com --revoke
"""
import argparse
import asyncio
import sys

from sqlalchemy import select

from app.core.config import settings
from app.core.database import AsyncSessionLocal, Base, engine
from app.core.passwords import password_problem
from app.models import Restaurant, User, UserRole

HQ_NAME = "Karibu Platform HQ"


async def main() -> int:
    parser = argparse.ArgumentParser(description="Manage platform admins")
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", help="Only needed when creating a new account")
    parser.add_argument("--name", default="Platform Admin")
    parser.add_argument("--revoke", action="store_true", help="Remove admin access")
    args = parser.parse_args()
    email = args.email.strip().lower()

    # NEVER create_all against production. Alembic owns that schema, and
    # create_all builds tables from the models WITHOUT stamping
    # alembic_version — so running this before `alembic upgrade head` would
    # create the pending tables and then make the migration fail with
    # "relation already exists". Convenience for a fresh dev database only.
    if settings.ENV != "production":
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async with AsyncSessionLocal() as db:
        user = (
            await db.execute(select(User).where(User.email == email))
        ).scalar_one_or_none()

        if args.revoke:
            if not user or not user.is_platform_admin:
                print(f"{email} is not a platform admin — nothing to revoke.")
                return 1
            user.is_platform_admin = False
            user.token_version += 1  # kill any live admin sessions immediately
            await db.commit()
            print(f"Revoked platform admin from {email} (sessions invalidated).")
            return 0

        if user:
            if user.is_platform_admin:
                print(f"{email} is already a platform admin.")
                return 0
            user.is_platform_admin = True
            await db.commit()
            print(f"Promoted existing account {email} to platform admin.")
            return 0

        # Creating a fresh admin account.
        if not args.password:
            print("Account doesn't exist — pass --password to create it.")
            return 1
        if len(args.password) < 8:
            print("Password must be at least 8 characters.")
            return 1
        problem = password_problem(args.password)
        if problem:
            print(f"Weak password: {problem}")
            return 1

        hq = (
            await db.execute(select(Restaurant).where(Restaurant.name == HQ_NAME))
        ).scalar_one_or_none()
        if not hq:
            hq = Restaurant(name=HQ_NAME, is_active=True)
            db.add(hq)
            await db.flush()

        user = User(
            full_name=args.name,
            email=email,
            role=UserRole.MANAGER,
            restaurant_id=hq.id,
            is_platform_admin=True,
        )
        user.set_password(args.password)
        db.add(user)
        await db.commit()
        print(f"Created platform admin {email} (attached to '{HQ_NAME}').")
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))


