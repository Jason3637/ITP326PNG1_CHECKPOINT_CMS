"""Send "repayment due soon" reminder emails.

Run on a schedule (cron / Task Scheduler / a worker), e.g. daily:

    python scripts/send_due_reminders.py

Finds active-loan installments whose due date is within
REPAYMENT_REMINDER_LEAD_DAYS and not yet paid, and emails each borrower via the
Notifications Router. Does nothing unless NOTIFICATIONS_ENABLED is set.
"""

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

from app import create_app
from app.extensions import db
from app.models import RepaymentSchedule
from app.models.enums import LoanStatus, RepaymentStatus
from app.services import notifications


def main() -> int:
    app = create_app()
    with app.app_context():
        lead = app.config["REPAYMENT_REMINDER_LEAD_DAYS"]
        window_end = date.today() + timedelta(days=lead)

        rows = (
            RepaymentSchedule.query.join(RepaymentSchedule.loan)
            .filter(
                RepaymentSchedule.status != RepaymentStatus.PAID,
                RepaymentSchedule.due_date >= date.today(),
                RepaymentSchedule.due_date <= window_end,
            )
            .all()
        )
        rows = [r for r in rows if r.loan.status == LoanStatus.ACTIVE]

        sent = 0
        for r in rows:
            result = notifications.notify_repayment_due_soon(r)
            sent += 1 if result.get("sent") else 0
            print(
                f"installment {r.id} (loan {r.loan_id}, due {r.due_date}) -> {result}"
            )

        print(f"\n{len(rows)} reminder(s) matched, {sent} sent.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
