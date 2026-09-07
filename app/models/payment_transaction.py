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
    status = db.Column(
        pg_enum(PaymentStatus, "payment_status"),
        nullable=False,
        default=PaymentStatus.PENDING,
        server_default=PaymentStatus.PENDING.value,
        index=True,
    )
    paid_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # ---------------------------------------------------------------- relationships
    loan = db.relationship("Loan", back_populates="payments")
    repayment_schedule = db.relationship(
        "RepaymentSchedule", back_populates="payments"
    )

    def __repr__(self) -> str:
        return f"<PaymentTransaction {self.id} loan={self.loan_id} {self.status}>"
