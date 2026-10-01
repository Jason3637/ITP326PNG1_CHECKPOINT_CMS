"""Credit Evaluation - interim model (app/services/credit_evaluation.py).

Most cases call `evaluate()` directly against a hand-built `LoanApplication`
row (status=SUBMITTED, matching the invariant `evaluate()` relies on - see
its docstring) so each factor can be isolated. A couple of end-to-end cases
go through POST /api/loans/apply to prove the wiring (request -> service ->
stored result) actually works together, and that it never influences
`status` (item 5's decoupling), not just the scoring math in isolation.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.extensions import db
from app.models import Loan, LoanApplication, PaymentTransaction
from app.models.enums import (
    LoanApplicationStatus,
    LoanStatus,
    PaymentStatus,
    RepaymentFrequency,
    RepaymentStatus,
)
from app.services import credit_evaluation, prime_pricing, repayments_scheduler
from app.services.interest_calculation import amortize

AMOUNT = Decimal("150")  # PRIME 1 (K100-K300): well under the 50%-of-ceiling ratio penalty


def _open_application(user, **overrides):
    """A SUBMITTED LoanApplication for `user` - what evaluate() looks up."""
    kwargs = dict(
        user_id=user.id,
        amount_requested=AMOUNT,
        status=LoanApplicationStatus.SUBMITTED,
    )
    kwargs.update(overrides)
    application = LoanApplication(**kwargs)
    db.session.add(application)
    db.session.flush()
    return application


def _settled_loan(user, *, installment_paid_days_after_due: int | None):
    """A single-installment (legacy amortized-product) loan for `user`, due
    30 days ago - only used to exercise the repayment-history signal, which
    reads Loan/RepaymentSchedule generically regardless of which product
    created them.

    `installment_paid_days_after_due=None` leaves it unpaid (currently
    overdue). Otherwise it's paid that many days after (negative = early).
    """
    application = LoanApplication(
        user_id=user.id,
        amount_requested=Decimal("100"),
        status=LoanApplicationStatus.APPROVED,
    )
    db.session.add(application)
    db.session.flush()

    terms = amortize(100, "0.18", 1, RepaymentFrequency.MONTHLY)
    disbursed = datetime.now(timezone.utc) - timedelta(days=60)
    loan = Loan(
        application_id=application.id,
        user_id=user.id,
        principal_amount=Decimal("100"),
        interest_rate=Decimal("0.18"),
        term_months=1,
        monthly_payment=terms["installment_amount"],
        total_repayable=terms["total_repayable"],
        status=LoanStatus.PAID if installment_paid_days_after_due is not None else LoanStatus.ACTIVE,
        disbursed_at=disbursed,
    )
    db.session.add(loan)
    db.session.flush()

    [row] = repayments_scheduler.generate_schedule(
        loan, RepaymentFrequency.MONTHLY, start_date=disbursed.date()
    )
    assert row.due_date < date.today(), "fixture bug: due date must already be in the past"

    if installment_paid_days_after_due is not None:
        row.amount_paid = row.amount_due
        row.status = RepaymentStatus.PAID
        paid_at = datetime.combine(
            row.due_date + timedelta(days=installment_paid_days_after_due),
            datetime.min.time(),
            tzinfo=timezone.utc,
        )
        txn = PaymentTransaction(
            loan_id=loan.id,
            repayment_schedule_id=row.id,
            amount=row.amount_due,
            payment_method="cash",
            payment_date=paid_at.date(),
            status=PaymentStatus.VERIFIED,
            paid_at=paid_at,
        )
        db.session.add(txn)
    db.session.flush()
    return loan


# ============================================================== income
def test_missing_income_is_insufficient_data_and_ineligible(app, make_user):
    user = make_user("customer")
    _open_application(user, employment_status="employed")
    result = credit_evaluation.evaluate(user, AMOUNT)

    assert result["insufficient_data"] is True
    assert result["eligible"] is False  # hard gate, independent of score
    assert any("No income information" in r for r in result["reasons"])


def test_income_at_exact_minimum_is_not_penalized(app, make_user):
    user = make_user("customer")
    _open_application(user, monthly_income=Decimal("200"), employment_status="employed")
    result = credit_evaluation.evaluate(user, AMOUNT)

    assert result["insufficient_data"] is False
    assert not any("below the minimum" in r for r in result["reasons"])
    assert result["eligible"] is True


def test_income_just_below_minimum_is_penalized(app, make_user):
    at_min = make_user("customer")
    _open_application(at_min, monthly_income=Decimal("200"), employment_status="employed")
    baseline = credit_evaluation.evaluate(at_min, AMOUNT)

    below_min = make_user("customer")
    _open_application(below_min, monthly_income=Decimal("199.99"), employment_status="employed")
    result = credit_evaluation.evaluate(below_min, AMOUNT)

    assert any("below the minimum required" in r for r in result["reasons"])
    assert result["score"] == baseline["score"] - 40


# ========================================================== employment
def test_unemployed_status_is_penalized(app, make_user):
    user = make_user("customer")
    _open_application(user, monthly_income=Decimal("500"), employment_status="unemployed")
    result = credit_evaluation.evaluate(user, AMOUNT)

    assert any("no current employment" in r for r in result["reasons"])


def test_missing_employment_status_is_penalized_but_not_a_hard_gate(app, make_user):
    user = make_user("customer")
    _open_application(user, monthly_income=Decimal("500"))  # employment_status omitted
    result = credit_evaluation.evaluate(user, AMOUNT)

    assert result["insufficient_data"] is False  # income alone is the hard gate
    assert any("Employment status not provided" in r for r in result["reasons"])


# ================================================== debt-to-income ratio
# PRIME has no ongoing monthly installment - the whole total_repayable
# (K225 for AMOUNT=K150, PRIME 1 @ 50%: 150 + round(150*0.5)) is treated as
# the "new obligation" (see credit_evaluation.py's DTI comment).
def test_low_debt_to_income_ratio_has_no_penalty(app, make_user):
    user = make_user("customer")
    _open_application(
        user, monthly_income=Decimal("2000"), employment_status="employed",
        existing_monthly_debt=Decimal("0"),
    )
    result = credit_evaluation.evaluate(user, AMOUNT)
    assert not any("Debt-to-income" in r for r in result["reasons"])


def test_debt_to_income_ratio_approaching_max_is_a_soft_penalty(app, make_user):
    # new_obligation/income ~ 0.346 (between 0.8*0.40=0.32 and 0.40).
    user = make_user("customer")
    _open_application(user, monthly_income=Decimal("650"), employment_status="employed")
    result = credit_evaluation.evaluate(user, AMOUNT)
    assert any("approaching the maximum" in r for r in result["reasons"])
    assert not any("exceeds the maximum" in r for r in result["reasons"])


def test_debt_to_income_ratio_over_max_is_a_hard_penalty(app, make_user):
    user = make_user("customer")
    _open_application(
        user, monthly_income=Decimal("100"), employment_status="employed",
        existing_monthly_debt=Decimal("50"),
    )
    result = credit_evaluation.evaluate(user, AMOUNT)
    assert any("exceeds the maximum allowed" in r for r in result["reasons"])


# ============================================= affordability-based cap
def test_max_eligible_amount_is_not_tightened_when_affordability_is_looser(app, make_user):
    """The other direction of the affordability-cap test: with high income,
    the DTI-based cap comfortably exceeds the score-linear cap, so the
    score-linear cap should still be the one reported (min() picks it)."""
    user = make_user("customer")
    _open_application(user, monthly_income=Decimal("100000"), employment_status="employed")
    result = credit_evaluation.evaluate(user, AMOUNT)

    score_based_cap = float(prime_pricing.PRIME_MAX_AMOUNT * Decimal(result["score"]) / Decimal(100))
    assert result["max_eligible_amount"] == pytest.approx(score_based_cap, abs=0.01)


# ==================================================== membership tenure
def test_brand_new_member_is_penalized(app, make_user):
    user = make_user("customer")  # created_at ~ now
    _open_application(user, monthly_income=Decimal("2000"), employment_status="employed")
    result = credit_evaluation.evaluate(user, AMOUNT)
    assert any("less than 30 days old" in r for r in result["reasons"])


def test_long_tenure_member_gets_a_small_bonus(app, make_user):
    user = make_user("customer")
    user.created_at = datetime.now(timezone.utc) - timedelta(days=400)
    db.session.commit()
    _open_application(user, monthly_income=Decimal("2000"), employment_status="employed")
    result = credit_evaluation.evaluate(user, AMOUNT)
    assert any("Member for over a year" in r for r in result["reasons"])


# =================================================== repayment history
def test_on_time_repayment_history_is_rewarded(app, make_user):
    user = make_user("customer")
    _settled_loan(user, installment_paid_days_after_due=-2)  # paid 2 days early
    _open_application(user, monthly_income=Decimal("2000"), employment_status="employed")
    result = credit_evaluation.evaluate(user, AMOUNT)
    assert any("Strong on-time repayment history" in r for r in result["reasons"])


def test_currently_overdue_installment_is_penalized(app, make_user):
    user = make_user("customer")
    _settled_loan(user, installment_paid_days_after_due=None)  # never paid, past due
    _open_application(user, monthly_income=Decimal("2000"), employment_status="employed")
    result = credit_evaluation.evaluate(user, AMOUNT)
    assert any("currently overdue" in r for r in result["reasons"])


def test_no_prior_loans_is_neutral(app, make_user):
    user = make_user("customer")
    _open_application(user, monthly_income=Decimal("2000"), employment_status="employed")
    result = credit_evaluation.evaluate(user, AMOUNT)
    assert not any("repayment history" in r.lower() for r in result["reasons"])


# ============================================= affordability-based cap
def test_max_eligible_amount_is_capped_by_affordability_when_tighter(app, make_user):
    user = make_user("customer")
    # Income chosen so the DTI-based affordability cap (~K266) binds below
    # the score-linear cap (~K900), but is still nonzero (large enough to
    # afford at least the cheapest PRIME tier).
    _open_application(user, monthly_income=Decimal("1000"), employment_status="employed")
    result = credit_evaluation.evaluate(user, AMOUNT)

    score_based_cap = float(prime_pricing.PRIME_MAX_AMOUNT * Decimal(result["score"]) / Decimal(100))
    assert 0 < result["max_eligible_amount"] < score_based_cap


# ==================================================== algorithm labeling
def test_algorithm_is_labeled_interim_not_placeholder(app, make_user):
    user = make_user("customer")
    _open_application(user, monthly_income=Decimal("2000"), employment_status="employed")
    result = credit_evaluation.evaluate(user, AMOUNT)
    assert result["algorithm"] == "interim-v2"
    assert "criteria_checked" in result and "minimum_income" in result["criteria_checked"]


def test_evaluate_without_a_matching_application_degrades_gracefully(app, make_user):
    """Calling evaluate() with no open LoanApplication row (e.g. a bare unit
    test) must not error - it should behave as if income/employment were
    simply never provided."""
    user = make_user("customer")
    result = credit_evaluation.evaluate(user, AMOUNT)
    assert result["insufficient_data"] is True


# ============================================== end-to-end, via the API
def test_apply_endpoint_stores_and_returns_the_new_fields(client, make_user, auth_header, apply_payload):
    ch = auth_header(make_user("customer"))
    r = client.post(
        "/api/loans/apply",
        headers=ch,
        json=apply_payload(
            amount_requested=150,
            monthly_income=2000,
            employment_status="employed",
            existing_monthly_debt=0,
        ),
    )
    assert r.status_code == 201, r.get_json()
    body = r.get_json()
    assert body["monthly_income"] == 2000.0
    assert body["employment_status"] == "employed"
    assert body["existing_monthly_debt"] == 0.0
    assert "credit_evaluation_result" not in body, "advisory score is staff-only"
    stored = db.session.get(LoanApplication, body["id"]).credit_evaluation_result
    assert stored["algorithm"] == "interim-v2"


def test_credit_evaluation_never_influences_status(client, make_user, auth_header, apply_payload):
    """Item 5: credit evaluation is purely advisory. An application with no
    income (insufficient_data=True, score heavily penalized) lands on
    exactly the same status as one with strong income - only a human
    (loan_processing's state machine) ever moves status.
    """
    with_income = auth_header(make_user("customer"))
    r = client.post(
        "/api/loans/apply",
        headers=with_income,
        json=apply_payload(monthly_income=2000, employment_status="employed"),
    )
    assert r.status_code == 201, r.get_json()
    stored = db.session.get(LoanApplication, r.get_json()["id"]).credit_evaluation_result
    assert stored["insufficient_data"] is False
    assert r.get_json()["status"] == "submitted"

    without_income = auth_header(make_user("customer"))
    r = client.post("/api/loans/apply", headers=without_income, json=apply_payload())
    assert r.status_code == 201, r.get_json()
    stored = db.session.get(LoanApplication, r.get_json()["id"]).credit_evaluation_result
    assert stored["insufficient_data"] is True
    assert r.get_json()["status"] == "submitted"
