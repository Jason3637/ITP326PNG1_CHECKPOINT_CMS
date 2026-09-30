"""Role-based access control: wrong role -> 403, no token -> 401."""

import pytest


def test_customer_cannot_list_applications(client, make_user, auth_header):
    ch = auth_header(make_user("customer"))
    r = client.get("/api/loans/applications", headers=ch)
    assert r.status_code == 403


def test_customer_cannot_apply_via_officer_decision(client, make_user, auth_header):
    ch = auth_header(make_user("customer"))
    r = client.post(
        "/api/loans/applications/1/decision",
        headers=ch,
        json={"decision": "approve"},
    )
    assert r.status_code == 403


def test_officer_cannot_apply_for_a_loan(client, make_user, auth_header):
    oh = auth_header(make_user("loan_officer"))
    r = client.post("/api/loans/apply", headers=oh, json={"amount_requested": 500})
    assert r.status_code == 403


def test_loan_officer_cannot_make_the_final_decision(client, make_user, auth_header):
    """Under the two-tier chain, only admin decides - loan_officer can only
    recommend (see test_loan_lifecycle.py's full-chain test)."""
    oh = auth_header(make_user("loan_officer"))
    r = client.post(
        "/api/loans/applications/1/decision", headers=oh, json={"decision": "approve"}
    )
    assert r.status_code == 403


def test_loan_officer_cannot_start_admin_review_or_disburse(client, make_user, auth_header):
    oh = auth_header(make_user("loan_officer"))
    assert client.post("/api/loans/applications/1/admin-review", headers=oh).status_code == 403
    assert (
        client.post(
            "/api/loans/applications/1/disburse", headers=oh, json={"method": "cash_on_hand"}
        ).status_code
        == 403
    )


def test_customer_cannot_call_officer_transitions(client, make_user, auth_header):
    ch = auth_header(make_user("customer"))
    assert client.post("/api/loans/applications/1/officer-review", headers=ch).status_code == 403
    assert client.post("/api/loans/applications/1/recommend", headers=ch).status_code == 403


def test_officer_cannot_read_audit_logs(client, make_user, auth_header):
    oh = auth_header(make_user("loan_officer"))
    r = client.get("/api/reports/audit-logs", headers=oh)
    assert r.status_code == 403


def test_non_admin_cannot_change_system_parameters(client, make_user, auth_header):
    oh = auth_header(make_user("loan_officer"))
    assert client.get("/api/admin/parameters", headers=oh).status_code == 403
    assert (
        client.put(
            "/api/admin/parameters", headers=oh, json={"max_loan_amount": 1}
        ).status_code
        == 403
    )


def test_admin_can_change_system_parameters(client, make_user, auth_header):
    ah = auth_header(make_user("admin"))
    r = client.put(
        "/api/admin/parameters",
        headers=ah,
        json={"default_annual_interest_rate": 0.2},
    )
    assert r.status_code == 200
    params = r.get_json()["parameters"]
    assert params["default_annual_interest_rate"]["value"] == 0.2
    assert params["default_annual_interest_rate"]["source"] == "override"


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/api/users/profile"),
        ("get", "/api/accounts/summary"),
        ("get", "/api/reports/dashboard"),
        ("post", "/api/loans/apply"),
        ("get", "/api/loans/applications"),
        ("get", "/api/reports/audit-logs"),
        ("get", "/api/admin/parameters"),
    ],
)
def test_protected_endpoints_require_a_token(client, method, path):
    r = getattr(client, method)(path, json={})
    assert r.status_code == 401


def test_customer_cannot_verify_payments(client, make_user, auth_header):
    """Only staff can move a payment out of REPORTED - see
    app/services/payment_processing.py's decoupling."""
    ch = auth_header(make_user("customer"))
    r = client.post("/api/payments/1/verify", headers=ch, json={"decision": "verified"})
    assert r.status_code == 403


def test_dashboard_shape_differs_by_role(client, make_user, auth_header):
    cust = client.get("/api/reports/dashboard", headers=auth_header(make_user("customer")))
    off = client.get("/api/reports/dashboard", headers=auth_header(make_user("loan_officer")))
    assert cust.get_json()["role"] == "customer"
    assert "outstanding_balance" in cust.get_json()["kpis"]
    assert off.get_json()["role"] == "loan_officer"
    assert "portfolio_at_risk_percent" in off.get_json()["kpis"]
