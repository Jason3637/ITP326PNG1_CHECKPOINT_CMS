# Staff Onboarding (loan_officer / admin)

Public registration (`POST /api/auth/register`) always creates a `customer`
account — that's deliberate, see BACKEND.md → *Authentication flow*. There is
no HTTP endpoint that creates a `loan_officer` or `admin` account. Until a
proper admin-facing "create staff user" endpoint exists (see HANDOFF.md →
*Staff onboarding*), creating one means running `scripts/seed_staff.py`
directly against the database, then having that person complete MFA
enrollment themselves through the normal public flow — the exact same flow a
customer goes through, so they end up on identical security footing (MFA
mandatory, backup codes, audited).

Repeat this whole process for every new hire.

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
or chat. This step matters more than usual here: there is currently no
password-change or reset endpoint anywhere in the API (see *Known
limitation* below), so this temporary password is their real password until
that changes.

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

- **No password-change or reset endpoint exists yet.** If a staff member's
  temp password is compromised or they simply want to change it, the only
  fix today is re-running a direct DB update — there's no self-service or
  admin-facing path. Worth prioritizing before onboarding many staff.
- **This whole process is a stand-in.** HANDOFF.md already flags "Staff
  onboarding... add an admin-only 'create staff user' endpoint if needed" as
  a deferred item. If staff turnover becomes routine, that endpoint (gated
  `@roles_required("admin")`, same hashing/audit logic as this script) is the
  natural next step — this doc's job is to make the manual process safe and
  repeatable until then, not to be the permanent answer.
