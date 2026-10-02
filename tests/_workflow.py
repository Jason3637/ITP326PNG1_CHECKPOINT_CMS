"""Loan Officer workflow steps as plain functions (client first), for tests
that need an application moved along the chain rather than testing these
steps themselves. Also exposed as the `workflow` fixture (see conftest).
Each returns the raw response so callers can still assert on it.
"""

from app.services import verification


def complete_checklist(client, app_id, officer_headers):
    r = None
    for item in verification.CHECKLIST:
        r = client.patch(
            f"/api/officer/applications/{app_id}/checklist/{item.key}",
            headers=officer_headers,
            json={"status": "verified", "note": "ok"},
        )
        assert r.status_code == 200, r.get_json()
    return r


def recommend(
    client, app_id, officer_headers, recommendation="recommend_approval", comments="Checks complete."
):
    if recommendation == "recommend_approval":
        complete_checklist(client, app_id, officer_headers)
    return client.post(
        f"/api/loans/applications/{app_id}/recommend",
        headers=officer_headers,
        json={"recommendation": recommendation, "comments": comments},
    )


def request_info(client, app_id, officer_headers, reason="Please confirm your employer.", **extra):
    item = {"request_type": "other", "reason": reason, **extra}
    return client.post(
        f"/api/loans/applications/{app_id}/request-action",
        headers=officer_headers,
        json={"requests": [item]},
    )


def open_request_ids(client, app_id, customer_headers):
    apps = client.get("/api/loans/applications/mine", headers=customer_headers).get_json()
    app = next(a for a in apps["applications"] if a["id"] == app_id)
    return [r["id"] for r in app["information_requests"] if r["status"] == "open"]


def respond(client, app_id, customer_headers, note="Done.", **fields):
    ids = open_request_ids(client, app_id, customer_headers)
    return client.post(
        f"/api/loans/applications/{app_id}/respond",
        headers=customer_headers,
        json={
            "responses": [{"information_request_id": i, "response_note": note} for i in ids],
            **fields,
        },
    )


def to_disbursed_loan(client, ch, oh, ah, apply_body):
    """Apply -> claim -> checklist -> recommend -> admin review -> approve
    -> disburse. Returns (application_id, loan_json)."""
    r = client.post("/api/loans/apply", headers=ch, json=apply_body)
    assert r.status_code == 201, r.get_json()
    app_id = r.get_json()["id"]
    assert client.post(f"/api/loans/applications/{app_id}/officer-review", headers=oh).status_code == 200
    r = recommend(client, app_id, oh)
    assert r.status_code == 200, r.get_json()
    assert client.post(f"/api/loans/applications/{app_id}/admin-review", headers=ah).status_code == 200
    r = client.post(
        f"/api/loans/applications/{app_id}/decision", headers=ah, json={"decision": "approve"}
    )
    assert r.status_code == 200, r.get_json()
    r = client.post(
        f"/api/loans/applications/{app_id}/disburse", headers=ah, json={"method": "cash_on_hand"}
    )
    assert r.status_code == 200, r.get_json()
    return app_id, r.get_json()["loan"]
