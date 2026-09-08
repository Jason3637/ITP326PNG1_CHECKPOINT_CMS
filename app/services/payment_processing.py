"""Payment Processing - record a payment against a repayment installment.

Access (deliberate MVP choice - see BACKEND.md):
  * the loan's own customer may pay their own installments;
  * a loan_officer / admin may also record a payment, for manual or over-the-
    counter (cash) entries. Every payment records who entered it and in what role.

Payments are marked COMPLETED immediately (no external gateway in this phase);
when a real gateway is added, create the row as PENDING and settle on callback.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from app.extensions import db
from app.models import Loan, PaymentTransaction, RepaymentSchedule, User
from app.models.enums import (
    LoanStatus,
    PaymentStatus,
    RepaymentStatus,
    UserRole,
)

from . import audit, notifications
from .errors import ServiceError

_CENTS = Decimal("0.01")
_STAFF_ROLES = (UserRole.LOAN_OFFICER, UserRole.ADMIN)


def record_payment(
    actor: User,
    *,
    repayment_schedule_id: int,
    amount,
    payment_method: str,
) -> dict:
    schedule = db.session.get(RepaymentSchedule, repayment_schedule_id)
    if schedule is None:
        raise ServiceError("Repayment schedule row not found.", 404)
    loan: Loan = schedule.loan

    is_staff = actor.role in _STAFF_ROLES
    if not is_staff and actor.id != loan.user_id:
        raise ServiceError("You can only pay your own loan.", 403)

    if loan.status != LoanStatus.ACTIVE:
        raise ServiceError(f"Loan #{loan.id} is {loan.status.value}, not active.", 409)
    if schedule.status == RepaymentStatus.PAID:
        raise ServiceError(
            f"Installment #{schedule.installment_number} is already paid.", 409
        )

    try:
        pay_amount = Decimal(str(amount)).quantize(_CENTS)
    except (InvalidOperation, TypeError):
        raise ServiceError("amount must be a number.")
    if pay_amount <= 0:
        raise ServiceError("amount must be positive.")

    method = (payment_method or "").strip()
    if not method:
        raise ServiceError("payment_method is required.")

    now = datetime.now(timezone.utc)
    txn = PaymentTransaction(
        loan_id=loan.id,
        repayment_schedule_id=schedule.id,
        amount=pay_amount,
        payment_method=method,
        status=PaymentStatus.COMPLETED,
        paid_at=now,
    )
    db.session.add(txn)

    schedule.amount_paid = (Decimal(schedule.amount_paid) + pay_amount).quantize(_CENTS)
    fully_covered = schedule.amount_paid >= Decimal(schedule.amount_due)
    if fully_covered:
        schedule.status = RepaymentStatus.PAID

    loan_completed = False
    if fully_covered:
        remaining = [
            r for r in loan.repayment_schedule if r.status != RepaymentStatus.PAID
        ]
        if not remaining:
            loan.status = LoanStatus.COMPLETED
            loan_completed = True

    db.session.flush()
    audit.record(
        "payment_recorded",
        actor_id=actor.id,
        entity_type="PaymentTransaction",
        entity_id=txn.id,
        details={
            "loan_id": loan.id,
            "repayment_schedule_id": schedule.id,
            "installment_number": schedule.installment_number,
            "amount": float(pay_amount),
            "payment_method": method,
            "entered_by_role": actor.role.value,
            "on_behalf": is_staff and actor.id != loan.user_id,
            "installment_status": schedule.status.value,
        },
        commit=False,
    )
    if loan_completed:
        audit.record(
            "loan_completed",
            actor_id=actor.id,
            entity_type="Loan",
            entity_id=loan.id,
            details={"total_repayable": float(Decimal(loan.total_repayable))},
            commit=False,
        )
    db.session.commit()

    notifications.notify_payment_received(txn, schedule)

    due = Decimal(schedule.amount_due)
    paid = Decimal(schedule.amount_paid)
    return {
        "transaction": {
            "id": txn.id,
            "loan_id": loan.id,
            "repayment_schedule_id": schedule.id,
            "amount": float(pay_amount),
            "payment_method": method,
            "status": str(txn.status),
            "paid_at": txn.paid_at.isoformat(),
        },
        "installment": {
            "installment_number": schedule.installment_number,
            "amount_due": float(due),
            "amount_paid": float(paid),
            "shortfall": float(max(Decimal("0"), due - paid).quantize(_CENTS)),
            "overpaid": float(max(Decimal("0"), paid - due).quantize(_CENTS)),
            "status": str(schedule.status),
        },
        "loan_status": str(loan.status),
        "loan_completed": loan_completed,
    }


def list_payments(loan_id: int, requester: User) -> list[dict]:
    loan = db.session.get(Loan, loan_id)
    if loan is None:
        raise ServiceError("Loan not found.", 404)
    if requester.role not in _STAFF_ROLES and requester.id != loan.user_id:
        raise ServiceError("You can only view your own loan's payments.", 403)
    rows = (
        PaymentTransaction.query.filter_by(loan_id=loan_id)
        .order_by(PaymentTransaction.created_at.asc())
        .all()
    )
    return [
        {
            "id": t.id,
            "repayment_schedule_id": t.repayment_schedule_id,
            "amount": float(Decimal(t.amount)),
            "payment_method": t.payment_method,
            "status": str(t.status),
            "paid_at": t.paid_at.isoformat() if t.paid_at else None,
        }
        for t in rows
    ]
