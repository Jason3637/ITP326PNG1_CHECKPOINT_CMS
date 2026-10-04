"""Item 8: Proof of Income required at/above K1,000 (named rule
PROOF_OF_INCOME_REQUIRED_ABOVE). Item 3: explicit loan closure/write-off
actions, since nothing auto-advances PAID -> CLOSED or ACTIVE -> CLOSED.
"""

from app.services import documents, loan_processing


def _make_document(user, *, document_type="proof_of_income"):
    """Insert a Document row directly - POST /users/documents hits real
    Supabase Storage (unconfigured in tests), so fixture setup bypasses it,
    same as other tests build Loan/RepaymentSchedule rows directly."""
    from app.extensions import db
    from app.models import Document
    from app.models.enums import DocumentType

    doc = Document(
        user_id=user.id,
        document_type=DocumentType(document_type),
        storage_path=f"users/{user.id}/{document_type}/test.pdf",
    )
    db.session.add(doc)
    db.session.commit()
    return doc


def test_amount_at_k1000_without_proof_of_income_is_rejected(
    client, make_user, auth_header, apply_payload
):
    ch = auth_header(make_user("customer"))
    r = client.post("/api/loans/apply", headers=ch, json=apply_payload(amount_requested=1000))
    assert r.status_code == 400
    assert "Proof of Income" in r.get_json()["message"]


def test_amount_at_k1000_with_proof_of_income_succeeds(
    client, make_user, auth_header, apply_payload
):
    user = make_user("customer")
    ch = auth_header(user)
    doc = _make_document(user)

    r = client.post(
        "/api/loans/apply",
        headers=ch,
        json=apply_payload(amount_requested=1000, document_ids=[doc.id]),
    )
    assert r.status_code == 201, r.get_json()
    assert r.get_json()["prime_category"] == "PRIME 3"


def test_amount_below_k1000_does_not_require_proof_of_income(
    client, make_user, auth_header, apply_payload
):
    ch = auth_header(make_user("customer"))
    r = client.post("/api/loans/apply", headers=ch, json=apply_payload(amount_requested=999))
    assert r.status_code == 201, r.get_json()


def test_named_rule_threshold_is_exactly_k1000(app):
    assert documents.proof_of_income_required(999) is False
    assert documents.proof_of_income_required(1000) is True


def test_write_off_loan_marks_closed_defaulted(app, make_user):
    from app.extensions import db
    from app.models import Loan, LoanApplication
    from app.models.enums import LoanApplicationStatus, LoanStatus

    user = make_user("customer")
    admin = make_user("admin")
    application = LoanApplication(
        user_id=user.id, amount_requested=500, status=LoanApplicationStatus.AWAITING_DISBURSEMENT
    )
    db.session.add(application)
    db.session.flush()
    loan = Loan(
        application_id=application.id,
        user_id=user.id,
        principal_amount=500,
        interest_rate="0.40",
        term_days=14,
        monthly_payment=700,
        total_repayable=700,
        status=LoanStatus.OVERDUE,
    )
    db.session.add(loan)
    db.session.flush()
    from datetime import date
    from app.models import LoanLedgerEntry
    from app.models.enums import LedgerActorKind, LedgerEntryType

    db.session.add(LoanLedgerEntry(loan_id=loan.id, entry_type=LedgerEntryType.ORIGINAL_OBLIGATION,
                                   amount=700, effective_date=date.today(),
                                   created_by_kind=LedgerActorKind.SYSTEM))
    db.session.commit()

    written_off = loan_processing.write_off_loan(loan, admin, note="Uncollectable.")
    assert str(written_off.status) == "closed"
    assert str(written_off.closure_reason) == "defaulted"
