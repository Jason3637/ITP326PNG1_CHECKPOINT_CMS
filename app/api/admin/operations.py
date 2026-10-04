"""Administrator operations API - every route here is admin-only at the
route level (roles_required("admin")); the services re-check it.

    queues            GET  /admin/queues, /admin/queues/<queue>
    final review      GET  /admin/applications/<id>, .../customer-history
    final decision    POST /admin/applications/<id>/approve | reject | return-to-officer
    disbursement      POST /admin/applications/<id>/disbursement-evidence, .../disbursement
    loans             GET  /admin/loans, /admin/loans/<id>;  POST /admin/loans/<id>/write-off
    repayments        GET  /admin/repayments;  POST /admin/repayments/<id>/verify | reject
    pricing / penalty GET, POST /admin/pricing, /admin/penalty-policy
    analytics         GET  /admin/analytics
The audit-log query stays at GET /api/reports/audit-logs (admin only).
"""

from flask import request
from flask_restx import Resource, abort, fields, reqparse
from werkzeug.datastructures import FileStorage

from app.api.auth.decorators import current_user_id, roles_required
from app.api.loans import serialize_application_staff
from app.extensions import db
from app.models import Loan, LoanApplication, User
from app.services import (
    admin_views,
    documents,
    loan_processing,
    officer_views,
    payment_processing,
    pricing_policy,
)
from app.models import PenaltyPolicyVersion, PrimePricingVersion
from app.services.errors import ServiceError

from . import error_out, ns

_ADMIN = ("admin",)
_DOC = {"security": "Bearer"}


def _admin() -> User:
    return db.session.get(User, current_user_id())


def _application(application_id: int) -> LoanApplication:
    a = db.session.get(LoanApplication, application_id)
    if a is None:
        abort(404, f"Application #{application_id} not found.")
    return a


def _loan(loan_id: int) -> Loan:
    loan = db.session.get(Loan, loan_id)
    if loan is None:
        abort(404, f"Loan #{loan_id} not found.")
    return loan


def _body() -> dict:
    return request.get_json(silent=True) or {}


def _int_arg(name, default):
    try:
        return int(request.args.get(name, default))
    except (TypeError, ValueError):
        abort(400, f"{name} must be a whole number.")


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except ServiceError as exc:
        abort(exc.status_code, exc.message)


# ------------------------------------------------------------------- models
queue_summary = ns.model("AdminQueueSummary", {
    "label": fields.String, "count": fields.Integer,
})
queue_counts_out = ns.model("AdminQueueCounts", {
    "as_of": fields.String(description="Port Moresby date the date-based queues use"),
    "queues": fields.Raw(description="{queue_key: {label, count}} - keys: " + ", ".join(admin_views.QUEUES)),
})
queue_list_out = ns.model("AdminQueuePage", {
    "queue": fields.String, "label": fields.String,
    "kind": fields.String(description="application | loan | repayment - decides the item shape"),
    "as_of": fields.String, "page": fields.Integer, "per_page": fields.Integer, "total": fields.Integer,
    "items": fields.List(fields.Raw, description="AdminApplicationItem, AdminLoanItem or AdminRepaymentItem"),
})
loan_item_out = ns.model("AdminLoanItem", {
    "loan_id": fields.Integer, "application_id": fields.Integer,
    "customer": fields.Raw(description="{id, full_name, email}"),
    "status": fields.String, "prime_category": fields.String,
    "principal": fields.Float, "interest_amount": fields.Float, "original_total_due": fields.Float,
    "penalties": fields.Float, "verified_repayments": fields.Float,
    "outstanding": fields.Float(description="From the ledger: original + penalties - verified repayments"),
    "disbursed_at": fields.String, "due_date": fields.String, "days_overdue": fields.Integer,
})
loan_page_out = ns.model("AdminLoanPage", {
    "status": fields.String, "page": fields.Integer, "per_page": fields.Integer, "total": fields.Integer,
    "items": fields.List(fields.Nested(loan_item_out)),
})
repayment_item_out = ns.model("AdminRepaymentItem", {
    "payment_id": fields.Integer, "loan_id": fields.Integer, "customer": fields.Raw,
    "amount_reported": fields.Float, "payment_date": fields.String, "payment_method": fields.String,
    "reference_number": fields.String, "receipts": fields.List(fields.Raw), "status": fields.String,
    "reported_at": fields.String, "loan_outstanding": fields.Float,
})
repayment_page_out = ns.model("AdminRepaymentPage", {
    "status": fields.String, "page": fields.Integer, "per_page": fields.Integer, "total": fields.Integer,
    "items": fields.List(fields.Nested(repayment_item_out)),
})
review_out = ns.model("AdminApplicationReview", {
    "application": fields.Raw, "customer": fields.Raw(description="identity, verification status, DOB"),
    "documents": fields.List(fields.Raw), "information_requests": fields.List(fields.Raw),
    "checklist": fields.Raw, "recommendations": fields.List(fields.Raw,
        description="officer, recommendation, comments, checklist snapshot, created_at"),
    "admin_returns": fields.List(fields.Raw), "assignment": fields.Raw, "credit_assessment": fields.Raw,
    "allowed_actions": fields.List(fields.String), "customer_history_url": fields.String,
    "final_decision": fields.Raw(description="{awaiting, can_approve, can_reject, can_return_to_officer, can_disburse, decided_at, decided_by, loan_id}"),
    "quote": fields.Raw(description="the PRIME quote locked at submission"),
})
loan_detail_out = ns.model("AdminLoanDetail", {
    "loan_id": fields.Integer, "application_id": fields.Integer, "customer": fields.Raw, "status": fields.String,
    "terms": fields.Raw(description="the terms snapshot - principal, category, rate, interest, original total, term, dates"),
    "balance": fields.Raw(description="from the ledger: original_obligation, penalties, verified_repayments, outstanding, days_overdue"),
    "disbursement": fields.Raw, "closure": fields.Raw,
    "ledger": fields.List(fields.Raw), "payments": fields.List(fields.Raw), "audit_history": fields.List(fields.Raw),
})
reason_in = ns.model("AdminReasonInput", {
    "reason": fields.String(required=True, example="Income could not be verified.", description="Required, max 2000."),
})
approve_in = ns.model("AdminApproveInput", {
    "note": fields.String(required=False, description="Required only when going against the officer's recommendation."),
})
disburse_in = ns.model("AdminDisbursementInput", {
    "method": fields.String(required=True, enum=["bsp_mobile_banking", "cash_on_hand"]),
    "reference": fields.String(required=True, example="BSP-TXN-88213",
                               description="BSP transaction number, or the cash acknowledgement number."),
    "disbursed_at": fields.String(required=False, example="2026-10-05T09:30:00+10:00",
                                  description="When the money moved; default now; not in the future."),
    "evidence_document_id": fields.Integer(required=False,
                                           description="From POST .../disbursement-evidence."),
    "note": fields.String(required=False, description="Max 500."),
})
pricing_tier_in = ns.model("PricingTierInput", {
    "category": fields.String(required=True, example="PRIME 1"),
    "min_amount": fields.Integer(required=True, example=100),
    "max_amount": fields.Integer(required=True, example=300),
    "interest_rate": fields.Float(required=True, example=0.5, description="Flat, over the 14-day term."),
})
pricing_in = ns.model("PricingVersionInput", {
    "tiers": fields.List(fields.Nested(pricing_tier_in), required=True,
                         description="Contiguous whole-Kina ranges, no gaps or overlaps."),
    "note": fields.String(required=False),
})
penalty_tier_in = ns.model("PenaltyTierInput", {
    "days_late": fields.Integer(required=True, example=7),
    "pct_of_original_interest": fields.Float(required=True, example=0.25,
                                             description="Of the loan's ORIGINAL interest, never the balance."),
})
penalty_in = ns.model("PenaltyPolicyInput", {
    "tiers": fields.List(fields.Nested(penalty_tier_in), required=True, description="Cumulative tiers."),
    "note": fields.String(required=False),
})
version_list_out = ns.model("PolicyVersions", {
    "current": fields.Raw, "history": fields.List(fields.Raw),
    "applies_to": fields.String,
})
analytics_out = ns.model("AdminAnalytics", {
    "window": fields.Raw, "as_of": fields.String, "currency": fields.String,
    "applications": fields.Raw, "disbursements": fields.Raw, "repayments": fields.Raw,
    "portfolio": fields.Raw, "processing_times": fields.Raw,
})

evidence_parser = reqparse.RequestParser()
evidence_parser.add_argument("file", type=FileStorage, location="files", required=True,
                             help="PDF, JPG or PNG, max 10 MiB")


# ------------------------------------------------------------------- queues
@ns.route("/queues")
class AdminQueues(Resource):
    @ns.doc(**_DOC, description="Counts for every Administrator queue (real counted queries).")
    @ns.response(200, "Counts", queue_counts_out)
    @roles_required(*_ADMIN)
    def get(self):
        return admin_views.queue_counts()


@ns.route("/queues/<string:queue>")
class AdminQueue(Resource):
    @ns.doc(**_DOC, params={"page": "default 1", "per_page": "default 25, max 100"},
            description="One queue, filtered and paged in the database. queue: " + ", ".join(admin_views.QUEUES))
    @ns.response(200, "One page", queue_list_out)
    @ns.response(404, "Unknown queue", error_out)
    @roles_required(*_ADMIN)
    def get(self, queue: str):
        return _call(admin_views.list_queue, queue, _admin(),
                     page=_int_arg("page", 1), per_page=_int_arg("per_page", 25))


# ------------------------------------------------------------- final review
@ns.route("/applications/<int:application_id>")
class AdminApplicationReview(Resource):
    @ns.doc(**_DOC, description="Final review screen: customer identity/verification, full application, "
                                "the officer's recommendation records, checklist, credit notes. Any application.")
    @ns.response(200, "Review", review_out)
    @ns.response(404, "Not found", error_out)
    @roles_required(*_ADMIN)
    def get(self, application_id: int):
        a = _application(application_id)
        return _call(admin_views.application_review, a, _admin(), serialize_application_staff(a))


@ns.route("/applications/<int:application_id>/customer-history")
class AdminCustomerHistory(Resource):
    @ns.doc(**_DOC, description="Same customer history the Loan Officer sees (one implementation); "
                                "admins aren't limited to open applications.")
    @ns.response(200, "History")
    @roles_required(*_ADMIN)
    def get(self, application_id: int):
        return _call(officer_views.customer_history, _application(application_id), _admin())


@ns.route("/applications/<int:application_id>/approve")
class AdminApprove(Resource):
    @ns.doc(**_DOC, description="Final approval -> AWAITING_DISBURSEMENT. Creates no loan and no disbursement.")
    @ns.expect(approve_in, validate=False)
    @ns.response(200, "Approved")
    @ns.response(409, "Not awaiting a final decision", error_out)
    @roles_required(*_ADMIN)
    def post(self, application_id: int):
        a = _call(loan_processing.admin_decide, _application(application_id), _admin(),
                  approve=True, reason=_body().get("note"))
        return serialize_application_staff(a)


@ns.route("/applications/<int:application_id>/reject")
class AdminReject(Resource):
    @ns.doc(**_DOC, description="Reject with a required reason - the final decision for an application "
                                "awaiting it, or an early exit from any other open status.")
    @ns.expect(reason_in, validate=False)
    @ns.response(200, "Rejected")
    @ns.response(400, "Reason missing", error_out)
    @ns.response(409, "Already decided", error_out)
    @roles_required(*_ADMIN)
    def post(self, application_id: int):
        a = _application(application_id)
        reason = _body().get("reason")
        if a.status in loan_processing.AWAITING_FINAL_DECISION:
            a = _call(loan_processing.admin_decide, a, _admin(), approve=False, reason=reason)
        else:
            a = _call(loan_processing.reject_application, a, _admin(), reason)
        return serialize_application_staff(a)


@ns.route("/applications/<int:application_id>/return-to-officer")
class AdminReturnToOfficer(Resource):
    @ns.doc(**_DOC, description="Send back to the Loan Officer (RETURNED_TO_OFFICER) with a required reason.")
    @ns.expect(reason_in, validate=False)
    @ns.response(200, "Returned")
    @ns.response(400, "Reason missing", error_out)
    @ns.response(409, "Not awaiting a final decision", error_out)
    @roles_required(*_ADMIN)
    def post(self, application_id: int):
        a = _application(application_id)
        _call(loan_processing.return_to_officer, a, _admin(), _body().get("reason"))
        return serialize_application_staff(a)


# ------------------------------------------------------------- disbursement
@ns.route("/applications/<int:application_id>/disbursement-evidence")
class AdminDisbursementEvidence(Resource):
    @ns.doc(**_DOC, description="Upload the BSP receipt / signed cash acknowledgement (filed under the "
                                "customer). Pass the returned id as evidence_document_id.")
    @ns.expect(evidence_parser)
    @ns.response(201, "Stored")
    @roles_required(*_ADMIN)
    def post(self, application_id: int):
        from app.models.enums import LoanApplicationStatus

        a = _application(application_id)
        if a.status != LoanApplicationStatus.AWAITING_DISBURSEMENT:
            abort(409, f"Application #{a.id} is not awaiting disbursement.")
        file = evidence_parser.parse_args()["file"]
        doc = _call(documents.upload_document, a.applicant, document_type="disbursement_evidence",
                    filename=file.filename, data=file.read(), content_type=file.mimetype,
                    loan_application_id=a.id, uploaded_by=_admin())
        return documents.serialize(doc), 201


@ns.route("/applications/<int:application_id>/disbursement")
class AdminDisbursement(Resource):
    @ns.doc(**_DOC, description="Record the disbursement. One transaction: Disbursement record, loan terms "
                                "snapshot, ORIGINAL_OBLIGATION ledger entry, loan ACTIVE, application DISBURSED. "
                                "A second disbursement of the same application is refused by the database.")
    @ns.expect(disburse_in, validate=False)
    @ns.response(201, "Disbursed")
    @ns.response(400, "Invalid field", error_out)
    @ns.response(409, "Not awaiting disbursement / already disbursed", error_out)
    @roles_required(*_ADMIN)
    def post(self, application_id: int):
        data = _body()
        _app, loan = _call(
            loan_processing.disburse_application, _application(application_id), _admin(),
            method=data.get("method"), method_reference=data.get("reference"), note=data.get("note"),
            disbursed_at=data.get("disbursed_at"), evidence_document_id=data.get("evidence_document_id"),
        )
        return _call(admin_views.loan_detail, loan), 201


# --------------------------------------------------------------------- loans
@ns.route("/loans")
class AdminLoans(Resource):
    @ns.doc(**_DOC, params={"status": "open (default) | closed | all", "page": "", "per_page": ""})
    @ns.response(200, "Loans", loan_page_out)
    @roles_required(*_ADMIN)
    def get(self):
        return _call(admin_views.list_loans, request.args.get("status", "open"),
                     _int_arg("page", 1), _int_arg("per_page", 25))


@ns.route("/loans/<int:loan_id>")
class AdminLoan(Resource):
    @ns.doc(**_DOC, description="Loan detail; every amount derived from the ledger and the terms snapshot.")
    @ns.response(200, "Loan", loan_detail_out)
    @ns.response(404, "Not found", error_out)
    @roles_required(*_ADMIN)
    def get(self, loan_id: int):
        return _call(admin_views.loan_detail, _loan(loan_id))


@ns.route("/loans/<int:loan_id>/write-off")
class AdminWriteOff(Resource):
    @ns.doc(**_DOC, description="Close an active/overdue loan as defaulted, with a required reason.")
    @ns.expect(reason_in, validate=False)
    @ns.response(200, "Written off", loan_detail_out)
    @roles_required(*_ADMIN)
    def post(self, loan_id: int):
        loan = _call(loan_processing.write_off_loan, _loan(loan_id), _admin(), _body().get("reason"))
        return _call(admin_views.loan_detail, loan)


# ---------------------------------------------------------------- repayments
@ns.route("/repayments")
class AdminRepayments(Resource):
    @ns.doc(**_DOC, params={"status": "awaiting (default) | verified | rejected | all", "page": "", "per_page": ""})
    @ns.response(200, "Repayments", repayment_page_out)
    @roles_required(*_ADMIN)
    def get(self):
        return _call(admin_views.list_repayments, request.args.get("status", "awaiting"),
                     _int_arg("page", 1), _int_arg("per_page", 25))


@ns.route("/repayments/<int:payment_id>/verify")
class AdminVerifyRepayment(Resource):
    @ns.doc(**_DOC, description="Verify: one transaction marks it VERIFIED, posts the VERIFIED_REPAYMENT "
                                "ledger entry and, at a zero balance, closes the loan. Verifying twice -> 409; "
                                "more than the outstanding balance -> 409.")
    @ns.response(200, "Verified")
    @ns.response(409, "Already verified/rejected, or overpayment", error_out)
    @roles_required(*_ADMIN)
    def post(self, payment_id: int):
        note = _body().get("note")
        return _call(payment_processing.verify_payment, _admin(), payment_id, decision="verified", note=note)


@ns.route("/repayments/<int:payment_id>/reject")
class AdminRejectRepayment(Resource):
    @ns.doc(**_DOC, description="Reject with a required reason. No ledger entry is written.")
    @ns.expect(reason_in, validate=False)
    @ns.response(200, "Rejected")
    @ns.response(400, "Reason missing", error_out)
    @roles_required(*_ADMIN)
    def post(self, payment_id: int):
        return _call(payment_processing.verify_payment, _admin(), payment_id,
                     decision="rejected", note=_body().get("reason"))


# ------------------------------------------------------- pricing / penalties
def _versions(model, serializer, current):
    rows = model.query.order_by(model.id.desc()).all()
    return {"current": serializer(current, current.id), "history": [serializer(v, current.id) for v in rows],
            "applies_to": "Applications submitted after a version is created. Existing quotes, "
                          "and the terms snapshot of every disbursed loan, never change."}


@ns.route("/pricing")
class AdminPricing(Resource):
    @ns.doc(**_DOC)
    @ns.response(200, "Current PRIME tiers and history", version_list_out)
    @roles_required(*_ADMIN)
    def get(self):
        return _call(lambda: _versions(PrimePricingVersion, pricing_policy.serialize_pricing_version,
                                       pricing_policy.current_pricing_version()))

    @ns.doc(**_DOC, description="Create a new PRIME pricing version (audited with before/after).")
    @ns.expect(pricing_in, validate=False)
    @ns.response(201, "Created", version_list_out)
    @ns.response(400, "Invalid tiers", error_out)
    @roles_required(*_ADMIN)
    def post(self):
        data = _body()
        _call(pricing_policy.create_pricing_version, _admin(), data.get("tiers"), data.get("note"))
        return self.get(), 201


@ns.route("/penalty-policy")
class AdminPenaltyPolicy(Resource):
    @ns.doc(**_DOC)
    @ns.response(200, "Current late-penalty tiers and history", version_list_out)
    @roles_required(*_ADMIN)
    def get(self):
        return _call(lambda: _versions(PenaltyPolicyVersion, pricing_policy.serialize_penalty_version,
                                       pricing_policy.current_penalty_policy()))

    @ns.doc(**_DOC, description="Create a new late-penalty policy version (audited with before/after).")
    @ns.expect(penalty_in, validate=False)
    @ns.response(201, "Created", version_list_out)
    @ns.response(400, "Invalid tiers", error_out)
    @roles_required(*_ADMIN)
    def post(self):
        data = _body()
        _call(pricing_policy.create_penalty_version, _admin(), data.get("tiers"), data.get("note"))
        return self.get(), 201


# ------------------------------------------------------------------ analytics
@ns.route("/analytics")
class AdminAnalytics(Resource):
    @ns.doc(**_DOC, params={"from": "YYYY-MM-DD (default: 29 days before `to`)", "to": "YYYY-MM-DD (default: today)"},
            description="Each metric is {value, definition}. Principal, interest, expected repayment, verified "
                        "cash received and outstanding obligation are separate metrics.")
    @ns.response(200, "Metrics", analytics_out)
    @roles_required(*_ADMIN)
    def get(self):
        return _call(admin_views.analytics, request.args.get("from"), request.args.get("to"))
