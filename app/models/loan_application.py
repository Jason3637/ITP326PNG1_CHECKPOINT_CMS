"""Loan application — a customer's request, before any money moves."""

from sqlalchemy import func

from app.extensions import db

from .base import JSONType, pg_enum
from .enums import LoanApplicationStatus, RepaymentFrequency


class LoanApplication(db.Model):
    __tablename__ = "loan_applications"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    amount_requested = db.Column(db.Numeric(12, 2), nullable=False)
    purpose = db.Column(db.String(500))
    term_months = db.Column(db.Integer, nullable=False)
    repayment_frequency = db.Column(
        pg_enum(RepaymentFrequency, "repayment_frequency"),
        nullable=False,
        default=RepaymentFrequency.MONTHLY,
        server_default=RepaymentFrequency.MONTHLY.value,
    )
    status = db.Column(
        pg_enum(LoanApplicationStatus, "loan_application_status"),
        nullable=False,
        default=LoanApplicationStatus.PENDING,
        server_default=LoanApplicationStatus.PENDING.value,
        index=True,
    )
    # Populated by the credit-evaluation engine in Phase B3 (score, flags, reasons).
    credit_evaluation_result = db.Column(JSONType)

    submitted_at = db.Column(
        db.DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    decided_at = db.Column(db.DateTime(timezone=True))
    decided_by = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    # ---------------------------------------------------------------- relationships
    applicant = db.relationship(
        "User", back_populates="loan_applications", foreign_keys=[user_id]
    )
    decided_by_officer = db.relationship(
        "User", back_populates="decided_applications", foreign_keys=[decided_by]
    )
    loan = db.relationship(
        "Loan",
        back_populates="application",
        uselist=False,
        cascade="all, delete-orphan",
        passive_deletes=True,
        single_parent=True,
    )
    documents = db.relationship(
        "Document", back_populates="loan_application"
    )

    def __repr__(self) -> str:
        return f"<LoanApplication {self.id} user={self.user_id} {self.status}>"
