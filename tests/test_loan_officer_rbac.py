"""RBAC boundary for the Loan Officer workflow (the Phase 1 matrix).

A loan_officer token must be refused every admin-only action - final
approve/reject, early reject, start admin review, return to officer,
reassign, record disbursement, verify/reject repayments, close and write off
loans, system parameters, audit logs. Each attempt is made against a REAL
application/loan/payment sitting in the exact status where the action would
otherwise succeed (so a 403 can't be masked by a 404/409), and the record is
then checked to be unchanged. The service layer is also called directly, to
prove the check doesn't rely on the route decorator alone.
"""

import pytest

from app.extensions import db
from app.models import Disbursement, Loan, LoanApplication, PaymentTransaction, RepaymentSchedule
from app.models.enums import PaymentStatus
from app.services import loan_processing, payment_processing
from app.services.errors import ServiceError


@pytest.fixture
def people(make_user, auth_header):
    customer, officer, admin = make_user("customer"), make_user("loan_officer"), make_user("admin")
    return {
        "customer": customer,
        "officer": officer,
        "admin": admin,
        "ch": auth_header(customer),
        "oh": auth_header(officer),
        "ah": auth_header(admin),
    }


def _status(app_id):
    db.session.expire_all()
    return str(db.session.get(LoanApplication, app_id).status)


def _app_in(client, people, workflow, apply_payload, status):
    """An application owned by people['customer'], claimed by people['officer'],
    walked to `status`."""
    ch, oh, ah = people["ch"], people["oh"], people["ah"]
    app_id = client.post("/api/loans/apply", headers=ch, json=apply_payload()).get_json()["id"]
    client.post(f"/api/loans/applications/{app_id}/officer-review", headers=oh)
    if status == "officer_review":
        return app_id
    assert workflow.recommend(app_id, oh).status_code == 200
    if status == "recommended_for_approval":
        return app_id
    assert client.post(f"/api/loans/applications/{app_id}/admin-review", headers=ah).status_code == 200
    if status == "admin_review":
        return app_id
    r = client.post(f"/api/loans/applications/{app_id}/decision", headers=ah, json={"decision": "approve"})
    assert r.status_code == 200
    assert status == "awaiting_disbursement"
    return app_id


# ------------------------------------------------- application decisions (HTTP)
def test_officer_cannot_make_the_final_decision(client, people, workflow, apply_payload):
    app_id = _app_in(client, people, workflow, apply_payload, "admin_review")
    url = f"/api/loans/applications/{app_id}/decision"
    for decision in ("approve", "reject"):
        r = client.post(url, headers=people["oh"], json={"decision": decision, "note": "x"})
        assert r.status_code == 403, decision
    assert _status(app_id) == "admin_review"


@pytest.mark.parametrize("status", ["officer_review", "recommended_for_approval", "admin_review"])
def test_officer_cannot_reject_at_any_stage(client, people, workflow, apply_payload, status):
    app_id = _app_in(client, people, workflow, apply_payload, status)
    r = client.post(f"/api/loans/applications/{app_id}/reject", headers=people["oh"], json={"note": "x"})
    assert r.status_code == 403
    assert _status(app_id) == status


def test_officer_cannot_start_admin_review_return_or_reassign(client, people, workflow, apply_payload):
    oh = people["oh"]
    app_id = _app_in(client, people, workflow, apply_payload, "recommended_for_approval")
    base = f"/api/loans/applications/{app_id}"
    assert client.post(f"{base}/admin-review", headers=oh).status_code == 403
    assert client.post(f"{base}/return-to-officer", headers=oh, json={"reason": "x"}).status_code == 403
    assert client.post(f"{base}/assign", headers=oh, json={"officer_id": people["officer"].id}).status_code == 403
    assert _status(app_id) == "recommended_for_approval"


def test_officer_cannot_record_a_disbursement(client, people, workflow, apply_payload):
    app_id = _app_in(client, people, workflow, apply_payload, "awaiting_disbursement")
    r = client.post(
        f"/api/loans/applications/{app_id}/disburse",
        headers=people["oh"],
        json={"method": "cash_on_hand"},
    )
    assert r.status_code == 403
    assert _status(app_id) == "awaiting_disbursement"
    assert Loan.query.count() == 0 and Disbursement.query.count() == 0


# ------------------------------------------------------ loans and repayments
@pytest.fixture
def disbursed(client, people, workflow, apply_payload):
    _, loan = workflow.to_disbursed_loan(people["ch"], people["oh"], people["ah"], apply_payload())
    row = RepaymentSchedule.query.filter_by(loan_id=loan["id"]).one()
    r = client.post(
        "/api/payments/repay",
        headers=people["ch"],
        json={"repayment_schedule_id": row.id, "amount": loan["total_repayable"], "payment_method": "cash"},
    )
    return loan, r.get_json()["transaction"]["id"]


def test_officer_cannot_verify_or_reject_repayments(client, people, disbursed):
    loan, txn_id = disbursed
    oh = people["oh"]
    # claiming for review is allowed...
    assert client.post(f"/api/payments/{txn_id}/start-verification", headers=oh).status_code == 200
    # ...deciding it is not
    for body in ({"decision": "verified"}, {"decision": "rejected", "note": "x"}):
        assert client.post(f"/api/payments/{txn_id}/verify", headers=oh, json=body).status_code == 403
    db.session.expire_all()
    assert db.session.get(PaymentTransaction, txn_id).status == PaymentStatus.VERIFICATION_PENDING
    assert float(RepaymentSchedule.query.filter_by(loan_id=loan["id"]).one().amount_paid) == 0


def test_officer_cannot_close_or_write_off_loans(client, people, disbursed):
    loan, txn_id = disbursed
    oh, ah = people["oh"], people["ah"]
    assert client.post(f"/api/loans/{loan['id']}/write-off", headers=oh, json={"note": "x"}).status_code == 403
    client.post(f"/api/payments/{txn_id}/verify", headers=ah, json={"decision": "verified"})
    db.session.expire_all()
    assert str(db.session.get(Loan, loan["id"]).status) == "paid"
    assert client.post(f"/api/loans/{loan['id']}/close", headers=oh).status_code == 403
    db.session.expire_all()
    assert str(db.session.get(Loan, loan["id"]).status) == "paid"


def test_officer_cannot_touch_admin_configuration_or_audit(client, people):
    oh = people["oh"]
    assert client.get("/api/admin/parameters", headers=oh).status_code == 403
    assert client.put("/api/admin/parameters", headers=oh, json={"min_loan_amount": 1}).status_code == 403
    assert client.get("/api/reports/audit-logs", headers=oh).status_code == 403


# ------------------------------------------- service layer, without the route
def test_admin_only_services_refuse_a_loan_officer_directly(client, people, workflow, apply_payload, disbursed):
    officer = people["officer"]
    review = db.session.get(LoanApplication, _app_in(client, people, workflow, apply_payload, "admin_review"))

    calls = {
        "decide": lambda: loan_processing.decide_application(review, officer, approve=True, note="x"),
        "reject": lambda: loan_processing.reject_application(review, officer, "x"),
        "return": lambda: loan_processing.return_to_officer(review, officer, "x"),
        "assign": lambda: loan_processing.assign_application(review, officer, officer.id),
        "disburse": lambda: loan_processing.disburse_application(review, officer, method="cash_on_hand"),
        "close": lambda: loan_processing.close_loan(db.session.get(Loan, disbursed[0]["id"]), officer),
        "write_off": lambda: loan_processing.write_off_loan(db.session.get(Loan, disbursed[0]["id"]), officer),
        "verify_payment": lambda: payment_processing.verify_payment(officer, disbursed[1], decision="verified"),
    }
    for name, call in calls.items():
        with pytest.raises(ServiceError) as exc:
            call()
        assert exc.value.status_code == 403, name
    assert _status(review.id) == "admin_review"


# ------------------------------------------- officer views are staff-only
@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/api/officer/queues"),
        ("get", "/api/officer/queues/awaiting_review"),
        ("get", "/api/officer/applications/{id}"),
        ("get", "/api/officer/applications/{id}/checklist"),
        ("patch", "/api/officer/applications/{id}/checklist/valid_id"),
        ("get", "/api/officer/applications/{id}/customer-history"),
        ("post", "/api/loans/applications/{id}/request-action"),
        ("post", "/api/loans/applications/{id}/recommend"),
        ("post", "/api/loans/applications/{id}/resume-review"),
    ],
)
def test_customer_cannot_use_officer_endpoints(client, people, apply_payload, method, path):
    app_id = client.post("/api/loans/apply", headers=people["ch"], json=apply_payload()).get_json()["id"]
    r = getattr(client, method)(path.format(id=app_id), headers=people["ch"], json={})
    assert r.status_code == 403


def test_officer_endpoints_require_a_token(client):
    assert client.get("/api/officer/queues").status_code == 401
    assert client.get("/api/officer/applications/1").status_code == 401
