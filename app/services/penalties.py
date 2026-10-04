"""Late-payment penalties - the policy, applied as ledger entries.

The policy is data, not arithmetic in the scheduler: each loan carries the
penalty-policy version locked when its customer applied (its terms
snapshot). Version 1:

    tier 1  7 days late   +25%  of the loan's ORIGINAL interest
    tier 2  14 days late  +100% of the ORIGINAL interest   (cumulative; nothing after)

For a K500 PRIME 2 loan (K200 interest): +K50 at one week, a further +K200
at two weeks - never a percentage of whatever is outstanding.

When a tier applies. With due date D and a tier of N days, the tier's date is
T = D + N. The tier applies once today (Port Moresby) is on or after T and
the ORIGINAL obligation (principal + interest) was not covered by verified
repayments the customer made before T - judged by the date they paid, not
when an admin verified it. So paying on day 7 after the due date is a week
late and gets tier 1; days 1-6 late carry no penalty. An unpaid penalty
never triggers the next tier by itself.

If a repayment the customer reported but an admin hasn't verified yet would
cover the original obligation before T, the tier is held back - the
customer isn't penalised for the admin's delay. The job looks again on its
next run, and applies the tier only if that report is rejected.

Idempotency: the ledger's unique index on (loan_id, penalty_tier) for
penalty entries makes a second entry for the same tier impossible, however
often the job runs. Each entry is written in a savepoint, so a conflict
skips that one entry and nothing else.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import (
    Loan,
    LoanLedgerEntry,
    LoanTermsSnapshot,
    PaymentTransaction,
    ScheduledJobRun,
)
from app.models.enums import (
    LedgerActorKind,
    LedgerEntryType,
    LoanStatus,
    PaymentStatus,
)

from . import audit, ledger

JOB_NAME = "overdue_and_penalties"
_CENTS = Decimal("0.01")
_PENDING = (PaymentStatus.REPORTED, PaymentStatus.VERIFICATION_PENDING)


def _repaid_before(loan_id: int, before: date, statuses) -> Decimal:
    """Sum of the customer's payments dated before `before`, in `statuses`
    (verified ones from the ledger; pending ones from their reports)."""
    if statuses == "verified":
        value = (
            db.session.query(func.coalesce(func.sum(-LoanLedgerEntry.amount), 0))
            .filter(
                LoanLedgerEntry.loan_id == loan_id,
                LoanLedgerEntry.entry_type == LedgerEntryType.VERIFIED_REPAYMENT,
                LoanLedgerEntry.effective_date < before,
            )
            .scalar()
        )
    else:
        value = (
            db.session.query(func.coalesce(func.sum(PaymentTransaction.amount), 0))
            .filter(
                PaymentTransaction.loan_id == loan_id,
                PaymentTransaction.status.in_(_PENDING),
                PaymentTransaction.payment_date < before,
            )
            .scalar()
        )
    return Decimal(value).quantize(_CENTS)


def tier_decision(snap: LoanTermsSnapshot, tier, as_of: date) -> str:
    """'not_yet' | 'paid_in_time' | 'held_pending_verification' | 'applies'"""
    tier_date = snap.due_date + timedelta(days=tier.days_late)
    if as_of < tier_date:
        return "not_yet"
    original = Decimal(snap.original_total_due)
    verified = _repaid_before(snap.loan_id, tier_date, "verified")
    if verified >= original:
        return "paid_in_time"
    if verified + _repaid_before(snap.loan_id, tier_date, "pending") >= original:
        return "held_pending_verification"
    return "applies"


def assess_loan(loan: Loan, as_of: date, job_run: ScheduledJobRun) -> list[dict]:
    """Apply every tier that is now due and not yet on the ledger. Returns
    one outcome dict per tier considered."""
    snap = loan.terms_snapshot
    if snap is None:
        return []
    already = {
        t for (t,) in db.session.query(LoanLedgerEntry.penalty_tier).filter(
            LoanLedgerEntry.loan_id == loan.id,
            LoanLedgerEntry.entry_type == LedgerEntryType.PENALTY,
        )
    }
    outcomes = []
    for tier in sorted(snap.penalty_policy_version.tiers, key=lambda t: t.tier):
        if tier.tier in already:
            outcomes.append({"tier": tier.tier, "outcome": "already_applied"})
            continue
        decision = tier_decision(snap, tier, as_of)
        if decision != "applies":
            outcomes.append({"tier": tier.tier, "outcome": decision})
            continue
        amount = (Decimal(tier.pct_of_original_interest) * Decimal(snap.interest_amount)).quantize(_CENTS)
        tier_date = snap.due_date + timedelta(days=tier.days_late)
        try:
            with db.session.begin_nested():  # a conflict undoes only this entry
                entry = LoanLedgerEntry(
                    loan_id=loan.id,
                    entry_type=LedgerEntryType.PENALTY,
                    amount=amount,
                    effective_date=tier_date,
                    created_by=None,
                    created_by_kind=LedgerActorKind.SYSTEM,
                    penalty_tier=tier.tier,
                    penalty_policy_version_id=snap.penalty_policy_version_id,
                    job_run_id=job_run.id,
                    note=(
                        f"{tier.days_late} days late: {tier.pct_of_original_interest * 100:.0f}% of "
                        f"the original interest K{Decimal(snap.interest_amount):,.2f}"
                    ),
                )
                db.session.add(entry)
                db.session.flush()
        except IntegrityError:
            outcomes.append({"tier": tier.tier, "outcome": "already_applied"})
            continue
        audit.record(
            "loan_penalty_applied",
            actor_id=None,
            entity_type="Loan",
            entity_id=loan.id,
            details={
                "ledger_entry_id": entry.id,
                "tier": tier.tier,
                "days_late": tier.days_late,
                "pct_of_original_interest": float(tier.pct_of_original_interest),
                "original_interest": float(snap.interest_amount),
                "amount": float(amount),
                "tier_date": tier_date.isoformat(),
                "due_date": snap.due_date.isoformat(),
                "job_run_id": job_run.id,
            },
            commit=False,
        )
        outcomes.append({"tier": tier.tier, "outcome": "applied", "amount": float(amount),
                         "ledger_entry_id": entry.id})
    return outcomes


def is_overdue(loan: Loan, today: date | None = None) -> bool:
    """Past its due date (Port Moresby) with the ledger still owing. Loans
    without a terms snapshot (the old amortized product) fall back to their
    installments."""
    if loan.status not in (LoanStatus.ACTIVE, LoanStatus.OVERDUE):
        return False
    today = today or ledger.today_local()
    snap = loan.terms_snapshot
    if snap is None:
        from app.models.enums import RepaymentStatus

        return any(r.status != RepaymentStatus.PAID and r.due_date < today for r in loan.repayment_schedule)
    return snap.due_date < today and ledger.balance(loan.id) > 0


def sync_status(loan: Loan, today: date | None = None) -> bool:
    """ACTIVE <-> OVERDUE from is_overdue(). Returns True if it changed."""
    if loan.status not in (LoanStatus.ACTIVE, LoanStatus.OVERDUE):
        return False
    new = LoanStatus.OVERDUE if is_overdue(loan, today) else LoanStatus.ACTIVE
    if new == loan.status:
        return False
    old = loan.status
    loan.status = new
    audit.record(
        "loan_status_changed",
        actor_id=None,
        entity_type="Loan",
        entity_id=loan.id,
        details={"from": old.value, "to": new.value, "reason": "overdue sweep"},
        commit=False,
    )
    return True


def run(as_of: date | None = None) -> dict:
    """The daily sweep over every active/overdue loan: penalties first, then
    the ACTIVE/OVERDUE status. One ScheduledJobRun row per run (every penalty
    entry points at it); one commit at the end."""
    as_of = as_of or ledger.today_local()
    job_run = ScheduledJobRun(job_name=JOB_NAME)
    db.session.add(job_run)
    db.session.flush()

    applied, held, status_changes = [], [], []
    loans = (
        Loan.query.filter(Loan.status.in_((LoanStatus.ACTIVE, LoanStatus.OVERDUE)))
        .order_by(Loan.id)
        .all()
    )
    for loan in loans:
        for o in assess_loan(loan, as_of, job_run):
            if o["outcome"] == "applied":
                applied.append({"loan_id": loan.id, **o})
            elif o["outcome"] == "held_pending_verification":
                held.append({"loan_id": loan.id, "tier": o["tier"]})
        before = loan.status
        if sync_status(loan, as_of):
            status_changes.append({"loan_id": loan.id, "from": before.value, "to": loan.status.value})

    job_run.finished_at = datetime.now(timezone.utc)
    job_run.summary = {
        "as_of": as_of.isoformat(),
        "loans_checked": len(loans),
        "penalties_applied": applied,
        "held_pending_verification": held,
        "status_changes": status_changes,
    }
    db.session.commit()
    return job_run.summary


def assess_before_verification(loan: Loan) -> list[dict]:
    """Called inside payment verification, before the balance is checked: a
    tier already due must be on the ledger first, or verifying a late full
    payment would close the loan without the penalty the policy charges
    (paying on day 7 is a week late). The payment being verified still
    counts as pending here, so it holds back any tier it would have avoided.
    Doesn't commit - it's part of the verification transaction."""
    job_run = ScheduledJobRun(job_name="penalty_check_on_verification")
    db.session.add(job_run)
    db.session.flush()
    outcomes = assess_loan(loan, ledger.today_local(), job_run)
    job_run.finished_at = datetime.now(timezone.utc)
    job_run.summary = {"loan_id": loan.id, "outcomes": outcomes}
    return outcomes


# ---------------------------------------------------------- read side (customer-safe)
def policy_text(version=None) -> str:
    """Plain-language statement of a penalty policy version (default: the
    current one), e.g. "25% of the loan's original interest at 7 days late,
    then a further 100% at 14 days late." """
    from . import pricing_policy

    version = version or pricing_policy.current_penalty_policy()
    parts = []
    for i, t in enumerate(sorted(version.tiers, key=lambda t: t.tier)):
        pct = f"{Decimal(t.pct_of_original_interest) * 100:.0f}%"
        prefix = "" if i == 0 else "then a further "
        parts.append(f"{prefix}{pct} of the loan's original interest at {t.days_late} days late")
    return ("Late payments add a penalty: " + ", ".join(parts) + ". Nothing is added after that.") if parts else ""


def describe(loan_id: int) -> list[dict]:
    """The penalties charged on one loan, oldest first, each with the date it
    applied and a reason a customer can read."""
    from app.models import PenaltyPolicyTier

    rows = (
        db.session.query(LoanLedgerEntry, PenaltyPolicyTier.days_late)
        .outerjoin(PenaltyPolicyTier, (PenaltyPolicyTier.version_id == LoanLedgerEntry.penalty_policy_version_id)
                   & (PenaltyPolicyTier.tier == LoanLedgerEntry.penalty_tier))
        .filter(LoanLedgerEntry.loan_id == loan_id, LoanLedgerEntry.entry_type == LedgerEntryType.PENALTY)
        .order_by(LoanLedgerEntry.penalty_tier)
        .all()
    )
    return [
        {
            "tier": e.penalty_tier,
            "amount": float(Decimal(e.amount)),
            "applied_on": e.effective_date.isoformat(),
            "days_late": days_late,
            "reason": e.note or (f"Late payment ({days_late} days late)" if days_late is not None else "Late payment"),
        }
        for e, days_late in rows
    ]
