"""Loans namespace - PRIME application intake, two-tier officer/admin review,
and disbursement.

Two serializations of an application:
  * customer view (apply, /applications/mine, respond) - never includes the
    credit assessment, staff identities or officers' internal notes;
  * staff view (every loan_officer/admin endpoint) - adds assignment, the
    full information-request history and the advisory credit assessment.

The Loan Officer workspace read endpoints (queues, review screen, checklist,
customer history) live in the `officer` namespace (app/api/officer).
"""

from decimal import Decimal

from flask import request
from flask_restx import Namespace, Resource, abort, fields

from app.api.auth.decorators import current_user_id, roles_required
from app.extensions import db
from app.models import Loan, LoanApplication, User
from app.services import loan_processing, officer_views, prime_pricing
from app.services.errors import ServiceError

ns = Namespace("loans", description="PRIME loan applications, review, and disbursement.")

# --------------------------------------------------------------------- payloads
referee_in = ns.model(
    "RefereeInput",
    {
        "full_name": fields.String(required=True, example="Maria Kaupa"),
        "relationship": fields.String(required=True, example="sibling"),
        "mobile_number": fields.String(required=True, example="+675 7123 4567"),
        "employer_name": fields.String(required=False, example="Bank South Pacific"),
    },
)
_PURPOSE_CATEGORIES = [
    "business", "school_fees", "medical", "home_improvement", "debt_consolidation", "other",
]
_DISBURSEMENT_METHODS = ["bsp_mobile_banking", "cash_on_hand"]
_REQUEST_TYPES = [
    "missing_document", "document_unclear", "document_expired", "information_mismatch",
    "referee_unreachable", "employment_confirmation", "other",
]
_DOCUMENT_TYPES = ["id_verification", "receipt", "loan_file", "proof_of_income"]

# Personal details: pre-filled by the frontend from GET /users/profile, shown
# back to the customer for review, and submitted alongside the application as
# a point-in-time confirmation - stored on the application, never silently
# overwriting the User row (see LoanApplication.confirmed_* in the model).
_personal_details_fields = {
    "confirmed_full_name": fields.String(required=True, example="Grace Waigani"),
    "confirmed_email": fields.String(required=True, example="grace.waigani@example.com"),
    "confirmed_phone_number": fields.String(required=False, example="+675 7123 4567"),
}
apply_in = ns.model(
    "LoanApplyInput",
    {
        "amount_requested": fields.Float(
            required=True,
            example=500,
            description="Whole Kina, K100-K1,000 (PRIME product). Preview via GET /loans/prime-preview.",
        ),
        "purpose_category": fields.String(required=True, enum=_PURPOSE_CATEGORIES, example="business"),
        "purpose": fields.String(
            required=False,
            example="Market stall inventory",
            description="Short description. Required when purpose_category is 'other'.",
        ),
        **_personal_details_fields,
        "monthly_income": fields.Float(
            required=False,
            example=800,
            description=(
                "Self-reported gross monthly income (interim credit-evaluation "
                "input, advisory only - see BACKEND.md)."
            ),
        ),
        "employment_status": fields.String(
            required=False,
            enum=["employed", "self_employed", "unemployed", "retired", "student"],
            example="employed",
        ),
        "existing_monthly_debt": fields.Float(
            required=False,
            example=0,
            description="Other recurring monthly debt obligations, if any. Defaults to 0 if omitted.",
        ),
        "residential_address": fields.String(
            required=True,
            example="Section 12, Lot 4, Gerehu Stage 2, Port Moresby, NCD",
            description="Where the applicant lives (max 500).",
        ),
        "employer_name": fields.String(
            required=False,
            example="Bank South Pacific",
            description="Who the applicant works for (business name if self-employed). "
            "Required when employment_status is employed or self_employed (max 255).",
        ),
        "referees": fields.List(
            fields.Nested(referee_in), required=True, description="At least one required."
        ),
        "disbursement_method_requested": fields.String(
            required=True, enum=_DISBURSEMENT_METHODS, example="bsp_mobile_banking"
        ),
        "disbursement_account_reference": fields.String(
            required=False,
            description="BSP mobile banking number - required when the method is bsp_mobile_banking.",
            example="71234567",
        ),
        "accept_terms": fields.Boolean(required=True, example=True),
        "policy_version": fields.String(required=True, example="2026-09-v1"),
        "document_ids": fields.List(
            fields.Integer,
            required=False,
            description=(
                "Ids of documents already uploaded via POST /users/documents. "
                "Required to include a proof_of_income document when "
                "amount_requested >= K1,000."
            ),
        ),
    },
)
information_request_in = ns.model(
    "InformationRequestInput",
    {
        "request_type": fields.String(required=True, enum=_REQUEST_TYPES, example="missing_document"),
        "reason": fields.String(
            required=True,
            example="Please upload a payslip from the last 3 months.",
            description="Customer-facing: what is needed and why (max 1000).",
        ),
        "required_document_type": fields.String(required=False, enum=_DOCUMENT_TYPES),
        "required_information": fields.String(
            required=False, example="Employer's name and phone number", description="Max 500."
        ),
        "internal_note": fields.String(
            required=False,
            example="Payslip on file looks edited.",
            description="Staff-only, never shown to the customer (max 1000).",
        ),
    },
)
request_action_in = ns.model(
    "RequestMoreInformationInput",
    {
        "requests": fields.List(
            fields.Nested(information_request_in),
            required=True,
            description="1-10 items. Each becomes its own InformationRequest the customer must answer.",
        )
    },
)
response_item_in = ns.model(
    "InformationResponseInput",
    {
        "information_request_id": fields.Integer(required=True, example=12),
        "response_note": fields.String(required=True, example="Uploaded my September payslip."),
    },
)
respond_in = ns.model(
    "RespondToActionInput",
    {
        "responses": fields.List(
            fields.Nested(response_item_in),
            required=True,
            description="Exactly one entry per OPEN request on the application.",
        ),
        "purpose_category": fields.String(required=False, enum=_PURPOSE_CATEGORIES),
        "purpose": fields.String(required=False),
        "confirmed_full_name": fields.String(required=False),
        "confirmed_email": fields.String(required=False),
        "confirmed_phone_number": fields.String(required=False),
        "monthly_income": fields.Float(required=False),
        "employment_status": fields.String(
            required=False, enum=["employed", "self_employed", "unemployed", "retired", "student"]
        ),
        "existing_monthly_debt": fields.Float(required=False),
        "residential_address": fields.String(required=False, description="Max 500."),
        "employer_name": fields.String(required=False, description="Max 255."),
        "referees": fields.List(
            fields.Nested(referee_in),
            required=False,
            description="If provided, REPLACES the full referee list.",
        ),
        "disbursement_method_requested": fields.String(required=False, enum=_DISBURSEMENT_METHODS),
        "disbursement_account_reference": fields.String(required=False),
        "document_ids": fields.List(
            fields.Integer, required=False, description="Additional documents to link (e.g. a re-upload)."
        ),
    },
)
resume_in = ns.model(
    "ResumeReviewInput",
    {
        "reason": fields.String(
            required=False,
            example="Customer confirmed by phone; no upload needed.",
            description="Required when resuming from customer_action_required (cancels the open requests).",
        )
    },
)
recommend_in = ns.model(
    "RecommendationInput",
    {
        "recommendation": fields.String(
            required=True, enum=["recommend_approval", "recommend_rejection"], example="recommend_approval"
        ),
        "comments": fields.String(required=True, example="ID, employer and referee all confirmed."),
    },
)
assign_in = ns.model("AssignInput", {"officer_id": fields.Integer(required=True, example=7)})
reason_in = ns.model(
    "ReasonInput",
    {"reason": fields.String(required=True, example="Please re-check the referee's employer.")},
)
decision_in = ns.model(
    "LoanDecisionInput",
    {
        "decision": fields.String(required=True, enum=["approve", "reject"], example="approve"),
        "note": fields.String(
            required=False,
            example="Approved at standard PRIME terms.",
            description="Required when the decision goes against the officer's recommendation.",
        ),
    },
)
note_in = ns.model("NoteInput", {"note": fields.String(required=False)})
disburse_in = ns.model(
    "DisburseInput",
    {
        "method": fields.String(required=True, enum=["bsp_mobile_banking", "cash_on_hand"], example="bsp_mobile_banking"),
        "method_reference": fields.String(required=False, example="BSP-TXN-88213"),
        "note": fields.String(required=False),
    },
)

# --------------------------------------------------------------- response models
error_out = ns.model("ErrorResponse", {"message": fields.String})
pricing_out = ns.model(
    "PrimePricing",
    {
        "category": fields.String(example="PRIME 2"),
        "amount": fields.Float,
        "interest_amount": fields.Float,
        "interest_rate": fields.Float(
            example=0.4,
            description="The tier's flat rate for the whole term as a fraction (0.40 = 40% over "
            "term_days) - not an annual rate. interest_amount is this rate applied, rounded to whole Kina.",
        ),
        "total_repayable": fields.Float,
        "term_days": fields.Integer,
    },
)
schedule_item_out = ns.model(
    "RepaymentScheduleItem",
    {
        # The actual row id - POST /payments/repay requires this exact value
        # as repayment_schedule_id. Without it a customer/frontend has no way
        # to identify which installment to pay (installment_number alone
        # isn't the primary key and was never enough to call that endpoint).
        "id": fields.Integer,
        "installment_number": fields.Integer,
        "due_date": fields.String,
        "amount_due": fields.Float,
        "amount_paid": fields.Float,
        "status": fields.String(example="upcoming"),
    },
)
referee_out = ns.model(
    "Referee",
    {
        "id": fields.Integer,
        "full_name": fields.String,
        "relationship": fields.String,
        "mobile_number": fields.String,
        "employer_name": fields.String,
    },
)
information_response_out = ns.model(
    "InformationResponse",
    {
        "id": fields.Integer,
        "response_note": fields.String,
        "responded_at": fields.String,
        "field_changes": fields.Raw(description='{"field": {"old": ..., "new": ...}} - only changed fields'),
        "provided_document_ids": fields.List(fields.Integer),
    },
)
information_request_out = ns.model(
    "InformationRequest",
    {
        "id": fields.Integer,
        "request_type": fields.String(example="missing_document"),
        "reason": fields.String,
        "required_document_type": fields.String,
        "required_information": fields.String,
        "status": fields.String(example="open", description="open | responded | cancelled"),
        "requested_at": fields.String,
        "cancelled_at": fields.String,
        "response": fields.Nested(information_response_out, allow_null=True),
    },
)
information_request_staff_out = ns.inherit(
    "InformationRequestStaff",
    information_request_out,
    {
        "internal_note": fields.String(description="Staff-only."),
        "requested_by": fields.Integer,
        "requested_by_name": fields.String,
        "cancelled_by": fields.Integer,
        "cancel_reason": fields.String,
    },
)
application_out = ns.model(
    "LoanApplication",
    {
        "id": fields.Integer,
        "user_id": fields.Integer,
        "amount_requested": fields.Float,
        "purpose_category": fields.String,
        "purpose": fields.String,
        "confirmed_full_name": fields.String,
        "confirmed_email": fields.String,
        "confirmed_phone_number": fields.String,
        "prime_category": fields.String,
        "pricing": fields.Nested(pricing_out, description="Recomputed from prime_pricing.calculate_prime()"),
        "monthly_income": fields.Float,
        "employment_status": fields.String,
        "existing_monthly_debt": fields.Float,
        "residential_address": fields.String(
            description="Self-reported at submission. Null only on applications made before it was collected."
        ),
        "employer_name": fields.String(
            description="Self-reported at submission; null when not employed/self-employed, or on older applications."
        ),
        "disbursement_method_requested": fields.String,
        "disbursement_account_reference": fields.String,
        "referees": fields.List(fields.Nested(referee_out)),
        "policy_version_accepted": fields.String,
        "status": fields.String(
            example="officer_review", description="Internal workflow status - staff UIs."
        ),
        "status_label": fields.String(
            example="Under Review",
            description="Customer-facing label - internal staff-routing statuses collapse to one friendly label.",
        ),
        "action_required_note": fields.String(
            description=(
                "Back-compat summary: the open requests' reasons, one per line. "
                "Use information_requests for the structured version."
            )
        ),
        "information_requests": fields.List(
            fields.Nested(information_request_out),
            description="Every Request More Information round, oldest first (customer view).",
        ),
        "submitted_at": fields.String,
        "decided_at": fields.String,
        "decided_by": fields.Integer,
        "loan_id": fields.Integer,
    },
)
credit_assessment_out = ns.model(
    "CreditAssessment",
    {
        "label": fields.String(example=officer_views.CREDIT_ASSESSMENT_LABEL),
        "advisory": fields.Boolean(example=True),
        "affects_status": fields.Boolean(example=False),
        "result": fields.Raw(description="Interim credit model output (score, reasons, ...). Never sets status."),
    },
)
application_staff_out = ns.inherit(
    "LoanApplicationStaff",
    application_out,
    {
        "assigned_officer_id": fields.Integer,
        "assigned_officer_name": fields.String,
        "assigned_at": fields.String,
        "information_requests": fields.List(fields.Nested(information_request_staff_out)),
        "credit_assessment": fields.Nested(credit_assessment_out),
    },
)
disbursement_out = ns.model(
    "Disbursement",
    {
        "method": fields.String,
        "method_reference": fields.String,
        "amount": fields.Float,
        "disbursed_at": fields.String,
        "recorded_by": fields.Integer,
    },
)
loan_out = ns.model(
    "Loan",
    {
        "id": fields.Integer,
        "application_id": fields.Integer,
        "user_id": fields.Integer,
        "principal_amount": fields.Float,
        "interest_rate": fields.Float,
        "term_days": fields.Integer,
        "installment_amount": fields.Float,
        "total_repayable": fields.Float,
        "status": fields.String(example="active"),
        "closure_reason": fields.String,
        "blocks_reapplication": fields.Boolean(
            description="Written off and not yet cleared by an admin - the customer can't apply again until it is."
        ),
        "balance": fields.Raw(
            description="From the ledger (null for a loan with no ledger): original_obligation, "
            "penalties, verified_repayments, outstanding, due_date, days_overdue, and "
            "penalty_items [{tier, amount, applied_on, days_late, reason}]."
        ),
        "disbursed_at": fields.String,
        "disbursement": fields.Nested(disbursement_out, allow_null=True, skip_none=True),
        "repayment_schedule": fields.List(fields.Nested(schedule_item_out)),
    },
)
application_list_out = ns.model(
    "LoanApplicationList",
    {"count": fields.Integer, "applications": fields.List(fields.Nested(application_staff_out))},
)
my_application_list_out = ns.model(
    "MyLoanApplicationList",
    {"count": fields.Integer, "applications": fields.List(fields.Nested(application_out))},
)
recommendation_out = ns.model(
    "OfficerRecommendation",
    {
        "id": fields.Integer,
        "officer_id": fields.Integer,
        "officer_name": fields.String,
        "recommendation": fields.String(example="recommend_approval"),
        "comments": fields.String,
        "checklist_snapshot": fields.Raw(description="Frozen checklist items at the time of recommending."),
        "credit_evaluation_snapshot": fields.Raw(description="Advisory credit result the officer saw."),
        "customer_verification_id": fields.Integer,
        "created_at": fields.String,
    },
)
recommend_out = ns.model(
    "RecommendationResult",
    {
        "application": fields.Nested(application_staff_out),
        "recommendation": fields.Nested(recommendation_out),
    },
)
admin_return_out = ns.model(
    "AdminReturn",
    {
        "id": fields.Integer,
        "recommendation_id": fields.Integer,
        "returned_by": fields.Integer,
        "reason": fields.String,
        "created_at": fields.String,
    },
)
return_out = ns.model(
    "ReturnToOfficerResult",
    {"application": fields.Nested(application_staff_out), "admin_return": fields.Nested(admin_return_out)},
)
decision_out = ns.model(
    "LoanDecisionResult",
    {"application": fields.Nested(application_staff_out)},
)
disburse_out = ns.model(
    "DisburseResult",
    {
        "application": fields.Nested(application_staff_out),
        "loan": fields.Nested(loan_out),
    },
)
my_loans_out = ns.model(
    "MyLoans",
    {"count": fields.Integer, "loans": fields.List(fields.Nested(loan_out))},
)


# --------------------------------------------------------------------- helpers
def _num(value):
    return None if value is None else float(Decimal(str(value)))


def serialize_application(a: LoanApplication) -> dict:
    """Customer view. See the module docstring for what it leaves out."""
    # The quote locked at submission is the price; recomputing from the
    # current tiers would show a price the customer was never quoted once
    # an admin changes the rates. Older rows without a quote fall back.
    pricing = None
    if a.pricing_version_id is not None:
        pricing = {
            "category": a.prime_category,
            "amount": a.amount_requested,
            "interest_amount": a.quoted_interest_amount,
            "rate": a.quoted_interest_rate,
            "total_repayable": a.quoted_total_repayable,
            "term_days": prime_pricing.PRIME_TERM_DAYS,
        }
    else:
        try:
            pricing = prime_pricing.calculate_prime(a.amount_requested)
        except ServiceError:
            pricing = None  # e.g. a pre-PRIME application above K1,000
    return {
        "id": a.id,
        "user_id": a.user_id,
        "amount_requested": _num(a.amount_requested),
        "purpose_category": str(a.purpose_category) if a.purpose_category else None,
        "purpose": a.purpose,
        "confirmed_full_name": a.confirmed_full_name,
        "confirmed_email": a.confirmed_email,
        "confirmed_phone_number": a.confirmed_phone_number,
        "prime_category": a.prime_category,
        "pricing": (
            None
            if pricing is None
            else {
                "category": pricing["category"],
                "amount": _num(pricing["amount"]),
                "interest_amount": _num(pricing["interest_amount"]),
                "interest_rate": _num(pricing["rate"]),
                "total_repayable": _num(pricing["total_repayable"]),
                "term_days": pricing["term_days"],
            }
        ),
        "monthly_income": _num(a.monthly_income),
        "employment_status": str(a.employment_status) if a.employment_status else None,
        "existing_monthly_debt": _num(a.existing_monthly_debt),
        "residential_address": a.residential_address,
        "employer_name": a.employer_name,
        "disbursement_method_requested": (
            str(a.disbursement_method_requested) if a.disbursement_method_requested else None
        ),
        "disbursement_account_reference": a.disbursement_account_reference,
        "referees": [
            {
                "id": r.id,
                "full_name": r.full_name,
                "relationship": r.relationship_to_applicant,
                "mobile_number": r.mobile_number,
                "employer_name": r.employer_name,
            }
            for r in a.referees
        ],
        "policy_version_accepted": (
            a.terms_acceptance.policy_version if a.terms_acceptance else None
        ),
        "status": str(a.status),
        "status_label": loan_processing.status_label(a.status),
        "action_required_note": loan_processing.action_required_text(a),
        "information_requests": [
            loan_processing.serialize_information_request(r, staff=False)
            for r in a.information_requests
        ],
        "submitted_at": a.submitted_at.isoformat() if a.submitted_at else None,
        "decided_at": a.decided_at.isoformat() if a.decided_at else None,
        "decided_by": a.decided_by,
        "loan_id": a.loan.id if a.loan else None,
    }


def serialize_application_staff(a: LoanApplication) -> dict:
    """Staff view: customer view + assignment, staff request fields, and the
    advisory credit assessment (labelled as such)."""
    data = serialize_application(a)
    data.update(
        {
            "assigned_officer_id": a.assigned_officer_id,
            "assigned_officer_name": a.assigned_officer.full_name if a.assigned_officer else None,
            "assigned_at": a.assigned_at.isoformat() if a.assigned_at else None,
            "information_requests": [
                loan_processing.serialize_information_request(r, staff=True)
                for r in a.information_requests
            ],
            "credit_assessment": officer_views.credit_assessment(a),
        }
    )
    return data


def _loan_balance(loan: Loan) -> dict | None:
    """What is owed, from the ledger (None for a loan with no ledger)."""
    from app.services import ledger, penalties

    if not loan.ledger_entries:
        return None
    t = ledger.totals(loan.id)
    snap = loan.terms_snapshot
    return {
        "original_obligation": _num(t["original_obligation"]),
        "penalties": _num(t["penalties"]),
        "verified_repayments": _num(t["verified_repayments"]),
        "outstanding": _num(t["outstanding"]),
        "due_date": snap.due_date.isoformat() if snap else None,
        "days_overdue": ledger.days_overdue(snap.due_date, t["outstanding"]) if snap else 0,
        "penalty_items": penalties.describe(loan.id),
    }


def serialize_loan(loan: Loan) -> dict:
    return {
        "id": loan.id,
        "application_id": loan.application_id,
        "user_id": loan.user_id,
        "principal_amount": _num(loan.principal_amount),
        "interest_rate": _num(loan.interest_rate),
        "term_days": loan.term_days,
        "installment_amount": _num(loan.monthly_payment),
        "total_repayable": _num(loan.total_repayable),
        "status": str(loan.status),
        "closure_reason": str(loan.closure_reason) if loan.closure_reason else None,
        "disbursed_at": loan.disbursed_at.isoformat() if loan.disbursed_at else None,
        "disbursement": (
            None
            if not loan.disbursement
            else {
                "method": str(loan.disbursement.method),
                "method_reference": loan.disbursement.method_reference,
                "amount": _num(loan.disbursement.amount),
                "disbursed_at": loan.disbursement.disbursed_at.isoformat(),
                "recorded_by": loan.disbursement.recorded_by,
            }
        ),
        # From the ledger: what is owed now, including any late-payment
        # penalties (each with when it applied and why). The schedule below
        # only ever holds the original amount.
        "balance": _loan_balance(loan),
        # True only for a written-off loan an admin hasn't cleared yet: the
        # customer can't apply for PRIME again until it's cleared.
        "blocks_reapplication": loan.blocks_reapplication,
        "repayment_schedule": [
            {
                "id": r.id,
                "installment_number": r.installment_number,
                "due_date": r.due_date.isoformat(),
                "amount_due": _num(r.amount_due),
                "amount_paid": _num(r.amount_paid),
                "status": str(r.status),
            }
            for r in sorted(loan.repayment_schedule, key=lambda r: r.installment_number)
        ],
    }


def _current_user() -> User:
    user = db.session.get(User, current_user_id())
    if user is None:
        abort(404, "User not found.")
    return user


def _get_application(application_id: int) -> LoanApplication:
    application = db.session.get(LoanApplication, application_id)
    if application is None:
        abort(404, f"Application #{application_id} not found.")
    return application


def _body() -> dict:
    return request.get_json(silent=True) or {}


# --------------------------------------------------------------------- 1. apply
@ns.route("/apply")
class LoanApply(Resource):
    @ns.doc(security="Bearer")
    @ns.expect(apply_in)
    @ns.response(201, "Application submitted", application_out)
    @ns.response(400, "Validation error (amount out of range, missing referee, etc.)", error_out)
    @ns.response(409, "You already have an open application", error_out)
    @roles_required("customer")
    def post(self):
        data = _body()
        try:
            application = loan_processing.submit_application(
                _current_user(),
                amount_requested=data.get("amount_requested"),
                purpose_category=data.get("purpose_category"),
                purpose=data.get("purpose"),
                confirmed_full_name=data.get("confirmed_full_name"),
                confirmed_email=data.get("confirmed_email"),
                confirmed_phone_number=data.get("confirmed_phone_number"),
                monthly_income=data.get("monthly_income"),
                employment_status=data.get("employment_status"),
                existing_monthly_debt=data.get("existing_monthly_debt"),
                residential_address=data.get("residential_address"),
                employer_name=data.get("employer_name"),
                referees=data.get("referees"),
                disbursement_method_requested=data.get("disbursement_method_requested"),
                disbursement_account_reference=data.get("disbursement_account_reference"),
                accept_terms=bool(data.get("accept_terms")),
                policy_version=data.get("policy_version"),
                document_ids=data.get("document_ids"),
            )
        except ServiceError as exc:
            abort(exc.status_code, exc.message)
        return serialize_application(application), 201


# ---------------------------------------------------------------- 1b. preview
@ns.route("/prime-preview")
class PrimePreview(Resource):
    @ns.doc(
        security="Bearer",
        params={"amount_requested": "Whole-Kina amount to price, e.g. 500"},
    )
    @ns.response(200, "Live PRIME pricing - not persisted, not a submission", pricing_out)
    @ns.response(400, "amount_requested is missing or out of range", error_out)
    @roles_required()
    def get(self):
        """Lets the frontend show a live category/interest/total as the
        customer types an amount. This is the SAME function apply() uses
        (app.services.prime_pricing.calculate_prime) - a dedicated endpoint
        rather than publishing the tier constants, so the frontend never
        needs to know the thresholds/rates and a future rate change needs no
        frontend redeploy. The value returned here is never trusted or
        reused for the actual submission - apply() recomputes it itself from
        amount_requested alone.
        """
        raw = request.args.get("amount_requested")
        if raw is None:
            abort(400, "amount_requested is required.")
        try:
            pricing = prime_pricing.calculate_prime(raw)
        except ServiceError as exc:
            abort(exc.status_code, exc.message)
        return {
            "category": pricing["category"],
            "amount": _num(pricing["amount"]),
            "interest_amount": _num(pricing["interest_amount"]),
            "interest_rate": _num(pricing["rate"]),
            "total_repayable": _num(pricing["total_repayable"]),
            "term_days": pricing["term_days"],
        }


# ------------------------------------------------------------- 2. list for review
@ns.route("/applications")
class LoanApplications(Resource):
    @ns.doc(
        security="Bearer",
        params={
            "status": "Filter by exact status (default: open applications only). "
            "For the officer dashboard queues use GET /officer/queues/<queue>."
        },
    )
    @ns.response(200, "List of applications", application_list_out)
    @roles_required("loan_officer", "admin")
    def get(self):
        try:
            rows = loan_processing.list_applications(status=request.args.get("status"))
        except ServiceError as exc:
            abort(exc.status_code, exc.message)
        return {"count": len(rows), "applications": [serialize_application_staff(a) for a in rows]}


@ns.route("/applications/mine")
class MyApplications(Resource):
    @ns.doc(security="Bearer")
    @ns.response(200, "The authenticated customer's own applications (open and past)", my_application_list_out)
    @roles_required("customer")
    def get(self):
        rows = loan_processing.list_my_applications(_current_user())
        return {"count": len(rows), "applications": [serialize_application(a) for a in rows]}


# --------------------------------------------------------- 3. state-machine hops
@ns.route("/applications/<int:application_id>/officer-review")
class StartOfficerReview(Resource):
    @ns.doc(security="Bearer")
    @ns.response(200, "SUBMITTED -> OFFICER_REVIEW; claims it (assigned_officer_id = caller)", application_staff_out)
    @ns.response(409, "Application is not SUBMITTED (already claimed)", error_out)
    @roles_required("loan_officer", "admin")
    def post(self, application_id: int):
        application = _get_application(application_id)
        try:
            result = loan_processing.start_officer_review(application, _current_user())
        except ServiceError as exc:
            abort(exc.status_code, exc.message)
        return serialize_application_staff(result)


@ns.route("/applications/<int:application_id>/assign")
class AssignApplication(Resource):
    @ns.doc(security="Bearer")
    @ns.expect(assign_in)
    @ns.response(200, "Assigned to the given loan_officer/admin", application_staff_out)
    @ns.response(400, "officer_id is not a staff account", error_out)
    @ns.response(409, "Application is not open", error_out)
    @roles_required("admin")
    def post(self, application_id: int):
        application = _get_application(application_id)
        try:
            result = loan_processing.assign_application(
                application, _current_user(), _body().get("officer_id")
            )
        except ServiceError as exc:
            abort(exc.status_code, exc.message)
        return serialize_application_staff(result)


@ns.route("/applications/<int:application_id>/request-action")
class RequestCustomerAction(Resource):
    @ns.doc(security="Bearer")
    @ns.expect(request_action_in)
    @ns.response(
        200,
        "OFFICER_REVIEW -> CUSTOMER_ACTION_REQUIRED; one InformationRequest per item",
        application_staff_out,
    )
    @ns.response(400, "Invalid request item", error_out)
    @ns.response(403, "Assigned to another officer", error_out)
    @ns.response(409, "Application is not in OFFICER_REVIEW", error_out)
    @roles_required("loan_officer", "admin")
    def post(self, application_id: int):
        application = _get_application(application_id)
        try:
            loan_processing.request_customer_action(
                application, _current_user(), _body().get("requests")
            )
        except ServiceError as exc:
            abort(exc.status_code, exc.message)
        return serialize_application_staff(application)


@ns.route("/applications/<int:application_id>/respond")
class RespondToCustomerAction(Resource):
    @ns.doc(security="Bearer")
    @ns.expect(respond_in)
    @ns.response(
        200,
        "CUSTOMER_ACTION_REQUIRED -> OFFICER_REVIEW (updates the SAME application; "
        "one InformationResponse per answered request)",
        application_out,
    )
    @ns.response(
        400,
        "An open request is unanswered, a request that names a required_document_type "
        "has no newly uploaded document of that type in document_ids, or a field is invalid",
        error_out,
    )
    @ns.response(403, "Not your application", error_out)
    @ns.response(409, "Not in CUSTOMER_ACTION_REQUIRED, or answers a request that isn't open", error_out)
    @roles_required("customer")
    def post(self, application_id: int):
        application = _get_application(application_id)
        data = _body()
        try:
            result = loan_processing.respond_to_customer_action(
                application,
                _current_user(),
                responses=data.get("responses"),
                purpose_category=data.get("purpose_category"),
                purpose=data.get("purpose"),
                confirmed_full_name=data.get("confirmed_full_name"),
                confirmed_email=data.get("confirmed_email"),
                confirmed_phone_number=data.get("confirmed_phone_number"),
                monthly_income=data.get("monthly_income"),
                employment_status=data.get("employment_status"),
                existing_monthly_debt=data.get("existing_monthly_debt"),
                residential_address=data.get("residential_address"),
                employer_name=data.get("employer_name"),
                referees=data.get("referees"),
                disbursement_method_requested=data.get("disbursement_method_requested"),
                disbursement_account_reference=data.get("disbursement_account_reference"),
                document_ids=data.get("document_ids"),
            )
        except ServiceError as exc:
            abort(exc.status_code, exc.message)
        return serialize_application(result)


@ns.route("/applications/<int:application_id>/resume-review")
class ResumeOfficerReview(Resource):
    @ns.doc(security="Bearer")
    @ns.expect(resume_in)
    @ns.response(
        200,
        "CUSTOMER_ACTION_REQUIRED (open requests cancelled) or RETURNED_TO_OFFICER -> OFFICER_REVIEW",
        application_staff_out,
    )
    @ns.response(400, "reason missing when cancelling open requests", error_out)
    @ns.response(403, "Assigned to another officer", error_out)
    @ns.response(409, "Application is not in the expected status", error_out)
    @roles_required("loan_officer", "admin")
    def post(self, application_id: int):
        application = _get_application(application_id)
        try:
            result = loan_processing.resume_officer_review(
                application, _current_user(), _body().get("reason")
            )
        except ServiceError as exc:
            abort(exc.status_code, exc.message)
        return serialize_application_staff(result)


@ns.route("/applications/<int:application_id>/recommend")
class Recommend(Resource):
    @ns.doc(security="Bearer")
    @ns.expect(recommend_in)
    @ns.response(
        200,
        "OFFICER_REVIEW -> RECOMMENDED_FOR_APPROVAL / RECOMMENDED_FOR_REJECTION. "
        "Records the recommendation only - creates no loan, touches no disbursement.",
        recommend_out,
    )
    @ns.response(400, "Missing comments / unknown recommendation", error_out)
    @ns.response(403, "Assigned to another officer", error_out)
    @ns.response(409, "Not in OFFICER_REVIEW, or checklist incomplete for an approval", error_out)
    @roles_required("loan_officer", "admin")
    def post(self, application_id: int):
        application = _get_application(application_id)
        data = _body()
        try:
            rec = loan_processing.submit_recommendation(
                application,
                _current_user(),
                recommendation=data.get("recommendation"),
                comments=data.get("comments"),
            )
        except ServiceError as exc:
            abort(exc.status_code, exc.message)
        return {
            "application": serialize_application_staff(application),
            "recommendation": officer_views.serialize_recommendation(rec),
        }


@ns.route("/applications/<int:application_id>/admin-review")
class StartAdminReview(Resource):
    @ns.doc(security="Bearer")
    @ns.response(200, "RECOMMENDED_FOR_APPROVAL / _REJECTION -> ADMIN_REVIEW", application_staff_out)
    @ns.response(409, "Application is not in the expected status", error_out)
    @roles_required("admin")
    def post(self, application_id: int):
        application = _get_application(application_id)
        try:
            result = loan_processing.start_admin_review(application, _current_user())
        except ServiceError as exc:
            abort(exc.status_code, exc.message)
        return serialize_application_staff(result)


@ns.route("/applications/<int:application_id>/return-to-officer")
class ReturnToOfficer(Resource):
    @ns.doc(security="Bearer")
    @ns.expect(reason_in)
    @ns.response(
        200, "RECOMMENDED_FOR_* / ADMIN_REVIEW -> RETURNED_TO_OFFICER (AdminReturn recorded)", return_out
    )
    @ns.response(400, "reason is required", error_out)
    @ns.response(409, "Application is not recommended / in admin review", error_out)
    @roles_required("admin")
    def post(self, application_id: int):
        application = _get_application(application_id)
        try:
            returned = loan_processing.return_to_officer(
                application, _current_user(), _body().get("reason")
            )
        except ServiceError as exc:
            abort(exc.status_code, exc.message)
        return {
            "application": serialize_application_staff(application),
            "admin_return": {
                "id": returned.id,
                "recommendation_id": returned.officer_recommendation_id,
                "returned_by": returned.returned_by,
                "reason": returned.reason,
                "created_at": returned.created_at.isoformat() if returned.created_at else None,
            },
        }


@ns.route("/applications/<int:application_id>/reject")
class RejectApplication(Resource):
    @ns.doc(security="Bearer")
    @ns.expect(note_in)
    @ns.response(200, "Any open status -> REJECTED (admin early exit)", application_staff_out)
    @ns.response(409, "Application is already decided", error_out)
    @roles_required("admin")
    def post(self, application_id: int):
        application = _get_application(application_id)
        try:
            result = loan_processing.reject_application(
                application, _current_user(), _body().get("note")
            )
        except ServiceError as exc:
            abort(exc.status_code, exc.message)
        return serialize_application_staff(result)


# ---------------------------------------------------------------- 4. decision
@ns.route("/applications/<int:application_id>/decision")
class LoanApplicationDecision(Resource):
    @ns.doc(security="Bearer")
    @ns.expect(decision_in)
    @ns.response(200, "ADMIN_REVIEW -> APPROVED (then AWAITING_DISBURSEMENT) or REJECTED", decision_out)
    @ns.response(400, "Invalid decision, or a note is missing when overriding the recommendation", error_out)
    @ns.response(409, "Application is not in ADMIN_REVIEW", error_out)
    @roles_required("admin")
    def post(self, application_id: int):
        application = _get_application(application_id)

        data = _body()
        decision = (data.get("decision") or "").strip().lower()
        if decision not in {"approve", "reject"}:
            abort(400, "decision must be 'approve' or 'reject'.")

        try:
            application = loan_processing.decide_application(
                application,
                _current_user(),
                approve=(decision == "approve"),
                note=data.get("note"),
            )
        except ServiceError as exc:
            abort(exc.status_code, exc.message)

        return {"application": serialize_application_staff(application)}


# ------------------------------------------------------------ 5. disbursement
@ns.route("/applications/<int:application_id>/disburse")
class DisburseApplication(Resource):
    @ns.doc(security="Bearer")
    @ns.expect(disburse_in)
    @ns.response(200, "AWAITING_DISBURSEMENT -> Loan created (ACTIVE)", disburse_out)
    @ns.response(409, "Application is not AWAITING_DISBURSEMENT", error_out)
    @roles_required("admin")
    def post(self, application_id: int):
        application = _get_application(application_id)
        data = _body()
        method = data.get("method")
        if not method:
            abort(400, "method is required.")
        try:
            application, loan = loan_processing.disburse_application(
                application,
                _current_user(),
                method=method,
                method_reference=data.get("method_reference"),
                note=data.get("note"),
            )
        except ServiceError as exc:
            abort(exc.status_code, exc.message)
        return {"application": serialize_application_staff(application), "loan": serialize_loan(loan)}


# ------------------------------------------------------------------- my loans
@ns.route("/mine")
class MyLoans(Resource):
    @ns.doc(security="Bearer")
    @ns.response(200, "The authenticated customer's loans", my_loans_out)
    @roles_required("customer")
    def get(self):
        rows = Loan.query.filter_by(user_id=current_user_id()).order_by(Loan.id.desc()).all()
        return {"count": len(rows), "loans": [serialize_loan(l) for l in rows]}


# --------------------------------------------------------------------- 6. closure
def _get_loan(loan_id: int) -> Loan:
    loan = db.session.get(Loan, loan_id)
    if loan is None:
        abort(404, f"Loan #{loan_id} not found.")
    return loan


@ns.route("/<int:loan_id>/write-off")
class WriteOffLoan(Resource):
    @ns.doc(security="Bearer")
    @ns.expect(note_in)
    @ns.response(200, "ACTIVE/OVERDUE -> CLOSED (closure_reason=defaulted)", loan_out)
    @ns.response(409, "Loan is not active/overdue", error_out)
    @roles_required("admin")
    def post(self, loan_id: int):
        loan = _get_loan(loan_id)
        try:
            result = loan_processing.write_off_loan(loan, _current_user(), _body().get("note"))
        except ServiceError as exc:
            abort(exc.status_code, exc.message)
        return serialize_loan(result)
