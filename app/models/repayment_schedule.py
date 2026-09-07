"""RepaymentSchedule — one row per expected installment on a loan."""

from app.extensions import db

from .base import pg_enum
from .enums import RepaymentStatus


class RepaymentSchedule(db.Model):
    __tablename__ = "repayment_schedules"

    id = db.Column(db.Integer, primary_key=True)
    loan_id = db.Column(
        db.Integer,
        db.ForeignKey("loans.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    installment_number = db.Column(db.Integer, nullable=False)
    due_date = db.Column(db.Date, nullable=False)
    amount_due = db.Column(db.Numeric(12, 2), nullable=False)
    amount_paid = db.Column(
        db.Numeric(12, 2), nullable=False, default=0, server_default=db.text("0")
    )
    status = db.Column(
        pg_enum(RepaymentStatus, "repayment_status"),
        nullable=False,
        default=RepaymentStatus.UPCOMING,
        server_default=RepaymentStatus.UPCOMING.value,
        index=True,
    )

    __table_args__ = (
        db.UniqueConstraint(
            "loan_id", "installment_number", name="uq_schedule_loan_installment"
        ),
    )

    # ---------------------------------------------------------------- relationships
    loan = db.relationship("Loan", back_populates="repayment_schedule")
    payments = db.relationship(
        "PaymentTransaction", back_populates="repayment_schedule"
    )

    def __repr__(self) -> str:
        return (
            f"<RepaymentSchedule loan={self.loan_id} "
            f"#{self.installment_number} {self.status}>"
        )
