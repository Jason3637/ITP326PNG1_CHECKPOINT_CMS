"""reapplication clearances for written-off loans (M11)

Revision ID: a9d3e5f7c1b8
Revises: f3a7c9e1b5d2
Create Date: 2026-10-06 10:00:00.000000

A customer whose loan was written off can't apply for PRIME again until an
admin clears it. Each clearance is a row here: which loan, who cleared it,
why, when - one per loan, insert-only (a Postgres trigger rejects UPDATE and
DELETE, like the other loan records). Nothing to backfill: production has no
written-off loans.
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'a9d3e5f7c1b8'
down_revision = 'f3a7c9e1b5d2'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'reapplication_clearances',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('loan_id', sa.Integer(), sa.ForeignKey('loans.id', ondelete='RESTRICT'),
                  nullable=False, unique=True),
        sa.Column('cleared_by', sa.Integer(), sa.ForeignKey('users.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('reason', sa.String(1000), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    if op.get_bind().dialect.name == 'postgresql':
        # reject_insert_only_change() was created by migration d8c1f4a2b6e3.
        op.execute(
            "CREATE TRIGGER reapplication_clearances_insert_only BEFORE UPDATE OR DELETE "
            "ON reapplication_clearances FOR EACH ROW EXECUTE FUNCTION reject_insert_only_change()"
        )


def downgrade():
    if op.get_bind().dialect.name == 'postgresql':
        op.execute("DROP TRIGGER IF EXISTS reapplication_clearances_insert_only ON reapplication_clearances")
    op.drop_table('reapplication_clearances')
