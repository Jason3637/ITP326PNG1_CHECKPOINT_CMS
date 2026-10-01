"""Referee — structured character/contact reference supplied with an application.

Deliberately structured columns, not a document upload: this is data the
credit/review process reads and potentially calls, not a file to archive.
"""

from sqlalchemy import func

from app.extensions import db


class Referee(db.Model):
    __tablename__ = "referees"

    id = db.Column(db.Integer, primary_key=True)
    loan_application_id = db.Column(
        db.Integer,
        db.ForeignKey("loan_applications.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    full_name = db.Column(db.String(255), nullable=False)
    # Free text (e.g. "sibling", "employer", "friend") - relationship types
    # aren't a fixed enumeration.
    relationship_to_applicant = db.Column(db.String(100), nullable=False)
    mobile_number = db.Column(db.String(32), nullable=False)
    employer_name = db.Column(db.String(255))
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # ---------------------------------------------------------------- relationships
    loan_application = db.relationship("LoanApplication", back_populates="referees")

    def __repr__(self) -> str:
        return f"<Referee {self.id} application={self.loan_application_id} {self.full_name!r}>"
