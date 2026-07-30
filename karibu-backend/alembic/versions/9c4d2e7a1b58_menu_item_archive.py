"""Add menu_items.is_archived so retiring a sold item can't break order history.

Deleting a menu item that appeared in any order raised
ForeignKeyViolationError on order_items_menu_item_id_fkey — the endpoint
returned 500. The fix is to archive instead of delete once an item has been
sold, so the row (and therefore every historical order line pointing at it)
survives.

Backfilled to false: every existing item is live until someone retires it.

Revision ID: 9c4d2e7a1b58
Revises: 7b1e4c9a2f30
Create Date: 2026-07-30
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '9c4d2e7a1b58'
down_revision: Union[str, None] = '7b1e4c9a2f30'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # server_default so the NOT NULL add works against existing rows without a
    # separate backfill pass.
    op.add_column(
        "menu_items",
        sa.Column(
            "is_archived",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    # Menu reads filter on (restaurant_id, is_archived) on every request — this
    # is the hottest read path in the app.
    op.create_index(
        "ix_menu_items_restaurant_archived",
        "menu_items",
        ["restaurant_id", "is_archived"],
    )


def downgrade() -> None:
    op.drop_index("ix_menu_items_restaurant_archived", table_name="menu_items")
    op.drop_column("menu_items", "is_archived")
