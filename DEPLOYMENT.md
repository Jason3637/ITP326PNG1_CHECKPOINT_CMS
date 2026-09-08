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

## Scheduled job

"Repayment due soon" emails are not automatic. Add a Railway **Cron** service (or
any scheduler) running, daily:

```
python scripts/send_due_reminders.py
```

## Running the tests (CI)

```
pip install -r requirements-dev.txt
pytest
```

Tests use an in-memory SQLite database and never touch Supabase or send email.
