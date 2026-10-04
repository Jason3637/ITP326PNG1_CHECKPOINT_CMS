"""Loan Officer workspace API: dashboard queues, the Application Review
screen, the verification checklist, Request More Information with linked
responses, recommendations, customer history and the advisory credit
assessment.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.extensions import db
from app.models import (
    AuditLog,
    Disbursement,
    InformationResponse,
    Loan,
    LoanApplication,
    OfficerRecommendation,
    PaymentTransaction,
    RepaymentSchedule,
)
from app.services import credit_evaluation, prime_pricing, verification

import _workflow


@pytest.fixture
def staff(make_user, auth_header):
    officer, other_officer, admin = (
        make_user("loan_officer", full_name="Olive Officer"),
        make_user("loan_officer", full_name="Oscar Other"),
        make_user("admin", full_name="Ada Admin"),
    )
    return {
        "officer": officer,
        "other": other_officer,
        "admin": admin,
        "oh": auth_header(officer),
        "xh": auth_header(other_officer),
        "ah": auth_header(admin),
    }


@pytest.fixture
def new_application(client, make_user, auth_header, apply_payload):
    """Each call: a fresh customer with one SUBMITTED application -> (app_id, customer, headers)."""

    def _make(**overrides):
        customer = make_user("customer", full_name="Grace Waigani")
        ch = auth_header(customer)
        r = client.post("/api/loans/apply", headers=ch, json=apply_payload(**overrides))
        assert r.status_code == 201, r.get_json()
        return r.get_json()["id"], customer, ch

    return _make


def _claim(client, app_id, headers):
    r = client.post(f"/api/loans/applications/{app_id}/officer-review", headers=headers)
    assert r.status_code == 200, r.get_json()
    return r.get_json()


# ===================================================================== queues
def test_queue_counts_and_filters_are_real_queries(client, staff, new_application, workflow):
    oh, xh, ah = staff["oh"], staff["xh"], staff["ah"]
    unclaimed, _, _ = new_application()
    mine, _, _ = new_application()
    theirs, _, _ = new_application()
    waiting, _, _ = new_application()
    sent, _, _ = new_application()
    returned, _, _ = new_application()

    _claim(client, mine, oh)
    _claim(client, theirs, xh)
    _claim(client, waiting, oh)
    assert workflow.request_info(waiting, oh).status_code == 200
    _claim(client, sent, oh)
    assert workflow.recommend(sent, oh).status_code == 200
    _claim(client, returned, oh)
    assert workflow.recommend(returned, oh).status_code == 200
    r = client.post(
        f"/api/loans/applications/{returned}/return-to-officer",
        headers=ah,
        json={"reason": "Re-check the referee."},
    )
    assert r.status_code == 200, r.get_json()

    r = client.get("/api/officer/queues", headers=oh)
    assert r.status_code == 200, r.get_json()
    q = r.get_json()["queues"]
    assert q["awaiting_review"] == {"total": 1, "mine": 0, "unassigned": 1}
    assert q["under_review"] == {"total": 2, "mine": 1, "unassigned": 0}
    assert q["customer_action_required"] == {"total": 1, "mine": 1, "unassigned": 0}
    assert q["sent_to_admin"] == {"total": 1, "mine": 1, "unassigned": 0}
    assert q["returned_by_admin"] == {"total": 1, "mine": 1, "unassigned": 0}
    assert r.get_json()["definitions"]["sent_to_admin"] == [
        "recommended_for_approval", "recommended_for_rejection", "admin_review",
    ]

    def ids(path):
        r = client.get(path, headers=oh)
        assert r.status_code == 200, r.get_json()
        return [i["id"] for i in r.get_json()["items"]]

    assert ids("/api/officer/queues/awaiting_review") == [unclaimed]
    assert ids("/api/officer/queues/under_review") == [mine, theirs]
    assert ids("/api/officer/queues/under_review?assigned=me") == [mine]
    assert ids(f"/api/officer/queues/under_review?officer_id={staff['other'].id}") == [theirs]
    assert ids("/api/officer/queues/awaiting_review?assigned=unassigned") == [unclaimed]
    assert ids("/api/officer/queues/sent_to_admin") == [sent]

    r = client.get("/api/officer/queues/returned_by_admin", headers=oh)
    item = r.get_json()["items"][0]
    assert item["id"] == returned
    assert item["returned_reason"] == "Re-check the referee."
    assert item["is_mine"] is True and item["latest_recommendation"] == "recommend_approval"

    page = client.get("/api/officer/queues/under_review?per_page=1&page=2", headers=oh).get_json()
    assert (page["total"], page["pages"], [i["id"] for i in page["items"]]) == (2, 2, [theirs])

    assert client.get("/api/officer/queues/everything", headers=oh).status_code == 400
    assert client.get("/api/officer/queues/under_review?assigned=bob", headers=oh).status_code == 400
    # admins see the same views
    assert client.get("/api/officer/queues", headers=ah).status_code == 200


# =============================================================== review screen
def test_review_screen_returns_everything_in_one_call(client, staff, new_application):
    oh = staff["oh"]
    app_id, customer, ch = new_application(amount_requested=500, monthly_income=900)
    _claim(client, app_id, oh)

    r = client.get(f"/api/officer/applications/{app_id}", headers=oh)
    assert r.status_code == 200, r.get_json()
    body = r.get_json()

    expected = prime_pricing.calculate_prime(500)
    pricing = body["application"]["pricing"]
    assert body["application"]["prime_category"] == expected["category"] == "PRIME 2"
    assert pricing["interest_amount"] == float(expected["interest_amount"]) == 200
    assert pricing["total_repayable"] == float(expected["total_repayable"]) == 700

    assert body["customer"]["id"] == customer.id
    assert body["customer"]["email"] == customer.email
    assert body["customer"]["verification"] is None
    assert len(body["application"]["referees"]) == 1
    assert [i["item_type"] for i in body["checklist"]["items"]] == [t.key for t in verification.CHECKLIST]
    assert body["assignment"]["officer_id"] == staff["officer"].id
    assert body["assignment"]["is_mine"] is True
    assert set(body["allowed_actions"]) == {
        "update_checklist", "request_information", "recommend_approval", "recommend_rejection",
    }
    assert body["customer_history_url"] == f"/api/officer/applications/{app_id}/customer-history"

    # Another officer can look, but has no actions on someone else's claim.
    other = client.get(f"/api/officer/applications/{app_id}", headers=staff["xh"]).get_json()
    assert other["assignment"]["is_mine"] is False
    assert other["allowed_actions"] == []


def test_credit_assessment_is_labelled_advisory_and_staff_only(client, staff, new_application):
    app_id, _, ch = new_application()
    body = client.get(f"/api/officer/applications/{app_id}", headers=staff["oh"]).get_json()
    assessment = body["credit_assessment"]
    assert assessment["label"] == "Advisory - not a decision input"
    assert assessment["advisory"] is True and assessment["affects_status"] is False
    assert assessment["result"]["algorithm"] == "interim-v2"
    assert assessment["result"]["disclaimer"] == credit_evaluation.DISCLAIMER
    assert body["application"]["credit_assessment"] == assessment

    mine = client.get("/api/loans/applications/mine", headers=ch).get_json()["applications"][0]
    assert "credit_assessment" not in mine and "credit_evaluation_result" not in mine


def test_credit_disclaimer_is_staff_wording_even_on_older_stored_results(client, staff, new_application):
    app_id, _, _ = new_application()
    application = db.session.get(LoanApplication, app_id)
    old = "Interim underwriting model - ... the thresholds are still engineering guesses."
    application.credit_evaluation_result = {**application.credit_evaluation_result, "disclaimer": old}
    db.session.commit()

    body = client.get(f"/api/officer/applications/{app_id}", headers=staff["oh"]).get_json()
    shown = body["credit_assessment"]["result"]["disclaimer"]
    assert shown.startswith("Advisory assessment only.") and "engineering" not in shown
    db.session.expire_all()
    assert db.session.get(LoanApplication, app_id).credit_evaluation_result["disclaimer"] == old, (
        "the stored result is left as it was"
    )


def test_documents_include_unlinked_id_documents(client, staff, new_application):
    from app.models import Document
    from app.models.enums import DocumentType

    app_id, customer, _ = new_application()
    id_doc = Document(user_id=customer.id, document_type=DocumentType.ID_VERIFICATION, storage_path="a.pdf")
    linked = Document(
        user_id=customer.id,
        loan_application_id=app_id,
        document_type=DocumentType.LOAN_FILE,
        storage_path="b.pdf",
    )
    db.session.add_all([id_doc, linked])
    db.session.commit()

    docs = client.get(f"/api/officer/applications/{app_id}", headers=staff["oh"]).get_json()["documents"]
    by_id = {d["id"]: d for d in docs}
    assert by_id[id_doc.id]["linked_to_this_application"] is False
    assert by_id[linked.id]["linked_to_this_application"] is True


# =================================================================== checklist
def test_checklist_items_update_individually_with_who_and_when(client, staff, new_application):
    oh = staff["oh"]
    app_id, _, _ = new_application()

    r = client.get(f"/api/officer/applications/{app_id}/checklist", headers=oh)
    assert r.get_json()["started"] is False, "checklist opens when the application is claimed"
    _claim(client, app_id, oh)

    r = client.patch(
        f"/api/officer/applications/{app_id}/checklist/valid_id",
        headers=oh,
        json={"status": "verified", "note": "NID card checked.", "id_document_id": _workflow.id_document_for(app_id)},
    )
    assert r.status_code == 200, r.get_json()
    items = {i["item_type"]: i for i in r.get_json()["items"]}
    assert items["valid_id"]["status"] == "verified"
    assert items["valid_id"]["checked_by"] == staff["officer"].id
    assert items["valid_id"]["checked_by_name"] == "Olive Officer"
    assert items["valid_id"]["checked_at"] is not None
    assert items["employment"]["status"] == "pending" and items["employment"]["checked_by"] is None
    assert items["proof_of_income"]["required"] is False, "K500 is below the proof-of-income threshold"

    entry = AuditLog.query.filter_by(action="verification_item_updated", entity_id=str(app_id)).one()
    assert entry.actor_id == staff["officer"].id
    assert entry.details["from"]["status"] == "pending"
    assert entry.details["to"] == {"status": "verified", "note": "NID card checked."}

    # failed / not_applicable need a note
    url = f"/api/officer/applications/{app_id}/checklist/referee"
    assert client.patch(url, headers=oh, json={"status": "failed"}).status_code == 400
    assert client.patch(url, headers=oh, json={"status": "failed", "note": "No answer."}).status_code == 200
    # reset to pending clears who/when
    r = client.patch(url, headers=oh, json={"status": "pending"})
    referee = next(i for i in r.get_json()["items"] if i["item_type"] == "referee")
    assert referee["checked_by"] is None and referee["checked_at"] is None

    assert client.patch(
        f"/api/officer/applications/{app_id}/checklist/bogus", headers=oh, json={"status": "verified"}
    ).status_code == 400
    # only the claiming officer (or an admin) may tick items
    assert client.patch(url, headers=staff["xh"], json={"status": "verified"}).status_code == 403
    assert client.patch(url, headers=staff["ah"], json={"status": "verified"}).status_code == 200


# ==================================================== request more information
def test_request_more_information_and_linked_responses(client, staff, new_application):
    oh = staff["oh"]
    app_id, customer, ch = new_application(monthly_income=800)
    _claim(client, app_id, oh)

    r = client.post(
        f"/api/loans/applications/{app_id}/request-action",
        headers=oh,
        json={
            "requests": [
                {
                    "request_type": "missing_document",
                    "reason": "Please upload a recent payslip.",
                    "required_document_type": "proof_of_income",
                    "internal_note": "Income looks high for the stated job.",
                },
                {
                    "request_type": "information_mismatch",
                    "reason": "Please confirm your monthly income.",
                    "required_information": "Gross monthly income",
                },
            ]
        },
    )
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["status"] == "customer_action_required"
    first, second = body["information_requests"]
    assert first["internal_note"] == "Income looks high for the stated job."
    assert first["requested_by"] == staff["officer"].id
    assert body["action_required_note"] == (
        "Please upload a recent payslip.\nPlease confirm your monthly income."
    )

    # Customer view: no internal note, no staff identities.
    mine = client.get("/api/loans/applications/mine", headers=ch).get_json()["applications"][0]
    assert mine["status_label"] == "Action Required"
    for req in mine["information_requests"]:
        assert "internal_note" not in req and "requested_by" not in req

    url = f"/api/loans/applications/{app_id}/respond"
    # every open request must be answered...
    r = client.post(
        url, headers=ch,
        json={"responses": [{"information_request_id": first["id"], "response_note": "Uploaded."}]},
    )
    assert r.status_code == 400 and str(second["id"]) in r.get_json()["message"]
    # ...and only open requests on THIS application
    r = client.post(
        url, headers=ch,
        json={"responses": [
            {"information_request_id": first["id"], "response_note": "a"},
            {"information_request_id": second["id"], "response_note": "b"},
            {"information_request_id": 9999, "response_note": "c"},
        ]},
    )
    assert r.status_code == 409

    r = client.post(
        url, headers=ch,
        json={
            "responses": [
                {"information_request_id": first["id"], "response_note": "Uploaded my payslip."},
                {"information_request_id": second["id"], "response_note": "It is K650."},
            ],
            "monthly_income": 650,
            "document_ids": [_workflow.uploaded_document(app_id, "proof_of_income")],
        },
    )
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["status"] == "officer_review"
    assert r.get_json()["action_required_note"] is None

    responses = {x.information_request_id: x for x in InformationResponse.query.all()}
    assert set(responses) == {first["id"], second["id"]}, "each response links to its own request"
    assert responses[second["id"]].response_note == "It is K650."
    assert responses[second["id"]].responded_by == customer.id
    assert responses[second["id"]].field_changes == {"monthly_income": {"old": 800.0, "new": 650.0}}

    # A second round adds rows; the first round's history is untouched.
    r = client.post(
        f"/api/loans/applications/{app_id}/request-action",
        headers=oh,
        json={"requests": [{"request_type": "referee_unreachable", "reason": "Referee didn't answer."}]},
    )
    assert r.status_code == 200, r.get_json()
    statuses = [q["status"] for q in r.get_json()["information_requests"]]
    assert statuses == ["responded", "responded", "open"]
    assert r.get_json()["information_requests"][0]["response"]["response_note"] == "Uploaded my payslip."


def test_request_validation(client, staff, new_application):
    app_id, _, _ = new_application()
    _claim(client, app_id, staff["oh"])
    url = f"/api/loans/applications/{app_id}/request-action"
    for bad in (
        {"requests": []},
        {"requests": [{"request_type": "nope", "reason": "x"}]},
        {"requests": [{"request_type": "other", "reason": "  "}]},
        {"requests": [{"request_type": "other", "reason": "x", "required_document_type": "selfie"}]},
    ):
        assert client.post(url, headers=staff["oh"], json=bad).status_code == 400, bad
    good = {"requests": [{"request_type": "other", "reason": "x"}]}
    assert client.post(url, headers=staff["xh"], json=good).status_code == 403, "not their claim"


# ============================================================= recommendation
def test_approval_recommendation_requires_a_complete_checklist(client, staff, new_application, workflow):
    oh = staff["oh"]
    app_id, _, _ = new_application()
    _claim(client, app_id, oh)
    url = f"/api/loans/applications/{app_id}/recommend"

    r = client.post(url, headers=oh, json={"recommendation": "recommend_approval", "comments": "ok"})
    assert r.status_code == 409
    assert "valid_id" in r.get_json()["message"]
    assert client.post(url, headers=oh, json={"recommendation": "recommend_approval"}).status_code == 400
    assert client.post(url, headers=oh, json={"recommendation": "approve", "comments": "x"}).status_code == 400

    workflow.complete_checklist(app_id, oh)
    client.patch(
        f"/api/officer/applications/{app_id}/checklist/employment",
        headers=oh,
        json={"status": "failed", "note": "Employer denies employment."},
    )
    assert client.post(
        url, headers=oh, json={"recommendation": "recommend_approval", "comments": "ok"}
    ).status_code == 409, "a failed item blocks approval"


def test_recommendation_records_snapshot_and_creates_no_loan(client, staff, new_application, workflow):
    oh = staff["oh"]
    app_id, _, _ = new_application()
    _claim(client, app_id, oh)
    workflow.complete_checklist(app_id, oh)

    r = client.post(
        f"/api/loans/applications/{app_id}/recommend",
        headers=oh,
        json={"recommendation": "recommend_approval", "comments": "All checks passed."},
    )
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["application"]["status"] == "recommended_for_approval"
    rec = body["recommendation"]
    assert rec["officer_id"] == staff["officer"].id
    assert rec["comments"] == "All checks passed."
    assert {i["item_type"]: i["status"] for i in rec["checklist_snapshot"]} == {
        t.key: "verified" for t in verification.CHECKLIST
    }
    assert rec["credit_evaluation_snapshot"]["algorithm"] == "interim-v2"

    # The recommendation is a recommendation only.
    assert Loan.query.count() == 0
    assert Disbursement.query.count() == 0
    assert RepaymentSchedule.query.count() == 0
    assert db.session.get(LoanApplication, app_id).decided_at is None


def test_rejection_recommendation_goes_to_admin_and_survives_the_decision(
    client, staff, new_application
):
    oh, ah = staff["oh"], staff["ah"]
    app_id, _, _ = new_application()
    _claim(client, app_id, oh)

    # Rejection can be recommended without a complete checklist.
    r = client.post(
        f"/api/loans/applications/{app_id}/recommend",
        headers=oh,
        json={"recommendation": "recommend_rejection", "comments": "Referee is the applicant."},
    )
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["application"]["status"] == "recommended_for_rejection"
    rec_id = r.get_json()["recommendation"]["id"]

    assert client.post(f"/api/loans/applications/{app_id}/admin-review", headers=ah).status_code == 200
    # Approving against the recommendation needs a reason...
    url = f"/api/loans/applications/{app_id}/decision"
    assert client.post(url, headers=ah, json={"decision": "approve"}).status_code == 400
    r = client.post(url, headers=ah, json={"decision": "approve", "note": "Referee re-verified by me."})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["application"]["status"] == "awaiting_disbursement"

    # ...the recommendation row is untouched by the admin's decision...
    rec = db.session.get(OfficerRecommendation, rec_id)
    assert rec.recommendation.value == "recommend_rejection"
    assert rec.comments == "Referee is the applicant."
    # ...and the decision's audit entry says it overrode it.
    entry = AuditLog.query.filter_by(action="loan_application_decision", entity_id=str(app_id)).one()
    assert entry.details["recommendation_id"] == rec_id
    assert entry.details["overrides_recommendation"] is True
    assert entry.details["same_actor_as_recommender"] is False


def test_admin_may_recommend_and_decide_as_separate_audited_steps(
    client, staff, new_application, workflow
):
    ah = staff["ah"]
    app_id, _, _ = new_application()
    _claim(client, app_id, ah)
    assert workflow.recommend(app_id, ah).status_code == 200
    assert client.post(f"/api/loans/applications/{app_id}/admin-review", headers=ah).status_code == 200
    r = client.post(
        f"/api/loans/applications/{app_id}/decision", headers=ah, json={"decision": "approve"}
    )
    assert r.status_code == 200, r.get_json()

    actions = [
        e.action
        for e in AuditLog.query.filter_by(entity_id=str(app_id), actor_id=staff["admin"].id)
        .order_by(AuditLog.id)
        .all()
    ]
    assert actions.index("loan_application_recommended_for_approval") < actions.index(
        "loan_application_decision"
    )
    decision = AuditLog.query.filter_by(action="loan_application_decision", entity_id=str(app_id)).one()
    assert decision.details["same_actor_as_recommender"] is True


def test_return_to_officer_round_trip(client, staff, new_application, workflow):
    oh, ah = staff["oh"], staff["ah"]
    app_id, _, _ = new_application()
    _claim(client, app_id, oh)
    workflow.recommend(app_id, oh)

    url = f"/api/loans/applications/{app_id}/return-to-officer"
    assert client.post(url, headers=ah, json={}).status_code == 400
    r = client.post(url, headers=ah, json={"reason": "Call the referee again."})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["application"]["status"] == "returned_to_officer"

    detail = client.get(f"/api/officer/applications/{app_id}", headers=oh).get_json()
    assert detail["admin_returns"][0]["reason"] == "Call the referee again."
    assert detail["allowed_actions"] == ["resume_review"]

    r = client.post(f"/api/loans/applications/{app_id}/resume-review", headers=oh, json={})
    assert r.status_code == 200 and r.get_json()["status"] == "officer_review"
    r = workflow.recommend(app_id, oh, comments="Referee confirmed on second call.")
    assert r.status_code == 200
    assert OfficerRecommendation.query.filter_by(loan_application_id=app_id).count() == 2


def test_admin_can_reassign(client, staff, new_application, workflow):
    oh, xh, ah = staff["oh"], staff["xh"], staff["ah"]
    app_id, customer, _ = new_application()
    _claim(client, app_id, oh)
    url = f"/api/loans/applications/{app_id}/assign"
    assert client.post(url, headers=ah, json={"officer_id": customer.id}).status_code == 400
    r = client.post(url, headers=ah, json={"officer_id": staff["other"].id})
    assert r.status_code == 200 and r.get_json()["assigned_officer_id"] == staff["other"].id

    assert workflow.request_info(app_id, oh).status_code == 403, "no longer theirs"
    assert workflow.request_info(app_id, xh).status_code == 200


# ============================================================ customer history
def _paid_loan_for(customer, *, paid_late: bool):
    """A previous PAID loan, settled on time or late, created directly."""
    from app.models.enums import LoanApplicationStatus, LoanStatus, PaymentStatus, RepaymentStatus

    now = datetime.now(timezone.utc)
    prior = LoanApplication(
        user_id=customer.id, amount_requested=300,
        status=LoanApplicationStatus.AWAITING_DISBURSEMENT,
    )
    db.session.add(prior)
    db.session.flush()
    loan = Loan(
        application_id=prior.id, user_id=customer.id, principal_amount=300, interest_rate=0.5,
        term_days=14, monthly_payment=450, total_repayable=450, status=LoanStatus.PAID,
        disbursed_at=now - timedelta(days=60),
    )
    db.session.add(loan)
    db.session.flush()
    due = (now - timedelta(days=46)).date()
    row = RepaymentSchedule(
        loan_id=loan.id, installment_number=1, due_date=due, amount_due=450, amount_paid=450,
        status=RepaymentStatus.PAID,
    )
    db.session.add(row)
    db.session.flush()
    paid_at = datetime.combine(due, datetime.min.time(), tzinfo=timezone.utc) + timedelta(
        days=5 if paid_late else -1
    )
    db.session.add(PaymentTransaction(
        loan_id=loan.id, repayment_schedule_id=row.id, amount=450, payment_method="cash",
        payment_date=paid_at.date(), status=PaymentStatus.VERIFIED, paid_at=paid_at,
    ))
    db.session.commit()
    return loan


def test_customer_history_summarises_this_customer(client, staff, new_application):
    app_id, customer, _ = new_application()
    _paid_loan_for(customer, paid_late=True)

    r = client.get(f"/api/officer/applications/{app_id}/customer-history", headers=staff["oh"])
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["customer"]["id"] == customer.id
    s = body["summary"]
    assert (s["previous_applications"], s["loans_total"], s["loans_completed"]) == (1, 1, 1)
    assert (s["total_borrowed"], s["total_repaid"], s["current_exposure"]) == (300.0, 450.0, 0.0)
    rec = body["repayment_record"]
    assert (rec["installments_paid_on_time"], rec["installments_paid_late"]) == (0, 1)
    assert rec["installments_ever_overdue"] == 1 and rec["payments_verified"] == 1
    assert body["penalties"]["applicable"] is False
    assert body["previous_applications"][0]["id"] != app_id, "the current application isn't 'previous'"
    assert body["loans"][0]["installments"]["paid_late"] == 1


def test_customer_history_is_team_wide_for_loan_officers(client, staff, new_application):
    """By design, not a bug: an officer who neither created nor claimed the
    application (it's claimed by someone else) may still view the history."""
    app_id, _, _ = new_application()
    _claim(client, app_id, staff["oh"])
    assert client.get(f"/api/officer/applications/{app_id}/customer-history", headers=staff["xh"]).status_code == 200


def test_customer_history_is_scoped_to_open_applications_for_officers(client, staff, new_application):
    app_id, _, _ = new_application()
    _claim(client, app_id, staff["oh"])
    client.post(f"/api/loans/applications/{app_id}/reject", headers=staff["ah"], json={"note": "x"})

    url = f"/api/officer/applications/{app_id}/customer-history"
    assert client.get(url, headers=staff["oh"]).status_code == 403
    assert client.get(url, headers=staff["ah"]).status_code == 200
    assert client.get("/api/officer/applications/99999/customer-history", headers=staff["oh"]).status_code == 404
