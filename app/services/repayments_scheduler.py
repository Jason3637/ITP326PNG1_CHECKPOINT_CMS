"""Repayments Scheduler - build the full RepaymentSchedule for a loan, and the
two daily maintenance jobs that keep it current (see scripts/send_due_reminders.py).
"""

from datetime import date, timedelta
from decimal import Decimal

from flask import current_app

from app.extensions import db
from app.models import RepaymentSchedule
from app.models.enums import LoanStatus, RepaymentFrequency, RepaymentStatus

from . import audit, notifications
from .interest_calculation import installment_count

_CENTS = Decimal("0.01")


def _add_months(d: date, months: int) -> date:
    """Add whole months, clamping the day to the target month's last day."""
    total = d.month - 1 + months
    year = d.year + total // 12
    month = total % 12 + 1
    # last day of target month
    if month == 12:
        last = 31
    else:
        last = (date(year, month + 1, 1) - timedelta(days=1)).day
    return date(year, month, min(d.day, last))


def _due_date(start: date, index: int, frequency: RepaymentFrequency) -> date:
    """Due date of installment `index` (1-based)."""
    if frequency == RepaymentFrequency.MONTHLY:
        return _add_months(start, index)
    if frequency == RepaymentFrequency.BIWEEKLY:
        return start + timedelta(weeks=2 * index)
    return start + timedelta(weeks=index)  # WEEKLY


def generate_schedule(
    loan,
    frequency: RepaymentFrequency,
    *,
    start_date: date | None = None,
    flush: bool = True,
) -> list[RepaymentSchedule]:
    """Create one RepaymentSchedule row per installment and add them to the
    session. Returns the list (caller commits).
    """
    if loan.repayment_schedule:
        raise ValueError(f"Loan {loan.id} already has a repayment schedule.")

    n = installment_count(loan.term_months, frequency)
    start = start_date or (
        loan.disbursed_at.date() if loan.disbursed_at else date.today()
    )
    installment = Decimal(loan.monthly_payment)
    total = Decimal(loan.total_repayable)

    rows: list[RepaymentSchedule] = []
    running = Decimal("0.00")
    for i in range(1, n + 1):
        if i < n:
            amount = installment
            running += amount
        else:
            # Last installment absorbs the rounding remainder.
            amount = (total - running).quantize(_CENTS)
        row = RepaymentSchedule(
            loan=loan,
            installment_number=i,
            due_date=_due_date(start, i, frequency),
            amount_due=amount,
            amount_paid=Decimal("0.00"),
            status=RepaymentStatus.UPCOMING,
        )
        db.session.add(row)
        rows.append(row)

    if flush:
        db.session.flush()
    return rows


# =========================================================== daily maintenance
# The two jobs below are meant to run on a schedule (see
# scripts/send_due_reminders.py and DEPLOYMENT.md's "Scheduled job" section) -
# nothing here depends on being called from a request, so both are safe to
# call from a CLI script under a bare app context.


def flip_overdue_installments() -> list[RepaymentSchedule]:
    """Proactively transition schedule rows into ``overdue`` once their due
    date has passed with no full payment, and audit every transition.

    This is the PROACTIVE counterpart to the on-read overdue check in
    ``reporting.py``'s ``_is_overdue()`` / ``accounts.py``'s
    ``installments_overdue`` count. Deliberately does not replace that
    on-read check: if this job is delayed or skipped for a day, the read path
    still reports the correct (computed) overdue state - it just isn't
    reflected in the *stored* `status` column, and hence in `accounts.py`'s
    ``installments_overdue`` / ``has_overdue`` (which read the stored value),
    until this job next runs. Idempotent - only currently-`upcoming` rows are
    touched, so re-running it is always safe.
    """
    today = date.today()
    rows = (
        RepaymentSchedule.query.filter(
            RepaymentSchedule.status == RepaymentStatus.UPCOMING,
            RepaymentSchedule.due_date < today,
        ).all()
    )

    for row in rows:
        row.status = RepaymentStatus.OVERDUE
        audit.record(
            "repayment_marked_overdue",
            actor_id=None,  # system job, not a user action
            entity_type="RepaymentSchedule",
            entity_id=row.id,
            details={
                "loan_id": row.loan_id,
                "installment_number": row.installment_number,
                "due_date": row.due_date.isoformat(),
                "amount_due": float(Decimal(row.amount_due)),
                "amount_paid": float(Decimal(row.amount_paid)),
            },
            commit=False,
        )

    if rows:
        db.session.commit()
    return rows


def send_due_soon_reminders() -> list[dict]:
    """Email each borrower with an installment due within
    ``REPAYMENT_REMINDER_LEAD_DAYS`` days, and audit every attempt (sent or
    not - e.g. notifications disabled/SMTP unconfigured still gets a row, so
    the ledger reflects what was *attempted*, not just what succeeded).

    Returns one ``{"row": RepaymentSchedule, "outcome": {...}}`` dict per
    matched installment, in the shape ``notifications.notify_repayment_due_soon``
    returns, for the caller (the script) to report on.
    """
    lead = current_app.config["REPAYMENT_REMINDER_LEAD_DAYS"]
    today = date.today()
    window_end = today + timedelta(days=lead)

    rows = (
        RepaymentSchedule.query.join(RepaymentSchedule.loan)
        .filter(
            RepaymentSchedule.status != RepaymentStatus.PAID,
            RepaymentSchedule.due_date >= today,
            RepaymentSchedule.due_date <= window_end,
        )
        .all()
    )
    rows = [r for r in rows if r.loan.status == LoanStatus.ACTIVE]

    results = []
    for row in rows:
        outcome = notifications.notify_repayment_due_soon(row)
        audit.record(
            "repayment_reminder_sent" if outcome.get("sent") else "repayment_reminder_not_sent",
            actor_id=None,
            entity_type="RepaymentSchedule",
            entity_id=row.id,
            details={
                "loan_id": row.loan_id,
                "installment_number": row.installment_number,
                "due_date": row.due_date.isoformat(),
                "sent": outcome.get("sent"),
                "reason": outcome.get("reason"),
            },
            commit=False,
        )
        results.append({"row": row, "outcome": outcome})

    if rows:
        db.session.commit()
    return results
