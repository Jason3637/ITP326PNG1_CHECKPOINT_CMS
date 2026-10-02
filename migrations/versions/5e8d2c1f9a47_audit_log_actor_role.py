"""audit log actor role (M4)

Revision ID: 5e8d2c1f9a47
Revises: 9c4e1a7d3b52
Create Date: 2026-10-02 14:00:00.000000

Adds audit_logs.actor_role - the actor's role AT THE TIME of the action, so
the ledger can show who acted as loan_officer vs admin even if a user's role
changes later. Written by app.services.audit.record() from now on.

Deliberately NOT backfilled: for existing rows the only available value is
the user's CURRENT role, which may not be the role they acted under, and a
plausible-but-possibly-wrong value is worse than an honest NULL in an audit
ledger.
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '5e8d2c1f9a47'
down_revision = '9c4e1a7d3b52'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('audit_logs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('actor_role', sa.String(length=20), nullable=True))
        batch_op.create_index(batch_op.f('ix_audit_logs_actor_role'), ['actor_role'], unique=False)


def downgrade():
    with op.batch_alter_table('audit_logs', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_audit_logs_actor_role'))
        batch_op.drop_column('actor_role')
