"""The RBAC matrix (Phase 1) as an executable spec, checked against the
app's actual route table.

1. Every route's required roles - read from the live app, not a
   hand-kept list - must equal MATRIX below. A new route without a matrix
   entry, or a route whose roles drift, fails here and forces a decision.
2. Every role NOT allowed on a route gets 403 over real HTTP - in particular
   a loan_officer token against every admin-only route, and a customer token
   against every Loan Officer route.
3. Every role that IS allowed is not turned away by the role check.
4. No token -> 401 on every protected route.

The deeper "against a real record in the right status" checks for the
admin-only actions are in test_loan_officer_rbac.py.

Notes on STAFF routes: the role check is all there is for the read-only
officer views. In particular customer-history is TEAM-WIDE on purpose - any
loan_officer may view it for any open application, not just ones they
claimed (test_customer_history_is_team_wide_for_loan_officers in
test_loan_officer_api.py). The write actions that ARE limited to the
assignee (checklist, request info, resume, recommend) enforce that in the
service layer (`_require_assignee`).
"""

import re

import pytest

C, O, A = "customer", "loan_officer", "admin"
ANY = frozenset()  # @roles_required() with no roles: any authenticated user
STAFF = frozenset({O, A})
ADMIN = frozenset({A})
CUSTOMER = frozenset({C})
EVERYONE = frozenset({C, O, A})

MATRIX: dict[tuple[str, str], frozenset] = {
    # --- auth / profile ---------------------------------------------------
    ("GET", "/api/auth/me"): ANY,
    ("GET", "/api/users/profile"): ANY,
    ("GET", "/api/users/documents"): ANY,
    ("POST", "/api/users/documents"): CUSTOMER,
    ("GET", "/api/users/documents/<int:document_id>/download"): ANY,  # owner-or-staff in the service
    ("GET", "/api/users/<int:user_id>/documents"): STAFF,
    # --- customer ---------------------------------------------------------
    ("GET", "/api/accounts/summary"): CUSTOMER,
    ("POST", "/api/loans/apply"): CUSTOMER,
    ("GET", "/api/loans/prime-preview"): ANY,
    ("GET", "/api/loans/applications/mine"): CUSTOMER,
    ("POST", "/api/loans/applications/<int:application_id>/respond"): CUSTOMER,
    ("GET", "/api/loans/mine"): CUSTOMER,
    # --- Loan Officer (admin may also act in the officer role) ------------
    ("GET", "/api/loans/applications"): STAFF,
    ("POST", "/api/loans/applications/<int:application_id>/officer-review"): STAFF,
    ("POST", "/api/loans/applications/<int:application_id>/request-action"): STAFF,
    ("POST", "/api/loans/applications/<int:application_id>/resume-review"): STAFF,
    ("POST", "/api/loans/applications/<int:application_id>/recommend"): STAFF,
    ("GET", "/api/officer/queues"): STAFF,
    ("GET", "/api/officer/queues/<string:queue>"): STAFF,
    ("GET", "/api/officer/applications/<int:application_id>"): STAFF,
    ("GET", "/api/officer/applications/<int:application_id>/checklist"): STAFF,
    ("PATCH", "/api/officer/applications/<int:application_id>/checklist/<string:item_type>"): STAFF,
    ("GET", "/api/officer/applications/<int:application_id>/customer-history"): STAFF,  # team-wide, see notes
    ("POST", "/api/payments/<int:transaction_id>/start-verification"): STAFF,
    ("POST", "/api/officer/applications/<int:application_id>/customer-verification/invalidate"): STAFF,
    # --- Administrator only -----------------------------------------------
    ("POST", "/api/loans/applications/<int:application_id>/assign"): ADMIN,
    ("POST", "/api/loans/applications/<int:application_id>/admin-review"): ADMIN,
    ("POST", "/api/loans/applications/<int:application_id>/return-to-officer"): ADMIN,
    ("POST", "/api/loans/applications/<int:application_id>/decision"): ADMIN,
    ("POST", "/api/loans/applications/<int:application_id>/reject"): ADMIN,
    ("POST", "/api/loans/applications/<int:application_id>/disburse"): ADMIN,
    ("POST", "/api/payments/<int:transaction_id>/verify"): ADMIN,
    ("POST", "/api/loans/<int:loan_id>/write-off"): ADMIN,
    ("GET", "/api/admin/parameters"): ADMIN,
    ("PUT", "/api/admin/parameters"): ADMIN,
    ("GET", "/api/reports/audit-logs"): ADMIN,
    ("POST", "/api/admin/customer-verifications/invalidate-outdated"): ADMIN,
    ("POST", "/api/admin/staff"): ADMIN,
    ("POST", "/api/admin/staff/<int:user_id>/reset-password"): ADMIN,
    # --- Administrator operations API (all admin-only) --------------------
    ("GET", "/api/admin/queues"): ADMIN,
    ("GET", "/api/admin/queues/<string:queue>"): ADMIN,
    ("GET", "/api/admin/applications/<int:application_id>"): ADMIN,
    ("GET", "/api/admin/applications/<int:application_id>/customer-history"): ADMIN,
    ("POST", "/api/admin/applications/<int:application_id>/approve"): ADMIN,
    ("POST", "/api/admin/applications/<int:application_id>/reject"): ADMIN,
    ("POST", "/api/admin/applications/<int:application_id>/return-to-officer"): ADMIN,
    ("POST", "/api/admin/applications/<int:application_id>/disbursement-evidence"): ADMIN,
    ("POST", "/api/admin/applications/<int:application_id>/disbursement"): ADMIN,
    ("GET", "/api/admin/loans"): ADMIN,
    ("GET", "/api/admin/loans/<int:loan_id>"): ADMIN,
    ("POST", "/api/admin/loans/<int:loan_id>/write-off"): ADMIN,
    ("POST", "/api/admin/loans/<int:loan_id>/clear-reapplication-block"): ADMIN,
    ("GET", "/api/admin/repayments"): ADMIN,
    ("POST", "/api/admin/repayments/<int:payment_id>/verify"): ADMIN,
    ("POST", "/api/admin/repayments/<int:payment_id>/reject"): ADMIN,
    ("GET", "/api/admin/pricing"): ADMIN,
    ("POST", "/api/admin/pricing"): ADMIN,
    ("GET", "/api/admin/penalty-policy"): ADMIN,
    ("POST", "/api/admin/penalty-policy"): ADMIN,
    ("GET", "/api/admin/analytics"): ADMIN,
    # --- shared -----------------------------------------------------------
    ("POST", "/api/payments/repay"): EVERYONE,  # customer: own loan; staff: counter entry
    ("GET", "/api/payments/loan/<int:loan_id>"): EVERYONE,  # customer: own loan only
    ("GET", "/api/reports/dashboard"): ANY,  # shape depends on role
}

# No role check at all: pre-login steps (MFA steps use their own step-token scope).
PUBLIC = {
    ("POST", "/api/auth/register"),
    ("POST", "/api/auth/login"),
    ("POST", "/api/auth/mfa/setup"),
    ("POST", "/api/auth/mfa/verify-setup"),
    ("POST", "/api/auth/mfa/verify-login"),
    ("POST", "/api/auth/refresh"),
    ("GET", "/api/swagger.json"),
}

# The Phase 1 boundary: actions a loan officer must never be able to take.
PHASE1_ADMIN_ONLY = {
    "final approve / reject": ("POST", "/api/loans/applications/<int:application_id>/decision"),
    "early-exit reject": ("POST", "/api/loans/applications/<int:application_id>/reject"),
    "record disbursement": ("POST", "/api/loans/applications/<int:application_id>/disburse"),
    "verify / reject repayments": ("POST", "/api/payments/<int:transaction_id>/verify"),
    "write off a loan": ("POST", "/api/loans/<int:loan_id>/write-off"),
}

_SAMPLE = {"queue": "awaiting_review", "item_type": "valid_id"}


def _route_table(app) -> tuple[dict, set]:
    protected, public = {}, set()
    for rule in app.url_map.iter_rules():
        view_class = getattr(app.view_functions[rule.endpoint], "view_class", None)
        if view_class is None:
            continue  # static files etc.
        for method in rule.methods - {"HEAD", "OPTIONS"}:
            roles = getattr(getattr(view_class, method.lower(), None), "required_roles", None)
            if roles is None:
                public.add((method, rule.rule))
            else:
                protected[(method, rule.rule)] = roles
    return protected, public


def _url(rule: str) -> str:
    """Fill path params: ids that don't exist (the role check runs first)."""
    return re.sub(
        r"<(?:(\w+):)?(\w+)>",
        lambda m: _SAMPLE.get(m.group(2), "999999") if m.group(1) == "string" else "999999",
        rule,
    )


def _call(client, method, rule, headers=None):
    return client.open(_url(rule), method=method, headers=headers or {}, json={})


@pytest.fixture
def headers(make_user, auth_header):
    return {role: auth_header(make_user(role)) for role in (C, O, A)}


# ----------------------------------------------------------- 1. the spec
def test_route_table_matches_the_rbac_matrix(app):
    protected, public = _route_table(app)
    unexpected = {k: sorted(v) for k, v in protected.items() if k not in MATRIX}
    assert not unexpected, f"routes missing from the RBAC matrix - decide who may call them: {unexpected}"
    missing = sorted(set(MATRIX) - set(protected))
    assert not missing, f"matrix entries with no route (renamed/removed?): {missing}"
    drift = {k: (sorted(protected[k]), sorted(v)) for k, v in MATRIX.items() if protected[k] != v}
    assert not drift, f"(actual, expected) roles differ: {drift}"
    assert public == PUBLIC, f"unexpected unprotected routes: {sorted(public - PUBLIC)}"


def test_phase1_admin_only_actions_are_admin_only_in_the_matrix():
    for action, route in PHASE1_ADMIN_ONLY.items():
        assert MATRIX[route] == ADMIN, action


# --------------------------------------------- 2. forbidden roles -> 403
def _forbidden(role):
    return sorted(k for k, roles in MATRIX.items() if roles and role not in roles)


@pytest.mark.parametrize("method,rule", _forbidden(O))
def test_loan_officer_token_is_refused_on_every_admin_only_route(client, headers, method, rule):
    r = _call(client, method, rule, headers[O])
    assert r.status_code == 403, (method, rule, r.status_code, r.get_json())


@pytest.mark.parametrize("method,rule", _forbidden(C))
def test_customer_token_is_refused_on_every_staff_route(client, headers, method, rule):
    r = _call(client, method, rule, headers[C])
    assert r.status_code == 403, (method, rule, r.status_code, r.get_json())


@pytest.mark.parametrize("method,rule", _forbidden(A))
def test_admin_is_refused_on_customer_only_routes(client, headers, method, rule):
    """Admins act on applications through staff routes, never as the customer."""
    r = _call(client, method, rule, headers[A])
    assert r.status_code == 403, (method, rule, r.status_code)


def test_every_loan_officer_route_is_closed_to_customers():
    officer_routes = {k for k, roles in MATRIX.items() if roles == STAFF}
    assert officer_routes <= set(_forbidden(C))
    assert len(officer_routes) == 14  # 13 workflow routes + member documents


# ---------------------------------------- 3. allowed roles get past RBAC
@pytest.mark.parametrize(
    "role,method,rule",
    sorted((role, *k) for k, roles in MATRIX.items() for role in (roles or {C, O, A})),
)
def test_allowed_roles_are_not_refused_by_the_role_check(client, headers, role, method, rule):
    r = _call(client, method, rule, headers[role])
    # 404 (no such record) / 400 (empty body) / 200 are all fine; a role
    # refusal would be 403 with this exact message.
    body = r.get_json(silent=True) or {}
    assert not (r.status_code == 403 and str(body.get("message", "")).startswith("Requires role")), (
        role, method, rule,
    )
    assert r.status_code != 401


# --------------------------------------------------- 4. no token -> 401
@pytest.mark.parametrize("method,rule", sorted(MATRIX))
def test_protected_routes_need_a_token(client, method, rule):
    assert _call(client, method, rule).status_code == 401
