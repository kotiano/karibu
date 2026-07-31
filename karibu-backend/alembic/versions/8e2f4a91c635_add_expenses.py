"""Add the expenses table.

Money going OUT, kept deliberately separate from Payment (money coming IN).
Combining them behind a sign would make every analytics query a hazard — one
missed filter and rent counts as revenue.

Revision ID: 8e2f4a91c635
Revises: 5a71b3c8d204
Create Date: 2026-07-31
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '8e2f4a91c635'
down_revision: Union[str, None] = '5a71b3c8d204'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "expenses",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("restaurant_id", sa.String(36), sa.ForeignKey("restaurants.id"), nullable=False),
        sa.Column("category", sa.String(20), nullable=False, server_default="other"),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("payee", sa.String(120), nullable=True),
        sa.Column("method", sa.String(20), nullable=False, server_default="cash"),
        sa.Column("reference", sa.String(120), nullable=True),
        sa.Column("spent_at", sa.DateTime(), nullable=False),
        sa.Column("recorded_by_id", sa.String(36), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_expenses_restaurant_id", "expenses", ["restaurant_id"])
    op.create_index("ix_expenses_spent_at", "expenses", ["spent_at"])
    # Every read is "this restaurant, this date range".
    op.create_index(
        "ix_expenses_restaurant_spent", "expenses", ["restaurant_id", "spent_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_expenses_restaurant_spent", table_name="expenses")
    op.drop_index("ix_expenses_spent_at", table_name="expenses")
    op.drop_index("ix_expenses_restaurant_id", table_name="expenses")
    op.drop_table("expenses")
