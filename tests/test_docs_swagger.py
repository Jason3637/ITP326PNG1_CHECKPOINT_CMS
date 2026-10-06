"""The Swagger spec at /api/swagger.json covers every Phase B2-B5 and Loan Officer workflow endpoint
with typed request/response models (not free-form JSON)."""

import pytest

# (method, path-with-{braces}) for every endpoint built in Phases B2-B5.
EXPECTED_ENDPOINTS = {
    ("post", "/auth/register"),
    ("post", "/auth/login"),
    ("post", "/auth/mfa/setup"),
    ("post", "/auth/mfa/verify-setup"),
    ("post", "/auth/mfa/verify-login"),
    ("post", "/auth/refresh"),
    ("get", "/auth/me"),
    ("get", "/users/profile"),
    ("post", "/users/documents"),
    ("get", "/users/documents"),
    ("get", "/users/documents/{document_id}/download"),
    ("get", "/users/{user_id}/documents"),
    ("get", "/accounts/summary"),
    ("post", "/loans/apply"),
    ("get", "/loans/prime-preview"),
    ("get", "/loans/applications"),
    ("get", "/loans/applications/mine"),
    ("post", "/loans/applications/{application_id}/officer-review"),
    ("post", "/loans/applications/{application_id}/request-action"),
    ("post", "/loans/applications/{application_id}/respond"),
    ("post", "/loans/applications/{application_id}/resume-review"),
    ("post", "/loans/applications/{application_id}/recommend"),
    ("post", "/loans/applications/{application_id}/admin-review"),
    ("post", "/loans/applications/{application_id}/reject"),
    ("post", "/loans/applications/{application_id}/decision"),
    ("post", "/loans/applications/{application_id}/disburse"),
    ("get", "/loans/mine"),
    ("post", "/loans/{loan_id}/write-off"),
    ("post", "/payments/repay"),
    ("post", "/payments/{transaction_id}/start-verification"),
    ("post", "/payments/{transaction_id}/verify"),
    ("get", "/payments/loan/{loan_id}"),
    ("get", "/reports/dashboard"),
    ("get", "/reports/audit-logs"),
    ("get", "/admin/parameters"),
    ("put", "/admin/parameters"),
    # Loan Officer workflow
    ("post", "/loans/applications/{application_id}/assign"),
    ("post", "/loans/applications/{application_id}/return-to-officer"),
    ("get", "/officer/queues"),
    ("get", "/officer/queues/{queue}"),
    ("get", "/officer/applications/{application_id}"),
    ("get", "/officer/applications/{application_id}/checklist"),
    ("patch", "/officer/applications/{application_id}/checklist/{item_type}"),
    ("get", "/officer/applications/{application_id}/customer-history"),
    # Customer verification
    ("post", "/officer/applications/{application_id}/customer-verification/invalidate"),
    ("post", "/admin/customer-verifications/invalidate-outdated"),
    # Administrator operations
    ("get", "/admin/queues"),
    ("get", "/admin/queues/{queue}"),
    ("get", "/admin/applications/{application_id}"),
    ("get", "/admin/applications/{application_id}/customer-history"),
    ("post", "/admin/applications/{application_id}/approve"),
    ("post", "/admin/applications/{application_id}/reject"),
    ("post", "/admin/applications/{application_id}/return-to-officer"),
    ("post", "/admin/applications/{application_id}/disbursement-evidence"),
    ("post", "/admin/applications/{application_id}/disbursement"),
    ("get", "/admin/loans"),
    ("get", "/admin/loans/{loan_id}"),
    ("post", "/admin/loans/{loan_id}/write-off"),
    ("post", "/admin/loans/{loan_id}/clear-reapplication-block"),
    ("get", "/admin/repayments"),
    ("post", "/admin/repayments/{payment_id}/verify"),
    ("post", "/admin/repayments/{payment_id}/reject"),
    ("get", "/admin/pricing"),
    ("post", "/admin/pricing"),
    ("get", "/admin/penalty-policy"),
    ("post", "/admin/penalty-policy"),
    ("get", "/admin/analytics"),
    # Staff account administration
    ("post", "/admin/staff"),
    ("post", "/admin/staff/{user_id}/reset-password"),
}

# Endpoints that take a JSON body and must declare a body model.
BODY_ENDPOINTS = {
    ("post", "/auth/register"),
    ("post", "/auth/login"),
    ("post", "/auth/mfa/verify-setup"),
    ("post", "/auth/mfa/verify-login"),
    ("post", "/loans/apply"),
    ("post", "/loans/applications/{application_id}/request-action"),
    ("post", "/loans/applications/{application_id}/respond"),
    ("post", "/loans/applications/{application_id}/recommend"),
    ("post", "/loans/applications/{application_id}/reject"),
    ("post", "/loans/applications/{application_id}/decision"),
    ("post", "/loans/applications/{application_id}/disburse"),
    ("post", "/loans/{loan_id}/write-off"),
    ("post", "/payments/repay"),
    ("post", "/payments/{transaction_id}/verify"),
    ("put", "/admin/parameters"),
    ("post", "/admin/staff"),
    ("post", "/admin/applications/{application_id}/reject"),
    ("post", "/admin/applications/{application_id}/return-to-officer"),
    ("post", "/admin/applications/{application_id}/disbursement"),
    ("post", "/admin/loans/{loan_id}/write-off"),
    ("post", "/admin/repayments/{payment_id}/reject"),
    ("post", "/admin/pricing"),
    ("post", "/admin/penalty-policy"),
    ("post", "/admin/applications/{application_id}/approve"),
    ("post", "/loans/applications/{application_id}/resume-review"),
    ("post", "/loans/applications/{application_id}/assign"),
    ("post", "/loans/applications/{application_id}/return-to-officer"),
    ("patch", "/officer/applications/{application_id}/checklist/{item_type}"),
}


@pytest.fixture
def spec(client):
    r = client.get("/api/swagger.json")
    assert r.status_code == 200
    return r.get_json()


def test_swagger_lists_every_endpoint(spec):
    found = {
        (method, path)
        for path, ops in spec["paths"].items()
        for method in ops
        if method in {"get", "post", "put", "patch", "delete"}
    }
    missing = EXPECTED_ENDPOINTS - found
    assert not missing, f"endpoints absent from Swagger: {sorted(missing)}"


def test_body_endpoints_reference_a_model(spec):
    definitions = spec.get("definitions", {})
    assert definitions, "no models defined in the spec at all"

    for method, path in BODY_ENDPOINTS:
        op = spec["paths"][path][method]
        body_params = [p for p in op.get("parameters", []) if p.get("in") == "body"]
        assert body_params, f"{method.upper()} {path} has no body model"
        ref = body_params[0].get("schema", {}).get("$ref", "")
        model_name = ref.split("/")[-1]
        assert model_name in definitions, f"{method.upper()} {path} -> unknown model {ref}"


def test_endpoints_document_success_responses(spec):
    for method, path in EXPECTED_ENDPOINTS:
        op = spec["paths"][path][method]
        codes = set(op.get("responses", {}))
        assert codes & {"200", "201"}, f"{method.upper()} {path} documents no success response"


def test_protected_endpoints_declare_bearer_security(spec):
    # everything except the two unauthenticated auth entry points
    public = {("post", "/auth/register"), ("post", "/auth/login")}
    for method, path in EXPECTED_ENDPOINTS - public:
        op = spec["paths"][path][method]
        assert any("Bearer" in s for s in op.get("security", [])), (
            f"{method.upper()} {path} is missing the Bearer security declaration"
        )
