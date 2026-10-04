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
applicant at submission time.

DECOUPLED FROM DECISION-MAKING: this module never sets
`LoanApplication.status`, never auto-approves, and never auto-rejects. Its
only effect is `evaluate()` populating `credit_evaluation_result` - a JSON
blob attached to the row purely for a Loan Officer / Administrator to read
during OFFICER_REVIEW / ADMIN_REVIEW. Every status transition, including the
final approve/reject call, is a human action in
`app.services.loan_processing` (see that module's state machine docstring).
====================================================================================
"""

from datetime import date, datetime, timezone
from decimal import ROUND_FLOOR, Decimal

from app.models import Loan, LoanApplication
from app.models.enums import (
    EmploymentStatus,
    LoanApplicationStatus,
    LoanClosureReason,
    LoanStatus,
    RepaymentStatus,
)

from . import parameters, prime_pricing

ALGORITHM = "interim-v2"

# Staff-facing (shown under the assessment on the Application Review
# screen). Served at read time by officer_views.credit_assessment(), so a
# wording change reaches results already stored on applications too.
# Drop the last sentence once the client confirms its lending policy.
DISCLAIMER = (
    "Advisory assessment only. It does not replace the judgment of the Loan "
    "Officer or Administrator. The assessment criteria are provisional until "
    "Prime's Vault's lending policy is finalised."
)

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
    max_amount: Decimal,
) -> tuple[int, list[str], bool]:
    """Return (score 0-100, reasons[], insufficient_data).

    No "term length" criterion: every PRIME application has the same fixed
    14-day term (app.services.prime_pricing.PRIME_TERM_DAYS), so term length
    carries zero discriminating signal between applicants under this product.
    """
    score = 100
    reasons: list[str] = []

    # 1. Requested amount relative to the PRIME ceiling (K1,000).
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

    # 2. Repayment history on this member's past loans.
    prior_loans = Loan.query.filter_by(user_id=user.id).all()
    if any(
        l.status == LoanStatus.CLOSED and l.closure_reason == LoanClosureReason.DEFAULTED
        for l in prior_loans
    ):
        score -= 50
        reasons.append("Member has a previously defaulted loan.")
    if any(l.status in (LoanStatus.ACTIVE, LoanStatus.OVERDUE) for l in prior_loans):
        score -= 25
        reasons.append("Member already has an active loan.")
    if prior_loans and all(
        l.status == LoanStatus.PAID
        or (l.status == LoanStatus.CLOSED and l.closure_reason == LoanClosureReason.PAID_IN_FULL)
        for l in prior_loans
    ):
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
    # PRIME has no ongoing monthly installment - it's a single lump-sum
    # repayment due in 14 days - so the whole total_repayable is treated as
    # the "new obligation" here (the conservative reading: can the member's
    # income cover it within one income cycle). Same interim-model caveat as
    # the rest of this module: not client-confirmed methodology.
    if income is not None and income > 0:
        existing_debt = (
            Decimal(str(application.existing_monthly_debt))
            if application and application.existing_monthly_debt is not None
            else Decimal("0")
        )
        new_obligation = prime_pricing.calculate_prime(amount)["total_repayable"]
        dti = (existing_debt + new_obligation) / income
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


def _max_prime_principal_for_budget(budget: Decimal) -> Decimal:
    """Largest whole-Kina PRIME principal whose total_repayable fits within
    `budget`. PRIME's tiered flat rate means this has no closed-form inverse
    (the rate itself depends on which tier the answer lands in), so this
    walks candidates per tier - cheap, since the whole range is <=901 values.
    """
    if budget < prime_pricing.PRIME_MIN_AMOUNT:
        return Decimal("0")
    best = Decimal("0")
    for _category, tier_min, tier_max, rate in prime_pricing.TIERS:
        # total_repayable = principal * (1 + rate) within a tier (before
        # whole-Kina rounding of the interest component).
        candidate = (budget / (Decimal("1") + rate)).to_integral_value(rounding=ROUND_FLOOR)
        candidate = min(candidate, tier_max)
        if candidate < tier_min:
            continue
        # Rounding of the interest component can occasionally push
        # total_repayable a Kina over budget; step down until it fits.
        while candidate >= tier_min:
            if prime_pricing.calculate_prime(candidate)["total_repayable"] <= budget:
                break
            candidate -= 1
        if candidate >= tier_min:
            best = max(best, candidate)
    return best


def evaluate(
    user,
    amount_requested,
    term_days: int = prime_pricing.PRIME_TERM_DAYS,
    *,
    application: LoanApplication | None = None,
) -> dict:
    """Produce an eligibility result dict to store on
    ``LoanApplication.credit_evaluation_result``. Purely advisory - see the
    module docstring; nothing here ever touches `LoanApplication.status`.

    `term_days` is accepted for forward compatibility but currently unused:
    every PRIME application has the same fixed 14-day term, so it carries no
    scoring signal (see `_score()`'s docstring).

    Pass `application` explicitly when re-evaluating an existing row whose
    status has already moved off SUBMITTED (e.g.
    loan_processing.respond_to_customer_action() re-scoring a
    CUSTOMER_ACTION_REQUIRED row after the customer updates their income).
    Omit it (the default) to fall back to looking up the member's
    just-flushed SUBMITTED row - what submit_application() relies on,
    calling this after flush() but before the row's status moves off
    SUBMITTED. Called with neither a matching application nor an explicit one
    (e.g. a standalone unit test), it degrades gracefully: income/employment
    are treated as missing, same as an applicant who left the form blank.
    """
    amount = Decimal(str(amount_requested))
    max_amount = prime_pricing.PRIME_MAX_AMOUNT
    min_amount = prime_pricing.PRIME_MIN_AMOUNT

    if application is None:
        application = (
            LoanApplication.query.filter_by(
                user_id=user.id, status=LoanApplicationStatus.SUBMITTED
            )
            .order_by(LoanApplication.id.desc())
            .first()
        )

    score, reasons, insufficient_data = _score(user, application, amount, max_amount)

    within_bounds = min_amount <= amount <= max_amount
    if not within_bounds and f"Requested amount exceeds the maximum ({max_amount})." not in reasons:
        reasons.append(f"Requested amount outside the allowed range ({min_amount}-{max_amount}).")

    eligible = bool(within_bounds and score >= 50 and not insufficient_data)

    # Affordability-based cap: the largest PRIME principal whose total
    # repayment still respects the DTI ceiling, given known income. Falls
    # back to the score-linear cap when income isn't known (nothing to base
    # affordability on) or isn't more restrictive.
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
        affordable_repayment = max(Decimal("0"), income * max_dti - existing_debt)
        affordability_cap = _max_prime_principal_for_budget(affordable_repayment)
        max_eligible = min(score_based_cap, affordability_cap)

    return {
        "algorithm": ALGORITHM,
        "disclaimer": DISCLAIMER,
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
            "repayment_history",
            "prior_loan_status",
            "account_standing",
            "minimum_income",
            "employment_status",
            "debt_to_income_ratio",
            "membership_tenure",
        ],
    }
