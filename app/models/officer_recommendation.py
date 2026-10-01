"""OfficerRecommendation — a Loan Officer's (or an admin acting in that role)
recommendation to approve or reject, handed to an admin for the final call.

Immutable once written: nothing updates or deletes these rows, and the
admin's final decision only touches ``loan_applications``, so the record of
what was recommended - and on what evidence - survives whatever happens to
the application afterwards. More than one row per application is allowed
(e.g. a future "send back to officer" path).
"""

from sqlalchemy import func

from app.extensions import db

from .base import JSONType, pg_enum
from .enums import OfficerRecommendationType


class OfficerRecommendation(db.Model):
    __tablename__ = "officer_recommendations"

    id = db.Column(db.Integer, primary_key=True)
    loan_application_id = db.Column(
        db.Integer,
        db.ForeignKey("loan_applications.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # NO ACTION (not SET NULL): a recommendation must always name its officer.
    officer_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    recommendation = db.Column(
        pg_enum(OfficerRecommendationType, "officer_recommendation_type"), nullable=False
    )
    comments = db.Column(db.String(2000), nullable=False)
    # Frozen copy of every VerificationItem on the application at the moment
    # of recommending: [{item_type, status, note, checked_by, checked_at}, ...].
    checklist_snapshot = db.Column(JSONType, nullable=False)
    # The advisory credit_evaluation_result the officer was looking at.
    credit_evaluation_snapshot = db.Column(JSONType)
    customer_verification_id = db.Column(
        db.Integer, db.ForeignKey("customer_verifications.id")
    )
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # ---------------------------------------------------------------- relationships
    loan_application = db.relationship(
        "LoanApplication", back_populates="officer_recommendations"
    )
    officer = db.relationship("User", foreign_keys=[officer_id])
    customer_verification = db.relationship("CustomerVerification")

    def __repr__(self) -> str:
        return (
            f"<OfficerRecommendation {self.id} application={self.loan_application_id} "
            f"{self.recommendation}>"
        )
