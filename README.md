# ITP326PNG1_CHECKPOINT_CMS

[![CI](https://github.com/Jason3637/ITP326PNG1_CHECKPOINT_CMS/actions/workflows/ci.yml/badge.svg)](https://github.com/Jason3637/ITP326PNG1_CHECKPOINT_CMS/actions/workflows/ci.yml)

This is the CMS repo for our capstone project where we will build a computer and phone app for our client Prime's Vault money lending business to automatically track member savings, make loan steps easier, handle payments, and let members check their own accounts anytime.

## Documentation

- [BACKEND.md](BACKEND.md) — API reference, architecture, auth flow, credit evaluation.
- [DEPLOYMENT.md](DEPLOYMENT.md) — Railway deployment, environment variables.
- [STAFF_ONBOARDING.md](STAFF_ONBOARDING.md) — creating `loan_officer`/`admin` accounts.
- [HANDOFF.md](HANDOFF.md) — dated project status snapshot.

## Credit evaluation status

Loan applications are scored by `app/services/credit_evaluation.py`
(`"algorithm": "interim-v2"` on stored results). This is **not** the client's
final, confirmed lending policy — it's a materially improved interim model
(minimum income, employment status, debt-to-income ratio, membership tenure,
repayment history) built while the real underwriting criteria are pending.
See BACKEND.md → *Credit Evaluation* for exactly what's live and which
thresholds (`min_monthly_income`, `max_debt_to_income_ratio`) still need the
client's real numbers.
