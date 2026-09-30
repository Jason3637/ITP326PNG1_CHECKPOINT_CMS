"""Enumerated types used across the data model.

``enum.StrEnum`` (Python 3.11+) means each member *is* its string value, so
these serialize cleanly to JSON and compare equal to plain strings.
"""

import enum


class UserRole(enum.StrEnum):
    CUSTOMER = "customer"
    LOAN_OFFICER = "loan_officer"
    ADMIN = "admin"


class RepaymentFrequency(enum.StrEnum):
    WEEKLY = "weekly"
    BIWEEKLY = "biweekly"
    MONTHLY = "monthly"


class EmploymentStatus(enum.StrEnum):
    """Applicant-declared employment status, captured at loan application time.

    Self-reported, not independently verified (no payslip/employer-check
    integration yet — see credit_evaluation.py's module docstring). Used only
    as a credit-evaluation signal.
    """

    EMPLOYED = "employed"
    SELF_EMPLOYED = "self_employed"
    UNEMPLOYED = "unemployed"
    RETIRED = "retired"
    STUDENT = "student"


class LoanApplicationStatus(enum.StrEnum):
    """Two-tier officer -> admin review chain for the PRIME workflow.

    DRAFT exists for forward compatibility but is not reachable yet - there
    is no draft-create/edit endpoint in this phase; `submit_application()`
    still lands directly on SUBMITTED in one atomic call.

    Old -> new mapping (see the Phase 1 migration plan for the data-migration
    CASE expression this mirrors):
        pending       -> submitted        (credit eval no longer routes status)
        under_review  -> officer_review
        approved      -> approved         (unchanged)
        rejected      -> rejected         (unchanged)
    """

    DRAFT = "draft"
    SUBMITTED = "submitted"
    OFFICER_REVIEW = "officer_review"
    CUSTOMER_ACTION_REQUIRED = "customer_action_required"
    RECOMMENDED_FOR_APPROVAL = "recommended_for_approval"
    ADMIN_REVIEW = "admin_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    AWAITING_DISBURSEMENT = "awaiting_disbursement"


class LoanStatus(enum.StrEnum):
    """Old -> new mapping:
        active     -> active     (unchanged)
        completed  -> paid
        defaulted  -> closed     (+ LoanClosureReason.DEFAULTED, see below)
    """

    ACTIVE = "active"
    OVERDUE = "overdue"
    PAID = "paid"
    CLOSED = "closed"


class LoanClosureReason(enum.StrEnum):
    """Why a Loan reached CLOSED. Exists so folding the old DEFAULTED status
    into CLOSED doesn't lose the signal credit_evaluation.py scores on.
    """

    PAID_IN_FULL = "paid_in_full"
    DEFAULTED = "defaulted"


class RepaymentStatus(enum.StrEnum):
    UPCOMING = "upcoming"
    PAID = "paid"
    OVERDUE = "overdue"


class PaymentStatus(enum.StrEnum):
    """Decouples "customer reported a payment" from "the ledger changed".
    The balance only ever updates when a transaction reaches VERIFIED - see
    app/services/payment_processing.py:verify_payment().

    Old -> new mapping: pending -> reported, completed -> verified,
    failed -> rejected. (Nothing in the codebase ever set PENDING, so there
    are no real rows expected to carry that value.)
    """

    REPORTED = "reported"
    VERIFICATION_PENDING = "verification_pending"
    VERIFIED = "verified"
    REJECTED = "rejected"


class DocumentType(enum.StrEnum):
    ID_VERIFICATION = "id_verification"
    RECEIPT = "receipt"
    LOAN_FILE = "loan_file"
    PROOF_OF_INCOME = "proof_of_income"


class DisbursementMethod(enum.StrEnum):
    BSP_MOBILE_BANKING = "bsp_mobile_banking"
    CASH_ON_HAND = "cash_on_hand"


class LoanPurposeCategory(enum.StrEnum):
    """Fixed purpose options shown on the apply form. ``LoanApplication.purpose``
    holds the free-text description - required when category is OTHER,
    optional (a short note) otherwise.
    """

    BUSINESS = "business"
    SCHOOL_FEES = "school_fees"
    MEDICAL = "medical"
    HOME_IMPROVEMENT = "home_improvement"
    DEBT_CONSOLIDATION = "debt_consolidation"
    OTHER = "other"
