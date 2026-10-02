"""Loan Officer verification checklist - one VerificationItem row per check
per application, each updated individually with who/when.

The checklist's item types live in ``CHECKLIST`` below, not in a database
enum: adding a check later is one entry here and no migration. ``required``
decides whether the item blocks an approval recommendation for a given
application (e.g. proof of income only above the named threshold).

Role checks are enforced at the API layer (roles_required) and, for "only
the assigned officer or an admin", in loan_processing._require_assignee().
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from app.extensions import db
from app.models import LoanApplication, VerificationItem
from app.models.enums import VerificationItemStatus

from . import audit, documents
from .errors import ServiceError


@dataclass(frozen=True)
class ChecklistItemType:
    key: str
    label: str
    required: Callable[[LoanApplication], bool] = lambda _application: True


CHECKLIST: tuple[ChecklistItemType, ...] = (
    ChecklistItemType("age_18_plus", "Age 18+ verified"),
    ChecklistItemType("valid_id", "Valid ID checked"),
    ChecklistItemType("contact_details", "Contact details checked"),
    ChecklistItemType("employment", "Employment checked"),
    ChecklistItemType("referee", "Referee checked"),
    ChecklistItemType(
        "proof_of_income",
        "Proof of income checked",
        lambda a: documents.proof_of_income_required(a.amount_requested),
    ),
    ChecklistItemType("repayment_history", "Repayment history reviewed"),
    ChecklistItemType("application_consistency", "Application information consistent"),
)
_BY_KEY = {t.key: t for t in CHECKLIST}

# Statuses that count as "done" for an approval recommendation.
_COMPLETE = (VerificationItemStatus.VERIFIED, VerificationItemStatus.NOT_APPLICABLE)
# A note is mandatory when the officer marks an item as a problem or skips it.
_NOTE_REQUIRED = (VerificationItemStatus.FAILED, VerificationItemStatus.NOT_APPLICABLE)


def ensure_checklist(application: LoanApplication) -> list[VerificationItem]:
    """Create a PENDING row for every registered item type the application
    doesn't have yet. Idempotent; flushes but does not commit - the caller
    owns the transaction.
    """
    existing = {item.item_type for item in application.verification_items}
    for item_type in CHECKLIST:
        if item_type.key not in existing:
            application.verification_items.append(
                VerificationItem(item_type=item_type.key, status=VerificationItemStatus.PENDING)
            )
    db.session.flush()
    return application.verification_items


def _item_dict(application: LoanApplication, item: VerificationItem) -> dict:
    item_type = _BY_KEY.get(item.item_type)
    return {
        "item_type": item.item_type,
        # An item type removed from CHECKLIST later still shows (history),
        # but no longer blocks anything.
        "label": item_type.label if item_type else item.item_type,
        "required": bool(item_type and item_type.required(application)),
        "status": str(item.status),
        "note": item.note,
        "checked_by": item.checked_by,
        "checked_by_name": item.checker.full_name if item.checker else None,
        "checked_at": item.checked_at.isoformat() if item.checked_at else None,
        "customer_verification_id": item.customer_verification_id,
    }


def blocking_items(application: LoanApplication) -> list[str]:
    """Item types that stop an approval recommendation: any required item
    not yet verified / not-applicable, and any item marked failed.
    """
    blocking = []
    present = {item.item_type: item for item in application.verification_items}
    for item_type in CHECKLIST:
        item = present.get(item_type.key)
        if item_type.required(application) and (item is None or item.status not in _COMPLETE):
            blocking.append(item_type.key)
    for item in application.verification_items:
        if item.status == VerificationItemStatus.FAILED and item.item_type not in blocking:
            blocking.append(item.item_type)
    return blocking


def serialize_checklist(application: LoanApplication) -> dict:
    order = {t.key: i for i, t in enumerate(CHECKLIST)}
    items = sorted(
        (_item_dict(application, i) for i in application.verification_items),
        key=lambda d: (order.get(d["item_type"], len(order)), d["item_type"]),
    )
    required = [i for i in items if i["required"]]
    blocking = blocking_items(application)
    return {
        "application_id": application.id,
        "started": bool(items),
        "items": items,
        "summary": {
            "total": len(items),
            "required": len(required),
            "required_complete": sum(1 for i in required if i["status"] in _COMPLETE),
            "pending": sum(1 for i in items if i["status"] == VerificationItemStatus.PENDING),
            "failed": sum(1 for i in items if i["status"] == VerificationItemStatus.FAILED),
            "blocking_items": blocking,
            "ready_for_approval_recommendation": bool(items) and not blocking,
        },
    }


def snapshot(application: LoanApplication) -> list[dict]:
    """Frozen copy for OfficerRecommendation.checklist_snapshot."""
    return serialize_checklist(application)["items"]


def update_item(
    application: LoanApplication, actor, item_type: str, *, status, note: str | None = None
) -> VerificationItem:
    """Set ONE checklist item. Records who/when (or clears them when reset to
    pending) and audit-logs the old and new state. Caller has already
    checked application status and assignment; this commits.
    """
    if item_type not in _BY_KEY:
        allowed = ", ".join(_BY_KEY)
        raise ServiceError(f"Unknown checklist item '{item_type}'. Expected one of: {allowed}.")
    try:
        new_status = VerificationItemStatus(status)
    except ValueError:
        allowed = ", ".join(s.value for s in VerificationItemStatus)
        raise ServiceError(f"status must be one of: {allowed}.")
    note = (note or "").strip() or None
    if new_status in _NOTE_REQUIRED and not note:
        raise ServiceError(f"note is required when marking an item {new_status.value}.")
    if note and len(note) > 1000:
        raise ServiceError("note must be at most 1000 characters.")

    ensure_checklist(application)
    item = next(i for i in application.verification_items if i.item_type == item_type)
    old = {"status": str(item.status), "note": item.note}

    item.status = new_status
    item.note = note
    if new_status == VerificationItemStatus.PENDING:
        item.checked_by = None
        item.checked_at = None
    else:
        item.checked_by = actor.id
        item.checked_at = datetime.now(timezone.utc)

    audit.record(
        "verification_item_updated",
        actor_id=actor.id,
        entity_type="LoanApplication",
        entity_id=application.id,
        details={
            "item_type": item_type,
            "from": old,
            "to": {"status": new_status.value, "note": note},
        },
        commit=False,
    )
    db.session.commit()
    return item
