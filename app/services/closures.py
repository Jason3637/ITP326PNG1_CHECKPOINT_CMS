"""Closing a loan: the LoanClosure record (insert-only) and Loan.status.

Two ways in:
  * paid in full - automatic, from payment_processing.verify_payment(), in
    the same transaction as the payment that brought the ledger to zero;
  * written off - an admin's explicit action (loan_processing.write_off_loan).
Neither commits: the caller's transaction does.
"""

from datetime import datetime, timezone
from decimal import Decimal

from app.extensions import db
from app.models import LoanClosure, PaymentTransaction
from app.models.enums import LoanClosureReason, LoanStatus, LoanTimeliness, PaymentStatus

from . import audit, ledger


def _tier_days(loan) -> tuple[int, int]:
    """Days late at which penalty tiers 1 and 2 start, from the policy the
    loan was disbursed under (7 and 14 in version 1)."""
    snap = loan.terms_snapshot
    tiers = sorted(snap.penalty_policy_version.tiers, key=lambda t: t.tier) if snap else []
    days = [t.days_late for t in tiers] + [7, 14]
    return days[0], days[1] if len(tiers) != 1 else days[0]


def timeliness(loan, final_payment_date) -> LoanTimeliness | None:
    snap = loan.terms_snapshot
    if final_payment_date is None or snap is None:
        return None
    late = (final_payment_date - snap.due_date).days
    tier1, tier2 = _tier_days(loan)
    if late <= 0:
        return LoanTimeliness.ON_TIME
    if late < tier1:
        return LoanTimeliness.LATE_NO_PENALTY
    if late < tier2:
        return LoanTimeliness.LATE_TIER_1
    return LoanTimeliness.LATE_TIER_2


def record_closure(loan, reason: LoanClosureReason, *, actor_id, closing_payment=None) -> LoanClosure:
    totals = ledger.totals(loan.id)
    last = (
        PaymentTransaction.query.filter_by(loan_id=loan.id, status=PaymentStatus.VERIFIED)
        .order_by(PaymentTransaction.payment_date.desc(), PaymentTransaction.id.desc())
        .first()
    )
    final_date = last.payment_date if last else None
    snap = loan.terms_snapshot
    duration = None
    if final_date and snap:
        duration = max(0, (final_date - snap.disbursed_local_date).days)

    closure = LoanClosure(
        loan_id=loan.id,
        closed_at=datetime.now(timezone.utc),
        closure_reason=reason,
        closed_by=actor_id,
        closing_payment_transaction_id=closing_payment.id if closing_payment else None,
        original_total_due=totals["original_obligation"],
        total_penalties=totals["penalties"],
        total_verified_paid=totals["verified_repayments"],
        outstanding_at_closure=max(Decimal("0.00"), totals["outstanding"]),
        final_payment_date=final_date,
        repayment_duration_days=duration,
        timeliness=timeliness(loan, final_date),
    )
    db.session.add(closure)
    loan.status = LoanStatus.CLOSED
    loan.closure_reason = reason
    db.session.flush()
    audit.record(
        "loan_closed" if reason == LoanClosureReason.PAID_IN_FULL else "loan_written_off",
        actor_id=actor_id,
        entity_type="Loan",
        entity_id=loan.id,
        details={
            "closure_id": closure.id,
            "closure_reason": reason.value,
            "automatic": reason == LoanClosureReason.PAID_IN_FULL,
            "closing_payment_transaction_id": closure.closing_payment_transaction_id,
            "total_verified_paid": float(closure.total_verified_paid),
            "outstanding_at_closure": float(closure.outstanding_at_closure),
            "timeliness": closure.timeliness.value if closure.timeliness else None,
        },
        commit=False,
    )
    return closure
