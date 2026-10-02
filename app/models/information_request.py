"""InformationRequest / InformationResponse — the Request More Information
history for an application.

Supersedes the single ``LoanApplication.action_required_note`` column: each
thing an officer asks for is its own row, and the customer's answer to it is
a separate row, so neither the question nor the answer is ever overwritten
by a later round. ``InformationResponse.field_changes`` keeps the old value
of every application field the customer changed, so the application row
itself can still be updated in place without losing history.

Staff references (requested_by, responded_by, cancelled_by) use the default
ON DELETE NO ACTION rather than SET NULL: a history row must always name who
acted, so deleting a user who is still referenced here is refused by the
database. Deleting the customer themselves still works - their applications
cascade away in the same statement, taking these rows with them.
"""

from sqlalchemy import func

from app.extensions import db

from .base import JSONType, pg_enum
from .enums import DocumentType, InformationRequestStatus, InformationRequestType


class InformationRequest(db.Model):
    __tablename__ = "information_requests"
    __table_args__ = (
        # CANCELLED <=> who/when cancelled is recorded.
        db.CheckConstraint(
            "(status = 'cancelled') = (cancelled_by IS NOT NULL AND cancelled_at IS NOT NULL)",
            name="ck_information_requests_cancelled_fields",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    loan_application_id = db.Column(
        db.Integer,
        db.ForeignKey("loan_applications.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    request_type = db.Column(
        pg_enum(InformationRequestType, "information_request_type"), nullable=False
    )
    # Customer-facing: what's needed and why.
    reason = db.Column(db.String(1000), nullable=False)
    required_document_type = db.Column(pg_enum(DocumentType, "document_type"))
    required_information = db.Column(db.String(500))
    # Staff-only. Must never appear in a customer-facing serializer.
    internal_note = db.Column(db.String(1000))
    status = db.Column(
        pg_enum(InformationRequestStatus, "information_request_status"),
        nullable=False,
        default=InformationRequestStatus.OPEN,
        server_default=InformationRequestStatus.OPEN.value,
        index=True,
    )
    requested_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    requested_at = db.Column(
        db.DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    cancelled_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    cancelled_at = db.Column(db.DateTime(timezone=True))
    cancel_reason = db.Column(db.String(1000))

    # ---------------------------------------------------------------- relationships
    loan_application = db.relationship(
        "LoanApplication", back_populates="information_requests"
    )
    requester = db.relationship("User", foreign_keys=[requested_by])
    canceller = db.relationship("User", foreign_keys=[cancelled_by])
    response = db.relationship(
        "InformationResponse",
        back_populates="request",
        uselist=False,
        cascade="save-update, merge, delete",
        passive_deletes=True,
    )

    def __repr__(self) -> str:
        return (
            f"<InformationRequest {self.id} application={self.loan_application_id} "
            f"{self.request_type} {self.status}>"
        )


class InformationResponse(db.Model):
    __tablename__ = "information_responses"

    id = db.Column(db.Integer, primary_key=True)
    information_request_id = db.Column(
        db.Integer,
        db.ForeignKey("information_requests.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    responded_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    responded_at = db.Column(
        db.DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    response_note = db.Column(db.String(1000), nullable=False)
    # {"monthly_income": {"old": 800.0, "new": 950.0}, ...} - only fields
    # this response actually changed.
    field_changes = db.Column(JSONType)
    # Document ids uploaded/linked as part of this answer. Documents are
    # never hard-deleted (re-uploads supersede, see Document.superseded_by_id),
    # so these ids stay resolvable.
    provided_document_ids = db.Column(JSONType)

    # ---------------------------------------------------------------- relationships
    request = db.relationship("InformationRequest", back_populates="response")
    responder = db.relationship("User", foreign_keys=[responded_by])

    def __repr__(self) -> str:
        return f"<InformationResponse {self.id} request={self.information_request_id}>"
