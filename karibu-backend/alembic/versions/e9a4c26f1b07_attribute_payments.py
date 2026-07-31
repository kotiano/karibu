"""Record who took each payment

Recording a payment is open to every role, waiters included: someone handed cash
must be able to write it down, and blocking them only means the takings go
unrecorded. The control is the accountability ledger, not a permission — which
requires the payment to actually carry a name.

The order's server is NOT that name. On a busy floor whoever is nearest closes
the bill, so "who served the table" answers a different question from "who says
they took this money".

Revision ID: e9a4c26f1b07
Revises: d5c81f0a3b76
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e9a4c26f1b07"
down_revision: Union[str, None] = "d5c81f0a3b76"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Existing payments keep NULL. Attributing historical takings to whoever
    # happens to be the manager would invent a fact this column exists to
    # establish.
    with op.batch_alter_table("payments") as batch:
        batch.add_column(sa.Column("recorded_by_id", sa.String(length=36), nullable=True))
        batch.create_foreign_key(
            "fk_payments_recorded_by_id_users", "users", ["recorded_by_id"], ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("payments") as batch:
        batch.drop_constraint("fk_payments_recorded_by_id_users", type_="foreignkey")
        batch.drop_column("recorded_by_id")
