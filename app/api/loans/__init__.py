"""Loans namespace - application intake and loan-officer review."""

from decimal import Decimal

from flask import request
from flask_restx import Namespace, Resource, abort, fields

from app.api.auth.decorators import current_user_id, roles_required
from app.extensions import db
from app.models import LoanApplication, User
from app.services import loan_processing
from app.services.loan_processing import LoanProcessingError

ns = Namespace("loans", description="Loan applications, review, and approval.")

# --------------------------------------------------------------------- payloads
apply_in = ns.model(
    "LoanApplyInput",
    {
        "amount_requested": fields.Float(required=True, example=5000),
        "purpose": fields.String(required=False, example="Working capital for market stall"),
        "term_months": fields.Integer(required=True, example=12),
        "repayment_frequency": fields.String(
            required=True, enum=["weekly", "biweekly", "monthly"], example="monthly"
        ),
        "monthly_income": fields.Float(
            required=False,
            example=800,
            description=(
                "Self-reported gross monthly income (interim credit-evaluation "
                "input - see BACKEND.md). Omitting this makes the application "
                "ineligible regardless of score, since affordability can't be assessed."
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
    },
)
decision_in = ns.model(
    "LoanDecisionInput",
    {
        "decision": fields.String(required=True, enum=["approve", "reject"], example="approve"),
        "interest_rate": fields.Float(
            required=False, description="Override annual rate as a fraction (e.g. 0.18). Omit to use the configured default.", example=0.18
        ),
        "note": fields.String(required=False, example="Approved at standard rate."),
    },
)

# --------------------------------------------------------------- response models
error_out = ns.model("ErrorResponse", {"message": fields.String})
schedule_item_out = ns.model(
    "RepaymentScheduleItem",
    {
        "installment_number": fields.Integer,
        "due_date": fields.String,
        "amount_due": fields.Float,
        "amount_paid": fields.Float,
        "status": fields.String(example="upcoming"),
    },
)
application_out = ns.model(
    "LoanApplication",
    {
        "id": fields.Integer,
        "user_id": fields.Integer,
        "amount_requested": fields.Float,
        "purpose": fields.String,
        "term_months": fields.Integer,
        "repayment_frequency": fields.String,
        "monthly_income": fields.Float,
        "employment_status": fields.String,
        "existing_monthly_debt": fields.Float,
        "status": fields.String(example="under_review"),
        "credit_evaluation_result": fields.Raw(
            description="Credit-evaluation payload - see app/services/credit_evaluation.py "
            "for the current algorithm (`algorithm` field identifies which one produced it)."
        ),
        "submitted_at": fields.String,
        "decided_at": fields.String,
        "decided_by": fields.Integer,
        "loan_id": fields.Integer,
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
        "term_months": fields.Integer,
        "installment_amount": fields.Float,
        "total_repayable": fields.Float,
        "status": fields.String(example="active"),
        "disbursed_at": fields.String,
        "repayment_schedule": fields.List(fields.Nested(schedule_item_out)),
    },
)
application_list_out = ns.model(
    "LoanApplicationList",
    {"count": fields.Integer, "applications": fields.List(fields.Nested(application_out))},
)
decision_out = ns.model(
    "LoanDecisionResult",
    {
        "application": fields.Nested(application_out),
        "loan": fields.Nested(loan_out, allow_null=True, skip_none=True),
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
    return {
        "id": a.id,
        "user_id": a.user_id,
        "amount_requested": _num(a.amount_requested),
        "purpose": a.purpose,
        "term_months": a.term_months,
        "repayment_frequency": str(a.repayment_frequency),
        "monthly_income": _num(a.monthly_income),
        "employment_status": str(a.employment_status) if a.employment_status else None,
        "existing_monthly_debt": _num(a.existing_monthly_debt),
        "status": str(a.status),
        "credit_evaluation_result": a.credit_evaluation_result,
        "submitted_at": a.submitted_at.isoformat() if a.submitted_at else None,
        "decided_at": a.decided_at.isoformat() if a.decided_at else None,
        "decided_by": a.decided_by,
        "loan_id": a.loan.id if a.loan else None,
    }


def serialize_loan(loan) -> dict:
    return {
        "id": loan.id,
        "application_id": loan.application_id,
        "user_id": loan.user_id,
        "principal_amount": _num(loan.principal_amount),
        "interest_rate": _num(loan.interest_rate),
        "term_months": loan.term_months,
        "installment_amount": _num(loan.monthly_payment),
        "total_repayable": _num(loan.total_repayable),
        "status": str(loan.status),
        "disbursed_at": loan.disbursed_at.isoformat() if loan.disbursed_at else None,
        "repayment_schedule": [
            {
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


# --------------------------------------------------------------------- 1. apply
@ns.route("/apply")
class LoanApply(Resource):
    @ns.doc(security="Bearer")
    @ns.expect(apply_in)
    @ns.response(201, "Application submitted", application_out)
    @ns.response(409, "You already have an open application", error_out)
    @roles_required("customer")
    def post(self):
        data = request.get_json(silent=True) or {}
        try:
            application = loan_processing.submit_application(
                _current_user(),
                amount_requested=data.get("amount_requested"),
                purpose=data.get("purpose"),
                term_months=data.get("term_months"),
                repayment_frequency=data.get("repayment_frequency"),
                monthly_income=data.get("monthly_income"),
                employment_status=data.get("employment_status"),
                existing_monthly_debt=data.get("existing_monthly_debt"),
            )
        except LoanProcessingError as exc:
            abort(exc.status_code, exc.message)
        return serialize_application(application), 201


# ------------------------------------------------------------- 2. list for review
@ns.route("/applications")
class LoanApplications(Resource):
    @ns.doc(security="Bearer", params={"status": "Filter by exact status (default: open applications only)"})
    @ns.response(200, "List of applications", application_list_out)
    @roles_required("loan_officer", "admin")
    def get(self):
        try:
            rows = loan_processing.list_applications(status=request.args.get("status"))
        except LoanProcessingError as exc:
            abort(exc.status_code, exc.message)
        return {"count": len(rows), "applications": [serialize_application(a) for a in rows]}


# ---------------------------------------------------------------- 3. decision
@ns.route("/applications/<int:application_id>/decision")
class LoanApplicationDecision(Resource):
    @ns.doc(security="Bearer")
    @ns.expect(decision_in)
    @ns.response(200, "Decision recorded", decision_out)
    @ns.response(409, "Application already decided", error_out)
    @roles_required("loan_officer", "admin")
    def post(self, application_id: int):
        application = db.session.get(LoanApplication, application_id)
        if application is None:
            abort(404, f"Application #{application_id} not found.")

        data = request.get_json(silent=True) or {}
        decision = (data.get("decision") or "").strip().lower()
        if decision not in {"approve", "reject"}:
            abort(400, "decision must be 'approve' or 'reject'.")

        try:
            application, loan = loan_processing.decide_application(
                application,
                _current_user(),
                approve=(decision == "approve"),
                interest_rate=data.get("interest_rate"),
                note=data.get("note"),
            )
        except LoanProcessingError as exc:
            abort(exc.status_code, exc.message)

        return {
            "application": serialize_application(application),
            "loan": serialize_loan(loan) if loan else None,
        }


# ------------------------------------------------------------------- my loans
@ns.route("/mine")
class MyLoans(Resource):
    @ns.doc(security="Bearer")
    @ns.response(200, "The authenticated customer's loans", my_loans_out)
    @roles_required("customer")
    def get(self):
        from app.models import Loan

        rows = Loan.query.filter_by(user_id=current_user_id()).order_by(Loan.id.desc()).all()
        return {"count": len(rows), "loans": [serialize_loan(l) for l in rows]}
