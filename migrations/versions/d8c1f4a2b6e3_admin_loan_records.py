"""administrator loan records: ledger, terms snapshot, closures (M8)

Revision ID: d8c1f4a2b6e3
Revises: d2b7e4f8a1c6
Create Date: 2026-10-04 16:00:00.000000

Additive schema for Administrator operations. The data backfill is the next
migration (e4a9b7c2d158).
  * prime_pricing_versions / _tiers, penalty_policy_versions / _tiers -
    versioned, insert-only policy
  * loan_applications: the PRIME quote locked at submission (insert-once)
  * disbursements: application_id (NOT NULL, UNIQUE - the database-level
    guard against disbursing an application twice), recorded_at,
    destination_masked, evidence_document_id
  * loan_terms_snapshots, loan_ledger_entries, loan_closures (insert-only),
    scheduled_job_runs
  * Postgres triggers that reject UPDATE/DELETE on the insert-only tables
    and any change to an application's locked quote

"Return to officer" needs nothing here: RETURNED_TO_OFFICER and
admin_returns already exist (migrations 2b12227f1932 / 9c4e1a7d3b52).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = 'd8c1f4a2b6e3'
down_revision = 'd2b7e4f8a1c6'
branch_labels = None
depends_on = None

INSERT_ONLY_TABLES = (
    'loan_terms_snapshots',
    'loan_ledger_entries',
    'loan_closures',
    'disbursements',
    'prime_pricing_versions',
    'prime_pricing_tiers',
    'penalty_policy_versions',
    'penalty_policy_tiers',
)


def upgrade():
    bind = op.get_bind()
    is_pg = bind.dialect.name == 'postgresql'

    if is_pg:
        ledger_entry_type = postgresql.ENUM(
            'original_obligation', 'penalty', 'verified_repayment', name='ledger_entry_type')
        ledger_actor_kind = postgresql.ENUM('system', 'admin', name='ledger_actor_kind')
        loan_timeliness = postgresql.ENUM(
            'on_time', 'late_no_penalty', 'late_tier_1', 'late_tier_2', name='loan_timeliness')
        for e in (ledger_entry_type, ledger_actor_kind, loan_timeliness):
            e.create(bind, checkfirst=True)
        entry_type_col = postgresql.ENUM(name='ledger_entry_type', create_type=False)
        actor_kind_col = postgresql.ENUM(name='ledger_actor_kind', create_type=False)
        timeliness_col = postgresql.ENUM(name='loan_timeliness', create_type=False)
        closure_reason_col = postgresql.ENUM(name='loan_closure_reason', create_type=False)
        json_col = postgresql.JSONB(astext_type=sa.Text())
    else:
        entry_type_col = sa.Enum('original_obligation', 'penalty', 'verified_repayment',
                                 name='ledger_entry_type')
        actor_kind_col = sa.Enum('system', 'admin', name='ledger_actor_kind')
        timeliness_col = sa.Enum('on_time', 'late_no_penalty', 'late_tier_1', 'late_tier_2',
                                 name='loan_timeliness')
        closure_reason_col = sa.Enum('paid_in_full', 'defaulted', name='loan_closure_reason')
        json_col = sa.JSON()

    # ---- versioned policy ------------------------------------------------
    op.create_table(
        'prime_pricing_versions',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('label', sa.String(50), nullable=False, unique=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('created_by', sa.Integer(), sa.ForeignKey('users.id', ondelete='RESTRICT')),
        sa.Column('note', sa.String(500)),
    )
    op.create_table(
        'prime_pricing_tiers',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('version_id', sa.Integer(),
                  sa.ForeignKey('prime_pricing_versions.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('category', sa.String(20), nullable=False),
        sa.Column('min_amount', sa.Numeric(12, 2), nullable=False),
        sa.Column('max_amount', sa.Numeric(12, 2), nullable=False),
        sa.Column('interest_rate', sa.Numeric(6, 4), nullable=False),
        sa.UniqueConstraint('version_id', 'category', name='uq_prime_pricing_tiers_version_category'),
        sa.CheckConstraint('min_amount > 0 AND max_amount >= min_amount', name='ck_prime_pricing_tiers_range'),
        sa.CheckConstraint('interest_rate > 0 AND interest_rate <= 1', name='ck_prime_pricing_tiers_rate'),
    )
    op.create_table(
        'penalty_policy_versions',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('label', sa.String(50), nullable=False, unique=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('created_by', sa.Integer(), sa.ForeignKey('users.id', ondelete='RESTRICT')),
        sa.Column('note', sa.String(500)),
    )
    op.create_table(
        'penalty_policy_tiers',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('version_id', sa.Integer(),
                  sa.ForeignKey('penalty_policy_versions.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('tier', sa.SmallInteger(), nullable=False),
        sa.Column('days_late', sa.Integer(), nullable=False),
        sa.Column('pct_of_original_interest', sa.Numeric(6, 4), nullable=False),
        sa.UniqueConstraint('version_id', 'tier', name='uq_penalty_policy_tiers_version_tier'),
        sa.CheckConstraint('tier >= 1 AND days_late >= 1', name='ck_penalty_policy_tiers_positive'),
        sa.CheckConstraint('pct_of_original_interest > 0 AND pct_of_original_interest <= 10',
                           name='ck_penalty_policy_tiers_pct'),
    )
    op.create_table(
        'scheduled_job_runs',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('job_name', sa.String(50), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('finished_at', sa.DateTime(timezone=True)),
        sa.Column('summary', json_col),
    )
    op.create_index('ix_scheduled_job_runs_job_name', 'scheduled_job_runs', ['job_name'])

    # ---- the PRIME quote on each application (insert-once) ----------------
    with op.batch_alter_table('loan_applications', schema=None) as batch_op:
        batch_op.add_column(sa.Column('pricing_version_id', sa.Integer()))
        batch_op.add_column(sa.Column('penalty_policy_version_id', sa.Integer()))
        batch_op.add_column(sa.Column('quoted_interest_rate', sa.Numeric(6, 4)))
        batch_op.add_column(sa.Column('quoted_interest_amount', sa.Numeric(12, 2)))
        batch_op.add_column(sa.Column('quoted_total_repayable', sa.Numeric(12, 2)))
        batch_op.create_foreign_key('fk_loan_applications_pricing_version', 'prime_pricing_versions',
                                    ['pricing_version_id'], ['id'], ondelete='RESTRICT')
        batch_op.create_foreign_key('fk_loan_applications_penalty_policy_version', 'penalty_policy_versions',
                                    ['penalty_policy_version_id'], ['id'], ondelete='RESTRICT')

    # ---- disbursements: one per application, by constraint ----------------
    with op.batch_alter_table('disbursements', schema=None) as batch_op:
        batch_op.add_column(sa.Column('application_id', sa.Integer()))
        batch_op.add_column(sa.Column('recorded_at', sa.DateTime(timezone=True),
                                      nullable=False, server_default=sa.func.now()))
        batch_op.add_column(sa.Column('destination_masked', sa.String(64)))
        batch_op.add_column(sa.Column('evidence_document_id', sa.Integer()))
    # Existing rows: the application is the loan's; recorded when disbursed.
    op.execute(
        "UPDATE disbursements SET application_id = "
        "(SELECT loans.application_id FROM loans WHERE loans.id = disbursements.loan_id), "
        "recorded_at = disbursed_at"
    )
    with op.batch_alter_table('disbursements', schema=None) as batch_op:
        batch_op.alter_column('application_id', existing_type=sa.Integer(), nullable=False)
        batch_op.create_unique_constraint('uq_disbursements_application_id', ['application_id'])
        batch_op.create_foreign_key('fk_disbursements_application', 'loan_applications',
                                    ['application_id'], ['id'], ondelete='RESTRICT')
        batch_op.create_foreign_key('fk_disbursements_evidence_document', 'documents',
                                    ['evidence_document_id'], ['id'], ondelete='RESTRICT')

    # ---- the permanent loan record ----------------------------------------
    op.create_table(
        'loan_terms_snapshots',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('loan_id', sa.Integer(), sa.ForeignKey('loans.id', ondelete='RESTRICT'),
                  nullable=False, unique=True),
        sa.Column('application_id', sa.Integer(),
                  sa.ForeignKey('loan_applications.id', ondelete='RESTRICT'), nullable=False, unique=True),
        sa.Column('disbursement_id', sa.Integer(),
                  sa.ForeignKey('disbursements.id', ondelete='RESTRICT'), unique=True),
        sa.Column('pricing_version_id', sa.Integer(),
                  sa.ForeignKey('prime_pricing_versions.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('penalty_policy_version_id', sa.Integer(),
                  sa.ForeignKey('penalty_policy_versions.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('prime_category', sa.String(20), nullable=False),
        sa.Column('principal', sa.Numeric(12, 2), nullable=False),
        sa.Column('interest_rate', sa.Numeric(6, 4), nullable=False),
        sa.Column('interest_amount', sa.Numeric(12, 2), nullable=False),
        sa.Column('original_total_due', sa.Numeric(12, 2), nullable=False),
        sa.Column('term_days', sa.Integer(), nullable=False),
        sa.Column('disbursed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('disbursed_local_date', sa.Date(), nullable=False),
        sa.Column('due_date', sa.Date(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('created_by', sa.Integer(), sa.ForeignKey('users.id', ondelete='RESTRICT')),
        sa.CheckConstraint('original_total_due = principal + interest_amount',
                           name='ck_loan_terms_snapshots_total'),
        sa.CheckConstraint(
            'principal > 0 AND interest_amount >= 0 AND term_days > 0 AND due_date > disbursed_local_date',
            name='ck_loan_terms_snapshots_positive'),
    )
    op.create_table(
        'loan_ledger_entries',
        sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), primary_key=True),
        sa.Column('loan_id', sa.Integer(), sa.ForeignKey('loans.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('entry_type', entry_type_col, nullable=False),
        sa.Column('amount', sa.Numeric(12, 2), nullable=False),
        sa.Column('effective_date', sa.Date(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('created_by', sa.Integer(), sa.ForeignKey('users.id', ondelete='RESTRICT')),
        sa.Column('created_by_kind', actor_kind_col, nullable=False),
        sa.Column('disbursement_id', sa.Integer(), sa.ForeignKey('disbursements.id', ondelete='RESTRICT')),
        sa.Column('payment_transaction_id', sa.Integer(),
                  sa.ForeignKey('payment_transactions.id', ondelete='RESTRICT')),
        sa.Column('penalty_tier', sa.SmallInteger()),
        sa.Column('penalty_policy_version_id', sa.Integer(),
                  sa.ForeignKey('penalty_policy_versions.id', ondelete='RESTRICT')),
        sa.Column('job_run_id', sa.Integer(), sa.ForeignKey('scheduled_job_runs.id', ondelete='RESTRICT')),
        sa.Column('note', sa.String(500)),
        sa.CheckConstraint(
            "(entry_type IN ('original_obligation', 'penalty') AND amount > 0)"
            " OR (entry_type = 'verified_repayment' AND amount < 0)",
            name='ck_loan_ledger_amount_sign'),
        sa.CheckConstraint(
            "(entry_type <> 'verified_repayment' OR payment_transaction_id IS NOT NULL)"
            " AND (entry_type <> 'penalty' OR (penalty_tier IS NOT NULL"
            " AND penalty_policy_version_id IS NOT NULL AND job_run_id IS NOT NULL))",
            name='ck_loan_ledger_source'),
        sa.CheckConstraint("(created_by_kind = 'admin') = (created_by IS NOT NULL)",
                           name='ck_loan_ledger_actor'),
    )
    op.create_index('ix_loan_ledger_entries_loan_id', 'loan_ledger_entries', ['loan_id'])
    for name, cols, where in (
        ('uq_loan_ledger_one_obligation_per_loan', ['loan_id'], "entry_type = 'original_obligation'"),
        ('uq_loan_ledger_one_penalty_per_tier', ['loan_id', 'penalty_tier'], "entry_type = 'penalty'"),
        ('uq_loan_ledger_one_entry_per_payment', ['payment_transaction_id'],
         'payment_transaction_id IS NOT NULL'),
    ):
        op.create_index(name, 'loan_ledger_entries', cols, unique=True,
                        postgresql_where=sa.text(where), sqlite_where=sa.text(where))

    op.create_table(
        'loan_closures',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('loan_id', sa.Integer(), sa.ForeignKey('loans.id', ondelete='RESTRICT'),
                  nullable=False, unique=True),
        sa.Column('closed_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('closure_reason', closure_reason_col, nullable=False),
        sa.Column('closed_by', sa.Integer(), sa.ForeignKey('users.id', ondelete='RESTRICT')),
        sa.Column('closing_payment_transaction_id', sa.Integer(),
                  sa.ForeignKey('payment_transactions.id', ondelete='RESTRICT')),
        sa.Column('original_total_due', sa.Numeric(12, 2), nullable=False),
        sa.Column('total_penalties', sa.Numeric(12, 2), nullable=False),
        sa.Column('total_verified_paid', sa.Numeric(12, 2), nullable=False),
        sa.Column('outstanding_at_closure', sa.Numeric(12, 2), nullable=False),
        sa.Column('final_payment_date', sa.Date()),
        sa.Column('repayment_duration_days', sa.Integer()),
        sa.Column('timeliness', timeliness_col),
        sa.CheckConstraint("(closure_reason = 'paid_in_full') = (outstanding_at_closure = 0)",
                           name='ck_loan_closures_outstanding'),
    )

    if is_pg:
        _create_triggers()


def _create_triggers():
    op.execute("""
        CREATE FUNCTION reject_insert_only_change() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION '% rows are insert-only and cannot be %d',
                TG_TABLE_NAME, lower(TG_OP)
                USING ERRCODE = 'restrict_violation';
        END $$;
    """)
    for table in INSERT_ONLY_TABLES:
        op.execute(
            f"CREATE TRIGGER {table}_insert_only BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION reject_insert_only_change()"
        )
    op.execute("""
        CREATE FUNCTION reject_quote_change() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF OLD.pricing_version_id IS NOT NULL AND (
                NEW.pricing_version_id IS DISTINCT FROM OLD.pricing_version_id
                OR NEW.penalty_policy_version_id IS DISTINCT FROM OLD.penalty_policy_version_id
                OR NEW.quoted_interest_rate IS DISTINCT FROM OLD.quoted_interest_rate
                OR NEW.quoted_interest_amount IS DISTINCT FROM OLD.quoted_interest_amount
                OR NEW.quoted_total_repayable IS DISTINCT FROM OLD.quoted_total_repayable
                OR NEW.amount_requested IS DISTINCT FROM OLD.amount_requested
            ) THEN
                RAISE EXCEPTION 'loan application % has a locked PRIME quote that cannot change', OLD.id
                    USING ERRCODE = 'restrict_violation';
            END IF;
            RETURN NEW;
        END $$;
    """)
    op.execute(
        "CREATE TRIGGER loan_applications_quote_locked BEFORE UPDATE ON loan_applications "
        "FOR EACH ROW EXECUTE FUNCTION reject_quote_change()"
    )


def downgrade():
    bind = op.get_bind()
    is_pg = bind.dialect.name == 'postgresql'
    if is_pg:
        op.execute("DROP TRIGGER IF EXISTS loan_applications_quote_locked ON loan_applications")
        op.execute("DROP FUNCTION IF EXISTS reject_quote_change()")
        for table in INSERT_ONLY_TABLES:
            op.execute(f"DROP TRIGGER IF EXISTS {table}_insert_only ON {table}")
        op.execute("DROP FUNCTION IF EXISTS reject_insert_only_change()")

    op.drop_table('loan_closures')
    for name in ('uq_loan_ledger_one_entry_per_payment', 'uq_loan_ledger_one_penalty_per_tier',
                 'uq_loan_ledger_one_obligation_per_loan', 'ix_loan_ledger_entries_loan_id'):
        op.drop_index(name, table_name='loan_ledger_entries')
    op.drop_table('loan_ledger_entries')
    op.drop_table('loan_terms_snapshots')

    with op.batch_alter_table('disbursements', schema=None) as batch_op:
        batch_op.drop_constraint('fk_disbursements_evidence_document', type_='foreignkey')
        batch_op.drop_constraint('fk_disbursements_application', type_='foreignkey')
        batch_op.drop_constraint('uq_disbursements_application_id', type_='unique')
        batch_op.drop_column('evidence_document_id')
        batch_op.drop_column('destination_masked')
        batch_op.drop_column('recorded_at')
        batch_op.drop_column('application_id')

    with op.batch_alter_table('loan_applications', schema=None) as batch_op:
        batch_op.drop_constraint('fk_loan_applications_penalty_policy_version', type_='foreignkey')
        batch_op.drop_constraint('fk_loan_applications_pricing_version', type_='foreignkey')
        for col in ('quoted_total_repayable', 'quoted_interest_amount', 'quoted_interest_rate',
                    'penalty_policy_version_id', 'pricing_version_id'):
            batch_op.drop_column(col)

    op.drop_index('ix_scheduled_job_runs_job_name', table_name='scheduled_job_runs')
    op.drop_table('scheduled_job_runs')
    op.drop_table('penalty_policy_tiers')
    op.drop_table('penalty_policy_versions')
    op.drop_table('prime_pricing_tiers')
    op.drop_table('prime_pricing_versions')

    if is_pg:
        for name in ('loan_timeliness', 'ledger_actor_kind', 'ledger_entry_type'):
            op.execute(f"DROP TYPE IF EXISTS {name}")
