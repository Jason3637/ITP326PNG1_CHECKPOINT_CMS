# Deployment (Railway)

The backend is a standard WSGI app. Railway builds it with Nixpacks/Railpack
(auto-detected from `requirements.txt` + `.python-version`) and runs the
`Procfile`.

## Process model

`Procfile`:

```
web:     gunicorn "run:app" --bind "0.0.0.0:${PORT:-8000}" --workers "${WEB_CONCURRENCY:-2}" --timeout 60 --access-logfile - --error-logfile -
release: flask --app run.py db upgrade
```

- **`web`** runs **gunicorn** — the Flask dev server (`app.run()` in `run.py`) is
  never used in production; it only runs when `run.py` is executed directly.
  `gunicorn` cannot run on Windows (`fcntl`); local dev on Windows uses
  `flask run`. Railway is Linux, so gunicorn runs there.
- **`release`** runs Alembic migrations before the new release goes live. If your
  Railway plan doesn't execute the `release` process, set the service's
  **Pre-Deploy Command** to `flask --app run.py db upgrade` instead (or run it
  once manually via `railway run flask --app run.py db upgrade`).
- `WEB_CONCURRENCY` (worker count) and `PORT` are provided/overridable by Railway.

## Python version

`.python-version` pins **3.14** (matches the dev environment). If Railway's
builder doesn't yet offer 3.14, change it to `3.12` — the code is compatible
(minimum is 3.11 for `enum.StrEnum`). `requirements.txt` pins only the direct
dependencies; transitive packages resolve against whatever interpreter is used.

## Required environment variables

Set these in the Railway service **Variables** tab. There is no `.env` in
production — `.env` is gitignored and only used for local dev.

| Variable | Required | Example / notes |
|---|---|---|
| `DATABASE_URL` | **yes** | Supabase Postgres URI. Use the **Session pooler** URI (port 5432, IPv4). URL-encode reserved chars in the password. `postgresql://postgres.<ref>:<pw>@aws-0-<region>.pooler.supabase.com:5432/postgres?sslmode=require` |
| `JWT_SECRET_KEY` | **yes** | Long random string (≥32 chars). `python -c "import secrets;print(secrets.token_urlsafe(48))"` |
| `MFA_ENCRYPTION_KEY` | **yes** | Fernet key that encrypts TOTP secrets at rest. `python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"` — auth breaks without it |
| `SUPABASE_URL` | **yes** (for docs/storage) | `https://<ref>.supabase.co` |
| `SUPABASE_SERVICE_KEY` | **yes** (for storage) | Supabase → Settings → API → `service_role` secret. Server-side only |
| `SUPABASE_STORAGE_BUCKET` | **yes** (for storage) | e.g. `primes-vault-documents` (created automatically on first use) |
| `CORS_ORIGINS` | **yes** | Comma-separated deployed frontend origin(s), e.g. `https://app.primesvault.example`. No trailing slash |
| `FLASK_ENV` | recommended | `production` |
| `NOTIFICATIONS_ENABLED` | for email | `true` to actually send; default `false` (logs only) |
| `SMTP_USERNAME` | if notifications on | Zoho mailbox address |
| `SMTP_PASSWORD` | if notifications on | Zoho **app-specific password** (not the account password) |
| `MAIL_FROM` | if notifications on | Usually same as `SMTP_USERNAME` |
| `SMTP_HOST` | optional | default `smtp.zoho.com` |
| `SMTP_PORT` | optional | default `587` |
| `SMTP_USE_TLS` | optional | default `true` (STARTTLS on 587) |
| `MAIL_FROM_NAME` | optional | default `Prime's Vault` |

Lending tunables (`DEFAULT_ANNUAL_INTEREST_RATE`, `MIN/MAX_LOAN_AMOUNT`,
`MIN/MAX_LOAN_TERM_MONTHS`) are optional env seeds — an admin can also change them
at runtime via `PUT /api/admin/parameters`. `DOCUMENT_MAX_BYTES`,
`SIGNED_URL_EXPIRY_SECONDS`, `REPAYMENT_REMINDER_LEAD_DAYS`, `CURRENCY_CODE` are
optional with sensible defaults (see `.env.example`).

## First deploy checklist

1. Provision the service, connect the repo, set the variables above.
2. Confirm migrations ran (`release` / pre-deploy) — the DB should have the
   `alembic_version`, `users`, `loans`, … tables.
3. Hit `GET /<domain>/health` → `{"status":"ok","database":"up"}`.
4. Open `GET /<domain>/api/docs` → Swagger UI lists every namespace.
5. Point the frontend's API base URL at the Railway domain and set
   `CORS_ORIGINS` to the frontend's domain.

## Staff accounts (loan_officer / admin)

Public registration is customer-only by design — there is no HTTP endpoint
that creates a `loan_officer` or `admin` account. See
[STAFF_ONBOARDING.md](STAFF_ONBOARDING.md) for the seeding script
(`scripts/seed_staff.py`) and the enrollment steps to hand each new hire.

## Scheduled job

`scripts/send_due_reminders.py` runs two daily maintenance jobs (see its
docstring and `app/services/repayments_scheduler.py`):

1. Flips any `RepaymentSchedule` row whose due date has passed with no full
   payment to `overdue` (proactive - the on-read overdue computation in
   `reporting.py`/`accounts.py` stays as a safety net if this is delayed).
2. Emails each borrower with an installment due within
   `REPAYMENT_REMINDER_LEAD_DAYS` days.

Both are audited (`repayment_marked_overdue`, `repayment_reminder_sent` /
`repayment_reminder_not_sent`). Today it only runs when invoked manually -
nothing schedules it yet. **Set one of the following up:**

### Option A: Railway Cron Schedule (recommended, no extra infra)

Railway's cron scheduling is a **per-service dashboard setting** — there is
currently no `railway.json`/`railway.toml` field for it, so it can't be
committed to the repo. It also requires its own **service**, separate from
the always-on `web` service: a cron service must exit after each run, which
`web` (gunicorn) never does.

1. In the Railway project, **New → GitHub Repo** → same repo/branch as `web`,
   to create a second service (e.g. name it `scheduled-jobs`). It reuses this
   repo's build (same `requirements.txt`/`.python-version`) — no separate
   Dockerfile or build config needed.
2. That service's **Settings → Deploy**:
   - **Start Command**: `python scripts/send_due_reminders.py` (overrides the
     `Procfile`'s `web` line for this service only).
   - **Cron Schedule**: a crontab expression, e.g. `0 6 * * *` (06:00 UTC
     daily). Minimum interval is 5 minutes; Railway skips a run if the
     previous one is still going, so this only matters if the DB/SMTP calls
     ever hang.
3. **Variables**: copy the same variables as `web` (`DATABASE_URL`,
   `MFA_ENCRYPTION_KEY` isn't needed here since this job never touches
   TOTP secrets, but `NOTIFICATIONS_ENABLED`/`SMTP_*` are — otherwise
   reminders run but never actually send). Easiest: Railway → service
   Variables → **"Add variable" → reference** the `web` service's variables,
   or just paste the same values.
4. Deploy once manually to confirm it exits 0 quickly rather than hanging.

### Option B: APScheduler inside the app (if you'd rather not add a service)

Add `APScheduler` to `requirements.txt` and start a background scheduler in
`app/__init__.py`'s `create_app()` that calls
`repayments_scheduler.flip_overdue_installments()` and
`.send_due_soon_reminders()` daily. **Tradeoff:** it runs inside every
gunicorn worker process, so with `WEB_CONCURRENCY` > 1 the job fires once per
worker (needs a lock, e.g. a Postgres advisory lock or a "only worker 0"
check) unless guarded — Railway Cron avoids that entirely since it's a
single, separate, short-lived process. Prefer Option A unless Railway Cron
turns out to be unavailable on your plan.

```
python scripts/send_due_reminders.py   # still the entrypoint either way
```

## CI (GitHub Actions)

`.github/workflows/ci.yml` runs on every push and pull request to `Develop`
and `main`: install `requirements-dev.txt`, `ruff check .` (lint), then
`pytest` (the full suite) — the build fails on any lint error or test
failure. No secrets/env vars needed: tests run entirely against
`TestingConfig` (in-memory SQLite, hardcoded test-only JWT/MFA keys,
notifications disabled — see `tests/conftest.py`) and never touch Supabase,
Railway, or a real mailbox.

This is deliberately just a regression safety net, not a deployment
pipeline — Railway deploys off its own GitHub integration (see above),
independently of this workflow.

Run the same checks locally before pushing:

```
pip install -r requirements-dev.txt
ruff check .
python -m pytest
```
