"""Reusable auth guards built on Flask-JWT-Extended claim verification.

    @roles_required()                      -> any authenticated user (access token)
    @roles_required('loan_officer', 'admin') -> access token AND role in the set
    @token_scope_required('mfa_setup')     -> only the MFA-setup step token

This is the "Claims Verified per request" box from the diagram.
"""

from functools import wraps

from flask_jwt_extended import get_jwt, get_jwt_identity, verify_jwt_in_request
from flask_restx import abort

from .tokens import SCOPE_ACCESS


def token_scope_required(expected_scope: str):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            verify_jwt_in_request()
            scope = get_jwt().get("scope")
            if scope != expected_scope:
                abort(
                    401,
                    f"This endpoint requires a '{expected_scope}' token "
                    f"(received '{scope or 'none'}').",
                )
            return fn(*args, **kwargs)

        return wrapper

    return decorator


def roles_required(*roles: str):
    """Require a full access token; if roles are given, the claim must match one."""

    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            verify_jwt_in_request()
            claims = get_jwt()
            if claims.get("scope") != SCOPE_ACCESS:
                abort(401, "A full access token is required for this endpoint.")
            if roles and claims.get("role") not in roles:
                abort(403, f"Requires role: {' or '.join(roles)}.")
            return fn(*args, **kwargs)

        return wrapper

    return decorator


def current_user_id() -> int:
    return int(get_jwt_identity())
