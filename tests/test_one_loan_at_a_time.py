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


# ------------------------------------------------- written off: blocked until an admin clears it
def _written_off(client, people, apply_payload):
    app_id = _apply(client, people, apply_payload).get_json()["id"]
    _approve(client, people, app_id)
    loan_id = client.post(f"/api/admin/applications/{app_id}/disbursement", headers=people["ah"],
                          json={"method": "cash_on_hand", "reference": "CASH-1"}).get_json()["loan_id"]
    r = client.post(f"/api/admin/loans/{loan_id}/write-off", headers=people["ah"], json={"reason": "Uncollectable."})
    assert r.status_code == 200
    return loan_id


def test_a_written_off_loan_blocks_applying_until_an_admin_clears_it(client, people, apply_payload):
    loan_id = _written_off(client, people, apply_payload)
    r = _apply(client, people, apply_payload)
    assert r.status_code == 409
    assert r.get_json()["message"] == (
        f"Your loan (#{loan_id}) was written off, so you can't apply for a new PRIME loan until "
        "PRIMESTONE has reviewed it. Contact PRIMESTONE to ask for a review."
    )
    (loan,) = client.get("/api/loans/mine", headers=people["ch"]).get_json()["loans"]
    assert loan["blocks_reapplication"] is True

    url = f"/api/admin/loans/{loan_id}/clear-reapplication-block"
    assert client.post(url, headers=people["oh"], json={"reason": "x"}).status_code == 403
    assert client.post(url, headers=people["ch"], json={"reason": "x"}).status_code == 403
    assert client.post(url, headers=people["ah"], json={"reason": " "}).status_code == 400
    r = client.post(url, headers=people["ah"], json={"reason": "Debt settled with the branch."})
    assert r.status_code == 200
    assert r.get_json()["reapplication"]["blocked"] is False
    assert r.get_json()["reapplication"]["reason"] == "Debt settled with the branch."
    assert client.post(url, headers=people["ah"], json={"reason": "again"}).status_code == 409

    (loan,) = client.get("/api/loans/mine", headers=people["ch"]).get_json()["loans"]
    assert loan["blocks_reapplication"] is False
    assert _apply(client, people, apply_payload).status_code == 201


def test_only_a_written_off_loan_can_be_cleared(client, people, apply_payload):
    app_id = _apply(client, people, apply_payload).get_json()["id"]
    _approve(client, people, app_id)
    loan_id = client.post(f"/api/admin/applications/{app_id}/disbursement", headers=people["ah"],
                          json={"method": "cash_on_hand", "reference": "CASH-1"}).get_json()["loan_id"]
    r = client.post(f"/api/admin/loans/{loan_id}/clear-reapplication-block", headers=people["ah"],
                    json={"reason": "x"})
    assert r.status_code == 409
    assert client.get(f"/api/admin/loans/{loan_id}", headers=people["ah"]).get_json()["reapplication"] is None


def test_a_clearance_is_insert_only(client, people, apply_payload):
    from app.models import ReapplicationClearance
    from app.models.immutability import ImmutableRecordError

    loan_id = _written_off(client, people, apply_payload)
    client.post(f"/api/admin/loans/{loan_id}/clear-reapplication-block", headers=people["ah"], json={"reason": "ok"})
    row = ReapplicationClearance.query.filter_by(loan_id=loan_id).one()
    row.reason = "edited"
    with pytest.raises(ImmutableRecordError):
        db.session.flush()
    db.session.rollback()
