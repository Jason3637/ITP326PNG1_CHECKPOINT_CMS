"""PRIME pricing - the authoritative, single-source-of-truth calculation for
Prime's Vault's fixed-tier micro-loan product.

PRIME is a flat-fee, 14-day, single-repayment product covering K100-K1,000.
The above-K1,000 tier is a distinct, larger-loan product that is NOT being
activated yet - amounts above K1,000 are rejected here, not silently priced.

This is deliberately the only place tier boundaries/rates are defined, so
both the application-submit flow and any future pricing-preview endpoint
call the same function and can never drift apart.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from .errors import ServiceError

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

# (category, min_amount, max_amount, flat_interest_rate). Public - read by
# credit_evaluation.py's affordability-cap inverse calculation.
TIERS: tuple[tuple[str, Decimal, Decimal, Decimal], ...] = (
    ("PRIME 1", Decimal("100"), Decimal("300"), Decimal("0.50")),
    ("PRIME 2", Decimal("301"), Decimal("700"), Decimal("0.40")),
    ("PRIME 3", Decimal("701"), Decimal("1000"), Decimal("0.35")),
)


def calculate_prime(amount_requested) -> dict:
    """Price a PRIME loan for a requested whole-Kina amount.

    Returns {category, amount, interest_amount, total_repayable, term_days}.
    Raises ServiceError (400) for anything outside K100-K1,000 or with a
    fractional-Kina (toea) component - PRIME amounts are whole Kina only.
    """
    try:
        amount = Decimal(str(amount_requested))
    except (InvalidOperation, TypeError):
        raise ServiceError("amount_requested must be a number.")

    if amount != amount.to_integral_value():
        raise ServiceError("amount_requested must be a whole-Kina amount (no toea).")

    if amount < PRIME_MIN_AMOUNT or amount > PRIME_MAX_AMOUNT:
        raise ServiceError(
            f"amount_requested must be between K{PRIME_MIN_AMOUNT:,.0f} and "
            f"K{PRIME_MAX_AMOUNT:,.0f}. Loans above K{PRIME_MAX_AMOUNT:,.0f} are "
            "a separate product that is not currently being offered."
        )

    for category, tier_min, tier_max, rate in TIERS:
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
            }

    # Unreachable given the K100-K1,000 gate above and contiguous tiers,
    # but fail loudly rather than silently mis-price if that invariant ever breaks.
    raise ServiceError(f"No PRIME tier configured for amount {amount}.")
