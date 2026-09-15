"""One-off staff account seeding (loan_officer / admin).

Public registration (``POST /api/auth/register``) always creates a `customer`
account by design - see BACKEND.md -> Authentication flow. Staff accounts
(`loan_officer`, `admin`) have no HTTP provisioning path on purpose, so this
script is the only way to create one. It is a standalone CLI tool, NOT a route
- nothing here is reachable over HTTP.

What it does:
    1. Hashes a securely-generated (or operator-supplied) temporary password
       with the exact same function the real register endpoint uses
       (`app.services.security.hash_password` - pbkdf2:sha256).
    2. Inserts the `User` row directly with `totp_enabled=False`, so the
       account lands in exactly the state a fresh customer registration would:
       able to log in, immediately told MFA setup is required.
    3. Writes one audit-log row (`staff_account_created`) so the seed action
       itself is traceable, same ledger as everything else.

It does NOT set up MFA. The staff member does that themselves on first login,
through the normal public flow (POST /api/auth/mfa/setup then
/mfa/verify-setup) - see STAFF_ONBOARDING.md for the exact steps to hand them.

Usage (local):
    python scripts/seed_staff.py --email jane@primesvault.pg \\
        --full-name "Jane Officer" --role loan_officer --created-by "you@primesvault.pg"

Usage (against Railway production, without deploying anything):
    railway run --service <service> python scripts/seed_staff.py \\
        --email jane@primesvault.pg --full-name "Jane Officer" \\
        --role loan_officer --created-by "you@primesvault.pg"

Add --yes to skip the confirmation prompt (for scripted/non-interactive use).
Omit --password to have one generated for you - it is printed ONCE and never
stored or logged anywhere in plaintext.
"""

import argparse
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

import os

from app import create_app
from app.extensions import db
from app.models import User
from app.models.enums import UserRole
from app.services import audit, security

_ASSIGNABLE_ROLES = {UserRole.LOAN_OFFICER.value, UserRole.ADMIN.value}


def _mask_db_url(url: str | None) -> str:
    if not url:
        return "(unset - falls back to a local throwaway SQLite file)"
    if "@" in url and "://" in url:
        scheme, rest = url.split("://", 1)
        creds, host = rest.split("@", 1)
        user = creds.split(":", 1)[0]
        return f"{scheme}://{user}:***@{host}"
    return url


def _generate_temp_password() -> str:
    # url-safe, ~20 chars, well over the app's 8-char minimum. Not a passphrase
    # meant to be memorized - it's handed off once and paired with mandatory MFA.
    return secrets.token_urlsafe(15)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Seed a loan_officer or admin User row directly (no HTTP endpoint exists for this)."
    )
    p.add_argument("--email", required=True)
    p.add_argument("--full-name", required=True)
    p.add_argument(
        "--role",
        required=True,
        choices=sorted(_ASSIGNABLE_ROLES),
        help="'customer' is deliberately not an option - use POST /api/auth/register for that.",
    )
    p.add_argument("--phone-number", default=None)
    p.add_argument(
        "--created-by",
        required=True,
        help="Who is running this (name or email) - recorded in the audit log details, since there's no authenticated admin actor in a CLI context.",
    )
    p.add_argument(
        "--password",
        default=None,
        help="Temporary password to set. Omit to auto-generate a secure one (recommended).",
    )
    p.add_argument(
        "--yes", action="store_true", help="Skip the confirmation prompt."
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    email = args.email.strip().lower()
    full_name = args.full_name.strip()
    role = args.role
    created_by = args.created_by.strip()

    if len(email) < 3 or "@" not in email:
        print(f"FAIL: '{email}' doesn't look like a valid email.")
        return 1

    temp_password = args.password or _generate_temp_password()
    if len(temp_password) < 8:
        print("FAIL: --password must be at least 8 characters (same rule as /api/auth/register).")
        return 1

    print(f"Target database: {_mask_db_url(os.environ.get('DATABASE_URL'))}")
    print(f"About to create: {email}  |  {full_name}  |  role={role}  |  created_by={created_by}")
    if not args.yes:
        confirm = input("Type 'yes' to continue: ").strip().lower()
        if confirm != "yes":
            print("Aborted - no changes made.")
            return 1

    app = create_app()
    with app.app_context():
        if User.query.filter_by(email=email).first():
            print(f"FAIL: '{email}' is already registered. Not touching the existing row.")
            return 1

        user = User(
            email=email,
            password_hash=security.hash_password(temp_password),
            full_name=full_name,
            phone_number=args.phone_number,
            role=UserRole(role),
            is_active=True,
            totp_enabled=False,  # first login will be told mfa_required: "setup", same as a fresh customer
        )
        db.session.add(user)
        db.session.flush()  # assigns user.id without committing yet

        audit.record(
            "staff_account_created",
            actor_id=None,  # no authenticated actor in a CLI context - see created_by below
            entity_type="User",
            entity_id=user.id,
            details={"email": email, "role": role, "created_by": created_by},
            commit=False,
        )
        db.session.commit()

        print()
        print("=" * 70)
        print("STAFF ACCOUNT CREATED")
        print(f"  user_id        : {user.id}")
        print(f"  email          : {email}")
        print(f"  role           : {role}")
        print(f"  temp password  : {temp_password}")
        print("=" * 70)
        print(
            "Hand the temp password to this person over a secure, private channel\n"
            "(password manager share, verbal call, etc.) - never plain email/chat.\n"
            "They complete enrollment themselves via the normal public flow:\n"
            "  1. POST /api/auth/login            (email + temp password)\n"
            "     -> 403 {mfa_required: 'setup', mfa_setup_token: ...}\n"
            "  2. POST /api/auth/mfa/setup         (Bearer <mfa_setup_token>)\n"
            "  3. POST /api/auth/mfa/verify-setup  (Bearer <mfa_setup_token>, {code})\n"
            "     -> backup codes, shown once - they must store these\n"
            "  4. POST /api/auth/login again -> POST /api/auth/mfa/verify-login\n"
            "     -> real access + refresh tokens, role claim = '" + role + "'\n"
            "See STAFF_ONBOARDING.md for the full walkthrough."
        )
        return 0


if __name__ == "__main__":
    sys.exit(main())
