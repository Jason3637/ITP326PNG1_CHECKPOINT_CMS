"""loan officer workflow - data backfill (M2)

Revision ID: f72b8f821647
Revises: 2b12227f1932
Create Date: 2026-10-01 14:05:00.000000

NEW LOAN OFFICER WORK ONLY. Best-effort backfill of the new tables from
what already exists, so in-flight applications aren't stranded:

  1. information_requests - one OPEN row per application currently in
     customer_action_required, from loan_applications.action_required_note
     (falling back to the audit entry's note), attributed to the officer
     and time on the latest loan_application_customer_action_requested
     audit entry. ABORTS if any such application can't be attributed - a
     request row must name its requester, and silently skipping an open
     request would leave a customer's pending action invisible.
     Already-answered historical rounds are NOT reconstructed: they stay in
     the audit log, where they've always been.

  2. officer_recommendations - one recommend_approval row per
     loan_application_recommended_for_approval audit entry.
     checklist_snapshot records {"backfilled_from_audit_log": <audit id>}
     because no checklist existed then; credit_evaluation_snapshot stays
     NULL because the value the officer saw at the time wasn't recorded.
     Audit entries whose actor was since deleted (actor_id NULL) are
     skipped and counted in the migration log - the audit log keeps them.

  3. loan_applications.assigned_officer_id / assigned_at - for open
     applications past SUBMITTED, from the latest
     loan_application_officer_review_started audit entry (skipped where its
     actor no longer exists - those simply return to the shared queue).

verification_items are deliberately NOT backfilled: there's no evidence of
which checks were done before the checklist existed. The service layer
creates pending rows the first time an application without any is opened.

Postgres only (the SQL relies on JSONB and LATERAL); a no-op elsewhere,
like the other data steps in this migration chain.
"""
import logging

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'f72b8f821647'
down_revision = '2b12227f1932'
branch_labels = None
depends_on = None

logger = logging.getLogger('alembic.runtime.migration')

_OPEN_PAST_SUBMITTED = (
    "'officer_review', 'customer_action_required', "
    "'recommended_for_approval', 'admin_review'"
)


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name != 'postgresql':
        return

    # ---- 1. open customer-action rounds -> information_requests ----
    open_rounds = """
        SELECT la.id AS application_id,
               COALESCE(NULLIF(TRIM(la.action_required_note), ''),
                        NULLIF(TRIM(al.details->>'note'), '')) AS reason,
               al.actor_id,
               al.created_at
        FROM loan_applications la
        LEFT JOIN LATERAL (
            SELECT a.actor_id, a.created_at, a.details
            FROM audit_logs a
            WHERE a.action = 'loan_application_customer_action_requested'
              AND a.entity_type = 'LoanApplication'
              AND a.entity_id = la.id::text
            ORDER BY a.created_at DESC, a.id DESC
            LIMIT 1
        ) al ON true
        WHERE la.status = 'customer_action_required'
    """
    unattributable = bind.execute(sa.text(
        f"SELECT application_id FROM ({open_rounds}) r "
        "WHERE r.actor_id IS NULL OR r.reason IS NULL"
    )).scalars().all()
    if unattributable:
        raise RuntimeError(
            "Cannot backfill information_requests: application(s) "
            f"{unattributable} are in customer_action_required but have no "
            "attributable loan_application_customer_action_requested audit "
            "entry (or no note). Resolve these by hand (e.g. resume review) "
            "and re-run the migration."
        )
    inserted = bind.execute(sa.text(
        f"""
        INSERT INTO information_requests
            (loan_application_id, request_type, reason, status, requested_by, requested_at)
        SELECT application_id, 'other', LEFT(reason, 1000), 'open', actor_id, created_at
        FROM ({open_rounds}) r
        """
    )).rowcount
    logger.info("backfill: %s open information_requests created", inserted)

    # ---- 2. past recommendations -> officer_recommendations ----
    skipped = bind.execute(sa.text(
        """
        SELECT count(*) FROM audit_logs
        WHERE action = 'loan_application_recommended_for_approval'
          AND entity_type = 'LoanApplication'
          AND actor_id IS NULL
        """
    )).scalar_one()
    inserted = bind.execute(sa.text(
        """
        INSERT INTO officer_recommendations
            (loan_application_id, officer_id, recommendation, comments,
             checklist_snapshot, credit_evaluation_snapshot, created_at)
        SELECT la.id,
               a.actor_id,
               'recommend_approval',
               COALESCE(NULLIF(TRIM(a.details->>'note'), ''),
                        '(no comment recorded - backfilled from audit log)'),
               jsonb_build_object('backfilled_from_audit_log', a.id),
               NULL,
               a.created_at
        FROM audit_logs a
        JOIN loan_applications la ON la.id::text = a.entity_id
        WHERE a.action = 'loan_application_recommended_for_approval'
          AND a.entity_type = 'LoanApplication'
          AND a.actor_id IS NOT NULL
        ORDER BY a.created_at, a.id
        """
    )).rowcount
    logger.info(
        "backfill: %s officer_recommendations created, %s skipped (actor deleted)",
        inserted, skipped,
    )

    # ---- 3. claim-on-review ownership for in-flight applications ----
    updated = bind.execute(sa.text(
        f"""
        UPDATE loan_applications la
        SET assigned_officer_id = s.actor_id, assigned_at = s.created_at
        FROM (
            SELECT DISTINCT ON (a.entity_id) a.entity_id, a.actor_id, a.created_at
            FROM audit_logs a
            WHERE a.action = 'loan_application_officer_review_started'
              AND a.entity_type = 'LoanApplication'
            ORDER BY a.entity_id, a.created_at DESC, a.id DESC
        ) s
        WHERE la.id::text = s.entity_id
          AND s.actor_id IS NOT NULL
          AND la.assigned_officer_id IS NULL
          AND la.status IN ({_OPEN_PAST_SUBMITTED})
        """
    )).rowcount
    logger.info("backfill: %s applications assigned to their reviewing officer", updated)


def downgrade():
    # Nothing to undo separately: downgrading 2b12227f1932 next drops the
    # tables and columns this filled. (Backfilled rows can't be told apart
    # from rows created after the upgrade, so they aren't deleted here.)
    pass
