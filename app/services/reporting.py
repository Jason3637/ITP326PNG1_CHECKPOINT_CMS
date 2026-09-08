"""Reporting Tool - role-aware dashboard aggregates.

Response shape is built for a Chart.js frontend: every chart block is
``{"labels": [...], "series": [{"name": ..., "data": [...]}]}`` (single-series
blocks still use that shape), alongside flat ``kpis`` scalars for stat cards and
``tables`` lists for grids.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from flask import current_app

from app.models import Loan, LoanApplication, PaymentTransaction, User
from app.models.enums import (
    LoanApplicationStatus,
    LoanStatus,
    PaymentStatus,
    RepaymentStatus,
    UserRole,
)

_ZERO = Decimal("0")
_STAFF = (UserRole.LOAN_OFFICER, UserRole.ADMIN)


def _f(value) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.01")))


def _chart(labels, data, name="value"):
    return {"labels": list(labels), "series": [{"name": name, "data": list(data)}]}


def _multi_chart(labels, series: dict):
    return {
        "labels": list(labels),
        "series": [{"name": k, "data": list(v)} for k, v in series.items()],
    }


def _is_overdue(row) -> bool:
    return row.status != RepaymentStatus.PAID and row.due_date < date.today()


def _last_months(n: int) -> list[str]:
    today = date.today()
    keys = []
    y, m = today.year, today.month
    for _ in range(n):
        keys.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    return list(reversed(keys))


def _month_key(dt) -> str:
    return f"{dt.year:04d}-{dt.month:02d}"


# ============================================================ entry point
def build_dashboard(user: User) -> dict:
    base = {
        "role": str(user.role),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "currency": current_app.config.get("CURRENCY_CODE", "PGK"),
    }
    if user.role in _STAFF:
        base.update(_portfolio_dashboard())
    else:
        base.update(_customer_dashboard(user))
    return base


# ============================================================ customer view
def _customer_dashboard(user: User) -> dict:
    loans = Loan.query.filter_by(user_id=user.id).order_by(Loan.id.desc()).all()
    active = [l for l in loans if l.status == LoanStatus.ACTIVE]

    all_rows = [r for l in loans for r in l.repayment_schedule]
    active_rows = [r for l in active for r in l.repayment_schedule]

    total_borrowed = sum((Decimal(l.principal_amount) for l in loans), _ZERO)
    repayable_active = sum((Decimal(l.total_repayable) for l in active), _ZERO)
    repaid_active = sum((Decimal(r.amount_paid) for r in active_rows), _ZERO)
    outstanding = max(_ZERO, repayable_active - repaid_active)

    overdue_rows = [r for r in active_rows if _is_overdue(r)]
    overdue_amount = sum(
        (Decimal(r.amount_due) - Decimal(r.amount_paid) for r in overdue_rows), _ZERO
    )

    unpaid = sorted(
        (r for r in active_rows if r.status != RepaymentStatus.PAID),
        key=lambda r: (r.due_date, r.installment_number),
    )
    next_payment = None
    if unpaid:
        nr = unpaid[0]
        next_payment = {
            "loan_id": nr.loan_id,
            "installment_number": nr.installment_number,
            "due_date": nr.due_date.isoformat(),
            "amount_due": _f(Decimal(nr.amount_due) - Decimal(nr.amount_paid)),
            "days_until_due": (nr.due_date - date.today()).days,
        }

    # installments by (effective) status - bar chart
    paid_n = sum(1 for r in active_rows if r.status == RepaymentStatus.PAID)
    overdue_n = len(overdue_rows)
    upcoming_n = len(active_rows) - paid_n - overdue_n

    # projected paydown - line chart over the remaining installments
    running = outstanding
    forecast_labels, forecast_values = [], []
    for r in unpaid:
        running = max(_ZERO, running - (Decimal(r.amount_due) - Decimal(r.amount_paid)))
        forecast_labels.append(r.due_date.isoformat())
        forecast_values.append(_f(running))

    return {
        "kpis": {
            "active_loans": len(active),
            "total_loans": len(loans),
            "total_borrowed": _f(total_borrowed),
            "outstanding_balance": _f(outstanding),
            "amount_repaid": _f(repaid_active),
            "overdue_installments": overdue_n,
            "overdue_amount": _f(overdue_amount),
            "next_payment": next_payment,
        },
        "charts": {
            "repayment_progress": _chart(
                ["Repaid", "Outstanding"], [_f(repaid_active), _f(outstanding)], "amount"
            ),
            "installments_by_status": _chart(
                ["Paid", "Upcoming", "Overdue"], [paid_n, upcoming_n, overdue_n], "installments"
            ),
            "projected_balance": _chart(forecast_labels, forecast_values, "outstanding"),
        },
        "tables": {
            "loans": [
                {
                    "loan_id": l.id,
                    "status": str(l.status),
                    "principal": _f(l.principal_amount),
                    "interest_rate": float(Decimal(l.interest_rate)),
                    "total_repayable": _f(l.total_repayable),
                    "installment_amount": _f(l.monthly_payment),
                    "installments_total": len(l.repayment_schedule),
                    "installments_paid": sum(
                        1 for r in l.repayment_schedule if r.status == RepaymentStatus.PAID
                    ),
                    "disbursed_at": l.disbursed_at.isoformat() if l.disbursed_at else None,
                }
                for l in loans
            ]
        },
    }


# ============================================================ portfolio view
def _portfolio_dashboard() -> dict:
    loans = Loan.query.all()
    applications = LoanApplication.query.all()

    active = [l for l in loans if l.status == LoanStatus.ACTIVE]
    active_ids = {l.id for l in active}

    all_active_rows = [r for l in active for r in l.repayment_schedule]
    outstanding_by_loan: dict[int, Decimal] = {}
    for r in all_active_rows:
        bal = Decimal(r.amount_due) - Decimal(r.amount_paid)
        if bal > 0:
            outstanding_by_loan[r.loan_id] = outstanding_by_loan.get(r.loan_id, _ZERO) + bal
    total_outstanding = sum(outstanding_by_loan.values(), _ZERO)

    overdue_rows = [r for r in all_active_rows if _is_overdue(r)]
    arrears_loan_ids = {r.loan_id for r in overdue_rows}
    at_risk = sum((outstanding_by_loan.get(lid, _ZERO) for lid in arrears_loan_ids), _ZERO)

    total_disbursed = sum((Decimal(l.principal_amount) for l in loans), _ZERO)
    completed_txns = PaymentTransaction.query.filter_by(
        status=PaymentStatus.COMPLETED
    ).all()
    total_collected = sum((Decimal(t.amount) for t in completed_txns), _ZERO)

    pending_review = [
        a
        for a in applications
        if a.status in (LoanApplicationStatus.PENDING, LoanApplicationStatus.UNDER_REVIEW)
    ]

    # ---- charts ----
    loans_by_status = {s: 0 for s in ("active", "completed", "defaulted")}
    for l in loans:
        loans_by_status[l.status.value] += 1

    apps_by_status = {s.value: 0 for s in LoanApplicationStatus}
    for a in applications:
        apps_by_status[a.status.value] += 1

    months = _last_months(6)
    disb = {m: _ZERO for m in months}
    for l in loans:
        if l.disbursed_at and _month_key(l.disbursed_at) in disb:
            disb[_month_key(l.disbursed_at)] += Decimal(l.principal_amount)
    coll = {m: _ZERO for m in months}
    for t in completed_txns:
        key = _month_key(t.paid_at or t.created_at)
        if key in coll:
            coll[key] += Decimal(t.amount)

    # ---- tables ----
    top_overdue = []
    for lid in sorted(
        arrears_loan_ids, key=lambda i: outstanding_by_loan.get(i, _ZERO), reverse=True
    )[:5]:
        rows = [r for r in overdue_rows if r.loan_id == lid]
        oldest = min(r.due_date for r in rows)
        loan = next(l for l in active if l.id == lid)
        top_overdue.append(
            {
                "loan_id": lid,
                "user_id": loan.user_id,
                "overdue_installments": len(rows),
                "overdue_amount": _f(
                    sum((Decimal(r.amount_due) - Decimal(r.amount_paid) for r in rows), _ZERO)
                ),
                "outstanding": _f(outstanding_by_loan.get(lid, _ZERO)),
                "oldest_due_date": oldest.isoformat(),
                "days_overdue": (date.today() - oldest).days,
            }
        )

    pending_table = [
        {
            "application_id": a.id,
            "user_id": a.user_id,
            "amount_requested": _f(a.amount_requested),
            "term_months": a.term_months,
            "status": a.status.value,
            "credit_score": (a.credit_evaluation_result or {}).get("score"),
            "submitted_at": a.submitted_at.isoformat() if a.submitted_at else None,
        }
        for a in sorted(pending_review, key=lambda x: x.submitted_at or datetime.min)[:10]
    ]

    par_pct = float((at_risk / total_outstanding * 100).quantize(Decimal("0.1"))) if total_outstanding else 0.0

    return {
        "kpis": {
            "active_loans": len(active),
            "total_loans": len(loans),
            "applications_pending_review": len(pending_review),
            "total_disbursed": _f(total_disbursed),
            "total_outstanding": _f(total_outstanding),
            "total_collected": _f(total_collected),
            "overdue_installments": len(overdue_rows),
            "loans_in_arrears": len(arrears_loan_ids),
            "portfolio_at_risk_amount": _f(at_risk),
            "portfolio_at_risk_percent": par_pct,
            "average_loan_size": _f(total_disbursed / len(loans)) if loans else 0.0,
        },
        "charts": {
            "loans_by_status": _chart(
                [k.title() for k in loans_by_status], list(loans_by_status.values()), "loans"
            ),
            "applications_by_status": _chart(
                [k.replace("_", " ").title() for k in apps_by_status],
                list(apps_by_status.values()),
                "applications",
            ),
            "disbursements_vs_collections_by_month": _multi_chart(
                months,
                {
                    "Disbursed": [_f(disb[m]) for m in months],
                    "Collected": [_f(coll[m]) for m in months],
                },
            ),
        },
        "tables": {
            "applications_pending_review": pending_table,
            "top_overdue_loans": top_overdue,
        },
    }
