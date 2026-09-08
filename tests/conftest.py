"""Shared pytest fixtures.

Tests run against an in-memory SQLite database (see TestingConfig) built from
the models with ``db.create_all()`` - no migration and no Supabase involved.
"""

import uuid

import pyotp
import pytest
from sqlalchemy import event
from sqlalchemy.engine import Engine

from app import create_app
from app.extensions import db as _db


@event.listens_for(Engine, "connect")
def _sqlite_fk_pragma(dbapi_connection, _):
    """Enforce ON DELETE CASCADE etc. on SQLite (off by default)."""
    try:
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()
    except Exception:
        pass


@pytest.fixture
def app():
    application = create_app("testing")
    with application.app_context():
        _db.create_all()
        try:
            yield application
        finally:
            _db.session.remove()
            _db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def make_user(app):
    """Insert a user directly (used for staff roles, which have no signup path)."""
    from app.models import User
    from app.models.enums import UserRole
    from app.services import security

    def _make(role="customer", *, totp_enabled=True, is_active=True, **kw):
        user = User(
            email=kw.get("email", f"{role}-{uuid.uuid4().hex[:8]}@test.local"),
            password_hash=security.hash_password(kw.get("password", "password123")),
            full_name=kw.get("full_name", f"{role.title()} User"),
            role=UserRole(role),
            is_active=is_active,
            totp_enabled=totp_enabled,
        )
        _db.session.add(user)
        _db.session.commit()
        return user

    return _make


@pytest.fixture
def access_token(app):
    """Mint a real access token for a user object (bypasses the login UI)."""
    from app.api.auth.tokens import issue_auth_tokens

    def _token(user):
        with app.test_request_context():
            return issue_auth_tokens(user)["access_token"]

    return _token


@pytest.fixture
def auth_header(access_token):
    def _header(user):
        return {"Authorization": f"Bearer {access_token(user)}"}

    return _header


@pytest.fixture
def enrolled_customer(client):
    """Run the real register -> MFA setup -> verify flow; return the useful bits."""

    def _make(email=None, password="Sup3r-secret-pw"):
        email = email or f"cust-{uuid.uuid4().hex[:8]}@test.local"

        r = client.post(
            "/api/auth/register",
            json={"email": email, "password": password, "full_name": "Test Customer"},
        )
        assert r.status_code == 201, r.get_json()
        setup_token = r.get_json()["mfa_setup_token"]
        hdr = {"Authorization": f"Bearer {setup_token}"}

        r = client.post("/api/auth/mfa/setup", headers=hdr)
        assert r.status_code == 200, r.get_json()
        secret = r.get_json()["totp_secret"]

        r = client.post(
            "/api/auth/mfa/verify-setup",
            headers=hdr,
            json={"code": pyotp.TOTP(secret).now()},
        )
        assert r.status_code == 200, r.get_json()
        backup_codes = r.get_json()["backup_codes"]

        return {
            "email": email,
            "password": password,
            "totp_secret": secret,
            "backup_codes": backup_codes,
        }

    return _make
