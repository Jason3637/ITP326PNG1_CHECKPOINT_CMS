# Prime's Vault Backend — Phase B Handoff

Status as of 2026-09-08. Branch `Develop` @ `8f3b8ee`, pushed to GitHub.

## 1. What's built and working

A Flask REST API (application-factory pattern) covering the whole architecture
diagram's backend. Every phase was verified end-to-end against the live Supabase
database before moving on; there is now also an automated pytest suite.

| Area | Endpoints | State |
|---|---|---|
| **Auth** (`/api/auth/*`) | register, mfa/setup, mfa/verify-setup, login, mfa/verify-login, refresh, me | Complete. MFA (TOTP) mandatory for every role. Scoped tokens gate each step. JWT carries a `role` claim. Backup codes (hashed, single-use). Every attempt audited. |
| **Members** (`/api/users/*`) | profile, documents (upload/list/download), member documents (staff) | Complete. |
| **Loans** (`/api/loans/*`) | apply, applications (review), applications/{id}/decision, mine | Complete. Apply → credit check → officer approve/reject → on approve: price + disburse + generate repayment schedule. |
| **Accounts** (`/api/accounts/summary`) | dashboard read model | Complete. |
| **Payments** (`/api/payments/*`) | repay, loan/{id} | Complete. Records payment against an installment, rolls status up to loan completion. |
| **Reports** (`/api/reports/*`) | dashboard (role-aware, Chart.js-shaped), audit-logs (admin, paginated/filterable) | Complete. |
| **Admin** (`/api/admin/parameters`) | GET/PUT runtime tunables | Complete. Interest rate & loan limits editable live (no redeploy). |
| **Notifications** | — (event-driven + `scripts/send_due_reminders.py`) | Complete, Zoho SMTP. Disabled until credentials set. Never carries auth codes. |

**Data model** (Postgres, 2 Alembic migrations): users, mfa_backup_codes,
loan_applications, loans, repayment_schedules, payment_transactions, documents,
audit_logs, system_parameters.

**Infra**: Supabase Postgres (session pooler), Supabase Storage (private bucket,
signed-URL downloads — file bytes never in Postgres), CORS for the separate
Next.js origin, gunicorn + `Procfile` for Railway.

**Tests**: `pytest` — 29 tests, in-memory SQLite, no external deps. Covers the
full auth flow, a loan lifecycle, RBAC rejection, and Swagger coverage.

**Docs**: `/api/docs` (Swagger UI) — every endpoint has typed request and
response models (35 schema definitions). `BACKEND.md` (reference),
`DEPLOYMENT.md` (Railway + env vars), this file.

## 2. What's mocked, simplified, or deferred

| Thing | Current state | Why it matters |
|---|---|---|
| **Credit evaluation** | `credit_evaluation.py` — a transparent placeholder heuristic (amount vs. cap, term length, prior-loan history, account standing → score 0–100). Flagged in code and in every stored result (`"algorithm": "placeholder-v1"`). | Must be replaced with Prime's Vault's real lending criteria (income verification, affordability ratios, guarantor rules). It never auto-decides — an officer always approves/rejects. |
| **Disbursement** | Approval == disbursement (`disbursed_at = now` on approve). No separate "funds released" step or bank integration. | If disbursement is a distinct real-world event, add a `disburse` transition. |
| **Payments** | Marked `completed` immediately. No payment gateway — built for manual/cash entry by staff or self-report by customers. | When a gateway (card, mobile money) is added: create the transaction as `pending`, settle on callback. |
| **Overdue status** | Installments are only flipped to `overdue` by reporting logic on read (`due_date < today AND not paid`); the stored `overdue` enum value is never written by a job yet. | Wire a daily job to persist overdue status + trigger dunning, alongside `send_due_reminders.py`. |
| **Repayment reminders** | `scripts/send_due_reminders.py` exists but nothing schedules it. | Add a Railway Cron service running it daily. |
| **Notifications** | Off by default (`NOTIFICATIONS_ENABLED=false`); plain-text emails only. | Set Zoho credentials to enable. HTML templates optional. |
| **Staff onboarding** | `loan_officer` / `admin` accounts must be inserted directly into the DB (no API). Deliberate — public registration is customer-only. | Fine for a small team; add an admin-only "create staff user" endpoint if needed. |
| **Rate limiting** | None on auth endpoints. | Add before public launch (e.g. Flask-Limiter) to slow credential/TOTP brute force. |
| **Tests** | Happy paths + key rejections. No load tests, no storage/notification integration tests (those paths were verified manually). | Expand coverage over time; add CI. |
| **Python version** | Dev + `.python-version` pin = 3.14. | If Railway's builder lacks 3.14, drop to `3.12` (code is 3.11+ compatible). |

## 3. What should happen next

1. **Connect the Next.js frontend to these real endpoints.** The frontend
   currently runs on mock data. Point its API base URL at the deployed backend,
   set `CORS_ORIGINS` to the frontend's domain, and replace mock fetches with:
   - the multi-step auth flow (see `BACKEND.md` → *Authentication flow*, with curl
     examples for every step),
   - `/api/reports/dashboard` for the Reports/Analytics screen (already shaped for
     Chart.js — `{labels, series:[{name,data}]}` per chart block),
   - `/api/accounts/summary` for the customer account screen,
   - `/api/loans/*` for the application + officer review screens.
2. **Deploy to Railway** following `DEPLOYMENT.md`; set all required env vars
   (especially `MFA_ENCRYPTION_KEY` and `JWT_SECRET_KEY` — generate fresh, don't
   reuse dev values), confirm `/health` and `/api/docs`.
3. **Seed staff accounts** (one admin, loan officers) directly in the DB.
4. **Replace the credit evaluation algorithm** once the client provides real
   lending criteria — it's isolated in one function.
5. **Schedule** `send_due_reminders.py` (Railway Cron) and enable notifications.
6. **Add rate limiting** on `/api/auth/*` before going live.
7. **Rotate the Supabase `service_role` key** if the dev value was shared in
   plaintext anywhere; it's only needed server-side.
