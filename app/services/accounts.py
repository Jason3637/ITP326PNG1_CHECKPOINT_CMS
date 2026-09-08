"""Account tracking - the customer dashboard read model."""

from datetime import date
from decimal import Decimal

from app.models import Loan, RepaymentSchedule
from app.models.enums import LoanStatus, RepaymentStatus

_CENTS = Decimal("0.01")


def _loan_progress(loan: Loan) -> dict:
    rows = loan.repayment_schedule
    total_due = sum((Decimal(r.amount_due) for r in rows), Decimal("0"))
    total_paid = sum((Decimal(r.amount_paid) for r in rows), Decimal("0"))
    paid_count = sum(1 for r in rows if r.status == RepaymentStatus.PAID)
    overdue_count = sum(1 for r in rows if r.status == RepaymentStatus.OVERDUE)

    upcoming = sorted(
        (r for r in rows if r.status != RepaymentStatus.PAID),
        key=lambda r: (r.due_date, r.installment_number),
    )
    next_due = upcoming[0] if upcoming else None

    pct = float((total_paid / total_due * 100).quantize(Decimal("0.1"))) if total_due else 0.0

    return {
        "loan_id": loan.id,
        "status": str(loan.status),
        "principal_amount": float(Decimal(loan.principal_amount)),
        "interest_rate": float(Decimal(loan.interest_rate)),
        "total_repayable": float(Decimal(loan.total_repayable)),
        "installment_amount": float(Decimal(loan.monthly_payment)),
        "installments_total": len(rows),
        "installments_paid": paid_count,
        "installments_overdue": overdue_count,
        "amount_paid": float(total_paid.quantize(_CENTS)),
        "amount_remaining": float((total_due - total_paid).quantize(_CENTS)),
        "progress_percent": pct,
        "disbursed_at": loan.disbursed_at.isoformat() if loan.disbursed_at else None,
        "next_repayment": (
            None
            if next_due is None
            else {
                "installment_number": next_due.installment_number,
                "due_date": next_due.due_date.isoformat(),
                "amount_due": float(Decimal(next_due.amount_due)),
                "amount_paid": float(Decimal(next_due.amount_paid)),
                "status": str(next_due.status),
                "days_until_due": (next_due.due_date - date.today()).days,
            }
        ),
    }


def get_account_summary(user_id: int) -> dict:
    loans = (
        Loan.query.filter_by(user_id=user_id).order_by(Loan.id.desc()).all()
    )
    active = [l for l in loans if l.status == LoanStatus.ACTIVE]

    active_summaries = [_loan_progress(l) for l in active]
    next_repayment = None
    candidates = [s["next_repayment"] for s in active_summaries if s["next_repayment"]]
    if candidates:
        next_repayment = min(candidates, key=lambda n: n["due_date"])

    return {
        "user_id": user_id,
        "counts": {
            "active": len(active),
            "completed": sum(1 for l in loans if l.status == LoanStatus.COMPLETED),
            "defaulted": sum(1 for l in loans if l.status == LoanStatus.DEFAULTED),
            "total": len(loans),
        },
        "active_loans": active_summaries,
        "next_repayment_due": next_repayment,
        "has_overdue": any(s["installments_overdue"] > 0 for s in active_summaries),
    }
