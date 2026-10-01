"""Loans namespace - PRIME application intake, two-tier officer/admin review,
and disbursement.
"""

from decimal import Decimal

from flask import request
from flask_restx import Namespace, Resource, abort, fields

from app.api.auth.decorators import current_user_id, roles_required
from app.extensions import db
from app.models import Loan, LoanApplication, User
from app.services import loan_processing, prime_pricing
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
respond_in = ns.model(
    "RespondToActionInput",
    {
        "response_note": fields.String(
            required=True, example="Added the requested second referee and updated my employer."
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
decision_in = ns.model(
    "LoanDecisionInput",
    {
        "decision": fields.String(required=True, enum=["approve", "reject"], example="approve"),
        "note": fields.String(required=False, example="Approved at standard PRIME terms."),
    },
)
note_in = ns.model("NoteInput", {"note": fields.String(required=False)})
customer_action_in = ns.model(
    "CustomerActionInput",
    {
        "note": fields.String(
            required=True,
            example="Please add a second referee and confirm your employer's name.",
            description="Shown to the customer - what's missing / what to do.",
        )
    },
)
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
            description="What the officer needs from the customer - set while status is customer_action_required."
        ),
        "credit_evaluation_result": fields.Raw(
            description="Advisory only - see app/services/credit_evaluation.py. Never sets status."
        ),
        "submitted_at": fields.String,
        "decided_at": fields.String,
        "decided_by": fields.Integer,
        "loan_id": fields.Integer,
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
        "disbursed_at": fields.String,
        "disbursement": fields.Nested(disbursement_out, allow_null=True, skip_none=True),
        "repayment_schedule": fields.List(fields.Nested(schedule_item_out)),
    },
)
application_list_out = ns.model(
    "LoanApplicationList",
    {"count": fields.Integer, "applications": fields.List(fields.Nested(application_out))},
)
decision_out = ns.model(
    "LoanDecisionResult",
    {"application": fields.Nested(application_out)},
)
disburse_out = ns.model(
    "DisburseResult",
    {
        "application": fields.Nested(application_out),
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
    pricing = None
    try:
        pricing = prime_pricing.calculate_prime(a.amount_requested)
    except ServiceError:
        pricing = None  # shouldn't happen for a persisted application, but don't 500 on it
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
                "total_repayable": _num(pricing["total_repayable"]),
                "term_days": pricing["term_days"],
            }
        ),
        "monthly_income": _num(a.monthly_income),
        "employment_status": str(a.employment_status) if a.employment_status else None,
        "existing_monthly_debt": _num(a.existing_monthly_debt),
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
        "action_required_note": a.action_required_note,
        "credit_evaluation_result": a.credit_evaluation_result,
        "submitted_at": a.submitted_at.isoformat() if a.submitted_at else None,
        "decided_at": a.decided_at.isoformat() if a.decided_at else None,
        "decided_by": a.decided_by,
        "loan_id": a.loan.id if a.loan else None,
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
        data = request.get_json(silent=True) or {}
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
            "total_repayable": _num(pricing["total_repayable"]),
            "term_days": pricing["term_days"],
        }


# ------------------------------------------------------------- 2. list for review
@ns.route("/applications")
class LoanApplications(Resource):
    @ns.doc(security="Bearer", params={"status": "Filter by exact status (default: open applications only)"})
    @ns.response(200, "List of applications", application_list_out)
    @roles_required("loan_officer", "admin")
    def get(self):
        try:
            rows = loan_processing.list_applications(status=request.args.get("status"))
        except ServiceError as exc:
            abort(exc.status_code, exc.message)
        return {"count": len(rows), "applications": [serialize_application(a) for a in rows]}


@ns.route("/applications/mine")
class MyApplications(Resource):
    @ns.doc(security="Bearer")
    @ns.response(200, "The authenticated customer's own applications (open and past)", application_list_out)
    @roles_required("customer")
    def get(self):
        rows = loan_processing.list_my_applications(_current_user())
        return {"count": len(rows), "applications": [serialize_application(a) for a in rows]}


# --------------------------------------------------------- 3. state-machine hops
@ns.route("/applications/<int:application_id>/officer-review")
class StartOfficerReview(Resource):
    @ns.doc(security="Bearer")
    @ns.response(200, "SUBMITTED -> OFFICER_REVIEW", application_out)
    @ns.response(409, "Application is not in the expected status", error_out)
    @roles_required("loan_officer", "admin")
    def post(self, application_id: int):
        application = _get_application(application_id)
        try:
            result = loan_processing.start_officer_review(application, _current_user())
        except ServiceError as exc:
            abort(exc.status_code, exc.message)
        return serialize_application(result)


@ns.route("/applications/<int:application_id>/request-action")
class RequestCustomerAction(Resource):
    @ns.doc(security="Bearer")
    @ns.expect(customer_action_in)
    @ns.response(200, "OFFICER_REVIEW -> CUSTOMER_ACTION_REQUIRED", application_out)
    @ns.response(409, "Application is not in the expected status", error_out)
    @roles_required("loan_officer", "admin")
    def post(self, application_id: int):
        application = _get_application(application_id)
        data = request.get_json(silent=True) or {}
        try:
            result = loan_processing.request_customer_action(
                application, _current_user(), data.get("note")
            )
        except ServiceError as exc:
            abort(exc.status_code, exc.message)
        return serialize_application(result)


@ns.route("/applications/<int:application_id>/respond")
class RespondToCustomerAction(Resource):
    @ns.doc(security="Bearer")
    @ns.expect(respond_in)
    @ns.response(200, "CUSTOMER_ACTION_REQUIRED -> OFFICER_REVIEW (updates the SAME application)", application_out)
    @ns.response(403, "Not your application", error_out)
    @ns.response(409, "Application is not in CUSTOMER_ACTION_REQUIRED", error_out)
    @roles_required("customer")
    def post(self, application_id: int):
        application = _get_application(application_id)
        data = request.get_json(silent=True) or {}
        try:
            result = loan_processing.respond_to_customer_action(
                application,
                _current_user(),
                response_note=data.get("response_note"),
                purpose_category=data.get("purpose_category"),
                purpose=data.get("purpose"),
                confirmed_full_name=data.get("confirmed_full_name"),
                confirmed_email=data.get("confirmed_email"),
                confirmed_phone_number=data.get("confirmed_phone_number"),
                monthly_income=data.get("monthly_income"),
                employment_status=data.get("employment_status"),
                existing_monthly_debt=data.get("existing_monthly_debt"),
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
    @ns.response(200, "CUSTOMER_ACTION_REQUIRED -> OFFICER_REVIEW", application_out)
    @ns.response(409, "Application is not in the expected status", error_out)
    @roles_required("loan_officer", "admin")
    def post(self, application_id: int):
        application = _get_application(application_id)
        try:
            result = loan_processing.resume_officer_review(application, _current_user())
        except ServiceError as exc:
            abort(exc.status_code, exc.message)
        return serialize_application(result)


@ns.route("/applications/<int:application_id>/recommend")
class RecommendForApproval(Resource):
    @ns.doc(security="Bearer")
    @ns.expect(note_in)
    @ns.response(200, "OFFICER_REVIEW -> RECOMMENDED_FOR_APPROVAL", application_out)
    @ns.response(409, "Application is not in the expected status", error_out)
    @roles_required("loan_officer", "admin")
    def post(self, application_id: int):
        application = _get_application(application_id)
        data = request.get_json(silent=True) or {}
        try:
            result = loan_processing.recommend_for_approval(
                application, _current_user(), data.get("note")
            )
        except ServiceError as exc:
            abort(exc.status_code, exc.message)
        return serialize_application(result)


@ns.route("/applications/<int:application_id>/admin-review")
class StartAdminReview(Resource):
    @ns.doc(security="Bearer")
    @ns.response(200, "RECOMMENDED_FOR_APPROVAL -> ADMIN_REVIEW", application_out)
    @ns.response(409, "Application is not in the expected status", error_out)
    @roles_required("admin")
    def post(self, application_id: int):
        application = _get_application(application_id)
        try:
            result = loan_processing.start_admin_review(application, _current_user())
        except ServiceError as exc:
            abort(exc.status_code, exc.message)
        return serialize_application(result)


@ns.route("/applications/<int:application_id>/reject")
class RejectApplication(Resource):
    @ns.doc(security="Bearer")
    @ns.expect(note_in)
    @ns.response(200, "Any open status -> REJECTED (early exit)", application_out)
    @ns.response(409, "Application is already decided", error_out)
    @roles_required("loan_officer", "admin")
    def post(self, application_id: int):
        application = _get_application(application_id)
        data = request.get_json(silent=True) or {}
        try:
            result = loan_processing.reject_application(
                application, _current_user(), data.get("note")
            )
        except ServiceError as exc:
            abort(exc.status_code, exc.message)
        return serialize_application(result)


# ---------------------------------------------------------------- 4. decision
@ns.route("/applications/<int:application_id>/decision")
class LoanApplicationDecision(Resource):
    @ns.doc(security="Bearer")
    @ns.expect(decision_in)
    @ns.response(200, "ADMIN_REVIEW -> APPROVED (then AWAITING_DISBURSEMENT) or REJECTED", decision_out)
    @ns.response(409, "Application is not in ADMIN_REVIEW", error_out)
    @roles_required("admin")
    def post(self, application_id: int):
        application = _get_application(application_id)

        data = request.get_json(silent=True) or {}
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

        return {"application": serialize_application(application)}


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
        data = request.get_json(silent=True) or {}
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
        return {"application": serialize_application(application), "loan": serialize_loan(loan)}


# ------------------------------------------------------------------- my loans
@ns.route("/mine")
class MyLoans(Resource):
    @ns.doc(security="Bearer")
    @ns.response(200, "The authenticated customer's loans", my_loans_out)
    @roles_required("customer")
    def get(self):
        rows = Loan.query.filter_by(user_id=current_user_id()).order_by(Loan.id.desc()).all()
        return {"count": len(rows), "loans": [serialize_loan(l) for l in rows]}
