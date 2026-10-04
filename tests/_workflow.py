"""Loan Officer workflow steps as plain functions (client first), for tests
that need an application moved along the chain rather than testing these
steps themselves. Also exposed as the `workflow` fixture (see conftest).
Each returns the raw response so callers can still assert on it.
"""

from app.extensions import db
from app.models import Document, LoanApplication
from app.models.enums import DocumentType, IdDocumentType
from app.services import verification

ADULT_DOB = "1990-05-01"


def id_document_for(app_id) -> int:
    """The application's customer's current ID document - created if they
    have none (most tests never upload one), as the upload service would."""
    application = db.session.get(LoanApplication, app_id)
    doc = Document.query.filter_by(
        user_id=application.user_id, document_type=DocumentType.ID_VERIFICATION, superseded_by_id=None
    ).first()
    if doc is None:
        doc = Document(
            user_id=application.user_id,
            document_type=DocumentType.ID_VERIFICATION,
            id_document_type=IdDocumentType.NATIONAL_ID,
            storage_path=f"users/{application.user_id}/id_verification/test_national_id-id.png",
        )
        db.session.add(doc)
        db.session.commit()
    return doc.id


def uploaded_document(app_id, document_type="proof_of_income") -> int:
    """A document the application's customer has just uploaded and not yet
    attached to anything - what POST /users/documents leaves behind before
    the respond call links it."""
    application = db.session.get(LoanApplication, app_id)
    doc = Document(
        user_id=application.user_id,
        document_type=DocumentType(document_type),
        storage_path=f"users/{application.user_id}/{document_type}/upload-{id(object())}.pdf",
    )
    db.session.add(doc)
    db.session.commit()
    return doc.id


def evidence_for(app_id, item_key) -> dict:
    """What verifying each identity check needs (see verification._parse_evidence)."""
    if item_key == "age_18_plus":
        return {"date_of_birth": ADULT_DOB}
    if item_key == "valid_id":
        return {"id_document_id": id_document_for(app_id)}
    return {}


def complete_checklist(client, app_id, officer_headers):
    r = None
    for item in verification.CHECKLIST:
        r = client.patch(
            f"/api/officer/applications/{app_id}/checklist/{item.key}",
            headers=officer_headers,
            json={"status": "verified", "note": "ok", **evidence_for(app_id, item.key)},
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
