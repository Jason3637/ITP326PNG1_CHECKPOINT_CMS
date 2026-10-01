"""VerificationItem — one line of a Loan Officer's per-application checklist
(age 18+, valid ID, contact details, employment, referee, proof of income,
repayment history, application consistency).

``item_type`` is a plain string, not a Postgres enum, on purpose: the set of
checklist items is defined by a registry in the service layer, so adding a
new item type later is a code change only - no migration, no enum rebuild.

Rows hold the CURRENT state of each check (one per application + item type);
every change is audit-logged, and the checklist is frozen into
OfficerRecommendation.checklist_snapshot whenever an officer recommends, so
the state an officer actually signed off on is never lost.
"""

from sqlalchemy import func

from app.extensions import db

from .base import pg_enum
from .enums import VerificationItemStatus


class VerificationItem(db.Model):
    __tablename__ = "verification_items"
    __table_args__ = (
        db.UniqueConstraint(
            "loan_application_id", "item_type", name="uq_verification_items_application_item"
        ),
        # A checked item always says who checked it and when; a pending one never does.
        db.CheckConstraint(
            "(status = 'pending') = (checked_by IS NULL AND checked_at IS NULL)",
            name="ck_verification_items_checked_fields",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    loan_application_id = db.Column(
        db.Integer,
        db.ForeignKey("loan_applications.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    item_type = db.Column(db.String(50), nullable=False)
    status = db.Column(
        pg_enum(VerificationItemStatus, "verification_item_status"),
        nullable=False,
        default=VerificationItemStatus.PENDING,
        server_default=VerificationItemStatus.PENDING.value,
    )
    note = db.Column(db.String(1000))
    # NO ACTION (not SET NULL): a checked item must keep naming its checker.
    checked_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    checked_at = db.Column(db.DateTime(timezone=True))
    # Set when the item was satisfied by carrying over a still-valid
    # customer-level verification instead of re-checking from scratch.
    customer_verification_id = db.Column(
        db.Integer, db.ForeignKey("customer_verifications.id")
    )
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # ---------------------------------------------------------------- relationships
    loan_application = db.relationship(
        "LoanApplication", back_populates="verification_items"
    )
    checker = db.relationship("User", foreign_keys=[checked_by])
    customer_verification = db.relationship("CustomerVerification")

    def __repr__(self) -> str:
        return (
            f"<VerificationItem application={self.loan_application_id} "
            f"{self.item_type} {self.status}>"
        )
