"""Members Registry - read access to member (User) records."""

from app.extensions import db
from app.models import User


def get_profile(user_id: int) -> dict | None:
    """Public-facing profile for the given user, or None if not found."""
    user = db.session.get(User, user_id)
    if user is None:
        return None
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
