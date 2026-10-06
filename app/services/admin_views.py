"""Read side of the Administrator API: dashboard queues, the final review
screen, loan detail, repayments awaiting verification, and analytics.

Every queue is a real filtered/counted query. Every money figure for a loan
comes from the ledger (app/services/ledger.py) and the loan's terms
snapshot - never from an editable stored amount.
"""

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from statistics import median

from sqlalchemy import and_, or_

from app.extensions import db
from app.models import (
    AuditLog,
    Loan,
    LoanApplication,
    LoanLedgerEntry,
    LoanTermsSnapshot,
    PaymentTransaction,
    User,
)
from app.models.enums import (
    LedgerEntryType,
    LoanApplicationStatus as S,
    LoanStatus,
    PaymentStatus,
)

from . import ledger, loan_processing, officer_views, payment_processing, prime_pricing
from .errors import ServiceError

_OPEN_LOAN = (LoanStatus.ACTIVE, LoanStatus.OVERDUE)
_AWAITING_VERIFICATION = (PaymentStatus.REPORTED, PaymentStatus.VERIFICATION_PENDING)

# queue key -> (label, kind)
QUEUES = {
    "awaiting_decision": ("Applications Awaiting Final Decision", "application"),
    "awaiting_disbursement": ("Approved - Awaiting Disbursement", "application"),
    "active_loans": ("Active Loans", "loan"),
    "due_today": ("Due Today", "loan"),
    "due_this_week": ("Due This Week", "loan"),
    "overdue": ("Overdue Loans", "loan"),
    "repayments_awaiting_verification": ("Repayments Awaiting Verification", "repayment"),
}


def _f(value) -> float:
    return float(Decimal(value or 0).quantize(Decimal("0.01")))


def _iso(value):
    return value.isoformat() if value is not None else None


# ===================================================================== queues
def _application_query(key):
    statuses = (
        loan_processing.AWAITING_FINAL_DECISION if key == "awaiting_decision"
        else (S.AWAITING_DISBURSEMENT,)
    )
    return LoanApplication.query.filter(LoanApplication.status.in_(statuses)).order_by(
        LoanApplication.submitted_at.asc(), LoanApplication.id.asc()
    )


def _loan_query(key, today: date):
    bal = ledger.balance_subquery()
    q = (
        db.session.query(Loan, LoanTermsSnapshot, bal.c.balance)
        .join(LoanTermsSnapshot, LoanTermsSnapshot.loan_id == Loan.id)
        .outerjoin(bal, bal.c.loan_id == Loan.id)
        .filter(Loan.status.in_(_OPEN_LOAN))
    )
    owing = bal.c.balance > 0
    if key == "due_today":
        q = q.filter(owing, LoanTermsSnapshot.due_date == today)
    elif key == "due_this_week":
        q = q.filter(owing, LoanTermsSnapshot.due_date.between(today, today + timedelta(days=6)))
    elif key == "overdue":
        q = q.filter(owing, LoanTermsSnapshot.due_date < today)
    return q.order_by(LoanTermsSnapshot.due_date.asc(), Loan.id.asc())


def _repayment_query():
    return PaymentTransaction.query.filter(
        PaymentTransaction.status.in_(_AWAITING_VERIFICATION)
    ).order_by(PaymentTransaction.reported_at.asc(), PaymentTransaction.id.asc())


def _query_for(key, today):
    kind = QUEUES[key][1]
    if kind == "application":
        return _application_query(key)
    if kind == "loan":
        return _loan_query(key, today)
    return _repayment_query()


def queue_counts() -> dict:
    today = ledger.today_local()
    return {
        "as_of": today.isoformat(),
        "queues": {
            key: {"label": label, "count": _query_for(key, today).count()}
            for key, (label, _kind) in QUEUES.items()
        },
    }


def list_queue(key: str, viewer: User, *, page: int = 1, per_page: int = 25) -> dict:
    if key not in QUEUES:
        raise ServiceError(f"queue must be one of: {', '.join(QUEUES)}.", 404)
    page, per_page = max(1, page), min(max(1, per_page), 100)
    today = ledger.today_local()
    q = _query_for(key, today)
    total = q.count()
    rows = q.offset((page - 1) * per_page).limit(per_page).all()
    kind = QUEUES[key][1]
    if kind == "application":
        items = [application_item(a, viewer) for a in rows]
    elif kind == "loan":
        items = [loan_item(loan, snap, bal, today) for loan, snap, bal in rows]
    else:
        items = [repayment_item(t) for t in rows]
    return {"queue": key, "label": QUEUES[key][0], "kind": kind, "as_of": today.isoformat(),
            "page": page, "per_page": per_page, "total": total, "items": items}


def application_item(a: LoanApplication, viewer: User) -> dict:
    row = officer_views.queue_item(a, viewer)
    rec = loan_processing.latest_recommendation(a)
    row["recommendation"] = (
        None if rec is None else {
            "id": rec.id,
            "recommendation": rec.recommendation.value,
            "officer_id": rec.officer_id,
            "officer_name": rec.officer.full_name if rec.officer else None,
            "created_at": _iso(rec.created_at),
        }
    )
    row["decided_at"] = _iso(a.decided_at)
    return row


def _customer(user_id) -> dict:
    u = db.session.get(User, user_id)
    return {"id": user_id, "full_name": u.full_name if u else None, "email": u.email if u else None}


def loan_item(loan: Loan, snap: LoanTermsSnapshot, balance, today: date) -> dict:
    t = ledger.totals(loan.id)
    return {
        "loan_id": loan.id,
        "application_id": loan.application_id,
        "customer": _customer(loan.user_id),
        "status": loan.status.value,
        "prime_category": snap.prime_category,
        "principal": _f(snap.principal),
        "interest_amount": _f(snap.interest_amount),
        "original_total_due": _f(snap.original_total_due),
        "penalties": _f(t["penalties"]),
        "verified_repayments": _f(t["verified_repayments"]),
        "outstanding": _f(t["outstanding"]),
        "disbursed_at": _iso(snap.disbursed_at),
        "due_date": snap.due_date.isoformat(),
        "days_overdue": ledger.days_overdue(snap.due_date, t["outstanding"], today),
    }


def repayment_item(t: PaymentTransaction) -> dict:
    loan = t.loan
    return {
        "payment_id": t.id,
        "loan_id": t.loan_id,
        "customer": _customer(loan.user_id) if loan else None,
        "amount_reported": _f(t.amount),
        "payment_date": _iso(t.payment_date),
        "payment_method": t.payment_method,
        "reference_number": t.reference_number,
        "receipts": payment_processing._serialize_receipts(t),
        "status": t.status.value,
        "reported_at": _iso(t.reported_at),
        "loan_outstanding": _f(ledger.balance(t.loan_id)) if loan else None,
    }


def list_repayments(status: str = "awaiting", page: int = 1, per_page: int = 25) -> dict:
    choices = {
        "awaiting": _AWAITING_VERIFICATION,
        "verified": (PaymentStatus.VERIFIED,),
        "rejected": (PaymentStatus.REJECTED,),
        "all": tuple(PaymentStatus),
    }
    if status not in choices:
        raise ServiceError(f"status must be one of: {', '.join(choices)}.")
    page, per_page = max(1, page), min(max(1, per_page), 100)
    q = PaymentTransaction.query.filter(PaymentTransaction.status.in_(choices[status])).order_by(
        PaymentTransaction.reported_at.desc() if status != "awaiting" else PaymentTransaction.reported_at.asc(),
        PaymentTransaction.id.asc(),
    )
    total = q.count()
    rows = q.offset((page - 1) * per_page).limit(per_page).all()
    return {"status": status, "page": page, "per_page": per_page, "total": total,
            "items": [repayment_item(t) for t in rows]}


# ======================================================== final review screen
def application_review(a: LoanApplication, viewer: User, application_payload: dict) -> dict:
    """The Loan Officer review screen's content (officer_views.application_
    detail - one source), plus the admin's own pieces. Admins see every
    application regardless of assignment or status - the officer-side
    scoping rules don't apply to them."""
    officer_views.open_checklist_if_needed(a, viewer)
    detail = officer_views.application_detail(a, viewer, application_payload)
    detail["customer_history_url"] = f"/api/admin/applications/{a.id}/customer-history"
    detail["final_decision"] = {
        "awaiting": a.status in loan_processing.AWAITING_FINAL_DECISION,
        "can_approve": a.status in loan_processing.AWAITING_FINAL_DECISION,
        "can_reject": a.status in loan_processing.AWAITING_FINAL_DECISION
        or a.status in loan_processing.OPEN_APPLICATION_STATUSES,
        "can_return_to_officer": a.status in loan_processing.AWAITING_FINAL_DECISION,
        "can_disburse": a.status == S.AWAITING_DISBURSEMENT,
        "decided_at": _iso(a.decided_at),
        "decided_by": a.decided_by,
        "loan_id": a.loan.id if a.loan else None,
    }
    detail["quote"] = {
        "pricing_version_id": a.pricing_version_id,
        "penalty_policy_version_id": a.penalty_policy_version_id,
        "interest_rate": float(a.quoted_interest_rate) if a.quoted_interest_rate is not None else None,
        "interest_amount": _f(a.quoted_interest_amount) if a.quoted_interest_amount is not None else None,
        "total_repayable": _f(a.quoted_total_repayable) if a.quoted_total_repayable is not None else None,
    }
    return detail


# ================================================================ loan detail
def list_loans(status: str = "open", page: int = 1, per_page: int = 25) -> dict:
    choices = {"open": _OPEN_LOAN, "closed": (LoanStatus.CLOSED,), "all": tuple(LoanStatus)}
    if status not in choices:
        raise ServiceError(f"status must be one of: {', '.join(choices)}.")
    page, per_page = max(1, page), min(max(1, per_page), 100)
    today = ledger.today_local()
    bal = ledger.balance_subquery()
    q = (
        db.session.query(Loan, LoanTermsSnapshot, bal.c.balance)
        .join(LoanTermsSnapshot, LoanTermsSnapshot.loan_id == Loan.id)
        .outerjoin(bal, bal.c.loan_id == Loan.id)
        .filter(Loan.status.in_(choices[status]))
        .order_by(Loan.id.desc())
    )
    total = q.count()
    rows = q.offset((page - 1) * per_page).limit(per_page).all()
    return {"status": status, "page": page, "per_page": per_page, "total": total,
            "items": [loan_item(loan, snap, b, today) for loan, snap, b in rows]}


def _reapplication(loan: Loan) -> dict | None:
    """For a written-off loan: whether it still stops the customer applying,
    and the admin clearance if there is one. None for any other loan."""
    from app.models.enums import LoanClosureReason

    if loan.closure_reason != LoanClosureReason.DEFAULTED:
        return None
    c = loan.reapplication_clearance
    return {
        "blocked": c is None,
        "cleared_at": _iso(c.created_at) if c else None,
        "cleared_by": c.cleared_by if c else None,
        "cleared_by_name": c.cleared_by_user.full_name if c and c.cleared_by_user else None,
        "reason": c.reason if c else None,
    }


def loan_detail(loan: Loan) -> dict:
    snap = loan.terms_snapshot
    if snap is None:
        raise ServiceError(f"Loan #{loan.id} has no terms snapshot.", 409)
    t = ledger.totals(loan.id)
    disb = loan.disbursement
    closure = loan.closure
    payments = PaymentTransaction.query.filter_by(loan_id=loan.id).order_by(PaymentTransaction.id).all()
    payment_ids = [str(p.id) for p in payments]
    audit_rows = (
        AuditLog.query.filter(or_(
            and_(AuditLog.entity_type == "Loan", AuditLog.entity_id == str(loan.id)),
            and_(AuditLog.entity_type == "LoanApplication", AuditLog.entity_id == str(loan.application_id)),
            and_(AuditLog.entity_type == "PaymentTransaction", AuditLog.entity_id.in_(payment_ids or ["-"])),
        ))
        .order_by(AuditLog.id.asc())
        .all()
    )
    return {
        "loan_id": loan.id,
        "application_id": loan.application_id,
        "customer": _customer(loan.user_id),
        "status": loan.status.value,
        "terms": {
            "prime_category": snap.prime_category,
            "principal": _f(snap.principal),
            "interest_rate": float(snap.interest_rate),
            "interest_amount": _f(snap.interest_amount),
            "original_total_due": _f(snap.original_total_due),
            "term_days": snap.term_days,
            "disbursed_at": _iso(snap.disbursed_at),
            "disbursed_local_date": snap.disbursed_local_date.isoformat(),
            "due_date": snap.due_date.isoformat(),
            "pricing_version_id": snap.pricing_version_id,
            "penalty_policy_version_id": snap.penalty_policy_version_id,
        },
        "balance": {
            "original_obligation": _f(t["original_obligation"]),
            "penalties": _f(t["penalties"]),
            "verified_repayments": _f(t["verified_repayments"]),
            "outstanding": _f(t["outstanding"]),
            "days_overdue": ledger.days_overdue(snap.due_date, t["outstanding"]),
        },
        "disbursement": None if disb is None else {
            "id": disb.id,
            "method": disb.method.value,
            "amount": _f(disb.amount),
            "reference": disb.method_reference,
            "destination_masked": disb.destination_masked,
            "evidence_document_id": disb.evidence_document_id,
            "disbursed_at": _iso(disb.disbursed_at),
            "recorded_at": _iso(disb.recorded_at),
            "recorded_by": disb.recorded_by,
            "recorded_by_name": disb.recorded_by_admin.full_name if disb.recorded_by_admin else None,
            "note": disb.note,
        },
        "closure": None if closure is None else {
            "closed_at": _iso(closure.closed_at),
            "closure_reason": closure.closure_reason.value,
            "closed_by": closure.closed_by,
            "closing_payment_transaction_id": closure.closing_payment_transaction_id,
            "total_verified_paid": _f(closure.total_verified_paid),
            "total_penalties": _f(closure.total_penalties),
            "outstanding_at_closure": _f(closure.outstanding_at_closure),
            "final_payment_date": _iso(closure.final_payment_date),
            "repayment_duration_days": closure.repayment_duration_days,
            "timeliness": closure.timeliness.value if closure.timeliness else None,
        },
        "reapplication": _reapplication(loan),
        "ledger": [ledger.serialize_entry(e) for e in loan.ledger_entries],
        "payments": [payment_processing._serialize_transaction(p)["transaction"] for p in payments],
        "audit_history": [
            {
                "id": r.id, "action": r.action, "actor_id": r.actor_id, "actor_role": r.actor_role,
                "entity_type": r.entity_type, "entity_id": r.entity_id,
                "details": r.details, "created_at": _iso(r.created_at),
            }
            for r in audit_rows
        ],
    }


# ================================================================== analytics
def _window(date_from, date_to) -> tuple[date, date, datetime, datetime]:
    today = ledger.today_local()
    try:
        end = date.fromisoformat(date_to) if date_to else today
        start = date.fromisoformat(date_from) if date_from else end - timedelta(days=29)
    except ValueError:
        raise ServiceError("from and to must be dates (YYYY-MM-DD).")
    if start > end:
        raise ServiceError("from must be on or before to.")
    tz = prime_pricing.LOCAL_TZ
    return (start, end, datetime.combine(start, time.min, tz).astimezone(timezone.utc),
            datetime.combine(end + timedelta(days=1), time.min, tz).astimezone(timezone.utc))


def _metric(value, definition):
    return {"value": value, "definition": definition}


def _hours(a, b):
    if a is None or b is None:
        return None
    a = a if a.tzinfo else a.replace(tzinfo=timezone.utc)
    b = b if b.tzinfo else b.replace(tzinfo=timezone.utc)
    return (b - a).total_seconds() / 3600


def _duration(values):
    values = [v for v in values if v is not None]
    if not values:
        return {"count": 0, "average_hours": None, "median_hours": None}
    return {"count": len(values), "average_hours": round(sum(values) / len(values), 1),
            "median_hours": round(median(values), 1)}


def analytics(date_from=None, date_to=None) -> dict:
    start, end, t0, t1 = _window(date_from, date_to)
    today = ledger.today_local()
    in_window = lambda col: and_(col >= t0, col < t1)  # noqa: E731

    received = LoanApplication.query.filter(in_window(LoanApplication.submitted_at)).all()
    decided = LoanApplication.query.filter(in_window(LoanApplication.decided_at)).all()
    approved = [a for a in decided if a.status in (S.APPROVED, S.AWAITING_DISBURSEMENT, S.DISBURSED)]
    rejected = [a for a in decided if a.status == S.REJECTED]
    n_decided = len(approved) + len(rejected)

    snaps = LoanTermsSnapshot.query.filter(in_window(LoanTermsSnapshot.disbursed_at)).all()
    principal = sum((Decimal(s.principal) for s in snaps), Decimal("0"))
    interest = sum((Decimal(s.interest_amount) for s in snaps), Decimal("0"))
    expected = sum((Decimal(s.original_total_due) for s in snaps), Decimal("0"))

    repaid = -ledger.sum_where(
        LoanLedgerEntry.entry_type == LedgerEntryType.VERIFIED_REPAYMENT,
        LoanLedgerEntry.effective_date.between(start, end),
    )
    penalties = ledger.sum_where(
        LoanLedgerEntry.entry_type == LedgerEntryType.PENALTY,
        LoanLedgerEntry.effective_date.between(start, end),
    )

    bal = ledger.balance_subquery()
    open_rows = (
        db.session.query(LoanTermsSnapshot, bal.c.balance)
        .join(Loan, Loan.id == LoanTermsSnapshot.loan_id)
        .outerjoin(bal, bal.c.loan_id == Loan.id)
        .filter(Loan.status.in_(_OPEN_LOAN))
        .all()
    )
    exposure = sum((Decimal(s.principal) for s, _ in open_rows), Decimal("0"))
    outstanding = sum((Decimal(b or 0) for _, b in open_rows), Decimal("0"))
    overdue = [(s, Decimal(b or 0)) for s, b in open_rows if (b or 0) > 0 and s.due_date < today]

    def by_category(rows, amount):
        out = {}
        for r in rows:
            key = r.prime_category or "not PRIME"
            c = out.setdefault(key, {"count": 0, "amount": 0.0})
            c["count"] += 1
            c["amount"] = round(c["amount"] + float(amount(r)), 2)
        return dict(sorted(out.items()))

    disbursed_apps = {s.application_id: s for s in snaps}
    apps = {a.id: a for a in LoanApplication.query.filter(
        LoanApplication.id.in_(list(disbursed_apps) or [-1])).all()}

    return {
        "window": {"from": start.isoformat(), "to": end.isoformat(), "timezone": "Pacific/Port_Moresby",
                   "note": "Flow metrics count events inside the window; stock metrics are as of now."},
        "as_of": today.isoformat(),
        "currency": "PGK",
        "applications": {
            "received": _metric(len(received), "Applications submitted in the window."),
            "approved": _metric(len(approved), "Applications given a final APPROVE decision in the window."),
            "rejected": _metric(len(rejected), "Applications rejected in the window (final decision or early exit)."),
            "approval_rate": _metric(round(len(approved) / n_decided, 4) if n_decided else None,
                                     "approved / (approved + rejected), decisions in the window; null if none."),
            "rejection_rate": _metric(round(len(rejected) / n_decided, 4) if n_decided else None,
                                      "rejected / (approved + rejected), decisions in the window; null if none."),
            "by_prime_category": _metric(by_category(received, lambda a: a.amount_requested),
                                         "Applications submitted in the window by PRIME category: count and total amount requested."),
        },
        "disbursements": {
            "loans_disbursed": _metric(len(snaps), "Loans disbursed in the window."),
            "principal_disbursed": _metric(_f(principal), "Sum of principal paid out on loans disbursed in the window (no interest)."),
            "interest_contracted": _metric(_f(interest), "Sum of the original flat interest on loans disbursed in the window."),
            "expected_repayment": _metric(_f(expected), "Principal + original interest owed on loans disbursed in the window, excluding penalties."),
            "by_prime_category": _metric(by_category([apps[s.application_id] for s in snaps if s.application_id in apps],
                                                     lambda a: disbursed_apps[a.id].principal),
                                         "Loans disbursed in the window by PRIME category: count and principal."),
        },
        "repayments": {
            "verified_repayments": _metric(_f(repaid), "Cash received: verified repayments whose payment date (the date the customer paid) is in the window."),
            "penalties_charged": _metric(_f(penalties), "Late-payment penalties added to loans with a penalty date in the window."),
        },
        "portfolio": {
            "active_loans": _metric(len(open_rows), "Loans currently active or overdue (as of now)."),
            "active_principal_exposure": _metric(_f(exposure), "Original principal of loans currently active or overdue (as of now), regardless of payments."),
            "outstanding_value": _metric(_f(outstanding), "What is still owed on loans currently active or overdue: original principal + interest + penalties - verified repayments, from the ledger (as of now)."),
            "overdue_loans": _metric(len(overdue), "Active/overdue loans past their due date with a balance still owing (as of now)."),
            "overdue_value": _metric(_f(sum((b for _, b in overdue), Decimal("0"))), "Outstanding balance on those overdue loans (as of now)."),
        },
        "processing_times": {
            "submitted_to_decided": _metric(_duration([_hours(a.submitted_at, a.decided_at) for a in decided]),
                                            "Hours from submission to the final decision, for decisions in the window."),
            "approved_to_disbursed": _metric(_duration([_hours(apps[s.application_id].decided_at, s.disbursed_at) for s in snaps if s.application_id in apps]),
                                             "Hours from approval to disbursement, for loans disbursed in the window."),
            "submitted_to_disbursed": _metric(_duration([_hours(apps[s.application_id].submitted_at, s.disbursed_at) for s in snaps if s.application_id in apps]),
                                              "Hours from submission to disbursement, for loans disbursed in the window."),
        },
    }
