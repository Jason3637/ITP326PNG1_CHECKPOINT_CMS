"""User account and MFA-related tables."""

from sqlalchemy import func

from app.extensions import db

from .enums import UserRole
from .base import pg_enum


class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(
        pg_enum(UserRole, "user_role"),
        nullable=False,
        default=UserRole.CUSTOMER,
        server_default=UserRole.CUSTOMER.value,
    )
    full_name = db.Column(db.String(255), nullable=False)
    phone_number = db.Column(db.String(32))
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    is_active = db.Column(
        db.Boolean, nullable=False, default=True, server_default=db.text("true")
    )

    # ------------------------------------------------------------------ MFA / TOTP
    # SECURITY: `totp_secret` stores the TOTP shared secret **encrypted at rest**
    # (application-layer symmetric encryption, e.g. Fernet, keyed from an env
    # secret). NEVER store the plaintext base32 secret here. The service layer
    # encrypts on write and decrypts only transiently in memory when generating
    # or verifying a code. Column is nullable until the user enrolls.
    totp_secret = db.Column(db.Text)  # ciphertext only
    totp_enabled = db.Column(
        db.Boolean, nullable=False, default=False, server_default=db.text("false")
    )

    # Backup codes live in their own table (see `MfaBackupCode`) rather than a
    # JSON column: individual codes can be marked used without rewriting the set,
    # and each is stored only as a one-way hash.
    backup_codes = db.relationship(
        "MfaBackupCode",
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    # ---------------------------------------------------------------- relationships
    loan_applications = db.relationship(
        "LoanApplication",
        back_populates="applicant",
        foreign_keys="LoanApplication.user_id",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    decided_applications = db.relationship(
        "LoanApplication",
        back_populates="decided_by_officer",
        foreign_keys="LoanApplication.decided_by",
    )
    loans = db.relationship(
        "Loan",
        back_populates="borrower",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    documents = db.relationship(
        "Document",
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    audit_logs = db.relationship(
        "AuditLog",
        back_populates="actor",
        foreign_keys="AuditLog.actor_id",
    )

    def __repr__(self) -> str:
        return f"<User {self.id} {self.email} ({self.role})>"


class MfaBackupCode(db.Model):
    __tablename__ = "mfa_backup_codes"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # SECURITY: one-way hash of the backup code (same hashing scheme as passwords),
    # never the plaintext code. `used_at` is set the first time a code is consumed.
    code_hash = db.Column(db.String(255), nullable=False)
    used_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    user = db.relationship("User", back_populates="backup_codes")

    def __repr__(self) -> str:
        state = "used" if self.used_at else "unused"
        return f"<MfaBackupCode user={self.user_id} {state}>"
