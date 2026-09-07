"""Payments namespace - record repayments against installments."""

from flask import request
from flask_restx import Namespace, Resource, abort, fields

from app.api.auth.decorators import current_user_id, roles_required
from app.extensions import db
from app.models import User
from app.services import payment_processing
from app.services.errors import ServiceError

ns = Namespace("payments", description="Loan repayments.")

repay_in = ns.model(
    "RepayInput",
    {
        "repayment_schedule_id": fields.Integer(required=True, example=1),
        "amount": fields.Float(required=True, example=458.40),
        "payment_method": fields.String(required=True, example="cash"),
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
    @ns.expect(repay_in, validate=True)
    @ns.response(201, "Payment recorded")
    @ns.response(403, "Not your loan")
    @ns.response(409, "Installment already paid / loan not active")
    @roles_required("customer", "loan_officer", "admin")
    def post(self):
        data = request.get_json()
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


@ns.route("/loan/<int:loan_id>")
class LoanPayments(Resource):
    @ns.doc(security="Bearer")
    @ns.response(200, "Payments recorded against this loan")
    @roles_required("customer", "loan_officer", "admin")
    def get(self, loan_id: int):
        try:
            payments = payment_processing.list_payments(loan_id, _current_user())
        except ServiceError as exc:
            abort(exc.status_code, exc.message)
        return {"loan_id": loan_id, "count": len(payments), "payments": payments}
