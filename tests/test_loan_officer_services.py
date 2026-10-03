"""Service-level tests for the Phase 2-3 models: Request More Information
history, the verification checklist, and the recommendation record.

Complements test_loan_officer_schema.py (database constraints) and
test_loan_officer_api.py (HTTP behaviour) by exercising the service rules
directly.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.extensions import db
from app.models import (
    CustomerVerification,
    Document,
    InformationRequest,
    InformationResponse,
    LoanApplication,
    OfficerRecommendation,
    VerificationItem,
)
from app.models.enums import (
    CustomerVerificationStatus,
    DocumentType,
    InformationRequestStatus,
    LoanApplicationStatus,
    VerificationItemStatus,
)
from app.services import loan_processing, verification
from app.services.errors import ServiceError

import _workflow

S = LoanApplicationStatus


@pytest.fixture
def people(make_user):
    return make_user("customer"), make_user("loan_officer"), make_user("admin")


def _claimed(customer, officer, amount="500") -> LoanApplication:
    application = LoanApplication(
        user_id=customer.id, amount_requested=Decimal(amount), status=S.SUBMITTED
    )
    db.session.add(application)
    db.session.commit()
    return loan_processing.start_officer_review(application, officer)


def _complete(application, officer):
    for t in verification.CHECKLIST:
        verification.update_item(
            application, officer, t.key, status="verified", note="ok",
            evidence=_workflow.evidence_for(application.id, t.key),
        )


def _items(application):
    return {i["item_type"]: i for i in verification.serialize_checklist(application)["items"]}


# ======================================================== verification checklist
def test_claim_opens_one_pending_row_per_registered_item(people):
    customer, officer, _ = people
    application = _claimed(customer, officer)
    assert sorted(i.item_type for i in application.verification_items) == sorted(
        t.key for t in verification.CHECKLIST
    )
    assert {i.status for i in application.verification_items} == {VerificationItemStatus.PENDING}
    verification.ensure_checklist(application)  # idempotent
    db.session.commit()
    assert VerificationItem.query.filter_by(loan_application_id=application.id).count() == 8


@pytest.mark.parametrize("amount,required", [("999", False), ("1000", True)])
def test_proof_of_income_is_required_only_at_the_threshold(people, amount, required):
    customer, officer, _ = people
    application = _claimed(customer, officer, amount)
    assert _items(application)["proof_of_income"]["required"] is required
    blocking = verification.blocking_items(application)
    assert ("proof_of_income" in blocking) is required


def test_not_applicable_completes_a_required_item_but_failed_anything_blocks(people):
    customer, officer, _ = people
    application = _claimed(customer, officer)
    _complete(application, officer)
    verification.update_item(application, officer, "repayment_history", status="not_applicable", note="First loan.")
    assert verification.blocking_items(application) == []
    assert verification.serialize_checklist(application)["summary"]["ready_for_approval_recommendation"]

    # proof_of_income is NOT required for K500, but a failed check still blocks approval.
    verification.update_item(application, officer, "proof_of_income", status="failed", note="Forged.")
    assert verification.blocking_items(application) == ["proof_of_income"]


def test_checklist_update_rules(people):
    customer, officer, _ = people
    application = _claimed(customer, officer)
    for status, note in (("failed", None), ("not_applicable", " "), ("bogus", "x")):
        with pytest.raises(ServiceError):
            verification.update_item(application, officer, "referee", status=status, note=note)
    with pytest.raises(ServiceError):
        verification.update_item(application, officer, "not_a_check", status="verified")
    with pytest.raises(ServiceError):
        verification.update_item(application, officer, "referee", status="verified", note="x" * 1001)

    item = verification.update_item(application, officer, "referee", status="verified")
    assert (item.checked_by, item.checked_at is not None) == (officer.id, True)
    item = verification.update_item(application, officer, "referee", status="pending")
    assert (item.checked_by, item.checked_at) == (None, None)


def test_an_item_type_dropped_from_the_registry_still_shows_but_never_blocks(people):
    customer, officer, _ = people
    application = _claimed(customer, officer)
    application.verification_items.append(
        VerificationItem(item_type="legacy_check", status=VerificationItemStatus.PENDING)
    )
    db.session.commit()
    legacy = _items(application)["legacy_check"]
    assert (legacy["label"], legacy["required"]) == ("legacy_check", False)
    assert "legacy_check" not in verification.blocking_items(application)
    order = [i["item_type"] for i in verification.serialize_checklist(application)["items"]]
    assert order == [t.key for t in verification.CHECKLIST] + ["legacy_check"]


def test_checklist_is_editable_only_while_with_the_officer(people):
    customer, officer, _ = people
    application = _claimed(customer, officer)
    loan_processing.submit_recommendation(
        application, officer, recommendation="recommend_rejection", comments="No."
    )
    with pytest.raises(ServiceError) as exc:
        loan_processing.update_checklist_item(application, officer, "referee", status="verified")
    assert exc.value.status_code == 409


# =================================================== request / response history
def _ask(application, officer, *reasons):
    return loan_processing.request_customer_action(
        application, officer, [{"request_type": "other", "reason": r} for r in reasons]
    )


def test_each_round_adds_rows_and_responses_link_to_their_own_request(people):
    customer, officer, _ = people
    application = _claimed(customer, officer)
    application.monthly_income = Decimal("800")
    db.session.commit()

    first = _ask(application, officer, "Payslip?", "Employer?")
    loan_processing.respond_to_customer_action(
        application, customer,
        responses=[
            {"information_request_id": first[0].id, "response_note": "Sent."},
            {"information_request_id": first[1].id, "response_note": "ACME."},
        ],
        monthly_income=650,
        referees=[{"full_name": "New Ref", "relationship": "friend", "mobile_number": "1"}],
    )
    second = _ask(application, officer, "Referee unreachable.")
    loan_processing.respond_to_customer_action(
        application, customer,
        responses=[{"information_request_id": second[0].id, "response_note": "Call again."}],
    )

    rows = InformationRequest.query.filter_by(loan_application_id=application.id).order_by("id").all()
    assert [r.status for r in rows] == [InformationRequestStatus.RESPONDED] * 3
    responses = {r.information_request_id: r for r in InformationResponse.query.all()}
    assert responses[first[1].id].response_note == "ACME."
    changes = responses[first[0].id].field_changes
    assert changes["monthly_income"] == {"old": 800.0, "new": 650.0}
    assert changes["referees"]["old"] == []  # created directly, with no referees
    assert changes["referees"]["new"][0]["full_name"] == "New Ref"
    assert responses[second[0].id].field_changes is None, "nothing changed in round two"
    assert application.status == S.OFFICER_REVIEW


def test_resuming_cancels_open_requests_but_keeps_them(people):
    customer, officer, _ = people
    application = _claimed(customer, officer)
    (request,) = _ask(application, officer, "Payslip?")

    with pytest.raises(ServiceError):
        loan_processing.resume_officer_review(application, officer)  # reason required
    loan_processing.resume_officer_review(application, officer, "Confirmed by phone.")

    db.session.refresh(request)
    assert request.status == InformationRequestStatus.CANCELLED
    assert (request.cancelled_by, request.cancel_reason) == (officer.id, "Confirmed by phone.")
    assert request.cancelled_at is not None and request.response is None
    # The customer can no longer answer it.
    with pytest.raises(ServiceError) as exc:
        loan_processing.respond_to_customer_action(
            application, customer,
            responses=[{"information_request_id": request.id, "response_note": "late"}],
        )
    assert exc.value.status_code == 409
    assert loan_processing.action_required_text(application) is None


def test_customer_view_of_a_request_hides_staff_fields(people):
    customer, officer, _ = people
    application = _claimed(customer, officer)
    loan_processing.request_customer_action(
        application, officer,
        [{"request_type": "other", "reason": "Why?", "internal_note": "Suspicious."}],
    )
    request = application.information_requests[0]
    customer_view = loan_processing.serialize_information_request(request, staff=False)
    staff_view = loan_processing.serialize_information_request(request, staff=True)
    hidden = {"internal_note", "requested_by", "requested_by_name", "cancelled_by", "cancel_reason"}
    assert not hidden & set(customer_view)
    assert staff_view["internal_note"] == "Suspicious."


# ====================================================== recommendation record
def _verification(customer, officer, *, valid_until):
    doc = Document(user_id=customer.id, document_type=DocumentType.ID_VERIFICATION, storage_path="id.pdf")
    db.session.add(doc)
    db.session.flush()
    row = CustomerVerification(
        user_id=customer.id, verified_by=officer.id, id_document_id=doc.id,
        date_of_birth=date(1990, 1, 1), verified_full_name="C", verified_email="c@x",
        valid_until=valid_until, policy_version="v1", status=CustomerVerificationStatus.VERIFIED,
    )
    db.session.add(row)
    db.session.commit()
    return row


@pytest.mark.parametrize("days,attached", [(30, True), (-1, False)])
def test_recommendation_references_the_customer_verification_only_while_valid(people, days, attached):
    customer, officer, _ = people
    cv = _verification(customer, officer, valid_until=date.today() + timedelta(days=days))
    application = _claimed(customer, officer)
    rec = loan_processing.submit_recommendation(
        application, officer, recommendation="recommend_rejection", comments="x"
    )
    assert rec.customer_verification_id == (cv.id if attached else None)


def test_recommendation_snapshot_is_frozen_against_later_checklist_changes(people):
    customer, officer, admin = people
    application = _claimed(customer, officer)
    _complete(application, officer)
    rec = loan_processing.submit_recommendation(
        application, officer, recommendation="recommend_approval", comments="All good."
    )
    loan_processing.return_to_officer(application, admin, "Re-check employment.")
    loan_processing.resume_officer_review(application, officer)
    verification.update_item(application, officer, "employment", status="failed", note="Not employed.")

    db.session.refresh(rec)
    frozen = {i["item_type"]: i["status"] for i in rec.checklist_snapshot}
    assert frozen["employment"] == "verified", "the recommendation keeps what the officer signed off on"
    assert _items(application)["employment"]["status"] == "failed"


def test_recommendation_survives_every_admin_outcome(people):
    customer, officer, admin = people
    application = _claimed(customer, officer)
    rec = loan_processing.submit_recommendation(
        application, officer, recommendation="recommend_rejection", comments="Weak referee."
    )
    loan_processing.start_admin_review(application, admin)
    loan_processing.reject_application(application, admin, "Agreed.")
    db.session.expire_all()
    stored = db.session.get(OfficerRecommendation, rec.id)
    assert (stored.recommendation.value, stored.comments, stored.officer_id) == (
        "recommend_rejection", "Weak referee.", officer.id,
    )


def test_recommendations_have_no_update_or_delete_route(app):
    for rule in app.url_map.iter_rules():
        if "recommend" in rule.rule:
            assert not (rule.methods & {"PUT", "PATCH", "DELETE"}), rule.rule


def test_recommend_validation(people):
    customer, officer, _ = people
    application = _claimed(customer, officer)
    for kind, comments in (("approve", "x"), ("recommend_rejection", " "), ("recommend_rejection", "x" * 2001)):
        with pytest.raises(ServiceError):
            loan_processing.submit_recommendation(
                application, officer, recommendation=kind, comments=comments
            )
    assert application.status == S.OFFICER_REVIEW and not application.officer_recommendations


def test_payment_report_without_a_schedule_id_is_a_validation_error(people):
    from app.services import payment_processing

    customer, _, _ = people
    with pytest.raises(ServiceError) as exc:
        payment_processing.record_payment(
            customer, repayment_schedule_id=None, amount=1, payment_method="cash"
        )
    assert exc.value.status_code == 400
