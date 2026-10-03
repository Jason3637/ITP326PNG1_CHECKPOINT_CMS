"""Customer-level verification - created from the Loan Officer checklist,
carried over to the customer's later applications, and invalidated (never
edited) when it stops being trustworthy.

Lifecycle:
  * CREATE: when an application's "Age 18+ verified" and "Valid ID checked"
    items are both VERIFIED (each with its evidence - DOB / which ID), a
    CustomerVerification is built from them. verified_by is the officer who
    completed the second of the two checks. Any older VERIFIED row for the
    customer is invalidated first (SUPERSEDED, or EXPIRED if it had lapsed) -
    rows are append-only and at most one per customer may be VERIFIED.
  * CARRY OVER: when a later application's checklist opens and the customer
    has a current verification, those two items are marked VERIFIED from it
    (linked via customer_verification_id) instead of being redone.
  * INVALIDATE, with an explicit reason:
      EXPIRED              valid_until passed (daily job; also lazily)
      INFORMATION_CHANGED  an application's confirmed name/email/phone no
                           longer matches what was verified
      STAFF_REQUESTED      an officer asked for re-verification, or undid one
                           of the two checks the verification came from
      POLICY_UPDATED       verified under an older CUSTOMER_VERIFICATION_POLICY_VERSION
      SUPERSEDED           the customer was verified again

The "Verified customer" flag on the review screen is exactly current():
a VERIFIED row whose valid_until hasn't passed - nothing else.
"""

from __future__ import annotations

import calendar
from datetime import date, datetime, timezone

from flask import current_app

from app.extensions import db
from app.models import CustomerVerification, LoanApplication, VerificationItem
from app.models.enums import (
    CustomerVerificationInvalidationReason as Reason,
    CustomerVerificationStatus,
    VerificationItemStatus,
)

from . import audit, parameters
from .errors import ServiceError

# The two checklist items a customer-level verification is made of.
AGE_ITEM = "age_18_plus"
ID_ITEM = "valid_id"
EVIDENCE_ITEMS = (AGE_ITEM, ID_ITEM)


# ------------------------------------------------------------------ queries
def current(user_id: int, *, today: date | None = None) -> CustomerVerification | None:
    """The customer's VERIFIED, not-yet-expired verification, if any."""
    row = _verified_row(user_id)
    if row is None or row.valid_until < (today or date.today()):
        return None
    return row


def _verified_row(user_id: int) -> CustomerVerification | None:
    """The VERIFIED row, even if it has lapsed (not yet swept to EXPIRED)."""
    return CustomerVerification.query.filter_by(
        user_id=user_id, status=CustomerVerificationStatus.VERIFIED
    ).first()


# ------------------------------------------------------------- invalidation
def invalidate(row: CustomerVerification, reason: Reason, *, actor_id: int | None, note: str | None):
    """Mark one verification INVALIDATED. Flushes, doesn't commit."""
    if row.status != CustomerVerificationStatus.VERIFIED:
        return row
    now = datetime.now(timezone.utc)
    with db.session.no_autoflush:  # CHECK ties status to invalidated_at/reason
        row.status = CustomerVerificationStatus.INVALIDATED
        row.invalidated_at = now
        row.invalidated_by = actor_id
        row.invalidation_reason = reason
        row.invalidation_note = (note or "")[:1000] or None
    audit.record(
        "customer_verification_invalidated",
        actor_id=actor_id,
        entity_type="CustomerVerification",
        entity_id=row.id,
        details={"user_id": row.user_id, "reason": reason.value, "note": row.invalidation_note},
        commit=False,
    )
    db.session.flush()
    return row


def expire_due(today: date | None = None) -> list[CustomerVerification]:
    """Daily sweep: VERIFIED rows past valid_until -> INVALIDATED/EXPIRED.
    Flushes; the caller commits."""
    today = today or date.today()
    rows = (
        CustomerVerification.query.filter_by(status=CustomerVerificationStatus.VERIFIED)
        .filter(CustomerVerification.valid_until < today)
        .all()
    )
    for row in rows:
        invalidate(row, Reason.EXPIRED, actor_id=None, note=f"Valid until {row.valid_until.isoformat()}.")
    return rows


def _norm_text(value: str | None) -> str:
    return " ".join((value or "").split()).casefold()


def _norm_phone(value: str | None) -> str:
    return "".join(ch for ch in (value or "") if ch.isdigit())


def check_information_changed(application: LoanApplication) -> CustomerVerification | None:
    """INFORMATION_CHANGED: the application's confirmed contact details no
    longer match the ones the current verification was made against.
    Returns the invalidated row, if any. Flushes; the caller commits."""
    row = _verified_row(application.user_id)
    if row is None:
        return None
    changed = []
    if application.confirmed_full_name and _norm_text(application.confirmed_full_name) != _norm_text(
        row.verified_full_name
    ):
        changed.append("full name")
    if application.confirmed_email and _norm_text(application.confirmed_email) != _norm_text(row.verified_email):
        changed.append("email")
    if (
        application.confirmed_phone_number
        and row.verified_phone_number
        and _norm_phone(application.confirmed_phone_number) != _norm_phone(row.verified_phone_number)
    ):
        changed.append("phone number")
    if not changed:
        return None
    return invalidate(
        row,
        Reason.INFORMATION_CHANGED,
        actor_id=None,
        note=f"Application #{application.id} has a different {', '.join(changed)} from the verified details.",
    )


def request_reverification(application: LoanApplication, actor, note: str) -> CustomerVerification:
    """STAFF_REQUESTED, from an application's review screen. Also re-opens
    that application's two identity checks if they came from this
    verification, so they're done again. Flushes; the caller commits."""
    note = (note or "").strip()
    if not note:
        raise ServiceError("note is required - say why the customer needs re-verifying.")
    if len(note) > 1000:
        raise ServiceError("note must be at most 1000 characters.")
    row = current(application.user_id)
    if row is None:
        raise ServiceError("This customer has no current verification to invalidate.", 409)
    actor_id = actor.id
    invalidate(row, Reason.STAFF_REQUESTED, actor_id=actor_id, note=note)
    _reopen_items_from(application, row.id)
    return row


def invalidate_outdated_policy(actor) -> int:
    """POLICY_UPDATED: every VERIFIED row made under an older policy version.
    Flushes; the caller commits. Returns how many were invalidated."""
    version = current_app.config["CUSTOMER_VERIFICATION_POLICY_VERSION"]
    rows = (
        CustomerVerification.query.filter_by(status=CustomerVerificationStatus.VERIFIED)
        .filter(CustomerVerification.policy_version != version)
        .all()
    )
    actor_id = actor.id
    for row in rows:
        invalidate(
            row,
            Reason.POLICY_UPDATED,
            actor_id=actor_id,
            note=f"Verified under policy {row.policy_version}; current policy is {version}.",
        )
    return len(rows)


def _reopen_items_from(application: LoanApplication, verification_id: int) -> None:
    for item in application.verification_items:
        if item.item_type in EVIDENCE_ITEMS and item.customer_verification_id == verification_id:
            with db.session.no_autoflush:
                item.status = VerificationItemStatus.PENDING
                item.checked_by = None
                item.checked_at = None
                item.evidence = None
                item.customer_verification_id = None
                item.note = "Re-verification required."
    db.session.flush()


# ----------------------------------------------------------------- creation
def _add_months(d: date, months: int) -> date:
    month_index = d.month - 1 + months
    year, month = d.year + month_index // 12, month_index % 12 + 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def _items(application: LoanApplication) -> dict[str, VerificationItem]:
    return {i.item_type: i for i in application.verification_items}


def create_from_checklist(application: LoanApplication, actor) -> CustomerVerification:
    """Build the verification from the application's two VERIFIED identity
    checks. Flushes; the caller commits."""
    items = _items(application)
    age, id_check = items[AGE_ITEM], items[ID_ITEM]
    actor_id = actor.id
    today = date.today()

    replaced = _verified_row(application.user_id)
    if replaced is not None:
        lapsed = replaced.valid_until < today
        invalidate(
            replaced,
            Reason.EXPIRED if lapsed else Reason.SUPERSEDED,
            actor_id=None if lapsed else actor_id,
            note=f"Re-verified on application #{application.id}.",
        )

    expiry = id_check.evidence.get("id_expiry_date")
    expiry_date = date.fromisoformat(expiry) if expiry else None
    months = int(parameters.get_value("customer_verification_validity_months"))
    valid_until = _add_months(today, months)
    if expiry_date and expiry_date < valid_until:
        valid_until = expiry_date

    applicant = application.applicant
    row = CustomerVerification(
        user_id=application.user_id,
        status=CustomerVerificationStatus.VERIFIED,
        verified_by=actor_id,
        verified_at=datetime.now(timezone.utc),
        id_document_id=id_check.evidence["id_document_id"],
        date_of_birth=date.fromisoformat(age.evidence["date_of_birth"]),
        id_expiry_date=expiry_date,
        verified_full_name=application.confirmed_full_name or applicant.full_name,
        verified_email=application.confirmed_email or applicant.email,
        verified_phone_number=application.confirmed_phone_number or applicant.phone_number,
        valid_until=valid_until,
        policy_version=current_app.config["CUSTOMER_VERIFICATION_POLICY_VERSION"],
        source_application_id=application.id,
    )
    db.session.add(row)
    db.session.flush()
    age.customer_verification_id = row.id
    id_check.customer_verification_id = row.id
    audit.record(
        "customer_verified",
        actor_id=actor_id,
        entity_type="CustomerVerification",
        entity_id=row.id,
        details={
            "user_id": application.user_id,
            "application_id": application.id,
            "valid_until": valid_until.isoformat(),
            "id_document_id": row.id_document_id,
            "age_checked_by": age.checked_by,
            "id_checked_by": id_check.checked_by,
            "replaced_verification_id": replaced.id if replaced else None,
        },
        commit=False,
    )
    db.session.flush()
    return row


def sync_after_item_change(
    application: LoanApplication,
    actor,
    item: VerificationItem,
    *,
    previous_status: VerificationItemStatus,
    previous_verification_id: int | None,
) -> CustomerVerification | None:
    """Called after one checklist item changed (same transaction).

    * Both identity checks now VERIFIED with evidence, and not both already
      backed by a verification -> create one (superseding any older one).
    * An identity check that a current verification came from moved away
      from VERIFIED -> that verification is invalidated (STAFF_REQUESTED).
    """
    if item.item_type not in EVIDENCE_ITEMS:
        return None

    if (
        previous_verification_id is not None
        and previous_status == VerificationItemStatus.VERIFIED
        and item.status != VerificationItemStatus.VERIFIED
    ):
        row = db.session.get(CustomerVerification, previous_verification_id)
        if row is not None and row.status == CustomerVerificationStatus.VERIFIED:
            label = "Age 18+ check" if item.item_type == AGE_ITEM else "ID check"
            invalidate(
                row,
                Reason.STAFF_REQUESTED,
                actor_id=actor.id,
                note=f"{label} changed to {item.status.value} on application #{application.id}.",
            )
        return None

    items = _items(application)
    age, id_check = items.get(AGE_ITEM), items.get(ID_ITEM)
    both_verified = all(
        i is not None and i.status == VerificationItemStatus.VERIFIED and i.evidence for i in (age, id_check)
    )
    if both_verified and (age.customer_verification_id is None or id_check.customer_verification_id is None):
        return create_from_checklist(application, actor)
    return None


# ---------------------------------------------------------------- carry-over
def carry_over(application: LoanApplication, actor) -> list[VerificationItem]:
    """Mark a later application's PENDING identity checks VERIFIED from the
    customer's current verification - no need to redo them. Flushes; the
    caller commits. Returns the items it changed."""
    row = current(application.user_id)
    if row is None:
        return []
    evidence = {
        AGE_ITEM: {"date_of_birth": row.date_of_birth.isoformat()},
        ID_ITEM: {
            "id_document_id": row.id_document_id,
            "id_document_type": (
                row.id_document.id_document_type.value
                if row.id_document is not None and row.id_document.id_document_type
                else None
            ),
            "id_expiry_date": row.id_expiry_date.isoformat() if row.id_expiry_date else None,
        },
    }
    note = (
        f"Carried over from customer verification #{row.id} "
        f"(verified {row.verified_at.date().isoformat()}, valid until {row.valid_until.isoformat()})."
    )
    changed = []
    now = datetime.now(timezone.utc)
    verifier_id = row.verified_by
    for item in application.verification_items:
        if item.item_type in EVIDENCE_ITEMS and item.status == VerificationItemStatus.PENDING:
            with db.session.no_autoflush:
                item.status = VerificationItemStatus.VERIFIED
                item.checked_by = verifier_id
                item.checked_at = now
                item.evidence = evidence[item.item_type]
                item.customer_verification_id = row.id
                item.note = note
            changed.append(item)
    if changed:
        audit.record(
            "verification_items_carried_over",
            actor_id=actor.id if actor is not None else None,
            entity_type="LoanApplication",
            entity_id=application.id,
            details={"customer_verification_id": row.id, "item_types": [i.item_type for i in changed]},
            commit=False,
        )
        db.session.flush()
    return changed
