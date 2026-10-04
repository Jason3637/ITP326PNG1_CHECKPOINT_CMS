"""administrator loan records backfill (M9)

Revision ID: e4a9b7c2d158
Revises: d8c1f4a2b6e3
Create Date: 2026-10-04 16:05:00.000000

Data only, after M8 (d8c1f4a2b6e3):
  1. Seed version 1 of the PRIME pricing tiers (= prime_pricing.TIERS) and
     of the late-penalty policy (+25% / +100% of original interest at 7 /
     14 days late; cumulative; nothing after).
  2. Lock a version-1 quote onto every application that has none. Every
     existing application was priced under version 1 - rates have never
     changed - so this is the price its customer was shown.
  3. For every existing loan: its terms snapshot, its ORIGINAL_OBLIGATION
     ledger entry, a VERIFIED_REPAYMENT entry per verified payment, and -
     for closed loans - its closure record.

Who verified a payment and who closed a loan were never stored on those
rows; they come from the audit log (payment_verified / loan_closed /
loan_written_off), else the entry is attributed to the system. Postgres
only: on SQLite (throwaway local databases) only the seed runs.

downgrade() removes what this inserted. It runs before M8's downgrade, so
the insert-only triggers still exist then: it disables them around its own
deletes and re-enables them after.
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = 'e4a9b7c2d158'
down_revision = 'd8c1f4a2b6e3'
branch_labels = None
depends_on = None

PRICING_TIERS = (
    ('PRIME 1', 100, 300, '0.50'),
    ('PRIME 2', 301, 700, '0.40'),
    ('PRIME 3', 701, 1000, '0.35'),
)
PENALTY_TIERS = ((1, 7, '0.25'), (2, 14, '1.00'))

# Port Moresby date of a timestamptz (UTC+10, no daylight saving).
LOCAL_DATE = "((({col}) AT TIME ZONE 'UTC') + interval '10 hours')::date"


def upgrade():
    bind = op.get_bind()

    op.execute("INSERT INTO prime_pricing_versions (label, note) VALUES "
               "('prime-v1', 'Initial PRIME tiers.')")
    for category, low, high, rate in PRICING_TIERS:
        op.execute(
            "INSERT INTO prime_pricing_tiers (version_id, category, min_amount, max_amount, interest_rate) "
            f"SELECT id, '{category}', {low}, {high}, {rate} FROM prime_pricing_versions WHERE label = 'prime-v1'"
        )
    op.execute("INSERT INTO penalty_policy_versions (label, note) VALUES "
               "('penalty-v1', 'Initial late-penalty tiers.')")
    for tier, days, pct in PENALTY_TIERS:
        op.execute(
            "INSERT INTO penalty_policy_tiers (version_id, tier, days_late, pct_of_original_interest) "
            f"SELECT id, {tier}, {days}, {pct} FROM penalty_policy_versions WHERE label = 'penalty-v1'"
        )

    if bind.dialect.name != 'postgresql':
        return

    # 2. quotes - interest rounded to whole Kina, half up (as calculate_prime)
    op.execute("""
        UPDATE loan_applications a SET
            pricing_version_id = pv.id,
            penalty_policy_version_id = ppv.id,
            quoted_interest_rate = t.interest_rate,
            quoted_interest_amount = round(a.amount_requested * t.interest_rate, 0),
            quoted_total_repayable = a.amount_requested + round(a.amount_requested * t.interest_rate, 0)
        FROM prime_pricing_versions pv
        JOIN prime_pricing_tiers t ON t.version_id = pv.id
        CROSS JOIN penalty_policy_versions ppv
        WHERE pv.label = 'prime-v1' AND ppv.label = 'penalty-v1'
          AND a.pricing_version_id IS NULL
          AND a.amount_requested BETWEEN t.min_amount AND t.max_amount
    """)

    # 3a. terms snapshots - the terms each loan was actually disbursed on
    op.execute(f"""
        INSERT INTO loan_terms_snapshots (
            loan_id, application_id, disbursement_id, pricing_version_id, penalty_policy_version_id,
            prime_category, principal, interest_rate, interest_amount, original_total_due, term_days,
            disbursed_at, disbursed_local_date, due_date, created_at, created_by)
        SELECT l.id, l.application_id, d.id, pv.id, ppv.id,
               a.prime_category, l.principal_amount, l.interest_rate,
               l.total_repayable - l.principal_amount, l.total_repayable, l.term_days,
               l.disbursed_at, {LOCAL_DATE.format(col='l.disbursed_at')}, rs.due_date,
               l.disbursed_at, d.recorded_by
        FROM loans l
        JOIN loan_applications a ON a.id = l.application_id
        LEFT JOIN disbursements d ON d.loan_id = l.id
        JOIN repayment_schedules rs ON rs.loan_id = l.id AND rs.installment_number = 1
        CROSS JOIN prime_pricing_versions pv
        CROSS JOIN penalty_policy_versions ppv
        WHERE pv.label = 'prime-v1' AND ppv.label = 'penalty-v1'
          AND l.term_days IS NOT NULL AND l.disbursed_at IS NOT NULL
    """)

    # 3b. original obligation per loan
    op.execute(f"""
        INSERT INTO loan_ledger_entries (
            loan_id, entry_type, amount, effective_date, created_at, created_by, created_by_kind,
            disbursement_id, note)
        SELECT l.id, 'original_obligation'::ledger_entry_type, l.total_repayable,
               {LOCAL_DATE.format(col='l.disbursed_at')}, l.disbursed_at, d.recorded_by,
               (CASE WHEN d.recorded_by IS NULL THEN 'system' ELSE 'admin' END)::ledger_actor_kind,
               d.id, 'backfilled from the loan record'
        FROM loans l
        LEFT JOIN disbursements d ON d.loan_id = l.id
        WHERE l.disbursed_at IS NOT NULL
    """)

    # 3c. one repayment entry per verified payment
    op.execute("""
        INSERT INTO loan_ledger_entries (
            loan_id, entry_type, amount, effective_date, created_at, created_by, created_by_kind,
            payment_transaction_id, note)
        SELECT p.loan_id, 'verified_repayment'::ledger_entry_type, -p.amount, p.payment_date,
               coalesce(p.paid_at, p.created_at), v.actor_id,
               (CASE WHEN v.actor_id IS NULL THEN 'system' ELSE 'admin' END)::ledger_actor_kind,
               p.id, 'backfilled from the payment record'
        FROM payment_transactions p
        LEFT JOIN LATERAL (
            SELECT al.actor_id FROM audit_logs al
            WHERE al.action = 'payment_verified' AND al.entity_type = 'PaymentTransaction'
              AND al.entity_id = p.id::text
            ORDER BY al.id DESC LIMIT 1
        ) v ON true
        WHERE p.status = 'verified'
    """)

    # 3d. closure records for closed loans
    op.execute("""
        INSERT INTO loan_closures (
            loan_id, closed_at, closure_reason, closed_by, closing_payment_transaction_id,
            original_total_due, total_penalties, total_verified_paid, outstanding_at_closure,
            final_payment_date, repayment_duration_days, timeliness)
        SELECT l.id,
               coalesce(c.created_at, now()),
               coalesce(l.closure_reason, 'paid_in_full'::loan_closure_reason),
               c.actor_id,
               last_pay.id,
               s.original_total_due,
               0,
               coalesce(paid.total, 0),
               s.original_total_due - coalesce(paid.total, 0),
               last_pay.payment_date,
               greatest(0, last_pay.payment_date - s.disbursed_local_date),
               CASE
                   WHEN last_pay.payment_date IS NULL THEN NULL
                   WHEN last_pay.payment_date <= s.due_date THEN 'on_time'
                   WHEN last_pay.payment_date - s.due_date < 7 THEN 'late_no_penalty'
                   WHEN last_pay.payment_date - s.due_date < 14 THEN 'late_tier_1'
                   ELSE 'late_tier_2'
               END::loan_timeliness
        FROM loans l
        JOIN loan_terms_snapshots s ON s.loan_id = l.id
        LEFT JOIN LATERAL (
            SELECT al.created_at, al.actor_id FROM audit_logs al
            WHERE al.action IN ('loan_closed', 'loan_written_off')
              AND al.entity_type = 'Loan' AND al.entity_id = l.id::text
            ORDER BY al.id DESC LIMIT 1
        ) c ON true
        LEFT JOIN LATERAL (
            SELECT sum(p.amount) AS total FROM payment_transactions p
            WHERE p.loan_id = l.id AND p.status = 'verified'
        ) paid ON true
        LEFT JOIN LATERAL (
            SELECT p.id, p.payment_date FROM payment_transactions p
            WHERE p.loan_id = l.id AND p.status = 'verified'
            ORDER BY p.payment_date DESC, p.id DESC LIMIT 1
        ) last_pay ON true
        WHERE l.status = 'closed'
    """)


def downgrade():
    bind = op.get_bind()
    is_pg = bind.dialect.name == 'postgresql'
    tables = ('loan_closures', 'loan_ledger_entries', 'loan_terms_snapshots',
              'penalty_policy_tiers', 'penalty_policy_versions',
              'prime_pricing_tiers', 'prime_pricing_versions')
    if is_pg:
        # The insert-only triggers (M8) would refuse these deletes.
        for table in tables:
            op.execute(f"ALTER TABLE {table} DISABLE TRIGGER {table}_insert_only")
        op.execute("ALTER TABLE loan_applications DISABLE TRIGGER loan_applications_quote_locked")

    op.execute("UPDATE loan_applications SET pricing_version_id = NULL, penalty_policy_version_id = NULL, "
               "quoted_interest_rate = NULL, quoted_interest_amount = NULL, quoted_total_repayable = NULL")
    for table in tables:
        op.execute(f"DELETE FROM {table}")

    if is_pg:
        for table in tables:
            op.execute(f"ALTER TABLE {table} ENABLE TRIGGER {table}_insert_only")
        op.execute("ALTER TABLE loan_applications ENABLE TRIGGER loan_applications_quote_locked")
