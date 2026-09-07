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
    @ns.expect(apply_in, validate=True)
    @ns.response(201, "Application submitted")
    @ns.response(409, "You already have an open application")
    @roles_required("customer")
    def post(self):
        data = request.get_json()
        try:
            application = loan_processing.submit_application(
                _current_user(),
                amount_requested=data.get("amount_requested"),
                purpose=data.get("purpose"),
                term_months=data.get("term_months"),
                repayment_frequency=data.get("repayment_frequency"),
            )
        except LoanProcessingError as exc:
            abort(exc.status_code, exc.message)
        return serialize_application(application), 201


# ------------------------------------------------------------- 2. list for review
@ns.route("/applications")
class LoanApplications(Resource):
    @ns.doc(security="Bearer", params={"status": "Filter by exact status (default: open applications only)"})
    @ns.response(200, "List of applications")
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
    @ns.expect(decision_in, validate=True)
    @ns.response(200, "Decision recorded")
    @ns.response(409, "Application already decided")
    @roles_required("loan_officer", "admin")
    def post(self, application_id: int):
        application = db.session.get(LoanApplication, application_id)
        if application is None:
            abort(404, f"Application #{application_id} not found.")

        data = request.get_json()
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
    @ns.response(200, "The authenticated customer's loans")
    @roles_required("customer")
    def get(self):
        from app.models import Loan

        rows = Loan.query.filter_by(user_id=current_user_id()).order_by(Loan.id.desc()).all()
        return {"count": len(rows), "loans": [serialize_loan(l) for l in rows]}
