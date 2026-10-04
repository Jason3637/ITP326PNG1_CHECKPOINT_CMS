"""Read side of the loan ledger. Every balance, total paid and penalty
figure the API reports comes from here - SUMs over loan_ledger_entries -
never from a stored, editable amount.
"""

from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import case, func

from app.extensions import db
from app.models import LoanLedgerEntry
from app.models.enums import LedgerEntryType

from . import prime_pricing

_ZERO = Decimal("0.00")
_CENTS = Decimal("0.01")


def balance_subquery():
    """(loan_id, balance) for every loan with ledger entries - for queries
    that filter or sort many loans by what they still owe."""
    return (
        db.session.query(
            LoanLedgerEntry.loan_id.label("loan_id"),
            func.sum(LoanLedgerEntry.amount).label("balance"),
        )
        .group_by(LoanLedgerEntry.loan_id)
        .subquery()
    )


def balance(loan_id: int) -> Decimal:
    value = (
        db.session.query(func.coalesce(func.sum(LoanLedgerEntry.amount), 0))
        .filter(LoanLedgerEntry.loan_id == loan_id)
        .scalar()
    )
    return Decimal(value).quantize(_CENTS)


def totals(loan_id: int) -> dict:
    """original obligation, penalties, verified repayments (as a positive
    amount) and outstanding balance for one loan."""
    by_type = dict(
        db.session.query(LoanLedgerEntry.entry_type, func.sum(LoanLedgerEntry.amount))
        .filter(LoanLedgerEntry.loan_id == loan_id)
        .group_by(LoanLedgerEntry.entry_type)
        .all()
    )
    get = lambda t: Decimal(by_type.get(t) or 0).quantize(_CENTS)  # noqa: E731
    original = get(LedgerEntryType.ORIGINAL_OBLIGATION)
    penalties = get(LedgerEntryType.PENALTY)
    repaid = -get(LedgerEntryType.VERIFIED_REPAYMENT)
    return {
        "original_obligation": original,
        "penalties": penalties,
        "verified_repayments": repaid,
        "outstanding": original + penalties - repaid,
    }


def today_local() -> date:
    return prime_pricing.local_date(datetime.now(timezone.utc))


def days_overdue(due_date: date | None, outstanding: Decimal, today: date | None = None) -> int:
    if due_date is None or outstanding <= 0:
        return 0
    return max(0, ((today or today_local()) - due_date).days)


def serialize_entry(e: LoanLedgerEntry) -> dict:
    return {
        "id": e.id,
        "entry_type": e.entry_type.value,
        "amount": float(e.amount),
        "effective_date": e.effective_date.isoformat(),
        "created_at": e.created_at.isoformat() if e.created_at else None,
        "created_by": e.created_by,
        "created_by_kind": e.created_by_kind.value,
        "disbursement_id": e.disbursement_id,
        "payment_transaction_id": e.payment_transaction_id,
        "penalty_tier": e.penalty_tier,
        "note": e.note,
    }


def sum_where(*filters) -> Decimal:
    """SUM(amount) over entries matching `filters` (signed as stored)."""
    value = db.session.query(func.coalesce(func.sum(LoanLedgerEntry.amount), 0)).filter(*filters).scalar()
    return Decimal(value).quantize(_CENTS)


def repaid_expr():
    """SUM of verified repayments as a positive number, for aggregate queries."""
    return func.coalesce(
        func.sum(case((LoanLedgerEntry.entry_type == LedgerEntryType.VERIFIED_REPAYMENT,
                       -LoanLedgerEntry.amount), else_=0)),
        0,
    )
