"""Append-only audit trail: writer + admin query helper."""

from datetime import datetime, time, timezone
from typing import Any

from flask import has_request_context, request

from app.extensions import db
from app.models import AuditLog


def client_ip() -> str | None:
    """Best-effort client IP, honoring a single proxy hop via X-Forwarded-For.

    Returns None outside a request context (system / CLI / scheduled actions).
    """
    if not has_request_context():
        return None
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr


def record(
    action: str,
    *,
    actor_id: int | None = None,
    entity_type: str | None = None,
    entity_id: Any | None = None,
    details: dict | None = None,
    commit: bool = True,
) -> AuditLog:
    """Add an AuditLog row. Pass ``commit=False`` to batch it with other writes."""
    entry = AuditLog(
        actor_id=actor_id,
        action=action,
        entity_type=entity_type or "auth",
        entity_id=None if entity_id is None else str(entity_id),
        details=details,
        ip_address=client_ip(),
    )
    db.session.add(entry)
    if commit:
        db.session.commit()
    return entry


def _parse_date(value: str, *, end_of_day: bool = False) -> datetime:
    """Parse an ISO date or datetime string to an aware UTC datetime."""
    try:
        if len(value) == 10:  # YYYY-MM-DD
            d = datetime.strptime(value, "%Y-%m-%d").date()
            t = time.max if end_of_day else time.min
            return datetime.combine(d, t, tzinfo=timezone.utc)
        dt = datetime.fromisoformat(value)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        raise ValueError(f"Invalid date '{value}'. Use YYYY-MM-DD or ISO 8601.")


def query_logs(
    *,
    page: int = 1,
    per_page: int = 50,
    actor_id: int | None = None,
    action: str | None = None,
    entity_type: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict:
    """Paginated, filtered view of the audit ledger (admin only)."""
    page = max(1, int(page))
    per_page = max(1, min(int(per_page), 200))

    q = AuditLog.query
    if actor_id is not None:
        q = q.filter(AuditLog.actor_id == actor_id)
    if action:
        q = q.filter(AuditLog.action == action)
    if entity_type:
        q = q.filter(AuditLog.entity_type == entity_type)
    if date_from:
        q = q.filter(AuditLog.created_at >= _parse_date(date_from))
    if date_to:
        q = q.filter(AuditLog.created_at <= _parse_date(date_to, end_of_day=True))

    q = q.order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
    pagination = q.paginate(page=page, per_page=per_page, error_out=False)

    return {
        "page": pagination.page,
        "per_page": pagination.per_page,
        "total": pagination.total,
        "pages": pagination.pages,
        "items": [
            {
                "id": r.id,
                "actor_id": r.actor_id,
                "action": r.action,
                "entity_type": r.entity_type,
                "entity_id": r.entity_id,
                "details": r.details,
                "ip_address": r.ip_address,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in pagination.items
        ],
    }
