"""AdminReturn — an admin sending a recommended application back to its
officer for more work, instead of approving or rejecting it.

Powers the Loan Officer "Returned by Administrator" queue and tells the
officer why it came back. Immutable, like OfficerRecommendation: a later
re-recommendation adds new rows, it never edits these.
"""

from sqlalchemy import func

from app.extensions import db


class AdminReturn(db.Model):
    __tablename__ = "admin_returns"

    id = db.Column(db.Integer, primary_key=True)
    loan_application_id = db.Column(
        db.Integer,
        db.ForeignKey("loan_applications.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # The recommendation being sent back. Always present: an application can
    # only be returned from a recommended / admin-review status.
    officer_recommendation_id = db.Column(
        db.Integer, db.ForeignKey("officer_recommendations.id"), nullable=False
    )
    # NO ACTION (not SET NULL): a return must always name the admin.
    returned_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    reason = db.Column(db.String(2000), nullable=False)
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # ---------------------------------------------------------------- relationships
    loan_application = db.relationship("LoanApplication", back_populates="admin_returns")
    recommendation = db.relationship("OfficerRecommendation")
    admin = db.relationship("User", foreign_keys=[returned_by])

    def __repr__(self) -> str:
        return f"<AdminReturn {self.id} application={self.loan_application_id}>"
