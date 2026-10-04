"""Repayment-reporting confirmation, end to end:

1. report-repayment accepts amount, payment date, reference number, and a
   receipt/screenshot upload - and never touches the outstanding balance.
2. admin verify-repayment moves REPORTED -> VERIFICATION_PENDING -> VERIFIED
   (or -> REJECTED with a reason) - only VERIFIED updates the ledger.
3. the loan reaches PAID only once the full amount is VERIFIED (never from
   a mere report or a rejected one), and PAID -> CLOSED is a distinct,
   explicit step.
4. every step is in AuditLog, including rejections with their reason.
"""

from datetime import date, timedelta
from decimal import Decimal

from app.extensions import db
from app.models import AuditLog, Document, RepaymentSchedule
from app.models.enums import DocumentType

import _workflow


def _make_receipt(user):
    doc = Document(
        user_id=user.id,
        document_type=DocumentType.RECEIPT,
        storage_path=f"users/{user.id}/receipt/test-{id(object())}.pdf",
    )
    db.session.add(doc)
    db.session.commit()
    return doc


def _disbursed_loan(client, make_user, auth_header, apply_payload):
    customer = make_user("customer")
    officer = make_user("loan_officer")
    admin = make_user("admin")
    ch, oh, ah = auth_header(customer), auth_header(officer), auth_header(admin)

    r = client.post("/api/loans/apply", headers=ch, json=apply_payload(amount_requested=500))
    app_id = r.get_json()["id"]
    client.post(f"/api/loans/applications/{app_id}/officer-review", headers=oh)
    _workflow.recommend(client, app_id, oh)
    client.post(f"/api/loans/applications/{app_id}/admin-review", headers=ah)
    client.post(
        f"/api/loans/applications/{app_id}/decision", headers=ah, json={"decision": "approve"}
    )
    r = client.post(
        f"/api/loans/applications/{app_id}/disburse", headers=ah, json={"method": "cash_on_hand", "method_reference": "CASH-ACK-0001"}
    )
    return r.get_json()["loan"], customer, officer, admin, ch, oh, ah


# ============================================================== point 1
def test_report_repayment_accepts_full_fields_and_never_touches_the_ledger(
    client, make_user, auth_header, apply_payload
):
    loan, customer, officer, admin, ch, oh, ah = _disbursed_loan(
        client, make_user, auth_header, apply_payload
    )
    row = RepaymentSchedule.query.filter_by(loan_id=loan["id"]).first()
    receipt = _make_receipt(customer)
    pay_date = (date.today() - timedelta(days=1)).isoformat()

    r = client.post(
        "/api/payments/repay",
        headers=ch,
        json={
            "repayment_schedule_id": row.id,
            "amount": loan["total_repayable"],
            "payment_method": "bank_transfer",
            "payment_date": pay_date,
            "reference_number": "BSP-TXN-77219",
            "document_ids": [receipt.id],
        },
    )
    assert r.status_code == 201, r.get_json()
    txn = r.get_json()["transaction"]
    assert txn["status"] == "reported"
    assert txn["payment_date"] == pay_date
    assert txn["reference_number"] == "BSP-TXN-77219"
    assert [d["id"] for d in txn["receipts"]] == [receipt.id]

    # Ledger untouched: schedule and loan status unchanged by a mere report.
    db.session.refresh(row)
    assert str(row.status) == "upcoming"
    assert Decimal(row.amount_paid) == Decimal("0")
    r = client.get("/api/accounts/summary", headers=ch)
    counts = r.get_json()["counts"]
    assert counts["active"] == 1 and counts["paid"] == 0


def test_payment_date_defaults_to_today_and_rejects_future_dates(
    client, make_user, auth_header, apply_payload
):
    loan, customer, officer, admin, ch, oh, ah = _disbursed_loan(
        client, make_user, auth_header, apply_payload
    )
    row = RepaymentSchedule.query.filter_by(loan_id=loan["id"]).first()

    r = client.post(
        "/api/payments/repay",
        headers=ch,
        json={"repayment_schedule_id": row.id, "amount": 100, "payment_method": "cash"},
    )
    assert r.status_code == 201, r.get_json()
    assert r.get_json()["transaction"]["payment_date"] == date.today().isoformat()

    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    r = client.post(
        "/api/payments/repay",
        headers=ch,
        json={
            "repayment_schedule_id": row.id,
            "amount": 50,
            "payment_method": "cash",
            "payment_date": tomorrow,
        },
    )
    assert r.status_code == 400


# ============================================================== point 2
def test_verify_chain_only_updates_ledger_on_verified(
    client, make_user, auth_header, apply_payload
):
    loan, customer, officer, admin, ch, oh, ah = _disbursed_loan(
        client, make_user, auth_header, apply_payload
    )
    row = RepaymentSchedule.query.filter_by(loan_id=loan["id"]).first()

    r = client.post(
        "/api/payments/repay",
        headers=ch,
        json={"repayment_schedule_id": row.id, "amount": loan["total_repayable"], "payment_method": "cash"},
    )
    txn_id = r.get_json()["transaction"]["id"]

    # REPORTED -> VERIFICATION_PENDING (officer claim step)
    r = client.post(f"/api/payments/{txn_id}/start-verification", headers=oh)
    assert r.status_code == 200
    assert r.get_json()["transaction"]["status"] == "verification_pending"
    db.session.refresh(row)
    assert Decimal(row.amount_paid) == Decimal("0"), "claiming for review must not touch the ledger"

    # VERIFICATION_PENDING -> VERIFIED (admin only, ledger moves HERE)
    r = client.post(f"/api/payments/{txn_id}/verify", headers=ah, json={"decision": "verified"})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["transaction"]["status"] == "verified"
    assert r.get_json()["loan_completed"] is True
    assert r.get_json()["loan_status"] == "closed"


def test_reject_requires_a_reason_and_never_touches_the_ledger(
    client, make_user, auth_header, apply_payload
):
    loan, customer, officer, admin, ch, oh, ah = _disbursed_loan(
        client, make_user, auth_header, apply_payload
    )
    row = RepaymentSchedule.query.filter_by(loan_id=loan["id"]).first()

    r = client.post(
        "/api/payments/repay",
        headers=ch,
        json={"repayment_schedule_id": row.id, "amount": loan["total_repayable"], "payment_method": "cash"},
    )
    txn_id = r.get_json()["transaction"]["id"]

    # No reason -> rejected outright
    r = client.post(f"/api/payments/{txn_id}/verify", headers=ah, json={"decision": "rejected"})
    assert r.status_code == 400

    r = client.post(
        f"/api/payments/{txn_id}/verify",
        headers=ah,
        json={"decision": "rejected", "note": "Receipt amount doesn't match reported amount."},
    )
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["transaction"]["status"] == "rejected"
    assert (
        r.get_json()["transaction"]["rejection_reason"]
        == "Receipt amount doesn't match reported amount."
    )

    db.session.refresh(row)
    assert str(row.status) == "upcoming"
    assert Decimal(row.amount_paid) == Decimal("0")
    r = client.get("/api/accounts/summary", headers=ch)
    assert r.get_json()["counts"]["active"] == 1
    assert r.get_json()["counts"]["paid"] == 0


# ============================================================== point 3
def test_loan_reaches_paid_only_once_the_full_verified_amount_covers_it(
    client, make_user, auth_header, apply_payload
):
    """A partial VERIFIED payment must not flip the loan to PAID; a
    REJECTED report for the remainder must not count toward it either -
    only the sum of VERIFIED amounts drives the transition."""
    loan, customer, officer, admin, ch, oh, ah = _disbursed_loan(
        client, make_user, auth_header, apply_payload
    )
    row = RepaymentSchedule.query.filter_by(loan_id=loan["id"]).first()
    total = Decimal(str(loan["total_repayable"]))
    half = (total / 2).quantize(Decimal("0.01"))
    remainder = (total - half).quantize(Decimal("0.01"))

    # Partial payment, verified.
    r = client.post(
        "/api/payments/repay",
        headers=ch,
        json={"repayment_schedule_id": row.id, "amount": float(half), "payment_method": "cash"},
    )
    txn1_id = r.get_json()["transaction"]["id"]
    r = client.post(f"/api/payments/{txn1_id}/verify", headers=ah, json={"decision": "verified"})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["loan_completed"] is False
    assert r.get_json()["loan_status"] == "active"

    # A second report for the remainder, REJECTED - must not count.
    r = client.post(
        "/api/payments/repay",
        headers=ch,
        json={"repayment_schedule_id": row.id, "amount": float(remainder), "payment_method": "cash"},
    )
    txn2_id = r.get_json()["transaction"]["id"]
    r = client.post(
        f"/api/payments/{txn2_id}/verify",
        headers=ah,
        json={"decision": "rejected", "note": "Could not confirm with bank statement."},
    )
    assert r.status_code == 200, r.get_json()

    db.session.refresh(row)
    assert Decimal(row.amount_paid) == half, "a rejected report must not add to the ledger"
    assert str(row.status) == "upcoming"

    r = client.get("/api/loans/mine", headers=ch)
    assert r.get_json()["loans"][0]["status"] == "active"

    # Re-report and verify the remainder - NOW it should reach PAID.
    r = client.post(
        "/api/payments/repay",
        headers=ch,
        json={"repayment_schedule_id": row.id, "amount": float(remainder), "payment_method": "cash"},
    )
    txn3_id = r.get_json()["transaction"]["id"]
    r = client.post(f"/api/payments/{txn3_id}/verify", headers=ah, json={"decision": "verified"})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["loan_completed"] is True
    assert r.get_json()["loan_status"] == "closed"


def test_paid_in_full_closes_automatically(client, make_user, auth_header, apply_payload):
    loan, customer, officer, admin, ch, oh, ah = _disbursed_loan(
        client, make_user, auth_header, apply_payload
    )
    row = RepaymentSchedule.query.filter_by(loan_id=loan["id"]).first()

    r = client.post(
        "/api/payments/repay",
        headers=ch,
        json={"repayment_schedule_id": row.id, "amount": loan["total_repayable"], "payment_method": "cash"},
    )
    txn_id = r.get_json()["transaction"]["id"]
    r = client.post(f"/api/payments/{txn_id}/verify", headers=ah, json={"decision": "verified"})
    assert r.get_json()["loan_status"] == "closed"

    # Closed means closed: there is no separate close step, and a write-off
    # needs an active/overdue loan.
    r = client.post(f"/api/loans/{loan['id']}/write-off", headers=ah, json={"note": "n/a"})
    assert r.status_code == 409, "cannot write off a closed loan"
    assert client.post(f"/api/loans/{loan['id']}/close", headers=ah).status_code == 404


# ============================================================== point 4
def test_the_full_loop_is_captured_in_auditlog(client, make_user, auth_header, apply_payload):
    loan, customer, officer, admin, ch, oh, ah = _disbursed_loan(
        client, make_user, auth_header, apply_payload
    )
    row = RepaymentSchedule.query.filter_by(loan_id=loan["id"]).first()

    r = client.post(
        "/api/payments/repay",
        headers=ch,
        json={
            "repayment_schedule_id": row.id,
            "amount": loan["total_repayable"],
            "payment_method": "cash",
            "reference_number": "REF-1",
        },
    )
    txn_id = r.get_json()["transaction"]["id"]
    client.post(f"/api/payments/{txn_id}/start-verification", headers=oh)
    client.post(
        f"/api/payments/{txn_id}/verify", headers=ah, json={"decision": "rejected", "note": "Bad receipt."}
    )

    reported = AuditLog.query.filter_by(
        action="payment_reported", entity_type="PaymentTransaction", entity_id=str(txn_id)
    ).first()
    assert reported is not None
    assert reported.actor_id == customer.id
    assert reported.details["reference_number"] == "REF-1"

    claimed = AuditLog.query.filter_by(
        action="payment_verification_started",
        entity_type="PaymentTransaction",
        entity_id=str(txn_id),
    ).first()
    assert claimed is not None
    assert claimed.actor_id == officer.id

    rejected = AuditLog.query.filter_by(
        action="payment_rejected", entity_type="PaymentTransaction", entity_id=str(txn_id)
    ).first()
    assert rejected is not None
    assert rejected.actor_id == admin.id
    assert rejected.details["reason"] == "Bad receipt."

    # Now a full verified pass, which closes the loan on its own.
    r = client.post(
        "/api/payments/repay",
        headers=ch,
        json={"repayment_schedule_id": row.id, "amount": loan["total_repayable"], "payment_method": "cash"},
    )
    txn2_id = r.get_json()["transaction"]["id"]
    client.post(f"/api/payments/{txn2_id}/verify", headers=ah, json={"decision": "verified"})

    verified = AuditLog.query.filter_by(
        action="payment_verified", entity_type="PaymentTransaction", entity_id=str(txn2_id)
    ).first()
    assert verified is not None and verified.actor_id == admin.id

    loan_closed = AuditLog.query.filter_by(
        action="loan_closed", entity_type="Loan", entity_id=str(loan["id"])
    ).first()
    assert loan_closed is not None
    assert loan_closed.actor_id == admin.id
    assert loan_closed.details["closure_reason"] == "paid_in_full"
    assert loan_closed.details["automatic"] is True
    assert loan_closed.details["closing_payment_transaction_id"] == txn2_id
