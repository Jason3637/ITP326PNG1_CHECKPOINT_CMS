"""add credit evaluation inputs to loan_applications

Revision ID: 3ec191c6a228
Revises: 96f93014c3eb
Create Date: 2026-09-16 00:00:00.000000

Adds the self-reported applicant fields the interim credit-evaluation model
(app/services/credit_evaluation.py) needs: monthly_income, employment_status,
existing_monthly_debt. See BACKEND.md's Credit Evaluation section for what
these feed into.

FRONTEND: the loan application form (POST /api/loans/apply) needs matching
fields - monthly_income, employment_status, existing_monthly_debt. All are
optional at the API level, but omitting monthly_income means the application
can never be marked eligible (see credit_evaluation.py).
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '3ec191c6a228'
down_revision = '96f93014c3eb'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'loan_applications',
        sa.Column('monthly_income', sa.Numeric(precision=12, scale=2), nullable=True),
    )
    op.add_column(
        'loan_applications',
        sa.Column(
            'employment_status',
            sa.Enum(
                'employed', 'self_employed', 'unemployed', 'retired', 'student',
                name='employment_status',
            ),
            nullable=True,
        ),
    )
    op.add_column(
        'loan_applications',
        sa.Column('existing_monthly_debt', sa.Numeric(precision=12, scale=2), nullable=True),
    )


def downgrade():
    op.drop_column('loan_applications', 'existing_monthly_debt')
    op.drop_column('loan_applications', 'employment_status')
    op.drop_column('loan_applications', 'monthly_income')
    # Postgres doesn't drop the ENUM type on drop_column; clean it up explicitly
    # so a downgrade -> upgrade cycle doesn't collide with an existing type.
    # (SQLite has no CREATE/DROP TYPE at all - only relevant on Postgres.)
    if op.get_bind().dialect.name == 'postgresql':
        op.execute('DROP TYPE IF EXISTS employment_status')
