"""Responding to Action Required: a request that names a required document
can only be answered with a newly uploaded document of that type - enforced
by the backend, not just the customer's form. A request for information
only (no required document) is answered by the note, with no file.
"""

import pytest

from app.extensions import db
from app.models import Document, InformationResponse, LoanApplication

import _workflow


@pytest.fixture
def asked(client, make_user, auth_header, apply_payload, workflow):
    """A claimed application with the officer's requests open. Returns
    (app_id, customer_headers, request(**fields) -> open)."""
    customer = make_user("customer")
    ch, oh = auth_header(customer), auth_header(make_user("loan_officer"))
    app_id = client.post("/api/loans/apply", headers=ch, json=apply_payload()).get_json()["id"]
    assert client.post(f"/api/loans/applications/{app_id}/officer-review", headers=oh).status_code == 200

    def _request(*items):
        r = client.post(
            f"/api/loans/applications/{app_id}/request-action", headers=oh, json={"requests": list(items)}
        )
        assert r.status_code == 200, r.get_json()

    return app_id, ch, _request


PAYSLIP = {
    "request_type": "missing_document",
    "reason": "Please upload a recent payslip.",
    "required_document_type": "proof_of_income",
}
INCOME = {
    "request_type": "information_mismatch",
    "reason": "Please confirm your monthly income.",
    "required_information": "Gross monthly income",
}


def _status(app_id):
    db.session.expire_all()
    return db.session.get(LoanApplication, app_id).status.value


def test_response_without_the_required_document_is_rejected(asked, workflow):
    app_id, ch, request = asked
    request(PAYSLIP)
    r = workflow.respond(app_id, ch, note="Sent it.")
    assert r.status_code == 400
    message = r.get_json()["message"]
    assert "proof of income" in message
    assert str(workflow.open_request_ids(app_id, ch)[0]) in message
    assert _status(app_id) == "customer_action_required", "nothing changed"
    assert InformationResponse.query.count() == 0


@pytest.mark.parametrize("which", ["wrong_type", "already_on_record", "someone_elses"])
def test_only_a_new_document_of_the_required_type_counts(
    asked, workflow, which, make_user
):
    app_id, ch, request = asked
    request(PAYSLIP)
    if which == "wrong_type":
        doc_id = _workflow.uploaded_document(app_id, "loan_file")
    elif which == "already_on_record":
        # Re-sending the file already attached to the application isn't a new upload.
        doc_id = _workflow.uploaded_document(app_id, "proof_of_income")
        db.session.get(Document, doc_id).loan_application_id = app_id
        db.session.commit()
    else:
        other = make_user("customer")
        doc = Document(user_id=other.id, document_type="proof_of_income", storage_path="users/x/p.pdf")
        db.session.add(doc)
        db.session.commit()
        doc_id = doc.id
    r = workflow.respond(app_id, ch, note="Sent it.", document_ids=[doc_id])
    assert r.status_code in (400, 403), r.get_json()
    assert _status(app_id) == "customer_action_required"


def test_response_with_the_required_document_succeeds(asked, workflow):
    app_id, ch, request = asked
    request(PAYSLIP)
    doc_id = _workflow.uploaded_document(app_id, "proof_of_income")
    r = workflow.respond(app_id, ch, note="Payslip attached.", document_ids=[doc_id])
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["status"] == "officer_review"
    assert db.session.get(Document, doc_id).loan_application_id == app_id
    assert InformationResponse.query.one().provided_document_ids == [doc_id]


def test_information_only_request_needs_no_file(asked, workflow):
    app_id, ch, request = asked
    request(INCOME)
    r = workflow.respond(app_id, ch, note="It is K650.", monthly_income=650)
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["status"] == "officer_review"
    assert InformationResponse.query.one().provided_document_ids is None


def test_mixed_round_needs_the_document_only_for_the_document_request(asked, workflow):
    app_id, ch, request = asked
    request(PAYSLIP, INCOME)
    assert workflow.respond(app_id, ch, note="Done.", monthly_income=650).status_code == 400
    doc_id = _workflow.uploaded_document(app_id, "proof_of_income")
    r = workflow.respond(app_id, ch, note="Done.", monthly_income=650, document_ids=[doc_id])
    assert r.status_code == 200, r.get_json()


def test_malformed_document_ids_are_a_validation_error(asked, workflow):
    app_id, ch, request = asked
    request(INCOME)
    for bad in ("12", ["abc"]):
        assert workflow.respond(app_id, ch, note="x", document_ids=bad).status_code == 400
