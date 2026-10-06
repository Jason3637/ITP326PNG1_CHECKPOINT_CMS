"""Loan — an approved, disbursed application with repayment terms.

term_months (the legacy amortized-product field) and term_days (the PRIME
product's fixed 14-day bullet term) are both nullable: exactly one is
populated depending on which product priced the loan. PRIME loans populate
term_days only; term_months stays null and interest_calculation.amortize()'s
multi-installment/multi-frequency machinery goes dormant for them - it's kept
for the (currently inactive) above-K1,000 product.
"""

from app.extensions import db

from .base import pg_enum
from .enums import LoanClosureReason, LoanStatus


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
    # For PRIME: the flat term rate (e.g. 0.40 = 40% over the 14-day term),
    # not an annualized rate. For the legacy amortized product: annual rate
    # as a fraction, e.g. 0.1750 = 17.5%.
    interest_rate = db.Column(db.Numeric(6, 4), nullable=False)
    term_months = db.Column(db.Integer, nullable=True)
    term_days = db.Column(db.Integer, nullable=True)
    # Named for the legacy amortized product's level installment; for PRIME
    # (a single bullet repayment) this equals total_repayable.
    monthly_payment = db.Column(db.Numeric(12, 2), nullable=False)
    total_repayable = db.Column(db.Numeric(12, 2), nullable=False)
    status = db.Column(
        pg_enum(LoanStatus, "loan_status"),
        nullable=False,
        default=LoanStatus.ACTIVE,
        server_default=LoanStatus.ACTIVE.value,
        index=True,
    )
    # Set only when status becomes CLOSED - see LoanClosureReason's docstring
    # for why this exists (it's what lets DEFAULTED fold into CLOSED without
    # losing the signal credit_evaluation.py scores on).
    closure_reason = db.Column(pg_enum(LoanClosureReason, "loan_closure_reason"))
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
    disbursement = db.relationship(
        "Disbursement",
        back_populates="loan",
        uselist=False,
        passive_deletes="all",
    )
    # The permanent record (insert-only, ON DELETE RESTRICT - see
    # app/models/loan_records.py). passive_deletes="all": the ORM never
    # touches these rows when a loan is deleted; the database refuses.
    terms_snapshot = db.relationship(
        "LoanTermsSnapshot", back_populates="loan", uselist=False, passive_deletes="all"
    )
    ledger_entries = db.relationship(
        "LoanLedgerEntry",
        back_populates="loan",
        order_by="LoanLedgerEntry.id",
        passive_deletes="all",
    )
    closure = db.relationship(
        "LoanClosure", back_populates="loan", uselist=False, passive_deletes="all"
    )
    reapplication_clearance = db.relationship(
        "ReapplicationClearance", back_populates="loan", uselist=False, passive_deletes="all"
    )

    @property
    def blocks_reapplication(self) -> bool:
        """Written off and not yet cleared by an admin - the customer can't
        apply for PRIME again until it is."""
        return (
            self.status == LoanStatus.CLOSED
            and self.closure_reason == LoanClosureReason.DEFAULTED
            and self.reapplication_clearance is None
        )

    def __repr__(self) -> str:
        return f"<Loan {self.id} user={self.user_id} {self.status}>"
