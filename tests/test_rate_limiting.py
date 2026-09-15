"""Rate limiting on the auth endpoints (register, mfa/setup, mfa/verify-setup,
login, mfa/verify-login) - added per the hardening review. Confirms actual
enforcement (not just that the decorator is present) and that a 429 never
leaks anything about the request that tripped it (e.g. whether an email is
registered)."""

import pyotp


def _post(client, path, json=None, headers=None, ip="9.9.9.9"):
    return client.post(path, json=json, headers=headers, environ_overrides={"REMOTE_ADDR": ip})


def test_login_endpoint_is_rate_limited(client, enrolled_customer):
    acct = enrolled_customer()
    body = {"email": acct["email"], "password": "wrong-password"}

    statuses = [_post(client, "/api/auth/login", body).status_code for _ in range(5)]
    assert statuses == [401] * 5  # under the limit: real auth logic still runs

    r = _post(client, "/api/auth/login", body)
    assert r.status_code == 429


def test_rate_limit_response_body_is_generic_and_json(client, enrolled_customer):
    acct = enrolled_customer()
    body = {"email": acct["email"], "password": "wrong-password"}
    for _ in range(5):
        _post(client, "/api/auth/login", body)

    r = _post(client, "/api/auth/login", body)
    assert r.status_code == 429
    data = r.get_json()
    assert data is not None, "429 body must be JSON, matching every other error response"
    assert list(data.keys()) == ["message"]  # same {"message": ...} shape as other errors
    text = data["message"].lower()
    # must not name the endpoint, the email, or anything account-specific
    assert acct["email"] not in data["message"]
    assert "login" not in text or "too many" in text  # generic wording only
    for leaky_word in ("password", "exist", "account", "user", "email"):
        assert leaky_word not in text


def test_rate_limit_does_not_reveal_whether_email_exists(client, enrolled_customer):
    """The 429 for a real, registered email must be byte-for-byte identical
    to the 429 for an email nobody ever registered - otherwise the rate
    limiter itself becomes an account-enumeration oracle."""
    acct = enrolled_customer()

    for _ in range(6):
        r_real = _post(
            client, "/api/auth/login",
            {"email": acct["email"], "password": "wrong-password"}, ip="1.1.1.1",
        )
    for _ in range(6):
        r_fake = _post(
            client, "/api/auth/login",
            {"email": "nobody-has-this-email@example.com", "password": "wrong-password"}, ip="2.2.2.2",
        )

    assert r_real.status_code == 429
    assert r_fake.status_code == 429
    assert r_real.get_json() == r_fake.get_json()


def test_rate_limit_is_scoped_per_ip(client, enrolled_customer):
    """Two different client IPs each get their own budget - one abusive IP
    must not lock out every other user of the app."""
    acct = enrolled_customer()
    body = {"email": acct["email"], "password": "wrong-password"}

    for _ in range(5):
        _post(client, "/api/auth/login", body, ip="10.0.0.1")
    tripped = _post(client, "/api/auth/login", body, ip="10.0.0.1")
    assert tripped.status_code == 429

    still_fine = _post(client, "/api/auth/login", body, ip="10.0.0.2")
    assert still_fine.status_code == 401  # a different IP is unaffected


def test_mfa_verify_login_is_rate_limited(client, enrolled_customer):
    acct = enrolled_customer()
    r = _post(client, "/api/auth/login", {"email": acct["email"], "password": acct["password"]})
    challenge_hdr = {"Authorization": f"Bearer {r.get_json()['mfa_challenge_token']}"}

    for _ in range(5):
        r = _post(client, "/api/auth/mfa/verify-login", {"code": "000000"}, headers=challenge_hdr)
        assert r.status_code == 401

    r = _post(client, "/api/auth/mfa/verify-login", {"code": "000000"}, headers=challenge_hdr)
    assert r.status_code == 429


def test_mfa_verify_setup_is_rate_limited(client):
    r = _post(client, "/api/auth/register", {
        "email": "ratelimit-verify-setup@test.local", "password": "password123", "full_name": "R",
    })
    setup_hdr = {"Authorization": f"Bearer {r.get_json()['mfa_setup_token']}"}

    for _ in range(5):
        r = _post(client, "/api/auth/mfa/verify-setup", {"code": "000000"}, headers=setup_hdr)
        assert r.status_code == 400

    r = _post(client, "/api/auth/mfa/verify-setup", {"code": "000000"}, headers=setup_hdr)
    assert r.status_code == 429


def test_register_endpoint_is_rate_limited(client):
    for i in range(10):
        r = _post(client, "/api/auth/register", {
            "email": f"spam{i}@test.local", "password": "password123", "full_name": "Spam",
        })
        assert r.status_code == 201

    r = _post(client, "/api/auth/register", {
        "email": "spam-overflow@test.local", "password": "password123", "full_name": "Spam",
    })
    assert r.status_code == 429


def test_mfa_setup_endpoint_is_rate_limited(client):
    # Calling /mfa/setup repeatedly just regenerates the secret each time
    # (totp_enabled only flips on verify-setup) - all 10 legitimately return
    # 200, and it's the 11th that should be throttled.
    r = _post(client, "/api/auth/register", {
        "email": "setup-limit@test.local", "password": "password123", "full_name": "S",
    })
    setup_hdr = {"Authorization": f"Bearer {r.get_json()['mfa_setup_token']}"}

    for _ in range(10):
        r = _post(client, "/api/auth/mfa/setup", headers=setup_hdr)
        assert r.status_code == 200

    r = _post(client, "/api/auth/mfa/setup", headers=setup_hdr)
    assert r.status_code == 429


def test_non_auth_endpoints_are_not_rate_limited(client, make_user, auth_header):
    """The limiter is applied per-endpoint, not as an app-wide default - a
    non-auth endpoint must tolerate more than 5 calls/minute without 429s."""
    ch = auth_header(make_user("customer"))
    statuses = [
        _post(client, "/api/loans/apply", {
            "amount_requested": 150, "term_months": 3, "repayment_frequency": "monthly",
        }, headers=ch).status_code
        for _ in range(8)
    ]
    assert 429 not in statuses
    assert statuses[0] == 201
    assert all(s == 409 for s in statuses[1:])  # duplicate open application, not rate limiting


def test_successful_login_before_the_limit_still_works(client, enrolled_customer):
    """The limiter must not interfere with normal, well-behaved use."""
    acct = enrolled_customer()
    r = _post(client, "/api/auth/login", {"email": acct["email"], "password": acct["password"]})
    assert r.status_code == 200
    challenge_hdr = {"Authorization": f"Bearer {r.get_json()['mfa_challenge_token']}"}
    r = _post(
        client, "/api/auth/mfa/verify-login",
        {"code": pyotp.TOTP(acct["totp_secret"]).now()}, headers=challenge_hdr,
    )
    assert r.status_code == 200
    assert r.get_json()["access_token"]
