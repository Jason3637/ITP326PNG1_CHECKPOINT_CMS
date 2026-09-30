"""Payments namespace - report a repayment, then an admin verifies it before
the ledger moves (see app/services/payment_processing.py). A loan_officer
may claim a transaction for review (start-verification) but only admin makes
the actual verify/reject call.
"""

from flask import request
from flask_restx import Namespace, Resource, abort, fields

from app.api.auth.decorators import current_user_id, roles_required
from app.extensions import db
from app.models import User
from app.services import payment_processing
from app.services.errors import ServiceError

ns = Namespace("payments", description="Loan repayments: report, then staff verify.")

repay_in = ns.model(
    "RepayInput",
    {
        "repayment_schedule_id": fields.Integer(required=True, example=1),
        "amount": fields.Float(required=True, example=700.00),
        "payment_method": fields.String(required=True, example="cash"),
    },
)
verify_in = ns.model(
    "VerifyPaymentInput",
    {
        "decision": fields.String(required=True, enum=["verified", "rejected"], example="verified"),
        "note": fields.String(
            required=False, description="Required (as the reason) when decision is 'rejected'."
        ),
    },
)

error_out = ns.model("ErrorResponse", {"message": fields.String})
payment_txn_out = ns.model(
    "PaymentTransaction",
    {
        "id": fields.Integer,
        "loan_id": fields.Integer,
        "repayment_schedule_id": fields.Integer,
        "amount": fields.Float,
        "payment_method": fields.String,
        "status": fields.String(example="reported"),
        "reported_at": fields.String,
        "paid_at": fields.String(description="Set only once VERIFIED - the settlement moment."),
    },
)
report_result_out = ns.model(
    "ReportPaymentResult",
    {"transaction": fields.Nested(payment_txn_out)},
)
verify_result_out = ns.model(
    "VerifyPaymentResult",
    {
        "transaction": fields.Nested(payment_txn_out),
        "installment": fields.Raw(
            description="{installment_number, amount_due, amount_paid, shortfall, overpaid, status} - present only when decision=verified"
        ),
        "loan_status": fields.String,
        "loan_completed": fields.Boolean,
    },
)
payment_list_out = ns.model(
    "LoanPaymentList",
    {
        "loan_id": fields.Integer,
        "count": fields.Integer,
        "payments": fields.List(fields.Nested(payment_txn_out)),
    },
)


def _current_user() -> User:
    user = db.session.get(User, current_user_id())
    if user is None:
        abort(404, "User not found.")
    return user


@ns.route("/repay")
class Repay(Resource):
    @ns.doc(security="Bearer")
    @ns.expect(repay_in)
    @ns.response(201, "Payment reported (not yet verified - the ledger is untouched)", report_result_out)
    @ns.response(403, "Not your loan", error_out)
    @ns.response(409, "Installment already paid / loan not active", error_out)
    @roles_required("customer", "loan_officer", "admin")
    def post(self):
        data = request.get_json(silent=True) or {}
        try:
            result = payment_processing.record_payment(
                _current_user(),
                repayment_schedule_id=data.get("repayment_schedule_id"),
                amount=data.get("amount"),
                payment_method=data.get("payment_method"),
            )
        except ServiceError as exc:
            abort(exc.status_code, exc.message)
        return result, 201


@ns.route("/<int:transaction_id>/start-verification")
class StartPaymentVerification(Resource):
    @ns.doc(security="Bearer")
    @ns.response(200, "REPORTED -> VERIFICATION_PENDING", report_result_out)
    @ns.response(409, "Transaction is not in REPORTED", error_out)
    @roles_required("loan_officer", "admin")
    def post(self, transaction_id: int):
        try:
            result = payment_processing.start_payment_verification(_current_user(), transaction_id)
        except ServiceError as exc:
            abort(exc.status_code, exc.message)
        return result


@ns.route("/<int:transaction_id>/verify")
class VerifyPayment(Resource):
    @ns.doc(security="Bearer")
    @ns.expect(verify_in)
    @ns.response(200, "The ledger only moves here, and only for decision=verified", verify_result_out)
    @ns.response(400, "note (a reason) is required when rejecting", error_out)
    @ns.response(403, "Only an admin may verify or reject a payment", error_out)
    @ns.response(409, "Transaction is not verifiable from its current status", error_out)
    @roles_required("admin")
    def post(self, transaction_id: int):
        data = request.get_json(silent=True) or {}
        decision = (data.get("decision") or "").strip().lower()
        if decision not in {"verified", "rejected"}:
            abort(400, "decision must be 'verified' or 'rejected'.")
        try:
            result = payment_processing.verify_payment(
                _current_user(),
                transaction_id,
                decision=decision,
                note=data.get("note"),
            )
        except ServiceError as exc:
            abort(exc.status_code, exc.message)
        return result


@ns.route("/loan/<int:loan_id>")
class LoanPayments(Resource):
    @ns.doc(security="Bearer")
    @ns.response(200, "Payments reported/verified against this loan", payment_list_out)
    @roles_required("customer", "loan_officer", "admin")
    def get(self, loan_id: int):
        try:
            payments = payment_processing.list_payments(loan_id, _current_user())
        except ServiceError as exc:
            abort(exc.status_code, exc.message)
        return {"loan_id": loan_id, "count": len(payments), "payments": payments}
