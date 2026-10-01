"""Payment verification - the decoupling itself (item 4): a rejected report
never touches the ledger, the optional VERIFICATION_PENDING claim step
works, and only admin makes the actual verify/reject call (loan_officer may
only claim). The happy "verified" path is covered end-to-end in
test_loan_lifecycle.py's full-chain test.
"""

from decimal import Decimal

from app.extensions import db
from app.models import RepaymentSchedule


def _disbursed_loan(client, make_user, auth_header, apply_payload):
    """Apply -> officer-review -> recommend -> admin-review -> approve ->
    disburse, returning (loan_json, customer_auth_header, officer_auth_header,
    admin_auth_header).
    """
    customer = make_user("customer")
    officer = make_user("loan_officer")
    admin = make_user("admin")
    ch, oh, ah = auth_header(customer), auth_header(officer), auth_header(admin)

    r = client.post("/api/loans/apply", headers=ch, json=apply_payload(amount_requested=500))
    app_id = r.get_json()["id"]
    client.post(f"/api/loans/applications/{app_id}/officer-review", headers=oh)
    client.post(f"/api/loans/applications/{app_id}/recommend", headers=oh)
    client.post(f"/api/loans/applications/{app_id}/admin-review", headers=ah)
    client.post(
        f"/api/loans/applications/{app_id}/decision", headers=ah, json={"decision": "approve"}
    )
    r = client.post(
        f"/api/loans/applications/{app_id}/disburse", headers=ah, json={"method": "cash_on_hand"}
    )
    return r.get_json()["loan"], ch, oh, ah


def test_rejected_payment_never_touches_the_ledger(client, make_user, auth_header, apply_payload):
    loan, ch, oh, ah = _disbursed_loan(client, make_user, auth_header, apply_payload)
    row = RepaymentSchedule.query.filter_by(loan_id=loan["id"]).first()

    r = client.post(
        "/api/payments/repay",
        headers=ch,
        json={"repayment_schedule_id": row.id, "amount": loan["total_repayable"], "payment_method": "cash"},
    )
    txn_id = r.get_json()["transaction"]["id"]

    r = client.post(
        f"/api/payments/{txn_id}/verify", headers=ah, json={"decision": "rejected", "note": "No proof."}
    )
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["transaction"]["status"] == "rejected"

    db.session.refresh(row)
    assert row.status == "upcoming" or str(row.status) == "upcoming"
    assert Decimal(row.amount_paid) == Decimal("0")

    r = client.get("/api/accounts/summary", headers=ch)
    assert r.get_json()["counts"]["active"] == 1
    assert r.get_json()["counts"]["paid"] == 0


def test_rejecting_a_payment_requires_a_reason(client, make_user, auth_header, apply_payload):
    loan, ch, oh, ah = _disbursed_loan(client, make_user, auth_header, apply_payload)
    row = RepaymentSchedule.query.filter_by(loan_id=loan["id"]).first()

    r = client.post(
        "/api/payments/repay",
        headers=ch,
        json={"repayment_schedule_id": row.id, "amount": loan["total_repayable"], "payment_method": "cash"},
    )
    txn_id = r.get_json()["transaction"]["id"]

    r = client.post(f"/api/payments/{txn_id}/verify", headers=ah, json={"decision": "rejected"})
    assert r.status_code == 400


def test_loan_officer_can_claim_but_not_verify(client, make_user, auth_header, apply_payload):
    loan, ch, oh, ah = _disbursed_loan(client, make_user, auth_header, apply_payload)
    row = RepaymentSchedule.query.filter_by(loan_id=loan["id"]).first()

    r = client.post(
        "/api/payments/repay",
        headers=ch,
        json={"repayment_schedule_id": row.id, "amount": loan["total_repayable"], "payment_method": "cash"},
    )
    txn_id = r.get_json()["transaction"]["id"]

    r = client.post(f"/api/payments/{txn_id}/start-verification", headers=oh)
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["transaction"]["status"] == "verification_pending"

    r = client.post(f"/api/payments/{txn_id}/verify", headers=oh, json={"decision": "verified"})
    assert r.status_code == 403

    r = client.post(f"/api/payments/{txn_id}/verify", headers=ah, json={"decision": "verified"})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["transaction"]["status"] == "verified"
    assert r.get_json()["loan_completed"] is True


def test_already_verified_transaction_cannot_be_verified_again(client, make_user, auth_header, apply_payload):
    loan, ch, oh, ah = _disbursed_loan(client, make_user, auth_header, apply_payload)
    row = RepaymentSchedule.query.filter_by(loan_id=loan["id"]).first()

    r = client.post(
        "/api/payments/repay",
        headers=ch,
        json={"repayment_schedule_id": row.id, "amount": loan["total_repayable"], "payment_method": "cash"},
    )
    txn_id = r.get_json()["transaction"]["id"]
    client.post(f"/api/payments/{txn_id}/verify", headers=ah, json={"decision": "verified"})

    r = client.post(f"/api/payments/{txn_id}/verify", headers=ah, json={"decision": "verified"})
    assert r.status_code == 409


def test_customer_cannot_report_on_someone_elses_loan(client, make_user, auth_header, apply_payload):
    loan, _ch, _oh, _ah = _disbursed_loan(client, make_user, auth_header, apply_payload)
    other_customer = make_user("customer")
    other_ch = auth_header(other_customer)
    row = RepaymentSchedule.query.filter_by(loan_id=loan["id"]).first()

    r = client.post(
        "/api/payments/repay",
        headers=other_ch,
        json={"repayment_schedule_id": row.id, "amount": loan["total_repayable"], "payment_method": "cash"},
    )
    assert r.status_code == 403
