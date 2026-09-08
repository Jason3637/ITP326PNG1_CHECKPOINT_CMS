"""The Swagger spec at /api/swagger.json covers every Phase B2-B5 endpoint
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
    ("get", "/loans/applications"),
    ("post", "/loans/applications/{application_id}/decision"),
    ("get", "/loans/mine"),
    ("post", "/payments/repay"),
    ("get", "/payments/loan/{loan_id}"),
    ("get", "/reports/dashboard"),
    ("get", "/reports/audit-logs"),
    ("get", "/admin/parameters"),
    ("put", "/admin/parameters"),
}

# Endpoints that take a JSON body and must declare a body model.
BODY_ENDPOINTS = {
    ("post", "/auth/register"),
    ("post", "/auth/login"),
    ("post", "/auth/mfa/verify-setup"),
    ("post", "/auth/mfa/verify-login"),
    ("post", "/loans/apply"),
    ("post", "/loans/applications/{application_id}/decision"),
    ("post", "/payments/repay"),
    ("put", "/admin/parameters"),
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
