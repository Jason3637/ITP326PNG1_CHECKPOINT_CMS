# Prime's Vault — UAT Test Script

Full customer journey (sign-up through repayment) and staff journey (review,
decision, audit, parameters) for User Acceptance Testing against the live
environments. Walk it top to bottom, in order — several steps hand off
between roles.

A formatted, checkbox-tracked version of this same script is published as an
Artifact: https://claude.ai/artifact/WTTLn2Hy3uyhArqqtNEnKF — use that during
an actual test session; this file is the version-controlled reference copy.

| | |
|---|---|
| App under test | https://primesvault.vercel.app |
| API / Swagger | https://itp326png1checkpointcms-production.up.railway.app/api/docs |
| Currency | PGK — Kina |
| Scoring build tag | `algorithm: interim-v2` |

## Before you start

- An authenticator app (Google Authenticator, Authy, 1Password) — MFA is
  mandatory for every role and is the real TOTP flow, not stubbed out.
- A `loan_officer` test account, already seeded and already through its own
  one-time MFA setup (dev team runs `scripts/seed_staff.py` once — see
  [STAFF_ONBOARDING.md](STAFF_ONBOARDING.md) — not something a tester does).
- An `admin` test account, same as above.

## Two things that look unfinished on purpose

Flagged in advance so feedback on them lands separately from bug reports.
Everything else is fair game to file as a defect.

**Credit-check & loan eligibility — interim, not final.** The scoring behind
every application (`algorithm: interim-v2`) is an engineering placeholder
built from generic lending signals (income floor, debt-to-income, repayment
history, membership tenure) — not Prime's Vault's actual, client-approved
lending policy. Concretely: the loan application form doesn't collect income
or employment yet, so **every application will read as needing manual
review** regardless of how strong it looks. Expected — the system never
auto-decides anyway; a loan officer always makes the final call.

**Manual/staff-entered payments — stand-in, not final.** Repayments are
recorded by a staff member entering an amount and a method (e.g. "cash")
directly, settled immediately. This stands in for a real Bank South Pacific
(BSP) integration that isn't live yet. Don't expect a card field, a BSP
redirect, or a pending/settled distinction.

**Also not yet built (skip — no need to report):** the app's "Applications"
and "Profile" nav tabs have no screen behind them yet, and there is no
staff-facing UI at all for review or admin work — that's why the staff
journey below runs through the API documentation page instead of the app.

## Customer journey (in the app: `primesvault.vercel.app`)

1. **Create an account** — `/signup`. Full name, email, phone, a password
   you haven't used here before. *Expect:* moved to the MFA setup step, not
   logged in yet.
2. **Set up MFA** — scan the QR with your authenticator app, enter the
   6-digit code. *Expect:* MFA enabled, 10 backup codes shown once — **save
   at least one**, it's never shown again.
3. **Log in** — `/login`, email + password, then the current TOTP code.
   *Expect:* land on the Dashboard.
4. **Apply for a loan** — Dashboard → Loans → Apply. Amount, purpose, term
   (months), repayment frequency. *Expect:* an "Application submitted"
   screen with your amount/term and a status. The status will read
   "pending," not an eligibility decision (see above) — note the
   amount/term you used, there's no screen to look this application back up
   later.
5. *Pause — complete Staff journey §A (loan officer review) before
   continuing.*
6. **Confirm the loan is active** — Dashboard → Loans. *Expect:* status
   "active," principal, per-installment amount, full repayment schedule.
7. *Pause — complete Staff journey §B (record a payment) before
   continuing.*
8. **Confirm repayment history updated** — Dashboard → Repayment History.
   *Expect:* the installment staff just paid shows as settled, and the
   payment appears.

## Staff journey (via `/api/docs` — no staff UI exists yet)

Run against Swagger UI: expand an endpoint, "Try it out," fill the body,
Execute. To authorize: run `/auth/login` then `/auth/mfa/verify-login`, copy
the returned `access_token`, click the padlock ("Authorize," top-right), and
paste `Bearer <token>`.

### §A — Review & decide (Loan Officer)

1. Authorize as the loan officer (login → verify-login → Authorize).
2. `GET /api/loans/applications` — *expect:* the customer's application from
   step 4, status "pending."
3. `POST /api/loans/applications/{application_id}/decision` — body
   `{"decision": "approve", "note": "UAT approval"}`. *Expect:* 200, a new
   loan object with `id` and a `repayment_schedule` array — **keep this
   response open**, needed next.

### §B — Record a payment (Loan Officer / Admin)

1. `POST /api/payments/repay` — body `{"repayment_schedule_id": <from A3>,
   "amount": <installment amount from A3>, "payment_method": "cash"}`.
   *Expect:* 201, the installment's status flips to settled.

### §C — Audit & parameters (Admin)

1. Authorize as admin (same login → verify-login → Authorize flow).
2. `GET /api/reports/audit-logs` — *expect:* rows for every action above
   (register, mfa_enabled, login_success, loan_application_submitted,
   loan_application_decision, loan_disbursed, payment_recorded), each with
   an actor, IP, and timestamp.
3. `GET /api/admin/parameters` — *expect:* current values (e.g.
   `default_annual_interest_rate`) with `source: "default"`.
4. `PUT /api/admin/parameters` — body `{"default_annual_interest_rate":
   0.20}` (or any in-range value). *Expect:* 200, the field reads back with
   `source: "override"`.
5. `GET /api/admin/parameters` again — *expect:* the override persisted.

## Where findings go

GitHub Issues on this repo — no separate tracker. Two intake forms
(`.github/ISSUE_TEMPLATE/`) keep the two known-different areas out of the
defect list automatically:

- **[UAT: Defect / bug](../../issues/new?template=uat-defect.yml)** —
  anything that didn't match this script's expected result and isn't one of
  the two flagged areas above. Captures step, role, expected vs. actual,
  repro, severity, screenshot.
- **[UAT: Expected-difference feedback](../../issues/new?template=uat-expected-difference.yml)**
  — reactions to the credit-check scoring or the manual-payment flow
  specifically. Captures which area, what you saw, what it should do
  instead — this is exactly the input needed for those later phases.
