"""PRIME pricing - the authoritative, single-source-of-truth calculation for
Prime's Vault's fixed-tier micro-loan product.

PRIME is a flat-fee, 14-day, single-repayment product covering K100-K1,000.
The above-K1,000 tier is a distinct, larger-loan product that is NOT being
activated yet - amounts above K1,000 are rejected here, not silently priced.

The tiers themselves are admin-configurable and versioned (the
prime_pricing_versions / _tiers tables, see app/services/pricing_policy.py):
calculate_prime() prices against the current version. TIERS below is
version 1 - what the database is seeded with - and the fallback when no
version exists (a bare database before its first migration).
"""

from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from .errors import ServiceError

# Version 1's overall range; the current range comes from current_tiers().
PRIME_MIN_AMOUNT = Decimal("100")
PRIME_MAX_AMOUNT = Decimal("1000")
PRIME_TERM_DAYS = 14

# Due dates are Port Moresby calendar dates. PNG is UTC+10 with no daylight
# saving, so a fixed offset is exact (and needs no tz database on Windows).
LOCAL_TZ = timezone(timedelta(hours=10), "Pacific/Port_Moresby")


def local_date(moment: datetime) -> date:
    """The Port Moresby calendar date of an aware datetime."""
    return moment.astimezone(LOCAL_TZ).date()

_KINA = Decimal("1")

# Version 1: (category, min_amount, max_amount, flat_interest_rate).
TIERS: tuple[tuple[str, Decimal, Decimal, Decimal], ...] = (
    ("PRIME 1", Decimal("100"), Decimal("300"), Decimal("0.50")),
    ("PRIME 2", Decimal("301"), Decimal("700"), Decimal("0.40")),
    ("PRIME 3", Decimal("701"), Decimal("1000"), Decimal("0.35")),
)


def current_version() -> tuple[int | None, tuple]:
    """(version id, tiers) of the current pricing version; (None, TIERS)
    if the database has none yet."""
    from flask import has_app_context

    from app.models import PrimePricingVersion  # local: models import services lazily

    if not has_app_context():  # pure pricing maths (unit tests, scripts) - version 1
        return None, TIERS
    version = PrimePricingVersion.query.order_by(PrimePricingVersion.id.desc()).first()
    if version is None or not version.tiers:
        return None, TIERS
    return version.id, tuple(
        (t.category, Decimal(t.min_amount), Decimal(t.max_amount), Decimal(t.interest_rate))
        for t in version.tiers
    )


def current_tiers() -> tuple:
    return current_version()[1]


def bounds(tiers=None) -> tuple[Decimal, Decimal]:
    tiers = tiers if tiers is not None else current_tiers()
    return min(t[1] for t in tiers), max(t[2] for t in tiers)


def calculate_prime(amount_requested, tiers=None) -> dict:
    """Price a PRIME loan for a requested whole-Kina amount against the
    current pricing version (or `tiers`, if given).

    Returns {category, amount, interest_amount, total_repayable, term_days,
    rate, pricing_version_id}. Raises ServiceError (400) for anything outside
    the tiers' overall range or with a fractional-Kina (toea) component -
    PRIME amounts are whole Kina only.
    """
    version_id = None
    if tiers is None:
        version_id, tiers = current_version()
    low, high = bounds(tiers)
    try:
        amount = Decimal(str(amount_requested))
    except (InvalidOperation, TypeError):
        raise ServiceError("amount_requested must be a number.")

    if amount != amount.to_integral_value():
        raise ServiceError("amount_requested must be a whole-Kina amount (no toea).")

    if amount < low or amount > high:
        raise ServiceError(
            f"amount_requested must be between K{low:,.0f} and "
            f"K{high:,.0f}. Loans above K{high:,.0f} are "
            "a separate product that is not currently being offered."
        )

    for category, tier_min, tier_max, rate in tiers:
        if tier_min <= amount <= tier_max:
            interest_amount = (amount * rate).quantize(_KINA, rounding=ROUND_HALF_UP)
            total_repayable = amount + interest_amount
            return {
                "category": category,
                "amount": amount,
                "interest_amount": interest_amount,
                "total_repayable": total_repayable,
                "term_days": PRIME_TERM_DAYS,
                # Nominal flat rate for the tier (e.g. 0.40 = 40% over the
                # term) - NOT interest_amount/amount, which drifts slightly
                # from the nominal rate once interest is rounded to whole Kina.
                "rate": rate,
                "pricing_version_id": version_id,
            }

    # Unreachable given the K100-K1,000 gate above and contiguous tiers,
    # but fail loudly rather than silently mis-price if that invariant ever breaks.
    raise ServiceError(f"No PRIME tier configured for amount {amount}.")
