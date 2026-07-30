"""Rename M-Pesa/Daraja columns to gateway-neutral provider_* (Paystack).

Daraja production access requires a registered company, so subscription
collection moved to Paystack, which resells M-Pesa STK push to unregistered
merchants. The columns held Daraja-specific ids; they now hold a Paystack
transaction reference, so they're renamed to match.

Existing rows keep their values. A historical Daraja CheckoutRequestID in
provider_reference will never match an inbound Paystack webhook, which is
correct — those charges are already finalized.

Revision ID: 7b1e4c9a2f30
Revises: 2c0d51cfd47e
Create Date: 2026-07-29
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '7b1e4c9a2f30'
down_revision: Union[str, None] = '2c0d51cfd47e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # batch_alter_table so this also works on SQLite, which cannot ALTER a
    # column in place and needs the table copied and rebuilt.
    with op.batch_alter_table("billing_charges") as batch:
        batch.alter_column(
            "mpesa_checkout_id",
            new_column_name="provider_reference",
            existing_type=sa.String(80),
            type_=sa.String(100),
            existing_nullable=True,
        )
        batch.alter_column(
            "mpesa_receipt",
            new_column_name="provider_receipt",
            existing_type=sa.String(40),
            existing_nullable=True,
        )
        # Paystack has no equivalent of Daraja's MerchantRequestID — the single
        # transaction reference identifies everything.
        batch.drop_column("mpesa_merchant_id")

    with op.batch_alter_table("processed_callbacks") as batch:
        batch.alter_column(
            "checkout_id",
            new_column_name="reference",
            existing_type=sa.String(80),
            type_=sa.String(100),
            existing_nullable=False,
        )

    # Renaming a column leaves its index carrying the old name on both backends.
    # Rename those too, or a migrated database and a freshly created one (the
    # app runs create_all at boot) end up with different index names for the
    # same thing — which makes every future autogenerate produce spurious drops.
    op.drop_index("ix_billing_charges_mpesa_checkout_id", table_name="billing_charges")
    op.create_index(
        "ix_billing_charges_provider_reference",
        "billing_charges",
        ["provider_reference"],
        unique=True,
    )
    op.drop_index("ix_processed_callbacks_checkout_id", table_name="processed_callbacks")
    op.create_index(
        "ix_processed_callbacks_reference",
        "processed_callbacks",
        ["reference"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_processed_callbacks_reference", table_name="processed_callbacks")
    op.create_index(
        "ix_processed_callbacks_checkout_id",
        "processed_callbacks",
        ["reference"],
        unique=True,
    )
    op.drop_index("ix_billing_charges_provider_reference", table_name="billing_charges")
    op.create_index(
        "ix_billing_charges_mpesa_checkout_id",
        "billing_charges",
        ["provider_reference"],
        unique=True,
    )

    with op.batch_alter_table("processed_callbacks") as batch:
        batch.alter_column(
            "reference",
            new_column_name="checkout_id",
            existing_type=sa.String(100),
            type_=sa.String(80),
            existing_nullable=False,
        )

    with op.batch_alter_table("billing_charges") as batch:
        batch.add_column(sa.Column("mpesa_merchant_id", sa.String(80), nullable=True))
        batch.alter_column(
            "provider_receipt",
            new_column_name="mpesa_receipt",
            existing_type=sa.String(40),
            existing_nullable=True,
        )
        batch.alter_column(
            "provider_reference",
            new_column_name="mpesa_checkout_id",
            existing_type=sa.String(100),
            type_=sa.String(80),
            existing_nullable=True,
        )
