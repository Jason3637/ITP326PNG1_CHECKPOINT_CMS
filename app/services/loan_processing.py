"""Loan Processing - PRIME application intake, two-tier officer/admin review,
disbursement, and loan closure.

State machine (see app/models/enums.py:LoanApplicationStatus for the full
docstring on the old->new status mapping):

    SUBMITTED --officer--> OFFICER_REVIEW
    OFFICER_REVIEW --officer--> CUSTOMER_ACTION_REQUIRED
    CUSTOMER_ACTION_REQUIRED --officer--> OFFICER_REVIEW
    OFFICER_REVIEW --officer--> RECOMMENDED_FOR_APPROVAL
    RECOMMENDED_FOR_APPROVAL --admin--> ADMIN_REVIEW
    ADMIN_REVIEW --admin--> APPROVED --(same call)--> AWAITING_DISBURSEMENT
    ADMIN_REVIEW --admin--> REJECTED
    {OFFICER_REVIEW, CUSTOMER_ACTION_REQUIRED, RECOMMENDED_FOR_APPROVAL,
     ADMIN_REVIEW} --officer or admin, early exit--> REJECTED
    AWAITING_DISBURSEMENT --admin, disburse_application()--> (Loan created, ACTIVE)

Role checks are enforced at the API layer (roles_required); each function
below documents which role it expects, same convention as the rest of this
service layer.
"""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from app.extensions import db
from app.models import Disbursement, Loan, LoanApplication, Referee, TermsAcceptance
from app.models.enums import (
    DisbursementMethod,
    EmploymentStatus,
    LoanApplicationStatus,
    LoanClosureReason,
    LoanPurposeCategory,
    LoanStatus,
)

from . import audit, credit_evaluation, documents, notifications, prime_pricing
from .errors import ServiceError

_OPEN_APPLICATION_STATUSES = (
    LoanApplicationStatus.SUBMITTED,
    LoanApplicationStatus.OFFICER_REVIEW,
    LoanApplicationStatus.CUSTOMER_ACTION_REQUIRED,
    LoanApplicationStatus.RECOMMENDED_FOR_APPROVAL,
    LoanApplicationStatus.ADMIN_REVIEW,
)
# Any of these may be early-exit rejected without walking the whole chain.
_REJECTABLE_STATUSES = _OPEN_APPLICATION_STATUSES

# Customer-facing labels - internal staff-routing statuses (OFFICER_REVIEW,
# RECOMMENDED_FOR_APPROVAL, ADMIN_REVIEW) collapse to one friendly label so a
# customer never sees raw workflow-stage enum text. Staff-facing UIs should
# use the raw `status` field instead, for precise workflow tracking.
_STATUS_LABELS: dict[LoanApplicationStatus, str] = {
    LoanApplicationStatus.DRAFT: "Draft",
    LoanApplicationStatus.SUBMITTED: "Submitted",
    LoanApplicationStatus.OFFICER_REVIEW: "Under Review",
    LoanApplicationStatus.CUSTOMER_ACTION_REQUIRED: "Action Required",
    LoanApplicationStatus.RECOMMENDED_FOR_APPROVAL: "Under Review",
    LoanApplicationStatus.ADMIN_REVIEW: "Under Review",
    LoanApplicationStatus.APPROVED: "Approved",
    LoanApplicationStatus.REJECTED: "Not Approved",
    LoanApplicationStatus.AWAITING_DISBURSEMENT: "Approved - Processing Disbursement",
}


def status_label(status: LoanApplicationStatus) -> str:
    """Customer-facing text for an application status. See _STATUS_LABELS."""
    return _STATUS_LABELS[LoanApplicationStatus(status)]


class LoanProcessingError(ServiceError):
    """Raised for loan business-rule violations."""


def _require_status(application: LoanApplication, expected: LoanApplicationStatus) -> None:
    if application.status != expected:
        raise LoanProcessingError(
            f"Application #{application.id} is {application.status.value}, "
            f"expected {expected.value}.",
            status_code=409,
        )


# --------------------------------------------------------------- 1. application
def _parse_referees(referees) -> list[dict]:
    if not referees or not isinstance(referees, list):
        raise LoanProcessingError("At least one referee is required.")
    parsed = []
    for i, r in enumerate(referees):
        if not isinstance(r, dict):
            raise LoanProcessingError(f"referees[{i}] must be an object.")
        full_name = (r.get("full_name") or "").strip()
        relationship = (r.get("relationship") or "").strip()
        mobile_number = (r.get("mobile_number") or "").strip()
        if not full_name or not relationship or not mobile_number:
            raise LoanProcessingError(
                f"referees[{i}] requires full_name, relationship, and mobile_number."
            )
        parsed.append(
            {
                "full_name": full_name,
                "relationship": relationship,
                "mobile_number": mobile_number,
                "employer_name": (r.get("employer_name") or "").strip() or None,
            }
        )
    return parsed


def _parse_disbursement_selection(method_requested, account_reference) -> tuple:
    try:
        method = DisbursementMethod(method_requested)
    except ValueError:
        allowed = ", ".join(m.value for m in DisbursementMethod)
        raise LoanProcessingError(f"disbursement_method_requested must be one of: {allowed}.")
    reference = (account_reference or "").strip() or None
    if method == DisbursementMethod.BSP_MOBILE_BANKING and not reference:
        raise LoanProcessingError(
            "disbursement_account_reference (BSP mobile banking number) is "
            "required when disbursement_method_requested is bsp_mobile_banking."
        )
    return method, reference


def _parse_purpose(purpose_category, purpose_description) -> tuple:
    try:
        category = LoanPurposeCategory(purpose_category)
    except ValueError:
        allowed = ", ".join(c.value for c in LoanPurposeCategory)
        raise LoanProcessingError(f"purpose_category must be one of: {allowed}.")
    description = (purpose_description or "").strip() or None
    if category == LoanPurposeCategory.OTHER and not description:
        raise LoanProcessingError(
            "purpose (a short description) is required when purpose_category is 'other'."
        )
    return category, description


def _parse_confirmed_personal_details(full_name, email, phone_number) -> tuple:
    full_name = (full_name or "").strip()
    email = (email or "").strip()
    phone_number = (phone_number or "").strip() or None
    if not full_name:
        raise LoanProcessingError("confirmed_full_name is required.")
    if not email:
        raise LoanProcessingError("confirmed_email is required.")
    return full_name, email, phone_number


def _validate_terms_acceptance(accept_terms, policy_version) -> str:
    from flask import current_app

    current_version = current_app.config["CURRENT_POLICY_VERSION"]
    if not accept_terms:
        raise LoanProcessingError("You must accept the current terms and conditions to apply.")
    if policy_version != current_version:
        raise LoanProcessingError(
            f"policy_version '{policy_version}' is out of date "
            f"(current is '{current_version}'). Reload the terms and try again."
        )
    return current_version


# ---- credit-evaluation inputs (advisory only - see credit_evaluation.py) ----
def _parse_income(monthly_income):
    if monthly_income is None:
        return None
    try:
        value = Decimal(str(monthly_income))
    except (InvalidOperation, TypeError):
        raise LoanProcessingError("monthly_income must be a number.")
    if value < 0:
        raise LoanProcessingError("monthly_income must not be negative.")
    return value


def _parse_debt(existing_monthly_debt):
    if existing_monthly_debt is None:
        return None
    try:
        value = Decimal(str(existing_monthly_debt))
    except (InvalidOperation, TypeError):
        raise LoanProcessingError("existing_monthly_debt must be a number.")
    if value < 0:
        raise LoanProcessingError("existing_monthly_debt must not be negative.")
    return value


def _parse_employment(employment_status):
    if employment_status is None:
        return None
    try:
        return EmploymentStatus(employment_status)
    except ValueError:
        allowed = ", ".join(e.value for e in EmploymentStatus)
        raise LoanProcessingError(f"employment_status must be one of: {allowed}.")


def submit_application(
    user,
    *,
    amount_requested,
    purpose_category,
    purpose: str | None = None,
    confirmed_full_name,
    confirmed_email,
    confirmed_phone_number: str | None = None,
    monthly_income=None,
    employment_status: str | None = None,
    existing_monthly_debt=None,
    referees=None,
    disbursement_method_requested=None,
    disbursement_account_reference=None,
    accept_terms: bool = False,
    policy_version: str | None = None,
    document_ids: list[int] | None = None,
) -> LoanApplication:
    # ---- 1. PRIME pricing gate (reject early, before anything else) ----
    pricing = prime_pricing.calculate_prime(amount_requested)
    amount = pricing["amount"]

    category, description = _parse_purpose(purpose_category, purpose)
    full_name, email, phone_number = _parse_confirmed_personal_details(
        confirmed_full_name, confirmed_email, confirmed_phone_number
    )
    referee_rows = _parse_referees(referees)
    method, account_reference = _parse_disbursement_selection(
        disbursement_method_requested, disbursement_account_reference
    )
    accepted_version = _validate_terms_acceptance(accept_terms, policy_version)

    income_val = _parse_income(monthly_income)
    debt_val = _parse_debt(existing_monthly_debt)
    employment_val = _parse_employment(employment_status)

    # ---- item 8: Proof of Income required at/above the named threshold ----
    if documents.proof_of_income_required(amount) and not documents.has_proof_of_income(
        document_ids, user
    ):
        raise LoanProcessingError(
            "Proof of Income is required for amounts of "
            f"K{documents.PROOF_OF_INCOME_REQUIRED_ABOVE:,.0f} or more "
            "(rule: PROOF_OF_INCOME_REQUIRED_ABOVE). Upload it via "
            "POST /users/documents first, then pass its id in document_ids."
        )

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
        purpose_category=category,
        purpose=description,
        confirmed_full_name=full_name,
        confirmed_email=email,
        confirmed_phone_number=phone_number,
        prime_category=pricing["category"],
        disbursement_method_requested=method,
        disbursement_account_reference=account_reference,
        status=LoanApplicationStatus.SUBMITTED,
        monthly_income=income_val,
        employment_status=employment_val,
        existing_monthly_debt=debt_val,
    )
    db.session.add(application)
    db.session.flush()

    for r in referee_rows:
        db.session.add(
            Referee(
                loan_application_id=application.id,
                full_name=r["full_name"],
                relationship_to_applicant=r["relationship"],
                mobile_number=r["mobile_number"],
                employer_name=r["employer_name"],
            )
        )
    db.session.add(
        TermsAcceptance(
            loan_application_id=application.id,
            user_id=user.id,
            policy_version=accepted_version,
        )
    )
    if document_ids:
        documents.link_documents_to_application(document_ids, user, application.id)

    # Credit Evaluation is advisory only - it computes and stores a result
    # for a human to read but never sets application.status (see item 5 /
    # credit_evaluation.py's module docstring).
    evaluation = credit_evaluation.evaluate(user, amount, prime_pricing.PRIME_TERM_DAYS)
    application.credit_evaluation_result = evaluation

    audit.record(
        "loan_application_submitted",
        actor_id=user.id,
        entity_type="LoanApplication",
        entity_id=application.id,
        details={
            "amount_requested": float(amount),
            "purpose_category": category.value,
            "prime_category": pricing["category"],
            "interest_amount": float(pricing["interest_amount"]),
            "total_repayable": float(pricing["total_repayable"]),
            "term_days": pricing["term_days"],
            "credit_score": evaluation["score"],
            "credit_eligible": evaluation["eligible"],
            "credit_insufficient_data": evaluation.get("insufficient_data"),
            "resulting_status": application.status.value,
        },
        commit=False,
    )
    db.session.commit()

    notifications.notify_application_received(application)
    return application


# ------------------------------------------------------------------ 2. review
def list_applications(*, status: str | None = None, open_only: bool = True):
    """loan_officer or admin - the review queue (every applicant's rows)."""
    query = LoanApplication.query
    if status:
        try:
            query = query.filter_by(status=LoanApplicationStatus(status))
        except ValueError:
            raise LoanProcessingError(f"Unknown status '{status}'.")
    elif open_only:
        query = query.filter(LoanApplication.status.in_(_OPEN_APPLICATION_STATUSES))
    return query.order_by(LoanApplication.submitted_at.asc()).all()


def list_my_applications(user):
    """customer - their own applications (open and terminal), newest first."""
    return (
        LoanApplication.query.filter_by(user_id=user.id)
        .order_by(LoanApplication.submitted_at.desc())
        .all()
    )


def _transition(
    application: LoanApplication,
    actor,
    *,
    expected: LoanApplicationStatus,
    new_status: LoanApplicationStatus,
    action: str,
    note: str | None = None,
) -> LoanApplication:
    _require_status(application, expected)
    application.status = new_status
    audit.record(
        action,
        actor_id=actor.id,
        entity_type="LoanApplication",
        entity_id=application.id,
        details={"note": note, "from": expected.value, "to": new_status.value},
        commit=False,
    )
    db.session.commit()
    return application


def start_officer_review(application: LoanApplication, officer) -> LoanApplication:
    """loan_officer or admin."""
    return _transition(
        application,
        officer,
        expected=LoanApplicationStatus.SUBMITTED,
        new_status=LoanApplicationStatus.OFFICER_REVIEW,
        action="loan_application_officer_review_started",
    )


def request_customer_action(application: LoanApplication, officer, note: str) -> LoanApplication:
    """loan_officer or admin. `note` is required - it's stored on the
    application (action_required_note) and shown to the customer, since
    AuditLog isn't customer-readable.
    """
    if not note or not note.strip():
        raise LoanProcessingError("note is required when requesting customer action.")
    application.action_required_note = note.strip()
    return _transition(
        application,
        officer,
        expected=LoanApplicationStatus.OFFICER_REVIEW,
        new_status=LoanApplicationStatus.CUSTOMER_ACTION_REQUIRED,
        action="loan_application_customer_action_requested",
        note=note,
    )


def respond_to_customer_action(
    application: LoanApplication,
    customer,
    *,
    response_note: str,
    purpose_category=None,
    purpose: str | None = None,
    confirmed_full_name: str | None = None,
    confirmed_email: str | None = None,
    confirmed_phone_number: str | None = None,
    monthly_income=None,
    employment_status: str | None = None,
    existing_monthly_debt=None,
    referees=None,
    disbursement_method_requested=None,
    disbursement_account_reference=None,
    document_ids: list[int] | None = None,
) -> LoanApplication:
    """customer only, and only the application's own owner. Updates the
    EXISTING application (never creates a new one) and returns it to
    OFFICER_REVIEW. Every field is optional except response_note - only
    what's actually provided gets changed; the rest of the application is
    left as-is.
    """
    if application.user_id != customer.id:
        raise LoanProcessingError("You can only respond to your own application.", 403)
    _require_status(application, LoanApplicationStatus.CUSTOMER_ACTION_REQUIRED)
    if not response_note or not response_note.strip():
        raise LoanProcessingError("response_note is required.")

    changed: list[str] = []

    if purpose_category is not None:
        category, description = _parse_purpose(purpose_category, purpose)
        application.purpose_category = category
        application.purpose = description
        changed.append("purpose")

    if confirmed_full_name is not None or confirmed_email is not None:
        full_name, email, phone_number = _parse_confirmed_personal_details(
            confirmed_full_name or application.confirmed_full_name,
            confirmed_email or application.confirmed_email,
            confirmed_phone_number
            if confirmed_phone_number is not None
            else application.confirmed_phone_number,
        )
        application.confirmed_full_name = full_name
        application.confirmed_email = email
        application.confirmed_phone_number = phone_number
        changed.append("personal_details")

    if monthly_income is not None:
        application.monthly_income = _parse_income(monthly_income)
        changed.append("monthly_income")
    if employment_status is not None:
        application.employment_status = _parse_employment(employment_status)
        changed.append("employment_status")
    if existing_monthly_debt is not None:
        application.existing_monthly_debt = _parse_debt(existing_monthly_debt)
        changed.append("existing_monthly_debt")

    if referees is not None:
        referee_rows = _parse_referees(referees)
        for old in list(application.referees):
            db.session.delete(old)
        db.session.flush()
        for r in referee_rows:
            db.session.add(
                Referee(
                    loan_application_id=application.id,
                    full_name=r["full_name"],
                    relationship_to_applicant=r["relationship"],
                    mobile_number=r["mobile_number"],
                    employer_name=r["employer_name"],
                )
            )
        changed.append("referees")

    if disbursement_method_requested is not None:
        method, account_reference = _parse_disbursement_selection(
            disbursement_method_requested, disbursement_account_reference
        )
        application.disbursement_method_requested = method
        application.disbursement_account_reference = account_reference
        changed.append("disbursement_method")

    if document_ids:
        documents.link_documents_to_application(document_ids, customer, application.id)
        changed.append("documents")

    # Refresh the advisory credit-evaluation result so the officer sees
    # current numbers when they resume review - still never touches status.
    # Passed explicitly: this row is CUSTOMER_ACTION_REQUIRED, not SUBMITTED,
    # so evaluate()'s default lookup wouldn't find it (see its docstring).
    evaluation = credit_evaluation.evaluate(
        customer,
        application.amount_requested,
        prime_pricing.PRIME_TERM_DAYS,
        application=application,
    )
    application.credit_evaluation_result = evaluation

    application.status = LoanApplicationStatus.OFFICER_REVIEW
    application.action_required_note = None
    audit.record(
        "loan_application_customer_responded",
        actor_id=customer.id,
        entity_type="LoanApplication",
        entity_id=application.id,
        details={"response_note": response_note, "changed_fields": changed},
        commit=False,
    )
    db.session.commit()
    return application


def resume_officer_review(application: LoanApplication, officer) -> LoanApplication:
    """loan_officer or admin - called once the requested info/docs are in."""
    return _transition(
        application,
        officer,
        expected=LoanApplicationStatus.CUSTOMER_ACTION_REQUIRED,
        new_status=LoanApplicationStatus.OFFICER_REVIEW,
        action="loan_application_officer_review_resumed",
    )


def recommend_for_approval(
    application: LoanApplication, officer, note: str | None = None
) -> LoanApplication:
    """loan_officer or admin."""
    return _transition(
        application,
        officer,
        expected=LoanApplicationStatus.OFFICER_REVIEW,
        new_status=LoanApplicationStatus.RECOMMENDED_FOR_APPROVAL,
        action="loan_application_recommended_for_approval",
        note=note,
    )


def start_admin_review(application: LoanApplication, admin) -> LoanApplication:
    """admin only."""
    return _transition(
        application,
        admin,
        expected=LoanApplicationStatus.RECOMMENDED_FOR_APPROVAL,
        new_status=LoanApplicationStatus.ADMIN_REVIEW,
        action="loan_application_admin_review_started",
    )


def reject_application(application: LoanApplication, actor, note: str | None = None) -> LoanApplication:
    """loan_officer or admin. Early-exit reject from any open status - a
    doomed application doesn't need to walk the whole chain first.
    """
    if application.status not in _REJECTABLE_STATUSES:
        raise LoanProcessingError(
            f"Application #{application.id} is already {application.status.value}.",
            status_code=409,
        )
    now = datetime.now(timezone.utc)
    application.status = LoanApplicationStatus.REJECTED
    application.decided_at = now
    application.decided_by = actor.id
    audit.record(
        "loan_application_decision",
        actor_id=actor.id,
        entity_type="LoanApplication",
        entity_id=application.id,
        details={"decision": "reject", "note": note},
        commit=False,
    )
    db.session.commit()
    notifications.notify_loan_rejected(application)
    return application


# ---------------------------------------------------------------- 3. decision
def decide_application(
    application: LoanApplication, admin, *, approve: bool, note: str | None = None
) -> LoanApplication:
    """admin only - the final approve/reject call, from ADMIN_REVIEW.

    On approve, sets APPROVED then immediately AWAITING_DISBURSEMENT in the
    same transaction: the application's job is done from here, no Loan is
    created yet (see disburse_application - that's the separate, genuinely
    distinct disbursement event, item 6).
    """
    _require_status(application, LoanApplicationStatus.ADMIN_REVIEW)

    now = datetime.now(timezone.utc)
    application.decided_at = now
    application.decided_by = admin.id

    if not approve:
        application.status = LoanApplicationStatus.REJECTED
        audit.record(
            "loan_application_decision",
            actor_id=admin.id,
            entity_type="LoanApplication",
            entity_id=application.id,
            details={"decision": "reject", "note": note},
            commit=False,
        )
        db.session.commit()
        notifications.notify_loan_rejected(application)
        return application

    application.status = LoanApplicationStatus.APPROVED
    audit.record(
        "loan_application_decision",
        actor_id=admin.id,
        entity_type="LoanApplication",
        entity_id=application.id,
        details={"decision": "approve", "note": note},
        commit=False,
    )
    application.status = LoanApplicationStatus.AWAITING_DISBURSEMENT
    audit.record(
        "loan_application_awaiting_disbursement",
        actor_id=admin.id,
        entity_type="LoanApplication",
        entity_id=application.id,
        details={},
        commit=False,
    )
    db.session.commit()
    return application


# ------------------------------------------------------------ 4. disbursement
def disburse_application(
    application: LoanApplication,
    admin,
    *,
    method: str,
    method_reference: str | None = None,
    note: str | None = None,
) -> tuple[LoanApplication, Loan]:
    """admin only. Creates the Loan, its single bullet repayment installment,
    and the Disbursement record - the concrete event that makes APPROVED /
    AWAITING_DISBURSEMENT / ACTIVE genuinely separable in the data.
    """
    from . import repayments_scheduler  # local import: avoid a circular import

    _require_status(application, LoanApplicationStatus.AWAITING_DISBURSEMENT)

    try:
        disb_method = DisbursementMethod(method)
    except ValueError:
        allowed = ", ".join(m.value for m in DisbursementMethod)
        raise LoanProcessingError(f"method must be one of: {allowed}.")

    pricing = prime_pricing.calculate_prime(application.amount_requested)
    now = datetime.now(timezone.utc)

    loan = Loan(
        application_id=application.id,
        user_id=application.user_id,
        principal_amount=pricing["amount"],
        interest_rate=pricing["rate"],
        term_days=pricing["term_days"],
        monthly_payment=pricing["total_repayable"],
        total_repayable=pricing["total_repayable"],
        status=LoanStatus.ACTIVE,
        disbursed_at=now,
    )
    db.session.add(loan)
    db.session.flush()

    schedule = repayments_scheduler.generate_bullet_schedule(loan)

    disbursement = Disbursement(
        loan_id=loan.id,
        method=disb_method,
        amount=pricing["amount"],
        method_reference=(method_reference or "").strip() or None,
        disbursed_at=now,
        recorded_by=admin.id,
        note=note,
    )
    db.session.add(disbursement)

    audit.record(
        "loan_disbursed",
        actor_id=admin.id,
        entity_type="Loan",
        entity_id=loan.id,
        details={
            "application_id": application.id,
            "principal": float(loan.principal_amount),
            "total_repayable": float(loan.total_repayable),
            "method": disb_method.value,
            "method_reference": disbursement.method_reference,
            "due_date": schedule[0].due_date.isoformat(),
        },
        commit=False,
    )
    db.session.commit()

    notifications.notify_loan_approved(loan)
    return application, loan


# ---------------------------------------------------------------- 5. closure
def close_loan(loan: Loan, actor) -> Loan:
    """loan_officer or admin. Explicit archival step: PAID -> CLOSED.
    Nothing auto-advances a loan from PAID to CLOSED - see the migration plan.
    """
    if loan.status != LoanStatus.PAID:
        raise LoanProcessingError(
            f"Loan #{loan.id} is {loan.status.value}, expected paid.", status_code=409
        )
    loan.status = LoanStatus.CLOSED
    loan.closure_reason = LoanClosureReason.PAID_IN_FULL
    audit.record(
        "loan_closed",
        actor_id=actor.id,
        entity_type="Loan",
        entity_id=loan.id,
        details={"closure_reason": loan.closure_reason.value},
        commit=False,
    )
    db.session.commit()
    return loan


def write_off_loan(loan: Loan, admin, note: str | None = None) -> Loan:
    """admin only. Marks a loan CLOSED/DEFAULTED directly from ACTIVE/OVERDUE
    - without this, LoanClosureReason.DEFAULTED (and the credit-scoring
    signal it feeds) would be unreachable dead code.
    """
    if loan.status not in (LoanStatus.ACTIVE, LoanStatus.OVERDUE):
        raise LoanProcessingError(
            f"Loan #{loan.id} is {loan.status.value}; only active/overdue loans "
            "can be written off.",
            status_code=409,
        )
    loan.status = LoanStatus.CLOSED
    loan.closure_reason = LoanClosureReason.DEFAULTED
    audit.record(
        "loan_written_off",
        actor_id=admin.id,
        entity_type="Loan",
        entity_id=loan.id,
        details={"closure_reason": loan.closure_reason.value, "note": note},
        commit=False,
    )
    db.session.commit()
    return loan
