"""Audit logging and customer notifications for the Loan Officer workflow.

Every officer action leaves an AuditLog row with actor, the actor's role at
the time, action, entity, timestamp and before/after context - and nothing
in those rows is a secret, a document's contents, or a copy of the
customer's personal values.
"""

import json
from unittest.mock import patch

import pytest

from app.extensions import db
from app.models import AuditLog, User
from app.models.enums import UserRole

import _workflow


@pytest.fixture
def flow(client, make_user, auth_header, apply_payload, workflow):
    """Claim -> one checklist item -> request info -> customer responds ->
    recommend -> view customer history. Returns ids, people and the emails
    the notification router was asked to send."""
    customer = make_user("customer", full_name="Grace Waigani")
    officer = make_user("loan_officer")
    ch, oh = auth_header(customer), auth_header(officer)
    sent = []

    def _capture(to, subject, body):
        sent.append({"to": to, "subject": subject, "body": body})
        return {"sent": True, "reason": None}

    with patch("app.services.notifications.send_email", side_effect=_capture):
        app_id = client.post(
            "/api/loans/apply", headers=ch, json=apply_payload(monthly_income=800)
        ).get_json()["id"]
        client.post(f"/api/loans/applications/{app_id}/officer-review", headers=oh)
        client.patch(
            f"/api/officer/applications/{app_id}/checklist/valid_id",
            headers=oh,
            json={"status": "verified", "note": "NID checked.", "id_document_id": _workflow.id_document_for(app_id)},
        )
        r = client.post(
            f"/api/loans/applications/{app_id}/request-action",
            headers=oh,
            json={
                "requests": [
                    {
                        "request_type": "missing_document",
                        "reason": "Please upload a recent payslip.",
                        "required_document_type": "proof_of_income",
                        "internal_note": "SECRET-INTERNAL-NOTE",
                    }
                ]
            },
        )
        assert r.status_code == 200, r.get_json()
        r = workflow.respond(
            app_id, ch, note="Uploaded.", monthly_income=654321,
            document_ids=[_workflow.uploaded_document(app_id, "proof_of_income")],
        )
        assert r.status_code == 200, r.get_json()
        assert workflow.recommend(app_id, oh).status_code == 200
        r = client.get(f"/api/officer/applications/{app_id}/customer-history", headers=oh)
        assert r.status_code == 200
    return {"app_id": app_id, "customer": customer, "officer": officer, "sent": sent}


def _entries(app_id, action):
    return (
        AuditLog.query.filter_by(action=action, entity_type="LoanApplication", entity_id=str(app_id))
        .order_by(AuditLog.id)
        .all()
    )


def _one(app_id, action):
    rows = _entries(app_id, action)
    assert len(rows) >= 1, f"no {action} audit row"
    return rows[-1]


@pytest.mark.parametrize(
    "action,actor,expected_keys",
    [
        ("loan_application_officer_review_started", "officer",
         {"from", "to", "assigned_officer_id", "previous_assigned_officer_id", "checklist_opened"}),
        ("verification_item_updated", "officer", {"verification_item_id", "item_type", "from", "to"}),
        ("loan_application_customer_action_requested", "officer", {"from", "to", "requests"}),
        ("customer_action_required_notification", "officer", {"request_ids", "sent", "reason"}),
        ("loan_application_customer_responded", "customer",
         {"from", "to", "request_ids", "response_ids", "changed_fields", "changed_field_names"}),
        ("loan_application_recommended_for_approval", "officer",
         {"from", "to", "recommendation_id", "recommendation", "note", "checklist", "credit_score"}),
        ("customer_history_viewed", "officer", {"customer_id", "application_status"}),
    ],
)
def test_every_officer_action_is_audited_with_actor_role_and_context(flow, action, actor, expected_keys):
    entry = _one(flow["app_id"], action)
    person = flow[actor]
    assert entry.actor_id == person.id
    assert entry.actor_role == str(person.role)
    assert entry.created_at is not None
    assert entry.entity_type == "LoanApplication" and entry.entity_id == str(flow["app_id"])
    assert expected_keys <= set(entry.details), expected_keys - set(entry.details)


def test_before_and_after_context(flow):
    app_id = flow["app_id"]
    claim = _one(app_id, "loan_application_officer_review_started").details
    assert (claim["from"], claim["to"]) == ("submitted", "officer_review")
    assert claim["previous_assigned_officer_id"] is None
    assert claim["assigned_officer_id"] == flow["officer"].id
    assert "valid_id" in claim["checklist_opened"]

    item = _entries(app_id, "verification_item_updated")[0].details  # the first, NID tick
    assert item["from"] == {"status": "pending", "note": None}
    assert item["to"] == {"status": "verified", "note": "NID checked."}

    requested = _one(app_id, "loan_application_customer_action_requested").details
    assert (requested["from"], requested["to"]) == ("officer_review", "customer_action_required")
    assert requested["requests"][0]["required_document_type"] == "proof_of_income"

    responded = _one(app_id, "loan_application_customer_responded").details
    assert (responded["from"], responded["to"]) == ("customer_action_required", "officer_review")
    assert responded["changed_field_names"] == ["monthly_income"]
    assert responded["request_ids"] == [requested["requests"][0]["id"]]

    rec = _one(app_id, "loan_application_recommended_for_approval").details
    assert (rec["from"], rec["to"]) == ("officer_review", "recommended_for_approval")
    assert rec["checklist"]["verified"] == 8 and rec["checklist"]["pending"] == 0


def test_audit_rows_hold_no_secrets_document_contents_or_personal_values(flow):
    rows = AuditLog.query.filter_by(entity_id=str(flow["app_id"])).all()
    blob = json.dumps([r.details for r in rows])
    customer = db.session.get(User, flow["customer"].id)
    for secret in (customer.password_hash, flow["officer"].password_hash):
        assert secret not in blob
    assert "totp" not in blob.lower() and "storage_path" not in blob
    # The new income lives in the InformationResponse row, not copied into the ledger.
    assert "654321" not in blob


def test_actor_role_is_the_role_at_the_time(flow):
    officer = db.session.get(User, flow["officer"].id)
    officer.role = UserRole.ADMIN
    db.session.commit()
    entry = _one(flow["app_id"], "verification_item_updated")
    assert entry.actor_role == "loan_officer"


def test_audit_log_api_filters_by_role_and_entity(client, flow, make_user, auth_header):
    ah = auth_header(make_user("admin"))
    r = client.get(
        f"/api/reports/audit-logs?actor_role=loan_officer&entity_type=LoanApplication"
        f"&entity_id={flow['app_id']}&per_page=200",
        headers=ah,
    )
    assert r.status_code == 200, r.get_json()
    items = r.get_json()["items"]
    assert items and all(i["actor_role"] == "loan_officer" for i in items)
    actions = {i["action"] for i in items}
    assert {
        "loan_application_officer_review_started",
        "verification_item_updated",
        "loan_application_customer_action_requested",
        "loan_application_recommended_for_approval",
        "customer_history_viewed",
    } <= actions
    assert "loan_application_customer_responded" not in actions, "that one was the customer"


# ------------------------------------------------------------ notifications
def test_customer_is_emailed_when_action_is_required_without_the_internal_note(flow):
    emails = [e for e in flow["sent"] if e["subject"] == "Action needed on your loan application"]
    assert len(emails) == 1
    email = emails[0]
    assert email["to"] == flow["customer"].email
    assert "Please upload a recent payslip." in email["body"]
    assert "proof of income" in email["body"]
    assert "SECRET-INTERNAL-NOTE" not in email["body"]
    outcome = _one(flow["app_id"], "customer_action_required_notification").details
    assert outcome["sent"] is True


def test_internal_steps_do_not_email_the_customer(flow):
    subjects = [e["subject"] for e in flow["sent"]]
    # application received + action needed - nothing for claim, checklist,
    # the customer's own response, the recommendation or a history view.
    assert subjects == ["We've received your loan application", "Action needed on your loan application"]


def test_a_mail_failure_never_undoes_the_request(client, make_user, auth_header, apply_payload, workflow):
    ch, oh = auth_header(make_user("customer")), auth_header(make_user("loan_officer"))
    app_id = client.post("/api/loans/apply", headers=ch, json=apply_payload()).get_json()["id"]
    client.post(f"/api/loans/applications/{app_id}/officer-review", headers=oh)
    with patch(
        "app.services.notifications.send_email",
        return_value={"sent": False, "reason": "error: SMTP down"},
    ):
        r = workflow.request_info(app_id, oh)
    assert r.status_code == 200 and r.get_json()["status"] == "customer_action_required"
    outcome = _one(app_id, "customer_action_required_notification").details
    assert outcome == {"request_ids": outcome["request_ids"], "sent": False, "reason": "error: SMTP down"}


def test_received_email_states_the_prime_term(flow):
    received = next(e for e in flow["sent"] if e["subject"] == "We've received your loan application")
    assert "over 14 days" in received["body"] and "None" not in received["body"]
