"""Users namespace - Members Registry + member document handling."""

from flask import request
from werkzeug.datastructures import FileStorage

from flask_restx import Namespace, Resource, abort, reqparse

from app.api.auth.decorators import current_user_id, roles_required
from app.extensions import db
from app.models import User
from app.services import documents, members
from app.services.errors import ServiceError

ns = Namespace("users", description="Member profiles and documents.")

_STAFF = ("loan_officer", "admin")

upload_parser = reqparse.RequestParser()
upload_parser.add_argument("file", type=FileStorage, location="files", required=True,
                           help="PDF, JPG or PNG, max 10 MiB")
upload_parser.add_argument("document_type", location="form", required=True,
                           choices=("id_verification", "receipt", "loan_file"))
upload_parser.add_argument("loan_application_id", type=int, location="form", required=False)


def _current_user() -> User:
    user = db.session.get(User, current_user_id())
    if user is None:
        abort(404, "User not found.")
    return user


@ns.route("/profile")
class Profile(Resource):
    @ns.doc(security="Bearer")
    @ns.response(200, "The authenticated member's profile")
    @roles_required()
    def get(self):
        profile = members.get_profile(current_user_id())
        if profile is None:
            abort(404, "Profile not found.")
        return profile


@ns.route("/documents")
class Documents(Resource):
    @ns.doc(security="Bearer")
    @ns.expect(upload_parser)
    @ns.response(201, "Uploaded to Supabase Storage; storage_path saved")
    @ns.response(413, "File too large")
    @ns.response(415, "Unsupported file type")
    @roles_required("customer")
    def post(self):
        args = upload_parser.parse_args()
        file: FileStorage = args["file"]
        data = file.read()
        try:
            document = documents.upload_document(
                _current_user(),
                document_type=args["document_type"],
                filename=file.filename,
                data=data,
                content_type=file.mimetype,
                loan_application_id=args.get("loan_application_id"),
            )
        except ServiceError as exc:
            abort(exc.status_code, exc.message)
        return documents.serialize(document), 201

    @ns.doc(security="Bearer", params={"document_type": "optional filter"})
    @ns.response(200, "The authenticated member's documents")
    @roles_required()
    def get(self):
        try:
            rows = documents.list_documents(
                current_user_id(), document_type=request.args.get("document_type")
            )
        except ServiceError as exc:
            abort(exc.status_code, exc.message)
        return {"count": len(rows), "documents": [documents.serialize(d) for d in rows]}


@ns.route("/documents/<int:document_id>/download")
class DocumentDownload(Resource):
    @ns.doc(security="Bearer")
    @ns.response(200, "A short-lived Supabase signed URL")
    @ns.response(403, "Not your document")
    @ns.response(404, "No such document")
    @roles_required()  # owner OR staff; enforced in the service
    def get(self, document_id: int):
        try:
            return documents.get_download_url(document_id, _current_user())
        except ServiceError as exc:
            abort(exc.status_code, exc.message)


@ns.route("/<int:user_id>/documents")
class MemberDocuments(Resource):
    @ns.doc(security="Bearer", params={"document_type": "optional filter"})
    @ns.response(200, "A member's documents (staff review)")
    @roles_required(*_STAFF)
    def get(self, user_id: int):
        if db.session.get(User, user_id) is None:
            abort(404, "User not found.")
        try:
            rows = documents.list_documents(
                user_id, document_type=request.args.get("document_type")
            )
        except ServiceError as exc:
            abort(exc.status_code, exc.message)
        return {
            "user_id": user_id,
            "count": len(rows),
            "documents": [documents.serialize(d) for d in rows],
        }
