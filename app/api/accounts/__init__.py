"""Accounts namespace - customer dashboard / account tracking."""

from flask_restx import Namespace, Resource, fields

from app.api.auth.decorators import current_user_id, roles_required
from app.services import accounts

ns = Namespace("accounts", description="Customer account tracking and dashboard.")

account_summary_out = ns.model(
    "AccountSummary",
    {
        "user_id": fields.Integer,
        "counts": fields.Raw(description="{active, completed, defaulted, total}"),
        "active_loans": fields.List(
            fields.Raw, description="per-loan progress objects"
        ),
        "next_repayment_due": fields.Raw(
            description="earliest unpaid installment across active loans, or null"
        ),
        "has_overdue": fields.Boolean,
    },
)


@ns.route("/summary")
class AccountSummary(Resource):
    @ns.doc(security="Bearer")
    @ns.response(200, "Active loans, next repayment due, and repayment progress", account_summary_out)
    @roles_required("customer")
    def get(self):
        return accounts.get_account_summary(current_user_id())
