"""Payment Processing - report a payment, then verify it before the ledger moves.

Access (deliberate MVP choice - see BACKEND.md):
  * the loan's own customer may report their own installments as paid;
  * a loan_officer / admin may also report one, for manual or over-the-
    counter (cash) entries. Every payment records who entered it and in what role.

Decoupled reporting from settlement: `record_payment()` only creates the
transaction row (status REPORTED) - it never touches the ledger
(RepaymentSchedule.amount_paid/status, Loan.status). Only
`verify_payment(decision="verified")` does that, once staff have confirmed
the payment actually happened. This is what lets a customer's claim "I paid"
be recorded without immediately trusting it.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import Loan, LoanLedgerEntry, PaymentTransaction, RepaymentSchedule, User
from app.models.enums import (
    LedgerActorKind,
    LoanClosureReason,
    LedgerEntryType,
    LoanStatus,
    PaymentStatus,
    RepaymentStatus,
    UserRole,
)

from . import audit, documents, notifications
from .errors import ServiceError

_CENTS = Decimal("0.01")
_STAFF_ROLES = (UserRole.LOAN_OFFICER, UserRole.ADMIN)
_VERIFIABLE_STATUSES = (PaymentStatus.REPORTED, PaymentStatus.VERIFICATION_PENDING)


def record_payment(
    actor: User,
    *,
    repayment_schedule_id: int,
    amount,
    payment_method: str,
    payment_date=None,
    reference_number: str | None = None,
    document_ids: list[int] | None = None,
) -> dict:
    """Customer or staff reports a payment. Ledger is untouched here - see
    verify_payment(). `payment_date` defaults to today if omitted (the date
    the customer says they paid, which may be earlier than now).
    `document_ids` are receipt/screenshot uploads (via the existing
    POST /users/documents pattern - upload first, unlinked, then reference
    the id here) linked to this transaction.
    """
    try:
        repayment_schedule_id = int(repayment_schedule_id)
    except (TypeError, ValueError):
        raise ServiceError("repayment_schedule_id is required and must be an integer.")
    schedule = db.session.get(RepaymentSchedule, repayment_schedule_id)
    if schedule is None:
        raise ServiceError("Repayment schedule row not found.", 404)
    loan: Loan = schedule.loan

    is_staff = actor.role in _STAFF_ROLES
    if not is_staff and actor.id != loan.user_id:
        raise ServiceError("You can only report payment on your own loan.", 403)

    if loan.status not in (LoanStatus.ACTIVE, LoanStatus.OVERDUE):
        raise ServiceError(f"Loan #{loan.id} is {loan.status.value}, not active.", 409)
    from . import ledger

    # The ledger says what is owed - including any late-payment penalty, which
    # the schedule row (original amount only) doesn't know about. A loan with
    # entries but nothing owing can't take a payment; one with no entries yet
    # (the old amortized product) falls back to its installment.
    owing = ledger.balance(loan.id) if loan.ledger_entries else None
    if owing is not None and owing <= 0:
        raise ServiceError(f"Nothing is owed on loan #{loan.id}.", 409)
    if owing is None and schedule.status == RepaymentStatus.PAID:
        raise ServiceError(
            f"Installment #{schedule.installment_number} is already paid.", 409
        )

    try:
        pay_amount = Decimal(str(amount)).quantize(_CENTS)
    except (InvalidOperation, TypeError):
        raise ServiceError("amount must be a number.")
    if pay_amount <= 0:
        raise ServiceError("amount must be positive.")
    if owing is not None and pay_amount > owing:
        raise ServiceError(
            f"That is more than the K{owing:,.2f} still owed on this loan.", 400
        )

    method = (payment_method or "").strip()
    if not method:
        raise ServiceError("payment_method is required.")

    today = ledger.today_local()
    if payment_date is None:
        pay_date = today
    else:
        try:
            pay_date = payment_date if isinstance(payment_date, date) else date.fromisoformat(str(payment_date))
        except ValueError:
            raise ServiceError("payment_date must be an ISO date (YYYY-MM-DD).")
    if pay_date > today:
        raise ServiceError("payment_date cannot be in the future.")

    ref = (reference_number or "").strip() or None

    now = datetime.now(timezone.utc)
    txn = PaymentTransaction(
        loan_id=loan.id,
        repayment_schedule_id=schedule.id,
        amount=pay_amount,
        payment_method=method,
        payment_date=pay_date,
        reference_number=ref,
        status=PaymentStatus.REPORTED,
        reported_at=now,
    )
    db.session.add(txn)
    db.session.flush()

    if document_ids:
        documents.link_documents_to_payment(document_ids, actor, txn.id)

    audit.record(
        "payment_reported",
        actor_id=actor.id,
        entity_type="PaymentTransaction",
        entity_id=txn.id,
        details={
            "loan_id": loan.id,
            "repayment_schedule_id": schedule.id,
            "installment_number": schedule.installment_number,
            "amount": float(pay_amount),
            "payment_method": method,
            "payment_date": pay_date.isoformat(),
            "reference_number": ref,
            "document_ids": document_ids or [],
            "entered_by_role": actor.role.value,
            "on_behalf": is_staff and actor.id != loan.user_id,
        },
        commit=False,
    )
    db.session.commit()

    return _serialize_transaction(txn)


def start_payment_verification(actor: User, transaction_id: int) -> dict:
    """Staff-only optional claim step: REPORTED -> VERIFICATION_PENDING."""
    txn = db.session.get(PaymentTransaction, transaction_id)
    if txn is None:
        raise ServiceError("Payment transaction not found.", 404)
    if actor.role not in _STAFF_ROLES:
        raise ServiceError("Only staff may verify payments.", 403)
    if txn.status != PaymentStatus.REPORTED:
        raise ServiceError(
            f"Transaction #{txn.id} is {txn.status.value}, expected reported.", 409
        )
    txn.status = PaymentStatus.VERIFICATION_PENDING
    audit.record(
        "payment_verification_started",
        actor_id=actor.id,
        entity_type="PaymentTransaction",
        entity_id=txn.id,
        details={},
        commit=False,
    )
    db.session.commit()
    return _serialize_transaction(txn)


def verify_payment(
    actor: User, transaction_id: int, *, decision: str, note: str | None = None
) -> dict:
    """Admin-only - the actual ledger-affecting decision. `decision` is
    "verified" or "rejected" (a `note` reason is required for "rejected";
    a rejection never writes a ledger entry).

    "verified" is ONE database transaction, all or nothing:
      (a) the PaymentTransaction -> VERIFIED,
      (b) its VERIFIED_REPAYMENT ledger entry,
      (c) if that brings the ledger balance to zero, the loan's closure
          (CLOSED / paid_in_full + its LoanClosure record).
    The transaction and its loan are row-locked first (Postgres), so two
    admins can't verify the same payment - or two payments on one loan - at
    the same moment; the unique index on the ledger's payment_transaction_id
    backs that up. A payment larger than the outstanding balance is refused.
    Nothing commits until every step has succeeded; any error rolls back all
    of it. Emails go out only after the commit.
    """
    from . import closures, ledger, penalties, repayments_scheduler  # local: avoid circular imports

    if actor.role != UserRole.ADMIN:
        raise ServiceError("Only an admin may verify or reject a payment.", 403)
    if decision not in ("verified", "rejected"):
        raise ServiceError("decision must be 'verified' or 'rejected'.")
    if decision == "rejected" and not (note and note.strip()):
        raise ServiceError("note (a reason) is required when rejecting a payment.")

    try:
        txn = (
            PaymentTransaction.query.filter_by(id=transaction_id).with_for_update().one_or_none()
        )
        if txn is None:
            raise ServiceError("Payment transaction not found.", 404)
        if txn.status not in _VERIFIABLE_STATUSES:
            raise ServiceError(
                f"Transaction #{txn.id} is already {txn.status.value}; only reported/"
                "verification_pending transactions can be verified or rejected.",
                409,
            )
        loan = Loan.query.filter_by(id=txn.loan_id).with_for_update().one()
        schedule: RepaymentSchedule | None = txn.repayment_schedule

        if decision == "rejected":
            txn.status = PaymentStatus.REJECTED
            txn.rejection_reason = note.strip()[:500]
            audit.record(
                "payment_rejected",
                actor_id=actor.id,
                entity_type="PaymentTransaction",
                entity_id=txn.id,
                details={"loan_id": loan.id, "reason": txn.rejection_reason},
                commit=False,
            )
            db.session.commit()
            return _serialize_transaction(txn)

        # ---- verified ----------------------------------------------------
        if loan.status not in (LoanStatus.ACTIVE, LoanStatus.OVERDUE):
            raise ServiceError(f"Loan #{loan.id} is {loan.status.value}; nothing is owed on it.", 409)
        pay_amount = Decimal(txn.amount).quantize(_CENTS)
        penalties.assess_before_verification(loan)  # tiers already due go on first
        outstanding = ledger.balance(loan.id)
        if pay_amount > outstanding:
            raise ServiceError(
                f"This payment (K{pay_amount:,.2f}) is more than the K{outstanding:,.2f} outstanding "
                "on the loan. Reject it with a reason so the customer can report the correct amount.",
                409,
            )

        now = datetime.now(timezone.utc)
        txn.status = PaymentStatus.VERIFIED  # (a)
        txn.paid_at = now
        db.session.add(LoanLedgerEntry(  # (b)
            loan_id=loan.id,
            entry_type=LedgerEntryType.VERIFIED_REPAYMENT,
            amount=-pay_amount,
            effective_date=txn.payment_date,
            created_by=actor.id,
            created_by_kind=LedgerActorKind.ADMIN,
            payment_transaction_id=txn.id,
            note=(note or "").strip()[:500] or None,
        ))
        # Kept in step for the screens that still read the schedule row; the
        # ledger is the record (amount_paid is retired in a later migration).
        if schedule is not None:
            schedule.amount_paid = (Decimal(schedule.amount_paid) + pay_amount).quantize(_CENTS)
            if schedule.amount_paid >= Decimal(schedule.amount_due):
                schedule.status = RepaymentStatus.PAID
        db.session.flush()  # the ledger's unique index fires here on a double post

        remaining = ledger.balance(loan.id)
        audit.record(
            "payment_verified",
            actor_id=actor.id,
            entity_type="PaymentTransaction",
            entity_id=txn.id,
            details={
                "loan_id": loan.id,
                "repayment_schedule_id": schedule.id if schedule else None,
                "amount": float(pay_amount),
                "note": note,
                "outstanding_before": float(outstanding),
                "outstanding_after": float(remaining),
            },
            commit=False,
        )
        loan_completed = remaining == 0
        if loan_completed:  # (c)
            closures.record_closure(
                loan, LoanClosureReason.PAID_IN_FULL, actor_id=actor.id, closing_payment=txn
            )
        else:
            repayments_scheduler.sync_loan_overdue_status(loan)
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        raise ServiceError(f"Transaction #{transaction_id} has already been verified.", 409)
    except Exception:
        db.session.rollback()
        raise

    if schedule is not None:
        notifications.notify_payment_received(txn, schedule)

    result = _serialize_transaction(txn)
    result["outstanding"] = float(remaining)
    result["loan_status"] = str(loan.status)
    result["loan_completed"] = loan_completed
    if schedule is not None:
        due, paid = Decimal(schedule.amount_due), Decimal(schedule.amount_paid)
        result["installment"] = {
            "installment_number": schedule.installment_number,
            "amount_due": float(due),
            "amount_paid": float(paid),
            "shortfall": float(max(Decimal("0"), due - paid).quantize(_CENTS)),
            "overpaid": 0.0,
            "status": str(schedule.status),
        }
    return result


def _serialize_receipts(t: PaymentTransaction) -> list[dict]:
    return [documents.serialize(d) for d in t.documents if d.superseded_by_id is None]


def _serialize_transaction(t: PaymentTransaction) -> dict:
    return {
        "transaction": {
            "id": t.id,
            "loan_id": t.loan_id,
            "repayment_schedule_id": t.repayment_schedule_id,
            "amount": float(Decimal(t.amount)),
            "payment_method": t.payment_method,
            "payment_date": t.payment_date.isoformat() if t.payment_date else None,
            "reference_number": t.reference_number,
            "status": str(t.status),
            "rejection_reason": t.rejection_reason,
            "reported_at": t.reported_at.isoformat() if t.reported_at else None,
            "paid_at": t.paid_at.isoformat() if t.paid_at else None,
            "receipts": _serialize_receipts(t),
        }
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
            "payment_date": t.payment_date.isoformat() if t.payment_date else None,
            "reference_number": t.reference_number,
            "status": str(t.status),
            "rejection_reason": t.rejection_reason,
            "reported_at": t.reported_at.isoformat() if t.reported_at else None,
            "paid_at": t.paid_at.isoformat() if t.paid_at else None,
            "receipts": _serialize_receipts(t),
        }
        for t in rows
    ]
