"""Daily maintenance: overdue installments, late penalties and loan status,
expired customer verifications, then "repayment due soon" reminder emails.

Run on a schedule (Railway Cron Schedule, or any scheduler), once daily:

    python scripts/send_due_reminders.py

Steps (the logic lives in app/services/repayments_scheduler.py,
penalties.py and customer_verification.py - this script is the CLI
entrypoint + a print summary). Dates are Port Moresby dates.

  0. penalties.run() - each penalty tier now due becomes a ledger entry
     (one per loan per tier, enforced by the database, so a second run the
     same day adds nothing), and each loan is set ACTIVE/OVERDUE from its
     snapshot due date and ledger balance. Runs after step 1 below.

  1. `flip_overdue_installments()` - proactively transitions any RepaymentSchedule
     row whose due date has passed with no full payment to `overdue`. This is
     additive: the on-read overdue computation in reporting.py/accounts.py
     stays in place as a safety net (see that function's docstring) in case
     this job is delayed or skipped for a day.
  2. `send_due_soon_reminders()` - finds active-loan installments whose due
     date is within REPAYMENT_REMINDER_LEAD_DAYS and not yet paid, and emails
     each borrower via the Notifications Router. Actually sending requires
     NOTIFICATIONS_ENABLED=true and SMTP configured; when it's not, this still
     runs (and audits every attempt) but emails are logged, not sent.

Every transition and every reminder attempt is written to AuditLog
(`repayment_marked_overdue`, `repayment_reminder_sent` /
`repayment_reminder_not_sent`), same ledger as manual actions.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

from app import create_app
from app.extensions import db
from app.services import customer_verification, penalties, repayments_scheduler


def main() -> int:
    app = create_app()
    with app.app_context():
        overdue = repayments_scheduler.flip_overdue_installments()
        print(f"Overdue sweep: {len(overdue)} installment(s) newly marked overdue.")
        for row in overdue:
            print(f"  installment {row.id} (loan {row.loan_id}, was due {row.due_date})")

        summary = penalties.run()
        print(
            f"Penalties ({summary['as_of']}): {len(summary['penalties_applied'])} applied, "
            f"{len(summary['held_pending_verification'])} held for verification, "
            f"{len(summary['status_changes'])} loan status change(s), "
            f"{summary['loans_checked']} loan(s) checked."
        )
        for p in summary["penalties_applied"]:
            print(f"  loan {p['loan_id']}: tier {p['tier']} +K{p['amount']:,.2f}")

        expired = customer_verification.expire_due()
        db.session.commit()
        print(f"Customer verifications expired: {len(expired)}.")

        results = repayments_scheduler.send_due_soon_reminders()
        sent = sum(1 for r in results if r["outcome"].get("sent"))
        print(f"\nDue-soon reminders: {len(results)} matched, {sent} sent.")
        for r in results:
            row = r["row"]
            print(
                f"  installment {row.id} (loan {row.loan_id}, due {row.due_date}) -> {r['outcome']}"
            )

        return 0


if __name__ == "__main__":
    sys.exit(main())
