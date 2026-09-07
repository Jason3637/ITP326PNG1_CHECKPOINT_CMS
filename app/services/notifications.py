"""Notifications Router - transactional email via Zoho Mail SMTP.

================================  SCOPE  ======================================
This module is for TRANSACTIONAL NOTIFICATIONS ONLY (application received, loan
approved/rejected, payment received, repayment due soon).

It must NEVER be used to deliver authentication factors - no TOTP codes, no
password-reset tokens, no magic links, no OTPs of any kind. Per the architecture
diagram's explicit note, MFA is app-based TOTP (app/services/mfa.py) and the
email channel is deliberately kept out of the authentication path. There is no
generic "send code" function here, and there should never be one.
==============================================================================

Sending is best-effort: a failure is logged and swallowed so it can never break
or roll back the business transaction that triggered it. Disabled by default
(`NOTIFICATIONS_ENABLED`); when disabled, events are logged, not sent.
"""

from __future__ import annotations

import smtplib
from datetime import date
from decimal import Decimal
from email.message import EmailMessage
from email.utils import formataddr

from flask import current_app


def _config():
    c = current_app.config
    return {
        "enabled": c.get("NOTIFICATIONS_ENABLED", False),
        "host": c.get("SMTP_HOST"),
        "port": c.get("SMTP_PORT"),
        "use_tls": c.get("SMTP_USE_TLS", True),
        "username": c.get("SMTP_USERNAME"),
        "password": c.get("SMTP_PASSWORD"),
        "from_addr": c.get("MAIL_FROM") or c.get("SMTP_USERNAME"),
        "from_name": c.get("MAIL_FROM_NAME", "Prime's Vault"),
        "timeout": c.get("MAIL_TIMEOUT_SECONDS", 10),
    }


def send_email(to_address: str, subject: str, body: str) -> dict:
    """Low-level send. Returns a result dict; never raises."""
    cfg = _config()
    log = current_app.logger

    if not to_address:
        return {"sent": False, "reason": "no_recipient"}
    if not cfg["enabled"]:
        log.info("notification (disabled, not sent) -> %s: %s", to_address, subject)
        return {"sent": False, "reason": "disabled"}
    if not (cfg["host"] and cfg["username"] and cfg["password"] and cfg["from_addr"]):
        log.warning("notification skipped -> SMTP not fully configured")
        return {"sent": False, "reason": "smtp_not_configured"}

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((cfg["from_name"], cfg["from_addr"]))
    msg["To"] = to_address
    msg.set_content(body)

    try:
        with smtplib.SMTP(cfg["host"], cfg["port"], timeout=cfg["timeout"]) as server:
            if cfg["use_tls"]:
                server.starttls()
            server.login(cfg["username"], cfg["password"])
            server.send_message(msg)
        log.info("notification sent -> %s: %s", to_address, subject)
        return {"sent": True, "reason": None}
    except Exception as exc:  # never propagate
        log.error("notification FAILED -> %s: %s (%s)", to_address, subject, exc)
        return {"sent": False, "reason": f"error: {exc}"}


def _money(value) -> str:
    return f"{Decimal(str(value)):,.2f}"


# --------------------------------------------------------------- event helpers
def notify_application_received(application) -> dict:
    user = application.applicant
    return send_email(
        user.email,
        "We've received your loan application",
        f"Hi {user.full_name},\n\n"
        f"Your application for {_money(application.amount_requested)} over "
        f"{application.term_months} months has been received and is now "
        f"{application.status.value.replace('_', ' ')}.\n\n"
        f"We'll email you again once a loan officer has reviewed it.\n\n"
        f"- Prime's Vault",
    )


def notify_loan_approved(loan) -> dict:
    user = loan.borrower
    first = min((r.due_date for r in loan.repayment_schedule), default=None)
    return send_email(
        user.email,
        "Your loan has been approved",
        f"Hi {user.full_name},\n\n"
        f"Good news - your loan of {_money(loan.principal_amount)} has been approved "
        f"and disbursed.\n\n"
        f"  Repayable in total: {_money(loan.total_repayable)}\n"
        f"  Installment:        {_money(loan.monthly_payment)}\n"
        f"  Term:               {loan.term_months} months\n"
        f"  First payment due:  {first.isoformat() if first else 'see your schedule'}\n\n"
        f"You can view your full repayment schedule in your account.\n\n"
        f"- Prime's Vault",
    )


def notify_loan_rejected(application) -> dict:
    user = application.applicant
    return send_email(
        user.email,
        "Update on your loan application",
        f"Hi {user.full_name},\n\n"
        f"After review, we're unable to approve your application for "
        f"{_money(application.amount_requested)} at this time.\n\n"
        f"You're welcome to contact us or apply again in the future.\n\n"
        f"- Prime's Vault",
    )


def notify_payment_received(transaction, schedule) -> dict:
    loan = schedule.loan
    user = loan.borrower
    remaining = sum(
        (Decimal(r.amount_due) - Decimal(r.amount_paid))
        for r in loan.repayment_schedule
        if r.status.value != "paid"
    )
    return send_email(
        user.email,
        "Payment received",
        f"Hi {user.full_name},\n\n"
        f"We've received your payment of {_money(transaction.amount)} "
        f"({transaction.payment_method}) toward installment "
        f"#{schedule.installment_number}.\n\n"
        f"  Installment status: {schedule.status.value}\n"
        f"  Balance remaining:  {_money(max(Decimal('0'), remaining))}\n\n"
        f"Thank you.\n\n- Prime's Vault",
    )


def notify_repayment_due_soon(schedule) -> dict:
    loan = schedule.loan
    user = loan.borrower
    days = (schedule.due_date - date.today()).days
    return send_email(
        user.email,
        "Repayment due soon",
        f"Hi {user.full_name},\n\n"
        f"This is a reminder that installment #{schedule.installment_number} of "
        f"{_money(schedule.amount_due)} is due on {schedule.due_date.isoformat()} "
        f"({days} day(s) from now).\n\n"
        f"- Prime's Vault",
    )
