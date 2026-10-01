"""The full LoanApplication status lifecycle, in one continuous walk:

SUBMITTED -> OFFICER_REVIEW -> CUSTOMER_ACTION_REQUIRED -> OFFICER_REVIEW
    -> RECOMMENDED_FOR_APPROVAL -> ADMIN_REVIEW -> APPROVED
    -> AWAITING_DISBURSEMENT -> (disburse) Loan ACTIVE

Two things this specifically proves, beyond the individual-hop tests
elsewhere: (1) the CUSTOMER_ACTION_REQUIRED detour goes through the
customer's own /respond endpoint (not just the officer's resume-review) and
never creates a second application row, and (2) APPROVED/AWAITING_DISBURSEMENT
are genuinely separable from an active Loan - nothing shows up under
/loans/mine until disburse() is explicitly called.
"""

from app.models import LoanApplication

import _workflow


def test_full_lifecycle_single_row_no_premature_loan(
    client, make_user, auth_header, apply_payload
):
    customer = make_user("customer")
    officer = make_user("loan_officer")
    admin = make_user("admin")
    ch, oh, ah = auth_header(customer), auth_header(officer), auth_header(admin)

    def _count_rows():
        return LoanApplication.query.count()

    # SUBMITTED
    r = client.post("/api/loans/apply", headers=ch, json=apply_payload(amount_requested=500))
    assert r.status_code == 201, r.get_json()
    app_id = r.get_json()["id"]
    assert r.get_json()["status"] == "submitted"
    assert r.get_json()["status_label"] == "Submitted"
    assert _count_rows() == 1

    # SUBMITTED -> OFFICER_REVIEW
    r = client.post(f"/api/loans/applications/{app_id}/officer-review", headers=oh)
    assert r.status_code == 200
    assert r.get_json()["id"] == app_id
    assert r.get_json()["status"] == "officer_review"
    assert r.get_json()["status_label"] == "Under Review"
    assert _count_rows() == 1

    # OFFICER_REVIEW -> CUSTOMER_ACTION_REQUIRED
    r = _workflow.request_info(client, app_id, oh, reason="Please add a second referee.")
    assert r.status_code == 200
    assert r.get_json()["status"] == "customer_action_required"
    assert r.get_json()["status_label"] == "Action Required"
    assert r.get_json()["action_required_note"] == "Please add a second referee."
    assert _count_rows() == 1

    # CUSTOMER_ACTION_REQUIRED -> OFFICER_REVIEW, via the CUSTOMER's own
    # /respond endpoint (not the officer's resume-review) - same row, updated.
    request_id = _workflow.open_request_ids(client, app_id, ch)[0]
    r = client.post(
        f"/api/loans/applications/{app_id}/respond",
        headers=ch,
        json={
            "responses": [
                {"information_request_id": request_id, "response_note": "Added Maria Kaupa."}
            ],
            "referees": [
                {"full_name": "John Doe", "relationship": "sibling", "mobile_number": "+675 700 0001"},
                {"full_name": "Maria Kaupa", "relationship": "friend", "mobile_number": "+675 700 0002"},
            ],
        },
    )
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["id"] == app_id, "must update the SAME row, never create a new one"
    assert r.get_json()["status"] == "officer_review"
    assert r.get_json()["action_required_note"] is None
    assert len(r.get_json()["referees"]) == 2
    assert _count_rows() == 1, "the CUSTOMER_ACTION_REQUIRED round trip must not duplicate the application"

    # OFFICER_REVIEW -> RECOMMENDED_FOR_APPROVAL
    r = _workflow.recommend(client, app_id, oh, comments="Referees confirmed.")
    assert r.status_code == 200
    assert r.get_json()["application"]["status"] == "recommended_for_approval"
    assert r.get_json()["application"]["status_label"] == "Under Review", "internal staff stages stay hidden from the customer"
    assert _count_rows() == 1

    # RECOMMENDED_FOR_APPROVAL -> ADMIN_REVIEW
    r = client.post(f"/api/loans/applications/{app_id}/admin-review", headers=ah)
    assert r.status_code == 200
    assert r.get_json()["status"] == "admin_review"
    assert _count_rows() == 1

    # ADMIN_REVIEW -> APPROVED -> AWAITING_DISBURSEMENT (one call, per the
    # state machine's design - see loan_processing.decide_application()).
    r = client.post(
        f"/api/loans/applications/{app_id}/decision",
        headers=ah,
        json={"decision": "approve", "note": "Approved at standard PRIME terms."},
    )
    assert r.status_code == 200, r.get_json()
    application = r.get_json()["application"]
    assert application["id"] == app_id
    assert application["status"] == "awaiting_disbursement"
    assert application["status_label"] == "Approved - Processing Disbursement"
    assert _count_rows() == 1

    # ---- point 3: approved-but-not-disbursed must NOT appear as an active loan ----
    r = client.get("/api/loans/mine", headers=ch)
    assert r.status_code == 200
    assert r.get_json()["count"] == 0, "no Loan exists until disburse() is explicitly called"

    # AWAITING_DISBURSEMENT -> Loan created, ACTIVE
    r = client.post(
        f"/api/loans/applications/{app_id}/disburse",
        headers=ah,
        json={"method": "cash_on_hand"},
    )
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["application"]["id"] == app_id
    assert r.get_json()["loan"]["status"] == "active"
    assert _count_rows() == 1, "disbursement never creates a second application row either"

    # NOW it appears as an active loan.
    r = client.get("/api/loans/mine", headers=ch)
    assert r.get_json()["count"] == 1
    assert r.get_json()["loans"][0]["status"] == "active"
    assert r.get_json()["loans"][0]["application_id"] == app_id


def test_early_rejection_from_officer_review_is_terminal_and_creates_no_loan(
    client, make_user, auth_header, apply_payload
):
    customer = make_user("customer")
    officer = make_user("loan_officer")
    admin = make_user("admin")
    ch, oh, ah = auth_header(customer), auth_header(officer), auth_header(admin)

    r = client.post("/api/loans/apply", headers=ch, json=apply_payload())
    app_id = r.get_json()["id"]
    client.post(f"/api/loans/applications/{app_id}/officer-review", headers=oh)

    # Early-exit rejection is the admin's call; the officer can only recommend it.
    r = client.post(
        f"/api/loans/applications/{app_id}/reject", headers=ah, json={"note": "Score too low."}
    )
    assert r.status_code == 200
    assert r.get_json()["status"] == "rejected"
    assert r.get_json()["status_label"] == "Not Approved"

    # Terminal: no further transition is accepted from REJECTED.
    r = client.post(f"/api/loans/applications/{app_id}/officer-review", headers=oh)
    assert r.status_code == 409

    r = client.get("/api/loans/mine", headers=ch)
    assert r.get_json()["count"] == 0


def test_admin_rejection_from_admin_review_creates_no_loan(
    client, make_user, auth_header, apply_payload
):
    customer = make_user("customer")
    officer = make_user("loan_officer")
    admin = make_user("admin")
    ch, oh, ah = auth_header(customer), auth_header(officer), auth_header(admin)

    r = client.post("/api/loans/apply", headers=ch, json=apply_payload())
    app_id = r.get_json()["id"]
    client.post(f"/api/loans/applications/{app_id}/officer-review", headers=oh)
    _workflow.recommend(client, app_id, oh)
    client.post(f"/api/loans/applications/{app_id}/admin-review", headers=ah)

    r = client.post(
        f"/api/loans/applications/{app_id}/decision",
        headers=ah,
        json={"decision": "reject", "note": "Insufficient affordability."},
    )
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["application"]["status"] == "rejected"

    r = client.get("/api/loans/mine", headers=ch)
    assert r.get_json()["count"] == 0

    # disburse must be refused on a rejected application
    r = client.post(
        f"/api/loans/applications/{app_id}/disburse", headers=ah, json={"method": "cash_on_hand"}
    )
    assert r.status_code == 409
