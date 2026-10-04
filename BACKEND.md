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

Every token also carries `tv`, the user's `users.token_version` when it was
issued. A token whose `tv` doesn't match the current version is rejected as
revoked (401) - an admin password reset bumps the version, signing that user
out of every session. Tokens without `tv` (issued before it existed) count as
version 0.

### Endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/api/auth/register` | none | create a `customer` account (staff accounts are created by an admin - `POST /api/admin/staff`) |
| POST | `/api/auth/mfa/setup` | `mfa_setup_token` | generate TOTP secret + QR |
| POST | `/api/auth/mfa/verify-setup` | `mfa_setup_token` | confirm code, enable MFA, return backup codes (once) |
| POST | `/api/auth/login` | none | verify password → `mfa_challenge_token` (or `mfa_setup_token` if MFA not yet set up) |
| POST | `/api/auth/mfa/verify-login` | `mfa_challenge_token` | verify TOTP **or** backup code → access + refresh tokens |
| POST | `/api/auth/refresh` | `refresh_token` | new access token |
| GET | `/api/auth/me` | `access_token` | current user from verified claims |

Every step above writes a row to `audit_logs` (actor, action, IP, timestamp,
details) — successful and failed attempts alike.

### Rate limiting

The five endpoints above that take a credential/code guess are throttled with
Flask-Limiter (`app/extensions.py`), per client IP, per minute:

| Endpoint | Limit | Why |
|---|---|---|
| `/register` | 10/min | spam-signup abuse |
| `/mfa/setup` | 10/min | repeated secret regeneration abuse |
| `/mfa/verify-setup` | 5/min | 6-digit TOTP guessing during enrollment |
| `/login` | 5/min | password brute-force |
| `/mfa/verify-login` | 5/min | 6-digit TOTP / backup-code guessing |

Exceeding a limit returns `429` with the same `{"message": ...}` shape as
every other error (`app/api/__init__.py`'s `RateLimitExceeded` handler) - a
fixed, generic message ("Too many attempts...") with **no** endpoint- or
account-specific detail, so the 429 itself can never be used to infer whether
an email is registered (confirmed in `tests/test_rate_limiting.py`, including
a byte-for-byte comparison of the 429 body for a real vs. a nonexistent
email).

The rate-limit key is the same "real client IP behind Railway's proxy" logic
`app.services.audit.client_ip()` already uses (a single `X-Forwarded-For`
hop) — reused deliberately so there's one source of truth for "what is the
client's IP," not two. Storage is in-memory (`memory://`): fine for a single
process, but with `WEB_CONCURRENCY` > 1 each gunicorn worker keeps its own
counters, so the *effective* limit is roughly `limit × worker count` rather
than exact. Move to Redis (`storage_uri="redis://..."`) if that stops being
precise enough.

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
| Loan Processing | `services/loan_processing.py` | the application state machine: `submit_application()`, `start_officer_review()`, `request_customer_action()`, `respond_to_customer_action()`, `submit_recommendation()`, `decide_application()`, `disburse_application()`, ... |
| Verification checklist | `services/verification.py` | `CHECKLIST` registry, `update_item()`, `serialize_checklist()` |
| Loan Officer workspace | `services/officer_views.py` | `queue_counts()`, `list_queue()`, `application_detail()`, `customer_history()` |
| Credit Evaluation | `services/credit_evaluation.py` | `evaluate()` — **interim model, thresholds still provisional, advisory only** (see below) |
| PRIME pricing | `services/prime_pricing.py` | `calculate_prime()` — the only place tiers/rates live |
| Repayments Scheduler | `services/repayments_scheduler.py` | `generate_bullet_schedule()` |
| Account tracking | `services/accounts.py` | `get_account_summary()` |
| Audit Ledger | `services/audit.py` | `record()` (every state change) |

### Application workflow

```
SUBMITTED --officer claims--> OFFICER_REVIEW
OFFICER_REVIEW --officer requests information--> CUSTOMER_ACTION_REQUIRED
CUSTOMER_ACTION_REQUIRED --customer responds | officer resumes (cancels open requests)--> OFFICER_REVIEW
OFFICER_REVIEW --officer recommends--> RECOMMENDED_FOR_APPROVAL | RECOMMENDED_FOR_REJECTION
RECOMMENDED_FOR_* --admin--> ADMIN_REVIEW
RECOMMENDED_FOR_* | ADMIN_REVIEW --admin returns--> RETURNED_TO_OFFICER --officer resumes--> OFFICER_REVIEW
ADMIN_REVIEW --admin--> APPROVED -> AWAITING_DISBURSEMENT --admin disburses--> Loan ACTIVE
ADMIN_REVIEW --admin--> REJECTED
any open status --admin early exit--> REJECTED
```

* **Claim-on-review:** a SUBMITTED application is in the shared officer queue; `officer-review` claims it (`assigned_officer_id`). Only that officer — or an admin — can then work the checklist, request information, resume or recommend. Admins can reassign.
* **Request More Information** creates one `InformationRequest` row per item; the customer's `respond` must answer every open request and creates one `InformationResponse` per request (with the old/new value of each field it changed). A request that names a `required_document_type` must be answered with a newly uploaded document of that type in `document_ids` (the customer's own, not yet attached to anything) - otherwise 400; an information-only request needs just the answer. Rounds are never overwritten. `action_required_note` in API responses is now derived from the open requests (the old column is no longer written).
* **Recommendations** are immutable `OfficerRecommendation` rows with the checklist frozen into them. Approval needs every required checklist item `verified`/`not_applicable` and none `failed`. Recommending never creates a loan or touches disbursement.
* **Credit assessment** is advisory only — shown to staff as `credit_assessment: {label: "Advisory - not a decision input", advisory: true, affects_status: false, result}`; customers never see it, and no code path reads it to set a status.

### RBAC matrix

| Action | customer | loan_officer | admin |
|---|:-:|:-:|:-:|
| Apply; respond to own information requests | ✅ own | — | — |
| Officer queues, review screen, checklist (read), customer history\* | — | ✅ | ✅ |
| Claim (start officer review) | — | ✅ | ✅ |
| Update checklist items; request information; resume review; recommend approval/rejection | — | ✅ if claimed by them | ✅ |
| Reassign; start admin review; return to officer | — | ❌ | ✅ |
| **Final approve / reject; early-exit reject** | — | ❌ | ✅ |
| **Record disbursement** | — | ❌ | ✅ |
| Report a repayment | ✅ own | ✅ counter entry | ✅ |
| Claim a repayment for verification | — | ✅ | ✅ |
| **Verify / reject a repayment** | — | ❌ | ✅ |
| Write off a loan (paid loans close automatically) | — | ❌ | ✅ |
| Administrator API (`/api/admin/...`: queues, decisions, disbursement, loans, repayments, pricing, analytics) | — | ❌ | ✅ |
| System parameters; audit logs | — | ❌ | ✅ |
| Create staff accounts; reset a staff password | — | ❌ | ✅ |

\* Loan officers reach customer history only through an application that is still open — there is no customer-id lookup. Within that, visibility is **team-wide by design**: any loan officer may view it for any open application, not only ones claimed by them (pinned by `test_customer_history_is_team_wide_for_loan_officers`).
Admin-only actions are checked at the route **and** in the service layer (`loan_processing._require_admin`, `payment_processing.verify_payment`), covered by `tests/test_loan_officer_rbac.py`. An admin may act in the officer role (claim, recommend) and then decide, but each is a separate, separately audited call; the decision's audit entry records `same_actor_as_recommender` and `overrides_recommendation`.

### Endpoints

Full request/response models are in Swagger (`/api/docs`).

| Method | Path | Role | Purpose |
|---|---|---|---|
| GET | `/api/users/profile` | any authenticated | logged-in member's profile |
| POST | `/api/loans/apply` | `customer` | submit a LoanApplication (runs the advisory credit evaluation) |
| GET | `/api/loans/prime-preview` | any authenticated | live PRIME pricing for an amount |
| GET | `/api/loans/applications/mine` | `customer` | own applications incl. information requests |
| GET | `/api/loans/applications` | `loan_officer`, `admin` | open applications (`?status=`) |
| POST | `/api/loans/applications/<id>/officer-review` | `loan_officer`, `admin` | claim: SUBMITTED → OFFICER_REVIEW |
| POST | `/api/loans/applications/<id>/request-action` | assigned officer, `admin` | `{"requests": [{request_type, reason, required_document_type?, required_information?, internal_note?}]}` |
| POST | `/api/loans/applications/<id>/respond` | `customer` (owner) | `{"responses": [{information_request_id, response_note}], ...field updates}` |
| POST | `/api/loans/applications/<id>/resume-review` | assigned officer, `admin` | `{"reason"}` (required when cancelling open requests) |
| POST | `/api/loans/applications/<id>/recommend` | assigned officer, `admin` | `{"recommendation": "recommend_approval" or "recommend_rejection", "comments"}` |
| POST | `/api/loans/applications/<id>/assign` | `admin` | `{"officer_id"}` |
| POST | `/api/loans/applications/<id>/admin-review` | `admin` | RECOMMENDED_FOR_* → ADMIN_REVIEW |
| POST | `/api/loans/applications/<id>/return-to-officer` | `admin` | `{"reason"}` → RETURNED_TO_OFFICER |
| POST | `/api/loans/applications/<id>/decision` | `admin` | `{"decision": "approve" or "reject", "note"}` (note required when overriding the recommendation) |
| POST | `/api/loans/applications/<id>/reject` | `admin` | early-exit reject from any open status |
| POST | `/api/loans/applications/<id>/disburse` | `admin` | creates the Loan + schedule + Disbursement |
| GET | `/api/officer/queues` | `loan_officer`, `admin` | `{queues: {name: {total, mine, unassigned}}}` |
| GET | `/api/officer/queues/<queue>` | `loan_officer`, `admin` | `awaiting_review`, `under_review`, `customer_action_required`, `sent_to_admin`, `returned_by_admin`; filters `assigned` (any/me/unassigned), `officer_id`, `prime_category`, `page`, `per_page` |
| GET | `/api/officer/applications/<id>` | `loan_officer`, `admin` | the Application Review screen in one call |
| GET | `/api/officer/applications/<id>/checklist` | `loan_officer`, `admin` | checklist state |
| PATCH | `/api/officer/applications/<id>/checklist/<item_type>` | assigned officer, `admin` | `{"status", "note"}` — one item, records who/when |
| GET | `/api/officer/applications/<id>/customer-history` | `loan_officer`\*, `admin` | this application's customer's history |
| GET | `/api/loans/mine` | `customer` | the customer's own loans + schedules |
| POST | `/api/loans/<id>/write-off` | `admin` | ACTIVE/OVERDUE → CLOSED (defaulted), reason required — also at `/api/admin/loans/<id>/write-off` |
| GET | `/api/accounts/summary` | `customer` | dashboard: active loans, next repayment due, progress % |

### Interest rate configuration — where it lives

Prime's Vault is a small SME, not a bank with tiered rate products, so there is
**no rate table**. The default annual rate is a single environment variable:

```
DEFAULT_ANNUAL_INTEREST_RATE=0.18   # 18% APR, as a fraction
```

The PRIME product does not use this rate: its flat per-tier rates live in
`app/services/prime_pricing.py`, and the decision endpoint takes no rate
override. Whatever rate is actually used is
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

### Credit Evaluation — interim model, still NOT the client's final policy

`credit_evaluation.evaluate()` produces `{score 0-100, eligible,
insufficient_data, max_eligible_amount, reasons[], recommendation,
criteria_checked[]}`, stored on `loan_applications.credit_evaluation_result`
with `"algorithm": "interim-v2"`. It is a materially more complete rule set
than the original placeholder (`"algorithm": "placeholder-v1"`, kept on old
rows so historical evaluations stay identifiable) — but the exact thresholds
are still engineering guesses, not criteria the client has signed off on.
**What's actually live right now** (see `_score()` for the exact math):

1. Requested amount vs. the configured min/max loan amount.
2. Term length (>36 months penalized, >24 months a smaller penalty).
3. Repayment history — both a coarse signal (any defaulted/active prior loan)
   and a finer on-time-payment ratio computed from each installment's
   `due_date` vs. the settling payment's `paid_at`.
4. Account standing (`User.is_active`).
5. **Minimum income** — self-reported `LoanApplication.monthly_income`,
   checked against the `min_monthly_income` parameter. Missing income is a
   **hard disqualifier** (`insufficient_data: true` in the result) —
   independent of the numeric score, since affordability can't be assessed
   without it.
6. **Employment status** — self-reported `LoanApplication.employment_status`
   (`employed | self_employed | unemployed | retired | student`);
   `unemployed`/`student` score lower, a missing value scores lower still but
   is *not* a hard gate the way missing income is.
7. **Debt-to-income ratio** — `(existing_monthly_debt + estimated new
   installment) / monthly_income` against the `max_debt_to_income_ratio`
   parameter (the new installment is estimated at the default rate, since the
   real rate isn't set until an officer approves).
8. **Membership tenure** — `User.created_at` age; accounts under 30 days old
   score slightly lower, accounts over a year old get a small bonus.

None of 5-7 are independently verified (no payslip upload, no employer
contact, no credit bureau integration) — entirely self-reported at
application time by the applicant. `max_eligible_amount` is capped by
affordability (the largest principal whose installment still respects the DTI
ceiling) whenever income is known, not just the naive score-linear cap. It
never auto-approves — an admin always makes the final decision, on a loan
officer's recommendation.

**Frontend note:** `POST /api/loans/apply` gained three optional request
fields feeding this — `monthly_income`, `employment_status`,
`existing_monthly_debt` (see `LoanApplyInput` in Swagger) — and returns them
back on the application object. The apply form needs matching inputs;
omitting `monthly_income` is valid but means the application can never be
marked eligible.

**Tuning:** `min_monthly_income` and `max_debt_to_income_ratio` are
admin-editable via `PUT /api/admin/parameters` (see above) — update them the
moment the client provides real figures, no redeploy needed.

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
Events: `notify_application_received`, `notify_customer_action_required`
(officer requested more information — lists each request's customer-facing
reason and required document/information, never the officer's internal note),
`notify_loan_approved`, `notify_loan_rejected`, `notify_payment_received`,
`notify_repayment_due_soon`. Internal workflow steps (claim, checklist,
recommendation, admin review/return) deliberately send nothing: the customer
only sees "Under Review" until a decision.
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
`page`, `per_page` (≤200), `actor_id`, `actor_role`, `action`, `entity_type`,
`entity_id`, `date_from`, `date_to` (`YYYY-MM-DD` or ISO 8601). Returns
`{page, per_page, total, pages, items[]}`; each item has `actor_id`,
`actor_role` (the role **at the time** — null for system actions and for rows
written before this column existed), `action`, `entity_type`, `entity_id`,
`details`, `ip_address`, `created_at`. For one application's full timeline:
`?entity_type=LoanApplication&entity_id=<id>`.

`details` never holds secrets (passwords, tokens, TOTP secrets) or document
contents — documents are referenced by id. Changed customer values (income,
contact details, referees) are not copied into the ledger: the customer's
response entry lists the field names and response ids, and the old/new values
live once, immutably, in `information_responses.field_changes`.

Loan Officer workflow actions (all `entity_type=LoanApplication`):

| action | actor | before/after context in `details` |
|---|---|---|
| `loan_application_officer_review_started` | officer/admin (claim) | `from`/`to` status, `assigned_officer_id`, `previous_assigned_officer_id`, `checklist_opened` |
| `loan_application_reassigned` | admin | `from_officer_id`, `to_officer_id` |
| `verification_checklist_opened` | viewer | `item_types` added lazily to an application already in review |
| `verification_item_updated` | officer/admin | `verification_item_id`, `item_type`, `from`/`to` `{status, note}` |
| `loan_application_customer_action_requested` | officer/admin | `from`/`to` status, `requests[]` (type, reason, required document/information, internal note) |
| `customer_action_required_notification` | officer/admin | `request_ids`, `sent`, `reason` (email outcome) |
| `loan_application_customer_responded` | customer | `from`/`to` status, `request_ids`, `response_ids`, `changed_field_names`, `provided_document_ids` |
| `loan_application_officer_review_resumed` | officer/admin | `from`/`to` status, `reason`, `cancelled_request_ids` |
| `loan_application_recommended_for_approval` / `_for_rejection` | officer/admin | `from`/`to` status, `recommendation_id`, `recommendation`, `note`, `checklist` counts, `credit_score` |
| `customer_history_viewed` | officer/admin | `customer_id`, `application_status` |
| `loan_application_returned_to_officer` | admin | `from`/`to` status, `admin_return_id`, `recommendation_id`, `reason` |
| `loan_application_decision` | admin | `decision`, `note`, `recommendation_id`, `overrides_recommendation`, `same_actor_as_recommender` (early exit: `early_exit`, `from`) |

### Admin: staff accounts

**Admin only.** No Administrator UI yet - use Swagger (`/api/docs`).

| Method | Path | Body | Result |
|---|---|---|---|
| POST | `/api/admin/staff` | `{"email", "full_name", "role": "loan_officer" or "admin", "phone_number"?, "is_active"? (default true)}` | 201 `{user, temporary_password, next_step}`; `customer` role → 400; email taken → 409 |
| POST | `/api/admin/staff/<id>/reset-password` | — | 200 `{user, temporary_password, next_step}`; not a staff account → 400 |

- The temporary password is generated server-side (`secrets.token_urlsafe`),
  only its hash is stored, and it appears in that one response (sent with
  `Cache-Control: no-store`) - never in the audit log, a log line, or any
  other endpoint.
- New accounts start with `totp_enabled=false`, so first login answers
  `mfa_required: "setup"` - the same public MFA flow as registered and
  seeded accounts.
- A reset keeps MFA enrolment (TOTP secret and backup codes): a forgotten
  password isn't a lost authenticator, and the temporary password alone
  still can't sign anyone in. It **does** sign the account out everywhere:
  `token_version` is bumped, so every access, refresh and MFA step token
  issued before the reset is rejected. There is no deactivate endpoint yet -
  see STAFF_ONBOARDING.md → *Known limitations*.
- Audited as `staff_account_created` (`email`, `role`, `is_active`) and
  `staff_password_reset` (`email`, `role`, `mfa_enrolment_kept`, `sessions_revoked`), actor =
  the admin, `entity_type=User`.

### Admin: system parameters

`GET` / `PUT /api/admin/parameters` (**admin only**) — the short, deliberate
list of runtime tunables:

| key | type | seed default |
|---|---|---|
| `min_monthly_income` | money | 200 (credit evaluation, interim model — see below) |
| `max_debt_to_income_ratio` | rate (0–1) | 0.40 (credit evaluation, interim model — see below) |
| `customer_verification_validity_months` | int | 12 (awaiting client confirmation) |

Stored in `system_parameters` (one row per override); config values are the
seed defaults. Each `PUT` is validated and audited (`system_parameters_updated`,
`details.changes = {key: {before, after}}`). The old interest-rate,
loan-amount and loan-term settings were removed (migration f3a7c9e1b5d2):
nothing read them once PRIME's tiers and fixed 14-day term replaced them.

PRIME pricing and the late-penalty tiers are configured as **versions**, not
parameters — see *Administrator API* below. A change applies to applications
submitted afterwards; existing quotes and every loan's terms snapshot never
change.

### Administrator API

All under `/api/admin`, **admin only at the route level** (a loan_officer or
customer token gets 403 on every one — `tests/test_admin_api.py`). Swagger
(`/api/docs`, namespace *admin*) has every request/response model.

| Method | Path | Purpose |
|---|---|---|
| GET | `/admin/queues` | counts: `awaiting_decision`, `awaiting_disbursement`, `active_loans`, `due_today`, `due_this_week`, `overdue`, `repayments_awaiting_verification` |
| GET | `/admin/queues/<queue>?page&per_page` | one queue, filtered and paged in SQL; `kind` = application / loan / repayment |
| GET | `/admin/applications/<id>` | final review: customer + verification, application, officer recommendation records, checklist, credit notes, locked quote |
| GET | `/admin/applications/<id>/customer-history` | the officer's customer-history logic; admins aren't limited to open applications |
| POST | `/admin/applications/<id>/approve` | `{note?}` → AWAITING_DISBURSEMENT (from RECOMMENDED_* or ADMIN_REVIEW); creates nothing |
| POST | `/admin/applications/<id>/reject` | `{reason}` required |
| POST | `/admin/applications/<id>/return-to-officer` | `{reason}` required → RETURNED_TO_OFFICER |
| POST | `/admin/applications/<id>/disbursement-evidence` | multipart `file` → a `disbursement_evidence` document under the customer |
| POST | `/admin/applications/<id>/disbursement` | `{method, reference, disbursed_at?, evidence_document_id?, note?}` → one transaction: Disbursement, terms snapshot, ORIGINAL_OBLIGATION, loan ACTIVE, application DISBURSED |
| GET | `/admin/loans?status=open\|closed\|all` | loans with ledger-derived balances |
| GET | `/admin/loans/<id>` | terms, ledger balance, disbursement, closure, full ledger, payments, audit history |
| POST | `/admin/loans/<id>/write-off` | `{reason}` required → CLOSED / defaulted + closure record |
| GET | `/admin/repayments?status=awaiting\|verified\|rejected\|all` | reported payments with the loan's outstanding balance |
| POST | `/admin/repayments/<id>/verify` | one transaction: VERIFIED + ledger entry (+ automatic closure at zero); twice → 409; above the balance → 409 |
| POST | `/admin/repayments/<id>/reject` | `{reason}` required; no ledger entry |
| GET/POST | `/admin/pricing` | PRIME tier versions (POST `{tiers:[{category,min_amount,max_amount,interest_rate}], note?}`) |
| GET/POST | `/admin/penalty-policy` | late-penalty versions (POST `{tiers:[{days_late,pct_of_original_interest}], note?}`) |
| GET | `/admin/analytics?from&to` | every metric `{value, definition}`; principal, interest, expected repayment, verified cash and outstanding are separate |

The audit-log query stays at `GET /api/reports/audit-logs` (admin; filters
`actor_id`, `actor_role`, `action`, `entity_type`, `entity_id`, `date_from`,
`date_to`).

**Loan closure is automatic**: verifying the payment that brings the ledger
balance to zero closes the loan (CLOSED / `paid_in_full`) and writes its
`loan_closures` record in the same transaction. There is no close endpoint.

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
python -m pytest
```

(`python -m pytest`, not bare `pytest` — `tests/` has no `__init__.py`, so a
bare `pytest` inserts `tests/` itself onto `sys.path` instead of the repo
root and `conftest.py`'s `from app import create_app` fails with
`ModuleNotFoundError`. `python -m` always puts the cwd on `sys.path` first,
which sidesteps that. Same reason `.github/workflows/ci.yml` uses `python -m
pytest`.)

`pytest` runs against an in-memory SQLite DB (`TestingConfig`) built with
`db.create_all()` — no migration, no Supabase, no email. Coverage:

| File | What it locks down |
|---|---|
| `tests/test_auth_flow.py` | full register → MFA setup → verify-setup → login → MFA verify-login → `/me`; login-before-MFA rejected; wrong password 401; backup code single-use; scoped token can't call the API |
| `tests/test_loan_lifecycle.py` | apply → officer review → approve → repay → loan `completed`; schedule sums to `total_repayable`; duplicate/over-limit applications rejected; amortization installment counts |
| `tests/test_credit_evaluation.py` | interim credit model: missing/boundary income, employment status, debt-to-income zones, membership tenure, on-time vs. overdue repayment history, affordability-capped `max_eligible_amount`, graceful degradation with no matching application |
| `tests/test_rbac.py` | customer→officer endpoint = 403, officer→admin = 403, no token = 401, dashboard shape differs by role |
| `tests/test_rate_limiting.py` | login/mfa endpoints actually throttle (not just decorated); 429 body is generic and identical for a real vs. nonexistent email; per-IP scoping; well-behaved use is unaffected |
| `tests/test_scheduled_jobs.py` | daily maintenance jobs: backdated installments flip to `overdue` (single row and multi-row/multi-loan batches) and are audited; idempotent re-runs; due-soon reminders match/exclude correctly by window/loan-status/paid-state; the on-read overdue safety net still works standalone |
| `tests/test_docs_swagger.py` | every B2–B5 endpoint present in `/api/swagger.json` with a body model, a documented 2xx response model, and Bearer security |

## Deployment

See [DEPLOYMENT.md](DEPLOYMENT.md) — Railway `Procfile` (gunicorn `web` +
`flask db upgrade` `release`), `.python-version`, and the full required-env-var
table.
