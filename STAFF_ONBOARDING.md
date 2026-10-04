# Staff Onboarding (loan_officer / admin)

Public registration (`POST /api/auth/register`) always creates a `customer`
account — that's deliberate, see BACKEND.md → *Authentication flow*. Staff
accounts (`loan_officer` / `admin`) are created by an administrator:

- **Normally — over the API, by a signed-in admin** (Swagger at `/api/docs`
  until an Administrator UI exists):
  - `POST /api/admin/staff` `{"email", "full_name", "role": "loan_officer" | "admin", "phone_number"?, "is_active"?}`
    → the new account plus a generated `temporary_password`, shown **once**
    in that response only (not stored in plaintext, not logged, never
    returned again).
  - `POST /api/admin/staff/<id>/reset-password` → a new `temporary_password`
    for an existing staff account; the old password stops working at once.
    Their MFA enrolment is kept, so they sign in with the new password and
    their existing authenticator.
  - Both are audited (`staff_account_created`, `staff_password_reset`) with
    the admin as actor — never the password.
- **Bootstrapping the first admin** (or if no admin can sign in) — run
  `scripts/seed_staff.py` directly against the database, as below.

Either way the person then completes MFA enrollment themselves through the
normal public flow (section 2) — the exact same flow a customer goes through,
so they end up on identical security footing (MFA mandatory, backup codes,
audited).

Repeat this for every new hire.

## 1. Seed the account

`scripts/seed_staff.py` is a standalone CLI script — it is **not** a route,
nothing about it is reachable over HTTP. It:

1. Hashes a temporary password with `app.services.security.hash_password` —
   the identical function `/api/auth/register` uses (`pbkdf2:sha256`).
2. Inserts the `User` row directly with `totp_enabled=False`, so the account
   lands in exactly the state a fresh customer registration would.
3. Writes one `staff_account_created` audit-log row so the seed action itself
   is traceable.

It does **not** touch MFA — the staff member sets that up themselves in step 2.

**Run it against production**, from a machine with the Railway CLI linked to
this project (`npm i -g @railway/cli && railway login`), so it picks up the
real `DATABASE_URL` without you ever pasting production credentials into a
local `.env`:

```bash
railway run --service ITP326PNG1_CHECKPOINT_CMS \
  python scripts/seed_staff.py \
  --email jane@primesvault.pg \
  --full-name "Jane Officer" \
  --role loan_officer \
  --created-by "your.name@primesvault.pg"
```

- `--role` only accepts `loan_officer` or `admin` — `customer` is rejected by
  design; use the real registration endpoint for customers.
- Omit `--password` to have a secure one generated for you (recommended) — it
  is printed once to your terminal and **never** written to a file, log, or
  the audit trail. Add `--password '<value>'` only if you need to set a
  specific one.
- Add `--yes` to skip the interactive confirmation (useful if you're scripting
  several hires at once).
- The command prints the masked DB target before asking you to confirm — check
  it says the Supabase production host, not a local SQLite fallback, before
  typing `yes`.

**Hand the temp password to the person over a secure, private channel** —
password manager share, verbal call, a secrets vault link. Never plain email
or chat. This step matters more than usual here: there is no self-service
password change yet (see *Known limitations* below), so this temporary
password is their real password until an admin resets it.

## 2. They complete enrollment (exactly like a customer would)

Send the new staff member these steps (or point them at BACKEND.md's curl
walkthrough — it's the identical sequence customers use):

```bash
BASE=https://<railway-domain>/api/auth

# 1. Login with the temp password -> not enrolled yet, so mfa_setup_token comes back
curl -s -X POST $BASE/login -H 'Content-Type: application/json' -d '{
  "email": "jane@primesvault.pg", "password": "<temp password>"}'
# -> 403 { "mfa_required": "setup", "mfa_setup_token": "<S>" }

# 2. Get a TOTP secret + QR
curl -s -X POST $BASE/mfa/setup -H "Authorization: Bearer <S>"
# -> { "totp_secret": "...", "provisioning_uri": "otpauth://...", "qr_code_png": "data:image/png;base64,..." }
# Scan the QR (or enter the secret manually) in Google Authenticator / Authy / 1Password.

# 3. Confirm enrollment with the 6-digit code -> MFA enabled, backup codes issued once
curl -s -X POST $BASE/mfa/verify-setup -H "Authorization: Bearer <S>" \
  -H 'Content-Type: application/json' -d '{"code": "123456"}'
# -> { "backup_codes": ["RB3WG-88UFA", ...] }   <-- they must store these now

# 4. Log in for real
curl -s -X POST $BASE/login -H 'Content-Type: application/json' -d '{
  "email": "jane@primesvault.pg", "password": "<temp password>"}'
# -> 200 { "mfa_required": "challenge", "mfa_challenge_token": "<C>" }

curl -s -X POST $BASE/mfa/verify-login -H "Authorization: Bearer <C>" \
  -H 'Content-Type: application/json' -d '{"code": "123456"}'
# -> { "access_token": "<A>", "refresh_token": "<R>", "role": "loan_officer" }
```

The JWT's `role` claim now reflects whatever `--role` you seeded — every
`@roles_required("loan_officer")` / `@roles_required("admin")` endpoint works
immediately, no further setup.

## 3. Verify the audit trail

As an existing admin, confirm the whole sequence landed in the ledger:

```bash
curl -s "$BASE/../reports/audit-logs?actor_id=<their user_id>" \
  -H "Authorization: Bearer <admin access_token>"
```

Expect, in order: `login_mfa_setup_required` → `mfa_setup_initiated` →
`mfa_enabled` → `login_password_verified` → `login_success`. Separately,
`GET /api/reports/audit-logs?action=staff_account_created` shows the seed
step itself — `actor_id` is `null` there (no authenticated user runs the CLI
script), with the operator's identity in `details.created_by` instead.

## Known limitations

- **No self-service password change.** Staff can't change their own
  password; an admin resets it (`POST /api/admin/staff/<id>/reset-password`)
  and hands over the new temporary one. Customers have no reset path at all
  yet.
- **A reset doesn't sign out existing sessions.** The old password stops
  working, but refresh tokens already issued stay valid until they expire
  (30 days). Inactive accounts can't log in or refresh, but there is no
  endpoint to deactivate an account yet — if an account may be compromised,
  that currently means a direct database update (`users.is_active = false`).
- **No Administrator UI yet** — the admin endpoints are used through
  Swagger (`/api/docs`) or an API client for now.
