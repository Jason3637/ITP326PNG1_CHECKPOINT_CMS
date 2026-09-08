"""Admin namespace - system parameter management (admin's "edit parameters")."""

from flask import request
from flask_restx import Namespace, Resource, abort, fields

from app.api.auth.decorators import current_user_id, roles_required
from app.services import parameters
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
