"""Schema-level guarantees for the Loan Officer workflow tables: no orphaned
history rows, every history row names who acted, and the check/unique
constraints hold. Exercised directly against the models (SQLite with
foreign keys ON, see conftest) - no service layer involved yet.
"""

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import (
    CustomerVerification,
    Document,
    InformationRequest,
    InformationResponse,
    LoanApplication,
    OfficerRecommendation,
    User,
    VerificationItem,
)
from app.models.enums import (
    CustomerVerificationInvalidationReason,
    CustomerVerificationStatus,
    DocumentType,
    InformationRequestStatus,
    InformationRequestType,
    LoanApplicationStatus,
    OfficerRecommendationType,
    VerificationItemStatus,
)

NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)


@pytest.fixture
def people(make_user):
    return make_user("customer"), make_user("loan_officer"), make_user("admin")


def _application(customer) -> LoanApplication:
    application = LoanApplication(
        user_id=customer.id,
        amount_requested=Decimal("500"),
        status=LoanApplicationStatus.OFFICER_REVIEW,
    )
    db.session.add(application)
    db.session.commit()
    return application


def _id_document(customer) -> Document:
    doc = Document(
        user_id=customer.id,
        document_type=DocumentType.ID_VERIFICATION,
        storage_path=f"id_verification/user_{customer.id}/id.pdf",
    )
    db.session.add(doc)
    db.session.commit()
    return doc


def _verification(customer, officer, doc, **kw) -> CustomerVerification:
    return CustomerVerification(
        user_id=customer.id,
        verified_by=officer.id,
        id_document_id=doc.id,
        date_of_birth=date(1990, 5, 1),
        verified_full_name=customer.full_name,
        verified_email=customer.email,
        valid_until=date(2027, 10, 1),
        policy_version="2026-10-v1",
        **kw,
    )


def _assert_rejected(row):
    db.session.add(row)
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


# ------------------------------------------------------------- no orphans
def test_verification_item_requires_an_existing_application(people):
    _assert_rejected(VerificationItem(item_type="valid_id"))
    _assert_rejected(VerificationItem(loan_application_id=999_999, item_type="valid_id"))


def test_recommendation_requires_an_application_and_an_officer(people):
    customer, officer, _ = people
    application = _application(customer)
    common = dict(
        recommendation=OfficerRecommendationType.RECOMMEND_APPROVAL,
        comments="All checks passed.",
        checklist_snapshot=[],
    )
    _assert_rejected(OfficerRecommendation(loan_application_id=application.id, **common))
    _assert_rejected(OfficerRecommendation(officer_id=officer.id, **common))
    _assert_rejected(
        OfficerRecommendation(loan_application_id=application.id, officer_id=999_999, **common)
    )


def test_information_request_and_response_require_their_parents_and_actors(people):
    customer, officer, _ = people
    application = _application(customer)
    _assert_rejected(
        InformationRequest(
            loan_application_id=application.id,
            request_type=InformationRequestType.OTHER,
            reason="Please confirm your employer.",
        )
    )  # no requested_by
    _assert_rejected(InformationResponse(responded_by=customer.id, response_note="Done."))


def test_customer_verification_requires_user_verifier_and_id_document(people):
    customer, officer, _ = people
    doc = _id_document(customer)
    row = _verification(customer, officer, doc)
    row.verified_by = None
    _assert_rejected(row)
    row = _verification(customer, officer, doc)
    row.id_document_id = None
    _assert_rejected(row)


def test_deleting_an_application_removes_all_its_history_rows(people):
    customer, officer, _ = people
    application = _application(customer)
    request = InformationRequest(
        loan_application_id=application.id,
        request_type=InformationRequestType.MISSING_DOCUMENT,
        reason="Upload a payslip.",
        requested_by=officer.id,
    )
    db.session.add(request)
    db.session.flush()
    db.session.add_all([
        InformationResponse(
            information_request_id=request.id, responded_by=customer.id, response_note="Uploaded."
        ),
        VerificationItem(loan_application_id=application.id, item_type="valid_id"),
        OfficerRecommendation(
            loan_application_id=application.id,
            officer_id=officer.id,
            recommendation=OfficerRecommendationType.RECOMMEND_REJECTION,
            comments="Income not verifiable.",
            checklist_snapshot=[],
        ),
    ])
    db.session.commit()

    db.session.delete(application)
    db.session.commit()

    for model in (InformationRequest, InformationResponse, VerificationItem, OfficerRecommendation):
        assert db.session.query(model).count() == 0, model.__name__


def test_removing_a_history_row_from_its_list_does_not_silently_delete_it(people):
    customer, officer, _ = people
    application = _application(customer)
    db.session.add(VerificationItem(loan_application_id=application.id, item_type="valid_id"))
    db.session.commit()

    application.verification_items.clear()
    with pytest.raises(IntegrityError):
        db.session.commit()  # would orphan the row - refused, not deleted
    db.session.rollback()
    assert VerificationItem.query.count() == 1


def test_staff_user_still_named_in_history_cannot_be_deleted(people):
    customer, officer, _ = people
    application = _application(customer)
    db.session.add(
        OfficerRecommendation(
            loan_application_id=application.id,
            officer_id=officer.id,
            recommendation=OfficerRecommendationType.RECOMMEND_APPROVAL,
            comments="OK.",
            checklist_snapshot=[],
        )
    )
    db.session.commit()

    db.session.delete(officer)
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()
    assert OfficerRecommendation.query.one().officer_id == officer.id


def test_deleting_the_customer_cascades_their_applications_history_and_verifications(people):
    customer, officer, _ = people
    application = _application(customer)
    doc = _id_document(customer)
    verification = _verification(customer, officer, doc, source_application_id=application.id)
    request = InformationRequest(
        loan_application_id=application.id,
        request_type=InformationRequestType.OTHER,
        reason="Confirm phone.",
        requested_by=officer.id,
    )
    db.session.add_all([verification, request])
    db.session.flush()
    db.session.add(
        InformationResponse(
            information_request_id=request.id, responded_by=customer.id, response_note="Confirmed."
        )
    )
    db.session.commit()

    db.session.delete(customer)
    db.session.commit()

    for model in (LoanApplication, InformationRequest, InformationResponse, CustomerVerification):
        assert db.session.query(model).count() == 0, model.__name__
    assert db.session.get(User, officer.id) is not None


def test_assigned_officer_removal_returns_application_to_the_shared_queue(people):
    customer, officer, _ = people
    application = _application(customer)
    application.assigned_officer_id = officer.id
    application.assigned_at = NOW
    db.session.commit()

    db.session.execute(db.text("DELETE FROM users WHERE id = :id"), {"id": officer.id})
    db.session.commit()
    db.session.refresh(application)
    assert application.assigned_officer_id is None


# ------------------------------------------------------- check / unique rules
def test_verification_item_checked_fields_must_match_status(people):
    customer, officer, _ = people
    application = _application(customer)
    _assert_rejected(  # pending but claims a checker
        VerificationItem(
            loan_application_id=application.id,
            item_type="valid_id",
            checked_by=officer.id,
            checked_at=NOW,
        )
    )
    _assert_rejected(  # verified but nobody checked it
        VerificationItem(
            loan_application_id=application.id,
            item_type="valid_id",
            status=VerificationItemStatus.VERIFIED,
        )
    )


def test_one_verification_item_per_type_per_application(people):
    customer, _, _ = people
    application = _application(customer)
    db.session.add(VerificationItem(loan_application_id=application.id, item_type="referee"))
    db.session.commit()
    _assert_rejected(VerificationItem(loan_application_id=application.id, item_type="referee"))


def test_new_item_types_need_no_schema_change(people):
    customer, _, _ = people
    application = _application(customer)
    db.session.add(VerificationItem(loan_application_id=application.id, item_type="some_future_check"))
    db.session.commit()


def test_cancelled_request_must_record_who_cancelled(people):
    customer, officer, _ = people
    application = _application(customer)
    _assert_rejected(
        InformationRequest(
            loan_application_id=application.id,
            request_type=InformationRequestType.OTHER,
            reason="x",
            requested_by=officer.id,
            status=InformationRequestStatus.CANCELLED,
        )
    )


def test_a_request_has_at_most_one_response(people):
    customer, officer, _ = people
    application = _application(customer)
    request = InformationRequest(
        loan_application_id=application.id,
        request_type=InformationRequestType.OTHER,
        reason="x",
        requested_by=officer.id,
    )
    db.session.add(request)
    db.session.flush()
    db.session.add(
        InformationResponse(information_request_id=request.id, responded_by=customer.id, response_note="a")
    )
    db.session.commit()
    _assert_rejected(
        InformationResponse(information_request_id=request.id, responded_by=customer.id, response_note="b")
    )


def test_only_one_verified_customer_verification_at_a_time(people):
    customer, officer, _ = people
    doc = _id_document(customer)
    db.session.add(_verification(customer, officer, doc))
    db.session.commit()
    _assert_rejected(_verification(customer, officer, doc))

    officer_id = officer.id
    first = CustomerVerification.query.one()
    with db.session.no_autoflush:
        first.status = CustomerVerificationStatus.INVALIDATED
        first.invalidated_at = NOW
        first.invalidated_by = officer_id
        first.invalidation_reason = CustomerVerificationInvalidationReason.STAFF_REQUESTED
    db.session.commit()
    db.session.add(_verification(customer, officer, doc))
    db.session.commit()  # re-verification is a new row; the old one is kept
    assert CustomerVerification.query.count() == 2


def test_invalidated_verification_must_say_when_and_why(people):
    customer, officer, _ = people
    doc = _id_document(customer)
    _assert_rejected(
        _verification(customer, officer, doc, status=CustomerVerificationStatus.INVALIDATED)
    )
