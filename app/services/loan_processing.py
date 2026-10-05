"""Loan Processing - PRIME application intake, two-tier officer/admin review,
disbursement, and loan closure.

State machine (see app/models/enums.py:LoanApplicationStatus for the full
docstring on the old->new status mapping):

    SUBMITTED --officer claims--> OFFICER_REVIEW   (sets assigned_officer_id)
    OFFICER_REVIEW --assigned officer--> CUSTOMER_ACTION_REQUIRED   (InformationRequest rows)
    CUSTOMER_ACTION_REQUIRED --customer responds--> OFFICER_REVIEW  (InformationResponse rows)
    CUSTOMER_ACTION_REQUIRED --assigned officer resumes--> OFFICER_REVIEW (open requests cancelled)
    OFFICER_REVIEW --assigned officer--> RECOMMENDED_FOR_APPROVAL | RECOMMENDED_FOR_REJECTION
                                         (OfficerRecommendation row; never creates a Loan)
    RECOMMENDED_FOR_* --admin--> ADMIN_REVIEW
    RECOMMENDED_FOR_* | ADMIN_REVIEW --admin--> RETURNED_TO_OFFICER   (AdminReturn row)
    RETURNED_TO_OFFICER --assigned officer resumes--> OFFICER_REVIEW
    ADMIN_REVIEW --admin--> APPROVED --(same call)--> AWAITING_DISBURSEMENT
    ADMIN_REVIEW --admin--> REJECTED
    any open status --admin, early exit--> REJECTED
    AWAITING_DISBURSEMENT --admin, disburse_application()--> DISBURSED (Loan created, ACTIVE)

A loan officer can never reject, decide, disburse, close or write off: those
are admin-only, enforced at the API layer (roles_required) AND re-checked
here (_require_admin) so a future caller that skips the route can't bypass
it. Officer actions on a claimed application are limited to the officer who
claimed it, or an admin (_require_assignee). An admin may also act in the
officer role, but each action stays a separate, separately audited call -
nothing here combines "recommend" and "decide".
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import (
    AdminReturn,
    CustomerVerification,
    Disbursement,
    Document,
    InformationRequest,
    InformationResponse,
    Loan,
    LoanApplication,
    LoanLedgerEntry,
    LoanTermsSnapshot,
    OfficerRecommendation,
    Referee,
    TermsAcceptance,
    User,
)
from app.models.enums import (
    DisbursementMethod,
    LedgerActorKind,
    LedgerEntryType,
    DocumentType,
    EmploymentStatus,
    InformationRequestStatus,
    InformationRequestType,
    LoanApplicationStatus,
    LoanClosureReason,
    LoanPurposeCategory,
    LoanStatus,
    OfficerRecommendationType,
    UserRole,
)

from . import (
    audit,
    credit_evaluation,
    documents,
    notifications,
    pricing_policy,
    prime_pricing,
    verification,
)
from . import customer_verification as cv_service
from .errors import ServiceError

_OPEN_APPLICATION_STATUSES = (
    LoanApplicationStatus.SUBMITTED,
    LoanApplicationStatus.OFFICER_REVIEW,
    LoanApplicationStatus.CUSTOMER_ACTION_REQUIRED,
    LoanApplicationStatus.RECOMMENDED_FOR_APPROVAL,
    LoanApplicationStatus.RECOMMENDED_FOR_REJECTION,
    LoanApplicationStatus.ADMIN_REVIEW,
    LoanApplicationStatus.RETURNED_TO_OFFICER,
)
OPEN_APPLICATION_STATUSES = _OPEN_APPLICATION_STATUSES
# Any of these may be early-exit rejected (by an admin) without walking the whole chain.
_REJECTABLE_STATUSES = _OPEN_APPLICATION_STATUSES
_RECOMMENDED_STATUSES = (
    LoanApplicationStatus.RECOMMENDED_FOR_APPROVAL,
    LoanApplicationStatus.RECOMMENDED_FOR_REJECTION,
)
_RETURNABLE_STATUSES = (*_RECOMMENDED_STATUSES, LoanApplicationStatus.ADMIN_REVIEW)
# Where the officer can work the checklist (incl. while waiting on the customer).
CHECKLIST_EDITABLE_STATUSES = (
    LoanApplicationStatus.OFFICER_REVIEW,
    LoanApplicationStatus.CUSTOMER_ACTION_REQUIRED,
)
_MAX_REQUESTS_PER_ROUND = 10

# Customer-facing labels - internal staff-routing statuses (OFFICER_REVIEW,
# RECOMMENDED_FOR_*, ADMIN_REVIEW, RETURNED_TO_OFFICER) collapse to one
# friendly label so a customer never sees raw workflow-stage enum text.
# Staff-facing UIs should use the raw `status` field instead, for precise
# workflow tracking.
_STATUS_LABELS: dict[LoanApplicationStatus, str] = {
    LoanApplicationStatus.DRAFT: "Draft",
    LoanApplicationStatus.SUBMITTED: "Submitted",
    LoanApplicationStatus.OFFICER_REVIEW: "Under Review",
    LoanApplicationStatus.CUSTOMER_ACTION_REQUIRED: "Action Required",
    LoanApplicationStatus.RECOMMENDED_FOR_APPROVAL: "Under Review",
    LoanApplicationStatus.RECOMMENDED_FOR_REJECTION: "Under Review",
    LoanApplicationStatus.ADMIN_REVIEW: "Under Review",
    LoanApplicationStatus.RETURNED_TO_OFFICER: "Under Review",
    LoanApplicationStatus.APPROVED: "Approved",
    LoanApplicationStatus.REJECTED: "Not Approved",
    LoanApplicationStatus.AWAITING_DISBURSEMENT: "Approved - Processing Disbursement",
    LoanApplicationStatus.DISBURSED: "Disbursed",
}


def status_label(status: LoanApplicationStatus) -> str:
    """Customer-facing text for an application status. See _STATUS_LABELS."""
    return _STATUS_LABELS[LoanApplicationStatus(status)]


class LoanProcessingError(ServiceError):
    """Raised for loan business-rule violations."""


def _require_status(application: LoanApplication, *expected: LoanApplicationStatus) -> None:
    if application.status not in expected:
        wanted = " or ".join(s.value for s in expected)
        raise LoanProcessingError(
            f"Application #{application.id} is {application.status.value}, expected {wanted}.",
            status_code=409,
        )


# ------------------------------------------------------------------- role guards
def _is_admin(actor) -> bool:
    return actor.role == UserRole.ADMIN


def _require_admin(actor, action: str) -> None:
    """Service-level backstop for admin-only actions (the route checks too)."""
    if not _is_admin(actor):
        raise LoanProcessingError(f"Only an admin may {action}.", status_code=403)


def _require_assignee(application: LoanApplication, actor) -> None:
    """Claim-on-review: officer actions on an application belong to the
    officer who claimed it. Admins may always act (e.g. covering for an
    absent officer); they can also formally reassign via assign_application().
    """
    if _is_admin(actor):
        return
    if actor.role != UserRole.LOAN_OFFICER:
        raise LoanProcessingError("Only staff may do this.", status_code=403)
    if application.assigned_officer_id is None:
        raise LoanProcessingError(
            f"Application #{application.id} isn't assigned to an officer. "
            "Claim it via officer-review, or ask an admin to assign it.",
            status_code=409,
        )
    if application.assigned_officer_id != actor.id:
        raise LoanProcessingError(
            f"Application #{application.id} is assigned to another officer.", status_code=403
        )


def can_act_as_officer(application: LoanApplication, actor) -> bool:
    try:
        _require_assignee(application, actor)
    except LoanProcessingError:
        return False
    return True


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


# Employment statuses for which "who do you work for" has an answer (for
# self-employed: the business name).
_HAS_EMPLOYER = (EmploymentStatus.EMPLOYED, EmploymentStatus.SELF_EMPLOYED)


def _parse_text(value, field: str, limit: int, *, required: bool) -> str | None:
    text = (value or "").strip() if isinstance(value, (str, type(None))) else None
    if text is None:
        raise LoanProcessingError(f"{field} must be text.")
    if not text:
        if required:
            raise LoanProcessingError(f"{field} is required.")
        return None
    if len(text) > limit:
        raise LoanProcessingError(f"{field} must be at most {limit} characters.")
    return text


def _parse_residence(residential_address) -> str:
    return _parse_text(residential_address, "residential_address", 500, required=True)


def _parse_employer(employer_name, employment_status) -> str | None:
    return _parse_text(
        employer_name,
        "employer_name",
        255,
        required=employment_status in _HAS_EMPLOYER,
    )


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
    residential_address: str | None = None,
    employer_name: str | None = None,
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
    residence_val = _parse_residence(residential_address)
    employer_val = _parse_employer(employer_name, employment_val)

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
    # PRIME is one loan at a time: no new application while one is approved
    # and waiting to be paid out, or while a loan is still being repaid.
    approved = (
        LoanApplication.query.filter_by(user_id=user.id)
        .filter(LoanApplication.status.in_(
            (LoanApplicationStatus.APPROVED, LoanApplicationStatus.AWAITING_DISBURSEMENT)))
        .first()
    )
    if approved is not None:
        raise LoanProcessingError(
            f"Your application #{approved.id} is approved and waiting to be paid out. "
            "You can apply again once that loan is fully repaid.",
            status_code=409,
        )
    current = (
        Loan.query.filter_by(user_id=user.id)
        .filter(Loan.status.in_((LoanStatus.ACTIVE, LoanStatus.OVERDUE)))
        .first()
    )
    if current is not None:
        raise LoanProcessingError(
            f"You still have a loan (#{current.id}) to repay. "
            "You can apply again once it's fully repaid.",
            status_code=409,
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
        residential_address=residence_val,
        employer_name=employer_val,
    )
    pricing_policy.lock_quote(application, pricing)
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

    # Contact details that differ from what the customer was verified with
    # mean the verification no longer describes them.
    cv_service.check_information_changed(application)

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
    expected: tuple[LoanApplicationStatus, ...],
    new_status: LoanApplicationStatus,
    action: str,
    details: dict | None = None,
) -> LoanApplication:
    from_status = application.status
    _require_status(application, *expected)
    application.status = new_status
    audit.record(
        action,
        actor_id=actor.id,
        entity_type="LoanApplication",
        entity_id=application.id,
        details={"from": from_status.value, "to": new_status.value, **(details or {})},
        commit=False,
    )
    db.session.commit()
    return application


def latest_recommendation(application: LoanApplication) -> OfficerRecommendation | None:
    recs = application.officer_recommendations
    return recs[-1] if recs else None


def open_information_requests(application: LoanApplication) -> list[InformationRequest]:
    return [r for r in application.information_requests if r.status == InformationRequestStatus.OPEN]


def action_required_text(application: LoanApplication) -> str | None:
    """Back-compat value for the API's `action_required_note` field: the
    open requests' customer-facing reasons, one per line. (The old
    loan_applications.action_required_note column is no longer written.)
    """
    reasons = [r.reason for r in open_information_requests(application)]
    return "\n".join(reasons) if reasons else None


def serialize_information_request(r: InformationRequest, *, staff: bool) -> dict:
    """Customer view omits internal_note and staff identities."""
    response = r.response
    data = {
        "id": r.id,
        "request_type": str(r.request_type),
        "reason": r.reason,
        "required_document_type": str(r.required_document_type) if r.required_document_type else None,
        "required_information": r.required_information,
        "status": str(r.status),
        "requested_at": r.requested_at.isoformat() if r.requested_at else None,
        "cancelled_at": r.cancelled_at.isoformat() if r.cancelled_at else None,
        "response": (
            None
            if response is None
            else {
                "id": response.id,
                "response_note": response.response_note,
                "responded_at": response.responded_at.isoformat() if response.responded_at else None,
                "field_changes": response.field_changes,
                "provided_document_ids": response.provided_document_ids,
            }
        ),
    }
    if staff:
        data.update(
            {
                "internal_note": r.internal_note,
                "requested_by": r.requested_by,
                "requested_by_name": r.requester.full_name if r.requester else None,
                "cancelled_by": r.cancelled_by,
                "cancel_reason": r.cancel_reason,
            }
        )
    return data


def current_customer_verification(user_id: int) -> CustomerVerification | None:
    """The customer's VERIFIED, not-yet-expired customer-level verification, if any."""
    return cv_service.current(user_id)


# ------------------------------------------------------------------ claim
def start_officer_review(application: LoanApplication, officer) -> LoanApplication:
    """loan_officer or admin. Claims the application from the shared queue
    (sets assigned_officer_id) and opens its verification checklist.
    """
    _require_status(application, LoanApplicationStatus.SUBMITTED)
    previous_officer_id = application.assigned_officer_id
    application.assigned_officer_id = officer.id
    application.assigned_at = datetime.now(timezone.utc)
    items = verification.ensure_checklist(application)
    # A returning customer with a current verification doesn't redo the
    # identity checks - they're marked verified from it (and linked).
    carried = cv_service.carry_over(application, officer)
    return _transition(
        application,
        officer,
        expected=(LoanApplicationStatus.SUBMITTED,),
        new_status=LoanApplicationStatus.OFFICER_REVIEW,
        action="loan_application_officer_review_started",
        details={
            "assigned_officer_id": officer.id,
            "previous_assigned_officer_id": previous_officer_id,
            "checklist_opened": sorted(i.item_type for i in items),
            "identity_checks_carried_over": sorted(i.item_type for i in carried),
        },
    )


def assign_application(application: LoanApplication, admin, officer_id) -> LoanApplication:
    """admin only. Reassign (or first-assign) an open application to an
    active loan_officer or admin.
    """
    _require_admin(admin, "reassign an application")
    _require_status(application, *_OPEN_APPLICATION_STATUSES)
    try:
        officer_id = int(officer_id)
    except (TypeError, ValueError):
        raise LoanProcessingError("officer_id must be an integer.")
    officer = db.session.get(User, officer_id)
    if officer is None or not officer.is_active:
        raise LoanProcessingError(f"No active user #{officer_id}.", status_code=404)
    if officer.role not in (UserRole.LOAN_OFFICER, UserRole.ADMIN):
        raise LoanProcessingError("Applications can only be assigned to staff.")

    previous = application.assigned_officer_id
    application.assigned_officer_id = officer.id
    application.assigned_at = datetime.now(timezone.utc)
    audit.record(
        "loan_application_reassigned",
        actor_id=admin.id,
        entity_type="LoanApplication",
        entity_id=application.id,
        details={"from_officer_id": previous, "to_officer_id": officer.id},
        commit=False,
    )
    db.session.commit()
    return application


# ------------------------------------------------- request more information
def _parse_information_requests(requests) -> list[dict]:
    if not requests or not isinstance(requests, list):
        raise LoanProcessingError("requests must be a non-empty list.")
    if len(requests) > _MAX_REQUESTS_PER_ROUND:
        raise LoanProcessingError(f"At most {_MAX_REQUESTS_PER_ROUND} requests per call.")
    parsed = []
    for i, r in enumerate(requests):
        if not isinstance(r, dict):
            raise LoanProcessingError(f"requests[{i}] must be an object.")
        try:
            request_type = InformationRequestType(r.get("request_type"))
        except ValueError:
            allowed = ", ".join(t.value for t in InformationRequestType)
            raise LoanProcessingError(f"requests[{i}].request_type must be one of: {allowed}.")
        reason = (r.get("reason") or "").strip()
        if not reason:
            raise LoanProcessingError(f"requests[{i}].reason is required (shown to the customer).")
        document_type = r.get("required_document_type")
        if document_type is not None:
            try:
                document_type = DocumentType(document_type)
            except ValueError:
                allowed = ", ".join(t.value for t in DocumentType)
                raise LoanProcessingError(
                    f"requests[{i}].required_document_type must be one of: {allowed}."
                )
        fields = {
            "reason": (reason, 1000),
            "required_information": ((r.get("required_information") or "").strip() or None, 500),
            "internal_note": ((r.get("internal_note") or "").strip() or None, 1000),
        }
        for name, (value, limit) in fields.items():
            if value and len(value) > limit:
                raise LoanProcessingError(f"requests[{i}].{name} must be at most {limit} characters.")
        parsed.append(
            {
                "request_type": request_type,
                "reason": reason,
                "required_document_type": document_type,
                "required_information": fields["required_information"][0],
                "internal_note": fields["internal_note"][0],
            }
        )
    return parsed


def request_customer_action(
    application: LoanApplication, officer, requests
) -> list[InformationRequest]:
    """Assigned loan_officer or admin. Creates one InformationRequest row per
    item asked for and moves the application to CUSTOMER_ACTION_REQUIRED.
    Earlier rounds' requests and responses are left untouched.
    """
    _require_status(application, LoanApplicationStatus.OFFICER_REVIEW)
    _require_assignee(application, officer)
    parsed = _parse_information_requests(requests)

    rows = [
        InformationRequest(requested_by=officer.id, status=InformationRequestStatus.OPEN, **p)
        for p in parsed
    ]
    application.information_requests.extend(rows)
    db.session.flush()
    _transition(
        application,
        officer,
        expected=(LoanApplicationStatus.OFFICER_REVIEW,),
        new_status=LoanApplicationStatus.CUSTOMER_ACTION_REQUIRED,
        action="loan_application_customer_action_requested",
        details={
            "requests": [
                {
                    "id": r.id,
                    "request_type": r.request_type.value,
                    "reason": r.reason,
                    "required_document_type": (
                        r.required_document_type.value if r.required_document_type else None
                    ),
                    "required_information": r.required_information,
                    "internal_note": r.internal_note,
                }
                for r in rows
            ]
        },
    )

    # Tell the customer (reasons only - never the officer's internal note).
    # Best-effort and after the commit, so a mail failure can't undo the request.
    outcome = notifications.notify_customer_action_required(application, rows)
    audit.record(
        "customer_action_required_notification",
        actor_id=officer.id,
        entity_type="LoanApplication",
        entity_id=application.id,
        details={
            "request_ids": [r.id for r in rows],
            "sent": outcome.get("sent"),
            "reason": outcome.get("reason"),
        },
    )
    return rows


def _json_value(value):
    if isinstance(value, Decimal):
        return float(value)
    if hasattr(value, "value"):  # enum
        return value.value
    return value


def _parse_responses(responses, open_ids: set[int]) -> dict[int, str]:
    if not responses or not isinstance(responses, list):
        raise LoanProcessingError(
            "responses must be a non-empty list of {information_request_id, response_note}."
        )
    notes: dict[int, str] = {}
    for i, r in enumerate(responses):
        if not isinstance(r, dict):
            raise LoanProcessingError(f"responses[{i}] must be an object.")
        try:
            request_id = int(r.get("information_request_id"))
        except (TypeError, ValueError):
            raise LoanProcessingError(f"responses[{i}].information_request_id must be an integer.")
        note = (r.get("response_note") or "").strip()
        if not note:
            raise LoanProcessingError(f"responses[{i}].response_note is required.")
        if len(note) > 1000:
            raise LoanProcessingError(f"responses[{i}].response_note must be at most 1000 characters.")
        if request_id in notes:
            raise LoanProcessingError(f"Request #{request_id} is answered twice.")
        notes[request_id] = note

    unknown = sorted(set(notes) - open_ids)
    if unknown:
        raise LoanProcessingError(
            f"Request(s) {unknown} are not open requests on this application.", status_code=409
        )
    missing = sorted(open_ids - set(notes))
    if missing:
        raise LoanProcessingError(
            f"Every open request must be answered - missing: {missing}.", status_code=400
        )
    return notes


def _parse_document_ids(document_ids) -> list[int]:
    if document_ids is None:
        return []
    if not isinstance(document_ids, list):
        raise LoanProcessingError("document_ids must be a list of document ids.")
    try:
        return sorted({int(d) for d in document_ids})
    except (TypeError, ValueError):
        raise LoanProcessingError("document_ids must be a list of document ids.")


def _check_required_documents(open_requests, document_ids: list[int], customer) -> None:
    """Every open request that names a required_document_type must be
    answered with a NEW document of that type: one of the customer's own,
    not yet attached to any application or payment (i.e. uploaded for this
    response). Re-sending the unclear/expired file already on record, or a
    file of another type, doesn't count. One document may satisfy several
    requests for the same type.

    Requests without a required_document_type (information only - the
    `required_information` is free text, e.g. "Gross monthly income") need
    no file: their answer is the response note, which _parse_responses()
    already requires, plus any field updates sent with it.
    """
    needs = [r for r in open_requests if r.required_document_type is not None]
    if not needs:
        return
    fresh_types = {
        d.document_type
        for d in Document.query.filter(
            Document.id.in_(document_ids or [-1]),
            Document.user_id == customer.id,
            Document.loan_application_id.is_(None),
            Document.payment_transaction_id.is_(None),
        )
    }
    missing = [r for r in needs if r.required_document_type not in fresh_types]
    if missing:
        listed = ", ".join(
            f"#{r.id}: {r.required_document_type.value.replace('_', ' ')}"
            for r in sorted(missing, key=lambda r: r.id)
        )
        raise LoanProcessingError(
            f"Upload the requested document with your response (request {listed})."
        )


def respond_to_customer_action(
    application: LoanApplication,
    customer,
    *,
    responses,
    purpose_category=None,
    purpose: str | None = None,
    confirmed_full_name: str | None = None,
    confirmed_email: str | None = None,
    confirmed_phone_number: str | None = None,
    monthly_income=None,
    employment_status: str | None = None,
    existing_monthly_debt=None,
    residential_address: str | None = None,
    employer_name: str | None = None,
    referees=None,
    disbursement_method_requested=None,
    disbursement_account_reference=None,
    document_ids: list[int] | None = None,
) -> LoanApplication:
    """customer only, and only the application's own owner. Answers EVERY
    open InformationRequest (one InformationResponse row each, linked to that
    exact request), applies any field updates to the EXISTING application
    (never creates a new one), and returns it to OFFICER_REVIEW. The old value
    of every changed field is kept in each response's field_changes.
    """
    if application.user_id != customer.id:
        raise LoanProcessingError("You can only respond to your own application.", 403)
    _require_status(application, LoanApplicationStatus.CUSTOMER_ACTION_REQUIRED)
    open_requests = {r.id: r for r in open_information_requests(application)}
    notes = _parse_responses(responses, set(open_requests))
    document_ids = _parse_document_ids(document_ids)
    _check_required_documents(open_requests.values(), document_ids, customer)

    changed: list[str] = []
    field_changes: dict[str, dict] = {}

    def _record(name, old, new):
        if old != new:
            field_changes[name] = {"old": _json_value(old), "new": _json_value(new)}

    if purpose_category is not None:
        category, description = _parse_purpose(purpose_category, purpose)
        _record("purpose_category", application.purpose_category, category)
        _record("purpose", application.purpose, description)
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
        _record("confirmed_full_name", application.confirmed_full_name, full_name)
        _record("confirmed_email", application.confirmed_email, email)
        _record("confirmed_phone_number", application.confirmed_phone_number, phone_number)
        application.confirmed_full_name = full_name
        application.confirmed_email = email
        application.confirmed_phone_number = phone_number
        changed.append("personal_details")
        cv_service.check_information_changed(application)

    if monthly_income is not None:
        new = _parse_income(monthly_income)
        _record("monthly_income", application.monthly_income, new)
        application.monthly_income = new
        changed.append("monthly_income")
    if employment_status is not None:
        new = _parse_employment(employment_status)
        _record("employment_status", application.employment_status, new)
        application.employment_status = new
        changed.append("employment_status")
    if existing_monthly_debt is not None:
        new = _parse_debt(existing_monthly_debt)
        _record("existing_monthly_debt", application.existing_monthly_debt, new)
        application.existing_monthly_debt = new
        changed.append("existing_monthly_debt")
    if residential_address is not None:
        new = _parse_residence(residential_address)
        _record("residential_address", application.residential_address, new)
        application.residential_address = new
        changed.append("residential_address")
    if employer_name is not None or (
        employment_status is not None and application.employment_status in _HAS_EMPLOYER
        and not application.employer_name
    ):
        new = _parse_employer(
            employer_name if employer_name is not None else application.employer_name,
            application.employment_status,
        )
        _record("employer_name", application.employer_name, new)
        application.employer_name = new
        changed.append("employer_name")

    if referees is not None:
        referee_rows = _parse_referees(referees)
        old_referees = [
            {
                "full_name": r.full_name,
                "relationship": r.relationship_to_applicant,
                "mobile_number": r.mobile_number,
                "employer_name": r.employer_name,
            }
            for r in application.referees
        ]
        _record("referees", old_referees, referee_rows)
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
        _record(
            "disbursement_method_requested", application.disbursement_method_requested, method
        )
        _record(
            "disbursement_account_reference",
            application.disbursement_account_reference,
            account_reference,
        )
        application.disbursement_method_requested = method
        application.disbursement_account_reference = account_reference
        changed.append("disbursement_method")

    provided_document_ids = None
    if document_ids:
        documents.link_documents_to_application(document_ids, customer, application.id)
        provided_document_ids = document_ids
        changed.append("documents")

    response_rows = []
    for request_id, note in notes.items():
        request = open_requests[request_id]
        request.status = InformationRequestStatus.RESPONDED
        response_rows.append(
            InformationResponse(
                information_request_id=request_id,
                responded_by=customer.id,
                response_note=note,
                field_changes=field_changes or None,
                provided_document_ids=provided_document_ids,
            )
        )
    db.session.add_all(response_rows)
    db.session.flush()

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
    audit.record(
        "loan_application_customer_responded",
        actor_id=customer.id,
        entity_type="LoanApplication",
        entity_id=application.id,
        # Field NAMES only: the old/new values (personal details, income,
        # referees) live once, immutably, in each InformationResponse row's
        # field_changes - referenced here by response id, not copied.
        details={
            "request_ids": sorted(notes),
            "response_ids": sorted(r.id for r in response_rows),
            "changed_fields": changed,
            "changed_field_names": sorted(field_changes),
            "provided_document_ids": provided_document_ids,
            "from": LoanApplicationStatus.CUSTOMER_ACTION_REQUIRED.value,
            "to": LoanApplicationStatus.OFFICER_REVIEW.value,
        },
        commit=False,
    )
    db.session.commit()
    return application


def resume_officer_review(
    application: LoanApplication, officer, reason: str | None = None
) -> LoanApplication:
    """Assigned loan_officer or admin. Back to OFFICER_REVIEW from either:
      * CUSTOMER_ACTION_REQUIRED without waiting for the customer - every
        open request is CANCELLED (kept, with who/when/why), so `reason` is
        required; or
      * RETURNED_TO_OFFICER, after an admin sent it back.
    """
    _require_status(
        application,
        LoanApplicationStatus.CUSTOMER_ACTION_REQUIRED,
        LoanApplicationStatus.RETURNED_TO_OFFICER,
    )
    _require_assignee(application, officer)
    reason = (reason or "").strip() or None
    if reason and len(reason) > 1000:
        raise LoanProcessingError("reason must be at most 1000 characters.")

    cancelled: list[int] = []
    if application.status == LoanApplicationStatus.CUSTOMER_ACTION_REQUIRED:
        if not reason:
            raise LoanProcessingError(
                "reason is required - resuming cancels the customer's open requests."
            )
        now = datetime.now(timezone.utc)
        officer_id = officer.id  # read before the rows change (CHECK ties status to cancelled_*)
        with db.session.no_autoflush:
            for r in open_information_requests(application):
                r.status = InformationRequestStatus.CANCELLED
                r.cancelled_by = officer_id
                r.cancelled_at = now
                r.cancel_reason = reason
                cancelled.append(r.id)

    return _transition(
        application,
        officer,
        expected=(
            LoanApplicationStatus.CUSTOMER_ACTION_REQUIRED,
            LoanApplicationStatus.RETURNED_TO_OFFICER,
        ),
        new_status=LoanApplicationStatus.OFFICER_REVIEW,
        action="loan_application_officer_review_resumed",
        details={"reason": reason, "cancelled_request_ids": cancelled},
    )


# --------------------------------------------------------- verification
def update_checklist_item(
    application: LoanApplication,
    officer,
    item_type: str,
    *,
    status,
    note: str | None = None,
    evidence: dict | None = None,
):
    """Assigned loan_officer or admin, while the application is with the officer.

    Completing both identity checks (age 18+ with the DOB, valid ID with the
    document) creates the customer-level verification; undoing one that a
    verification came from invalidates it - same transaction as the check.
    """
    _require_status(application, *CHECKLIST_EDITABLE_STATUSES)
    _require_assignee(application, officer)
    verification.ensure_checklist(application)
    item = next((i for i in application.verification_items if i.item_type == item_type), None)
    previous_status = item.status if item else None
    previous_verification_id = item.customer_verification_id if item else None
    item = verification.update_item(
        application, officer, item_type, status=status, note=note, evidence=evidence, commit=False
    )
    cv_service.sync_after_item_change(
        application,
        officer,
        item,
        previous_status=previous_status,
        previous_verification_id=previous_verification_id,
    )
    db.session.commit()
    return item


def request_customer_reverification(application: LoanApplication, officer, note: str):
    """Assigned loan_officer or admin: invalidate the customer's current
    verification (STAFF_REQUESTED) and re-open this application's identity
    checks if they came from it."""
    _require_assignee(application, officer)
    row = cv_service.request_reverification(application, officer, note)
    db.session.commit()
    return row


def invalidate_outdated_customer_verifications(admin) -> int:
    """admin only: POLICY_UPDATED for every verification made under an older
    CUSTOMER_VERIFICATION_POLICY_VERSION."""
    _require_admin(admin, "invalidate verifications for a policy change")
    count = cv_service.invalidate_outdated_policy(admin)
    db.session.commit()
    return count


# ------------------------------------------------------- recommendation
def submit_recommendation(
    application: LoanApplication, officer, *, recommendation, comments
) -> OfficerRecommendation:
    """Assigned loan_officer or admin. Records an immutable
    OfficerRecommendation (with the checklist frozen into it) and hands the
    application to the admin as RECOMMENDED_FOR_APPROVAL / _REJECTION.

    This ONLY writes the recommendation row and the application status. It
    never creates a Loan, a repayment schedule or a Disbursement, and never
    decides the application - that is decide_application(), admin only.
    """
    _require_status(application, LoanApplicationStatus.OFFICER_REVIEW)
    _require_assignee(application, officer)
    try:
        kind = OfficerRecommendationType(recommendation)
    except ValueError:
        allowed = ", ".join(k.value for k in OfficerRecommendationType)
        raise LoanProcessingError(f"recommendation must be one of: {allowed}.")
    comments = (comments or "").strip()
    if not comments:
        raise LoanProcessingError("comments are required.")
    if len(comments) > 2000:
        raise LoanProcessingError("comments must be at most 2000 characters.")

    verification.ensure_checklist(application)
    if kind == OfficerRecommendationType.RECOMMEND_APPROVAL:
        blocking = verification.blocking_items(application)
        if blocking:
            raise LoanProcessingError(
                "Cannot recommend approval until every required checklist item is "
                f"verified or not applicable, and none failed. Outstanding: {blocking}.",
                status_code=409,
            )

    customer_verification = current_customer_verification(application.user_id)
    rec = OfficerRecommendation(
        officer_id=officer.id,
        recommendation=kind,
        comments=comments,
        checklist_snapshot=verification.snapshot(application),
        credit_evaluation_snapshot=application.credit_evaluation_result,
        customer_verification_id=customer_verification.id if customer_verification else None,
    )
    application.officer_recommendations.append(rec)
    db.session.flush()

    approve = kind == OfficerRecommendationType.RECOMMEND_APPROVAL
    _transition(
        application,
        officer,
        expected=(LoanApplicationStatus.OFFICER_REVIEW,),
        new_status=(
            LoanApplicationStatus.RECOMMENDED_FOR_APPROVAL
            if approve
            else LoanApplicationStatus.RECOMMENDED_FOR_REJECTION
        ),
        action=(
            "loan_application_recommended_for_approval"
            if approve
            else "loan_application_recommended_for_rejection"
        ),
        details={
            "recommendation_id": rec.id,
            "recommendation": kind.value,
            "note": comments,
            "checklist": {
                status: sum(1 for i in rec.checklist_snapshot if i["status"] == status)
                for status in ("verified", "not_applicable", "failed", "pending")
            },
            "credit_score": (application.credit_evaluation_result or {}).get("score"),
            "customer_verification_id": rec.customer_verification_id,
        },
    )
    return rec


# ------------------------------------------------------------- admin review
def start_admin_review(application: LoanApplication, admin) -> LoanApplication:
    """admin only."""
    _require_admin(admin, "start admin review")
    return _transition(
        application,
        admin,
        expected=_RECOMMENDED_STATUSES,
        new_status=LoanApplicationStatus.ADMIN_REVIEW,
        action="loan_application_admin_review_started",
    )


def return_to_officer(application: LoanApplication, admin, reason: str) -> AdminReturn:
    """admin only. Sends a recommended application back to its officer
    (RETURNED_TO_OFFICER) with a reason, instead of deciding it.
    """
    _require_admin(admin, "return an application to its officer")
    _require_status(application, *_RETURNABLE_STATUSES)
    reason = (reason or "").strip()
    if not reason:
        raise LoanProcessingError("reason is required.")
    if len(reason) > 2000:
        raise LoanProcessingError("reason must be at most 2000 characters.")
    rec = latest_recommendation(application)
    if rec is None:
        raise LoanProcessingError(
            f"Application #{application.id} has no recommendation to return.", status_code=409
        )

    returned = AdminReturn(officer_recommendation_id=rec.id, returned_by=admin.id, reason=reason)
    application.admin_returns.append(returned)
    db.session.flush()
    _transition(
        application,
        admin,
        expected=_RETURNABLE_STATUSES,
        new_status=LoanApplicationStatus.RETURNED_TO_OFFICER,
        action="loan_application_returned_to_officer",
        details={"admin_return_id": returned.id, "recommendation_id": rec.id, "reason": reason},
    )
    return returned


def reject_application(application: LoanApplication, admin, note: str | None = None) -> LoanApplication:
    """admin only. Early-exit reject from any open status - a doomed
    application doesn't need to walk the whole chain first. (A loan officer
    recommends rejection instead; see submit_recommendation().)
    """
    _require_admin(admin, "reject an application")
    note = (note or "").strip() or None
    if not note:
        raise LoanProcessingError("A reason is required to reject an application.")
    if len(note) > 2000:
        raise LoanProcessingError("The reason must be at most 2000 characters.")
    if application.status not in _REJECTABLE_STATUSES:
        raise LoanProcessingError(
            f"Application #{application.id} is already {application.status.value}.",
            status_code=409,
        )
    from_status = application.status
    rec = latest_recommendation(application)
    now = datetime.now(timezone.utc)
    application.status = LoanApplicationStatus.REJECTED
    application.decided_at = now
    application.decided_by = admin.id
    audit.record(
        "loan_application_decision",
        actor_id=admin.id,
        entity_type="LoanApplication",
        entity_id=application.id,
        details={
            "decision": "reject",
            "note": note,
            "early_exit": True,
            "from": from_status.value,
            "recommendation_id": rec.id if rec else None,
        },
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

    Going against the officer's recommendation requires a note. The audit
    entry records which recommendation the decision was made on, whether it
    overrode it, and whether the same person made both (allowed, but visible).
    """
    _require_admin(admin, "make the final decision")
    _require_status(application, LoanApplicationStatus.ADMIN_REVIEW)

    rec = latest_recommendation(application)
    overrides = bool(
        rec
        and (rec.recommendation == OfficerRecommendationType.RECOMMEND_APPROVAL) != approve
    )
    note = (note or "").strip() or None
    if not approve and not note:
        raise LoanProcessingError("A reason is required to reject an application.")
    if overrides and not note:
        raise LoanProcessingError(
            "note is required when the decision goes against the officer's recommendation."
        )
    decision_details = {
        "decision": "approve" if approve else "reject",
        "note": note,
        "recommendation_id": rec.id if rec else None,
        "recommendation": rec.recommendation.value if rec else None,
        "overrides_recommendation": overrides,
        "same_actor_as_recommender": bool(rec and rec.officer_id == admin.id),
    }

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
            details=decision_details,
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
        details=decision_details,
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
    disbursed_at: str | None = None,
    evidence_document_id: int | None = None,
) -> tuple[LoanApplication, Loan]:
    """admin only, AWAITING_DISBURSEMENT only. In ONE transaction: the Loan
    (ACTIVE), its repayment installment, the Disbursement record, the
    LoanTermsSnapshot, the ORIGINAL_OBLIGATION ledger entry, and the
    application -> DISBURSED. Any failure rolls all of it back.

    `method_reference` is required: the BSP transaction number, or the
    cash acknowledgement number. A BSP Mobile Banking disbursement also
    requires `evidence_document_id` (the BSP receipt); for cash it's optional. `disbursed_at` (ISO timestamp, default now)
    is when the money moved - never in the future.
    """
    from . import repayments_scheduler  # local import: avoid a circular import

    _require_admin(admin, "record a disbursement")
    # Serialise concurrent disbursements of this application (Postgres row
    # lock; a no-op on SQLite). The unique constraints on loans.application_id
    # and disbursements.application_id are the real guarantee - see below.
    db.session.refresh(application, with_for_update=True)
    _require_status(application, LoanApplicationStatus.AWAITING_DISBURSEMENT)

    try:
        disb_method = DisbursementMethod(method)
    except ValueError:
        allowed = ", ".join(m.value for m in DisbursementMethod)
        raise LoanProcessingError(f"method must be one of: {allowed}.")

    reference = (method_reference or "").strip()
    if not reference:
        raise LoanProcessingError(
            "method_reference is required: the BSP transaction number, or the cash "
            "acknowledgement number."
        )
    if len(reference) > 255:
        raise LoanProcessingError("method_reference must be at most 255 characters.")
    note = (note or "").strip() or None
    if note and len(note) > 500:
        raise LoanProcessingError("note must be at most 500 characters.")
    evidence = _disbursement_evidence(application, evidence_document_id)
    # Every BSP Mobile Banking payout must carry its BSP receipt; cash
    # acknowledgements stay optional.
    if disb_method == DisbursementMethod.BSP_MOBILE_BANKING and evidence is None:
        raise LoanProcessingError(
            "evidence_document_id is required for BSP Mobile Banking disbursements: upload the BSP receipt first."
        )

    terms = _locked_terms(application)
    now = _parse_disbursed_at(disbursed_at, application)
    disbursed_on = prime_pricing.local_date(now)
    due_date = disbursed_on + timedelta(days=terms["term_days"])

    try:
        loan = Loan(
            application_id=application.id,
            user_id=application.user_id,
            principal_amount=terms["principal"],
            interest_rate=terms["interest_rate"],
            term_days=terms["term_days"],
            monthly_payment=terms["total"],
            total_repayable=terms["total"],
            status=LoanStatus.ACTIVE,
            disbursed_at=now,
        )
        db.session.add(loan)
        db.session.flush()

        schedule = repayments_scheduler.generate_bullet_schedule(loan, start_date=disbursed_on)

        disbursement = Disbursement(
            application_id=application.id,
            loan_id=loan.id,
            method=disb_method,
            amount=terms["principal"],
            method_reference=reference,
            disbursed_at=now,
            recorded_by=admin.id,
            note=note,
            destination_masked=(
                _mask(application.disbursement_account_reference)
                if disb_method == DisbursementMethod.BSP_MOBILE_BANKING
                else None
            ),
            evidence_document_id=evidence.id if evidence else None,
        )
        db.session.add(disbursement)
        db.session.flush()

        db.session.add(LoanTermsSnapshot(
            loan_id=loan.id,
            application_id=application.id,
            disbursement_id=disbursement.id,
            pricing_version_id=terms["pricing_version_id"],
            penalty_policy_version_id=terms["penalty_policy_version_id"],
            prime_category=terms["category"],
            principal=terms["principal"],
            interest_rate=terms["interest_rate"],
            interest_amount=terms["interest_amount"],
            original_total_due=terms["total"],
            term_days=terms["term_days"],
            disbursed_at=now,
            disbursed_local_date=disbursed_on,
            due_date=due_date,
            created_by=admin.id,
        ))
        db.session.add(LoanLedgerEntry(
            loan_id=loan.id,
            entry_type=LedgerEntryType.ORIGINAL_OBLIGATION,
            amount=terms["total"],
            effective_date=disbursed_on,
            created_by=admin.id,
            created_by_kind=LedgerActorKind.ADMIN,
            disbursement_id=disbursement.id,
        ))
        db.session.flush()
        application.status = LoanApplicationStatus.DISBURSED
        audit.record(
            "loan_disbursed",
            actor_id=admin.id,
            entity_type="Loan",
            entity_id=loan.id,
            details={
                "application_id": application.id,
                "disbursement_id": disbursement.id,
                "principal": float(loan.principal_amount),
                "total_repayable": float(loan.total_repayable),
                "method": disb_method.value,
                "method_reference": reference,
                "evidence_document_id": disbursement.evidence_document_id,
                "disbursed_at": now.isoformat(),
                "due_date": schedule[0].due_date.isoformat(),
                "from": LoanApplicationStatus.AWAITING_DISBURSEMENT.value,
                "to": LoanApplicationStatus.DISBURSED.value,
            },
            commit=False,
        )
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        # A concurrent request got there first (the unique constraints on
        # loans/disbursements.application_id). Anything else is a real bug.
        if Disbursement.query.filter_by(application_id=application.id).first() is not None:
            raise LoanProcessingError(
                f"Application #{application.id} has already been disbursed.", status_code=409
            )
        raise
    except Exception:
        db.session.rollback()  # all-or-nothing: no loan, record, snapshot or entry survives
        raise

    notifications.notify_loan_approved(loan)
    return application, loan


def _mask(value: str | None) -> str | None:
    value = (value or "").strip()
    if not value:
        return None
    return "•••• " + value[-4:]


def _parse_disbursed_at(raw, application) -> datetime:
    now = datetime.now(timezone.utc)
    if raw in (None, ""):
        return now
    try:
        moment = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        raise LoanProcessingError("disbursed_at must be an ISO 8601 timestamp.")
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=prime_pricing.LOCAL_TZ)  # a local wall-clock time
    if moment > now + timedelta(minutes=5):
        raise LoanProcessingError("disbursed_at can't be in the future.")
    decided = application.decided_at
    if decided is not None:
        if decided.tzinfo is None:
            decided = decided.replace(tzinfo=timezone.utc)
        if moment < decided:
            raise LoanProcessingError("disbursed_at can't be before the application was approved.")
    return moment


def _disbursement_evidence(application, document_id):
    if document_id in (None, ""):
        return None
    from app.models import Document
    from app.models.enums import DocumentType

    try:
        doc = db.session.get(Document, int(document_id))
    except (TypeError, ValueError):
        doc = None
    if doc is None or doc.user_id != application.user_id or doc.document_type != DocumentType.DISBURSEMENT_EVIDENCE:
        raise LoanProcessingError(
            "evidence_document_id must be disbursement evidence uploaded for this application's customer."
        )
    if Disbursement.query.filter_by(evidence_document_id=doc.id).first() is not None:
        raise LoanProcessingError("That evidence document is already attached to another disbursement.")
    return doc


def _locked_terms(application: LoanApplication) -> dict:
    """The terms a loan is disbursed on: the quote locked when the customer
    submitted. An application from before quotes existed (none should remain
    after migration e4a9b7c2d158) is quoted from the current versions."""
    if application.pricing_version_id is None:
        pricing_policy.lock_quote(
            application, prime_pricing.calculate_prime(application.amount_requested)
        )
    principal = Decimal(application.amount_requested).quantize(Decimal("0.01"))
    interest = Decimal(application.quoted_interest_amount)
    return {
        "pricing_version_id": application.pricing_version_id,
        "penalty_policy_version_id": application.penalty_policy_version_id,
        "category": application.prime_category,
        "principal": principal,
        "interest_rate": Decimal(application.quoted_interest_rate),
        "interest_amount": interest,
        "total": principal + interest,
        "term_days": prime_pricing.PRIME_TERM_DAYS,
    }


# ---------------------------------------------------------------- 5. closure
def write_off_loan(loan: Loan, admin, note: str | None = None) -> Loan:
    """admin only. Closes an ACTIVE/OVERDUE loan as DEFAULTED, with a
    reason, writing its LoanClosure record (outstanding at write-off kept).
    A loan paid in full closes itself - see payment_processing.verify_payment.
    """
    from . import closures

    _require_admin(admin, "write off a loan")
    note = (note or "").strip()
    if not note:
        raise LoanProcessingError("A reason is required to write off a loan.")
    if loan.status not in (LoanStatus.ACTIVE, LoanStatus.OVERDUE):
        raise LoanProcessingError(
            f"Loan #{loan.id} is {loan.status.value}; only active/overdue loans "
            "can be written off.",
            status_code=409,
        )
    from . import ledger

    if ledger.balance(loan.id) <= 0:
        raise LoanProcessingError(f"Loan #{loan.id} has nothing outstanding to write off.", status_code=409)
    closures.record_closure(loan, LoanClosureReason.DEFAULTED, actor_id=admin.id)
    audit.record(
        "loan_write_off_reason",
        actor_id=admin.id,
        entity_type="Loan",
        entity_id=loan.id,
        details={"note": note},
        commit=False,
    )
    db.session.commit()
    return loan


# ------------------------------------------------- admin decision (one call)
AWAITING_FINAL_DECISION = (
    LoanApplicationStatus.RECOMMENDED_FOR_APPROVAL,
    LoanApplicationStatus.RECOMMENDED_FOR_REJECTION,
    LoanApplicationStatus.ADMIN_REVIEW,
)


def admin_decide(application: LoanApplication, admin, *, approve: bool, reason: str | None = None):
    """The Administrator's final decision from any "awaiting final decision"
    status. A RECOMMENDED_* application is taken into ADMIN_REVIEW first, in
    the same transaction (and audited), then decided. Approve only moves it
    to AWAITING_DISBURSEMENT - no loan, no disbursement."""
    _require_admin(admin, "make the final decision")
    _require_status(application, *AWAITING_FINAL_DECISION)
    if application.status != LoanApplicationStatus.ADMIN_REVIEW:
        from_status = application.status
        application.status = LoanApplicationStatus.ADMIN_REVIEW
        audit.record(
            "loan_application_admin_review_started",
            actor_id=admin.id,
            entity_type="LoanApplication",
            entity_id=application.id,
            details={"from": from_status.value, "to": LoanApplicationStatus.ADMIN_REVIEW.value,
                     "implicit": True},
            commit=False,
        )
    try:
        return decide_application(application, admin, approve=approve, note=reason)
    except Exception:
        db.session.rollback()
        raise
