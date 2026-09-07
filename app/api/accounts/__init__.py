"""Accounts namespace - customer dashboard / account tracking."""

from flask_restx import Namespace, Resource

from app.api.auth.decorators import current_user_id, roles_required
from app.services import accounts

ns = Namespace("accounts", description="Customer account tracking and dashboard.")


@ns.route("/summary")
class AccountSummary(Resource):
    @ns.doc(security="Bearer")
    @ns.response(200, "Active loans, next repayment due, and repayment progress")
    @roles_required("customer")
    def get(self):
        return accounts.get_account_summary(current_user_id())
