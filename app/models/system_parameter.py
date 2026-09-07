"""SystemParameter - admin-editable runtime tunables (interest rate, limits).

Seed defaults live in ``app/config.py``; a row here overrides the matching
config value at runtime. Read through ``app.services.parameters``.
"""

from sqlalchemy import func

from app.extensions import db


class SystemParameter(db.Model):
    __tablename__ = "system_parameters"

    key = db.Column(db.String(64), primary_key=True)
    # Stored as a string; coerced to Decimal/int/str by the parameters service.
    value = db.Column(db.String(255), nullable=False)
    description = db.Column(db.String(255))
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    updated_by = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    updated_by_user = db.relationship("User", foreign_keys=[updated_by])

    def __repr__(self) -> str:
        return f"<SystemParameter {self.key}={self.value!r}>"
