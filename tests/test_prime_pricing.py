"""PRIME pricing - authoritative tier/interest/term calculation."""

from decimal import Decimal

import pytest

from app.services import prime_pricing
from app.services.errors import ServiceError


def test_k500_is_prime_2_with_k200_interest_and_k700_total():
    result = prime_pricing.calculate_prime(500)
    assert result["category"] == "PRIME 2"
    assert result["amount"] == Decimal("500")
    assert result["interest_amount"] == Decimal("200")
    assert result["total_repayable"] == Decimal("700")
    assert result["term_days"] == 14


@pytest.mark.parametrize(
    "amount,category,interest,total",
    [
        (100, "PRIME 1", 50, 150),
        (300, "PRIME 1", 150, 450),
        (301, "PRIME 2", 120, 421),
        (700, "PRIME 2", 280, 980),
        (701, "PRIME 3", 245, 946),
        (1000, "PRIME 3", 350, 1350),
    ],
)
def test_tier_boundaries(amount, category, interest, total):
    result = prime_pricing.calculate_prime(amount)
    assert result["category"] == category
    assert result["interest_amount"] == Decimal(interest)
    assert result["total_repayable"] == Decimal(total)
    assert result["term_days"] == 14


@pytest.mark.parametrize("amount", [0, -50, 99, 1001, 5000])
def test_amounts_outside_k100_k1000_are_rejected(amount):
    with pytest.raises(ServiceError):
        prime_pricing.calculate_prime(amount)


def test_above_k1000_error_names_the_ceiling():
    with pytest.raises(ServiceError, match=r"K1,000"):
        prime_pricing.calculate_prime(1001)


def test_non_whole_kina_amount_is_rejected():
    with pytest.raises(ServiceError, match="whole-Kina"):
        prime_pricing.calculate_prime("100.50")


def test_non_numeric_amount_is_rejected():
    with pytest.raises(ServiceError):
        prime_pricing.calculate_prime("not-a-number")
