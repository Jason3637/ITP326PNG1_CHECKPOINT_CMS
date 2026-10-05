"""System parameters - the short, deliberate list of runtime tunables an
admin may change live (``PUT /api/admin/parameters``), with config fallback.

PRIME pricing and the late-penalty tiers are configurable too, but as
versioned tables (app/services/pricing_policy.py), not here. The old
interest-rate / loan-amount / loan-term settings were removed: nothing read
them once PRIME's fixed tiers and 14-day term replaced them. Nothing here
ever touches an existing loan's terms snapshot.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from flask import current_app

from app.extensions import db
from app.models import SystemParameter

from . import audit
from .errors import ServiceError

# key -> (config seed key, python type, human description)
_SPEC: dict[str, tuple[str, str, str]] = {
    "min_monthly_income": (
        "MIN_MONTHLY_INCOME",
        "money",
        "Minimum self-reported monthly income to qualify (credit evaluation, interim model).",
    ),
    "max_debt_to_income_ratio": (
        "MAX_DEBT_TO_INCOME_RATIO",
        "rate",
        "Max (existing debt + new installment) / income, as a fraction (credit evaluation, interim model).",
    ),
    "customer_verification_validity_months": (
        "CUSTOMER_VERIFICATION_VALIDITY_MONTHS",
        "int",
        "Months a customer verification stays valid (capped at the ID's expiry). "
        "12 is an engineering default awaiting Prime's Vault confirmation.",
    ),
}

PARAMETER_KEYS = tuple(_SPEC)


def _coerce(key: str, raw: str):
    kind = _SPEC[key][1]
    try:
        if kind == "int":
            return int(raw)
        return Decimal(str(raw))
    except (InvalidOperation, ValueError):
        raise ServiceError(f"Parameter '{key}' has an invalid value: {raw!r}.")


def _validate(key: str, value) -> str:
    kind = _SPEC[key][1]
    if kind == "int":
        try:
            v = int(value)
        except (TypeError, ValueError):
            raise ServiceError(f"'{key}' must be an integer.")
        if v < 1:
            raise ServiceError(f"'{key}' must be >= 1.")
        return str(v)
    try:
        v = Decimal(str(value))
    except (InvalidOperation, TypeError):
        raise ServiceError(f"'{key}' must be a number.")
    if kind == "rate" and not (Decimal("0") <= v < Decimal("1")):
        raise ServiceError(f"'{key}' must be a fraction between 0 and 1 (e.g. 0.18).")
    if kind == "money" and v <= 0:
        raise ServiceError(f"'{key}' must be positive.")
    return format(v, "f")


def get_value(key: str):
    """Effective value for one key: DB override if present, else config seed."""
    if key not in _SPEC:
        raise ServiceError(f"Unknown parameter '{key}'.")
    row = db.session.get(SystemParameter, key)
    if row is not None:
        return _coerce(key, row.value)
    return current_app.config[_SPEC[key][0]]


def get_effective() -> dict:
    """All parameters with their current values and provenance."""
    overrides = {r.key: r for r in SystemParameter.query.all()}
    out = {}
    for key, (cfg_key, kind, desc) in _SPEC.items():
        row = overrides.get(key)
        value = _coerce(key, row.value) if row else current_app.config[cfg_key]
        out[key] = {
            "value": int(value) if kind == "int" else float(value),
            "type": kind,
            "description": desc,
            "source": "override" if row else "default",
            "updated_at": row.updated_at.isoformat() if row and row.updated_at else None,
            "updated_by": row.updated_by if row else None,
        }
    return out


def update(changes: dict, actor_id: int | None) -> dict:
    from app.models import User
    from app.models.enums import UserRole

    # Admin only - re-checked from the database role, not just the route.
    actor = db.session.get(User, actor_id) if actor_id is not None else None
    if actor is None or actor.role != UserRole.ADMIN:
        raise ServiceError("Only an administrator can change system parameters.", 403)
    if not isinstance(changes, dict) or not changes:
        raise ServiceError("Provide at least one parameter to update.")
    unknown = set(changes) - set(_SPEC)
    if unknown:
        raise ServiceError(f"Unknown parameter(s): {', '.join(sorted(unknown))}.")

    before = {k: v["value"] for k, v in get_effective().items()}
    applied = {}
    for key, value in changes.items():
        normalized = _validate(key, value)
        row = db.session.get(SystemParameter, key)
        if row is None:
            row = SystemParameter(key=key, description=_SPEC[key][2])
            db.session.add(row)
        row.value = normalized
        row.updated_by = actor_id
        applied[key] = normalized

    audit.record(
        "system_parameters_updated",
        actor_id=actor_id,
        entity_type="SystemParameter",
        entity_id=",".join(sorted(applied)),
        details={
            "changes": {
                k: {"before": before[k], "after": float(v) if _SPEC[k][1] != "int" else int(v)}
                for k, v in applied.items()
            }
        },
        commit=False,
    )
    db.session.commit()
    return get_effective()
