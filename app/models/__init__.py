"""SQLAlchemy models for PRIMESTONE (Layer 4 data entities, lending-only).

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
    Administrator operations -> loan_terms_snapshots, loan_ledger_entries,
                             loan_closures, scheduled_job_runs,
                             prime_pricing_versions (+ tiers),
                             penalty_policy_versions (+ tiers)
"""

from .enums import (
    CustomerVerificationInvalidationReason,
    CustomerVerificationStatus,
    DisbursementMethod,
    DocumentType,
    EmploymentStatus,
    IdDocumentType,
    InformationRequestStatus,
    LedgerActorKind,
    LedgerEntryType,
    LoanTimeliness,
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
from .loan_records import (
    LoanClosure,
    LoanLedgerEntry,
    LoanTermsSnapshot,
    ReapplicationClearance,
    ScheduledJobRun,
)
from .pricing_policy import (
    PenaltyPolicyTier,
    PenaltyPolicyVersion,
    PrimePricingTier,
    PrimePricingVersion,
)
from .officer_recommendation import OfficerRecommendation
from .payment_transaction import PaymentTransaction
from .referee import Referee
from .repayment_schedule import RepaymentSchedule
from .system_parameter import SystemParameter
from .terms_acceptance import TermsAcceptance
from .user import MfaBackupCode, User
from .verification_item import VerificationItem
from . import immutability  # noqa: E402,F401  registers the insert-only guards

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
    "LoanClosure",
    "LoanLedgerEntry",
    "LoanTermsSnapshot",
    "MfaBackupCode",
    "OfficerRecommendation",
    "PaymentTransaction",
    "PenaltyPolicyTier",
    "PenaltyPolicyVersion",
    "PrimePricingTier",
    "PrimePricingVersion",
    "ReapplicationClearance",
    "Referee",
    "RepaymentSchedule",
    "ScheduledJobRun",
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
    "LedgerActorKind",
    "LedgerEntryType",
    "LoanApplicationStatus",
    "LoanClosureReason",
    "LoanStatus",
    "LoanTimeliness",
    "OfficerRecommendationType",
    "PaymentStatus",
    "RepaymentFrequency",
    "RepaymentStatus",
    "UserRole",
    "VerificationItemStatus",
]
