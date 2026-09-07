"""System parameters - runtime-editable tunables with config fallback.

Every lending value that Phase B3 read straight off ``current_app.config`` is
routed through here so an admin can change it live via
``PUT /api/admin/parameters`` without a redeploy.
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
    "default_annual_interest_rate": (
        "DEFAULT_ANNUAL_INTEREST_RATE",
        "rate",
        "Default annual interest rate as a fraction (0.18 = 18% APR).",
    ),
    "min_loan_amount": ("MIN_LOAN_AMOUNT", "money", "Minimum loan amount."),
    "max_loan_amount": ("MAX_LOAN_AMOUNT", "money", "Maximum loan amount."),
    "min_loan_term_months": ("MIN_LOAN_TERM_MONTHS", "int", "Minimum loan term in months."),
    "max_loan_term_months": ("MAX_LOAN_TERM_MONTHS", "int", "Maximum loan term in months."),
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
    if not isinstance(changes, dict) or not changes:
        raise ServiceError("Provide at least one parameter to update.")
    unknown = set(changes) - set(_SPEC)
    if unknown:
        raise ServiceError(f"Unknown parameter(s): {', '.join(sorted(unknown))}.")

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
        details={"changes": applied},
        commit=False,
    )
    db.session.commit()
    return get_effective()
