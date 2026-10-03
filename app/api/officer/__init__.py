"""Officer namespace - the Loan Officer workspace: dashboard queues, the
Application Review screen, the verification checklist and the
application-scoped customer history.

Every route requires loan_officer or admin (admins see the same views).
State transitions (claim, request information, recommend, ...) stay on the
loans namespace: /api/loans/applications/<id>/...
"""

from flask import request
from flask_restx import Namespace, Resource, abort, fields

from app.api.auth.decorators import current_user_id, roles_required
from app.api.loans import (
    application_staff_out,
    credit_assessment_out,
    information_request_staff_out,
    recommendation_out,
    serialize_application_staff,
)
from app.extensions import db
from app.models import LoanApplication, User
from app.services import loan_processing, officer_views, verification
from app.services.errors import ServiceError

ns = Namespace("officer", description="Loan Officer workspace (loan_officer or admin).")

_STAFF = ("loan_officer", "admin")
_ITEM_STATUSES = ["pending", "verified", "failed", "not_applicable"]

# --------------------------------------------------------------------- payloads
checklist_update_in = ns.model(
    "ChecklistItemUpdateInput",
    {
        "status": fields.String(required=True, enum=_ITEM_STATUSES, example="verified"),
        "note": fields.String(
            required=False,
            example="Checked against NID card.",
            description="Required for failed and not_applicable (max 1000).",
        ),
        "date_of_birth": fields.String(
            required=False,
            example="1990-05-01",
            description="age_18_plus + status=verified only, and then REQUIRED: the DOB read off the ID "
            "(YYYY-MM-DD, must be 18+). Saved on the customer.",
        ),
        "id_document_id": fields.Integer(
            required=False,
            example=41,
            description="valid_id + status=verified only, and then REQUIRED: which of the customer's "
            "current ID documents was checked.",
        ),
        "id_expiry_date": fields.String(
            required=False,
            example="2030-01-31",
            description="valid_id + status=verified: the ID's expiry date, if it has one (caps how long "
            "the customer verification stays valid).",
        ),
    },
)
reverify_in = ns.model(
    "RequestReverificationInput",
    {"note": fields.String(required=True, example="Customer reports a new national ID card.")},
)

# --------------------------------------------------------------- response models
error_out = ns.model("OfficerErrorResponse", {"message": fields.String})
queue_count_out = ns.model(
    "QueueCount",
    {"total": fields.Integer, "mine": fields.Integer, "unassigned": fields.Integer},
)
queue_counts_out = ns.model(
    "QueueCounts",
    {
        "queues": fields.Raw(
            description="{awaiting_review|under_review|customer_action_required|sent_to_admin|"
            "returned_by_admin: {total, mine, unassigned}}"
        ),
        "definitions": fields.Raw(description="{queue: [statuses it covers]}"),
    },
)
queue_item_out = ns.model(
    "QueueItem",
    {
        "id": fields.Integer,
        "status": fields.String,
        "customer_id": fields.Integer,
        "customer_name": fields.String,
        "amount_requested": fields.Float,
        "prime_category": fields.String,
        "total_repayable": fields.Float,
        "purpose_category": fields.String,
        "submitted_at": fields.String,
        "assigned_officer_id": fields.Integer,
        "assigned_officer_name": fields.String,
        "assigned_at": fields.String,
        "is_mine": fields.Boolean,
        "open_information_requests": fields.Integer,
        "latest_recommendation": fields.String,
        "returned_reason": fields.String(description="Set in the returned_by_admin queue."),
    },
)
queue_page_out = ns.model(
    "QueuePage",
    {
        "queue": fields.String,
        "statuses": fields.List(fields.String),
        "page": fields.Integer,
        "per_page": fields.Integer,
        "total": fields.Integer,
        "pages": fields.Integer,
        "items": fields.List(fields.Nested(queue_item_out)),
    },
)
checklist_item_out = ns.model(
    "ChecklistItem",
    {
        "item_type": fields.String(example="valid_id"),
        "label": fields.String(example="Valid ID checked"),
        "required": fields.Boolean(description="Blocks an approval recommendation until verified/not_applicable."),
        "status": fields.String(example="pending"),
        "note": fields.String,
        "checked_by": fields.Integer,
        "checked_by_name": fields.String,
        "checked_at": fields.String,
        "customer_verification_id": fields.Integer(
            description="Set when this check is backed by a customer verification (created from it, "
            "or carried over from an earlier one)."
        ),
        "evidence": fields.Raw(
            description='What a verified identity check recorded: {"date_of_birth"} or '
            '{"id_document_id", "id_document_type", "id_expiry_date"}.'
        ),
    },
)
checklist_summary_out = ns.model(
    "ChecklistSummary",
    {
        "total": fields.Integer,
        "required": fields.Integer,
        "required_complete": fields.Integer,
        "pending": fields.Integer,
        "failed": fields.Integer,
        "blocking_items": fields.List(fields.String),
        "ready_for_approval_recommendation": fields.Boolean,
    },
)
checklist_out = ns.model(
    "Checklist",
    {
        "application_id": fields.Integer,
        "started": fields.Boolean(description="False until the application is claimed."),
        "items": fields.List(fields.Nested(checklist_item_out)),
        "summary": fields.Nested(checklist_summary_out),
    },
)
customer_verification_out = ns.model(
    "CustomerVerificationSummary",
    {
        "id": fields.Integer,
        "verified_at": fields.String,
        "verified_by": fields.Integer,
        "status": fields.String(example="verified"),
        "valid_until": fields.String,
        "date_of_birth": fields.String,
        "id_document_id": fields.Integer,
        "verified_by_name": fields.String,
        "id_expiry_date": fields.String,
        "policy_version": fields.String,
        "source_application_id": fields.Integer,
    },
)
review_customer_out = ns.model(
    "ReviewCustomer",
    {
        "id": fields.Integer,
        "full_name": fields.String,
        "email": fields.String,
        "phone_number": fields.String,
        "member_since": fields.String,
        "is_active": fields.Boolean,
        "date_of_birth": fields.String(
            description="Recorded by an officer from the ID; shown whether or not a verification is current."
        ),
        "verification": fields.Nested(
            customer_verification_out,
            allow_null=True,
            description="The customer's current verification - the only source of the 'Verified customer' flag.",
        ),
    },
)
review_document_out = ns.model(
    "ReviewDocument",
    {
        "id": fields.Integer,
        "user_id": fields.Integer,
        "loan_application_id": fields.Integer,
        "payment_transaction_id": fields.Integer,
        "document_type": fields.String,
        "id_document_type": fields.String(
            description="national_id | drivers_licence | passport | work_id (ID documents only)"
        ),
        "storage_path": fields.String,
        "uploaded_at": fields.String,
        "is_current": fields.Boolean,
        "superseded_by_id": fields.Integer,
        "linked_to_this_application": fields.Boolean,
    },
)
admin_return_detail_out = ns.model(
    "AdminReturnDetail",
    {
        "id": fields.Integer,
        "recommendation_id": fields.Integer,
        "returned_by": fields.Integer,
        "returned_by_name": fields.String,
        "reason": fields.String,
        "created_at": fields.String,
    },
)
assignment_out = ns.model(
    "Assignment",
    {
        "officer_id": fields.Integer,
        "officer_name": fields.String,
        "assigned_at": fields.String,
        "is_mine": fields.Boolean,
    },
)
review_out = ns.model(
    "ApplicationReview",
    {
        "application": fields.Nested(application_staff_out),
        "customer": fields.Nested(review_customer_out),
        "documents": fields.List(fields.Nested(review_document_out)),
        "information_requests": fields.List(fields.Nested(information_request_staff_out)),
        "checklist": fields.Nested(checklist_out),
        "recommendations": fields.List(fields.Nested(recommendation_out)),
        "admin_returns": fields.List(fields.Nested(admin_return_detail_out)),
        "assignment": fields.Nested(assignment_out),
        "credit_assessment": fields.Nested(credit_assessment_out),
        "allowed_actions": fields.List(
            fields.String,
            description="claim | update_checklist | request_information | recommend_approval | "
            "recommend_rejection | resume_review | assign | reject | start_admin_review | "
            "return_to_officer | decide | disburse",
        ),
        "customer_history_url": fields.String,
    },
)
history_out = ns.model(
    "CustomerHistory",
    {
        "application_id": fields.Integer,
        "customer": fields.Raw(description="{id, full_name, member_since}"),
        "summary": fields.Raw(
            description="{previous_applications, previous_applications_rejected, loans_total, "
            "loans_active, loans_overdue, loans_completed, loans_defaulted, total_borrowed, "
            "total_repayable, total_repaid, current_exposure}"
        ),
        "repayment_record": fields.Raw(
            description="{installments_paid_on_time, installments_paid_late, "
            "installments_currently_overdue, installments_ever_overdue, payments_verified, "
            "payments_rejected, payments_awaiting_verification}"
        ),
        "penalties": fields.Raw(description="{applicable: false, note} - PRIME has no penalty model."),
        "previous_applications": fields.List(fields.Raw),
        "loans": fields.List(fields.Raw),
    },
)


# --------------------------------------------------------------------- helpers
def _viewer() -> User:
    user = db.session.get(User, current_user_id())
    if user is None:
        abort(404, "User not found.")
    return user


def _application(application_id: int) -> LoanApplication:
    application = db.session.get(LoanApplication, application_id)
    if application is None:
        abort(404, f"Application #{application_id} not found.")
    return application


# --------------------------------------------------------------------- queues
@ns.route("/queues")
class QueueCounts(Resource):
    @ns.doc(security="Bearer")
    @ns.response(200, "Dashboard counts per queue", queue_counts_out)
    @roles_required(*_STAFF)
    def get(self):
        return {
            "queues": officer_views.queue_counts(_viewer()),
            "definitions": {k: [s.value for s in v] for k, v in officer_views.QUEUES.items()},
        }


@ns.route("/queues/<string:queue>")
class QueueList(Resource):
    @ns.doc(
        security="Bearer",
        params={
            "queue": "awaiting_review | under_review | customer_action_required | "
            "sent_to_admin | returned_by_admin",
            "assigned": "any (default) | me | unassigned",
            "officer_id": "only applications assigned to this officer (not combinable with assigned)",
            "prime_category": "e.g. 'PRIME 2'",
            "page": "1-based (default 1)",
            "per_page": "default 25, max 100",
        },
    )
    @ns.response(200, "One page of a queue, oldest submission first", queue_page_out)
    @ns.response(400, "Unknown queue or filter", error_out)
    @roles_required(*_STAFF)
    def get(self, queue: str):
        args = request.args
        try:
            return officer_views.list_queue(
                _viewer(),
                queue=queue,
                assigned=args.get("assigned", "any"),
                officer_id=args.get("officer_id", type=int),
                prime_category=args.get("prime_category"),
                page=args.get("page", 1, type=int),
                per_page=args.get("per_page", 25, type=int),
            )
        except ServiceError as exc:
            abort(exc.status_code, exc.message)


# --------------------------------------------------------------------- review
@ns.route("/applications/<int:application_id>")
class ApplicationReview(Resource):
    @ns.doc(security="Bearer")
    @ns.response(200, "Everything the Application Review screen needs (history is separate)", review_out)
    @ns.response(404, "Not found", error_out)
    @roles_required(*_STAFF)
    def get(self, application_id: int):
        application = _application(application_id)
        viewer = _viewer()
        officer_views.open_checklist_if_needed(application, viewer)
        return officer_views.application_detail(
            application, viewer, serialize_application_staff(application)
        )


@ns.route("/applications/<int:application_id>/checklist")
class Checklist(Resource):
    @ns.doc(security="Bearer")
    @ns.response(200, "Current checklist state", checklist_out)
    @roles_required(*_STAFF)
    def get(self, application_id: int):
        application = _application(application_id)
        officer_views.open_checklist_if_needed(application, _viewer())
        return verification.serialize_checklist(application)


@ns.route("/applications/<int:application_id>/checklist/<string:item_type>")
class ChecklistItem(Resource):
    @ns.doc(
        security="Bearer",
        params={"item_type": ", ".join(t.key for t in verification.CHECKLIST)},
    )
    @ns.expect(checklist_update_in)
    @ns.response(200, "Item updated (who/when recorded); returns the whole checklist", checklist_out)
    @ns.response(400, "Unknown item / status, or a required note is missing", error_out)
    @ns.response(403, "Assigned to another officer", error_out)
    @ns.response(409, "Application is not with the officer", error_out)
    @roles_required(*_STAFF)
    def patch(self, application_id: int, item_type: str):
        application = _application(application_id)
        data = request.get_json(silent=True) or {}
        try:
            loan_processing.update_checklist_item(
                application,
                _viewer(),
                item_type,
                status=data.get("status"),
                note=data.get("note"),
                evidence={
                    k: data[k] for k in ("date_of_birth", "id_document_id", "id_expiry_date") if k in data
                },
            )
        except ServiceError as exc:
            abort(exc.status_code, exc.message)
        return verification.serialize_checklist(application)


@ns.route("/applications/<int:application_id>/customer-verification/invalidate")
class RequestReverification(Resource):
    @ns.doc(security="Bearer")
    @ns.expect(reverify_in)
    @ns.response(
        200, "Current verification invalidated (staff_requested); identity checks re-opened", review_customer_out
    )
    @ns.response(400, "note is required", error_out)
    @ns.response(403, "Assigned to another officer", error_out)
    @ns.response(409, "The customer has no current verification", error_out)
    @roles_required(*_STAFF)
    def post(self, application_id: int):
        application = _application(application_id)
        try:
            loan_processing.request_customer_reverification(
                application, _viewer(), (request.get_json(silent=True) or {}).get("note")
            )
        except ServiceError as exc:
            abort(exc.status_code, exc.message)
        return officer_views.customer_block(application.applicant)


@ns.route("/applications/<int:application_id>/customer-history")
class CustomerHistory(Resource):
    @ns.doc(security="Bearer")
    @ns.response(200, "History of THIS application's customer", history_out)
    @ns.response(403, "Loan officers: application is no longer under review", error_out)
    @roles_required(*_STAFF)
    def get(self, application_id: int):
        application = _application(application_id)
        try:
            return officer_views.customer_history(application, _viewer())
        except ServiceError as exc:
            abort(exc.status_code, exc.message)
