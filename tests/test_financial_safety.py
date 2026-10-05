"""RBAC and financial-safety pass over the Administrator build (ledger,
terms snapshot, disbursement, verification, penalties, admin API).

1. Forbidden actions: loan_officer and customer tokens fail on every money-
   moving or policy-changing admin action - at the route AND in the service
   (so a stale "admin" token claim after a role change still can't act).
2. Duplicates: double disbursement, double verification and a double
   penalty are each refused - by the API and by the database itself.
3. Rollback: an error after the ledger write but before the status update
   leaves nothing behind - disbursement, verification and the penalty job.
4. Session/CORS/rate-limit protections still hold on the new routes.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import (
    AuditLog,
    Disbursement,
    Loan,
    LoanApplication,
    LoanLedgerEntry,
    LoanTermsSnapshot,
    PaymentTransaction,
    RepaymentSchedule,
    ScheduledJobRun,
    User,
)
from app.models.enums import (
    DisbursementMethod,
    LedgerActorKind,
    LedgerEntryType,
    LoanApplicationStatus,
    UserRole,
)
from app.services import loan_processing, parameters, payment_processing, penalties, pricing_policy
from app.services.errors import ServiceError

import _workflow


@pytest.fixture
def people(make_user, auth_header):
    customer, officer, admin = make_user("customer"), make_user("loan_officer"), make_user("admin")
    return {"customer": customer, "officer": officer, "admin": admin,
            "ch": auth_header(customer), "oh": auth_header(officer), "ah": auth_header(admin)}


def _recommended(client, people, apply_payload, customer_headers=None):
    ch = customer_headers or people["ch"]
    app_id = client.post("/api/loans/apply", headers=ch, json=apply_payload(amount_requested=500)).get_json()["id"]
    assert client.post(f"/api/loans/applications/{app_id}/officer-review", headers=people["oh"]).status_code == 200
    assert _workflow.recommend(client, app_id, people["oh"]).status_code == 200
    return app_id


def _approved(client, people, apply_payload, customer_headers=None):
    app_id = _recommended(client, people, apply_payload, customer_headers)
    assert client.post(f"/api/admin/applications/{app_id}/approve", headers=people["ah"], json={}).status_code == 200
    return app_id


def _disburse(client, people, app_id):
    return client.post(f"/api/admin/applications/{app_id}/disbursement", headers=people["ah"],
                       json={"method": "cash_on_hand", "reference": "CASH-ACK-9"})


@pytest.fixture
def world(client, people, apply_payload, make_user, auth_header):
    """One application awaiting the final decision, one awaiting
    disbursement, and an active loan with a payment report pending."""
    awaiting_decision = _recommended(client, people, apply_payload)
    second = auth_header(make_user("customer"))
    awaiting_disbursement = _approved(client, people, apply_payload, second)
    third = auth_header(make_user("customer"))
    loan_app = _approved(client, people, apply_payload, third)
    loan_id = _disburse(client, people, loan_app).get_json()["loan_id"]
    row = RepaymentSchedule.query.filter_by(loan_id=loan_id).one()
    txn = client.post("/api/payments/repay", headers=third, json={
        "repayment_schedule_id": row.id, "amount": 700, "payment_method": "cash"}).get_json()["transaction"]["id"]
    return {"decision": awaiting_decision, "disburse": awaiting_disbursement, "loan": loan_id, "txn": txn}


# ====================================================== 1. forbidden actions
def _financial_actions(w):
    d, b, loan, txn = w["decision"], w["disburse"], w["loan"], w["txn"]
    disb = {"method": "cash_on_hand", "reference": "X", "method_reference": "X"}
    return [
        # final decision
        ("POST", f"/api/admin/applications/{d}/approve", {}),
        ("POST", f"/api/admin/applications/{d}/reject", {"reason": "x"}),
        ("POST", f"/api/admin/applications/{d}/return-to-officer", {"reason": "x"}),
        ("POST", f"/api/loans/applications/{d}/admin-review", {}),
        ("POST", f"/api/loans/applications/{d}/decision", {"decision": "approve"}),
        ("POST", f"/api/loans/applications/{d}/reject", {"note": "x"}),
        ("POST", f"/api/loans/applications/{d}/return-to-officer", {"reason": "x"}),
        # disbursement
        ("POST", f"/api/admin/applications/{b}/disbursement", disb),
        ("POST", f"/api/admin/applications/{b}/disbursement-evidence", {}),
        ("POST", f"/api/loans/applications/{b}/disburse", disb),
        # verification
        ("POST", f"/api/admin/repayments/{txn}/verify", {}),
        ("POST", f"/api/admin/repayments/{txn}/reject", {"reason": "x"}),
        ("POST", f"/api/payments/{txn}/verify", {"decision": "verified"}),
        # loan closure
        ("POST", f"/api/admin/loans/{loan}/write-off", {"reason": "x"}),
        ("POST", f"/api/loans/{loan}/write-off", {"note": "x"}),
        # parameters and policy
        ("GET", "/api/admin/parameters", None),
        ("PUT", "/api/admin/parameters", {"min_monthly_income": 1}),
        ("POST", "/api/admin/pricing", {"tiers": [{"category": "P", "min_amount": 100, "max_amount": 1000,
                                                   "interest_rate": 0.01}]}),
        ("POST", "/api/admin/penalty-policy", {"tiers": [{"days_late": 1, "pct_of_original_interest": 5}]}),
    ]


@pytest.mark.parametrize("who", ["oh", "ch"])
def test_officer_and_customer_tokens_cannot_take_any_admin_action(client, people, world, who):
    for method, path, body in _financial_actions(world):
        r = client.open(path, method=method, headers=people[who], json=body)
        assert r.status_code == 403, (who, method, path, r.status_code)

    db.session.expire_all()
    assert db.session.get(LoanApplication, world["decision"]).status == LoanApplicationStatus.RECOMMENDED_FOR_APPROVAL
    assert db.session.get(LoanApplication, world["disburse"]).status == LoanApplicationStatus.AWAITING_DISBURSEMENT
    assert db.session.get(PaymentTransaction, world["txn"]).status.value == "reported"
    assert db.session.get(Loan, world["loan"]).status.value == "active"
    assert LoanLedgerEntry.query.filter_by(entry_type=LedgerEntryType.VERIFIED_REPAYMENT).count() == 0
    assert pricing_policy.current_pricing_version().label == "prime-v1"
    assert pricing_policy.current_penalty_policy().label == "penalty-v1"


def test_services_refuse_a_non_admin_even_without_the_route(people, world):
    officer = people["officer"]
    get = lambda m, i: db.session.get(m, i)  # noqa: E731
    calls = {
        "approve": lambda: loan_processing.admin_decide(get(LoanApplication, world["decision"]), officer, approve=True),
        "reject": lambda: loan_processing.reject_application(get(LoanApplication, world["decision"]), officer, "x"),
        "return": lambda: loan_processing.return_to_officer(get(LoanApplication, world["decision"]), officer, "x"),
        "disburse": lambda: loan_processing.disburse_application(
            get(LoanApplication, world["disburse"]), officer, method="cash_on_hand", method_reference="X"),
        "verify": lambda: payment_processing.verify_payment(officer, world["txn"], decision="verified"),
        "write_off": lambda: loan_processing.write_off_loan(get(Loan, world["loan"]), officer, note="x"),
        "parameters": lambda: parameters.update({"min_monthly_income": 1}, officer.id),
        "pricing": lambda: pricing_policy.create_pricing_version(officer, [
            {"category": "P", "min_amount": 100, "max_amount": 1000, "interest_rate": 0.01}]),
        "penalty": lambda: pricing_policy.create_penalty_version(officer, [
            {"days_late": 1, "pct_of_original_interest": 5}]),
    }
    for name, call in calls.items():
        with pytest.raises(ServiceError) as exc:
            call()
        assert exc.value.status_code == 403, name
        db.session.rollback()


def test_a_demoted_admins_old_token_still_cant_move_money(client, people, world):
    """The token still says "admin" for up to an hour after a role change;
    the service checks the role in the database and refuses."""
    admin = db.session.get(User, people["admin"].id)
    admin.role = UserRole.LOAN_OFFICER
    db.session.commit()
    ah = people["ah"]
    assert client.post(f"/api/admin/applications/{world['disburse']}/disbursement", headers=ah,
                       json={"method": "cash_on_hand", "reference": "X"}).status_code == 403
    assert client.post(f"/api/admin/repayments/{world['txn']}/verify", headers=ah).status_code == 403
    assert client.put("/api/admin/parameters", headers=ah, json={"min_monthly_income": 1}).status_code == 403
    assert client.post(f"/api/admin/applications/{world['decision']}/approve", headers=ah, json={}).status_code == 403
    assert Disbursement.query.filter_by(application_id=world["disburse"]).count() == 0


# ========================================================= 2. duplicates
def test_double_disbursement_is_refused_by_the_api_and_the_database(client, people, world):
    app_id = world["disburse"]
    assert _disburse(client, people, app_id).status_code == 201
    assert _disburse(client, people, app_id).status_code == 409

    loan = Loan.query.filter_by(application_id=app_id).one()
    other_loan = db.session.get(Loan, world["loan"])
    db.session.add(Disbursement(application_id=app_id, loan_id=other_loan.id,
                                method=DisbursementMethod.CASH_ON_HAND, amount=500, recorded_by=people["admin"].id))
    with pytest.raises(IntegrityError):
        db.session.flush()
    db.session.rollback()
    assert (Disbursement.query.filter_by(application_id=app_id).count(),
            LoanTermsSnapshot.query.filter_by(application_id=app_id).count(),
            LoanLedgerEntry.query.filter_by(loan_id=loan.id, entry_type=LedgerEntryType.ORIGINAL_OBLIGATION).count()
            ) == (1, 1, 1)


def test_double_verification_is_refused_by_the_api_and_the_database(client, people, world):
    txn = world["txn"]
    assert client.post(f"/api/admin/repayments/{txn}/verify", headers=people["ah"]).status_code == 200
    assert client.post(f"/api/admin/repayments/{txn}/verify", headers=people["ah"]).status_code == 409
    assert client.post(f"/api/payments/{txn}/verify", headers=people["ah"],
                       json={"decision": "verified"}).status_code == 409

    db.session.add(LoanLedgerEntry(
        loan_id=world["loan"], entry_type=LedgerEntryType.VERIFIED_REPAYMENT, amount=-700,
        effective_date=date.today(), created_by=people["admin"].id, created_by_kind=LedgerActorKind.ADMIN,
        payment_transaction_id=txn))
    with pytest.raises(IntegrityError):
        db.session.flush()
    db.session.rollback()
    assert LoanLedgerEntry.query.filter_by(payment_transaction_id=txn).count() == 1


@pytest.fixture
def overdue_loan(client, people, apply_payload, make_user, auth_header):
    class Past(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(timezone.utc) - timedelta(days=40)

    with patch("app.services.loan_processing.datetime", Past):
        app_id = _approved(client, people, apply_payload, auth_header(make_user("customer")))
        loan_id = _disburse(client, people, app_id).get_json()["loan_id"]
    return loan_id, LoanTermsSnapshot.query.filter_by(loan_id=loan_id).one().due_date


def test_double_penalty_is_refused_by_the_job_and_the_database(overdue_loan):
    loan_id, due = overdue_loan
    for _ in range(3):
        penalties.run(as_of=due + timedelta(days=8))
    loan = db.session.get(Loan, loan_id)
    run = ScheduledJobRun.query.first()
    penalties.assess_loan(loan, due + timedelta(days=8), run)  # directly, outside the job
    db.session.commit()
    assert LoanLedgerEntry.query.filter_by(loan_id=loan_id, entry_type=LedgerEntryType.PENALTY).count() == 1

    first = LoanLedgerEntry.query.filter_by(loan_id=loan_id, penalty_tier=1).one()
    db.session.add(LoanLedgerEntry(
        loan_id=loan_id, entry_type=LedgerEntryType.PENALTY, amount=50, effective_date=first.effective_date,
        created_by_kind=LedgerActorKind.SYSTEM, penalty_tier=1,
        penalty_policy_version_id=first.penalty_policy_version_id, job_run_id=first.job_run_id))
    with pytest.raises(IntegrityError):
        db.session.flush()
    db.session.rollback()


# ============================================================ 3. rollback
class _FailAfterLedgerWrite:
    """Raise from the flush that writes a ledger entry of `entry_type` - so
    the entry has reached the database (inside the transaction) but nothing
    after it - status update, closure, audit - has run."""

    def __init__(self, entry_type):
        self.entry_type = entry_type

    def __call__(self, session, _flush_context):
        if any(isinstance(o, LoanLedgerEntry) and o.entry_type == self.entry_type for o in session.new):
            raise RuntimeError("injected failure after the ledger write")

    def __enter__(self):
        event.listen(db.session, "after_flush", self)
        return self

    def __exit__(self, *exc):
        event.remove(db.session, "after_flush", self)


def test_disbursement_rolls_back_entirely_after_the_ledger_write(people, world):
    app_id = world["disburse"]
    application = db.session.get(LoanApplication, app_id)
    with _FailAfterLedgerWrite(LedgerEntryType.ORIGINAL_OBLIGATION), pytest.raises(RuntimeError):
        loan_processing.disburse_application(application, people["admin"], method="cash_on_hand",
                                             method_reference="CASH-1")
    db.session.expire_all()
    assert db.session.get(LoanApplication, app_id).status == LoanApplicationStatus.AWAITING_DISBURSEMENT
    assert (Loan.query.filter_by(application_id=app_id).count(),
            Disbursement.query.filter_by(application_id=app_id).count(),
            LoanTermsSnapshot.query.filter_by(application_id=app_id).count()) == (0, 0, 0)
    assert AuditLog.query.filter_by(action="loan_disbursed", entity_type="Loan").count() == 1, \
        "only the fixture's own disbursement"


def test_verification_rolls_back_entirely_after_the_ledger_write(people, world):
    txn_id, loan_id = world["txn"], world["loan"]
    with _FailAfterLedgerWrite(LedgerEntryType.VERIFIED_REPAYMENT), pytest.raises(RuntimeError):
        payment_processing.verify_payment(people["admin"], txn_id, decision="verified")
    db.session.expire_all()
    assert db.session.get(PaymentTransaction, txn_id).status.value == "reported"
    assert LoanLedgerEntry.query.filter_by(payment_transaction_id=txn_id).count() == 0
    row = RepaymentSchedule.query.filter_by(loan_id=loan_id).one()
    assert (row.amount_paid, row.status.value) == (Decimal("0.00"), "upcoming")
    loan = db.session.get(Loan, loan_id)
    assert (loan.status.value, loan.closure) == ("active", None)
    assert AuditLog.query.filter_by(action="payment_verified").count() == 0


def test_the_penalty_job_rolls_back_entirely_on_an_error(overdue_loan):
    loan_id, due = overdue_loan
    with patch("app.services.penalties.sync_status", side_effect=RuntimeError("boom")), pytest.raises(RuntimeError):
        penalties.run(as_of=due + timedelta(days=20))
    db.session.expire_all()
    assert LoanLedgerEntry.query.filter_by(loan_id=loan_id, entry_type=LedgerEntryType.PENALTY).count() == 0
    assert ScheduledJobRun.query.filter_by(job_name=penalties.JOB_NAME).count() == 0
    assert db.session.get(Loan, loan_id).status.value == "active"


# ================================================ 4. sessions, CORS, limits
def test_step_tokens_and_revoked_sessions_cant_reach_admin_routes(client, app, people, make_user):
    from app.api.auth.tokens import mfa_challenge_token, mfa_setup_token

    admin = db.session.get(User, people["admin"].id)
    with app.test_request_context():
        step_tokens = [mfa_setup_token(admin), mfa_challenge_token(admin)]
    for token in step_tokens:
        r = client.get("/api/admin/queues", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 401, "only a full access token reaches an admin route"

    assert client.get("/api/admin/queues", headers=people["ah"]).status_code == 200
    admin.token_version += 1  # what a password reset does
    db.session.commit()
    assert client.get("/api/admin/queues", headers=people["ah"]).status_code == 401


def test_cors_on_admin_routes_only_answers_the_configured_frontend(client, app):
    allowed = app.config["CORS_ORIGINS"][0]
    ok = client.options("/api/admin/queues", headers={
        "Origin": allowed, "Access-Control-Request-Method": "GET"})
    assert ok.headers.get("Access-Control-Allow-Origin") == allowed
    evil = client.options("/api/admin/queues", headers={
        "Origin": "https://evil.example", "Access-Control-Request-Method": "GET"})
    assert "Access-Control-Allow-Origin" not in evil.headers


def test_login_rate_limit_still_applies(client, make_user):
    make_user("admin", email="limit-check@test.local")
    codes = [client.post("/api/auth/login", json={"email": "limit-check@test.local", "password": "wrong-pw"}).status_code
             for _ in range(6)]
    assert codes[:5] == [401] * 5 and codes[5] == 429
