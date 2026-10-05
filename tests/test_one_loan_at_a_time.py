"""PRIME is one loan at a time: a customer can't apply while an application
is open, approved and awaiting payout, or while a loan is still being
repaid - and can apply again once it's fully repaid (or turned down)."""

import pytest

from app.extensions import db
from app.models import Loan, RepaymentSchedule

import _workflow


@pytest.fixture
def people(make_user, auth_header):
    customer, officer, admin = make_user("customer"), make_user("loan_officer"), make_user("admin")
    return {"ch": auth_header(customer), "oh": auth_header(officer), "ah": auth_header(admin)}


def _apply(client, people, apply_payload):
    return client.post("/api/loans/apply", headers=people["ch"], json=apply_payload(amount_requested=500))


def _approve(client, people, app_id):
    client.post(f"/api/loans/applications/{app_id}/officer-review", headers=people["oh"])
    _workflow.recommend(client, app_id, people["oh"])
    assert client.post(f"/api/admin/applications/{app_id}/approve", headers=people["ah"], json={}).status_code == 200


def test_cant_apply_while_approved_and_awaiting_payout(client, people, apply_payload):
    app_id = _apply(client, people, apply_payload).get_json()["id"]
    _approve(client, people, app_id)
    r = _apply(client, people, apply_payload)
    assert r.status_code == 409
    assert r.get_json()["message"] == (
        f"Your application #{app_id} is approved and waiting to be paid out. "
        "You can apply again once that loan is fully repaid."
    )


def test_cant_apply_while_a_loan_is_being_repaid_but_can_once_its_repaid(client, people, apply_payload):
    app_id = _apply(client, people, apply_payload).get_json()["id"]
    _approve(client, people, app_id)
    loan_id = client.post(f"/api/admin/applications/{app_id}/disbursement", headers=people["ah"],
                          json={"method": "cash_on_hand", "reference": "CASH-1"}).get_json()["loan_id"]

    r = _apply(client, people, apply_payload)
    assert r.status_code == 409
    assert r.get_json()["message"] == (
        f"You still have a loan (#{loan_id}) to repay. You can apply again once it's fully repaid."
    )

    row = RepaymentSchedule.query.filter_by(loan_id=loan_id).one()
    txn = client.post("/api/payments/repay", headers=people["ch"], json={
        "repayment_schedule_id": row.id, "amount": 700, "payment_method": "cash"}).get_json()["transaction"]["id"]
    assert client.post(f"/api/admin/repayments/{txn}/verify", headers=people["ah"]).status_code == 200
    assert db.session.get(Loan, loan_id).status.value == "closed"
    assert _apply(client, people, apply_payload).status_code == 201


def test_a_rejected_application_doesnt_block_a_new_one(client, people, apply_payload):
    app_id = _apply(client, people, apply_payload).get_json()["id"]
    client.post(f"/api/admin/applications/{app_id}/reject", headers=people["ah"], json={"reason": "Not eligible."})
    assert _apply(client, people, apply_payload).status_code == 201
