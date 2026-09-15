"""Credit Evaluation module.

================================  INTERIM MODEL  =================================
`evaluate()` implements a materially more complete rule set than the original
placeholder, built from the criteria categories a real lender would use
(minimum income, employment status, debt-to-income ratio, membership tenure,
repayment history), but the exact thresholds below are still ENGINEERING
GUESSES, not Prime's Vault's confirmed lending policy. Nobody at the client has
signed off on "PGK 200/month minimum income" or "40% max DTI" - those live in
``MIN_MONTHLY_INCOME`` / ``MAX_DEBT_TO_INCOME_RATIO`` (admin-tunable via
``PUT /api/admin/parameters``, see ``app/services/parameters.py``) specifically
so they can be corrected without a code change once the client provides real
numbers. The stored result records `"algorithm": "interim-v2"` so evaluations
made under this model stay distinguishable from both the original
`"placeholder-v1"` runs and whatever the client-approved model eventually uses.

What is actually live right now (see `_score()` for the exact math):
  1. Requested amount vs. the configured min/max loan amount.
  2. Term length (longer terms score slightly lower - more uncertainty).
  3. Repayment history on the member's own past loans - both the coarse signal
     (any defaulted/active loan) and a finer on-time-payment ratio computed
     from `RepaymentSchedule.due_date` vs. the settling `PaymentTransaction`'s
     `paid_at`.
  4. Account standing (`User.is_active`).
  5. Minimum income - self-reported `LoanApplication.monthly_income`. Missing
     income is a HARD DISQUALIFIER (`insufficient_data`): there is no way to
     assess affordability without it, independent of the numeric score.
  6. Employment status - self-reported `LoanApplication.employment_status`;
     "unemployed" / "student" score lower, missing status scores lower still.
  7. Debt-to-income ratio - `(existing_monthly_debt + estimated new
     installment) / monthly_income`, estimated with the default interest rate
     since the real rate isn't set until an officer approves.
  8. Membership tenure - `User.created_at` age; very new accounts score
     slightly lower (no track record yet).

None of this is independently verified (no payslip upload, no employer
contact, no credit bureau integration) - it is entirely self-reported by the
applicant at submission time. It never auto-approves; a loan officer always
makes the final call (see `loan_processing.decide_application`).
====================================================================================
"""

from datetime import date, datetime, timezone
from decimal import Decimal

from app.models import Loan, LoanApplication
from app.models.enums import (
    EmploymentStatus,
    LoanApplicationStatus,
    LoanStatus,
    RepaymentFrequency,
    RepaymentStatus,
)

from . import interest_calculation, parameters

ALGORITHM = "interim-v2"

_CENTS = Decimal("0.01")


def _repayment_history_signal(user) -> tuple[int, str | None]:
    """Score adjustment from the member's installment-level payment history.

    Distinct from (and additional to) the coarser defaulted/active loan check
    in `_score()` below: this looks at *how* past installments were actually
    paid, not just the current loan-level status.
    """
    today = date.today()
    rows = [
        r
        for loan in Loan.query.filter_by(user_id=user.id).all()
        for r in loan.repayment_schedule
    ]
    due_rows = [r for r in rows if r.due_date <= today]
    if not due_rows:
        return 0, None  # no track record yet - neither rewarded nor punished

    still_overdue = sum(1 for r in due_rows if r.status != RepaymentStatus.PAID)
    if still_overdue:
        return -30, f"{still_overdue} installment(s) currently overdue."

    on_time = 0
    for r in due_rows:
        settled = max(
            (p for p in r.payments if p.paid_at is not None),
            key=lambda p: p.paid_at,
            default=None,
        )
        if settled is not None and settled.paid_at.date() <= r.due_date:
            on_time += 1
    ratio = on_time / len(due_rows)

    if ratio >= 0.9:
        return 10, "Strong on-time repayment history."
    if ratio >= 0.75:
        return 5, None
    if ratio < 0.5:
        return -20, "Weak repayment history - many installments paid late."
    return -5, None


def _score(
    user,
    application: LoanApplication | None,
    amount: Decimal,
    term_months: int,
    max_amount: Decimal,
) -> tuple[int, list[str], bool]:
    """Return (score 0-100, reasons[], insufficient_data)."""
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

    history_delta, history_note = _repayment_history_signal(user)
    score += history_delta
    if history_note:
        reasons.append(history_note)

    # 4. Account standing.
    if not user.is_active:
        score -= 100
        reasons.append("Member account is not active.")

    # 5 & 6. Income and employment (self-reported on the application).
    income = Decimal(str(application.monthly_income)) if application and application.monthly_income is not None else None
    employment = application.employment_status if application else None
    insufficient_data = income is None

    if income is None:
        score -= 15
        reasons.append(
            "No income information provided - affordability cannot be assessed."
        )
    else:
        min_income = parameters.get_value("min_monthly_income")
        if income < min_income:
            score -= 40
            reasons.append(
                f"Reported monthly income ({income}) is below the minimum "
                f"required ({min_income})."
            )

    if employment is None:
        score -= 10
        reasons.append("Employment status not provided.")
    elif employment == EmploymentStatus.UNEMPLOYED:
        score -= 30
        reasons.append("Applicant reports no current employment.")
    elif employment == EmploymentStatus.STUDENT:
        score -= 10
        reasons.append("Applicant is a student - limited independent income expected.")
    # EMPLOYED / SELF_EMPLOYED / RETIRED: neutral, no adjustment.

    # 7. Debt-to-income ratio (only computable when income is known).
    if income is not None and income > 0:
        existing_debt = (
            Decimal(str(application.existing_monthly_debt))
            if application and application.existing_monthly_debt is not None
            else Decimal("0")
        )
        rate = parameters.get_value("default_annual_interest_rate")
        frequency = application.repayment_frequency if application else None
        estimate = interest_calculation.amortize(
            amount, rate, term_months, frequency or RepaymentFrequency.MONTHLY
        )
        new_installment = estimate["installment_amount"]
        dti = (existing_debt + new_installment) / income
        max_dti = parameters.get_value("max_debt_to_income_ratio")

        if dti > max_dti:
            score -= 35
            reasons.append(
                f"Debt-to-income ratio ({dti:.0%}) exceeds the maximum allowed ({max_dti:.0%})."
            )
        elif dti > max_dti * Decimal("0.8"):
            score -= 10
            reasons.append("Debt-to-income ratio is approaching the maximum allowed.")

    # 8. Membership tenure.
    if user.created_at:
        tenure_days = (datetime.now(timezone.utc).date() - user.created_at.date()).days
        if tenure_days < 30:
            score -= 10
            reasons.append("Member account is less than 30 days old - limited track record.")
        elif tenure_days >= 365:
            score += 5
            reasons.append("Member for over a year.")

    return max(0, min(100, score)), reasons, insufficient_data


def evaluate(user, amount_requested, term_months: int) -> dict:
    """Produce an eligibility result dict to store on
    ``LoanApplication.credit_evaluation_result``.

    Signature intentionally unchanged from the placeholder version so
    ``loan_processing.submit_application()`` needs no changes: the newer
    per-application fields (income, employment, existing debt) are read by
    looking up the member's currently-open application here rather than
    threading extra parameters through. This relies on the invariant
    `submit_application()` already enforces - at most one PENDING/UNDER_REVIEW
    application per user - and on being called after that application has
    been flushed (so it's visible to this query) but before its status moves
    off PENDING. Called without a matching application (e.g. a standalone
    unit test), it degrades gracefully: income/employment are treated as
    missing, same as an applicant who left the form blank.
    """
    amount = Decimal(str(amount_requested))
    max_amount = parameters.get_value("max_loan_amount")
    min_amount = parameters.get_value("min_loan_amount")

    application = (
        LoanApplication.query.filter_by(
            user_id=user.id, status=LoanApplicationStatus.PENDING
        )
        .order_by(LoanApplication.id.desc())
        .first()
    )

    score, reasons, insufficient_data = _score(user, application, amount, term_months, max_amount)

    within_bounds = min_amount <= amount <= max_amount
    if not within_bounds and f"Requested amount exceeds the maximum ({max_amount})." not in reasons:
        reasons.append(f"Requested amount outside the allowed range ({min_amount}-{max_amount}).")

    eligible = bool(within_bounds and score >= 50 and not insufficient_data)

    # Affordability-based cap: the largest principal whose installment still
    # respects the DTI ceiling, given known income. Falls back to the
    # score-linear cap when income isn't known (nothing to base affordability
    # on) or isn't more restrictive.
    score_based_cap = (max_amount * (Decimal(score) / Decimal(100))).quantize(_CENTS)
    max_eligible = score_based_cap
    if application and application.monthly_income:
        income = Decimal(str(application.monthly_income))
        existing_debt = (
            Decimal(str(application.existing_monthly_debt))
            if application.existing_monthly_debt is not None
            else Decimal("0")
        )
        max_dti = parameters.get_value("max_debt_to_income_ratio")
        affordable_installment = max(Decimal("0"), income * max_dti - existing_debt)
        rate = parameters.get_value("default_annual_interest_rate")
        affordability_cap = interest_calculation.principal_for_installment(
            affordable_installment, rate, term_months, application.repayment_frequency
        )
        max_eligible = min(score_based_cap, affordability_cap)

    return {
        "algorithm": ALGORITHM,
        "disclaimer": (
            "Interim underwriting model - a materially more complete rule set "
            "than the original placeholder (income, employment, DTI, tenure, "
            "repayment history), but the thresholds are still engineering "
            "guesses. Replace with Prime's Vault's confirmed lending criteria "
            "once provided."
        ),
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "score": score,
        "eligible": eligible,
        "insufficient_data": insufficient_data,
        "requested_amount": float(amount),
        "max_eligible_amount": float(max_eligible),
        "reasons": reasons or ["No adverse factors detected."],
        "recommendation": "review" if eligible else "decline",
        "criteria_checked": [
            "requested_amount_vs_limits",
            "term_length",
            "repayment_history",
            "prior_loan_status",
            "account_standing",
            "minimum_income",
            "employment_status",
            "debt_to_income_ratio",
            "membership_tenure",
        ],
    }


def suggested_status(evaluation: dict) -> LoanApplicationStatus:
    """Where a freshly submitted application should land given its evaluation."""
    return (
        LoanApplicationStatus.UNDER_REVIEW
        if evaluation.get("eligible")
        else LoanApplicationStatus.PENDING
    )
