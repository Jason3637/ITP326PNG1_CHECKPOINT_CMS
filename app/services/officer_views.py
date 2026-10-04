"""Loan Officer workspace read models: dashboard queues, the one-call
Application Review screen, and the application-scoped customer history.

Everything here is a read, filtered in SQL (queues are real queries, not
"fetch everything and filter client-side"). The writes are audit entries -
viewing a customer's history is logged, since it's access to personal data -
and lazily opening the verification checklist for an application already in
review that predates it (see open_checklist_if_needed()).

Role checks are enforced at the API layer (roles_required: loan_officer or
admin). Customer history is additionally scoped here: an officer can only
reach a customer THROUGH an application that is still open (in the review
pipeline); there is no customer-id lookup at all.
"""

from datetime import date
from decimal import Decimal

from sqlalchemy import func

from app.extensions import db
from app.models import Document, Loan, LoanApplication, User
from app.models.enums import (
    DocumentType,
    LoanApplicationStatus,
    LoanClosureReason,
    LoanStatus,
    PaymentStatus,
    RepaymentStatus,
    UserRole,
)

from . import audit, credit_evaluation, documents, loan_processing, prime_pricing, verification
from . import customer_verification as cv_service
from .errors import ServiceError

S = LoanApplicationStatus

# Dashboard queue -> the statuses it covers.
QUEUES: dict[str, tuple[LoanApplicationStatus, ...]] = {
    "awaiting_review": (S.SUBMITTED,),
    "under_review": (S.OFFICER_REVIEW,),
    "customer_action_required": (S.CUSTOMER_ACTION_REQUIRED,),
    "sent_to_admin": (S.RECOMMENDED_FOR_APPROVAL, S.RECOMMENDED_FOR_REJECTION, S.ADMIN_REVIEW),
    "returned_by_admin": (S.RETURNED_TO_OFFICER,),
}
ASSIGNMENT_FILTERS = ("any", "me", "unassigned")

CREDIT_ASSESSMENT_LABEL = "Advisory - not a decision input"

_CENTS = Decimal("0.01")


def _money(value) -> float:
    return float(Decimal(str(value or 0)).quantize(_CENTS))


def _iso(value):
    return value.isoformat() if value else None


# ------------------------------------------------------------------- queues
def queue_counts(viewer: User) -> dict:
    """{queue: {total, mine, unassigned}} from one grouped query."""
    rows = (
        db.session.query(
            LoanApplication.status,
            LoanApplication.assigned_officer_id,
            func.count(LoanApplication.id),
        )
        .filter(LoanApplication.status.in_([s for ss in QUEUES.values() for s in ss]))
        .group_by(LoanApplication.status, LoanApplication.assigned_officer_id)
        .all()
    )
    counts = {name: {"total": 0, "mine": 0, "unassigned": 0} for name in QUEUES}
    for status, assigned_id, n in rows:
        for name, statuses in QUEUES.items():
            if status in statuses:
                counts[name]["total"] += n
                if assigned_id == viewer.id:
                    counts[name]["mine"] += n
                if assigned_id is None:
                    counts[name]["unassigned"] += n
    return counts


def list_queue(
    viewer: User,
    *,
    queue: str,
    assigned: str = "any",
    officer_id: int | None = None,
    prime_category: str | None = None,
    page: int = 1,
    per_page: int = 25,
) -> dict:
    if queue not in QUEUES:
        raise ServiceError(f"queue must be one of: {', '.join(QUEUES)}.")
    if assigned not in ASSIGNMENT_FILTERS:
        raise ServiceError(f"assigned must be one of: {', '.join(ASSIGNMENT_FILTERS)}.")
    if officer_id is not None and assigned != "any":
        raise ServiceError("Use either officer_id or assigned, not both.")
    page = max(1, int(page))
    per_page = max(1, min(int(per_page), 100))

    q = LoanApplication.query.filter(LoanApplication.status.in_(QUEUES[queue]))
    if assigned == "me":
        q = q.filter(LoanApplication.assigned_officer_id == viewer.id)
    elif assigned == "unassigned":
        q = q.filter(LoanApplication.assigned_officer_id.is_(None))
    if officer_id is not None:
        q = q.filter(LoanApplication.assigned_officer_id == officer_id)
    if prime_category:
        q = q.filter(LoanApplication.prime_category == prime_category)

    # Oldest first - first in, first reviewed.
    pagination = q.order_by(LoanApplication.submitted_at.asc(), LoanApplication.id.asc()).paginate(
        page=page, per_page=per_page, error_out=False
    )
    return {
        "queue": queue,
        "statuses": [s.value for s in QUEUES[queue]],
        "page": pagination.page,
        "per_page": pagination.per_page,
        "total": pagination.total,
        "pages": pagination.pages,
        "items": [queue_item(a, viewer) for a in pagination.items],
    }


def quoted_total(a: LoanApplication) -> float | None:
    """The total repayable the customer was quoted (locked at submission);
    live pricing only for an older row with no quote, and None for a
    pre-PRIME application PRIME can't price."""
    if a.quoted_total_repayable is not None:
        return _money(a.quoted_total_repayable)
    try:
        return _money(prime_pricing.calculate_prime(a.amount_requested)["total_repayable"])
    except ServiceError:
        return None


def queue_item(a: LoanApplication, viewer: User) -> dict:
    """Compact row for a queue list - enough to triage without opening it."""
    returned = a.admin_returns[-1] if a.admin_returns else None
    rec = loan_processing.latest_recommendation(a)
    return {
        "id": a.id,
        "status": str(a.status),
        "customer_id": a.user_id,
        "customer_name": a.applicant.full_name if a.applicant else None,
        "amount_requested": _money(a.amount_requested),
        "prime_category": a.prime_category,
        "total_repayable": quoted_total(a),
        "purpose_category": str(a.purpose_category) if a.purpose_category else None,
        "submitted_at": _iso(a.submitted_at),
        "assigned_officer_id": a.assigned_officer_id,
        "assigned_officer_name": a.assigned_officer.full_name if a.assigned_officer else None,
        "assigned_at": _iso(a.assigned_at),
        "is_mine": a.assigned_officer_id == viewer.id,
        "open_information_requests": len(loan_processing.open_information_requests(a)),
        "latest_recommendation": rec.recommendation.value if rec else None,
        "returned_reason": returned.reason if returned and a.status == S.RETURNED_TO_OFFICER else None,
    }


# ------------------------------------------------------------------- detail
def _allowed_actions(a: LoanApplication, viewer: User) -> list[str]:
    """What the viewer can do next, so the UI doesn't re-derive the rules."""
    is_admin = viewer.role == UserRole.ADMIN
    mine = loan_processing.can_act_as_officer(a, viewer)
    actions = []
    if a.status == S.SUBMITTED:
        actions.append("claim")
    if mine and a.status in loan_processing.CHECKLIST_EDITABLE_STATUSES:
        actions.append("update_checklist")
    if mine and a.status == S.OFFICER_REVIEW:
        actions += ["request_information", "recommend_approval", "recommend_rejection"]
    if mine and a.status in (S.CUSTOMER_ACTION_REQUIRED, S.RETURNED_TO_OFFICER):
        actions.append("resume_review")
    if is_admin:
        if a.status in loan_processing.OPEN_APPLICATION_STATUSES:
            actions += ["assign", "reject"]
        if a.status in (S.RECOMMENDED_FOR_APPROVAL, S.RECOMMENDED_FOR_REJECTION):
            actions.append("start_admin_review")
        if a.status in (S.RECOMMENDED_FOR_APPROVAL, S.RECOMMENDED_FOR_REJECTION, S.ADMIN_REVIEW):
            actions.append("return_to_officer")
        if a.status == S.ADMIN_REVIEW:
            actions.append("decide")
        if a.status == S.AWAITING_DISBURSEMENT:
            actions.append("disburse")
    return actions


def customer_block(user: User) -> dict:
    cv = loan_processing.current_customer_verification(user.id)
    return {
        "id": user.id,
        "full_name": user.full_name,
        "email": user.email,
        "phone_number": user.phone_number,
        "member_since": _iso(user.created_at),
        "is_active": user.is_active,
        # From the customer record (recorded by an officer from the ID), so
        # it shows whether or not a verification is currently valid.
        "date_of_birth": user.date_of_birth.isoformat() if user.date_of_birth else None,
        # The ONLY source of the "Verified customer" flag: a VERIFIED,
        # unexpired CustomerVerification (customer_verification.current()).
        "verification": (
            None
            if cv is None
            else {
                "id": cv.id,
                "status": cv.status.value,
                "verified_at": _iso(cv.verified_at),
                "verified_by": cv.verified_by,
                "verified_by_name": cv.verifier.full_name if cv.verifier else None,
                "valid_until": cv.valid_until.isoformat(),
                "date_of_birth": cv.date_of_birth.isoformat(),
                "id_document_id": cv.id_document_id,
                "id_expiry_date": cv.id_expiry_date.isoformat() if cv.id_expiry_date else None,
                "policy_version": cv.policy_version,
                "source_application_id": cv.source_application_id,
            }
        ),
    }


def _documents_block(a: LoanApplication) -> list[dict]:
    """Current documents linked to this application, plus the customer's
    current ID documents even if never linked (ID is customer-level)."""
    rows = (
        Document.query.filter(Document.user_id == a.user_id)
        .filter(Document.superseded_by_id.is_(None))
        .filter(
            (Document.loan_application_id == a.id)
            | (Document.document_type == DocumentType.ID_VERIFICATION)
        )
        .order_by(Document.uploaded_at.desc())
        .all()
    )
    return [
        documents.serialize(d) | {"linked_to_this_application": d.loan_application_id == a.id}
        for d in rows
    ]


def credit_assessment(a: LoanApplication) -> dict:
    """The interim credit model's output, wrapped so no consumer can mistake
    it for a decision. Nothing reads it to set status - see
    credit_evaluation.py's module docstring.

    The stored result keeps the disclaimer it was produced with; staff are
    always shown the current wording (credit_evaluation.DISCLAIMER). The
    stored row - and the copy frozen on each OfficerRecommendation - is
    left as it was.
    """
    result = a.credit_evaluation_result
    if result is not None:
        result = {**result, "disclaimer": credit_evaluation.DISCLAIMER}
    return {
        "label": CREDIT_ASSESSMENT_LABEL,
        "advisory": True,
        "affects_status": False,
        "result": result,
    }


def open_checklist_if_needed(a: LoanApplication, viewer: User) -> None:
    """The one write on a read path: an application already with its officer
    that predates the checklist (claimed before it existed, or a newly added
    item type) gets its missing PENDING rows. Idempotent. SUBMITTED and
    decided applications are left alone - checklist starts at the claim.
    """
    if a.status in (*loan_processing.CHECKLIST_EDITABLE_STATUSES, S.RETURNED_TO_OFFICER):
        present = {i.item_type for i in a.verification_items}
        missing = [t.key for t in verification.CHECKLIST if t.key not in present]
        if missing:
            verification.ensure_checklist(a)
            audit.record(
                "verification_checklist_opened",
                actor_id=viewer.id,
                entity_type="LoanApplication",
                entity_id=a.id,
                details={"item_types": missing, "status": str(a.status)},
                commit=False,
            )
        # A customer verified (on another application) since this checklist
        # opened: carry it over to identity checks still pending here.
        carried = cv_service.carry_over(a, viewer)
        if missing or carried:
            db.session.commit()


def application_detail(a: LoanApplication, viewer: User, application_payload: dict) -> dict:
    """Everything the Application Review screen needs in one call, EXCEPT
    customer history (heavier; separate endpoint, linked here).

    `application_payload` is the API layer's staff serialization of the
    application row (keeps one serializer for the core fields). Call
    open_checklist_if_needed() first (the API layer does).
    """
    return {
        "application": application_payload,
        "customer": customer_block(a.applicant),
        "documents": _documents_block(a),
        "information_requests": [
            loan_processing.serialize_information_request(r, staff=True)
            for r in a.information_requests
        ],
        "checklist": verification.serialize_checklist(a),
        "recommendations": [serialize_recommendation(r) for r in a.officer_recommendations],
        "admin_returns": [
            {
                "id": r.id,
                "recommendation_id": r.officer_recommendation_id,
                "returned_by": r.returned_by,
                "returned_by_name": r.admin.full_name if r.admin else None,
                "reason": r.reason,
                "created_at": _iso(r.created_at),
            }
            for r in a.admin_returns
        ],
        "assignment": {
            "officer_id": a.assigned_officer_id,
            "officer_name": a.assigned_officer.full_name if a.assigned_officer else None,
            "assigned_at": _iso(a.assigned_at),
            "is_mine": a.assigned_officer_id == viewer.id,
        },
        "credit_assessment": credit_assessment(a),
        "allowed_actions": _allowed_actions(a, viewer),
        "customer_history_url": f"/api/officer/applications/{a.id}/customer-history",
    }


def serialize_recommendation(r) -> dict:
    return {
        "id": r.id,
        "officer_id": r.officer_id,
        "officer_name": r.officer.full_name if r.officer else None,
        "recommendation": r.recommendation.value,
        "comments": r.comments,
        "checklist_snapshot": r.checklist_snapshot,
        "credit_evaluation_snapshot": r.credit_evaluation_snapshot,
        "customer_verification_id": r.customer_verification_id,
        "created_at": _iso(r.created_at),
    }


# ----------------------------------------------------------- customer history
def _installment_outcome(row) -> str:
    """paid_on_time | paid_late | overdue | upcoming - for one installment."""
    if row.status == RepaymentStatus.PAID:
        settled = max(
            (p for p in row.payments if p.paid_at is not None and p.status == PaymentStatus.VERIFIED),
            key=lambda p: p.paid_at,
            default=None,
        )
        if settled is not None and settled.paid_at.date() <= row.due_date:
            return "paid_on_time"
        return "paid_late"
    if row.due_date < date.today() or row.status == RepaymentStatus.OVERDUE:
        return "overdue"
    return "upcoming"


def customer_history(a: LoanApplication, viewer: User) -> dict:
    """History of the customer behind application `a`. Loan officers may
    only use this while `a` is open (in the review pipeline) - a decided
    application is not a handle for browsing a customer. Admins: always.

    Intentionally team-wide: there is no assignee check - any loan officer
    may view history for any open application, claimed by them, by another
    officer, or by no one. Every view is audited (`customer_history_viewed`).
    """
    if viewer.role != UserRole.ADMIN and a.status not in loan_processing.OPEN_APPLICATION_STATUSES:
        raise ServiceError(
            "Customer history is only available to loan officers while the "
            "application is under review.",
            403,
        )
    customer = a.applicant
    audit.record(
        "customer_history_viewed",
        actor_id=viewer.id,
        entity_type="LoanApplication",
        entity_id=a.id,
        details={"customer_id": customer.id, "application_status": str(a.status)},
    )

    previous = (
        LoanApplication.query.filter(LoanApplication.user_id == customer.id)
        .filter(LoanApplication.id != a.id)
        .order_by(LoanApplication.submitted_at.desc())
        .all()
    )
    loans = Loan.query.filter_by(user_id=customer.id).order_by(Loan.id.desc()).all()

    tally = {"paid_on_time": 0, "paid_late": 0, "overdue": 0, "upcoming": 0}
    payments = {"reported": 0, "verified": 0, "rejected": 0}
    loan_rows = []
    total_borrowed = total_repayable = total_repaid = exposure = Decimal("0")
    for loan in loans:
        outcomes = [_installment_outcome(r) for r in loan.repayment_schedule]
        for o in outcomes:
            tally[o] += 1
        for p in loan.payments:
            if p.status == PaymentStatus.VERIFIED:
                payments["verified"] += 1
            elif p.status == PaymentStatus.REJECTED:
                payments["rejected"] += 1
            else:
                payments["reported"] += 1
        due = sum((Decimal(r.amount_due) for r in loan.repayment_schedule), Decimal("0"))
        paid = sum((Decimal(r.amount_paid) for r in loan.repayment_schedule), Decimal("0"))
        outstanding = max(Decimal("0"), due - paid)
        total_borrowed += Decimal(loan.principal_amount)
        total_repayable += Decimal(loan.total_repayable)
        total_repaid += paid
        if loan.status in (LoanStatus.ACTIVE, LoanStatus.OVERDUE):
            exposure += outstanding
        last_due = max((r.due_date for r in loan.repayment_schedule), default=None)
        loan_rows.append(
            {
                "id": loan.id,
                "application_id": loan.application_id,
                "principal_amount": _money(loan.principal_amount),
                "total_repayable": _money(loan.total_repayable),
                "amount_paid": _money(paid),
                "outstanding": _money(outstanding),
                "status": str(loan.status),
                "closure_reason": str(loan.closure_reason) if loan.closure_reason else None,
                "disbursed_at": _iso(loan.disbursed_at),
                "due_date": last_due.isoformat() if last_due else None,
                "installments": {
                    "total": len(outcomes),
                    "paid_on_time": outcomes.count("paid_on_time"),
                    "paid_late": outcomes.count("paid_late"),
                    "overdue": outcomes.count("overdue"),
                },
            }
        )

    completed = sum(
        1
        for loan in loans
        if loan.status == LoanStatus.PAID
        or (loan.status == LoanStatus.CLOSED and loan.closure_reason == LoanClosureReason.PAID_IN_FULL)
    )
    defaulted = sum(
        1
        for loan in loans
        if loan.status == LoanStatus.CLOSED and loan.closure_reason == LoanClosureReason.DEFAULTED
    )
    return {
        "application_id": a.id,
        "customer": {
            "id": customer.id,
            "full_name": customer.full_name,
            "member_since": _iso(customer.created_at),
        },
        "summary": {
            "previous_applications": len(previous),
            "previous_applications_rejected": sum(1 for p in previous if p.status == S.REJECTED),
            "loans_total": len(loans),
            "loans_active": sum(1 for loan in loans if loan.status == LoanStatus.ACTIVE),
            "loans_overdue": sum(1 for loan in loans if loan.status == LoanStatus.OVERDUE),
            "loans_completed": completed,
            "loans_defaulted": defaulted,
            "total_borrowed": _money(total_borrowed),
            "total_repayable": _money(total_repayable),
            "total_repaid": _money(total_repaid),
            "current_exposure": _money(exposure),
        },
        "repayment_record": {
            "installments_paid_on_time": tally["paid_on_time"],
            "installments_paid_late": tally["paid_late"],
            "installments_currently_overdue": tally["overdue"],
            # Every installment that has ever been late: paid late, or still unpaid past due.
            "installments_ever_overdue": tally["paid_late"] + tally["overdue"],
            "payments_verified": payments["verified"],
            "payments_rejected": payments["rejected"],
            "payments_awaiting_verification": payments["reported"],
        },
        "penalties": {
            "applicable": False,
            "note": (
                "PRIME loans carry no late-payment penalty or fee in this system - "
                "none are charged or tracked. Late repayment shows in repayment_record."
            ),
        },
        "previous_applications": [
            {
                "id": p.id,
                "submitted_at": _iso(p.submitted_at),
                "amount_requested": _money(p.amount_requested),
                "prime_category": p.prime_category,
                "status": str(p.status),
                "decided_at": _iso(p.decided_at),
                "loan_id": p.loan.id if p.loan else None,
            }
            for p in previous
        ],
        "loans": loan_rows,
    }
