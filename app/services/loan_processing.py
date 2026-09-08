"""Loan Processing - application intake, officer review, approval -> disbursement.

Ties together Credit Evaluation, Interest Calculation, and the Repayments
Scheduler, and writes an AuditLog row for every state change.
"""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from app.extensions import db
from app.models import Loan, LoanApplication
from app.models.enums import (
    LoanApplicationStatus,
    LoanStatus,
    RepaymentFrequency,
)

from . import (
    audit,
    credit_evaluation,
    interest_calculation,
    notifications,
    parameters,
    repayments_scheduler,
)
from .errors import ServiceError

_OPEN_APPLICATION_STATUSES = (
    LoanApplicationStatus.PENDING,
    LoanApplicationStatus.UNDER_REVIEW,
)


class LoanProcessingError(ServiceError):
    """Raised for loan business-rule violations."""


# --------------------------------------------------------------- 1. application
def submit_application(
    user,
    *,
    amount_requested,
    purpose: str | None,
    term_months: int,
    repayment_frequency: str,
) -> LoanApplication:
    min_amount = parameters.get_value("min_loan_amount")
    max_amount = parameters.get_value("max_loan_amount")
    min_term = parameters.get_value("min_loan_term_months")
    max_term = parameters.get_value("max_loan_term_months")

    try:
        amount = Decimal(str(amount_requested))
    except (InvalidOperation, TypeError):
        raise LoanProcessingError("amount_requested must be a number.")
    if amount <= 0:
        raise LoanProcessingError("amount_requested must be positive.")
    if not (min_amount <= amount <= max_amount):
        raise LoanProcessingError(
            f"amount_requested must be between {min_amount} and {max_amount}."
        )

    if not isinstance(term_months, int) or term_months <= 0:
        raise LoanProcessingError("term_months must be a positive integer.")
    if not (min_term <= term_months <= max_term):
        raise LoanProcessingError(
            f"term_months must be between {min_term} and {max_term}."
        )

    try:
        frequency = RepaymentFrequency(repayment_frequency)
    except ValueError:
        allowed = ", ".join(f.value for f in RepaymentFrequency)
        raise LoanProcessingError(f"repayment_frequency must be one of: {allowed}.")

    existing = (
        LoanApplication.query.filter_by(user_id=user.id)
        .filter(LoanApplication.status.in_(_OPEN_APPLICATION_STATUSES))
        .first()
    )
    if existing is not None:
        raise LoanProcessingError(
            f"You already have an open application (#{existing.id}).", status_code=409
        )

    application = LoanApplication(
        user_id=user.id,
        amount_requested=amount,
        purpose=(purpose or None),
        term_months=term_months,
        repayment_frequency=frequency,
        status=LoanApplicationStatus.PENDING,
    )
    db.session.add(application)
    db.session.flush()

    # Credit Evaluation (placeholder) runs immediately and is stored on the row.
    evaluation = credit_evaluation.evaluate(user, amount, term_months)
    application.credit_evaluation_result = evaluation
    application.status = credit_evaluation.suggested_status(evaluation)

    audit.record(
        "loan_application_submitted",
        actor_id=user.id,
        entity_type="LoanApplication",
        entity_id=application.id,
        details={
            "amount_requested": float(amount),
            "term_months": term_months,
            "repayment_frequency": frequency.value,
            "credit_score": evaluation["score"],
            "credit_eligible": evaluation["eligible"],
            "resulting_status": application.status.value,
        },
        commit=False,
    )
    db.session.commit()

    notifications.notify_application_received(application)
    return application


# ------------------------------------------------------------------ 2. review
def list_applications(*, status: str | None = None, open_only: bool = True):
    query = LoanApplication.query
    if status:
        try:
            query = query.filter_by(status=LoanApplicationStatus(status))
        except ValueError:
            raise LoanProcessingError(f"Unknown status '{status}'.")
    elif open_only:
        query = query.filter(LoanApplication.status.in_(_OPEN_APPLICATION_STATUSES))
    return query.order_by(LoanApplication.submitted_at.asc()).all()


# ---------------------------------------------------------------- 3. decision
def decide_application(
    application: LoanApplication,
    officer,
    *,
    approve: bool,
    interest_rate=None,
    note: str | None = None,
) -> tuple[LoanApplication, Loan | None]:
    if application.status not in _OPEN_APPLICATION_STATUSES:
        raise LoanProcessingError(
            f"Application #{application.id} is already {application.status.value}.",
            status_code=409,
        )

    now = datetime.now(timezone.utc)
    application.decided_at = now
    application.decided_by = officer.id

    loan: Loan | None = None
    audit_details = {
        "decision": "approve" if approve else "reject",
        "note": note,
    }

    if not approve:
        application.status = LoanApplicationStatus.REJECTED
        audit.record(
            "loan_application_decision",
            actor_id=officer.id,
            entity_type="LoanApplication",
            entity_id=application.id,
            details=audit_details,
            commit=False,
        )
        db.session.commit()
        notifications.notify_loan_rejected(application)
        return application, None

    # ---- approval: create the Loan, then price it and schedule repayments ----
    application.status = LoanApplicationStatus.APPROVED

    rate = _resolve_rate(interest_rate)
    frequency = application.repayment_frequency
    terms = interest_calculation.amortize(
        application.amount_requested, rate, application.term_months, frequency
    )

    loan = Loan(
        application_id=application.id,
        user_id=application.user_id,
        principal_amount=Decimal(str(application.amount_requested)),
        interest_rate=rate,
        term_months=application.term_months,
        monthly_payment=terms["installment_amount"],
        total_repayable=terms["total_repayable"],
        status=LoanStatus.ACTIVE,
        disbursed_at=now,  # small SME: approval == disbursement
    )
    db.session.add(loan)
    db.session.flush()

    schedule = repayments_scheduler.generate_schedule(loan, frequency)

    audit_details.update(
        {
            "loan_id": loan.id,
            "interest_rate": float(rate),
            "installment_amount": float(terms["installment_amount"]),
            "installment_count": terms["installment_count"],
            "total_repayable": float(terms["total_repayable"]),
        }
    )
    audit.record(
        "loan_application_decision",
        actor_id=officer.id,
        entity_type="LoanApplication",
        entity_id=application.id,
        details=audit_details,
        commit=False,
    )
    audit.record(
        "loan_disbursed",
        actor_id=officer.id,
        entity_type="Loan",
        entity_id=loan.id,
        details={
            "principal": float(loan.principal_amount),
            "total_repayable": float(loan.total_repayable),
            "installments": len(schedule),
            "first_due": schedule[0].due_date.isoformat(),
            "final_due": schedule[-1].due_date.isoformat(),
        },
        commit=False,
    )
    db.session.commit()

    notifications.notify_loan_approved(loan)
    return application, loan


def _resolve_rate(interest_rate) -> Decimal:
    if interest_rate is None:
        return parameters.get_value("default_annual_interest_rate")
    try:
        rate = Decimal(str(interest_rate))
    except (InvalidOperation, TypeError):
        raise LoanProcessingError("interest_rate must be a number (e.g. 0.18 for 18%).")
    if not (Decimal("0") <= rate < Decimal("1")):
        raise LoanProcessingError(
            "interest_rate must be a fraction between 0 and 1 (e.g. 0.18 = 18%)."
        )
    return rate
