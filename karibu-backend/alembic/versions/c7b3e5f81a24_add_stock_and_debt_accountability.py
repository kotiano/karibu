"""Add stock tables, and record who authorised a credit sale

Two things in one revision because they ship together: stock movements and
debts both answer "who did this", and splitting them would leave a migration
that only half-explains itself.

Revision ID: c7b3e5f81a24
Revises: a4d9f21c7e83
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c7b3e5f81a24"
down_revision: Union[str, None] = "a4d9f21c7e83"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── Stock ──────────────────────────────────────────────────────────────
    op.create_table(
        "stock_items",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("restaurant_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("unit", sa.String(length=20), nullable=False, server_default="kg"),
        # Integer thousandths, never a float — see app/models/stock.py.
        sa.Column("quantity_milli", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reorder_level_milli", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("unit_cost_cents", sa.Integer(), nullable=True),
        sa.Column("supplier", sa.String(length=120), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("is_archived", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["restaurant_id"], ["restaurants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_stock_items_restaurant_id", "stock_items", ["restaurant_id"])
    op.create_index(
        "ix_stock_items_restaurant_archived", "stock_items", ["restaurant_id", "is_archived"]
    )

    op.create_table(
        "stock_movements",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("stock_item_id", sa.String(length=36), nullable=False),
        sa.Column("delta_milli", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(length=20), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("balance_after_milli", sa.Integer(), nullable=False),
        sa.Column("recorded_by_id", sa.String(length=36), nullable=True),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["stock_item_id"], ["stock_items.id"], ondelete="CASCADE"),
        # SET NULL, not CASCADE: removing a staff member must not delete the
        # record that stock moved. The movement outlives the account.
        sa.ForeignKeyConstraint(["recorded_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_stock_movements_stock_item_id", "stock_movements", ["stock_item_id"])
    op.create_index("ix_stock_movements_occurred_at", "stock_movements", ["occurred_at"])

    # ── Accountability ─────────────────────────────────────────────────────
    # Existing debts keep NULL: nobody knows who authorised them, and inventing
    # an attribution for a historical credit sale would be worse than admitting
    # the gap — this column is going to be used to hold people responsible.
    #
    # batch_alter_table, not add_column + create_foreign_key: SQLite cannot
    # ALTER a constraint into an existing table at all, and raises
    # NotImplementedError. Batch mode does it by copy-and-move, and is a no-op
    # wrapper on Postgres — so one migration works on the dev database and the
    # production one. Found by running the migration against SQLite, which is
    # what every developer here has.
    with op.batch_alter_table("debts") as batch:
        batch.add_column(sa.Column("recorded_by_id", sa.String(length=36), nullable=True))
        batch.create_foreign_key(
            "fk_debts_recorded_by_id_users", "users", ["recorded_by_id"], ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("debts") as batch:
        batch.drop_constraint("fk_debts_recorded_by_id_users", type_="foreignkey")
        batch.drop_column("recorded_by_id")

    op.drop_index("ix_stock_movements_occurred_at", table_name="stock_movements")
    op.drop_index("ix_stock_movements_stock_item_id", table_name="stock_movements")
    op.drop_table("stock_movements")

    op.drop_index("ix_stock_items_restaurant_archived", table_name="stock_items")
    op.drop_index("ix_stock_items_restaurant_id", table_name="stock_items")
    op.drop_table("stock_items")
