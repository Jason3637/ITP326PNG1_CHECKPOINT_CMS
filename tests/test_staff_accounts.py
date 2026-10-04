"""Admin staff-account administration: POST /api/admin/staff and
POST /api/admin/staff/<id>/reset-password.

Admin only; staff roles only (customers register publicly); the temporary
password is returned once and appears nowhere else - not in a later GET,
not in the audit log; new accounts go through the normal MFA setup on first
login; a reset keeps MFA enrolment.
"""

import json

import pyotp
import pytest

from app.extensions import db
from app.models import AuditLog, MfaBackupCode, User

CREATE = "/api/admin/staff"


def _reset_url(user_id):
    return f"/api/admin/staff/{user_id}/reset-password"


@pytest.fixture
def admin(make_user, auth_header):
    user = make_user("admin", full_name="Ada Admin")
    return {"user": user, "h": auth_header(user)}


def _create(client, admin_h, **overrides):
    body = {"email": "jane.officer@test.local", "full_name": "Jane Officer", "role": "loan_officer"}
    body.update(overrides)
    return client.post(CREATE, headers=admin_h, json=body)


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def _login(client, email, password):
    return client.post("/api/auth/login", json={"email": email, "password": password})


def _first_login_with_mfa_setup(client, email, password):
    """The public first-login flow: login -> setup -> verify-setup -> login
    -> verify-login. Returns (totp_secret, tokens)."""
    r = _login(client, email, password)
    assert r.status_code == 403 and r.get_json()["mfa_required"] == "setup", r.get_json()
    setup_h = _bearer(r.get_json()["mfa_setup_token"])
    r = client.post("/api/auth/mfa/setup", headers=setup_h)
    assert r.status_code == 200, r.get_json()
    secret = r.get_json()["totp_secret"]
    r = client.post("/api/auth/mfa/verify-setup", headers=setup_h, json={"code": pyotp.TOTP(secret).now()})
    assert r.status_code == 200, r.get_json()

    r = _login(client, email, password)
    assert r.status_code == 200 and r.get_json()["mfa_required"] == "challenge", r.get_json()
    r = client.post(
        "/api/auth/mfa/verify-login",
        headers=_bearer(r.get_json()["mfa_challenge_token"]),
        json={"code": pyotp.TOTP(secret).now()},
    )
    assert r.status_code == 200, r.get_json()
    return secret, r.get_json()


# ----------------------------------------------------------------------- RBAC
@pytest.mark.parametrize("role", ["loan_officer", "customer"])
def test_non_admins_cannot_create_staff_or_reset_passwords(client, make_user, auth_header, role):
    h = auth_header(make_user(role))
    target = make_user("loan_officer")
    assert _create(client, h).status_code == 403
    assert client.post(_reset_url(target.id), headers=h).status_code == 403
    assert User.query.filter_by(email="jane.officer@test.local").first() is None


def test_no_token_is_401(client, make_user):
    target = make_user("loan_officer")
    assert client.post(CREATE, json={}).status_code == 401
    assert client.post(_reset_url(target.id)).status_code == 401


# --------------------------------------------------------------------- create
@pytest.mark.parametrize("role", ["loan_officer", "admin"])
def test_admin_creates_a_staff_account_that_must_set_up_mfa_on_first_login(client, admin, role):
    r = _create(client, admin["h"], role=role, email=f"new-{role}@test.local")
    assert r.status_code == 201, r.get_json()
    assert r.headers["Cache-Control"] == "no-store"
    body = r.get_json()
    temp = body["temporary_password"]
    assert len(temp) >= 16
    assert body["user"] | {"id": None, "created_at": None} == {
        "id": None, "created_at": None, "email": f"new-{role}@test.local", "full_name": "Jane Officer",
        "phone_number": None, "role": role, "is_active": True, "totp_enabled": False,
    }

    stored = db.session.get(User, body["user"]["id"])
    assert stored.password_hash != temp and stored.totp_enabled is False and stored.totp_secret is None

    # The same public first-login flow as seeded and registered accounts.
    _, tokens = _first_login_with_mfa_setup(client, f"new-{role}@test.local", temp)
    assert tokens["role"] == role
    queues = client.get("/api/officer/queues", headers=_bearer(tokens["access_token"]))
    assert queues.status_code == 200, "a working staff account"


def test_customer_role_and_bad_input_are_rejected(client, admin):
    r = _create(client, admin["h"], role="customer")
    assert r.status_code == 400 and "public registration" in r.get_json()["message"]
    for bad in ({"role": "superuser"}, {"role": None}, {"email": "not-an-email"}, {"full_name": " "},
                {"is_active": "yes"}):
        assert _create(client, admin["h"], **bad).status_code == 400, bad
    assert User.query.filter_by(email="jane.officer@test.local").first() is None


def test_duplicate_email_is_409(client, admin, make_user):
    make_user("customer", email="jane.officer@test.local")
    assert _create(client, admin["h"], email="Jane.Officer@test.local").status_code == 409


def test_inactive_account_can_be_created_but_cannot_log_in(client, admin):
    r = _create(client, admin["h"], is_active=False)
    assert r.status_code == 201 and r.get_json()["user"]["is_active"] is False
    assert _login(client, "jane.officer@test.local", r.get_json()["temporary_password"]).status_code == 403


# ---------------------------------------------------------------------- reset
def test_reset_replaces_the_password_and_keeps_mfa(client, admin):
    created = _create(client, admin["h"]).get_json()
    user_id, old = created["user"]["id"], created["temporary_password"]
    secret, _ = _first_login_with_mfa_setup(client, "jane.officer@test.local", old)
    backup_codes_before = MfaBackupCode.query.filter_by(user_id=user_id).count()

    r = client.post(_reset_url(user_id), headers=admin["h"])
    assert r.status_code == 200, r.get_json()
    assert r.headers["Cache-Control"] == "no-store"
    new = r.get_json()["temporary_password"]
    assert new != old and r.get_json()["user"]["totp_enabled"] is True

    assert _login(client, "jane.officer@test.local", old).status_code == 401, "old password is dead"
    r = _login(client, "jane.officer@test.local", new)
    assert r.status_code == 200 and r.get_json()["mfa_required"] == "challenge", "MFA kept - no re-enrolment"
    r = client.post(
        "/api/auth/mfa/verify-login",
        headers=_bearer(r.get_json()["mfa_challenge_token"]),
        json={"code": pyotp.TOTP(secret).now()},
    )
    assert r.status_code == 200, "the same authenticator still works"
    assert MfaBackupCode.query.filter_by(user_id=user_id).count() == backup_codes_before


def test_reset_before_mfa_setup_still_requires_setup(client, admin):
    created = _create(client, admin["h"]).get_json()
    new = client.post(_reset_url(created["user"]["id"]), headers=admin["h"]).get_json()["temporary_password"]
    r = _login(client, "jane.officer@test.local", new)
    assert r.status_code == 403 and r.get_json()["mfa_required"] == "setup"


def test_reset_only_targets_existing_staff(client, admin, make_user):
    customer = make_user("customer")
    r = client.post(_reset_url(customer.id), headers=admin["h"])
    assert r.status_code == 400
    assert client.post(_reset_url(999999), headers=admin["h"]).status_code == 404


# ---------------------------------------------- audit + the password stays put
def test_both_actions_are_audited_without_the_password(client, admin):
    created = _create(client, admin["h"]).get_json()
    user_id = created["user"]["id"]
    reset = client.post(_reset_url(user_id), headers=admin["h"]).get_json()

    for action in ("staff_account_created", "staff_password_reset"):
        entry = AuditLog.query.filter_by(action=action, entity_type="User", entity_id=str(user_id)).one()
        assert entry.actor_id == admin["user"].id and entry.actor_role == "admin"
        assert entry.created_at is not None
        assert entry.details["email"] == "jane.officer@test.local"

    everything = json.dumps([[a.action, a.details] for a in AuditLog.query.all()])
    for secret in (created["temporary_password"], reset["temporary_password"]):
        assert secret not in everything


def test_temporary_password_is_never_returned_by_a_later_get(client, admin):
    created = _create(client, admin["h"]).get_json()
    temp = created["temporary_password"]
    reset_temp = client.post(_reset_url(created["user"]["id"]), headers=admin["h"]).get_json()[
        "temporary_password"
    ]
    _, tokens = _first_login_with_mfa_setup(client, "jane.officer@test.local", reset_temp)
    staff_h = _bearer(tokens["access_token"])

    gets = [
        client.get("/api/auth/me", headers=staff_h),
        client.get("/api/users/profile", headers=staff_h),
        client.get("/api/reports/audit-logs?per_page=200", headers=admin["h"]),
        client.get("/api/reports/dashboard", headers=admin["h"]),
    ]
    for r in gets:
        assert r.status_code == 200, (r.request.path, r.get_json())
        text = r.get_data(as_text=True)
        assert temp not in text and reset_temp not in text, r.request.path
        assert "temporary_password" not in text and "password_hash" not in text, r.request.path


# ------------------------------------------------- reset signs out everywhere
def test_reset_revokes_every_existing_session(client, admin, make_user, auth_header):
    created = _create(client, admin["h"]).get_json()
    user_id, old = created["user"]["id"], created["temporary_password"]
    secret, tokens = _first_login_with_mfa_setup(client, "jane.officer@test.local", old)
    access_h, refresh_h = _bearer(tokens["access_token"]), _bearer(tokens["refresh_token"])
    pending = _login(client, "jane.officer@test.local", old).get_json()["mfa_challenge_token"]
    bystander_h = auth_header(make_user("loan_officer"))
    assert client.get("/api/auth/me", headers=access_h).status_code == 200

    new = client.post(_reset_url(user_id), headers=admin["h"]).get_json()["temporary_password"]

    assert client.get("/api/auth/me", headers=access_h).status_code == 401
    assert client.post("/api/auth/refresh", headers=refresh_h).status_code == 401
    code = {"code": pyotp.TOTP(secret).now()}
    assert client.post("/api/auth/mfa/verify-login", headers=_bearer(pending), json=code).status_code == 401
    assert client.get("/api/auth/me", headers=bystander_h).status_code == 200, "other users unaffected"
    assert client.get("/api/auth/me", headers=admin["h"]).status_code == 200

    r = _login(client, "jane.officer@test.local", new)
    r = client.post("/api/auth/mfa/verify-login", headers=_bearer(r.get_json()["mfa_challenge_token"]), json=code)
    assert r.status_code == 200
    assert client.get("/api/auth/me", headers=_bearer(r.get_json()["access_token"])).status_code == 200
    entry = AuditLog.query.filter_by(action="staff_password_reset", entity_id=str(user_id)).one()
    assert entry.details["sessions_revoked"] is True


def test_tokens_issued_before_token_versions_existed_still_work(app, client, make_user):
    """No "tv" claim counts as version 0 - deploying this signs nobody out."""
    from flask_jwt_extended import create_access_token

    user = make_user("loan_officer")
    with app.test_request_context():
        legacy = create_access_token(identity=str(user.id), additional_claims={"scope": "access", "role": "loan_officer"})
    assert client.get("/api/auth/me", headers=_bearer(legacy)).status_code == 200
