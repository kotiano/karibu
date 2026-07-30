"""add email_token_attempts for OTP-based email verification

Revision ID: 2c0d51cfd47e
Revises: e0068020425b
Create Date: 2026-07-23 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2c0d51cfd47e'
down_revision: Union[str, None] = 'e0068020425b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'users',
        sa.Column('email_token_attempts', sa.Integer(), nullable=False, server_default='0'),
    )


def downgrade() -> None:
    op.drop_column('users', 'email_token_attempts')
