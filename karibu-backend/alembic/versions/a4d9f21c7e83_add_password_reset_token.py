"""Add password reset token columns

Separate from email_token/email_token_expires on purpose. The two links grant
different things — one activates an account, one changes a password — and
sharing a slot would mean requesting a reset silently invalidated a pending
signup confirmation, and the reverse.

Revision ID: a4d9f21c7e83
Revises: 8e2f4a91c635
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a4d9f21c7e83"
down_revision: Union[str, None] = "8e2f4a91c635"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 64 chars: the columns hold a sha256 HEX DIGEST of the token, never the
    # token itself, and that is exactly 64 characters wide.
    op.add_column("users", sa.Column("reset_token", sa.String(length=64), nullable=True))
    op.add_column("users", sa.Column("reset_token_expires", sa.DateTime(), nullable=True))
    # Indexed because /reset-password looks a user up BY the hashed token —
    # there is no email in that request, deliberately.
    op.create_index("ix_users_reset_token", "users", ["reset_token"])


def downgrade() -> None:
    op.drop_index("ix_users_reset_token", table_name="users")
    op.drop_column("users", "reset_token_expires")
    op.drop_column("users", "reset_token")
