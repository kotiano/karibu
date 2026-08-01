"""What each dish consumes from stock

Revision ID: b6e0d4a71f52
Revises: a1c94f27e0b8
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b6e0d4a71f52"
down_revision: Union[str, None] = "a1c94f27e0b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "recipe_lines",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("menu_item_id", sa.String(length=36), nullable=False),
        sa.Column("stock_item_id", sa.String(length=36), nullable=False),
        # Thousandths of a stock unit per SALE. 250g of a kg item is 250.
        sa.Column("quantity_milli", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["menu_item_id"], ["menu_items.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["stock_item_id"], ["stock_items.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("menu_item_id", "stock_item_id", name="uq_recipe_dish_item"),
    )
    op.create_index("ix_recipe_lines_menu_item_id", "recipe_lines", ["menu_item_id"])
    op.create_index("ix_recipe_lines_stock_item_id", "recipe_lines", ["stock_item_id"])


def downgrade() -> None:
    op.drop_index("ix_recipe_lines_stock_item_id", table_name="recipe_lines")
    op.drop_index("ix_recipe_lines_menu_item_id", table_name="recipe_lines")
    op.drop_table("recipe_lines")
