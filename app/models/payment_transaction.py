"""PaymentTransaction — an actual payment attempt against a loan."""

from sqlalchemy import func

from app.extensions import db

from .base import pg_enum
from .enums import PaymentStatus


class PaymentTransaction(db.Model):
    __tablename__ = "payment_transactions"

    id = db.Column(db.Integer, primary_key=True)
    loan_id = db.Column(
        db.Integer,
        db.ForeignKey("loans.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Nullable: an ad-hoc / early payment may not map to a single installment.
    repayment_schedule_id = db.Column(
        db.Integer,
        db.ForeignKey("repayment_schedules.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    amount = db.Column(db.Numeric(12, 2), nullable=False)
    # Free-form for now (e.g. "cash", "bank_transfer", "mobile_money:mpesa").
    # Tighten to an enum once the client confirms accepted channels.
    payment_method = db.Column(db.String(50), nullable=False)
    # The date the customer says they paid (may be earlier than reported_at,
    # e.g. reporting a payment made a few days ago) - distinct from
    # reported_at, which is when the system recorded the report.
    payment_date = db.Column(db.Date, nullable=False)
    # Bank transfer / mobile money / cheque reference, where applicable
    # (e.g. not meaningful for a cash-in-hand payment).
    reference_number = db.Column(db.String(100))
    status = db.Column(
        pg_enum(PaymentStatus, "payment_status"),
        nullable=False,
        default=PaymentStatus.REPORTED,
        server_default=PaymentStatus.REPORTED.value,
        index=True,
    )
    # Set only when status becomes REJECTED - why, for the customer/officer
    # to see without needing admin-only AuditLog access. Mirrors
    # LoanApplication.action_required_note's "surface it beyond the audit
    # trail" pattern.
    rejection_reason = db.Column(db.String(500))
    # When the customer/staff reported this payment (set at creation, always).
    reported_at = db.Column(
        db.DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # When staff VERIFIED it (the ledger-affecting moment) - null until then.
    # Was previously set at creation; see payment_processing.verify_payment().
    paid_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # ---------------------------------------------------------------- relationships
    loan = db.relationship("Loan", back_populates="payments")
    repayment_schedule = db.relationship(
        "RepaymentSchedule", back_populates="payments"
    )
    # Receipt/screenshot uploads - same Document/Supabase Storage pattern as
    # loan application documents (see app/services/documents.py).
    documents = db.relationship(
        "Document", back_populates="payment_transaction"
    )

    def __repr__(self) -> str:
        return f"<PaymentTransaction {self.id} loan={self.loan_id} {self.status}>"
