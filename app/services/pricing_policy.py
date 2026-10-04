"""Which PRIME pricing and penalty-policy versions are current, and the
quote locked onto an application when the customer submits it.

Versions are insert-only (see app/models/pricing_policy.py). Version 1 of
each is seeded by migration e4a9b7c2d158 on real databases, and by
seed_reference_data() for tests and fresh local setups. Its values are
prime_pricing.TIERS and PENALTY_TIERS_V1 - the same numbers, one source.
"""

from decimal import Decimal

from app.extensions import db
from app.models import (
    PenaltyPolicyTier,
    PenaltyPolicyVersion,
    PrimePricingTier,
    PrimePricingVersion,
)

from . import prime_pricing
from .errors import ServiceError

PRICING_V1_LABEL = "prime-v1"
PENALTY_V1_LABEL = "penalty-v1"
# (tier, days_late, pct_of_original_interest): cumulative, nothing after tier 2.
PENALTY_TIERS_V1 = (
    (1, 7, Decimal("0.25")),
    (2, 14, Decimal("1.00")),
)


def seed_reference_data() -> None:
    """Insert version 1 of each policy if the table is empty. Idempotent."""
    if PrimePricingVersion.query.first() is None:
        version = PrimePricingVersion(label=PRICING_V1_LABEL, note="Initial PRIME tiers.")
        db.session.add(version)
        db.session.flush()
        for category, low, high, rate in prime_pricing.TIERS:
            db.session.add(PrimePricingTier(
                version_id=version.id, category=category,
                min_amount=low, max_amount=high, interest_rate=rate,
            ))
    if PenaltyPolicyVersion.query.first() is None:
        version = PenaltyPolicyVersion(label=PENALTY_V1_LABEL, note="Initial late-penalty tiers.")
        db.session.add(version)
        db.session.flush()
        for tier, days_late, pct in PENALTY_TIERS_V1:
            db.session.add(PenaltyPolicyTier(
                version_id=version.id, tier=tier, days_late=days_late,
                pct_of_original_interest=pct,
            ))
    db.session.commit()


def current_pricing_version() -> PrimePricingVersion:
    version = PrimePricingVersion.query.order_by(PrimePricingVersion.id.desc()).first()
    if version is None:
        raise ServiceError("No PRIME pricing version is configured.", 500)
    return version


def current_penalty_policy() -> PenaltyPolicyVersion:
    version = PenaltyPolicyVersion.query.order_by(PenaltyPolicyVersion.id.desc()).first()
    if version is None:
        raise ServiceError("No late-penalty policy version is configured.", 500)
    return version


def lock_quote(application, pricing: dict) -> None:
    """Record the PRIME quote on a just-submitted application (insert-once).
    `pricing` is prime_pricing.calculate_prime()'s result for its amount."""
    application.pricing_version_id = current_pricing_version().id
    application.penalty_policy_version_id = current_penalty_policy().id
    application.quoted_interest_rate = pricing["rate"]
    application.quoted_interest_amount = pricing["interest_amount"]
    application.quoted_total_repayable = pricing["total_repayable"]
