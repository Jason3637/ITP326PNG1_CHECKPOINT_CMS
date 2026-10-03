"""Admin namespace - system parameter management (admin's "edit parameters")."""

from flask import request
from flask_restx import Namespace, Resource, abort, fields

from app.api.auth.decorators import current_user_id, roles_required
from flask import current_app

from app.extensions import db
from app.models import User
from app.services import loan_processing, parameters
from app.services.errors import ServiceError

ns = Namespace("admin", description="Administrator configuration.")

parameters_in = ns.model(
    "SystemParametersInput",
    {
        "default_annual_interest_rate": fields.Float(required=False, example=0.18),
        "min_loan_amount": fields.Float(required=False, example=100),
        "max_loan_amount": fields.Float(required=False, example=50000),
        "min_loan_term_months": fields.Integer(required=False, example=1),
        "max_loan_term_months": fields.Integer(required=False, example=60),
        "min_monthly_income": fields.Float(
            required=False, example=200, description="Credit evaluation (interim model)."
        ),
        "max_debt_to_income_ratio": fields.Float(
            required=False, example=0.40, description="Credit evaluation (interim model)."
        ),
        "customer_verification_validity_months": fields.Integer(
            required=False,
            example=12,
            description="How long a customer verification stays valid (capped at the ID's expiry). "
            "12 is an engineering default awaiting client confirmation.",
        ),
    },
)

error_out = ns.model("ErrorResponse", {"message": fields.String})
parameters_out = ns.model(
    "SystemParameters",
    {
        "parameters": fields.Raw(
            description="{param_key: {value, type, description, source (default|override), updated_at, updated_by}}"
        )
    },
)


@ns.route("/parameters")
class Parameters(Resource):
    @ns.doc(security="Bearer")
    @ns.response(200, "Current effective parameters (default or admin override)", parameters_out)
    @roles_required("admin")
    def get(self):
        return {"parameters": parameters.get_effective()}

    @ns.doc(security="Bearer")
    @ns.expect(parameters_in, validate=False)
    @ns.response(200, "Updated - returns the new effective parameters", parameters_out)
    @ns.response(400, "Unknown parameter or invalid value", error_out)
    @roles_required("admin")
    def put(self):
        payload = request.get_json(silent=True) or {}
        try:
            updated = parameters.update(payload, actor_id=current_user_id())
        except ServiceError as exc:
            abort(exc.status_code, exc.message)
        return {"parameters": updated}

invalidated_out = ns.model(
    "InvalidatedVerifications",
    {
        "invalidated": fields.Integer(description="How many verifications were invalidated (policy_updated)."),
        "policy_version": fields.String(description="The current CUSTOMER_VERIFICATION_POLICY_VERSION."),
    },
)


@ns.route("/customer-verifications/invalidate-outdated")
class InvalidateOutdatedVerifications(Resource):
    @ns.doc(security="Bearer")
    @ns.response(
        200,
        "Every customer verification made under an older policy version is invalidated (policy_updated)",
        invalidated_out,
    )
    @roles_required("admin")
    def post(self):
        admin = db.session.get(User, current_user_id())
        try:
            count = loan_processing.invalidate_outdated_customer_verifications(admin)
        except ServiceError as exc:
            abort(exc.status_code, exc.message)
        return {
            "invalidated": count,
            "policy_version": current_app.config["CUSTOMER_VERIFICATION_POLICY_VERSION"],
        }
