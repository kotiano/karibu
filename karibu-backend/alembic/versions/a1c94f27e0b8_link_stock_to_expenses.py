"""Link a stock delivery to its cost, and separate incurred from paid

Buying stock is one event that needed two unlinked manual entries — a stock
movement and an expense — so the two drifted. And a delivery taken on credit is
a cost the moment it arrives but not money out of the till, which the expense
table had no way to say.

Revision ID: a1c94f27e0b8
Revises: f3b57d0e9a41
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a1c94f27e0b8"
down_revision: Union[str, None] = "f3b57d0e9a41"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "stock_movements", sa.Column("total_cost_cents", sa.Integer(), nullable=True)
    )

    with op.batch_alter_table("expenses") as batch:
        # Existing expenses are treated as PAID. Every one of them was recorded
        # under a model where paying was the only thing an expense could mean,
        # so marking them unpaid would invent a debt to a supplier who has
        # already been settled.
        batch.add_column(
            sa.Column("is_paid", sa.Boolean(), nullable=False, server_default=sa.true())
        )
        batch.add_column(sa.Column("paid_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("due_date", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("stock_movement_id", sa.String(length=36), nullable=True))
        batch.create_foreign_key(
            "fk_expenses_stock_movement_id", "stock_movements",
            ["stock_movement_id"], ["id"], ondelete="SET NULL",
        )
    op.create_index(
        "ix_expenses_stock_movement_id", "expenses", ["stock_movement_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_expenses_stock_movement_id", table_name="expenses")
    with op.batch_alter_table("expenses") as batch:
        batch.drop_constraint("fk_expenses_stock_movement_id", type_="foreignkey")
        batch.drop_column("stock_movement_id")
        batch.drop_column("due_date")
        batch.drop_column("paid_at")
        batch.drop_column("is_paid")
    op.drop_column("stock_movements", "total_cost_cents")
