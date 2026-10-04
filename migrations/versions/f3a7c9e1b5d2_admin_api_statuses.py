"""admin API: disbursed status, disbursement evidence, parameter cleanup (M10)

Revision ID: f3a7c9e1b5d2
Revises: e4a9b7c2d158
Create Date: 2026-10-05 10:00:00.000000

  * loan_application_status gains 'disbursed'. Applications that already
    have a loan move to it - until now a disbursed application stayed
    'awaiting_disbursement' for good, so it still read "Approved -
    Processing Disbursement" to the customer and sat in that queue.
  * document_type gains 'disbursement_evidence' (an admin's BSP receipt or
    signed cash acknowledgement).
  * Stored overrides of the five parameters nothing ever read
    (default_annual_interest_rate, min/max_loan_amount, min/max_loan_term_
    months) are removed: PRIME pricing lives in the versioned
    prime_pricing_* tables, the term is fixed at 14 days.

Postgres can't use a new enum value in the transaction that added it, so
the two ALTER TYPEs run in an autocommit block first.
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = 'f3a7c9e1b5d2'
down_revision = 'e4a9b7c2d158'
branch_labels = None
depends_on = None

DEAD_PARAMETERS = (
    'default_annual_interest_rate', 'min_loan_amount', 'max_loan_amount',
    'min_loan_term_months', 'max_loan_term_months',
)


def upgrade():
    if op.get_bind().dialect.name == 'postgresql':
        with op.get_context().autocommit_block():
            op.execute("ALTER TYPE loan_application_status ADD VALUE IF NOT EXISTS 'disbursed'")
            op.execute("ALTER TYPE document_type ADD VALUE IF NOT EXISTS 'disbursement_evidence'")
    op.execute(
        "UPDATE loan_applications SET status = 'disbursed' "
        "WHERE status = 'awaiting_disbursement' "
        "AND id IN (SELECT application_id FROM loans)"
    )
    keys = ", ".join(f"'{k}'" for k in DEAD_PARAMETERS)
    op.execute(f"DELETE FROM system_parameters WHERE key IN ({keys})")


def downgrade():
    # Postgres can't drop an enum value; move rows back so nothing uses it.
    op.execute("UPDATE loan_applications SET status = 'awaiting_disbursement' WHERE status = 'disbursed'")
