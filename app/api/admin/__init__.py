"""Admin namespace - system parameter management (admin's "edit parameters")
and staff account administration (create staff, reset a staff password)."""

from flask import request
from flask_restx import Namespace, Resource, abort, fields

from app.api.auth.decorators import current_user_id, roles_required
from flask import current_app

from app.extensions import db
from app.models import User
from app.services import loan_processing, parameters, staff_accounts
from app.services.errors import ServiceError

ns = Namespace("admin", description="Administrator configuration.")

parameters_in = ns.model(
    "SystemParametersInput",
    {
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


# ------------------------------------------------------------ staff accounts
# Responses carrying a temporary password must never be cached by a browser
# or proxy.
_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}

staff_create_in = ns.model(
    "StaffAccountInput",
    {
        "email": fields.String(required=True, example="jane.officer@primesvault.pg"),
        "full_name": fields.String(required=True, example="Jane Officer"),
        "role": fields.String(
            required=True,
            enum=[r.value for r in staff_accounts.STAFF_ROLES],
            example="loan_officer",
            description="loan_officer or admin. 'customer' is rejected - customers register publicly.",
        ),
        "phone_number": fields.String(required=False, example="+675 7123 4567"),
        "is_active": fields.Boolean(required=False, default=True),
    },
)
staff_out = ns.model(
    "StaffAccount",
    {
        "id": fields.Integer,
        "email": fields.String,
        "full_name": fields.String,
        "phone_number": fields.String,
        "role": fields.String,
        "is_active": fields.Boolean,
        "totp_enabled": fields.Boolean(description="false until they finish MFA setup on first login"),
        "created_at": fields.String,
    },
)
staff_credentials_out = ns.model(
    "StaffAccountWithTemporaryPassword",
    {
        "user": fields.Nested(staff_out),
        "temporary_password": fields.String(
            description="Shown ONCE, in this response only - not stored in plaintext, not logged, "
            "never returned by any other endpoint. Hand it over privately."
        ),
        "next_step": fields.String,
    },
)

_FIRST_LOGIN = (
    "Give the temporary password to the staff member privately. They sign in with "
    "POST /api/auth/login; until MFA is set up that answers mfa_required='setup' with an "
    "mfa_setup_token for /api/auth/mfa/setup and /api/auth/mfa/verify-setup."
)


def _admin() -> User:
    return db.session.get(User, current_user_id())


@ns.route("/staff")
class StaffAccounts(Resource):
    @ns.doc(
        security="Bearer",
        description="Create a loan_officer or admin account with a generated temporary password. "
        "The account must complete MFA setup on first login. Audited as staff_account_created "
        "(the password is never logged).",
    )
    @ns.expect(staff_create_in, validate=False)
    @ns.response(201, "Created - the temporary password is in this response only", staff_credentials_out)
    @ns.response(400, "Invalid field, or a non-staff role such as 'customer'", error_out)
    @ns.response(409, "Email already registered", error_out)
    @roles_required("admin")
    def post(self):
        data = request.get_json(silent=True) or {}
        try:
            user, temp_password = staff_accounts.create_staff_account(
                _admin(),
                email=data.get("email"),
                full_name=data.get("full_name"),
                role=data.get("role"),
                phone_number=data.get("phone_number"),
                is_active=data.get("is_active", True),
            )
        except ServiceError as exc:
            abort(exc.status_code, exc.message)
        return (
            {
                "user": staff_accounts.serialize_staff(user),
                "temporary_password": temp_password,
                "next_step": _FIRST_LOGIN,
            },
            201,
            _NO_STORE,
        )


@ns.route("/staff/<int:user_id>/reset-password")
class StaffPasswordReset(Resource):
    @ns.doc(
        security="Bearer",
        description="Replace a staff account's password with a new temporary one; the old password "
        "stops working immediately and every existing session is signed out (all tokens issued "
        "before the reset are rejected). MFA enrolment is kept - they still sign in with their "
        "authenticator. Audited as staff_password_reset (the password is never logged).",
    )
    @ns.response(200, "Reset - the temporary password is in this response only", staff_credentials_out)
    @ns.response(400, "Not a staff account", error_out)
    @ns.response(404, "User not found", error_out)
    @roles_required("admin")
    def post(self, user_id: int):
        try:
            user, temp_password = staff_accounts.reset_staff_password(_admin(), user_id)
        except ServiceError as exc:
            abort(exc.status_code, exc.message)
        next_step = (
            "Give the temporary password to the staff member privately. Their MFA enrolment is "
            "unchanged: they sign in with the new password and their authenticator code."
            if user.totp_enabled
            else _FIRST_LOGIN
        )
        return (
            {
                "user": staff_accounts.serialize_staff(user),
                "temporary_password": temp_password,
                "next_step": next_step,
            },
            200,
            _NO_STORE,
        )


# Administrator operations (queues, decisions, disbursement, loans, repayments,
# pricing, analytics) - registered on this namespace.
from . import operations  # noqa: E402,F401
