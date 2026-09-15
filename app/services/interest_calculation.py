"""Interest Calculation module - standard amortization.

The annual rate is configurable (``DEFAULT_ANNUAL_INTEREST_RATE`` env var / config),
never hardcoded here. A loan officer may pass an override rate at decision time;
whatever rate is used is stored on ``Loan.interest_rate``.
"""

from decimal import ROUND_HALF_UP, Decimal

from app.models.enums import RepaymentFrequency

_CENTS = Decimal("0.01")

# Payment periods per year for each frequency (calendar-based).
PERIODS_PER_YEAR = {
    RepaymentFrequency.WEEKLY: 52,
    RepaymentFrequency.BIWEEKLY: 26,
    RepaymentFrequency.MONTHLY: 12,
}


def installment_count(term_months: int, frequency: RepaymentFrequency) -> int:
    """Number of installments for a term expressed in months."""
    per_year = PERIODS_PER_YEAR[frequency]
    return max(1, round(term_months * per_year / 12))


def amortize(
    principal,
    annual_rate,
    term_months: int,
    frequency: RepaymentFrequency = RepaymentFrequency.MONTHLY,
) -> dict:
    """Compute the level installment and totals for an amortizing loan.

    installment = P * r / (1 - (1 + r)^-n)      (r = 0  ->  P / n)
    """
    p = Decimal(str(principal))
    annual = Decimal(str(annual_rate))
    n = installment_count(term_months, frequency)
    per_year = PERIODS_PER_YEAR[frequency]
    r = annual / Decimal(per_year)

    if r == 0:
        raw_installment = p / Decimal(n)
    else:
        factor = Decimal(1) - (Decimal(1) + r) ** (-n)
        raw_installment = p * r / factor

    installment = raw_installment.quantize(_CENTS, rounding=ROUND_HALF_UP)
    total_repayable = (installment * n).quantize(_CENTS, rounding=ROUND_HALF_UP)
    total_interest = (total_repayable - p).quantize(_CENTS, rounding=ROUND_HALF_UP)

    return {
        "principal": p,
        "annual_rate": annual,
        "periodic_rate": r,
        "periods_per_year": per_year,
        "installment_count": n,
        "installment_amount": installment,
        "total_repayable": total_repayable,
        "total_interest": total_interest,
    }


def principal_for_installment(
    installment_amount,
    annual_rate,
    term_months: int,
    frequency: RepaymentFrequency = RepaymentFrequency.MONTHLY,
) -> Decimal:
    """Inverse of ``amortize()``: the principal whose level installment equals
    ``installment_amount`` at the given rate/term/frequency.

    Used by credit_evaluation.py to turn an affordability ceiling (how big an
    installment the applicant can carry) back into a loan-amount ceiling.
    """
    installment = Decimal(str(installment_amount))
    if installment <= 0:
        return Decimal("0.00")

    annual = Decimal(str(annual_rate))
    n = installment_count(term_months, frequency)
    per_year = PERIODS_PER_YEAR[frequency]
    r = annual / Decimal(per_year)

    if r == 0:
        principal = installment * Decimal(n)
    else:
        factor = Decimal(1) - (Decimal(1) + r) ** (-n)
        principal = installment * factor / r

    return principal.quantize(_CENTS, rounding=ROUND_HALF_UP)
