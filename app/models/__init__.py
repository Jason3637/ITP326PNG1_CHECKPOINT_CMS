"""SQLAlchemy models for Prime's Vault (Layer 4 data entities, lending-only).

Importing this package registers every model on ``db.metadata`` so
Flask-Migrate autogenerate can see them. ``app.create_app`` imports it.

Entity map (diagram → table):
    Users                -> users            (+ mfa_backup_codes)
    Loan Applications     -> loan_applications (+ referees, terms_acceptances)
    Loans                -> loans
    Disbursements        -> disbursements
    Repayment Schedules  -> repayment_schedules
    Payments Transaction -> payment_transactions
    Audit Logs           -> audit_logs
    (Documents)          -> documents
"""

from .enums import (
    DisbursementMethod,
    DocumentType,
    EmploymentStatus,
    LoanApplicationStatus,
    LoanClosureReason,
    LoanStatus,
    PaymentStatus,
    RepaymentFrequency,
    RepaymentStatus,
    UserRole,
)
from .audit_log import AuditLog
from .disbursement import Disbursement
from .document import Document
from .loan import Loan
from .loan_application import LoanApplication
from .payment_transaction import PaymentTransaction
from .referee import Referee
from .repayment_schedule import RepaymentSchedule
from .system_parameter import SystemParameter
from .terms_acceptance import TermsAcceptance
from .user import MfaBackupCode, User

__all__ = [
    "AuditLog",
    "Disbursement",
    "Document",
    "Loan",
    "LoanApplication",
    "MfaBackupCode",
    "PaymentTransaction",
    "Referee",
    "RepaymentSchedule",
    "SystemParameter",
    "TermsAcceptance",
    "User",
    # enums
    "DisbursementMethod",
    "DocumentType",
    "EmploymentStatus",
    "LoanApplicationStatus",
    "LoanClosureReason",
    "LoanStatus",
    "PaymentStatus",
    "RepaymentFrequency",
    "RepaymentStatus",
    "UserRole",
]
