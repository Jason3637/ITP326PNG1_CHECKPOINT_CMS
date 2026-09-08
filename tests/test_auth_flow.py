"""Full authentication flow: register -> MFA setup -> login -> MFA verify-login."""

import pyotp


def test_full_registration_and_login_flow(client):
    email = "flow@test.local"
    password = "Correct-horse-battery"

    # 1. register - no JWT, get an mfa_setup token
    r = client.post(
        "/api/auth/register",
        json={"email": email, "password": password, "full_name": "Flow User"},
    )
    assert r.status_code == 201
    body = r.get_json()
    assert "access_token" not in body
    setup_hdr = {"Authorization": f"Bearer {body['mfa_setup_token']}"}

    # 2. mfa/setup - get the shared secret + provisioning material
    r = client.post("/api/auth/mfa/setup", headers=setup_hdr)
    assert r.status_code == 200
    setup = r.get_json()
    secret = setup["totp_secret"]
    assert setup["provisioning_uri"].startswith("otpauth://totp/")
    assert setup["qr_code_png"].startswith("data:image/png;base64,")

    # 3. mfa/verify-setup - wrong code rejected, correct code enables MFA
    assert (
        client.post(
            "/api/auth/mfa/verify-setup", headers=setup_hdr, json={"code": "000000"}
        ).status_code
        == 400
    )
    r = client.post(
        "/api/auth/mfa/verify-setup",
        headers=setup_hdr,
        json={"code": pyotp.TOTP(secret).now()},
    )
    assert r.status_code == 200
    backup_codes = r.get_json()["backup_codes"]
    assert len(backup_codes) == 10

    # 4. login step 1 - password -> challenge token (still no access token)
    r = client.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200
    body = r.get_json()
    assert "access_token" not in body
    challenge_hdr = {"Authorization": f"Bearer {body['mfa_challenge_token']}"}

    # 5. login step 2 - TOTP -> real access + refresh tokens with role claim
    r = client.post(
        "/api/auth/mfa/verify-login",
        headers=challenge_hdr,
        json={"code": pyotp.TOTP(secret).now()},
    )
    assert r.status_code == 200
    tokens = r.get_json()
    assert tokens["access_token"] and tokens["refresh_token"]
    assert tokens["role"] == "customer"

    # 6. the access token works on a protected endpoint
    r = client.get(
        "/api/auth/me",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )
    assert r.status_code == 200
    assert r.get_json()["email"] == email
    assert r.get_json()["totp_enabled"] is True


def test_login_before_mfa_setup_is_rejected(client):
    email = "nomfa@test.local"
    client.post(
        "/api/auth/register",
        json={"email": email, "password": "password123", "full_name": "No MFA"},
    )
    r = client.post("/api/auth/login", json={"email": email, "password": "password123"})
    assert r.status_code == 403
    assert r.get_json()["mfa_required"] == "setup"
    assert "mfa_setup_token" in r.get_json()


def test_login_with_wrong_password_is_401(client, enrolled_customer):
    acct = enrolled_customer()
    r = client.post(
        "/api/auth/login", json={"email": acct["email"], "password": "wrong-password"}
    )
    assert r.status_code == 401
    assert "access_token" not in r.get_json()


def test_backup_code_logs_in_once(client, enrolled_customer):
    acct = enrolled_customer()

    def challenge():
        r = client.post(
            "/api/auth/login",
            json={"email": acct["email"], "password": acct["password"]},
        )
        return {"Authorization": f"Bearer {r.get_json()['mfa_challenge_token']}"}

    code = acct["backup_codes"][0]
    r = client.post(
        "/api/auth/mfa/verify-login", headers=challenge(), json={"backup_code": code}
    )
    assert r.status_code == 200

    # same code cannot be reused
    r = client.post(
        "/api/auth/mfa/verify-login", headers=challenge(), json={"backup_code": code}
    )
    assert r.status_code == 401


def test_setup_scoped_token_cannot_call_protected_api(client):
    r = client.post(
        "/api/auth/register",
        json={"email": "scope@test.local", "password": "password123", "full_name": "S"},
    )
    setup_token = r.get_json()["mfa_setup_token"]
    r = client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {setup_token}"}
    )
    assert r.status_code == 401
