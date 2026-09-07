"""Repayments Scheduler - build the full RepaymentSchedule for a loan."""

from datetime import date, timedelta
from decimal import Decimal

from app.extensions import db
from app.models import RepaymentSchedule
from app.models.enums import RepaymentFrequency, RepaymentStatus

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
