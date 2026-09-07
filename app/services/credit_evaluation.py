"""Credit Evaluation module.

============================  PLACEHOLDER ALGORITHM  ============================
`evaluate()` implements a deliberately simple, transparent first-pass rule set so
the rest of the loan pipeline has something to consume. It is NOT a real credit
model. Replace the scoring in `_score()` with Prime's Vault's actual lending
criteria (income verification, repayment history weighting, guarantor rules,
affordability ratios, etc.) once the client provides them. The stored result
records `"algorithm": "placeholder-v1"` so old evaluations remain identifiable
after the real model lands.
===============================================================================
"""

from datetime import datetime, timezone
from decimal import Decimal

from app.models import Loan
from app.models.enums import LoanApplicationStatus, LoanStatus

from . import parameters

ALGORITHM = "placeholder-v1"


def _score(user, amount: Decimal, term_months: int, max_amount: Decimal) -> tuple[int, list[str]]:
    """Return (score 0-100, reasons[]). Placeholder heuristics only."""
    score = 100
    reasons: list[str] = []

    # 1. Requested amount relative to the configured ceiling.
    ratio = float(amount / max_amount) if max_amount else 1.0
    if ratio > 1.0:
        score -= 60
        reasons.append(f"Requested amount exceeds the maximum ({max_amount}).")
    elif ratio > 0.75:
        score -= 20
        reasons.append("Requested amount is in the top 25% of the allowed range.")
    elif ratio > 0.5:
        score -= 10
        reasons.append("Requested amount is above half the allowed range.")

    # 2. Longer terms carry more uncertainty.
    if term_months > 36:
        score -= 15
        reasons.append("Term longer than 36 months.")
    elif term_months > 24:
        score -= 5

    # 3. Repayment history on this member's past loans.
    prior_loans = Loan.query.filter_by(user_id=user.id).all()
    if any(l.status == LoanStatus.DEFAULTED for l in prior_loans):
        score -= 50
        reasons.append("Member has a previously defaulted loan.")
    if any(l.status == LoanStatus.ACTIVE for l in prior_loans):
        score -= 25
        reasons.append("Member already has an active loan.")
    if prior_loans and all(l.status == LoanStatus.COMPLETED for l in prior_loans):
        score += 10
        reasons.append("Member has fully repaid all previous loans.")

    # 4. Account standing.
    if not user.is_active:
        score -= 100
        reasons.append("Member account is not active.")

    return max(0, min(100, score)), reasons


def evaluate(user, amount_requested, term_months: int) -> dict:
    """Produce an eligibility result dict to store on
    ``LoanApplication.credit_evaluation_result``.
    """
    amount = Decimal(str(amount_requested))
    max_amount = parameters.get_value("max_loan_amount")
    min_amount = parameters.get_value("min_loan_amount")

    score, reasons = _score(user, amount, term_months, max_amount)

    within_bounds = min_amount <= amount <= max_amount
    if not within_bounds and f"Requested amount exceeds the maximum ({max_amount})." not in reasons:
        reasons.append(f"Requested amount outside the allowed range ({min_amount}-{max_amount}).")

    eligible = bool(within_bounds and score >= 50)
    # Max the placeholder is willing to indicate as supportable.
    max_eligible = (max_amount * (Decimal(score) / Decimal(100))).quantize(Decimal("0.01"))

    return {
        "algorithm": ALGORITHM,
        "disclaimer": "Placeholder heuristic - not a validated credit model. "
        "To be replaced with the client's real lending criteria.",
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "score": score,
        "eligible": eligible,
        "requested_amount": float(amount),
        "max_eligible_amount": float(max_eligible),
        "reasons": reasons or ["No adverse factors detected."],
        "recommendation": "review" if eligible else "decline",
    }


def suggested_status(evaluation: dict) -> LoanApplicationStatus:
    """Where a freshly submitted application should land given its evaluation."""
    return (
        LoanApplicationStatus.UNDER_REVIEW
        if evaluation.get("eligible")
        else LoanApplicationStatus.PENDING
    )
