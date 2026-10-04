"""The permanent financial record of a loan - all insert-only.

    loan_terms_snapshots  the terms the loan was disbursed on, written once
    loan_ledger_entries   every amount owed or paid; balance = SUM(amount)
    loan_closures         how and when the loan was closed
    scheduled_job_runs    a run of a scheduled job (penalty entries point here)

None of these rows is ever updated or deleted: there is no service code that
does it, app/models/immutability.py makes the ORM refuse it, and on Postgres
a trigger (migration d8c1f4a2b6e3) rejects it for any client. Their foreign
keys are ON DELETE RESTRICT, so deleting a loan, user or application can't
cascade into them either.
"""

from sqlalchemy import func

from app.extensions import db

from .base import JSONType, pg_enum
from .enums import LedgerActorKind, LedgerEntryType, LoanClosureReason, LoanTimeliness


class LoanTermsSnapshot(db.Model):
    __tablename__ = "loan_terms_snapshots"
    __table_args__ = (
        db.CheckConstraint(
            "original_total_due = principal + interest_amount",
            name="ck_loan_terms_snapshots_total",
        ),
        db.CheckConstraint(
            "principal > 0 AND interest_amount >= 0 AND term_days > 0 AND due_date > disbursed_local_date",
            name="ck_loan_terms_snapshots_positive",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    loan_id = db.Column(
        db.Integer, db.ForeignKey("loans.id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    application_id = db.Column(
        db.Integer, db.ForeignKey("loan_applications.id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    disbursement_id = db.Column(
        db.Integer, db.ForeignKey("disbursements.id", ondelete="RESTRICT"), unique=True
    )  # NULL only for loans that predate this table and had no disbursement row
    pricing_version_id = db.Column(
        db.Integer, db.ForeignKey("prime_pricing_versions.id", ondelete="RESTRICT"), nullable=False
    )
    penalty_policy_version_id = db.Column(
        db.Integer, db.ForeignKey("penalty_policy_versions.id", ondelete="RESTRICT"), nullable=False
    )
    prime_category = db.Column(db.String(20), nullable=False)
    principal = db.Column(db.Numeric(12, 2), nullable=False)
    interest_rate = db.Column(db.Numeric(6, 4), nullable=False)
    interest_amount = db.Column(db.Numeric(12, 2), nullable=False)
    original_total_due = db.Column(db.Numeric(12, 2), nullable=False)
    term_days = db.Column(db.Integer, nullable=False)
    disbursed_at = db.Column(db.DateTime(timezone=True), nullable=False)
    # Port Moresby calendar date of disbursement; due_date counts from it.
    disbursed_local_date = db.Column(db.Date, nullable=False)
    due_date = db.Column(db.Date, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, server_default=func.now())
    created_by = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="RESTRICT"))

    loan = db.relationship("Loan", back_populates="terms_snapshot")
    pricing_version = db.relationship("PrimePricingVersion")
    penalty_policy_version = db.relationship("PenaltyPolicyVersion")


_OBLIGATION = "entry_type = 'original_obligation'"
_PENALTY = "entry_type = 'penalty'"
_REPAYMENT_REF = "payment_transaction_id IS NOT NULL"


class LoanLedgerEntry(db.Model):
    __tablename__ = "loan_ledger_entries"
    __table_args__ = (
        # Idempotency, enforced by the database:
        db.Index(
            "uq_loan_ledger_one_obligation_per_loan", "loan_id", unique=True,
            postgresql_where=db.text(_OBLIGATION), sqlite_where=db.text(_OBLIGATION),
        ),
        db.Index(
            "uq_loan_ledger_one_penalty_per_tier", "loan_id", "penalty_tier", unique=True,
            postgresql_where=db.text(_PENALTY), sqlite_where=db.text(_PENALTY),
        ),
        db.Index(
            "uq_loan_ledger_one_entry_per_payment", "payment_transaction_id", unique=True,
            postgresql_where=db.text(_REPAYMENT_REF), sqlite_where=db.text(_REPAYMENT_REF),
        ),
        # Signs: owed > 0, paid < 0.
        db.CheckConstraint(
            "(entry_type IN ('original_obligation', 'penalty') AND amount > 0)"
            " OR (entry_type = 'verified_repayment' AND amount < 0)",
            name="ck_loan_ledger_amount_sign",
        ),
        # Every entry names the event that created it.
        db.CheckConstraint(
            "(entry_type <> 'verified_repayment' OR payment_transaction_id IS NOT NULL)"
            " AND (entry_type <> 'penalty' OR (penalty_tier IS NOT NULL"
            " AND penalty_policy_version_id IS NOT NULL AND job_run_id IS NOT NULL))",
            name="ck_loan_ledger_source",
        ),
        db.CheckConstraint(
            "(created_by_kind = 'admin') = (created_by IS NOT NULL)",
            name="ck_loan_ledger_actor",
        ),
    )

    id = db.Column(db.BigInteger().with_variant(db.Integer, "sqlite"), primary_key=True)
    loan_id = db.Column(
        db.Integer, db.ForeignKey("loans.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    entry_type = db.Column(pg_enum(LedgerEntryType, "ledger_entry_type"), nullable=False)
    amount = db.Column(db.Numeric(12, 2), nullable=False)
    # The business date: disbursement date, the tier's date, or the date the
    # customer paid - not when the row was written.
    effective_date = db.Column(db.Date, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, server_default=func.now())
    created_by = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="RESTRICT"))
    created_by_kind = db.Column(pg_enum(LedgerActorKind, "ledger_actor_kind"), nullable=False)
    # Source of the entry.
    disbursement_id = db.Column(db.Integer, db.ForeignKey("disbursements.id", ondelete="RESTRICT"))
    payment_transaction_id = db.Column(
        db.Integer, db.ForeignKey("payment_transactions.id", ondelete="RESTRICT")
    )
    penalty_tier = db.Column(db.SmallInteger)
    penalty_policy_version_id = db.Column(
        db.Integer, db.ForeignKey("penalty_policy_versions.id", ondelete="RESTRICT")
    )
    job_run_id = db.Column(db.Integer, db.ForeignKey("scheduled_job_runs.id", ondelete="RESTRICT"))
    note = db.Column(db.String(500))

    loan = db.relationship("Loan", back_populates="ledger_entries")

    def __repr__(self) -> str:
        return f"<LoanLedgerEntry loan={self.loan_id} {self.entry_type} {self.amount}>"


class LoanClosure(db.Model):
    __tablename__ = "loan_closures"
    __table_args__ = (
        db.CheckConstraint(
            "(closure_reason = 'paid_in_full') = (outstanding_at_closure = 0)",
            name="ck_loan_closures_outstanding",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    loan_id = db.Column(
        db.Integer, db.ForeignKey("loans.id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    closed_at = db.Column(db.DateTime(timezone=True), nullable=False, server_default=func.now())
    closure_reason = db.Column(pg_enum(LoanClosureReason, "loan_closure_reason"), nullable=False)
    closed_by = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="RESTRICT"))
    closing_payment_transaction_id = db.Column(
        db.Integer, db.ForeignKey("payment_transactions.id", ondelete="RESTRICT")
    )
    original_total_due = db.Column(db.Numeric(12, 2), nullable=False)
    total_penalties = db.Column(db.Numeric(12, 2), nullable=False)
    total_verified_paid = db.Column(db.Numeric(12, 2), nullable=False)
    outstanding_at_closure = db.Column(db.Numeric(12, 2), nullable=False)
    final_payment_date = db.Column(db.Date)
    repayment_duration_days = db.Column(db.Integer)
    timeliness = db.Column(pg_enum(LoanTimeliness, "loan_timeliness"))  # NULL for a write-off with no payment

    loan = db.relationship("Loan", back_populates="closure")


class ScheduledJobRun(db.Model):
    __tablename__ = "scheduled_job_runs"

    id = db.Column(db.Integer, primary_key=True)
    job_name = db.Column(db.String(50), nullable=False, index=True)
    started_at = db.Column(db.DateTime(timezone=True), nullable=False, server_default=func.now())
    finished_at = db.Column(db.DateTime(timezone=True))
    summary = db.Column(JSONType)
