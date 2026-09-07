"""Flask-RESTX API assembly.

A single :class:`~flask_restx.Api` is mounted at ``/api`` with Swagger UI
served at ``/api/docs``. Each route group from the architecture diagram gets
its own Namespace.
"""

from flask import Blueprint
from flask_restx import Api

from .accounts import ns as accounts_ns
from .admin import ns as admin_ns
from .auth import ns as auth_ns
from .loans import ns as loans_ns
from .payments import ns as payments_ns
from .reports import ns as reports_ns
from .users import ns as users_ns

api_bp = Blueprint("api", __name__, url_prefix="/api")

authorizations = {
    "Bearer": {
        "type": "apiKey",
        "in": "header",
        "name": "Authorization",
        "description": "Type 'Bearer <access_token>' from POST /api/auth/mfa/verify-login.",
    }
}

api = Api(
    api_bp,
    version="0.1.0",
    title="Prime's Vault API",
    description="Digital cooperative lending platform - backend API.",
    doc="/docs",
    authorizations=authorizations,
)

api.add_namespace(auth_ns)
api.add_namespace(users_ns)
api.add_namespace(accounts_ns)
api.add_namespace(loans_ns)
api.add_namespace(payments_ns)
api.add_namespace(reports_ns)
api.add_namespace(admin_ns)


# Make JWT failures (missing/expired/invalid token, wrong scope) render as clean
# JSON 401s through Flask-RESTX instead of bubbling up as 500s.
from flask_jwt_extended.exceptions import JWTExtendedException  # noqa: E402
from jwt.exceptions import PyJWTError  # noqa: E402


@api.errorhandler(JWTExtendedException)
def _handle_jwt_extended(error):
    return {"message": str(error) or "Authorization required."}, 401


@api.errorhandler(PyJWTError)
def _handle_pyjwt(error):
    return {"message": f"Invalid token: {error}"}, 401
