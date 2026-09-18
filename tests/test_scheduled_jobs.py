"""Daily repayment-schedule maintenance jobs (app/services/repayments_scheduler.py):
flip_overdue_installments() and send_due_soon_reminders(). Exercised the same
way scripts/send_due_reminders.py drives them - manually backdating a
schedule row's due_date and confirming the job reacts correctly, per the
manual-verification step in the task."""

from datetime import date, timedelta
from decimal import Decimal

from app.extensions import db
from app.models import AuditLog, Loan, LoanApplication, RepaymentSchedule
from app.models.enums import (
    LoanApplicationStatus,
    LoanStatus,
    RepaymentFrequency,
    RepaymentStatus,
)
from app.services import repayments_scheduler
from app.services.interest_calculation import amortize


def _make_active_loan(user, *, term_months=3):
    """A disbursed, ACTIVE loan with a generated schedule - due dates land in
    the future relative to "today" (disbursed just now), same as a real
    approval would produce."""
    application = LoanApplication(
        user_id=user.id,
        amount_requested=Decimal("300"),
        term_months=term_months,
        repayment_frequency=RepaymentFrequency.MONTHLY,
        status=LoanApplicationStatus.APPROVED,
    )
    db.session.add(application)
    db.session.flush()

    terms = amortize(300, "0.18", term_months, RepaymentFrequency.MONTHLY)
    loan = Loan(
        application_id=application.id,
        user_id=user.id,
        principal_amount=Decimal("300"),
        interest_rate=Decimal("0.18"),
        term_months=term_months,
        monthly_payment=terms["installment_amount"],
        total_repayable=terms["total_repayable"],
        status=LoanStatus.ACTIVE,
        disbursed_at=None,
    )
    db.session.add(loan)
    db.session.flush()

    repayments_scheduler.generate_schedule(loan, RepaymentFrequency.MONTHLY)
    db.session.commit()
    return loan


def _backdate(row: RepaymentSchedule, days_ago: int):
    row.due_date = date.today() - timedelta(days=days_ago)
    db.session.commit()


def _bring_forward(row: RepaymentSchedule, days_from_now: int):
    row.due_date = date.today() + timedelta(days=days_from_now)
    db.session.commit()


# ===================================================== flip_overdue_installments
def test_backdated_unpaid_installment_is_flagged_overdue(app, make_user):
    user = make_user("customer")
    loan = _make_active_loan(user)
    row = loan.repayment_schedule[0]
    _backdate(row, days_ago=5)  # manually backdate, as the task asks

    flipped = repayments_scheduler.flip_overdue_installments()

    assert row.id in [r.id for r in flipped]
    db.session.refresh(row)
    assert row.status == RepaymentStatus.OVERDUE


def test_flip_overdue_writes_an_audit_row(app, make_user):
    user = make_user("customer")
    loan = _make_active_loan(user)
    row = loan.repayment_schedule[0]
    _backdate(row, days_ago=1)

    repayments_scheduler.flip_overdue_installments()

    entry = AuditLog.query.filter_by(
        action="repayment_marked_overdue", entity_type="RepaymentSchedule", entity_id=str(row.id)
    ).first()
    assert entry is not None
    assert entry.actor_id is None  # system job, not a user action
    assert entry.details["loan_id"] == loan.id
    assert entry.details["due_date"] == row.due_date.isoformat()


def test_future_installment_is_not_flagged(app, make_user):
    user = make_user("customer")
    loan = _make_active_loan(user)
    row = loan.repayment_schedule[0]
    _bring_forward(row, days_from_now=10)

    flipped = repayments_scheduler.flip_overdue_installments()

    assert row.id not in [r.id for r in flipped]
    db.session.refresh(row)
    assert row.status == RepaymentStatus.UPCOMING


def test_paid_installment_is_never_flagged_even_if_past_due(app, make_user):
    user = make_user("customer")
    loan = _make_active_loan(user)
    row = loan.repayment_schedule[0]
    row.status = RepaymentStatus.PAID
    row.amount_paid = row.amount_due
    _backdate(row, days_ago=5)

    flipped = repayments_scheduler.flip_overdue_installments()

    assert row.id not in [r.id for r in flipped]


def test_flip_overdue_handles_multiple_rows_across_multiple_users_in_one_run(app, make_user):
    """The job runs as one batch query + loop, not per-installment - make
    sure that actually scales past a single row without cross-contaminating
    other users' or other loans' rows."""
    user_a = make_user("customer")
    user_b = make_user("customer")
    loan_a = _make_active_loan(user_a, term_months=3)  # 3 installments
    loan_b = _make_active_loan(user_b, term_months=2)  # 2 installments

    # Backdate installments 1 and 2 of loan_a, and installment 1 of loan_b.
    # Leave loan_a's #3 and loan_b's #2 in the future.
    _backdate(loan_a.repayment_schedule[0], days_ago=10)
    _backdate(loan_a.repayment_schedule[1], days_ago=1)
    _backdate(loan_b.repayment_schedule[0], days_ago=3)

    flipped = repayments_scheduler.flip_overdue_installments()

    assert {r.id for r in flipped} == {
        loan_a.repayment_schedule[0].id,
        loan_a.repayment_schedule[1].id,
        loan_b.repayment_schedule[0].id,
    }
    assert loan_a.repayment_schedule[2].status == RepaymentStatus.UPCOMING
    assert loan_b.repayment_schedule[1].status == RepaymentStatus.UPCOMING
    assert (
        AuditLog.query.filter_by(action="repayment_marked_overdue").count() == 3
    )


def test_flip_overdue_is_idempotent(app, make_user):
    user = make_user("customer")
    loan = _make_active_loan(user)
    row = loan.repayment_schedule[0]
    _backdate(row, days_ago=5)

    first = repayments_scheduler.flip_overdue_installments()
    second = repayments_scheduler.flip_overdue_installments()

    assert len(first) == 1
    assert len(second) == 0  # already-overdue rows aren't reprocessed
    count = AuditLog.query.filter_by(action="repayment_marked_overdue").count()
    assert count == 1  # not logged twice


# ======================================================= send_due_soon_reminders
def test_installment_due_within_lead_window_triggers_a_reminder_attempt(app, make_user):
    user = make_user("customer")
    loan = _make_active_loan(user)
    row = loan.repayment_schedule[0]
    lead = app.config["REPAYMENT_REMINDER_LEAD_DAYS"]
    _bring_forward(row, days_from_now=max(0, lead - 1))

    results = repayments_scheduler.send_due_soon_reminders()

    matched = [r for r in results if r["row"].id == row.id]
    assert len(matched) == 1
    # NOTIFICATIONS_ENABLED is off in TestingConfig, so it's attempted, not sent.
    assert matched[0]["outcome"]["sent"] is False

    entry = AuditLog.query.filter_by(
        action="repayment_reminder_not_sent", entity_type="RepaymentSchedule", entity_id=str(row.id)
    ).first()
    assert entry is not None
    assert entry.details["sent"] is False


def test_installment_outside_lead_window_is_not_matched(app, make_user):
    user = make_user("customer")
    loan = _make_active_loan(user)
    row = loan.repayment_schedule[0]
    lead = app.config["REPAYMENT_REMINDER_LEAD_DAYS"]
    _bring_forward(row, days_from_now=lead + 30)

    results = repayments_scheduler.send_due_soon_reminders()

    assert row.id not in [r["row"].id for r in results]


def test_already_overdue_installment_is_not_re_reminded_as_due_soon(app, make_user):
    """An overdue installment gets a different treatment (flip_overdue_installments,
    tested above) - it must not also show up in the due-soon reminder pass."""
    user = make_user("customer")
    loan = _make_active_loan(user)
    row = loan.repayment_schedule[0]
    _backdate(row, days_ago=3)

    results = repayments_scheduler.send_due_soon_reminders()
    assert row.id not in [r["row"].id for r in results]


def test_paid_installment_is_not_reminded(app, make_user):
    user = make_user("customer")
    loan = _make_active_loan(user)
    row = loan.repayment_schedule[0]
    row.status = RepaymentStatus.PAID
    row.amount_paid = row.amount_due
    db.session.commit()
    _bring_forward(row, days_from_now=1)

    results = repayments_scheduler.send_due_soon_reminders()
    assert row.id not in [r["row"].id for r in results]


def test_reminder_only_applies_to_active_loans(app, make_user):
    user = make_user("customer")
    loan = _make_active_loan(user)
    row = loan.repayment_schedule[0]
    _bring_forward(row, days_from_now=1)
    loan.status = LoanStatus.COMPLETED  # e.g. paid off through some other path
    db.session.commit()

    results = repayments_scheduler.send_due_soon_reminders()
    assert row.id not in [r["row"].id for r in results]


# ============================================== the safety-net stays in place
def test_on_read_overdue_computation_still_works_without_the_job(app, make_user):
    """reporting.py's _is_overdue() must keep working even if the scheduled
    job never ran - it's the safety net, not a replacement."""
    from app.services.reporting import _is_overdue

    user = make_user("customer")
    loan = _make_active_loan(user)
    row = loan.repayment_schedule[0]
    _backdate(row, days_ago=5)  # status is still 'upcoming' - job hasn't run

    assert row.status == RepaymentStatus.UPCOMING
    assert _is_overdue(row) is True
