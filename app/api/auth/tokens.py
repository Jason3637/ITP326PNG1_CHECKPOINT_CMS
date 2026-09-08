"""JWT minting for each step of the auth flow.

Three token "scopes" are issued, distinguished by a custom ``scope`` claim:

    mfa_setup      short-lived, only unlocks /mfa/setup + /mfa/verify-setup
    mfa_challenge  short-lived, only unlocks /mfa/verify-login
    access         the real access token (carries ``role``); paired with a refresh token
"""

from flask import current_app
from flask_jwt_extended import create_access_token, create_refresh_token

SCOPE_MFA_SETUP = "mfa_setup"
SCOPE_MFA_CHALLENGE = "mfa_challenge"
SCOPE_ACCESS = "access"


def _identity(user) -> str:
    # flask-jwt-extended expects a string identity (JWT "sub").
    return str(user.id)


def mfa_setup_token(user) -> str:
    return create_access_token(
        identity=_identity(user),
        additional_claims={"scope": SCOPE_MFA_SETUP},
        expires_delta=current_app.config["MFA_SETUP_TOKEN_EXPIRES"],
    )


def mfa_challenge_token(user) -> str:
    return create_access_token(
        identity=_identity(user),
        additional_claims={"scope": SCOPE_MFA_CHALLENGE},
        expires_delta=current_app.config["MFA_CHALLENGE_TOKEN_EXPIRES"],
    )


def issue_auth_tokens(user) -> dict:
    """The real login result: access token (with role claim) + refresh token."""
    role = str(user.role)
    access = create_access_token(
        identity=_identity(user),
        additional_claims={"scope": SCOPE_ACCESS, "role": role},
    )
    refresh = create_refresh_token(
        identity=_identity(user),
        additional_claims={"role": role},
    )
    return {
        "access_token": access,
        "refresh_token": refresh,
        "token_type": "Bearer",
        "role": role,
    }
