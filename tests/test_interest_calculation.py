"""interest_calculation.amortize() - the legacy amortized-product engine.

Dormant for the PRIME product (fixed 14-day bullet repayment - see
app/services/prime_pricing.py), but kept for the (currently inactive)
above-K1,000 product, so it stays covered on its own rather than through
the PRIME-based loan lifecycle test.
"""

from decimal import Decimal

import pytest

from app.models.enums import RepaymentFrequency
from app.services.interest_calculation import amortize


@pytest.mark.parametrize(
    "freq,expected_n",
    [
        (RepaymentFrequency.MONTHLY, 6),
        (RepaymentFrequency.BIWEEKLY, 13),
        (RepaymentFrequency.WEEKLY, 26),
    ],
)
def test_amortization_installment_counts(freq, expected_n):
    terms = amortize(1200, "0.18", 6, freq)
    assert terms["installment_count"] == expected_n
    assert terms["total_repayable"] > Decimal("1200")
    assert terms["total_interest"] == terms["total_repayable"] - Decimal("1200")
