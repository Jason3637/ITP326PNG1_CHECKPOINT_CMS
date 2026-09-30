"""Document handling - upload to Supabase Storage, signed-URL retrieval.

Only ``Document.storage_path`` is persisted in Postgres; the file bytes live in
the private Supabase bucket. Downloads are handed out as short-lived signed URLs
(the diagram's "Temporary Signed URL Access"), never streamed through Flask.
"""

from __future__ import annotations

import re
import uuid
from decimal import Decimal

from flask import current_app

from app.extensions import db
from app.models import Document, User
from app.models.enums import DocumentType, UserRole
from app.storage import supabase_storage

from . import audit
from .errors import ServiceError

# Accepted MIME types -> canonical extension.
ALLOWED_TYPES: dict[str, str] = {
    "application/pdf": ".pdf",
    "image/jpeg": ".jpg",
    "image/png": ".png",
}

_STAFF_ROLES = (UserRole.LOAN_OFFICER, UserRole.ADMIN)
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")

# Named rule (item 8): amounts at or above this require a PROOF_OF_INCOME
# document before an application can be submitted. Named and isolated here
# rather than an inline magic number so it's easy to find/adjust later.
PROOF_OF_INCOME_REQUIRED_ABOVE = Decimal("1000")


def proof_of_income_required(amount_requested) -> bool:
    """Rule: PROOF_OF_INCOME_REQUIRED_ABOVE. True when the requested amount
    is >= PROOF_OF_INCOME_REQUIRED_ABOVE, i.e. a Proof of Income document
    must already be uploaded and linked before the application can submit.
    """
    return Decimal(str(amount_requested)) >= PROOF_OF_INCOME_REQUIRED_ABOVE


def _safe_filename(raw: str | None, extension: str) -> str:
    base = (raw or "upload").rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    base = _SAFE_NAME.sub("_", base).strip("._") or "upload"
    if "." in base:
        base = base.rsplit(".", 1)[0]
    return f"{base[:60]}{extension}"


def _can_access(document: Document, requester: User) -> bool:
    return requester.id == document.user_id or requester.role in _STAFF_ROLES


def _supersede_prior_versions(new_document: Document, actor: User) -> None:
    """Versioning policy: superseded-but-kept. When `new_document` is linked
    to an application, any other document already linked to that SAME
    application with the SAME document_type is marked superseded (never
    deleted or overwritten) - so the officer view shows only the latest by
    default, but the full history stays available for audit.

    Only applies once a document is linked to a specific application:
    unlinked/orphan documents (loan_application_id IS NULL) have no "current
    for this application" context to supersede within.
    """
    if new_document.loan_application_id is None:
        return
    siblings = Document.query.filter(
        Document.loan_application_id == new_document.loan_application_id,
        Document.document_type == new_document.document_type,
        Document.id != new_document.id,
        Document.superseded_by_id.is_(None),
    ).all()
    for old in siblings:
        old.superseded_by_id = new_document.id
        audit.record(
            "document_superseded",
            actor_id=actor.id,
            entity_type="Document",
            entity_id=old.id,
            details={
                "superseded_by_document_id": new_document.id,
                "document_type": str(new_document.document_type),
                "loan_application_id": new_document.loan_application_id,
            },
            commit=False,
        )


def upload_document(
    owner: User,
    *,
    document_type: str,
    filename: str | None,
    data: bytes,
    content_type: str | None,
    loan_application_id: int | None = None,
) -> Document:
    try:
        doc_type = DocumentType(document_type)
    except ValueError:
        allowed = ", ".join(t.value for t in DocumentType)
        raise ServiceError(f"document_type must be one of: {allowed}.")

    ctype = (content_type or "").split(";")[0].strip().lower()
    if ctype not in ALLOWED_TYPES:
        raise ServiceError(
            f"Unsupported file type '{ctype or 'unknown'}'. Allowed: PDF, JPG, PNG.",
            status_code=415,
        )

    size = len(data or b"")
    max_bytes = current_app.config["DOCUMENT_MAX_BYTES"]
    if size == 0:
        raise ServiceError("Uploaded file is empty.")
    if size > max_bytes:
        raise ServiceError(
            f"File is {size} bytes; the limit is {max_bytes} bytes "
            f"({max_bytes // (1024 * 1024)} MiB).",
            status_code=413,
        )

    if loan_application_id is not None:
        from app.models import LoanApplication

        app_row = db.session.get(LoanApplication, loan_application_id)
        if app_row is None or app_row.user_id != owner.id:
            raise ServiceError("loan_application_id is not valid for this user.", 400)

    extension = ALLOWED_TYPES[ctype]
    safe_name = _safe_filename(filename, extension)
    storage_path = (
        f"users/{owner.id}/{doc_type.value}/{uuid.uuid4().hex[:12]}_{safe_name}"
    )

    supabase_storage.upload_file(storage_path, data, ctype)

    document = Document(
        user_id=owner.id,
        loan_application_id=loan_application_id,
        document_type=doc_type,
        storage_path=storage_path,
    )
    db.session.add(document)
    db.session.flush()
    audit.record(
        "document_uploaded",
        actor_id=owner.id,
        entity_type="Document",
        entity_id=document.id,
        details={
            "document_type": doc_type.value,
            "storage_path": storage_path,
            "content_type": ctype,
            "size_bytes": size,
        },
        commit=False,
    )
    _supersede_prior_versions(document, owner)
    db.session.commit()
    return document


def get_download_url(document_id: int, requester: User) -> dict:
    document = db.session.get(Document, document_id)
    if document is None:
        raise ServiceError("Document not found.", 404)
    if not _can_access(document, requester):
        raise ServiceError("You do not have access to this document.", 403)

    expires_in = current_app.config["SIGNED_URL_EXPIRY_SECONDS"]
    url = supabase_storage.get_signed_url(document.storage_path, expires_in)

    audit.record(
        "document_download",
        actor_id=requester.id,
        entity_type="Document",
        entity_id=document.id,
        details={
            "storage_path": document.storage_path,
            "owner_id": document.user_id,
            "by_staff": requester.role in _STAFF_ROLES and requester.id != document.user_id,
            "expires_in": expires_in,
        },
    )
    return {
        "document_id": document.id,
        "document_type": str(document.document_type),
        "signed_url": url,
        "expires_in_seconds": expires_in,
    }


def serialize(document: Document) -> dict:
    return {
        "id": document.id,
        "user_id": document.user_id,
        "loan_application_id": document.loan_application_id,
        "document_type": str(document.document_type),
        "storage_path": document.storage_path,
        "uploaded_at": document.uploaded_at.isoformat() if document.uploaded_at else None,
        "is_current": document.superseded_by_id is None,
        "superseded_by_id": document.superseded_by_id,
    }


def list_documents(
    owner_id: int, *, document_type: str | None = None, include_superseded: bool = False
) -> list[Document]:
    """By default, only "current" documents (the latest version of each
    type/application) - see _supersede_prior_versions(). Pass
    include_superseded=True for the full version history (audit view).
    """
    query = Document.query.filter_by(user_id=owner_id)
    if document_type:
        try:
            query = query.filter_by(document_type=DocumentType(document_type))
        except ValueError:
            raise ServiceError(f"Unknown document_type '{document_type}'.")
    if not include_superseded:
        query = query.filter(Document.superseded_by_id.is_(None))
    return query.order_by(Document.uploaded_at.desc()).all()


def link_documents_to_application(
    document_ids: list[int], owner: User, application_id: int
) -> list[Document]:
    """Attach previously-uploaded (unlinked-or-owned) documents to an
    application - either a newly created one (submit_application()) or an
    existing one the customer is responding to a CUSTOMER_ACTION_REQUIRED
    request on (respond_to_customer_action()). Superseded-but-kept
    versioning: if a document of the same type is already linked to this
    application, it's marked superseded rather than replaced or duplicated
    (see _supersede_prior_versions()).

    Raises ServiceError if any id doesn't exist or doesn't belong to owner.
    """
    if not document_ids:
        return []
    rows = Document.query.filter(Document.id.in_(document_ids)).all()
    found_ids = {d.id for d in rows}
    missing = set(document_ids) - found_ids
    if missing:
        raise ServiceError(f"Unknown document_id(s): {sorted(missing)}.")
    for row in rows:
        if row.user_id != owner.id:
            raise ServiceError(f"Document {row.id} does not belong to this user.", 403)
    # Ascending id order: within one batch, a later (higher-id, more
    # recently uploaded) document of the same type supersedes an earlier one.
    rows.sort(key=lambda d: d.id)
    for row in rows:
        row.loan_application_id = application_id
    for row in rows:
        _supersede_prior_versions(row, owner)
    return rows


def has_proof_of_income(document_ids: list[int], owner: User) -> bool:
    """Whether any of the given document ids is a CURRENT (not superseded)
    PROOF_OF_INCOME document belonging to owner. Used to enforce
    PROOF_OF_INCOME_REQUIRED_ABOVE.
    """
    if not document_ids:
        return False
    return (
        Document.query.filter(
            Document.id.in_(document_ids),
            Document.user_id == owner.id,
            Document.document_type == DocumentType.PROOF_OF_INCOME,
            Document.superseded_by_id.is_(None),
        ).first()
        is not None
    )
