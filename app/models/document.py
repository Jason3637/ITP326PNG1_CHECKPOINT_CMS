"""Document — metadata pointer to a file in Supabase Storage.

The file bytes live in the Storage bucket named by SUPABASE_STORAGE_BUCKET;
this table only records the object path and context.
"""

from sqlalchemy import func

from app.extensions import db

from .base import pg_enum
from .enums import DocumentType


class Document(db.Model):
    __tablename__ = "documents"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    loan_application_id = db.Column(
        db.Integer,
        db.ForeignKey("loan_applications.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    document_type = db.Column(
        pg_enum(DocumentType, "document_type"), nullable=False
    )
    # Supabase Storage object path, e.g. "id_verification/user_42/passport.pdf".
    # NOT the file contents.
    storage_path = db.Column(db.String(1024), nullable=False)
    uploaded_at = db.Column(
        db.DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # Versioning: when a customer re-uploads a document of the same type for
    # the same application (e.g. responding to a CUSTOMER_ACTION_REQUIRED
    # request), the OLD row is kept - never deleted or overwritten - and
    # pointed at its replacement here. NULL means "current" (the latest
    # version, or never superseded). See app/services/documents.py's
    # supersede logic. ondelete SET NULL: if the newer document is ever
    # removed, the older one reverts to being "current" again.
    superseded_by_id = db.Column(
        db.Integer,
        db.ForeignKey("documents.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # ---------------------------------------------------------------- relationships
    user = db.relationship("User", back_populates="documents")
    loan_application = db.relationship(
        "LoanApplication", back_populates="documents"
    )
    superseded_by = db.relationship(
        "Document", remote_side=[id], foreign_keys=[superseded_by_id]
    )

    def __repr__(self) -> str:
        current = "current" if self.superseded_by_id is None else f"superseded by {self.superseded_by_id}"
        return f"<Document {self.id} {self.document_type} user={self.user_id} ({current})>"
