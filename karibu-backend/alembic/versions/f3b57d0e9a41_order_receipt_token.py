"""Add a shareable receipt token to orders

Public by token: the customer a receipt is for has no account and never will,
so the link itself is the authorisation. Plaintext rather than hashed — unlike
the auth tokens this one must be re-displayable, because a customer who lost
the message needs the SAME link, not a new one.

Revision ID: f3b57d0e9a41
Revises: e9a4c26f1b07
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f3b57d0e9a41"
down_revision: Union[str, None] = "e9a4c26f1b07"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("orders") as batch:
        batch.add_column(sa.Column("receipt_token", sa.String(length=64), nullable=True))
        batch.create_unique_constraint("uq_orders_receipt_token", ["receipt_token"])
    op.create_index("ix_orders_receipt_token", "orders", ["receipt_token"])


def downgrade() -> None:
    op.drop_index("ix_orders_receipt_token", table_name="orders")
    with op.batch_alter_table("orders") as batch:
        batch.drop_constraint("uq_orders_receipt_token", type_="unique")
        batch.drop_column("receipt_token")
