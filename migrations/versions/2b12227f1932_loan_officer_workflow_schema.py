"""loan officer workflow - additive schema (M1)

Revision ID: 2b12227f1932
Revises: 67a24cb3ec63
Create Date: 2026-10-01 14:00:00.000000

NEW LOAN OFFICER WORK ONLY - no Customer Workflow prerequisites needed
finishing (the PRIME status enum, calculate_prime() and the credit-evaluation
decoupling all shipped in 627a6b23d180 / c744ece0db7c).

Purely additive:
  * loan_application_status gains 'recommended_for_rejection' (a loan
    officer can only recommend rejection; the admin makes the final call)
  * information_requests + information_responses (Request More Information
    history - supersedes loan_applications.action_required_note, which is
    left in place here and dropped in a later contract migration)
  * verification_items (per-application checklist)
  * officer_recommendations (immutable recommend-approval/rejection records)
  * customer_verifications (customer-level verification, append-only)
  * loan_applications.assigned_officer_id / assigned_at (claim-on-review)

Staff-reference FKs on the history tables (requested_by, responded_by,
cancelled_by, checked_by, officer_id, verified_by, invalidated_by) are left
at Postgres' default ON DELETE NO ACTION on purpose - see the model
docstrings: a history row must always name who acted.

Data backfill is the separate f72b8f821647 migration.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '2b12227f1932'
down_revision = '67a24cb3ec63'
branch_labels = None
depends_on = None

# document_type already exists (initial schema) - reuse it, never re-create it.
_DOCUMENT_TYPE = postgresql.ENUM(
    'id_verification', 'receipt', 'loan_file', 'proof_of_income',
    name='document_type', create_type=False,
)

_NEW_ENUM_TYPES = (
    'information_request_type',
    'information_request_status',
    'verification_item_status',
    'officer_recommendation_type',
    'customer_verification_status',
    'customer_verification_invalidation_reason',
)


def upgrade():
    # Same in-transaction ADD VALUE approach 627a6b23d180 used for
    # document_type: allowed on modern Postgres as long as the new value
    # isn't used in this transaction, and nothing here (or in the backfill
    # migration after it) uses it.
    if op.get_bind().dialect.name == 'postgresql':
        op.execute(
            "ALTER TYPE loan_application_status "
            "ADD VALUE IF NOT EXISTS 'recommended_for_rejection' AFTER 'recommended_for_approval'"
        )

    # ---------------------------------------------------- customer_verifications
    op.create_table('customer_verifications',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('status', sa.Enum('verified', 'invalidated', name='customer_verification_status'), server_default='verified', nullable=False),
    sa.Column('verified_by', sa.Integer(), nullable=False),
    sa.Column('verified_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('id_document_id', sa.Integer(), nullable=False),
    sa.Column('date_of_birth', sa.Date(), nullable=False),
    sa.Column('id_expiry_date', sa.Date(), nullable=True),
    sa.Column('verified_full_name', sa.String(length=255), nullable=False),
    sa.Column('verified_email', sa.String(length=255), nullable=False),
    sa.Column('verified_phone_number', sa.String(length=32), nullable=True),
    sa.Column('valid_until', sa.Date(), nullable=False),
    sa.Column('policy_version', sa.String(length=20), nullable=False),
    sa.Column('source_application_id', sa.Integer(), nullable=True),
    sa.Column('invalidated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('invalidated_by', sa.Integer(), nullable=True),
    sa.Column('invalidation_reason', sa.Enum('expired', 'information_changed', 'staff_requested', 'policy_updated', name='customer_verification_invalidation_reason'), nullable=True),
    sa.Column('invalidation_note', sa.String(length=1000), nullable=True),
    sa.CheckConstraint("(status = 'invalidated') = (invalidated_at IS NOT NULL AND invalidation_reason IS NOT NULL)", name='ck_customer_verifications_invalidated_fields'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['verified_by'], ['users.id']),
    sa.ForeignKeyConstraint(['id_document_id'], ['documents.id']),
    sa.ForeignKeyConstraint(['source_application_id'], ['loan_applications.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['invalidated_by'], ['users.id']),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('customer_verifications', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_customer_verifications_user_id'), ['user_id'], unique=False)
        batch_op.create_index(
            'uq_customer_verifications_one_verified_per_user', ['user_id'], unique=True,
            postgresql_where=sa.text("status = 'verified'"),
            sqlite_where=sa.text("status = 'verified'"),
        )

    # ------------------------------------------------------ information_requests
    op.create_table('information_requests',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('loan_application_id', sa.Integer(), nullable=False),
    sa.Column('request_type', sa.Enum('missing_document', 'document_unclear', 'document_expired', 'information_mismatch', 'referee_unreachable', 'employment_confirmation', 'other', name='information_request_type'), nullable=False),
    sa.Column('reason', sa.String(length=1000), nullable=False),
    sa.Column('required_document_type', _DOCUMENT_TYPE, nullable=True),
    sa.Column('required_information', sa.String(length=500), nullable=True),
    sa.Column('internal_note', sa.String(length=1000), nullable=True),
    sa.Column('status', sa.Enum('open', 'responded', 'cancelled', name='information_request_status'), server_default='open', nullable=False),
    sa.Column('requested_by', sa.Integer(), nullable=False),
    sa.Column('requested_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('cancelled_by', sa.Integer(), nullable=True),
    sa.Column('cancelled_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('cancel_reason', sa.String(length=1000), nullable=True),
    sa.CheckConstraint("(status = 'cancelled') = (cancelled_by IS NOT NULL AND cancelled_at IS NOT NULL)", name='ck_information_requests_cancelled_fields'),
    sa.ForeignKeyConstraint(['loan_application_id'], ['loan_applications.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['requested_by'], ['users.id']),
    sa.ForeignKeyConstraint(['cancelled_by'], ['users.id']),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('information_requests', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_information_requests_loan_application_id'), ['loan_application_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_information_requests_status'), ['status'], unique=False)

    # ----------------------------------------------------- information_responses
    op.create_table('information_responses',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('information_request_id', sa.Integer(), nullable=False),
    sa.Column('responded_by', sa.Integer(), nullable=False),
    sa.Column('responded_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('response_note', sa.String(length=1000), nullable=False),
    sa.Column('field_changes', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.Column('provided_document_ids', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.ForeignKeyConstraint(['information_request_id'], ['information_requests.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['responded_by'], ['users.id']),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('information_responses', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_information_responses_information_request_id'), ['information_request_id'], unique=True)

    # -------------------------------------------------------- verification_items
    op.create_table('verification_items',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('loan_application_id', sa.Integer(), nullable=False),
    sa.Column('item_type', sa.String(length=50), nullable=False),
    sa.Column('status', sa.Enum('pending', 'verified', 'failed', 'not_applicable', name='verification_item_status'), server_default='pending', nullable=False),
    sa.Column('note', sa.String(length=1000), nullable=True),
    sa.Column('checked_by', sa.Integer(), nullable=True),
    sa.Column('checked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('customer_verification_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(status = 'pending') = (checked_by IS NULL AND checked_at IS NULL)", name='ck_verification_items_checked_fields'),
    sa.ForeignKeyConstraint(['loan_application_id'], ['loan_applications.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['checked_by'], ['users.id']),
    sa.ForeignKeyConstraint(['customer_verification_id'], ['customer_verifications.id']),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('loan_application_id', 'item_type', name='uq_verification_items_application_item')
    )
    with op.batch_alter_table('verification_items', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_verification_items_loan_application_id'), ['loan_application_id'], unique=False)

    # --------------------------------------------------- officer_recommendations
    op.create_table('officer_recommendations',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('loan_application_id', sa.Integer(), nullable=False),
    sa.Column('officer_id', sa.Integer(), nullable=False),
    sa.Column('recommendation', sa.Enum('recommend_approval', 'recommend_rejection', name='officer_recommendation_type'), nullable=False),
    sa.Column('comments', sa.String(length=2000), nullable=False),
    sa.Column('checklist_snapshot', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('credit_evaluation_snapshot', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.Column('customer_verification_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['loan_application_id'], ['loan_applications.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['officer_id'], ['users.id']),
    sa.ForeignKeyConstraint(['customer_verification_id'], ['customer_verifications.id']),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('officer_recommendations', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_officer_recommendations_loan_application_id'), ['loan_application_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_officer_recommendations_officer_id'), ['officer_id'], unique=False)

    # ----------------------------------------- loan_applications: claim-on-review
    with op.batch_alter_table('loan_applications', schema=None) as batch_op:
        batch_op.add_column(sa.Column('assigned_officer_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('assigned_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.create_index(batch_op.f('ix_loan_applications_assigned_officer_id'), ['assigned_officer_id'], unique=False)
        batch_op.create_foreign_key(
            'fk_loan_applications_assigned_officer_id_users', 'users',
            ['assigned_officer_id'], ['id'], ondelete='SET NULL',
        )


def downgrade():
    with op.batch_alter_table('loan_applications', schema=None) as batch_op:
        batch_op.drop_constraint('fk_loan_applications_assigned_officer_id_users', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_loan_applications_assigned_officer_id'))
        batch_op.drop_column('assigned_at')
        batch_op.drop_column('assigned_officer_id')

    # Dependents first: these reference customer_verifications.
    op.drop_table('officer_recommendations')
    op.drop_table('verification_items')
    op.drop_table('information_responses')
    op.drop_table('information_requests')
    op.drop_table('customer_verifications')

    bind = op.get_bind()
    if bind.dialect.name != 'postgresql':
        return

    # drop_table() doesn't drop the enum types create_table() made.
    for enum_name in _NEW_ENUM_TYPES:
        sa.Enum(name=enum_name).drop(bind, checkfirst=True)

    # Postgres can't DROP an enum VALUE - rebuild loan_application_status
    # without it (same swap pattern as c744ece0db7c). LOSSY: any application
    # sitting in recommended_for_rejection is mapped to
    # recommended_for_approval, the only pre-existing "awaiting admin" state.
    old_values = (
        'draft', 'submitted', 'officer_review', 'customer_action_required',
        'recommended_for_approval', 'admin_review', 'approved', 'rejected',
        'awaiting_disbursement',
    )
    when_sql = "\n            ".join(
        f"WHEN '{v}' THEN '{v}'" for v in old_values
    )
    values_sql = ", ".join(f"'{v}'" for v in old_values)
    op.execute("ALTER TABLE loan_applications ALTER COLUMN status DROP DEFAULT")
    op.execute("ALTER TYPE loan_application_status RENAME TO loan_application_status_new")
    op.execute(f"CREATE TYPE loan_application_status AS ENUM ({values_sql})")
    op.execute(
        f"""
        ALTER TABLE loan_applications
        ALTER COLUMN status TYPE loan_application_status
        USING (CASE status::text
            WHEN 'recommended_for_rejection' THEN 'recommended_for_approval'
            {when_sql}
        END)::loan_application_status
        """
    )
    op.execute(
        "ALTER TABLE loan_applications ALTER COLUMN status "
        "SET DEFAULT 'submitted'::loan_application_status"
    )
    op.execute("DROP TYPE loan_application_status_new")
