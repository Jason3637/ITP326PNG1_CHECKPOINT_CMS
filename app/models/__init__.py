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
    Loan Officer workflow -> information_requests (+ information_responses),
                             admin_returns,
                             verification_items, officer_recommendations,
                             customer_verifications
"""

from .enums import (
    CustomerVerificationInvalidationReason,
    CustomerVerificationStatus,
    DisbursementMethod,
    DocumentType,
    EmploymentStatus,
    IdDocumentType,
    InformationRequestStatus,
    InformationRequestType,
    LoanApplicationStatus,
    LoanClosureReason,
    LoanStatus,
    OfficerRecommendationType,
    PaymentStatus,
    RepaymentFrequency,
    RepaymentStatus,
    UserRole,
    VerificationItemStatus,
)
from .admin_return import AdminReturn
from .audit_log import AuditLog
from .customer_verification import CustomerVerification
from .disbursement import Disbursement
from .document import Document
from .information_request import InformationRequest, InformationResponse
from .loan import Loan
from .loan_application import LoanApplication
from .officer_recommendation import OfficerRecommendation
from .payment_transaction import PaymentTransaction
from .referee import Referee
from .repayment_schedule import RepaymentSchedule
from .system_parameter import SystemParameter
from .terms_acceptance import TermsAcceptance
from .user import MfaBackupCode, User
from .verification_item import VerificationItem

__all__ = [
    "AdminReturn",
    "AuditLog",
    "CustomerVerification",
    "Disbursement",
    "Document",
    "InformationRequest",
    "InformationResponse",
    "Loan",
    "LoanApplication",
    "MfaBackupCode",
    "OfficerRecommendation",
    "PaymentTransaction",
    "Referee",
    "RepaymentSchedule",
    "SystemParameter",
    "TermsAcceptance",
    "User",
    "VerificationItem",
    # enums
    "CustomerVerificationInvalidationReason",
    "CustomerVerificationStatus",
    "DisbursementMethod",
    "DocumentType",
    "EmploymentStatus",
    "IdDocumentType",
    "InformationRequestStatus",
    "InformationRequestType",
    "LoanApplicationStatus",
    "LoanClosureReason",
    "LoanStatus",
    "OfficerRecommendationType",
    "PaymentStatus",
    "RepaymentFrequency",
    "RepaymentStatus",
    "UserRole",
    "VerificationItemStatus",
]
