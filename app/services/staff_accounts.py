"""Staff account administration: an admin creates loan_officer / admin
accounts and resets their passwords (POST /api/admin/staff...).

Customers are never created here - public registration stays the only way
to get a `customer` account. Every account made here starts exactly like a
fresh registration or a scripts/seed_staff.py account: `totp_enabled=False`,
so the first login answers `mfa_required: "setup"` and the person enrols
TOTP themselves through the normal public flow.

Temporary passwords are generated here, returned to the caller ONCE, and
only their hash is stored. They are never written to the audit log, any
other row, or a log line.
"""

import secrets

from app.extensions import db
from app.models import User
from app.models.enums import UserRole

from . import audit, security
from .errors import ServiceError

STAFF_ROLES = (UserRole.LOAN_OFFICER, UserRole.ADMIN)


def generate_temp_password() -> str:
    # url-safe, 20 chars - well over the 8-char minimum. Handed off once and
    # always paired with mandatory MFA (same as scripts/seed_staff.py).
    return secrets.token_urlsafe(15)


def _require_admin(actor: User) -> None:
    if actor is None or actor.role != UserRole.ADMIN:
        raise ServiceError("Only an administrator can manage staff accounts.", 403)


def _parse_staff_role(role) -> UserRole:
    try:
        parsed = UserRole(role)
    except ValueError:
        parsed = None
    if parsed not in STAFF_ROLES:
        allowed = ", ".join(r.value for r in STAFF_ROLES)
        raise ServiceError(
            f"role must be one of: {allowed}. Customer accounts are created through "
            "public registration only."
        )
    return parsed


def create_staff_account(
    admin: User,
    *,
    email,
    full_name,
    role,
    phone_number=None,
    is_active=True,
) -> tuple[User, str]:
    """Admin only. Returns (user, temporary_password) - the password is not
    stored anywhere in plaintext; the caller shows it once."""
    _require_admin(admin)
    email = (email or "").strip().lower()
    if len(email) < 3 or "@" not in email or len(email) > 255:
        raise ServiceError("email must be a valid email address.")
    full_name = (full_name or "").strip()
    if not full_name or len(full_name) > 255:
        raise ServiceError("full_name is required (max 255 characters).")
    phone_number = (phone_number or "").strip() or None
    if phone_number and len(phone_number) > 32:
        raise ServiceError("phone_number must be at most 32 characters.")
    if not isinstance(is_active, bool):
        raise ServiceError("is_active must be true or false.")
    staff_role = _parse_staff_role(role)
    if User.query.filter_by(email=email).first():
        raise ServiceError("That email is already registered.", 409)

    temp_password = generate_temp_password()
    user = User(
        email=email,
        password_hash=security.hash_password(temp_password),
        full_name=full_name,
        phone_number=phone_number,
        role=staff_role,
        is_active=is_active,
        totp_enabled=False,  # first login -> mfa_required: "setup"
    )
    db.session.add(user)
    db.session.flush()
    audit.record(
        "staff_account_created",
        actor_id=admin.id,
        entity_type="User",
        entity_id=user.id,
        details={"email": email, "role": staff_role.value, "is_active": is_active},
        commit=False,
    )
    db.session.commit()
    return user, temp_password


def reset_staff_password(admin: User, user_id: int) -> tuple[User, str]:
    """Admin only. Replaces a staff account's password with a new temporary
    one - the old password stops working immediately. MFA enrolment (TOTP
    secret, backup codes) is deliberately left as it is: a forgotten
    password isn't a lost authenticator, and keeping MFA means the
    temporary password alone still can't sign anyone in.
    """
    _require_admin(admin)
    user = db.session.get(User, user_id)
    if user is None:
        raise ServiceError("User not found.", 404)
    if user.role not in STAFF_ROLES:
        raise ServiceError("Only staff (loan_officer / admin) passwords can be reset here.")

    temp_password = generate_temp_password()
    user.password_hash = security.hash_password(temp_password)
    audit.record(
        "staff_password_reset",
        actor_id=admin.id,
        entity_type="User",
        entity_id=user.id,
        details={
            "email": user.email,
            "role": user.role.value,
            "mfa_enrolment_kept": bool(user.totp_enabled),
        },
        commit=False,
    )
    db.session.commit()
    return user, temp_password


def serialize_staff(user: User) -> dict:
    return {
        "id": user.id,
        "email": user.email,
        "full_name": user.full_name,
        "phone_number": user.phone_number,
        "role": str(user.role),
        "is_active": user.is_active,
        "totp_enabled": user.totp_enabled,
        "created_at": user.created_at.isoformat() if user.created_at else None,
    }
