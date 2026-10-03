"""customer verification wiring (M6)

Revision ID: c7d4e2a9f613
Revises: a3f1c9e7d2b4
Create Date: 2026-10-03 14:00:00.000000

Nothing created CustomerVerification rows, so "Verified customer" and the
date of birth never appeared. Additive only:
  * users.date_of_birth - read off the ID by the officer at the Age 18+
    check (not self-reported); kept on the user so it outlives a verification
  * verification_items.evidence - what a check recorded (DOB; which ID
    document and its expiry), from which the verification is built
  * customer_verification_invalidation_reason gains 'superseded'

Nothing to backfill: no verification has ever been created.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = 'c7d4e2a9f613'
down_revision = 'a3f1c9e7d2b4'
branch_labels = None
depends_on = None

_REASONS_OLD = ('expired', 'information_changed', 'staff_requested', 'policy_updated')


def upgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('date_of_birth', sa.Date(), nullable=True))
    with op.batch_alter_table('verification_items', schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            'evidence',
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'),
            nullable=True,
        ))
    if op.get_bind().dialect.name == 'postgresql':
        op.execute(
            "ALTER TYPE customer_verification_invalidation_reason "
            "ADD VALUE IF NOT EXISTS 'superseded'"
        )


def downgrade():
    with op.batch_alter_table('verification_items', schema=None) as batch_op:
        batch_op.drop_column('evidence')
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('date_of_birth')

    if op.get_bind().dialect.name != 'postgresql':
        return
    # Rebuild the enum without 'superseded'. LOSSY: such rows become
    # 'staff_requested' (the closest pre-existing reason).
    values = ", ".join(f"'{v}'" for v in _REASONS_OLD)
    when = "\n            ".join(f"WHEN '{v}' THEN '{v}'" for v in _REASONS_OLD)
    op.execute("ALTER TYPE customer_verification_invalidation_reason "
               "RENAME TO customer_verification_invalidation_reason_new")
    op.execute(f"CREATE TYPE customer_verification_invalidation_reason AS ENUM ({values})")
    op.execute(
        f"""
        ALTER TABLE customer_verifications
        ALTER COLUMN invalidation_reason TYPE customer_verification_invalidation_reason
        USING (CASE invalidation_reason::text
            WHEN 'superseded' THEN 'staff_requested'
            {when}
        END)::customer_verification_invalidation_reason
        """
    )
    op.execute("DROP TYPE customer_verification_invalidation_reason_new")
