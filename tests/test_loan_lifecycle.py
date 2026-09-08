"""One full loan lifecycle: apply -> review -> approve -> repay -> complete."""

from decimal import Decimal

import pytest

from app.services import interest_calculation
from app.models.enums import RepaymentFrequency


def test_apply_approve_repay_completes_loan(client, make_user, auth_header):
    customer = make_user("customer")
    officer = make_user("loan_officer")
    ch = auth_header(customer)
    oh = auth_header(officer)

    # apply (1-month term -> a single installment, so one payment completes it)
    r = client.post(
        "/api/loans/apply",
        headers=ch,
        json={
            "amount_requested": 1000,
            "purpose": "Inventory",
            "term_months": 1,
            "repayment_frequency": "monthly",
        },
    )
    assert r.status_code == 201, r.get_json()
    application = r.get_json()
    app_id = application["id"]
    assert application["status"] in ("pending", "under_review")
    assert application["credit_evaluation_result"]["algorithm"] == "placeholder-v1"

    # officer sees it in the review queue
    r = client.get("/api/loans/applications", headers=oh)
    assert r.status_code == 200
    assert any(a["id"] == app_id for a in r.get_json()["applications"])

    # approve -> loan + schedule created
    r = client.post(
        f"/api/loans/applications/{app_id}/decision",
        headers=oh,
        json={"decision": "approve", "note": "ok"},
    )
    assert r.status_code == 200, r.get_json()
    result = r.get_json()
    loan = result["loan"]
    assert result["application"]["status"] == "approved"
    assert loan["status"] == "active"

    schedule = loan["repayment_schedule"]
    assert len(schedule) == 1
    assert schedule[0]["status"] == "upcoming"
    # schedule sums exactly to total repayable
    assert sum(Decimal(str(s["amount_due"])) for s in schedule) == Decimal(
        str(loan["total_repayable"])
    )

    r = client.get("/api/accounts/summary", headers=ch)
    assert r.get_json()["counts"]["active"] == 1

    # repay the full installment
    from app.models import RepaymentSchedule

    row = RepaymentSchedule.query.filter_by(loan_id=loan["id"]).first()
    r = client.post(
        "/api/payments/repay",
        headers=ch,
        json={
            "repayment_schedule_id": row.id,
            "amount": loan["installment_amount"],
            "payment_method": "cash",
        },
    )
    assert r.status_code == 201, r.get_json()
    pay = r.get_json()
    assert pay["installment"]["status"] == "paid"
    assert pay["loan_completed"] is True
    assert pay["loan_status"] == "completed"

    # dashboard/account now reflect completion
    r = client.get("/api/accounts/summary", headers=ch)
    counts = r.get_json()["counts"]
    assert counts["active"] == 0 and counts["completed"] == 1


def test_duplicate_open_application_is_rejected(client, make_user, auth_header):
    customer = make_user("customer")
    ch = auth_header(customer)
    body = {
        "amount_requested": 500,
        "term_months": 3,
        "repayment_frequency": "monthly",
    }
    assert client.post("/api/loans/apply", headers=ch, json=body).status_code == 201
    r = client.post("/api/loans/apply", headers=ch, json=body)
    assert r.status_code == 409


def test_amount_outside_limits_is_rejected(client, make_user, auth_header):
    ch = auth_header(make_user("customer"))
    r = client.post(
        "/api/loans/apply",
        headers=ch,
        json={"amount_requested": 999999, "term_months": 6, "repayment_frequency": "monthly"},
    )
    assert r.status_code == 400


@pytest.mark.parametrize(
    "freq,expected_n",
    [
        (RepaymentFrequency.MONTHLY, 6),
        (RepaymentFrequency.BIWEEKLY, 13),
        (RepaymentFrequency.WEEKLY, 26),
    ],
)
def test_amortization_installment_counts(freq, expected_n):
    terms = interest_calculation.amortize(1200, "0.18", 6, freq)
    assert terms["installment_count"] == expected_n
    assert terms["total_repayable"] > Decimal("1200")
    assert terms["total_interest"] == terms["total_repayable"] - Decimal("1200")
