"""Loan application — a customer's request, before any money moves."""

from sqlalchemy import func

from app.extensions import db

from .base import JSONType, pg_enum
from .enums import (
    DisbursementMethod,
    EmploymentStatus,
    LoanApplicationStatus,
    LoanPurposeCategory,
    RepaymentFrequency,
)


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
    purpose_category = db.Column(
        pg_enum(LoanPurposeCategory, "loan_purpose_category"), nullable=True
    )
    # Free-text description. Required (by the service layer) when
    # purpose_category is OTHER; an optional short note for every other category.
    purpose = db.Column(db.String(500))
    # Snapshot of the applicant's personal details as confirmed at submission
    # time - the frontend pre-fills these from GET /users/profile, the
    # customer reviews/edits, and the confirmed values are stored here rather
    # than silently overwriting the User row (an email/phone change belongs
    # to a dedicated profile-update flow, not a side effect of applying).
    confirmed_full_name = db.Column(db.String(255), nullable=True)
    confirmed_email = db.Column(db.String(255), nullable=True)
    confirmed_phone_number = db.Column(db.String(32), nullable=True)
    # Legacy amortized-product fields - unused by PRIME (which fixes both:
    # a 14-day bullet term, no installment frequency). Nullable going forward;
    # left in place for the (currently inactive) above-K1,000 product.
    term_months = db.Column(db.Integer, nullable=True)
    repayment_frequency = db.Column(
        pg_enum(RepaymentFrequency, "repayment_frequency"), nullable=True
    )
    # PRIME 1/2/3, computed by app.services.prime_pricing.calculate_prime()
    # at submission time and stored for officer/admin visibility and
    # reporting without recomputation. Interest/total/term are NOT persisted
    # here - they're derived from amount_requested + this category on read,
    # keeping calculate_prime() the single source of truth.
    prime_category = db.Column(db.String(20))
    disbursement_method_requested = db.Column(
        pg_enum(DisbursementMethod, "disbursement_method"), nullable=True
    )
    # Method-specific reference: BSP mobile banking number for
    # BSP_MOBILE_BANKING, optional pickup note for CASH_ON_HAND.
    disbursement_account_reference = db.Column(db.String(255))
    # Set by loan_processing.request_customer_action() - what the officer
    # needs from the customer while status is CUSTOMER_ACTION_REQUIRED.
    # Surfaced on the application (not just buried in AuditLog, which
    # customers can't read) so the frontend can show it and
    # respond_to_customer_action() has something concrete to answer.
    action_required_note = db.Column(db.String(1000))
    status = db.Column(
        pg_enum(LoanApplicationStatus, "loan_application_status"),
        nullable=False,
        default=LoanApplicationStatus.SUBMITTED,
        server_default=LoanApplicationStatus.SUBMITTED.value,
        index=True,
    )
    # ------------------------------------------------- credit-evaluation inputs
    # Self-reported by the applicant at submission time (see the apply form).
    # All nullable: an application can be submitted without them, but
    # credit_evaluation.evaluate() treats missing income as disqualifying (it
    # cannot assess affordability without it) - see that module's docstring.
    monthly_income = db.Column(db.Numeric(12, 2), nullable=True)
    employment_status = db.Column(
        pg_enum(EmploymentStatus, "employment_status"), nullable=True
    )
    # Other recurring monthly debt (rent-to-own, other loans, etc.), for the
    # debt-to-income check. Defaults to 0 (assumed no other debt) when omitted.
    existing_monthly_debt = db.Column(db.Numeric(12, 2), nullable=True)

    # Populated by the credit-evaluation engine (score, flags, reasons). See
    # app/services/credit_evaluation.py for exactly what "algorithm" means.
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
    referees = db.relationship(
        "Referee",
        back_populates="loan_application",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    terms_acceptance = db.relationship(
        "TermsAcceptance",
        back_populates="loan_application",
        uselist=False,
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def __repr__(self) -> str:
        return f"<LoanApplication {self.id} user={self.user_id} {self.status}>"
