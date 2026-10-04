"""One full PRIME loan lifecycle: apply -> officer review -> recommend ->
admin review -> approve -> disburse -> report payment -> verify -> paid."""

from decimal import Decimal

import pytest

import _workflow


def test_apply_through_full_chain_to_paid(client, make_user, auth_header, apply_payload):
    customer = make_user("customer")
    officer = make_user("loan_officer")
    admin = make_user("admin")
    ch = auth_header(customer)
    oh = auth_header(officer)
    ah = auth_header(admin)

    # apply -> SUBMITTED (K500 -> PRIME 2, K200 interest, K700 total, per spec)
    r = client.post("/api/loans/apply", headers=ch, json=apply_payload(amount_requested=500))
    assert r.status_code == 201, r.get_json()
    application = r.get_json()
    app_id = application["id"]
    assert application["status"] == "submitted"
    assert application["prime_category"] == "PRIME 2"
    assert application["pricing"]["interest_amount"] == 200
    assert application["pricing"]["total_repayable"] == 700
    assert "credit_evaluation_result" not in application, "advisory score is staff-only"

    # officer sees it in the review queue
    r = client.get("/api/loans/applications", headers=oh)
    assert r.status_code == 200
    assert any(a["id"] == app_id for a in r.get_json()["applications"])

    # SUBMITTED -> OFFICER_REVIEW
    r = client.post(f"/api/loans/applications/{app_id}/officer-review", headers=oh)
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["status"] == "officer_review"

    # OFFICER_REVIEW -> RECOMMENDED_FOR_APPROVAL
    r = _workflow.recommend(client, app_id, oh, comments="Looks good.")
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["application"]["status"] == "recommended_for_approval"

    # loan_officer cannot skip straight to admin-review or decide
    assert client.post(f"/api/loans/applications/{app_id}/admin-review", headers=oh).status_code == 403
    assert (
        client.post(
            f"/api/loans/applications/{app_id}/decision", headers=oh, json={"decision": "approve"}
        ).status_code
        == 403
    )

    # RECOMMENDED_FOR_APPROVAL -> ADMIN_REVIEW (admin only)
    r = client.post(f"/api/loans/applications/{app_id}/admin-review", headers=ah)
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["status"] == "admin_review"

    # ADMIN_REVIEW -> APPROVED -> AWAITING_DISBURSEMENT
    r = client.post(
        f"/api/loans/applications/{app_id}/decision",
        headers=ah,
        json={"decision": "approve", "note": "ok"},
    )
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["application"]["status"] == "awaiting_disbursement"

    # no Loan exists yet - disbursement is a separate, later step
    r = client.get("/api/loans/mine", headers=ch)
    assert r.get_json()["count"] == 0

    # AWAITING_DISBURSEMENT -> disburse (admin only) -> Loan created, ACTIVE
    r = client.post(
        f"/api/loans/applications/{app_id}/disburse",
        headers=ah,
        json={"method": "cash_on_hand", "method_reference": "voucher-1"},
    )
    assert r.status_code == 200, r.get_json()
    result = r.get_json()
    loan = result["loan"]
    assert loan["status"] == "active"
    assert loan["term_days"] == 14
    assert loan["total_repayable"] == 700
    assert loan["disbursement"]["method"] == "cash_on_hand"

    schedule = loan["repayment_schedule"]
    assert len(schedule) == 1
    assert schedule[0]["status"] == "upcoming"
    assert Decimal(str(schedule[0]["amount_due"])) == Decimal("700")

    r = client.get("/api/accounts/summary", headers=ch)
    assert r.get_json()["counts"]["active"] == 1

    # report the full payment - ledger must NOT move yet
    from app.models import RepaymentSchedule

    row = RepaymentSchedule.query.filter_by(loan_id=loan["id"]).first()
    r = client.post(
        "/api/payments/repay",
        headers=ch,
        json={
            "repayment_schedule_id": row.id,
            "amount": loan["total_repayable"],
            "payment_method": "cash",
        },
    )
    assert r.status_code == 201, r.get_json()
    txn = r.get_json()["transaction"]
    assert txn["status"] == "reported"
    txn_id = txn["id"]

    r = client.get("/api/accounts/summary", headers=ch)
    # ledger untouched by a mere report - loan is still just "active", not paid
    assert r.get_json()["counts"]["active"] == 1
    assert r.get_json()["counts"]["paid"] == 0

    # loan_officer cannot verify - only admin makes the ledger-affecting call
    r = client.post(f"/api/payments/{txn_id}/verify", headers=oh, json={"decision": "verified"})
    assert r.status_code == 403

    # admin verifies -> only now does the ledger move
    r = client.post(
        f"/api/payments/{txn_id}/verify", headers=ah, json={"decision": "verified"}
    )
    assert r.status_code == 200, r.get_json()
    pay = r.get_json()
    assert pay["installment"]["status"] == "paid"
    assert pay["loan_completed"] is True
    assert pay["loan_status"] == "closed", "paid in full closes automatically"

    r = client.get("/api/accounts/summary", headers=ch)
    counts = r.get_json()["counts"]
    assert counts["active"] == 0 and counts["closed"] == 1


def test_only_admin_can_reject_early(client, make_user, auth_header, apply_payload):
    """A loan officer can only RECOMMEND rejection; the early-exit reject is admin-only."""
    customer = make_user("customer")
    officer = make_user("loan_officer")
    admin = make_user("admin")
    ch, oh, ah = auth_header(customer), auth_header(officer), auth_header(admin)

    r = client.post("/api/loans/apply", headers=ch, json=apply_payload())
    app_id = r.get_json()["id"]
    client.post(f"/api/loans/applications/{app_id}/officer-review", headers=oh)

    r = client.post(
        f"/api/loans/applications/{app_id}/reject", headers=oh, json={"note": "Ineligible."}
    )
    assert r.status_code == 403

    r = client.post(
        f"/api/loans/applications/{app_id}/reject", headers=ah, json={"note": "Ineligible."}
    )
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["status"] == "rejected"


def test_customer_action_required_round_trip(client, make_user, auth_header, apply_payload):
    customer = make_user("customer")
    officer = make_user("loan_officer")
    ch, oh = auth_header(customer), auth_header(officer)

    r = client.post("/api/loans/apply", headers=ch, json=apply_payload())
    app_id = r.get_json()["id"]
    client.post(f"/api/loans/applications/{app_id}/officer-review", headers=oh)

    # at least one request item is required
    r = client.post(f"/api/loans/applications/{app_id}/request-action", headers=oh, json={})
    assert r.status_code == 400

    r = _workflow.request_info(client, app_id, oh, reason="Please confirm your employer.")
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["status"] == "customer_action_required"

    # resuming without the customer cancels the open request, so a reason is required
    r = client.post(f"/api/loans/applications/{app_id}/resume-review", headers=oh, json={})
    assert r.status_code == 400
    r = client.post(
        f"/api/loans/applications/{app_id}/resume-review",
        headers=oh,
        json={"reason": "Confirmed by phone."},
    )
    assert r.status_code == 200, r.get_json()
    assert [q["status"] for q in r.get_json()["information_requests"]] == ["cancelled"]
    assert r.get_json()["status"] == "officer_review"


def test_duplicate_open_application_is_rejected(client, make_user, auth_header, apply_payload):
    ch = auth_header(make_user("customer"))
    body = apply_payload()
    assert client.post("/api/loans/apply", headers=ch, json=body).status_code == 201
    r = client.post("/api/loans/apply", headers=ch, json=body)
    assert r.status_code == 409


@pytest.mark.parametrize("amount", [50, 1500])
def test_amount_outside_prime_range_is_rejected(client, make_user, auth_header, apply_payload, amount):
    ch = auth_header(make_user("customer"))
    r = client.post("/api/loans/apply", headers=ch, json=apply_payload(amount_requested=amount))
    assert r.status_code == 400


def test_apply_requires_at_least_one_referee(client, make_user, auth_header, apply_payload):
    ch = auth_header(make_user("customer"))
    r = client.post("/api/loans/apply", headers=ch, json=apply_payload(referees=[]))
    assert r.status_code == 400


def test_apply_requires_accepting_current_terms(client, make_user, auth_header, apply_payload):
    ch = auth_header(make_user("customer"))
    r = client.post("/api/loans/apply", headers=ch, json=apply_payload(accept_terms=False))
    assert r.status_code == 400


def test_bsp_mobile_banking_requires_account_reference(client, make_user, auth_header, apply_payload):
    ch = auth_header(make_user("customer"))
    r = client.post(
        "/api/loans/apply",
        headers=ch,
        json=apply_payload(disbursement_method_requested="bsp_mobile_banking"),
    )
    assert r.status_code == 400
