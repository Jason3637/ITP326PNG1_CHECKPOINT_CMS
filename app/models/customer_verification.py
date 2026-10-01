"""CustomerVerification — customer-level identity verification, separate from
the per-application VerificationItem checklist, so a returning customer's
still-valid verification can be carried over instead of redone.

Append-only: re-verifying creates a NEW row; the old one is marked
INVALIDATED (with an explicit reason) rather than edited. At most one row per
user can be VERIFIED at a time (partial unique index below).

``valid_until`` = min(id_expiry_date, verified_at + the admin-tunable
customer-verification validity period). The 12-month default for that
period is an engineering default awaiting Prime's Vault confirmation.
"""

from sqlalchemy import func

from app.extensions import db

from .base import pg_enum
from .enums import CustomerVerificationInvalidationReason, CustomerVerificationStatus

_VERIFIED_ONLY = "status = 'verified'"


class CustomerVerification(db.Model):
    __tablename__ = "customer_verifications"
    __table_args__ = (
        db.Index(
            "uq_customer_verifications_one_verified_per_user",
            "user_id",
            unique=True,
            postgresql_where=db.text(_VERIFIED_ONLY),
            sqlite_where=db.text(_VERIFIED_ONLY),
        ),
        # INVALIDATED <=> when and why are recorded (invalidated_by may be
        # NULL - the system invalidates expired rows on its own).
        db.CheckConstraint(
            "(status = 'invalidated') = (invalidated_at IS NOT NULL AND invalidation_reason IS NOT NULL)",
            name="ck_customer_verifications_invalidated_fields",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status = db.Column(
        pg_enum(CustomerVerificationStatus, "customer_verification_status"),
        nullable=False,
        default=CustomerVerificationStatus.VERIFIED,
        server_default=CustomerVerificationStatus.VERIFIED.value,
    )
    # NO ACTION (not SET NULL): a verification must always name its verifier.
    verified_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    verified_at = db.Column(
        db.DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    id_document_id = db.Column(db.Integer, db.ForeignKey("documents.id"), nullable=False)
    # Read off the ID by the officer - the basis of the age 18+ check. Not
    # self-reported: nothing else in the schema stores a date of birth.
    date_of_birth = db.Column(db.Date, nullable=False)
    id_expiry_date = db.Column(db.Date)
    # Snapshot of the contact details that were verified, so later changes
    # can be detected (-> invalidation_reason=information_changed).
    verified_full_name = db.Column(db.String(255), nullable=False)
    verified_email = db.Column(db.String(255), nullable=False)
    verified_phone_number = db.Column(db.String(32))
    valid_until = db.Column(db.Date, nullable=False)
    policy_version = db.Column(db.String(20), nullable=False)
    source_application_id = db.Column(
        db.Integer, db.ForeignKey("loan_applications.id", ondelete="SET NULL")
    )
    invalidated_at = db.Column(db.DateTime(timezone=True))
    # NULL when the system invalidated it (e.g. the daily expiry job).
    invalidated_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    invalidation_reason = db.Column(
        pg_enum(
            CustomerVerificationInvalidationReason,
            "customer_verification_invalidation_reason",
        )
    )
    invalidation_note = db.Column(db.String(1000))

    # ---------------------------------------------------------------- relationships
    user = db.relationship(
        "User", back_populates="customer_verifications", foreign_keys=[user_id]
    )
    verifier = db.relationship("User", foreign_keys=[verified_by])
    invalidator = db.relationship("User", foreign_keys=[invalidated_by])
    id_document = db.relationship("Document")
    source_application = db.relationship("LoanApplication")

    def __repr__(self) -> str:
        return f"<CustomerVerification {self.id} user={self.user_id} {self.status}>"
