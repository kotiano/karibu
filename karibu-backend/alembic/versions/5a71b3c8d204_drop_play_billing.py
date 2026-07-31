"""Drop the Google Play Billing columns.

The Android app is retired in favour of a web app, so Play Billing no longer
applies — Play's Payments policy governs apps distributed through Play, and a
browser is not that. Paystack is the only rail again.

Reverses 3f8a1c6d9e42. Safe because no subscription was ever billed through
Play: the integration was never activated in Play Console, so every row is
provider='paystack' and google_purchase_token IS NULL.

Revision ID: 5a71b3c8d204
Revises: 3f8a1c6d9e42
Create Date: 2026-07-31
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '5a71b3c8d204'
down_revision: Union[str, None] = '3f8a1c6d9e42'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index("ix_subscriptions_provider_status", table_name="subscriptions")
    op.drop_index(
        "ix_subscriptions_google_purchase_token", table_name="subscriptions"
    )
    with op.batch_alter_table("subscriptions") as batch:
        batch.drop_column("google_expiry_at")
        batch.drop_column("google_purchase_token")
        batch.drop_column("provider")


def downgrade() -> None:
    op.add_column(
        "subscriptions",
        sa.Column(
            "provider", sa.String(20), nullable=False, server_default="paystack"
        ),
    )
    op.add_column(
        "subscriptions",
        sa.Column("google_purchase_token", sa.String(255), nullable=True),
    )
    op.add_column(
        "subscriptions", sa.Column("google_expiry_at", sa.DateTime(), nullable=True)
    )
    op.create_index(
        "ix_subscriptions_google_purchase_token",
        "subscriptions",
        ["google_purchase_token"],
        unique=True,
    )
    op.create_index(
        "ix_subscriptions_provider_status", "subscriptions", ["provider", "status"]
    )
