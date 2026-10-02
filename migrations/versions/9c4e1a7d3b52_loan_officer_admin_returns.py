"""loan officer workflow - admin returns (M3)

Revision ID: 9c4e1a7d3b52
Revises: f72b8f821647
Create Date: 2026-10-02 10:00:00.000000

NEW LOAN OFFICER WORK ONLY. Added in the API phase: the Loan Officer
dashboard's "Returned by Administrator" queue needs a real return path,
which the state machine didn't have.

  * loan_application_status gains 'returned_to_officer'
  * admin_returns (who returned which recommendation, and why)

Purely additive. Nothing to backfill - no application has ever been returned.
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '9c4e1a7d3b52'
down_revision = 'f72b8f821647'
branch_labels = None
depends_on = None


def upgrade():
    if op.get_bind().dialect.name == 'postgresql':
        op.execute(
            "ALTER TYPE loan_application_status ADD VALUE IF NOT EXISTS 'returned_to_officer'"
        )

    op.create_table('admin_returns',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('loan_application_id', sa.Integer(), nullable=False),
    sa.Column('officer_recommendation_id', sa.Integer(), nullable=False),
    sa.Column('returned_by', sa.Integer(), nullable=False),
    sa.Column('reason', sa.String(length=2000), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['loan_application_id'], ['loan_applications.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['officer_recommendation_id'], ['officer_recommendations.id']),
    sa.ForeignKeyConstraint(['returned_by'], ['users.id']),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('admin_returns', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_admin_returns_loan_application_id'), ['loan_application_id'], unique=False)


def downgrade():
    with op.batch_alter_table('admin_returns', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_admin_returns_loan_application_id'))
    op.drop_table('admin_returns')

    if op.get_bind().dialect.name != 'postgresql':
        return

    # Rebuild loan_application_status without the value (same swap pattern
    # as c744ece0db7c / 2b12227f1932). LOSSY: a returned application maps
    # back to officer_review, the state the officer would resume into anyway.
    values = (
        'draft', 'submitted', 'officer_review', 'customer_action_required',
        'recommended_for_approval', 'recommended_for_rejection', 'admin_review',
        'approved', 'rejected', 'awaiting_disbursement',
    )
    when_sql = "\n            ".join(f"WHEN '{v}' THEN '{v}'" for v in values)
    values_sql = ", ".join(f"'{v}'" for v in values)
    op.execute("ALTER TABLE loan_applications ALTER COLUMN status DROP DEFAULT")
    op.execute("ALTER TYPE loan_application_status RENAME TO loan_application_status_new")
    op.execute(f"CREATE TYPE loan_application_status AS ENUM ({values_sql})")
    op.execute(
        f"""
        ALTER TABLE loan_applications
        ALTER COLUMN status TYPE loan_application_status
        USING (CASE status::text
            WHEN 'returned_to_officer' THEN 'officer_review'
            {when_sql}
        END)::loan_application_status
        """
    )
    op.execute(
        "ALTER TABLE loan_applications ALTER COLUMN status "
        "SET DEFAULT 'submitted'::loan_application_status"
    )
    op.execute("DROP TYPE loan_application_status_new")
