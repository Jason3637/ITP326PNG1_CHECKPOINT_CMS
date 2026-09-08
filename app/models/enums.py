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


class LoanApplicationStatus(enum.StrEnum):
    PENDING = "pending"
    UNDER_REVIEW = "under_review"
    APPROVED = "approved"
    REJECTED = "rejected"


class LoanStatus(enum.StrEnum):
    ACTIVE = "active"
    COMPLETED = "completed"
    DEFAULTED = "defaulted"


class RepaymentStatus(enum.StrEnum):
    UPCOMING = "upcoming"
    PAID = "paid"
    OVERDUE = "overdue"


class PaymentStatus(enum.StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"


class DocumentType(enum.StrEnum):
    ID_VERIFICATION = "id_verification"
    RECEIPT = "receipt"
    LOAN_FILE = "loan_file"
