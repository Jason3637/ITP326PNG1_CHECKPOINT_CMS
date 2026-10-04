"""JWT minting for each step of the auth flow.

Three token "scopes" are issued, distinguished by a custom ``scope`` claim:

    mfa_setup      short-lived, only unlocks /mfa/setup + /mfa/verify-setup
    mfa_challenge  short-lived, only unlocks /mfa/verify-login
    access         the real access token (carries ``role``); paired with a refresh token

Every token also carries ``tv``, the user's ``token_version`` when it was
issued. A token whose ``tv`` no longer matches the user's current version is
rejected as revoked (401) - that's how a password reset signs a user out
everywhere. Tokens issued before ``tv`` existed count as version 0.
"""

from flask import current_app
from flask_jwt_extended import create_access_token, create_refresh_token

from app.extensions import db, jwt

SCOPE_MFA_SETUP = "mfa_setup"
SCOPE_MFA_CHALLENGE = "mfa_challenge"
SCOPE_ACCESS = "access"
TOKEN_VERSION_CLAIM = "tv"


def _identity(user) -> str:
    # flask-jwt-extended expects a string identity (JWT "sub").
    return str(user.id)


def version_claims(user, **claims) -> dict:
    return {**claims, TOKEN_VERSION_CLAIM: user.token_version or 0}


@jwt.token_in_blocklist_loader
def _token_revoked(_jwt_header, jwt_payload) -> bool:
    from app.models import User

    try:
        user = db.session.get(User, int(jwt_payload["sub"]))
    except (KeyError, TypeError, ValueError):
        return True
    if user is None:
        return False  # unchanged: endpoints answer a missing user themselves
    return jwt_payload.get(TOKEN_VERSION_CLAIM, 0) != (user.token_version or 0)


def mfa_setup_token(user) -> str:
    return create_access_token(
        identity=_identity(user),
        additional_claims=version_claims(user, scope=SCOPE_MFA_SETUP),
        expires_delta=current_app.config["MFA_SETUP_TOKEN_EXPIRES"],
    )


def mfa_challenge_token(user) -> str:
    return create_access_token(
        identity=_identity(user),
        additional_claims=version_claims(user, scope=SCOPE_MFA_CHALLENGE),
        expires_delta=current_app.config["MFA_CHALLENGE_TOKEN_EXPIRES"],
    )


def issue_auth_tokens(user) -> dict:
    """The real login result: access token (with role claim) + refresh token."""
    role = str(user.role)
    access = create_access_token(
        identity=_identity(user),
        additional_claims=version_claims(user, scope=SCOPE_ACCESS, role=role),
    )
    refresh = create_refresh_token(
        identity=_identity(user),
        additional_claims=version_claims(user, role=role),
    )
    return {
        "access_token": access,
        "refresh_token": refresh,
        "token_type": "Bearer",
        "role": role,
    }
