"""Loan — an approved, disbursed application with repayment terms."""

from app.extensions import db

from .base import pg_enum
from .enums import LoanStatus


class Loan(db.Model):
    __tablename__ = "loans"

    id = db.Column(db.Integer, primary_key=True)
    # One loan per application.
    application_id = db.Column(
        db.Integer,
        db.ForeignKey("loan_applications.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    principal_amount = db.Column(db.Numeric(12, 2), nullable=False)
    # Annual rate as a fraction, e.g. 0.1750 = 17.5%.
    interest_rate = db.Column(db.Numeric(6, 4), nullable=False)
    term_months = db.Column(db.Integer, nullable=False)
    monthly_payment = db.Column(db.Numeric(12, 2), nullable=False)
    total_repayable = db.Column(db.Numeric(12, 2), nullable=False)
    status = db.Column(
        pg_enum(LoanStatus, "loan_status"),
        nullable=False,
        default=LoanStatus.ACTIVE,
        server_default=LoanStatus.ACTIVE.value,
        index=True,
    )
    disbursed_at = db.Column(db.DateTime(timezone=True))

    # ---------------------------------------------------------------- relationships
    application = db.relationship("LoanApplication", back_populates="loan")
    borrower = db.relationship("User", back_populates="loans")
    repayment_schedule = db.relationship(
        "RepaymentSchedule",
        back_populates="loan",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="RepaymentSchedule.installment_number",
    )
    payments = db.relationship(
        "PaymentTransaction",
        back_populates="loan",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def __repr__(self) -> str:
        return f"<Loan {self.id} user={self.user_id} {self.status}>"
