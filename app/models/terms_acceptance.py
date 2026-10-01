"""TermsAcceptance — records that a customer accepted a specific policy
version when submitting an application (version + timestamp + application
link; the policy text itself is not stored here).
"""

from sqlalchemy import func

from app.extensions import db


class TermsAcceptance(db.Model):
    __tablename__ = "terms_acceptances"

    id = db.Column(db.Integer, primary_key=True)
    loan_application_id = db.Column(
        db.Integer,
        db.ForeignKey("loan_applications.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    policy_version = db.Column(db.String(50), nullable=False)
    accepted_at = db.Column(
        db.DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # ---------------------------------------------------------------- relationships
    loan_application = db.relationship(
        "LoanApplication", back_populates="terms_acceptance"
    )
    user = db.relationship("User", foreign_keys=[user_id])

    def __repr__(self) -> str:
        return f"<TermsAcceptance application={self.loan_application_id} v{self.policy_version}>"
