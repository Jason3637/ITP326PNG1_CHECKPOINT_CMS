"""Administrator API (/api/admin/...): queues, final review and decisions,
atomic disbursement, ledger-derived loan detail, transactional repayment
verification with automatic closure, versioned pricing/penalty policy,
parameters and analytics - and that no loan_officer or customer token
reaches any of it.
"""

import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from io import BytesIO
from unittest.mock import patch

import pytest

from app.extensions import db
from app.models import (
    AuditLog,
    Disbursement,
    Loan,
    LoanApplication,
    LoanClosure,
    LoanLedgerEntry,
    LoanTermsSnapshot,
    PaymentTransaction,
)
from app.services import loan_processing

import _workflow

# Every route this phase added. The RBAC matrix covers them too; this list
# makes the officer/customer refusal explicit and fails if one goes missing.
ADMIN_ROUTES = [
    ("GET", "/api/admin/queues"),
    ("GET", "/api/admin/queues/awaiting_decision"),
    ("GET", "/api/admin/applications/1"),
    ("GET", "/api/admin/applications/1/customer-history"),
    ("POST", "/api/admin/applications/1/approve"),
    ("POST", "/api/admin/applications/1/reject"),
    ("POST", "/api/admin/applications/1/return-to-officer"),
    ("POST", "/api/admin/applications/1/disbursement-evidence"),
    ("POST", "/api/admin/applications/1/disbursement"),
    ("GET", "/api/admin/loans"),
    ("GET", "/api/admin/loans/1"),
    ("POST", "/api/admin/loans/1/write-off"),
    ("GET", "/api/admin/repayments"),
    ("POST", "/api/admin/repayments/1/verify"),
    ("POST", "/api/admin/repayments/1/reject"),
    ("GET", "/api/admin/pricing"),
    ("POST", "/api/admin/pricing"),
    ("GET", "/api/admin/penalty-policy"),
    ("POST", "/api/admin/penalty-policy"),
    ("GET", "/api/admin/analytics"),
    ("GET", "/api/admin/parameters"),
    ("PUT", "/api/admin/parameters"),
]


@pytest.fixture
def people(make_user, auth_header):
    customer, officer, admin = make_user("customer"), make_user("loan_officer"), make_user("admin")
    return {"customer": customer, "admin": admin,
            "ch": auth_header(customer), "oh": auth_header(officer), "ah": auth_header(admin)}


def _recommended(client, people, apply_payload, amount=500, **overrides):
    r = client.post("/api/loans/apply", headers=people["ch"],
                    json=apply_payload(amount_requested=amount, **overrides))
    app_id = r.get_json()["id"]
    assert client.post(f"/api/loans/applications/{app_id}/officer-review", headers=people["oh"]).status_code == 200
    assert _workflow.recommend(client, app_id, people["oh"]).status_code == 200
    return app_id


def _approved(client, people, apply_payload, amount=500, **overrides):
    app_id = _recommended(client, people, apply_payload, amount, **overrides)
    r = client.post(f"/api/admin/applications/{app_id}/approve", headers=people["ah"], json={})
    assert r.status_code == 200, r.get_json()
    return app_id


def _disburse(client, people, app_id, **extra):
    body = {"method": "cash_on_hand", "reference": "CASH-ACK-7"} | extra
    return client.post(f"/api/admin/applications/{app_id}/disbursement", headers=people["ah"], json=body)


def _loan(client, people, apply_payload, amount=500):
    app_id = _approved(client, people, apply_payload, amount)
    r = _disburse(client, people, app_id)
    assert r.status_code == 201, r.get_json()
    return app_id, r.get_json()["loan_id"]


def _report(client, people, loan_id, amount, paid_on=None):
    row = db.session.get(Loan, loan_id).repayment_schedule[0]
    body = {"repayment_schedule_id": row.id, "amount": amount, "payment_method": "cash"}
    if paid_on:
        body["payment_date"] = paid_on
    r = client.post("/api/payments/repay", headers=people["ch"], json=body)
    assert r.status_code == 201, r.get_json()
    return r.get_json()["transaction"]["id"]


# ===================================================================== RBAC
@pytest.mark.parametrize("method,path", ADMIN_ROUTES)
def test_loan_officer_and_customer_tokens_are_refused(client, people, method, path):
    for who in ("oh", "ch"):
        r = client.open(path, method=method, headers=people[who], json={})
        assert r.status_code == 403, (who, method, path, r.status_code)
    assert client.open(path, method=method, json={}).status_code == 401


def test_every_admin_route_is_in_the_list(app):
    listed = {(m, re.sub(r"/\d+", "/<id>", p).replace("/awaiting_decision", "/<queue>")) for m, p in ADMIN_ROUTES}
    for rule in app.url_map.iter_rules():
        if not rule.rule.startswith("/api/admin/") or "staff" in rule.rule or "customer-verifications" in rule.rule:
            continue
        path = re.sub(r"<[^>]+>", "<id>", rule.rule).replace("/queues/<id>", "/queues/<queue>")
        for m in rule.methods - {"HEAD", "OPTIONS"}:
            assert (m, path) in listed, (m, rule.rule)


# ============================================================ decisions
def test_approve_from_recommended_goes_to_awaiting_disbursement_and_creates_nothing(client, people, apply_payload):
    app_id = _recommended(client, people, apply_payload)
    r = client.post(f"/api/admin/applications/{app_id}/approve", headers=people["ah"], json={})
    assert r.status_code == 200 and r.get_json()["status"] == "awaiting_disbursement"
    assert Loan.query.count() == 0 and Disbursement.query.count() == 0
    actions = [a.action for a in AuditLog.query.filter_by(entity_type="LoanApplication", entity_id=str(app_id))]
    assert "loan_application_admin_review_started" in actions and "loan_application_decision" in actions


def test_reject_and_return_need_a_reason(client, people, apply_payload, make_user, auth_header):
    app_id = _recommended(client, people, apply_payload)
    for path in ("reject", "return-to-officer"):
        r = client.post(f"/api/admin/applications/{app_id}/{path}", headers=people["ah"], json={"reason": " "})
        assert r.status_code == 400, path
    r = client.post(f"/api/admin/applications/{app_id}/return-to-officer", headers=people["ah"],
                    json={"reason": "Check the payslip date."})
    assert r.status_code == 200 and r.get_json()["status"] == "returned_to_officer"

    other = _recommended(client, people | {"ch": auth_header(make_user("customer"))}, apply_payload)
    r = client.post(f"/api/admin/applications/{other}/reject", headers=people["ah"], json={"reason": "Income too low."})
    assert r.status_code == 200 and r.get_json()["status"] == "rejected"
    assert Loan.query.count() == 0


def test_final_review_shows_the_recommendation_and_admin_sees_any_history(client, people, apply_payload):
    app_id = _recommended(client, people, apply_payload)
    body = client.get(f"/api/admin/applications/{app_id}", headers=people["ah"]).get_json()
    (rec,) = body["recommendations"]
    assert rec["recommendation"] == "recommend_approval" and rec["officer_name"] and rec["created_at"]
    assert "checklist_snapshot" in rec and "comments" in rec
    assert body["customer"]["id"] == people["customer"].id
    assert body["final_decision"]["can_approve"] is True
    assert body["quote"]["total_repayable"] == 700.0

    client.post(f"/api/admin/applications/{app_id}/reject", headers=people["ah"], json={"reason": "No."})
    # Decided: a loan officer can no longer open its history; an admin can.
    url = f"/api/admin/applications/{app_id}/customer-history"
    assert client.get(url, headers=people["ah"]).status_code == 200
    assert client.get(f"/api/officer/applications/{app_id}/customer-history",
                      headers=people["oh"]).status_code == 403


# ============================================================ disbursement
def test_disbursement_creates_everything_and_only_once(client, people, apply_payload):
    app_id = _approved(client, people, apply_payload, disbursement_method_requested="bsp_mobile_banking",
                       disbursement_account_reference="+675 7123 4567")
    assert _disburse(client, people, app_id, reference="").status_code == 400

    # A BSP payout needs its receipt.
    r = _disburse(client, people, app_id, method="bsp_mobile_banking", reference="BSP-TXN-1")
    assert r.status_code == 400 and r.get_json()["message"] == "evidence_document_id is required for BSP Mobile Banking disbursements: upload the BSP receipt first."
    with patch("app.services.documents.supabase_storage.upload_file", return_value="ok"):
        receipt = client.post(f"/api/admin/applications/{app_id}/disbursement-evidence", headers=people["ah"],
                              data={"file": (BytesIO(b"%PDF-1.4 bsp"), "bsp.pdf", "application/pdf")},
                              content_type="multipart/form-data").get_json()["id"]

    r = _disburse(client, people, app_id, method="bsp_mobile_banking", reference="BSP-TXN-1",
                  evidence_document_id=receipt)
    assert r.status_code == 201, r.get_json()
    loan = r.get_json()
    assert db.session.get(LoanApplication, app_id).status.value == "disbursed"
    assert loan["status"] == "active"
    assert loan["disbursement"]["reference"] == "BSP-TXN-1"
    assert loan["disbursement"]["destination_masked"].endswith("4567")
    assert loan["balance"]["outstanding"] == 700.0
    assert LoanTermsSnapshot.query.filter_by(application_id=app_id).count() == 1
    assert [e["entry_type"] for e in loan["ledger"]] == ["original_obligation"]

    assert _disburse(client, people, app_id).status_code == 409
    assert Disbursement.query.filter_by(application_id=app_id).count() == 1


def test_disbursement_is_all_or_nothing(client, people, apply_payload):
    app_id = _approved(client, people, apply_payload)
    application = db.session.get(LoanApplication, app_id)
    with patch("app.services.loan_processing.LoanLedgerEntry", side_effect=RuntimeError("disk full")):
        with pytest.raises(RuntimeError):
            loan_processing.disburse_application(application, people["admin"], method="cash_on_hand",
                                                 method_reference="CASH-1")
    db.session.expire_all()
    assert db.session.get(LoanApplication, app_id).status.value == "awaiting_disbursement"
    assert (Loan.query.count(), Disbursement.query.count(), LoanTermsSnapshot.query.count(),
            LoanLedgerEntry.query.count()) == (0, 0, 0, 0)


def test_disbursement_evidence_is_uploaded_and_attached(client, people, apply_payload):
    app_id = _approved(client, people, apply_payload)
    with patch("app.services.documents.supabase_storage.upload_file", return_value="ok"):
        r = client.post(f"/api/admin/applications/{app_id}/disbursement-evidence", headers=people["ah"],
                        data={"file": (BytesIO(b"%PDF-1.4 receipt"), "receipt.pdf", "application/pdf")},
                        content_type="multipart/form-data")
    assert r.status_code == 201, r.get_json()
    doc_id = r.get_json()["id"]
    upload = AuditLog.query.filter_by(action="document_uploaded", entity_id=str(doc_id)).one()
    assert upload.actor_id == people["admin"].id, "the admin is the audited uploader"
    r = _disburse(client, people, app_id, evidence_document_id=doc_id)
    assert r.status_code == 201 and r.get_json()["disbursement"]["evidence_document_id"] == doc_id


# ========================================================= repayments
def test_verification_posts_the_ledger_and_closes_at_zero(client, people, apply_payload):
    _, loan_id = _loan(client, people, apply_payload)
    first = _report(client, people, loan_id, 300)
    awaiting = client.get("/api/admin/repayments", headers=people["ah"]).get_json()
    (item,) = awaiting["items"]
    assert (item["payment_id"], item["amount_reported"], item["loan_outstanding"]) == (first, 300.0, 700.0)

    r = client.post(f"/api/admin/repayments/{first}/verify", headers=people["ah"], json={})
    assert r.status_code == 200 and r.get_json()["outstanding"] == 400.0
    assert client.post(f"/api/admin/repayments/{first}/verify", headers=people["ah"]).status_code == 409

    # More than is owed is refused when it's reported...
    row = db.session.get(Loan, loan_id).repayment_schedule[0]
    r = client.post("/api/payments/repay", headers=people["ch"], json={
        "repayment_schedule_id": row.id, "amount": 500, "payment_method": "cash"})
    assert r.status_code == 400
    # ...and at verification, when two reports each fit but not together.
    a, b = _report(client, people, loan_id, 300), _report(client, people, loan_id, 300)
    assert client.post(f"/api/admin/repayments/{a}/verify", headers=people["ah"]).status_code == 200
    assert client.post(f"/api/admin/repayments/{b}/verify", headers=people["ah"]).status_code == 409
    assert client.post(f"/api/admin/repayments/{b}/reject", headers=people["ah"], json={}).status_code == 400
    r = client.post(f"/api/admin/repayments/{b}/reject", headers=people["ah"],
                    json={"reason": "Duplicate of an earlier payment."})
    assert r.status_code == 200
    assert LoanLedgerEntry.query.filter_by(payment_transaction_id=b).count() == 0

    last = _report(client, people, loan_id, 100)
    r = client.post(f"/api/admin/repayments/{last}/verify", headers=people["ah"])
    assert r.status_code == 200 and r.get_json()["loan_completed"] is True

    detail = client.get(f"/api/admin/loans/{loan_id}", headers=people["ah"]).get_json()
    assert detail["status"] == "closed"
    assert detail["balance"] == {"original_obligation": 700.0, "penalties": 0.0,
                                 "verified_repayments": 700.0, "outstanding": 0.0, "days_overdue": 0}
    assert detail["closure"]["closure_reason"] == "paid_in_full"
    assert detail["closure"]["closing_payment_transaction_id"] == last
    assert detail["closure"]["timeliness"] == "on_time"
    assert [p["status"] for p in detail["payments"]] == ["verified", "verified", "rejected", "verified"]
    assert {"loan_disbursed", "payment_verified", "payment_rejected", "loan_closed"} <= {
        a["action"] for a in detail["audit_history"]}
    assert LoanClosure.query.filter_by(loan_id=loan_id).count() == 1


def test_a_failed_verification_changes_nothing(client, people, apply_payload):
    _, loan_id = _loan(client, people, apply_payload)
    txn = _report(client, people, loan_id, 700)
    with patch("app.services.closures.record_closure", side_effect=RuntimeError("boom")):
        with pytest.raises(RuntimeError):
            client.post(f"/api/admin/repayments/{txn}/verify", headers=people["ah"])
    db.session.expire_all()
    assert db.session.get(PaymentTransaction, txn).status.value == "reported"
    assert LoanLedgerEntry.query.filter_by(payment_transaction_id=txn).count() == 0
    assert db.session.get(Loan, loan_id).status.value == "active"


def test_write_off_needs_a_reason_and_records_the_closure(client, people, apply_payload):
    _, loan_id = _loan(client, people, apply_payload)
    assert client.post(f"/api/admin/loans/{loan_id}/write-off", headers=people["ah"], json={}).status_code == 400
    r = client.post(f"/api/admin/loans/{loan_id}/write-off", headers=people["ah"], json={"reason": "Customer left PNG."})
    assert r.status_code == 200
    closure = r.get_json()["closure"]
    assert (closure["closure_reason"], closure["outstanding_at_closure"]) == ("defaulted", 700.0)


# =============================================================== queues
def test_queues_are_real_filtered_counts(client, people, apply_payload, make_user, auth_header):
    def loan_disbursed_days_ago(days):
        class Fixed(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.now(timezone.utc) - timedelta(days=days)

        customer = make_user("customer")
        p = people | {"ch": auth_header(customer)}
        with patch("app.services.loan_processing.datetime", Fixed):
            return _loan(client, p, apply_payload)[1]

    due_today = loan_disbursed_days_ago(14)
    due_soon = loan_disbursed_days_ago(10)
    overdue = loan_disbursed_days_ago(20)
    _recommended(client, people, apply_payload)  # awaiting decision

    counts = {k: v["count"] for k, v in client.get("/api/admin/queues", headers=people["ah"]).get_json()["queues"].items()}
    assert counts == {"awaiting_decision": 1, "awaiting_disbursement": 0, "active_loans": 3,
                      "due_today": 1, "due_this_week": 2, "overdue": 1,
                      "repayments_awaiting_verification": 0}
    ids = lambda q: [i["loan_id"] for i in client.get(f"/api/admin/queues/{q}", headers=people["ah"]).get_json()["items"]]  # noqa: E731
    assert ids("due_today") == [due_today]
    assert ids("due_this_week") == [due_today, due_soon]
    (item,) = client.get("/api/admin/queues/overdue", headers=people["ah"]).get_json()["items"]
    assert (item["loan_id"], item["days_overdue"], item["outstanding"]) == (overdue, 6, 700.0)
    assert client.get("/api/admin/queues/nope", headers=people["ah"]).status_code == 404


# ===================================================== pricing / parameters
def test_new_pricing_version_applies_only_to_new_applications(client, people, apply_payload, make_user, auth_header):
    old_app, loan_id = _loan(client, people, apply_payload)
    snap_before = LoanTermsSnapshot.query.filter_by(loan_id=loan_id).one().interest_amount

    bad = [{"category": "PRIME 1", "min_amount": 100, "max_amount": 300, "interest_rate": 0.5},
           {"category": "PRIME 2", "min_amount": 305, "max_amount": 1000, "interest_rate": 0.3}]
    assert client.post("/api/admin/pricing", headers=people["ah"], json={"tiers": bad}).status_code == 400
    good = [{"category": "PRIME 1", "min_amount": 100, "max_amount": 300, "interest_rate": 0.5},
            {"category": "PRIME 2", "min_amount": 301, "max_amount": 1000, "interest_rate": 0.3}]
    r = client.post("/api/admin/pricing", headers=people["ah"], json={"tiers": good, "note": "Lower PRIME 2."})
    assert r.status_code == 201 and r.get_json()["current"]["label"] == "prime-v2"
    entry = AuditLog.query.filter_by(action="prime_pricing_version_created").one()
    assert entry.details["before"][1]["interest_rate"] == 0.4 and entry.details["after"][1]["interest_rate"] == 0.3

    db.session.expire_all()
    assert LoanTermsSnapshot.query.filter_by(loan_id=loan_id).one().interest_amount == snap_before
    assert db.session.get(LoanApplication, old_app).quoted_interest_amount == Decimal("200.00")
    other = auth_header(make_user("customer"))
    r = client.post("/api/loans/apply", headers=other, json=apply_payload(amount_requested=500))
    assert r.get_json()["pricing"]["interest_amount"] == 150.0


def test_penalty_policy_versions_and_validation(client, people):
    assert client.post("/api/admin/penalty-policy", headers=people["ah"],
                       json={"tiers": [{"days_late": 0, "pct_of_original_interest": 0.25}]}).status_code == 400
    r = client.post("/api/admin/penalty-policy", headers=people["ah"], json={"tiers": [
        {"days_late": 14, "pct_of_original_interest": 1.0}, {"days_late": 7, "pct_of_original_interest": 0.3}]})
    assert r.status_code == 201
    assert [t["days_late"] for t in r.get_json()["current"]["tiers"]] == [7, 14]


def test_parameters_are_the_short_list_with_audited_before_and_after(client, people):
    params = client.get("/api/admin/parameters", headers=people["ah"]).get_json()["parameters"]
    assert set(params) == {"min_monthly_income", "max_debt_to_income_ratio", "customer_verification_validity_months"}
    assert client.put("/api/admin/parameters", headers=people["ah"],
                      json={"default_annual_interest_rate": 0.2}).status_code == 400
    assert client.put("/api/admin/parameters", headers=people["ah"], json={"min_monthly_income": 300}).status_code == 200
    change = AuditLog.query.filter_by(action="system_parameters_updated").one().details["changes"]["min_monthly_income"]
    assert change["after"] == 300.0 and change["before"] != 300.0


# ================================================================ analytics
def test_analytics_keeps_each_money_figure_separate(client, people, apply_payload):
    _, loan_id = _loan(client, people, apply_payload)
    txn = _report(client, people, loan_id, 300)
    client.post(f"/api/admin/repayments/{txn}/verify", headers=people["ah"])
    body = client.get("/api/admin/analytics", headers=people["ah"]).get_json()
    figures = {
        "principal": body["disbursements"]["principal_disbursed"]["value"],
        "interest": body["disbursements"]["interest_contracted"]["value"],
        "expected": body["disbursements"]["expected_repayment"]["value"],
        "cash": body["repayments"]["verified_repayments"]["value"],
        "outstanding": body["portfolio"]["outstanding_value"]["value"],
    }
    assert figures == {"principal": 500.0, "interest": 200.0, "expected": 700.0, "cash": 300.0, "outstanding": 400.0}
    assert body["applications"]["approved"]["value"] == 1
    assert body["applications"]["approval_rate"]["value"] == 1.0
    for section in ("applications", "disbursements", "repayments", "portfolio", "processing_times"):
        for metric in body[section].values():
            assert metric["definition"], section
    assert client.get("/api/admin/analytics?from=2026-13-01", headers=people["ah"]).status_code == 400
