"""Versioned PRIME pricing and late-penalty policy (insert-only).

An admin change never edits a version: it adds a new one. Each application
records the versions current when the customer submitted it (its quote), and
the loan-terms snapshot copies that quote at disbursement - so a later change
only affects new applications and never reprices an existing loan.
"""

from sqlalchemy import func

from app.extensions import db


class PrimePricingVersion(db.Model):
    __tablename__ = "prime_pricing_versions"

    id = db.Column(db.Integer, primary_key=True)
    label = db.Column(db.String(50), nullable=False, unique=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, server_default=func.now())
    created_by = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="RESTRICT"))  # NULL = seeded
    note = db.Column(db.String(500))

    tiers = db.relationship(
        "PrimePricingTier",
        back_populates="version",
        order_by="PrimePricingTier.min_amount",
        passive_deletes="all",
    )

    def __repr__(self) -> str:
        return f"<PrimePricingVersion {self.label}>"


class PrimePricingTier(db.Model):
    __tablename__ = "prime_pricing_tiers"
    __table_args__ = (
        db.UniqueConstraint("version_id", "category", name="uq_prime_pricing_tiers_version_category"),
        db.CheckConstraint(
            "min_amount > 0 AND max_amount >= min_amount", name="ck_prime_pricing_tiers_range"
        ),
        db.CheckConstraint(
            "interest_rate > 0 AND interest_rate <= 1", name="ck_prime_pricing_tiers_rate"
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    version_id = db.Column(
        db.Integer, db.ForeignKey("prime_pricing_versions.id", ondelete="RESTRICT"), nullable=False
    )
    category = db.Column(db.String(20), nullable=False)  # "PRIME 1"
    min_amount = db.Column(db.Numeric(12, 2), nullable=False)
    max_amount = db.Column(db.Numeric(12, 2), nullable=False)
    interest_rate = db.Column(db.Numeric(6, 4), nullable=False)  # flat, over the term

    version = db.relationship("PrimePricingVersion", back_populates="tiers")


class PenaltyPolicyVersion(db.Model):
    __tablename__ = "penalty_policy_versions"

    id = db.Column(db.Integer, primary_key=True)
    label = db.Column(db.String(50), nullable=False, unique=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, server_default=func.now())
    created_by = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="RESTRICT"))  # NULL = seeded
    note = db.Column(db.String(500))

    tiers = db.relationship(
        "PenaltyPolicyTier",
        back_populates="version",
        order_by="PenaltyPolicyTier.tier",
        passive_deletes="all",
    )

    def __repr__(self) -> str:
        return f"<PenaltyPolicyVersion {self.label}>"


class PenaltyPolicyTier(db.Model):
    """One late tier: `days_late` after the due date with the ORIGINAL
    obligation still unpaid adds `pct_of_original_interest` x the loan's
    original interest (never x the outstanding balance). Tiers are
    cumulative; there is nothing after the last tier."""

    __tablename__ = "penalty_policy_tiers"
    __table_args__ = (
        db.UniqueConstraint("version_id", "tier", name="uq_penalty_policy_tiers_version_tier"),
        db.CheckConstraint("tier >= 1 AND days_late >= 1", name="ck_penalty_policy_tiers_positive"),
        db.CheckConstraint(
            "pct_of_original_interest > 0 AND pct_of_original_interest <= 10",
            name="ck_penalty_policy_tiers_pct",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    version_id = db.Column(
        db.Integer, db.ForeignKey("penalty_policy_versions.id", ondelete="RESTRICT"), nullable=False
    )
    tier = db.Column(db.SmallInteger, nullable=False)
    days_late = db.Column(db.Integer, nullable=False)
    pct_of_original_interest = db.Column(db.Numeric(6, 4), nullable=False)

    version = db.relationship("PenaltyPolicyVersion", back_populates="tiers")
