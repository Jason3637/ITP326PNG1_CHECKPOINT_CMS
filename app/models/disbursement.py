"""Disbursement — the actual money-movement event for a Loan.

A distinct entity (not just a status flag) so APPROVED, AWAITING_DISBURSEMENT
and ACTIVE are genuinely separable in the data: an application can sit
approved-and-awaiting-disbursement for a while before an admin actually
executes and records the disbursement here.
"""

from sqlalchemy import func

from app.extensions import db

from .base import pg_enum
from .enums import DisbursementMethod


class Disbursement(db.Model):
    __tablename__ = "disbursements"

    id = db.Column(db.Integer, primary_key=True)
    # One disbursement per application, enforced by the database: a second
    # insert for the same application fails with a unique violation even if
    # two requests race past every application-level check.
    application_id = db.Column(
        db.Integer,
        db.ForeignKey("loan_applications.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    loan_id = db.Column(
        db.Integer,
        db.ForeignKey("loans.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    method = db.Column(
        pg_enum(DisbursementMethod, "disbursement_method"), nullable=False
    )
    amount = db.Column(db.Numeric(12, 2), nullable=False)
    # BSP mobile banking transaction reference, cash voucher number, etc.
    method_reference = db.Column(db.String(255))
    disbursed_at = db.Column(
        db.DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    recorded_by = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    note = db.Column(db.String(500))
    # When the row was written (disbursed_at is when the money moved).
    recorded_at = db.Column(
        db.DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # BSP only: the destination with all but the last 4 characters hidden.
    destination_masked = db.Column(db.String(64))
    # Receipt / acknowledgement file, stored like every other document.
    evidence_document_id = db.Column(
        db.Integer, db.ForeignKey("documents.id", ondelete="RESTRICT")
    )

    # ---------------------------------------------------------------- relationships
    loan = db.relationship("Loan", back_populates="disbursement")
    recorded_by_admin = db.relationship("User", foreign_keys=[recorded_by])

    def __repr__(self) -> str:
        return f"<Disbursement loan={self.loan_id} {self.method}>"
