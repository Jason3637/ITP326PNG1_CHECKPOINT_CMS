"""AuditLog — append-only record of who did what."""

from sqlalchemy import func

from app.extensions import db

from .base import JSONType


class AuditLog(db.Model):
    __tablename__ = "audit_logs"

    id = db.Column(db.Integer, primary_key=True)
    # Nullable: system / automated actions have no human actor.
    actor_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    action = db.Column(db.String(100), nullable=False)
    entity_type = db.Column(db.String(100), nullable=False)
    # String so it can reference any table's PK regardless of type; nullable for
    # actions not tied to a single row (e.g. "login").
    entity_id = db.Column(db.String(64))
    details = db.Column(JSONType)
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )
    ip_address = db.Column(db.String(45))  # fits IPv4 and full IPv6

    # ---------------------------------------------------------------- relationships
    actor = db.relationship(
        "User", back_populates="audit_logs", foreign_keys=[actor_id]
    )

    def __repr__(self) -> str:
        return (
            f"<AuditLog {self.id} actor={self.actor_id} "
            f"{self.action} {self.entity_type}:{self.entity_id}>"
        )
