"""Late-payment penalties (app/services/penalties.py) and the daily sweep.

Each test backdates a real loan - disbursed through the admin API with the
clock moved back - so its due date (from the terms snapshot) lies in the
past, then runs the job for a chosen day. K500 PRIME 2: K200 original
interest, so tier 1 is +K50 and tier 2 a further +K200.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import Loan, LoanLedgerEntry, LoanTermsSnapshot, RepaymentSchedule, ScheduledJobRun
from app.models.enums import LedgerActorKind, LedgerEntryType
from app.services import ledger, penalties

import _workflow


@pytest.fixture
def people(make_user, auth_header):
    customer, officer, admin = make_user("customer"), make_user("loan_officer"), make_user("admin")
    return {"customer": customer, "ch": auth_header(customer), "oh": auth_header(officer), "ah": auth_header(admin)}


@pytest.fixture
def late_loan(client, people, apply_payload):
    """A K500 loan disbursed 40 days ago - due 26 days ago."""
    class Past(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(timezone.utc) - timedelta(days=40)

    with patch("app.services.loan_processing.datetime", Past):
        _, loan = _workflow.to_disbursed_loan(
            client, people["ch"], people["oh"], people["ah"], apply_payload(amount_requested=500))
    snap = LoanTermsSnapshot.query.filter_by(loan_id=loan["id"]).one()
    return loan["id"], snap.due_date


def _penalties(loan_id):
    return [(e.penalty_tier, e.amount, e.effective_date) for e in LoanLedgerEntry.query.filter_by(
        loan_id=loan_id, entry_type=LedgerEntryType.PENALTY).order_by(LoanLedgerEntry.penalty_tier)]


def _pay(client, people, loan_id, amount, paid_on, verify=True):
    row = RepaymentSchedule.query.filter_by(loan_id=loan_id).one()
    r = client.post("/api/payments/repay", headers=people["ch"], json={
        "repayment_schedule_id": row.id, "amount": amount, "payment_method": "cash",
        "payment_date": paid_on.isoformat()})
    assert r.status_code == 201, r.get_json()
    txn = r.get_json()["transaction"]["id"]
    if verify:
        r = client.post(f"/api/admin/repayments/{txn}/verify", headers=people["ah"])
        assert r.status_code == 200, r.get_json()
    return txn


def _status(loan_id):
    db.session.expire_all()
    return db.session.get(Loan, loan_id).status.value


# ------------------------------------------------------------- due date + status
def test_due_date_comes_from_the_snapshot_and_overdue_starts_the_next_day(late_loan):
    loan_id, due = late_loan
    assert RepaymentSchedule.query.filter_by(loan_id=loan_id).one().due_date == due
    assert due == LoanTermsSnapshot.query.filter_by(loan_id=loan_id).one().disbursed_local_date + timedelta(days=14)

    penalties.run(as_of=due)
    assert _status(loan_id) == "active", "due today is not yet overdue"
    penalties.run(as_of=due + timedelta(days=1))
    assert _status(loan_id) == "overdue"


# ------------------------------------------------------------- tiers, once each
def test_six_days_late_is_no_penalty_and_day_seven_is_tier_1_once(late_loan):
    loan_id, due = late_loan
    penalties.run(as_of=due + timedelta(days=6))
    assert _penalties(loan_id) == []

    for _ in range(2):  # same day twice
        penalties.run(as_of=due + timedelta(days=7))
    penalties.run(as_of=due + timedelta(days=8))
    assert _penalties(loan_id) == [(1, Decimal("50.00"), due + timedelta(days=7))]
    assert ledger.balance(loan_id) == Decimal("750.00")


def test_two_weeks_late_adds_tier_2_and_nothing_after(late_loan):
    loan_id, due = late_loan
    for day in (14, 14, 20, 26):
        penalties.run(as_of=due + timedelta(days=day))
    assert _penalties(loan_id) == [
        (1, Decimal("50.00"), due + timedelta(days=7)),
        (2, Decimal("200.00"), due + timedelta(days=14)),
    ], "25% then 100% of the K200 ORIGINAL interest - never of the balance"
    assert ledger.balance(loan_id) == Decimal("950.00")
    entry = LoanLedgerEntry.query.filter_by(loan_id=loan_id, penalty_tier=2).one()
    assert entry.created_by is None and entry.created_by_kind == LedgerActorKind.SYSTEM
    assert db.session.get(ScheduledJobRun, entry.job_run_id).job_name == penalties.JOB_NAME


def test_the_database_refuses_a_second_entry_for_a_tier(late_loan):
    loan_id, due = late_loan
    penalties.run(as_of=due + timedelta(days=7))
    first = LoanLedgerEntry.query.filter_by(loan_id=loan_id, penalty_tier=1).one()
    db.session.add(LoanLedgerEntry(
        loan_id=loan_id, entry_type=LedgerEntryType.PENALTY, amount=50, effective_date=first.effective_date,
        created_by_kind=LedgerActorKind.SYSTEM, penalty_tier=1,
        penalty_policy_version_id=first.penalty_policy_version_id, job_run_id=first.job_run_id))
    with pytest.raises(IntegrityError):
        db.session.flush()
    db.session.rollback()


# ------------------------------------------------------------- what counts as paid
def test_paying_in_full_six_days_late_avoids_tier_1(client, people, late_loan):
    loan_id, due = late_loan
    _pay(client, people, loan_id, 700, due + timedelta(days=6))
    assert _status(loan_id) == "closed"
    penalties.run(as_of=due + timedelta(days=20))
    assert _penalties(loan_id) == []


def test_paying_in_full_on_day_seven_still_owes_tier_1(client, people, late_loan):
    """Verification applies a tier that's already due before it checks the
    balance - so the late full payment doesn't close the loan."""
    loan_id, due = late_loan
    _pay(client, people, loan_id, 700, due + timedelta(days=7))
    assert _penalties(loan_id)[0][:2] == (1, Decimal("50.00"))
    assert ledger.balance(loan_id) == Decimal("50.00")
    assert _status(loan_id) == "overdue"

    penalties.run(as_of=due + timedelta(days=26))
    assert len(_penalties(loan_id)) == 1, "an unpaid penalty doesn't trigger tier 2"

    row = RepaymentSchedule.query.filter_by(loan_id=loan_id).one()
    assert row.status.value == "paid", "the original installment is paid"
    r = client.post("/api/payments/repay", headers=people["ch"], json={
        "repayment_schedule_id": row.id, "amount": 60, "payment_method": "cash"})
    assert r.status_code == 400, "more than the K50 still owed"
    _pay(client, people, loan_id, 50, due + timedelta(days=25))
    assert _status(loan_id) == "closed"
    closure = db.session.get(Loan, loan_id).closure
    assert (closure.total_penalties, closure.total_verified_paid, closure.timeliness.value) == (
        Decimal("50.00"), Decimal("750.00"), "late_tier_2")


def test_a_partial_payment_doesnt_avoid_the_tier(client, people, late_loan):
    loan_id, due = late_loan
    # Verified today - 26 days after the due date - so both tiers already
    # due go on at verification: K700 + K50 + K200 - K300.
    _pay(client, people, loan_id, 300, due + timedelta(days=2))
    penalties.run(as_of=due + timedelta(days=26))
    assert [p[:2] for p in _penalties(loan_id)] == [(1, Decimal("50.00")), (2, Decimal("200.00"))]
    assert ledger.balance(loan_id) == Decimal("650.00")


def test_an_unverified_report_holds_the_tier_until_its_rejected(client, people, late_loan):
    loan_id, due = late_loan
    txn = _pay(client, people, loan_id, 700, due + timedelta(days=3), verify=False)
    summary = penalties.run(as_of=due + timedelta(days=7))
    assert summary["held_pending_verification"] == [{"loan_id": loan_id, "tier": 1}]
    assert _penalties(loan_id) == []

    client.post(f"/api/admin/repayments/{txn}/reject", headers=people["ah"], json={"reason": "No such payment."})
    penalties.run(as_of=due + timedelta(days=8))
    assert _penalties(loan_id)[0][:2] == (1, Decimal("50.00"))


def test_a_report_verified_late_but_paid_on_time_is_never_penalised(client, people, late_loan):
    loan_id, due = late_loan
    txn = _pay(client, people, loan_id, 700, due, verify=False)
    penalties.run(as_of=due + timedelta(days=9))  # held: the report covers it
    assert client.post(f"/api/admin/repayments/{txn}/verify", headers=people["ah"]).status_code == 200
    assert _penalties(loan_id) == [] and _status(loan_id) == "closed"


# ------------------------------------------------------------- the customer sees it
def test_customer_summary_shows_the_penalty_owed(client, people, late_loan):
    loan_id, due = late_loan
    penalties.run(as_of=due + timedelta(days=7))
    (summary,) = client.get("/api/accounts/summary", headers=people["ch"]).get_json()["active_loans"]
    assert (summary["penalties"], summary["amount_remaining"], summary["amount_paid"]) == (50.0, 750.0, 0.0)
    assert summary["status"] == "overdue"
