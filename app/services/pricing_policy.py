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
    `pricing` is prime_pricing.calculate_prime()'s result for its amount -
    the version it priced against is the one recorded."""
    application.pricing_version_id = pricing.get("pricing_version_id") or current_pricing_version().id
    application.penalty_policy_version_id = current_penalty_policy().id
    application.quoted_interest_rate = pricing["rate"]
    application.quoted_interest_amount = pricing["interest_amount"]
    application.quoted_total_repayable = pricing["total_repayable"]


# ------------------------------------------------------- admin: new versions
def _require_admin(actor) -> None:
    """Re-checked here, from the database role, not only at the route: a
    token's role claim can be up to an hour stale after a role change."""
    from app.models.enums import UserRole

    if actor is None or actor.role != UserRole.ADMIN:
        raise ServiceError("Only an administrator can change pricing or penalty policy.", 403)


def _money(v) -> float:
    return float(Decimal(v))


def serialize_pricing_version(v: PrimePricingVersion, current_id: int | None = None) -> dict:
    return {
        "id": v.id,
        "label": v.label,
        "note": v.note,
        "created_at": v.created_at.isoformat() if v.created_at else None,
        "created_by": v.created_by,
        "is_current": v.id == current_id,
        "tiers": [
            {"category": t.category, "min_amount": _money(t.min_amount),
             "max_amount": _money(t.max_amount), "interest_rate": float(t.interest_rate)}
            for t in v.tiers
        ],
    }


def serialize_penalty_version(v: PenaltyPolicyVersion, current_id: int | None = None) -> dict:
    return {
        "id": v.id,
        "label": v.label,
        "note": v.note,
        "created_at": v.created_at.isoformat() if v.created_at else None,
        "created_by": v.created_by,
        "is_current": v.id == current_id,
        "tiers": [
            {"tier": t.tier, "days_late": t.days_late,
             "pct_of_original_interest": float(t.pct_of_original_interest)}
            for t in v.tiers
        ],
    }


def _dec(value, field: str) -> Decimal:
    try:
        d = Decimal(str(value))
    except Exception:
        raise ServiceError(f"{field} must be a number.")
    if not d.is_finite():
        raise ServiceError(f"{field} must be a number.")
    return d


def _clean_note(note) -> str | None:
    note = (note or "").strip() or None
    if note and len(note) > 500:
        raise ServiceError("note must be at most 500 characters.")
    return note


def _parse_pricing_tiers(tiers) -> list[tuple[str, Decimal, Decimal, Decimal]]:
    if not isinstance(tiers, list) or not 1 <= len(tiers) <= 10:
        raise ServiceError("tiers must be a list of 1-10 {category, min_amount, max_amount, interest_rate}.")
    parsed = []
    for i, t in enumerate(tiers):
        if not isinstance(t, dict):
            raise ServiceError(f"tiers[{i}] must be an object.")
        category = (t.get("category") or "").strip()
        if not category or len(category) > 20:
            raise ServiceError(f"tiers[{i}].category is required (max 20 characters).")
        low, high = _dec(t.get("min_amount"), f"tiers[{i}].min_amount"), _dec(t.get("max_amount"), f"tiers[{i}].max_amount")
        rate = _dec(t.get("interest_rate"), f"tiers[{i}].interest_rate")
        if low != low.to_integral_value() or high != high.to_integral_value():
            raise ServiceError(f"tiers[{i}]: amounts must be whole Kina.")
        if low < 1 or high < low:
            raise ServiceError(f"tiers[{i}]: min_amount must be at least 1 and no more than max_amount.")
        if not (Decimal("0") < rate <= Decimal("1")):
            raise ServiceError(f"tiers[{i}].interest_rate must be a fraction above 0 and at most 1 (0.40 = 40%).")
        parsed.append((category, low, high, rate))
    parsed.sort(key=lambda t: t[1])
    if len({t[0] for t in parsed}) != len(parsed):
        raise ServiceError("Each tier needs its own category name.")
    for prev, nxt in zip(parsed, parsed[1:]):
        if nxt[1] != prev[2] + 1:
            raise ServiceError(
                f"Tiers must be contiguous with no gaps or overlaps: {prev[0]} ends at K{prev[2]:,.0f}, "
                f"so {nxt[0]} must start at K{prev[2] + 1:,.0f}."
            )
    return parsed


def _parse_penalty_tiers(tiers) -> list[tuple[int, int, Decimal]]:
    if not isinstance(tiers, list) or not 1 <= len(tiers) <= 5:
        raise ServiceError("tiers must be a list of 1-5 {days_late, pct_of_original_interest}.")
    parsed = []
    for i, t in enumerate(tiers):
        if not isinstance(t, dict):
            raise ServiceError(f"tiers[{i}] must be an object.")
        try:
            days = int(t.get("days_late"))
        except (TypeError, ValueError):
            raise ServiceError(f"tiers[{i}].days_late must be a whole number of days.")
        pct = _dec(t.get("pct_of_original_interest"), f"tiers[{i}].pct_of_original_interest")
        if days < 1:
            raise ServiceError(f"tiers[{i}].days_late must be at least 1.")
        if not (Decimal("0") < pct <= Decimal("10")):
            raise ServiceError(f"tiers[{i}].pct_of_original_interest must be above 0 and at most 10 (1.00 = 100%).")
        parsed.append((days, pct))
    parsed.sort()
    if len({d for d, _ in parsed}) != len(parsed):
        raise ServiceError("Each tier needs a different days_late.")
    return [(n, d, p) for n, (d, p) in enumerate(parsed, start=1)]


def create_pricing_version(admin, tiers, note=None) -> PrimePricingVersion:
    """A new PRIME pricing version, current from now on for NEW applications.
    Existing applications keep their locked quote; existing loans keep their
    terms snapshot - nothing already quoted or disbursed is repriced."""
    from . import audit

    _require_admin(admin)
    parsed = _parse_pricing_tiers(tiers)
    note = _clean_note(note)
    previous = current_pricing_version()
    count = PrimePricingVersion.query.count()
    version = PrimePricingVersion(label=f"prime-v{count + 1}", created_by=admin.id, note=note)
    db.session.add(version)
    db.session.flush()
    for category, low, high, rate in parsed:
        db.session.add(PrimePricingTier(version_id=version.id, category=category,
                                        min_amount=low, max_amount=high, interest_rate=rate))
    db.session.flush()
    audit.record(
        "prime_pricing_version_created",
        actor_id=admin.id,
        entity_type="PrimePricingVersion",
        entity_id=version.id,
        details={
            "before": serialize_pricing_version(previous)["tiers"],
            "before_version": previous.label,
            "after": serialize_pricing_version(version)["tiers"],
            "after_version": version.label,
            "note": note,
        },
        commit=False,
    )
    db.session.commit()
    return version


def create_penalty_version(admin, tiers, note=None) -> PenaltyPolicyVersion:
    """A new late-penalty policy version, for applications submitted from now
    on. Loans already disbursed keep the version recorded in their snapshot."""
    from . import audit

    _require_admin(admin)
    parsed = _parse_penalty_tiers(tiers)
    note = _clean_note(note)
    previous = current_penalty_policy()
    count = PenaltyPolicyVersion.query.count()
    version = PenaltyPolicyVersion(label=f"penalty-v{count + 1}", created_by=admin.id, note=note)
    db.session.add(version)
    db.session.flush()
    for tier, days, pct in parsed:
        db.session.add(PenaltyPolicyTier(version_id=version.id, tier=tier, days_late=days,
                                         pct_of_original_interest=pct))
    db.session.flush()
    audit.record(
        "penalty_policy_version_created",
        actor_id=admin.id,
        entity_type="PenaltyPolicyVersion",
        entity_id=version.id,
        details={
            "before": serialize_penalty_version(previous)["tiers"],
            "before_version": previous.label,
            "after": serialize_penalty_version(version)["tiers"],
            "after_version": version.label,
            "note": note,
        },
        commit=False,
    )
    db.session.commit()
    return version
