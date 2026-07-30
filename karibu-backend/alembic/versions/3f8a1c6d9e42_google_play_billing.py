"""Add Google Play billing fields to subscriptions.

Play requires Google Play Billing for in-app subscriptions to cloud business
software, so subscriptions can now be billed on either rail. `provider` records
which one owns a given subscription's lifecycle — critically, run_billing_sweep
filters on it so a Play-billed subscription is never also charged through
Paystack.

Existing rows default to 'paystack', which is what they are.

Revision ID: 3f8a1c6d9e42
Revises: 9c4d2e7a1b58
Create Date: 2026-07-30
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '3f8a1c6d9e42'
down_revision: Union[str, None] = '9c4d2e7a1b58'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
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
    # UNIQUE, not merely indexed: this is what stops one Play purchase being
    # replayed to grant a second restaurant a subscription. Enforcing it in the
    # database means application logic cannot be bypassed to get around it.
    op.create_index(
        "ix_subscriptions_google_purchase_token",
        "subscriptions",
        ["google_purchase_token"],
        unique=True,
    )
    # The sweep filters on provider every run.
    op.create_index(
        "ix_subscriptions_provider_status", "subscriptions", ["provider", "status"]
    )


def downgrade() -> None:
    op.drop_index("ix_subscriptions_provider_status", table_name="subscriptions")
    op.drop_index(
        "ix_subscriptions_google_purchase_token", table_name="subscriptions"
    )
    with op.batch_alter_table("subscriptions") as batch:
        batch.drop_column("google_expiry_at")
        batch.drop_column("google_purchase_token")
        batch.drop_column("provider")
