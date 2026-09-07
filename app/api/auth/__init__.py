"""Authentication namespace.

Flow (every role, MFA mandatory):

    register ─▶ mfa/setup ─▶ mfa/verify-setup ─▶ login ─▶ mfa/verify-login ─▶ JWT
                (mfa_setup token)                (mfa_challenge token)   (access+refresh)
"""

from datetime import datetime, timezone

from flask import current_app, request
from flask_jwt_extended import get_jwt_identity, jwt_required
from flask_restx import Namespace, Resource, abort, fields

from app.extensions import db
from app.models import MfaBackupCode, User
from app.models.enums import UserRole
from app.services import audit, mfa, security

from .decorators import current_user_id, roles_required, token_scope_required
from .tokens import (
    SCOPE_ACCESS,
    issue_auth_tokens,
    mfa_challenge_token,
    mfa_setup_token,
)

ns = Namespace(
    "auth",
    description="Registration, MFA enrollment, and TOTP-gated JWT login.",
)

# --------------------------------------------------------------------- API models
register_in = ns.model(
    "RegisterInput",
    {
        "email": fields.String(required=True, example="jane@example.com"),
        "password": fields.String(required=True, min_length=8, example="correct horse battery"),
        "full_name": fields.String(required=True, example="Jane Doe"),
        "phone_number": fields.String(required=False, example="+675 7000 0000"),
    },
)
login_in = ns.model(
    "LoginInput",
    {
        "email": fields.String(required=True, example="jane@example.com"),
        "password": fields.String(required=True, example="correct horse battery"),
    },
)
setup_verify_in = ns.model(
    "MfaVerifySetupInput",
    {"code": fields.String(required=True, example="123456", description="6-digit code from the authenticator app")},
)
login_verify_in = ns.model(
    "MfaVerifyLoginInput",
    {
        "code": fields.String(required=False, example="123456", description="6-digit TOTP code"),
        "backup_code": fields.String(required=False, example="ABCDE-FGHJK", description="One-time backup code (use instead of `code`)"),
    },
)

_BEARER = {"security": "Bearer"}


def _load_user(user_id: int) -> User:
    user = db.session.get(User, user_id)
    if user is None:
        abort(404, "User not found.")
    return user


# ------------------------------------------------------------------- 1. register
@ns.route("/register")
class Register(Resource):
    @ns.expect(register_in, validate=True)
    @ns.response(201, "Registered - proceed to MFA setup")
    @ns.response(409, "Email already registered")
    def post(self):
        data = request.get_json()
        email = data["email"].strip().lower()
        password = data["password"]
        if len(password) < 8:
            abort(400, "Password must be at least 8 characters.")
        if User.query.filter_by(email=email).first():
            abort(409, "That email is already registered.")

        user = User(
            email=email,
            password_hash=security.hash_password(password),
            full_name=data["full_name"].strip(),
            phone_number=(data.get("phone_number") or None),
            role=UserRole.CUSTOMER,  # public registration is customer-only
        )
        db.session.add(user)
        db.session.flush()
        audit.record(
            "register",
            actor_id=user.id,
            entity_type="User",
            entity_id=user.id,
            details={"email": email},
            commit=False,
        )
        db.session.commit()

        return {
            "message": "Registration successful. Set up MFA to activate your account.",
            "user_id": user.id,
            "next_step": "POST /api/auth/mfa/setup with header 'Authorization: Bearer <mfa_setup_token>'",
            "mfa_setup_token": mfa_setup_token(user),
        }, 201


# ---------------------------------------------------------------- 2. mfa/setup
@ns.route("/mfa/setup")
class MfaSetup(Resource):
    @ns.doc(**_BEARER)
    @ns.response(200, "TOTP secret + QR generated")
    @token_scope_required("mfa_setup")
    def post(self):
        user = _load_user(current_user_id())
        if user.totp_enabled:
            abort(400, "MFA is already enabled for this account.")

        secret = mfa.generate_totp_secret()
        user.totp_secret = security.encrypt_secret(secret)  # stored encrypted
        audit.record(
            "mfa_setup_initiated",
            actor_id=user.id,
            entity_type="User",
            entity_id=user.id,
            commit=False,
        )
        db.session.commit()

        uri = mfa.provisioning_uri(secret, user.email)
        return {
            "message": "Scan the QR in an authenticator app, then confirm at POST /api/auth/mfa/verify-setup.",
            "totp_secret": secret,  # shown once, for manual entry
            "provisioning_uri": uri,
            "qr_code_png": mfa.qr_data_uri(uri),
            "next_step": "POST /api/auth/mfa/verify-setup with the same mfa_setup_token",
        }


# --------------------------------------------------------- 3. mfa/verify-setup
@ns.route("/mfa/verify-setup")
class MfaVerifySetup(Resource):
    @ns.doc(**_BEARER)
    @ns.expect(setup_verify_in, validate=True)
    @ns.response(200, "MFA enabled - backup codes returned once")
    @token_scope_required("mfa_setup")
    def post(self):
        user = _load_user(current_user_id())
        if user.totp_enabled:
            abort(400, "MFA is already enabled.")
        if not user.totp_secret:
            abort(400, "Call POST /api/auth/mfa/setup first.")

        code = (request.get_json() or {}).get("code", "")
        secret = security.decrypt_secret(user.totp_secret)
        if not mfa.verify_totp(secret, code):
            audit.record(
                "mfa_setup_verification_failed",
                actor_id=user.id,
                entity_type="User",
                entity_id=user.id,
            )
            abort(400, "Invalid code. Check the authenticator app and try again.")

        user.totp_enabled = True
        MfaBackupCode.query.filter_by(user_id=user.id).delete(synchronize_session=False)
        plaintext_codes = security.generate_backup_codes(
            current_app.config["BACKUP_CODE_COUNT"]
        )
        for c in plaintext_codes:
            db.session.add(
                MfaBackupCode(user_id=user.id, code_hash=security.hash_backup_code(c))
            )
        audit.record(
            "mfa_enabled",
            actor_id=user.id,
            entity_type="User",
            entity_id=user.id,
            details={"backup_codes_issued": len(plaintext_codes)},
            commit=False,
        )
        db.session.commit()

        return {
            "message": "MFA enabled. Store these backup codes now - they will not be shown again.",
            "backup_codes": plaintext_codes,
            "next_step": "POST /api/auth/login",
        }


# ------------------------------------------------------------------- 4. login
@ns.route("/login")
class Login(Resource):
    @ns.expect(login_in, validate=True)
    @ns.response(200, "Password OK - TOTP code required")
    @ns.response(401, "Invalid credentials")
    @ns.response(403, "MFA setup required / account disabled")
    def post(self):
        data = request.get_json()
        email = data["email"].strip().lower()
        user = User.query.filter_by(email=email).first()

        if user is None or not security.verify_password(data["password"], user.password_hash):
            audit.record(
                "login_failed",
                actor_id=(user.id if user else None),
                entity_type="User",
                entity_id=(user.id if user else None),
                details={"email": email, "reason": "bad_credentials"},
            )
            abort(401, "Invalid email or password.")

        if not user.is_active:
            audit.record(
                "login_denied_inactive",
                actor_id=user.id,
                entity_type="User",
                entity_id=user.id,
            )
            abort(403, "This account is disabled. Contact an administrator.")

        if not user.totp_enabled:
            audit.record(
                "login_mfa_setup_required",
                actor_id=user.id,
                entity_type="User",
                entity_id=user.id,
            )
            return {
                "message": "MFA setup is required before you can log in.",
                "mfa_required": "setup",
                "mfa_setup_token": mfa_setup_token(user),
                "next_step": "POST /api/auth/mfa/setup",
            }, 403

        audit.record(
            "login_password_verified",
            actor_id=user.id,
            entity_type="User",
            entity_id=user.id,
        )
        return {
            "message": "Password verified. Submit your TOTP code to finish logging in.",
            "mfa_required": "challenge",
            "mfa_challenge_token": mfa_challenge_token(user),
            "next_step": "POST /api/auth/mfa/verify-login",
        }


# -------------------------------------------------------- 5. mfa/verify-login
@ns.route("/mfa/verify-login")
class MfaVerifyLogin(Resource):
    @ns.doc(**_BEARER)
    @ns.expect(login_verify_in, validate=True)
    @ns.response(200, "MFA verified - access + refresh tokens issued")
    @ns.response(401, "Invalid or expired code")
    @token_scope_required("mfa_challenge")
    def post(self):
        user = _load_user(current_user_id())
        if not user.totp_enabled or not user.totp_secret:
            abort(401, "MFA is not set up for this account.")

        body = request.get_json() or {}
        code = (body.get("code") or "").strip()
        backup = (body.get("backup_code") or "").strip()

        method = None
        verified = False
        if backup:
            method = "backup_code"
            match = next(
                (
                    row
                    for row in MfaBackupCode.query.filter_by(
                        user_id=user.id, used_at=None
                    ).all()
                    if security.verify_backup_code(backup, row.code_hash)
                ),
                None,
            )
            if match is not None:
                match.used_at = datetime.now(timezone.utc)
                verified = True
        elif code:
            method = "totp"
            verified = mfa.verify_totp(
                security.decrypt_secret(user.totp_secret), code, valid_window=1
            )
        else:
            abort(400, "Provide either 'code' (TOTP) or 'backup_code'.")

        if not verified:
            audit.record(
                "mfa_login_failed",
                actor_id=user.id,
                entity_type="User",
                entity_id=user.id,
                details={"method": method},
                commit=False,
            )
            db.session.commit()
            abort(401, "Invalid or expired code.")

        tokens = issue_auth_tokens(user)
        audit.record(
            "login_success",
            actor_id=user.id,
            entity_type="User",
            entity_id=user.id,
            details={"method": method},
            commit=False,
        )
        db.session.commit()
        return {"message": "Login complete.", **tokens}


# ------------------------------------------------------------------- refresh
@ns.route("/refresh")
class Refresh(Resource):
    @ns.doc(**_BEARER)
    @ns.response(200, "New access token issued")
    @jwt_required(refresh=True)
    def post(self):
        from flask_jwt_extended import create_access_token

        user = _load_user(int(get_jwt_identity()))
        if not user.is_active:
            abort(401, "Account is disabled.")
        access = create_access_token(
            identity=str(user.id),
            additional_claims={"scope": SCOPE_ACCESS, "role": str(user.role)},
        )
        audit.record(
            "token_refreshed",
            actor_id=user.id,
            entity_type="User",
            entity_id=user.id,
        )
        return {"access_token": access, "token_type": "Bearer", "role": str(user.role)}


# ---------------------------------------------------------------------- me
@ns.route("/me")
class Me(Resource):
    @ns.doc(**_BEARER)
    @ns.response(200, "Current user (from verified claims)")
    @roles_required()  # any authenticated access token
    def get(self):
        user = _load_user(current_user_id())
        return {
            "id": user.id,
            "email": user.email,
            "full_name": user.full_name,
            "role": str(user.role),
            "is_active": user.is_active,
            "totp_enabled": user.totp_enabled,
        }
