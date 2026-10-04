"""Administrator loan records (migrations d8c1f4a2b6e3 / e4a9b7c2d158):
the locked PRIME quote, the terms snapshot and ledger written at
disbursement, the database-level guard against disbursing twice, and the
insert-only rules.

Unique and CHECK constraints are exercised here on SQLite (create_all builds
the same constraints and partial indexes). The Postgres triggers that also
refuse UPDATE/DELETE are checked against a real Postgres when the migration
is run (see the migration report); the ORM guard tested here is the same
rule enforced inside the app.
"""

import ast
import pathlib
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import (
    Disbursement,
    LoanApplication,
    LoanLedgerEntry,
    LoanTermsSnapshot,
    Loan,
    PenaltyPolicyVersion,
    PrimePricingVersion,
    RepaymentSchedule,
)
from app.models.enums import DisbursementMethod, LedgerActorKind, LedgerEntryType, LoanStatus
from app.models.immutability import ImmutableRecordError
from app.services import prime_pricing

import _workflow

APP_ROOT = pathlib.Path(__file__).resolve().parent.parent / "app"


@pytest.fixture
def people(make_user, auth_header):
    customer, officer, admin = make_user("customer"), make_user("loan_officer"), make_user("admin")
    return {
        "admin": admin,
        "ch": auth_header(customer), "oh": auth_header(officer), "ah": auth_header(admin),
    }


@pytest.fixture
def disbursed(client, people, apply_payload):
    """A K500 (PRIME 2) loan, disbursed through the API."""
    app_id, loan = _workflow.to_disbursed_loan(
        client, people["ch"], people["oh"], people["ah"], apply_payload(amount_requested=500)
    )
    return app_id, loan["id"]


def _balance(loan_id):
    return db.session.query(func.coalesce(func.sum(LoanLedgerEntry.amount), 0)).filter_by(
        loan_id=loan_id
    ).scalar()


# ------------------------------------------------------------- the quote
def test_seeded_version_1_matches_the_pricing_code(app):
    version = PrimePricingVersion.query.one()
    assert [(t.category, t.min_amount, t.max_amount, t.interest_rate) for t in version.tiers] == [
        (c, Decimal(lo).quantize(Decimal("0.01")), Decimal(hi).quantize(Decimal("0.01")), r)
        for c, lo, hi, r in prime_pricing.TIERS
    ]
    penalty = PenaltyPolicyVersion.query.one()
    assert [(t.tier, t.days_late, t.pct_of_original_interest) for t in penalty.tiers] == [
        (1, 7, Decimal("0.2500")), (2, 14, Decimal("1.0000"))
    ]


def test_submission_locks_the_quote(client, people, apply_payload):
    r = client.post("/api/loans/apply", headers=people["ch"], json=apply_payload(amount_requested=500))
    application = db.session.get(LoanApplication, r.get_json()["id"])
    assert application.pricing_version_id == PrimePricingVersion.query.one().id
    assert application.penalty_policy_version_id == PenaltyPolicyVersion.query.one().id
    assert (application.quoted_interest_rate, application.quoted_interest_amount,
            application.quoted_total_repayable) == (Decimal("0.4000"), Decimal("200.00"), Decimal("700.00"))

    application.quoted_interest_amount = Decimal("1.00")
    with pytest.raises(ImmutableRecordError):
        db.session.flush()
    db.session.rollback()


# ----------------------------------------------- snapshot + ledger at disbursement
def test_disbursement_writes_the_snapshot_and_the_original_obligation(disbursed, people):
    app_id, loan_id = disbursed
    snap = LoanTermsSnapshot.query.filter_by(loan_id=loan_id).one()
    disb = Disbursement.query.filter_by(loan_id=loan_id).one()
    assert (snap.application_id, snap.disbursement_id, disb.application_id) == (app_id, disb.id, app_id)
    assert (snap.prime_category, snap.principal, snap.interest_rate, snap.interest_amount,
            snap.original_total_due, snap.term_days) == (
        "PRIME 2", Decimal("500.00"), Decimal("0.4000"), Decimal("200.00"), Decimal("700.00"), 14)
    assert (snap.due_date - snap.disbursed_local_date).days == 14
    assert RepaymentSchedule.query.filter_by(loan_id=loan_id).one().due_date == snap.due_date
    assert snap.created_by == people["admin"].id

    (entry,) = LoanLedgerEntry.query.filter_by(loan_id=loan_id).all()
    assert (entry.entry_type, entry.amount, entry.disbursement_id, entry.created_by_kind) == (
        LedgerEntryType.ORIGINAL_OBLIGATION, Decimal("700.00"), disb.id, LedgerActorKind.ADMIN)
    assert _balance(loan_id) == Decimal("700.00")


def test_due_date_counts_from_the_port_moresby_date(client, people, apply_payload):
    # 20:00 UTC on 5 Oct is 06:00 on 6 Oct in Port Moresby: due 20 Oct, not 19 Oct.
    class Fixed(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 5, 20, 0, tzinfo=timezone.utc)

    with patch("app.services.loan_processing.datetime", Fixed):
        _, loan = _workflow.to_disbursed_loan(
            client, people["ch"], people["oh"], people["ah"], apply_payload(amount_requested=300)
        )
    snap = LoanTermsSnapshot.query.filter_by(loan_id=loan["id"]).one()
    assert (snap.disbursed_local_date, snap.due_date) == (date(2026, 10, 6), date(2026, 10, 20))


def test_verifying_a_payment_posts_a_repayment_entry(client, disbursed, people):
    _, loan_id = disbursed
    row = RepaymentSchedule.query.filter_by(loan_id=loan_id).one()
    r = client.post("/api/payments/repay", headers=people["ch"], json={
        "repayment_schedule_id": row.id, "amount": 700, "payment_method": "cash"})
    txn_id = r.get_json()["transaction"]["id"]
    assert _balance(loan_id) == Decimal("700.00"), "a report alone moves nothing"

    assert client.post(f"/api/payments/{txn_id}/verify", headers=people["ah"],
                       json={"decision": "verified"}).status_code == 200
    entry = LoanLedgerEntry.query.filter_by(payment_transaction_id=txn_id).one()
    assert (entry.entry_type, entry.amount, entry.created_by) == (
        LedgerEntryType.VERIFIED_REPAYMENT, Decimal("-700.00"), people["admin"].id)
    assert _balance(loan_id) == 0


# --------------------------------------------- disbursing twice: the database says no
def test_second_disbursement_via_the_api_is_409(client, disbursed, people):
    app_id, _ = disbursed
    r = client.post(f"/api/loans/applications/{app_id}/disburse", headers=people["ah"],
                    json={"method": "cash_on_hand", "method_reference": "CASH-ACK-0001"})
    assert r.status_code == 409


def test_second_disbursement_row_is_rejected_by_the_database(client, disbursed, people, apply_payload):
    """Bypass every application check and insert directly: the unique
    constraints still refuse a second loan and a second disbursement for
    the same application."""
    app_id, loan_id = disbursed
    admin_id = people["admin"].id

    db.session.add(Loan(application_id=app_id, user_id=db.session.get(Loan, loan_id).user_id,
                        principal_amount=500, interest_rate=Decimal("0.4"), term_days=14,
                        monthly_payment=700, total_repayable=700, status=LoanStatus.ACTIVE))
    with pytest.raises(IntegrityError):
        db.session.flush()
    db.session.rollback()

    # A second, unrelated loan exists; point a disbursement at it but claim
    # the first application - disbursements.application_id is unique too.
    _, other = _workflow.to_disbursed_loan(
        client, people["ch"], people["oh"], people["ah"], apply_payload(amount_requested=300))
    db.session.add(Disbursement(application_id=app_id, loan_id=other["id"],
                                method=DisbursementMethod.CASH_ON_HAND, amount=300, recorded_by=admin_id))
    with pytest.raises(IntegrityError):
        db.session.flush()
    db.session.rollback()
    assert Disbursement.query.filter_by(application_id=app_id).count() == 1


# --------------------------------------------------------------- ledger guards
def _entry(loan_id, **kw):
    base = dict(loan_id=loan_id, effective_date=date.today(), created_by_kind=LedgerActorKind.SYSTEM)
    return LoanLedgerEntry(**(base | kw))


@pytest.mark.parametrize("bad", [
    {"entry_type": LedgerEntryType.ORIGINAL_OBLIGATION, "amount": 700},     # a second obligation
    {"entry_type": LedgerEntryType.ORIGINAL_OBLIGATION, "amount": -5},      # wrong sign
    {"entry_type": LedgerEntryType.VERIFIED_REPAYMENT, "amount": -5},       # no payment named
    {"entry_type": LedgerEntryType.PENALTY, "amount": 50},                  # no tier / policy / run
    {"entry_type": LedgerEntryType.PENALTY, "amount": 50, "penalty_tier": 1,
     "created_by_kind": LedgerActorKind.ADMIN},                             # admin with no user
])
def test_ledger_constraints_refuse_bad_entries(disbursed, bad):
    _, loan_id = disbursed
    db.session.add(_entry(loan_id, **bad))
    with pytest.raises(IntegrityError):
        db.session.flush()
    db.session.rollback()


# ------------------------------------------------------------- insert-only
@pytest.mark.parametrize("model", [LoanTermsSnapshot, LoanLedgerEntry, Disbursement])
def test_insert_only_rows_cannot_be_updated_or_deleted(disbursed, model):
    _, loan_id = disbursed
    row = model.query.filter_by(loan_id=loan_id).first()
    if model is LoanTermsSnapshot:
        row.due_date = date(2030, 1, 1)
    elif model is LoanLedgerEntry:
        row.amount = Decimal("1.00")
    else:
        row.note = "edited"
    with pytest.raises(ImmutableRecordError):
        db.session.flush()
    db.session.rollback()

    db.session.delete(model.query.filter_by(loan_id=loan_id).first())
    with pytest.raises(ImmutableRecordError):
        db.session.flush()
    db.session.rollback()


def test_snapshot_has_no_update_path_in_the_app():
    """Outside the models, a LoanTermsSnapshot is only ever constructed in
    loan_processing.py (the insert at disbursement). Other modules may read
    it (admin views, the ledger), but nothing assigns to a snapshot's
    attributes or deletes one."""
    calls, writes = [], []
    for path in APP_ROOT.rglob("*.py"):
        if path.parts[-2] == "models":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "LoanTermsSnapshot"):
                calls.append(path.name)
            targets = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                targets = [node.target]
            elif isinstance(node, ast.Delete):
                targets = node.targets
            for t in targets:
                src = ast.unparse(t)
                if isinstance(t, ast.Attribute) and ("snap" in src or "terms_snapshot" in src):
                    writes.append((path.name, src))
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("delete", "update")
                    and "snap" in ast.unparse(node).lower()):
                writes.append((path.name, ast.unparse(node)))
    assert calls == ["loan_processing.py"]
    assert writes == []


def test_no_route_writes_to_a_snapshot(app):
    for rule in app.url_map.iter_rules():
        assert "snapshot" not in rule.rule or rule.methods <= {"GET", "HEAD", "OPTIONS"}, rule.rule
