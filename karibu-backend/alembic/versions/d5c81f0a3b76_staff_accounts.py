"""Staff accounts: collapse owner into manager, drop branch, add invite fields

THREE CHANGES, and the first one moves DATA, not just schema:

1. Every existing 'owner' becomes 'manager'. There is no separate owner rank —
   whoever signs a restaurant up is its manager. Done before anything else, so
   a failure later leaves roles consistent rather than half-renamed.

2. branch_name is dropped. It was a free-text label on the user that nothing
   scoped by; reports now use the restaurant's actual name.

3. email becomes nullable and its uniqueness becomes conditional; phone becomes
   a login identifier, unique per restaurant. A waiter may have no email, and
   requiring one produces a table full of invented addresses.

Revision ID: d5c81f0a3b76
Revises: c7b3e5f81a24
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d5c81f0a3b76"
down_revision: Union[str, None] = "c7b3e5f81a24"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Data first.
    op.execute("UPDATE users SET role = 'manager' WHERE role = 'owner'")

    # 2/3. Schema. batch mode because SQLite cannot ALTER a column's nullability
    # or add a constraint in place — and every developer here runs SQLite.
    with op.batch_alter_table("users") as batch:
        batch.drop_column("branch_name")
        batch.add_column(
            sa.Column(
                "must_change_password", sa.Boolean(), nullable=False,
                server_default=sa.false(),
            )
        )
        batch.add_column(sa.Column("invited_by_id", sa.String(length=36), nullable=True))
        batch.alter_column("email", existing_type=sa.String(length=120), nullable=True)
        batch.create_unique_constraint(
            "uq_users_restaurant_phone", ["restaurant_id", "phone"]
        )
        batch.create_foreign_key(
            "fk_users_invited_by_id_users", "users", ["invited_by_id"], ["id"],
            ondelete="SET NULL",
        )

    # The old unqualified UNIQUE on email is replaced by a PARTIAL index, so
    # "unique among the rows that have an email" holds while many staff share
    # NULL. Postgres and SQLite both support the WHERE clause; the syntax is
    # identical, so no dialect branch is needed.
    op.create_index(
        "uq_users_email_present",
        "users",
        ["email"],
        unique=True,
        sqlite_where=sa.text("email IS NOT NULL"),
        postgresql_where=sa.text("email IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_users_email_present", table_name="users")
    with op.batch_alter_table("users") as batch:
        batch.drop_constraint("fk_users_invited_by_id_users", type_="foreignkey")
        batch.drop_constraint("uq_users_restaurant_phone", type_="unique")
        # Rows with no email cannot exist under the old NOT NULL constraint.
        # Reversing is only safe on a database that never used the new shape.
        batch.alter_column("email", existing_type=sa.String(length=120), nullable=False)
        batch.drop_column("invited_by_id")
        batch.drop_column("must_change_password")
        batch.add_column(
            sa.Column(
                "branch_name", sa.String(length=120), nullable=False,
                server_default="Main Branch",
            )
        )
