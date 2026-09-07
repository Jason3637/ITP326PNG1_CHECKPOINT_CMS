"""Reports namespace - dashboard analytics + admin audit-log access."""

from flask import request
from flask_restx import Namespace, Resource, abort

from app.api.auth.decorators import current_user_id, roles_required
from app.extensions import db
from app.models import User
from app.services import audit, reporting

ns = Namespace("reports", description="Reporting Tool + Audit Ledger (admin view).")


def _current_user() -> User:
    user = db.session.get(User, current_user_id())
    if user is None:
        abort(404, "User not found.")
    return user


@ns.route("/dashboard")
class Dashboard(Resource):
    @ns.doc(security="Bearer")
    @ns.response(200, "Role-aware dashboard (customer summary or portfolio aggregates)")
    @roles_required()  # any authenticated user; shape depends on role
    def get(self):
        return reporting.build_dashboard(_current_user())


@ns.route("/audit-logs")
class AuditLogs(Resource):
    @ns.doc(
        security="Bearer",
        params={
            "page": "1-based page number (default 1)",
            "per_page": "rows per page (default 50, max 200)",
            "actor_id": "filter by acting user id",
            "action": "filter by exact action string",
            "entity_type": "filter by entity type",
            "date_from": "inclusive start (YYYY-MM-DD or ISO 8601)",
            "date_to": "inclusive end (YYYY-MM-DD or ISO 8601)",
        },
    )
    @ns.response(200, "Paginated audit records")
    @roles_required("admin")
    def get(self):
        args = request.args
        try:
            return audit.query_logs(
                page=args.get("page", 1, type=int),
                per_page=args.get("per_page", 50, type=int),
                actor_id=args.get("actor_id", type=int),
                action=args.get("action"),
                entity_type=args.get("entity_type"),
                date_from=args.get("date_from"),
                date_to=args.get("date_to"),
            )
        except ValueError as exc:
            abort(400, str(exc))
