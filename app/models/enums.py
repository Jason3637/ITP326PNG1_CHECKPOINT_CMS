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

    RECOMMENDED_FOR_REJECTION (Loan Officer workflow) is the counterpart of
    RECOMMENDED_FOR_APPROVAL: a loan officer cannot reject, only recommend
    rejection, and the application still goes to an admin for the final call.

    RETURNED_TO_OFFICER: an admin sent a recommended application back to its
    officer for more work (reason kept in AdminReturn); the officer resumes
    review from there.

    DISBURSED: the money has been paid out and the Loan exists - the
    application's journey is over; the loan carries on from here.
    """

    DRAFT = "draft"
    SUBMITTED = "submitted"
    OFFICER_REVIEW = "officer_review"
    CUSTOMER_ACTION_REQUIRED = "customer_action_required"
    RECOMMENDED_FOR_APPROVAL = "recommended_for_approval"
    RECOMMENDED_FOR_REJECTION = "recommended_for_rejection"
    ADMIN_REVIEW = "admin_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    AWAITING_DISBURSEMENT = "awaiting_disbursement"
    RETURNED_TO_OFFICER = "returned_to_officer"
    DISBURSED = "disbursed"


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
    # Uploaded by an admin: the BSP receipt or signed cash acknowledgement
    # for a disbursement. Filed under the borrower.
    DISBURSEMENT_EVIDENCE = "disbursement_evidence"


class IdDocumentType(enum.StrEnum):
    """Which kind of ID an id_verification Document is. The apply form has
    always asked for this; before it had a column, it survived only as a
    filename prefix (see documents.infer_id_document_type()).
    """

    NATIONAL_ID = "national_id"
    DRIVERS_LICENCE = "drivers_licence"
    PASSPORT = "passport"
    WORK_ID = "work_id"


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


# ------------------------------------------------------- Loan Officer workflow
class InformationRequestType(enum.StrEnum):
    """What kind of thing an officer is asking the customer for. The
    customer-facing explanation lives in InformationRequest.reason; this is
    the category, for filtering and reporting.
    """

    MISSING_DOCUMENT = "missing_document"
    DOCUMENT_UNCLEAR = "document_unclear"
    DOCUMENT_EXPIRED = "document_expired"
    INFORMATION_MISMATCH = "information_mismatch"
    REFEREE_UNREACHABLE = "referee_unreachable"
    EMPLOYMENT_CONFIRMATION = "employment_confirmation"
    OTHER = "other"


class InformationRequestStatus(enum.StrEnum):
    """OPEN until the customer answers it (RESPONDED, with a matching
    InformationResponse row) or staff withdraw it (CANCELLED, e.g. resuming
    review without waiting). Never deleted either way.
    """

    OPEN = "open"
    RESPONDED = "responded"
    CANCELLED = "cancelled"


class VerificationItemStatus(enum.StrEnum):
    PENDING = "pending"
    VERIFIED = "verified"
    FAILED = "failed"
    NOT_APPLICABLE = "not_applicable"


class OfficerRecommendationType(enum.StrEnum):
    """"Request more information" is deliberately NOT a member: it never goes
    to an admin, so it's an InformationRequest, not a recommendation.
    """

    RECOMMEND_APPROVAL = "recommend_approval"
    RECOMMEND_REJECTION = "recommend_rejection"


class CustomerVerificationStatus(enum.StrEnum):
    VERIFIED = "verified"
    INVALIDATED = "invalidated"


class CustomerVerificationInvalidationReason(enum.StrEnum):
    EXPIRED = "expired"
    INFORMATION_CHANGED = "information_changed"
    STAFF_REQUESTED = "staff_requested"
    POLICY_UPDATED = "policy_updated"
    # Replaced by a newer verification of the same customer (re-verified).
    SUPERSEDED = "superseded"


class LedgerEntryType(enum.StrEnum):
    """What a LoanLedgerEntry records. Amounts are signed: what the customer
    owes is positive, what they have paid is negative, so a loan's
    outstanding balance is the plain SUM of its entries.
    """

    ORIGINAL_OBLIGATION = "original_obligation"  # principal + interest, at disbursement
    PENALTY = "penalty"  # a late-payment tier, added by the penalty job
    VERIFIED_REPAYMENT = "verified_repayment"  # posted only when an admin verifies


class LedgerActorKind(enum.StrEnum):
    SYSTEM = "system"  # a scheduled job (created_by is NULL)
    ADMIN = "admin"  # an administrator's action (created_by is set)


class LoanTimeliness(enum.StrEnum):
    """How a closed loan was repaid, judged by the final payment's date
    against the due date and the penalty tiers (7 and 14 days late)."""

    ON_TIME = "on_time"
    LATE_NO_PENALTY = "late_no_penalty"  # 1-6 days late
    LATE_TIER_1 = "late_tier_1"  # 7-13 days late
    LATE_TIER_2 = "late_tier_2"  # 14+ days late
