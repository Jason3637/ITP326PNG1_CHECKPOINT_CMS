"""users.token_version (M7)

Revision ID: d2b7e4f8a1c6
Revises: c7d4e2a9f613
Create Date: 2026-10-04 10:00:00.000000

Every JWT now carries the user's token_version ("tv" claim); bumping it -
on an admin password reset - revokes every token issued before. Additive:
existing users start at 0, and tokens issued before this column existed
carry no "tv" and count as 0, so nobody is signed out by deploying it.
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'd2b7e4f8a1c6'
down_revision = 'c7d4e2a9f613'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('token_version', sa.Integer(), nullable=False, server_default=sa.text('0'))
        )


def downgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('token_version')
