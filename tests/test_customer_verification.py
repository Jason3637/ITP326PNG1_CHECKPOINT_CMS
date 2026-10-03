"""Customer-level verification: created from the Age 18+ and Valid ID
checklist items, carried over to a returning customer's next application,
and invalidated - never left stale - by each re-verification trigger.
"""

from datetime import date, timedelta

import pytest

from app.extensions import db
from app.models import AuditLog, CustomerVerification, Document, LoanApplication, User
from app.models.enums import (
    CustomerVerificationInvalidationReason as Reason,
    CustomerVerificationStatus as CVS,
    DocumentType,
    IdDocumentType,
)
from app.services import customer_verification as cv_service

import _workflow

DOB = "1990-05-01"


@pytest.fixture
def people(make_user, auth_header):
    customer = make_user("customer", full_name="Grace Waigani")
    officer, other = make_user("loan_officer", full_name="Olive Officer"), make_user("loan_officer")
    admin = make_user("admin")
    return {
        "customer": customer, "officer": officer, "other": other, "admin": admin,
        "ch": auth_header(customer), "oh": auth_header(officer), "xh": auth_header(other), "ah": auth_header(admin),
    }


def _apply(client, people, apply_payload, **overrides):
    r = client.post("/api/loans/apply", headers=people["ch"], json=apply_payload(**overrides))
    assert r.status_code == 201, r.get_json()
    return r.get_json()["id"]


def _claimed(client, people, apply_payload, **overrides):
    app_id = _apply(client, people, apply_payload, **overrides)
    assert client.post(f"/api/loans/applications/{app_id}/officer-review", headers=people["oh"]).status_code == 200
    return app_id


def _check(client, headers, app_id, item, status="verified", **body):
    return client.patch(
        f"/api/officer/applications/{app_id}/checklist/{item}", headers=headers, json={"status": status, **body}
    )


def _verify_identity(client, people, app_id, *, dob=DOB, expiry=None, headers=None):
    headers = headers or people["oh"]
    r = _check(client, headers, app_id, "age_18_plus", date_of_birth=dob)
    assert r.status_code == 200, r.get_json()
    body = {"id_document_id": _workflow.id_document_for(app_id)}
    if expiry:
        body["id_expiry_date"] = expiry
    r = _check(client, headers, app_id, "valid_id", **body)
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def _review(client, people, app_id):
    r = client.get(f"/api/officer/applications/{app_id}", headers=people["oh"])
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def _finish(client, people, app_id):
    """Close out a claimed application so the customer can apply again."""
    _workflow.recommend(client, app_id, people["oh"])
    client.post(f"/api/loans/applications/{app_id}/reject", headers=people["ah"], json={"note": "test"})


def _months_from_today(n: int) -> date:
    return cv_service._add_months(date.today(), n)


# ===================================================================== create
def test_completing_both_identity_checks_creates_the_verification(client, people, apply_payload):
    app_id = _claimed(client, people, apply_payload)
    assert CustomerVerification.query.count() == 0

    checklist = _verify_identity(client, people, app_id)

    row = CustomerVerification.query.one()
    assert row.status == CVS.VERIFIED
    assert row.user_id == people["customer"].id
    assert row.verified_by == people["officer"].id
    assert row.verified_at is not None
    assert row.date_of_birth == date(1990, 5, 1)
    assert row.id_document_id == _workflow.id_document_for(app_id)
    assert row.valid_until == _months_from_today(12)
    assert row.policy_version == "2026-10-v1"
    assert row.source_application_id == app_id
    assert row.verified_email == apply_payload()["confirmed_email"]

    items = {i["item_type"]: i for i in checklist["items"]}
    assert items["age_18_plus"]["customer_verification_id"] == row.id
    assert items["valid_id"]["customer_verification_id"] == row.id
    assert items["age_18_plus"]["evidence"] == {"date_of_birth": DOB}

    customer = _review(client, people, app_id)["customer"]
    assert customer["verification"]["id"] == row.id
    assert customer["verification"]["verified_by_name"] == "Olive Officer"
    assert customer["date_of_birth"] == DOB
    assert AuditLog.query.filter_by(action="customer_verified", entity_id=str(row.id)).count() == 1


def test_verified_by_is_whoever_completes_the_second_check(client, people, apply_payload):
    app_id = _claimed(client, people, apply_payload)
    assert _check(client, people["oh"], app_id, "age_18_plus", date_of_birth=DOB).status_code == 200
    assert CustomerVerification.query.count() == 0, "one check alone creates nothing"
    r = _check(client, people["ah"], app_id, "valid_id", id_document_id=_workflow.id_document_for(app_id))
    assert r.status_code == 200, r.get_json()
    assert CustomerVerification.query.one().verified_by == people["admin"].id


def test_date_of_birth_shows_without_a_verification(client, people, apply_payload):
    app_id = _claimed(client, people, apply_payload)
    _check(client, people["oh"], app_id, "age_18_plus", date_of_birth=DOB)
    customer = _review(client, people, app_id)["customer"]
    assert customer["verification"] is None, "only the age check is done"
    assert customer["date_of_birth"] == DOB


def test_validity_is_capped_at_the_id_expiry_and_admin_tunable(client, people, apply_payload):
    app_id = _claimed(client, people, apply_payload)
    soon = (date.today() + timedelta(days=40)).isoformat()
    _verify_identity(client, people, app_id, expiry=soon)
    assert CustomerVerification.query.one().valid_until.isoformat() == soon

    r = client.put(
        "/api/admin/parameters", headers=people["ah"], json={"customer_verification_validity_months": 6}
    )
    assert r.status_code == 200, r.get_json()
    _check(client, people["oh"], app_id, "valid_id", id_document_id=_workflow.id_document_for(app_id))
    current = cv_service.current(people["customer"].id)
    assert current.valid_until == _months_from_today(6)


@pytest.mark.parametrize(
    "item,body,message",
    [
        ("age_18_plus", {}, "date_of_birth"),
        ("age_18_plus", {"date_of_birth": "01/05/1990"}, "YYYY-MM-DD"),
        ("age_18_plus", {"date_of_birth": (date.today() + timedelta(days=1)).isoformat()}, "future"),
        ("age_18_plus", {"date_of_birth": (date.today() - timedelta(days=17 * 365)).isoformat()}, "under 18"),
        ("valid_id", {}, "id_document_id"),
        ("valid_id", {"id_document_id": 99999}, "isn't one of this customer's ID documents"),
        ("valid_id", {"id_expiry_date": "2001-01-01"}, "expired"),
    ],
)
def test_identity_checks_need_valid_evidence(client, people, apply_payload, item, body, message):
    app_id = _claimed(client, people, apply_payload)
    if item == "valid_id" and "id_document_id" not in body and "id_expiry_date" in body:
        body = {**body, "id_document_id": _workflow.id_document_for(app_id)}
    r = _check(client, people["oh"], app_id, item, **body)
    assert r.status_code == 400 and message in r.get_json()["message"], r.get_json()
    assert CustomerVerification.query.count() == 0


def test_a_replaced_or_someone_elses_id_document_is_refused(client, people, apply_payload, make_user):
    app_id = _claimed(client, people, apply_payload)
    old_id = _workflow.id_document_for(app_id)
    newer = Document(
        user_id=people["customer"].id, document_type=DocumentType.ID_VERIFICATION,
        id_document_type=IdDocumentType.PASSPORT, storage_path="users/x/id_verification/new_passport-p.png",
    )
    db.session.add(newer)
    db.session.flush()
    db.session.get(Document, old_id).superseded_by_id = newer.id
    stranger = make_user("customer")
    theirs = Document(
        user_id=stranger.id, document_type=DocumentType.ID_VERIFICATION, storage_path="users/y/id_verification/t.png"
    )
    db.session.add(theirs)
    db.session.commit()
    assert "replaced" in _check(client, people["oh"], app_id, "valid_id", id_document_id=old_id).get_json()["message"]
    assert _check(client, people["oh"], app_id, "valid_id", id_document_id=theirs.id).status_code == 400
    assert _check(client, people["oh"], app_id, "valid_id", id_document_id=newer.id).status_code == 200


def test_the_verified_flag_comes_only_from_the_verification_record(client, people, apply_payload):
    """Every checklist item verified but no verification row -> not verified."""
    app_id = _claimed(client, people, apply_payload)
    _workflow.complete_checklist(client, app_id, people["oh"])
    for item in db.session.get(LoanApplication, app_id).verification_items:
        item.customer_verification_id = None  # (the items' FK would otherwise block the delete)
    db.session.flush()
    CustomerVerification.query.delete()
    db.session.commit()
    assert _review(client, people, app_id)["customer"]["verification"] is None


# ================================================================= carry over
def test_returning_verified_customer_is_verified_without_redoing_the_checks(client, people, apply_payload):
    first = _claimed(client, people, apply_payload)
    _verify_identity(client, people, first)
    row = CustomerVerification.query.one()
    _finish(client, people, first)

    second = _claimed(client, people, apply_payload)
    review = _review(client, people, second)
    items = {i["item_type"]: i for i in review["checklist"]["items"]}
    for key in ("age_18_plus", "valid_id"):
        assert items[key]["status"] == "verified"
        assert items[key]["customer_verification_id"] == row.id
        assert items[key]["note"].startswith(f"Carried over from customer verification #{row.id}")
    assert items["valid_id"]["evidence"]["id_document_id"] == row.id_document_id
    assert review["customer"]["verification"]["id"] == row.id
    assert CustomerVerification.query.count() == 1, "no second verification created"

    # The carried-over checks count toward an approval recommendation.
    for key in ("contact_details", "employment", "referee", "repayment_history", "application_consistency"):
        assert _check(client, people["oh"], second, key, note="ok").status_code == 200
    r = client.post(
        f"/api/loans/applications/{second}/recommend",
        headers=people["oh"],
        json={"recommendation": "recommend_approval", "comments": "Verified customer."},
    )
    assert r.status_code == 200, r.get_json()


def test_no_carry_over_from_an_expired_verification(client, people, apply_payload):
    first = _claimed(client, people, apply_payload)
    _verify_identity(client, people, first)
    _finish(client, people, first)
    CustomerVerification.query.one().valid_until = date.today() - timedelta(days=1)
    db.session.commit()

    second = _claimed(client, people, apply_payload)
    items = {i["item_type"]: i for i in _review(client, people, second)["checklist"]["items"]}
    assert items["age_18_plus"]["status"] == "pending" and items["valid_id"]["status"] == "pending"


# ================================================================ invalidation
def test_expired_verification_is_not_shown_and_the_sweep_invalidates_it(client, people, apply_payload):
    app_id = _claimed(client, people, apply_payload)
    _verify_identity(client, people, app_id)
    row = CustomerVerification.query.one()
    row.valid_until = date.today() - timedelta(days=1)
    db.session.commit()

    assert _review(client, people, app_id)["customer"]["verification"] is None
    assert [r.id for r in cv_service.expire_due()] == [row.id]
    db.session.commit()
    assert (row.status, row.invalidation_reason) == (CVS.INVALIDATED, Reason.EXPIRED)
    assert row.invalidated_by is None and row.invalidated_at is not None


def test_reverifying_after_a_lapse_marks_the_old_row_expired(client, people, apply_payload):
    """The one-verified-per-customer rule would otherwise block a new row."""
    app_id = _claimed(client, people, apply_payload)
    _verify_identity(client, people, app_id)
    old = CustomerVerification.query.one()
    old.valid_until = date.today() - timedelta(days=1)
    db.session.commit()

    _check(client, people["oh"], app_id, "valid_id", id_document_id=_workflow.id_document_for(app_id))
    db.session.refresh(old)
    assert old.invalidation_reason == Reason.EXPIRED
    assert CustomerVerification.query.filter_by(status=CVS.VERIFIED).count() == 1


def test_resaving_an_identity_check_unchanged_keeps_the_verification(client, people, apply_payload):
    app_id = _claimed(client, people, apply_payload)
    _verify_identity(client, people, app_id)
    _verify_identity(client, people, app_id)  # officer saves both again, same evidence
    assert CustomerVerification.query.count() == 1
    assert CustomerVerification.query.one().status == CVS.VERIFIED


def test_reverifying_supersedes_the_previous_verification(client, people, apply_payload):
    app_id = _claimed(client, people, apply_payload)
    _verify_identity(client, people, app_id)
    old = CustomerVerification.query.one()
    new_id = Document(
        user_id=people["customer"].id, document_type=DocumentType.ID_VERIFICATION,
        id_document_type=IdDocumentType.PASSPORT, storage_path="users/x/id_verification/n_passport-p.png",
    )
    db.session.add(new_id)
    db.session.commit()
    r = _check(client, people["oh"], app_id, "valid_id", id_document_id=new_id.id)
    assert r.status_code == 200, r.get_json()
    db.session.refresh(old)
    assert (old.status, old.invalidation_reason) == (CVS.INVALIDATED, Reason.SUPERSEDED)
    current = CustomerVerification.query.filter_by(status=CVS.VERIFIED).one()
    assert current.id != old.id


def test_changed_contact_details_invalidate_the_verification(client, people, apply_payload):
    first = _claimed(client, people, apply_payload)
    _verify_identity(client, people, first)
    row = CustomerVerification.query.one()
    _finish(client, people, first)

    second = _apply(client, people, apply_payload, confirmed_phone_number="+675 7999 8888")
    db.session.refresh(row)
    assert (row.status, row.invalidation_reason) == (CVS.INVALIDATED, Reason.INFORMATION_CHANGED)
    assert "phone number" in row.invalidation_note
    client.post(f"/api/loans/applications/{second}/officer-review", headers=people["oh"])
    items = {i["item_type"]: i for i in _review(client, people, second)["checklist"]["items"]}
    assert items["age_18_plus"]["status"] == "pending", "nothing stale carried over"


def test_same_details_with_different_spacing_or_case_keep_the_verification(client, people, apply_payload):
    first = _claimed(client, people, apply_payload)
    _verify_identity(client, people, first)
    _finish(client, people, first)
    payload = apply_payload()
    _apply(
        client, people, apply_payload,
        confirmed_full_name="  " + payload["confirmed_full_name"].upper(),
        confirmed_phone_number=payload["confirmed_phone_number"].replace(" ", ""),
    )
    assert CustomerVerification.query.one().status == CVS.VERIFIED


def test_staff_can_request_reverification(client, people, apply_payload):
    app_id = _claimed(client, people, apply_payload)
    _verify_identity(client, people, app_id)
    url = f"/api/officer/applications/{app_id}/customer-verification/invalidate"

    assert client.post(url, headers=people["oh"], json={}).status_code == 400
    assert client.post(url, headers=people["xh"], json={"note": "x"}).status_code == 403, "not their claim"
    assert client.post(url, headers=people["ch"], json={"note": "x"}).status_code == 403
    r = client.post(url, headers=people["oh"], json={"note": "Customer has a new ID card."})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["verification"] is None

    row = CustomerVerification.query.one()
    assert (row.status, row.invalidation_reason, row.invalidated_by) == (
        CVS.INVALIDATED, Reason.STAFF_REQUESTED, people["officer"].id,
    )
    items = {i["item_type"]: i for i in _review(client, people, app_id)["checklist"]["items"]}
    assert items["age_18_plus"]["status"] == "pending" and items["valid_id"]["status"] == "pending"
    assert client.post(url, headers=people["oh"], json={"note": "again"}).status_code == 409


def test_undoing_an_identity_check_invalidates_the_verification_it_made(client, people, apply_payload):
    app_id = _claimed(client, people, apply_payload)
    _verify_identity(client, people, app_id)
    r = _check(client, people["oh"], app_id, "valid_id", status="failed", note="Photo doesn't match.")
    assert r.status_code == 200
    row = CustomerVerification.query.one()
    assert (row.status, row.invalidation_reason) == (CVS.INVALIDATED, Reason.STAFF_REQUESTED)
    assert "ID check changed to failed" in row.invalidation_note


def test_policy_update_invalidates_older_verifications(client, people, apply_payload, app):
    app_id = _claimed(client, people, apply_payload)
    _verify_identity(client, people, app_id)
    url = "/api/admin/customer-verifications/invalidate-outdated"

    r = client.post(url, headers=people["ah"])
    assert r.get_json() == {"invalidated": 0, "policy_version": "2026-10-v1"}, "current policy - nothing to do"
    assert client.post(url, headers=people["oh"]).status_code == 403

    app.config["CUSTOMER_VERIFICATION_POLICY_VERSION"] = "2027-01-v2"
    try:
        r = client.post(url, headers=people["ah"])
        assert r.get_json()["invalidated"] == 1
    finally:
        app.config["CUSTOMER_VERIFICATION_POLICY_VERSION"] = "2026-10-v1"
    row = CustomerVerification.query.one()
    assert (row.status, row.invalidation_reason) == (CVS.INVALIDATED, Reason.POLICY_UPDATED)


def test_dob_stays_on_the_customer_after_the_verification_is_invalidated(client, people, apply_payload):
    app_id = _claimed(client, people, apply_payload)
    _verify_identity(client, people, app_id)
    client.post(
        f"/api/officer/applications/{app_id}/customer-verification/invalidate",
        headers=people["oh"],
        json={"note": "New ID."},
    )
    customer = _review(client, people, app_id)["customer"]
    assert customer["verification"] is None
    assert customer["date_of_birth"] == DOB
    assert db.session.get(User, people["customer"].id).date_of_birth == date(1990, 5, 1)


def test_dob_is_not_copied_into_the_audit_log(client, people, apply_payload):
    app_id = _claimed(client, people, apply_payload)
    _verify_identity(client, people, app_id)
    entry = AuditLog.query.filter_by(action="verification_item_updated", entity_id=str(app_id)).first()
    assert entry.details["date_of_birth_recorded"] is True
    assert DOB not in str([a.details for a in AuditLog.query.all()])


def test_application_status_is_untouched_by_verification(client, people, apply_payload):
    app_id = _claimed(client, people, apply_payload)
    _verify_identity(client, people, app_id)
    assert db.session.get(LoanApplication, app_id).status.value == "officer_review"
