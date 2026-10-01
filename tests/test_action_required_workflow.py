"""End-to-end round-trip of the Action Required / document-response loop:
officer requests action -> customer re-uploads a document and responds ->
application returns to OFFICER_REVIEW - verifying it's the SAME application
row, the old document is superseded-but-kept (not deleted or duplicated),
and every step is in AuditLog.
"""

from app.extensions import db
from app.models import AuditLog, Document, LoanApplication
from app.models.enums import DocumentType


def _make_document(user, application_id=None, *, document_type="id_verification"):
    doc = Document(
        user_id=user.id,
        loan_application_id=application_id,
        document_type=DocumentType(document_type),
        storage_path=f"users/{user.id}/{document_type}/test-{id(object())}.pdf",
    )
    db.session.add(doc)
    db.session.commit()
    return doc


def test_action_required_round_trip_with_document_replacement(
    client, make_user, auth_header, apply_payload
):
    customer = make_user("customer")
    officer = make_user("loan_officer")
    ch, oh = auth_header(customer), auth_header(officer)

    # 1. Apply, then attach an initial (blurry) ID verification document.
    r = client.post("/api/loans/apply", headers=ch, json=apply_payload(amount_requested=500))
    assert r.status_code == 201, r.get_json()
    app_id = r.get_json()["id"]
    old_doc = _make_document(customer, app_id)

    # 2. Officer opens review, then requests customer action.
    r = client.post(f"/api/loans/applications/{app_id}/officer-review", headers=oh)
    assert r.status_code == 200, r.get_json()

    r = client.post(
        f"/api/loans/applications/{app_id}/request-action",
        headers=oh,
        json={"note": "Please upload a clearer ID photo."},
    )
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["id"] == app_id
    assert body["status"] == "customer_action_required"
    assert body["action_required_note"] == "Please upload a clearer ID photo."

    # AuditLog: the request is recorded against this specific application.
    entry = AuditLog.query.filter_by(
        action="loan_application_customer_action_requested",
        entity_type="LoanApplication",
        entity_id=str(app_id),
    ).first()
    assert entry is not None
    assert entry.actor_id == officer.id

    # 3. Customer uploads a replacement document (not yet linked to the
    #    application - same as a fresh POST /users/documents call would leave it).
    new_doc = _make_document(customer, application_id=None)

    # 4. Customer responds - SAME application, transitions back to OFFICER_REVIEW.
    r = client.post(
        f"/api/loans/applications/{app_id}/respond",
        headers=ch,
        json={
            "response_note": "Uploaded a clearer photo.",
            "document_ids": [new_doc.id],
        },
    )
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["id"] == app_id, "must update the SAME application, never create a new one"
    assert body["status"] == "officer_review"
    assert body["action_required_note"] is None, "cleared once the customer responds"
    assert LoanApplication.query.count() == 1, "no new application row was created"

    # 5. Versioning: superseded-but-kept, not deleted, not duplicated as two
    #    equally-"current" rows.
    db.session.refresh(old_doc)
    db.session.refresh(new_doc)
    assert old_doc.superseded_by_id == new_doc.id
    assert new_doc.superseded_by_id is None
    assert new_doc.loan_application_id == app_id, "respond() linked it to the application"

    # 6. Officer's document view shows only the latest by default...
    r = client.get(f"/api/users/{customer.id}/documents", headers=oh)
    assert r.status_code == 200, r.get_json()
    docs = r.get_json()["documents"]
    assert [d["id"] for d in docs] == [new_doc.id]
    assert docs[0]["is_current"] is True

    # ...but the full history is still available on request (audit trail).
    r = client.get(f"/api/users/{customer.id}/documents?include_superseded=true", headers=oh)
    assert r.status_code == 200, r.get_json()
    ids = {d["id"] for d in r.get_json()["documents"]}
    assert ids == {old_doc.id, new_doc.id}
    by_id = {d["id"]: d for d in r.get_json()["documents"]}
    assert by_id[old_doc.id]["is_current"] is False
    assert by_id[old_doc.id]["superseded_by_id"] == new_doc.id

    # 7. AuditLog: the supersede event and the customer's response are both recorded.
    superseded_entry = AuditLog.query.filter_by(
        action="document_superseded", entity_type="Document", entity_id=str(old_doc.id)
    ).first()
    assert superseded_entry is not None
    assert superseded_entry.details["superseded_by_document_id"] == new_doc.id

    responded_entry = AuditLog.query.filter_by(
        action="loan_application_customer_responded",
        entity_type="LoanApplication",
        entity_id=str(app_id),
    ).first()
    assert responded_entry is not None
    assert responded_entry.actor_id == customer.id
    assert "documents" in responded_entry.details["changed_fields"]


def test_respond_requires_customer_action_required_status(
    client, make_user, auth_header, apply_payload
):
    """respond() must reject a call made while the application isn't
    actually waiting on the customer (e.g. still SUBMITTED)."""
    customer = make_user("customer")
    ch = auth_header(customer)
    r = client.post("/api/loans/apply", headers=ch, json=apply_payload())
    app_id = r.get_json()["id"]

    r = client.post(
        f"/api/loans/applications/{app_id}/respond",
        headers=ch,
        json={"response_note": "Nothing was requested yet."},
    )
    assert r.status_code == 409


def test_customer_cannot_respond_to_someone_elses_application(
    client, make_user, auth_header, apply_payload
):
    customer = make_user("customer")
    officer = make_user("loan_officer")
    other_customer = make_user("customer")
    ch, oh = auth_header(customer), auth_header(officer)
    other_ch = auth_header(other_customer)

    r = client.post("/api/loans/apply", headers=ch, json=apply_payload())
    app_id = r.get_json()["id"]
    client.post(f"/api/loans/applications/{app_id}/officer-review", headers=oh)
    client.post(
        f"/api/loans/applications/{app_id}/request-action",
        headers=oh,
        json={"note": "Please clarify your employer."},
    )

    r = client.post(
        f"/api/loans/applications/{app_id}/respond",
        headers=other_ch,
        json={"response_note": "Not my application."},
    )
    assert r.status_code == 403


def test_respond_without_documents_still_clears_the_note_and_updates_fields(
    client, make_user, auth_header, apply_payload
):
    """Confirms respond() also handles the "updated fields, no new
    documents" case - not every action-required request is about a file."""
    customer = make_user("customer")
    officer = make_user("loan_officer")
    ch, oh = auth_header(customer), auth_header(officer)

    r = client.post("/api/loans/apply", headers=ch, json=apply_payload())
    app_id = r.get_json()["id"]
    client.post(f"/api/loans/applications/{app_id}/officer-review", headers=oh)
    client.post(
        f"/api/loans/applications/{app_id}/request-action",
        headers=oh,
        json={"note": "Please confirm your employer's name."},
    )

    r = client.post(
        f"/api/loans/applications/{app_id}/respond",
        headers=ch,
        json={
            "response_note": "Updated my employer.",
            "employment_status": "self_employed",
        },
    )
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["status"] == "officer_review"
    assert body["action_required_note"] is None
    assert body["employment_status"] == "self_employed"
