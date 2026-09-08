# Prime's Vault - Backend (Phase A scaffold)

Flask backend using the application-factory pattern.

## Stack

| Concern        | Library                |
|----------------|------------------------|
| Web framework  | Flask                  |
| REST + Swagger | flask-restx            |
| Auth tokens    | flask-jwt-extended     |
| ORM            | flask-sqlalchemy       |
| Migrations     | flask-migrate (Alembic)|
| Postgres driver| psycopg2-binary        |
| Config         | python-dotenv          |
| TOTP / MFA     | pyotp, qrcode[pil]     |
| File storage   | supabase (Storage)     |
| Prod server    | gunicorn               |

## Layout

```
app/
  __init__.py      # create_app() factory + /health route
  extensions.py    # db, migrate, jwt instances
  config.py        # env-driven config classes
  models/          # (empty - Phase B1)
  api/             # Flask-RESTX Api mounted at /api, docs at /api/docs
    auth/ users/ accounts/ loans/ payments/ reports/   # placeholder namespaces
  services/        # (empty - business logic, later phases)
  storage/         # (empty - Supabase Storage helper, Phase B4)
scripts/
  healthcheck_db.py  # verify Supabase Postgres connectivity
.env.example
requirements.txt
run.py
```

## Setup

```bash
py -m venv venv
venv\Scripts\activate           # Windows
pip install -r requirements.txt
copy .env.example .env          # then fill in values
```

## Verify DB connectivity (do this before any modeling)

```bash
python scripts/healthcheck_db.py
```

## Run

```bash
set FLASK_APP=run.py
set FLASK_ENV=development
flask run
```

- Swagger UI:  http://127.0.0.1:5000/api/docs
- Health:      http://127.0.0.1:5000/health
- Namespaces (all return 501 for now): /api/auth, /api/users, /api/accounts,
  /api/loans, /api/payments, /api/reports

Production:

```bash
gunicorn "run:app"
```

## DATABASE_URL format (Supabase)

Supabase Project -> **Settings -> Database -> Connection string -> URI**.

**Session pooler** (recommended for a long-lived Flask app, IPv4-friendly):

```
postgresql://postgres.<project-ref>:<PASSWORD>@aws-0-<region>.pooler.supabase.com:5432/postgres?sslmode=require
```

**Transaction pooler** (port 6543 - use only if you disable SQLAlchemy pooling / server-side prepared statements):

```
postgresql://postgres.<project-ref>:<PASSWORD>@aws-0-<region>.pooler.supabase.com:6543/postgres?sslmode=require
```

**Direct connection** (non-pooled, needs IPv6 or the IPv4 add-on):

```
postgresql://postgres:<PASSWORD>@db.<project-ref>.supabase.co:5432/postgres?sslmode=require
```

Notes:
- URL-encode special characters in the password (`@` -> `%40`, `:` -> `%3A`, `?` -> `%3F`, `%` -> `%25`, etc.).
- `sslmode=require` is expected by Supabase.
- SQLAlchemy also accepts the `postgresql+psycopg2://` prefix; plain `postgresql://` resolves to psycopg2 here.

---

## Authentication flow (multi-step, MFA mandatory)

TOTP is required for **every** account, so login is a two-call sequence and there
is a one-time enrollment sequence before a user can ever log in.

```
register ──▶ mfa/setup ──▶ mfa/verify-setup ──▶ login ──▶ mfa/verify-login ──▶ access + refresh JWT
             │  needs mfa_setup_token          │          │  needs mfa_challenge_token
             │  (returned by register/login)   │          │  (returned by login)
             └── QR + secret ──────────────────┘          └── role embedded as a JWT claim
```

Token types (all JWT, distinguished by a `scope` claim):

| Token | Lifetime | Unlocks |
|---|---|---|
| `mfa_setup_token` | 15 min | `mfa/setup`, `mfa/verify-setup` |
| `mfa_challenge_token` | 5 min | `mfa/verify-login` |
| `access_token` | 1 hour | every protected endpoint (`scope=access`, carries `role`) |
| `refresh_token` | 30 days | `POST /api/auth/refresh` |

### Endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/api/auth/register` | none | create a `customer` account (staff are provisioned manually) |
| POST | `/api/auth/mfa/setup` | `mfa_setup_token` | generate TOTP secret + QR |
| POST | `/api/auth/mfa/verify-setup` | `mfa_setup_token` | confirm code, enable MFA, return backup codes (once) |
| POST | `/api/auth/login` | none | verify password → `mfa_challenge_token` (or `mfa_setup_token` if MFA not yet set up) |
| POST | `/api/auth/mfa/verify-login` | `mfa_challenge_token` | verify TOTP **or** backup code → access + refresh tokens |
| POST | `/api/auth/refresh` | `refresh_token` | new access token |
| GET | `/api/auth/me` | `access_token` | current user from verified claims |

Every step above writes a row to `audit_logs` (actor, action, IP, timestamp,
details) — successful and failed attempts alike.

### Example: full flow with curl

Set a base URL and register:

```bash
BASE=http://127.0.0.1:5000/api/auth

# 1. Register (role is always 'customer')
curl -s -X POST $BASE/register -H 'Content-Type: application/json' -d '{
  "email": "jane@example.com",
  "password": "correct horse battery staple",
  "full_name": "Jane Doe",
  "phone_number": "+675 7000 0000"
}'
# -> 201 { "mfa_setup_token": "<S>", "user_id": 1, ... }
```

```bash
S=<paste mfa_setup_token>

# 2. MFA setup - returns the secret, otpauth:// URI, and a base64 PNG QR
curl -s -X POST $BASE/mfa/setup -H "Authorization: Bearer $S"
# -> 200 { "totp_secret": "JBSWY3DP...", "provisioning_uri": "otpauth://...", "qr_code_png": "data:image/png;base64,..." }
```

Scan the QR in Google Authenticator / Authy / 1Password. For testing without an
app, derive the current code from the secret:

```bash
CODE=$(python -c "import pyotp,sys; print(pyotp.TOTP(sys.argv[1]).now())" JBSWY3DP...)

# 3. Confirm enrollment -> enables MFA, returns 10 one-time backup codes
curl -s -X POST $BASE/mfa/verify-setup -H "Authorization: Bearer $S" \
  -H 'Content-Type: application/json' -d "{\"code\": \"$CODE\"}"
# -> 200 { "backup_codes": ["RB3WG-88UFA", ...] }   <-- store these, shown once
```

```bash
# 4. Login step 1 - password
curl -s -X POST $BASE/login -H 'Content-Type: application/json' -d '{
  "email": "jane@example.com",
  "password": "correct horse battery staple"
}'
# -> 200 { "mfa_challenge_token": "<C>", "mfa_required": "challenge" }
# (if MFA is not set up yet: -> 403 { "mfa_setup_token": "<S>", "mfa_required": "setup" })
```

```bash
C=<paste mfa_challenge_token>
CODE=$(python -c "import pyotp,sys; print(pyotp.TOTP(sys.argv[1]).now())" JBSWY3DP...)

# 5. Login step 2 - TOTP (or backup code) -> real tokens
curl -s -X POST $BASE/mfa/verify-login -H "Authorization: Bearer $C" \
  -H 'Content-Type: application/json' -d "{\"code\": \"$CODE\"}"
# -> 200 { "access_token": "<A>", "refresh_token": "<R>", "role": "customer", "token_type": "Bearer" }

# backup code instead of TOTP:
#   -d '{"backup_code": "RB3WG-88UFA"}'
```

```bash
A=<paste access_token>

# Use it
curl -s $BASE/me -H "Authorization: Bearer $A"
# -> 200 { "id": 1, "email": "jane@example.com", "role": "customer", "totp_enabled": true, ... }

# Refresh when the access token expires
curl -s -X POST $BASE/refresh -H "Authorization: Bearer <refresh_token>"
```

### Protecting other endpoints

`app/api/auth/decorators.py` exports guards for the other namespaces:

```python
from app.api.auth.decorators import roles_required, current_user_id

@ns.route("/pending")
class PendingLoans(Resource):
    @ns.doc(security="Bearer")
    @roles_required("loan_officer", "admin")   # 401 if not an access token, 403 if role mismatch
    def get(self):
        ...

@roles_required()          # any authenticated user (access token, any role)
```

The access token's `role` claim is the source of truth — it is verified on every
request ("Claims Verified per request" in the diagram).

---

## Lending modules (Business Logic box)

Each module in the architecture diagram is a plain module in `app/services/`,
orchestrated by `loan_processing.py` and exposed through the namespaces.

| Diagram module | File | Entry points |
|---|---|---|
| Members Registry | `services/members.py` | `get_profile()` |
| Loan Processing | `services/loan_processing.py` | `submit_application()`, `list_applications()`, `decide_application()` |
| Credit Evaluation | `services/credit_evaluation.py` | `evaluate()` — **placeholder algorithm** |
| Interest Calculation | `services/interest_calculation.py` | `amortize()`, `installment_count()` |
| Repayments Scheduler | `services/repayments_scheduler.py` | `generate_schedule()` |
| Account tracking | `services/accounts.py` | `get_account_summary()` |
| Audit Ledger | `services/audit.py` | `record()` (every state change) |

### Endpoints

| Method | Path | Role | Purpose |
|---|---|---|---|
| GET | `/api/users/profile` | any authenticated | logged-in member's profile |
| POST | `/api/loans/apply` | `customer` | submit a LoanApplication (runs credit evaluation) |
| GET | `/api/loans/applications` | `loan_officer`, `admin` | list open applications (`?status=` to filter) |
| POST | `/api/loans/applications/<id>/decision` | `loan_officer`, `admin` | `{"decision":"approve"\|"reject", "interest_rate"?, "note"?}` |
| GET | `/api/loans/mine` | `customer` | the customer's own loans + schedules |
| GET | `/api/accounts/summary` | `customer` | dashboard: active loans, next repayment due, progress % |

On **approve**: a `Loan` row is created, priced via `amortize()`, disbursed
(`disbursed_at = now` — for a small SME, approval == disbursement), and a full
`RepaymentSchedule` is generated. Audit rows written: `loan_application_submitted`,
`loan_application_decision`, `loan_disbursed`.

### Interest rate configuration — where it lives

Prime's Vault is a small SME, not a bank with tiered rate products, so there is
**no rate table**. The default annual rate is a single environment variable:

```
DEFAULT_ANNUAL_INTEREST_RATE=0.18   # 18% APR, as a fraction
```

A loan officer may override the rate for an individual decision
(`"interest_rate": 0.15` in the decision body). Whatever rate is actually used is
persisted on `loans.interest_rate`, so historical loans are unaffected by later
config changes. Other tunables (also env vars, defaults shown):
`MIN_LOAN_AMOUNT=100`, `MAX_LOAN_AMOUNT=50000`, `MIN_LOAN_TERM_MONTHS=1`,
`MAX_LOAN_TERM_MONTHS=60`.

If rate rules later become more complex (per-product, per-tenure, promotional),
promote this to a small `lending_parameters` table with an effective-date column —
but not before the client actually needs it.

### Amortization

Standard formula, `installment = P·r / (1 − (1+r)^−n)` (with `r = 0` handled).
Periods per year: monthly 12, biweekly 26, weekly 52; `n = round(term_months ·
periods_per_year / 12)`. The final installment absorbs the rounding remainder so
`Σ amount_due == total_repayable` exactly.

### Credit Evaluation is a PLACEHOLDER

`credit_evaluation.evaluate()` uses a transparent heuristic (requested amount vs.
configured max, term length, prior-loan repayment history, account standing) to
produce `{score 0-100, eligible, reasons[], recommendation}`, stored on
`loan_applications.credit_evaluation_result` with `"algorithm": "placeholder-v1"`.
**Replace `_score()` with the client's real lending criteria** (income
verification, affordability ratios, guarantor rules) when available. It never
auto-approves — a loan officer always makes the final decision.

---

## Payments & documents (Phase B4)

### Payments

| Method | Path | Role | Purpose |
|---|---|---|---|
| POST | `/api/payments/repay` | `customer`, `loan_officer`, `admin` | record a payment against a `RepaymentSchedule` installment |
| GET | `/api/payments/loan/<loan_id>` | owner customer, or staff | list payments on a loan |

`POST /api/payments/repay` body: `{"repayment_schedule_id": int, "amount": number, "payment_method": string}`.
It creates a `PaymentTransaction` (status `completed`), adds to
`repayment_schedules.amount_paid`, flips the installment to `paid` once
`amount_paid >= amount_due`, and flips the loan to `completed` when every
installment is paid.

**Access — deliberate MVP choice:** a customer can pay their **own** loan's
installments; a `loan_officer`/`admin` can **also** record a payment, for manual
or over-the-counter (cash) entries — a real workflow for a small SME where members
pay in person. Every payment records `entered_by_role` and `on_behalf` in its
audit row. If you would rather lock this to **customers only** for the MVP, say so
and it's a one-line change in `payment_processing.record_payment` — but the office
would then have no way to log a cash payment.

Payments are marked `completed` immediately (no payment gateway in this phase).
When a gateway is integrated, create the row as `pending` and settle on callback.

### Documents — Supabase Storage only

Files go to a **private** Supabase Storage bucket (`SUPABASE_STORAGE_BUCKET`,
auto-created on first use). Postgres stores **only** `documents.storage_path` —
never the bytes, never local disk.

Object path layout: `users/<user_id>/<document_type>/<random>_<filename>`
(`document_type` ∈ `id_verification`, `receipt`, `loan_file`).

| Method | Path | Role | Purpose |
|---|---|---|---|
| POST | `/api/users/documents` | `customer` | multipart upload (`file`, `document_type`, optional `loan_application_id`) |
| GET | `/api/users/documents` | any authenticated | list own documents (`?document_type=`) |
| GET | `/api/users/documents/<id>/download` | owner **or** staff | returns a short-lived **signed URL** (default 600 s) |
| GET | `/api/users/<user_id>/documents` | `loan_officer`, `admin` | list a member's documents for review |

**Validation:** content type must be `application/pdf`, `image/jpeg`, or
`image/png`; size `1 … DOCUMENT_MAX_BYTES` (default 10 MiB) → `415` / `413`.

**Download = Temporary Signed URL Access:** Flask never streams the file. The
download endpoint calls Supabase `create_signed_url` and returns
`{"signed_url": ..., "expires_in_seconds": 600}`; the client fetches the bytes
directly from Supabase. A user can only sign URLs for their own documents;
`loan_officer`/`admin` can sign any (for review). Every upload and every
signed-URL issue writes an AuditLog row (`document_uploaded` / `document_download`,
with `storage_path`, `owner_id`, `by_staff`).

Storage config (env, defaults shown): `DOCUMENT_MAX_BYTES=10485760`,
`SIGNED_URL_EXPIRY_SECONDS=600`.

### Example: upload + signed-URL download with curl

```bash
A=<access_token from the auth flow>
BASE=http://127.0.0.1:5000/api

# upload
curl -s -X POST $BASE/users/documents -H "Authorization: Bearer $A" \
  -F "document_type=id_verification" \
  -F "file=@/path/to/id.png;type=image/png"
# -> 201 { "id": 5, "storage_path": "users/1/id_verification/ab12cd34ef56_id.png", ... }

# get a temporary link
curl -s $BASE/users/documents/5/download -H "Authorization: Bearer $A"
# -> 200 { "signed_url": "https://<project>.supabase.co/storage/v1/object/sign/...", "expires_in_seconds": 600 }

# fetch the file straight from Supabase (no auth header - the token is in the URL)
curl -s -o id.png "<signed_url>"
```

---

## Reporting, notifications & admin (Phase B5)

### Reporting Tool

`GET /api/reports/dashboard` (any authenticated user) is **role-aware**:

- **customer** → own loan/repayment summary
- **loan_officer / admin** → portfolio aggregates

Response is shaped for Chart.js. Every chart block is
`{"labels": [...], "series": [{"name": ..., "data": [...]}]}` (single- and
multi-series both use this), alongside flat `kpis` for stat cards and `tables`
for grids. `charts.disbursements_vs_collections_by_month` is multi-series (last 6
months). Overdue is computed dynamically (`due_date < today AND status != paid`),
independent of the stored `overdue` status.

### Notifications Router — `app/services/notifications.py`

Transactional email over **Zoho Mail SMTP** (`smtplib`, no extra dependency).
Events: `notify_application_received`, `notify_loan_approved`,
`notify_loan_rejected`, `notify_payment_received`, `notify_repayment_due_soon`.
Wired into `loan_processing` and `payment_processing` **after commit**, and
best-effort: a send failure is logged and swallowed, never rolling back the
transaction. Disabled by default (`NOTIFICATIONS_ENABLED=false`) — when disabled,
events are logged, not sent.

> **Scope (per the diagram's explicit note):** this module is for notifications
> ONLY. It must never carry authentication factors — no TOTP codes, no
> password-reset tokens, no OTPs. MFA stays app-based TOTP (`app/services/mfa.py`).
> There is no generic "send code" function and there must never be one.

Config (env): `NOTIFICATIONS_ENABLED`, `SMTP_HOST` (`smtp.zoho.com`), `SMTP_PORT`
(`587`), `SMTP_USE_TLS`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `MAIL_FROM`,
`MAIL_FROM_NAME`, `REPAYMENT_REMINDER_LEAD_DAYS` (`3`).

"Repayment due soon" is schedule-driven — run `python scripts/send_due_reminders.py`
from cron / Task Scheduler (e.g. daily).

### Admin: audit ledger access

`GET /api/reports/audit-logs` (**admin only**) — paginated, filterable:
`page`, `per_page` (≤200), `actor_id`, `action`, `entity_type`, `date_from`,
`date_to` (`YYYY-MM-DD` or ISO 8601). Returns
`{page, per_page, total, pages, items[]}`.

### Admin: system parameters

`GET` / `PUT /api/admin/parameters` (**admin only**) — the runtime tunables from
Phase B3:

| key | type | seed default |
|---|---|---|
| `default_annual_interest_rate` | rate (0–1) | 0.18 |
| `min_loan_amount` / `max_loan_amount` | money | 100 / 50000 |
| `min_loan_term_months` / `max_loan_term_months` | int | 1 / 60 |

Stored in the `system_parameters` table (one row per override). Config values are
the **seed defaults**; a stored row overrides at runtime with no redeploy.
`loan_processing` and `credit_evaluation` read every one of these through
`app/services/parameters.py`, so a `PUT` takes effect on the next application.
`PUT` body is a partial object (`{"default_annual_interest_rate": 0.24}`); each
change is validated and audited (`system_parameters_updated`).

### CORS

`flask-cors` is applied to `/api/*` and `/health` in the app factory. Allowed
origins come from `CORS_ORIGINS` (comma-separated); dev default is
`http://localhost:3000,http://127.0.0.1:3000` (Next.js dev server). Allowed
headers: `Authorization`, `Content-Type`. Methods: `GET, POST, PUT, PATCH,
DELETE, OPTIONS`. **On deploy, set `CORS_ORIGINS` to the deployed frontend URL(s)**
— e.g. `CORS_ORIGINS=https://app.primesvault.example`. Because auth is a Bearer
JWT header (not cookies), credentialed CORS is not needed.

---

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

`pytest` runs against an in-memory SQLite DB (`TestingConfig`) built with
`db.create_all()` — no migration, no Supabase, no email. Coverage:

| File | What it locks down |
|---|---|
| `tests/test_auth_flow.py` | full register → MFA setup → verify-setup → login → MFA verify-login → `/me`; login-before-MFA rejected; wrong password 401; backup code single-use; scoped token can't call the API |
| `tests/test_loan_lifecycle.py` | apply → officer review → approve → repay → loan `completed`; schedule sums to `total_repayable`; duplicate/over-limit applications rejected; amortization installment counts |
| `tests/test_rbac.py` | customer→officer endpoint = 403, officer→admin = 403, no token = 401, dashboard shape differs by role |
| `tests/test_docs_swagger.py` | every B2–B5 endpoint present in `/api/swagger.json` with a body model, a documented 2xx response model, and Bearer security |

## Deployment

See [DEPLOYMENT.md](DEPLOYMENT.md) — Railway `Procfile` (gunicorn `web` +
`flask db upgrade` `release`), `.python-version`, and the full required-env-var
table.
