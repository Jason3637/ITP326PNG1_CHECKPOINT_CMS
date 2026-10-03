"""Regression: the Loan Officer review screen's residence, employer, ID type
and interest rate.

All four used to come back missing ("not collected"): residence and employer
were never captured, the ID type was asked for but only survived as a
filename prefix, and the interest rate was computed but never serialized.
"""

import io
from unittest.mock import patch

import pytest

from app.extensions import db
from app.models import Document, LoanApplication
from app.services import documents

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


@pytest.fixture(autouse=True)
def no_real_storage():
    with patch("app.services.documents.supabase_storage.upload_file", return_value="ok"):
        yield


def _upload_id(client, headers, *, filename="id.png", id_document_type="national_id"):
    form = {"document_type": "id_verification", "file": (io.BytesIO(PNG), filename, "image/png")}
    if id_document_type:
        form["id_document_type"] = id_document_type
    return client.post("/api/users/documents", headers=headers, data=form, content_type="multipart/form-data")


@pytest.fixture
def submitted(client, make_user, auth_header, apply_payload):
    """A fully-submitted application, the way the apply form sends it:
    ID uploaded (with its type) and linked, residence and employer given."""
    customer = make_user("customer")
    ch = auth_header(customer)
    r = _upload_id(client, ch)
    assert r.status_code == 201, r.get_json()
    id_doc = r.get_json()
    r = client.post(
        "/api/loans/apply",
        headers=ch,
        json=apply_payload(
            amount_requested=500,
            employment_status="employed",
            employer_name="Bank South Pacific",
            residential_address="Section 12, Lot 4, Gerehu Stage 2, Port Moresby, NCD",
            document_ids=[id_doc["id"]],
        ),
    )
    assert r.status_code == 201, r.get_json()
    return {"app_id": r.get_json()["id"], "ch": ch, "id_doc": id_doc}


def test_review_screen_returns_all_four_fields_with_real_values(client, make_user, auth_header, submitted):
    oh = auth_header(make_user("loan_officer"))
    r = client.get(f"/api/officer/applications/{submitted['app_id']}", headers=oh)
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    application = body["application"]

    assert application["residential_address"] == "Section 12, Lot 4, Gerehu Stage 2, Port Moresby, NCD"
    assert application["employer_name"] == "Bank South Pacific"
    assert application["pricing"]["interest_rate"] == 0.4, "PRIME 2 flat rate"
    assert application["pricing"]["interest_amount"] == 200
    id_docs = [d for d in body["documents"] if d["document_type"] == "id_verification"]
    assert [d["id_document_type"] for d in id_docs] == ["national_id"]

    for value in (
        application["residential_address"],
        application["employer_name"],
        application["pricing"]["interest_rate"],
        id_docs[0]["id_document_type"],
    ):
        assert value not in (None, "", "Not collected by the system")


def test_customer_sees_their_own_interest_rate(client, submitted):
    mine = client.get("/api/loans/applications/mine", headers=submitted["ch"]).get_json()["applications"][0]
    assert mine["pricing"]["interest_rate"] == 0.4
    assert mine["residential_address"] and mine["employer_name"]
    r = client.get("/api/loans/prime-preview?amount_requested=150", headers=submitted["ch"])
    assert r.get_json()["interest_rate"] == 0.5, "PRIME 1 flat rate"


@pytest.mark.parametrize("amount,rate", [(100, 0.5), (300, 0.5), (301, 0.4), (700, 0.4), (701, 0.35), (1000, 0.35)])
def test_interest_rate_matches_the_prime_tier(client, make_user, auth_header, amount, rate):
    r = client.get(
        f"/api/loans/prime-preview?amount_requested={amount}", headers=auth_header(make_user("customer"))
    )
    assert r.get_json()["interest_rate"] == rate


# --------------------------------------------------- captured at the source
def test_apply_requires_a_residence(client, make_user, auth_header, apply_payload):
    ch = auth_header(make_user("customer"))
    for bad in (None, "", "   ", "x" * 501):
        body = apply_payload(residential_address=bad)
        if bad is None:
            body.pop("residential_address")
        r = client.post("/api/loans/apply", headers=ch, json=body)
        assert r.status_code == 400 and "residential_address" in r.get_json()["message"], bad


@pytest.mark.parametrize(
    "status,employer,expected",
    [
        ("employed", None, 400),
        ("self_employed", "  ", 400),
        ("employed", "Bank South Pacific", 201),
        ("self_employed", "Kaupa Market Stall", 201),
        ("unemployed", None, 201),
        ("student", None, 201),
        (None, None, 201),
    ],
)
def test_employer_is_required_only_when_working(
    client, make_user, auth_header, apply_payload, status, employer, expected
):
    body = apply_payload(employment_status=status, employer_name=employer)
    if status is None:
        body.pop("employment_status", None)
    r = client.post("/api/loans/apply", headers=auth_header(make_user("customer")), json=body)
    assert r.status_code == expected, r.get_json()


def test_id_upload_records_the_id_type(client, make_user, auth_header):
    ch = auth_header(make_user("customer"))
    r = _upload_id(client, ch, id_document_type="passport")
    assert r.status_code == 201 and r.get_json()["id_document_type"] == "passport"
    assert db.session.get(Document, r.get_json()["id"]).id_document_type.value == "passport"


def test_id_type_is_inferred_from_the_older_frontends_filename_prefix(client, make_user, auth_header):
    """The deployed frontend sends only '<id type>-<name>' - keep accepting it."""
    ch = auth_header(make_user("customer"))
    r = _upload_id(client, ch, filename="drivers_licence-scan.png", id_document_type=None)
    assert r.status_code == 201, r.get_json()
    assert r.get_json()["id_document_type"] == "drivers_licence"


def test_id_upload_without_any_id_type_is_refused(client, make_user, auth_header):
    ch = auth_header(make_user("customer"))
    r = _upload_id(client, ch, filename="scan.png", id_document_type=None)
    assert r.status_code == 400 and "id_document_type" in r.get_json()["message"]


def test_id_type_only_applies_to_id_documents(client, make_user, auth_header):
    ch = auth_header(make_user("customer"))
    form = {
        "document_type": "receipt",
        "id_document_type": "passport",
        "file": (io.BytesIO(PNG), "r.png", "image/png"),
    }
    r = client.post("/api/users/documents", headers=ch, data=form, content_type="multipart/form-data")
    assert r.status_code == 400


@pytest.mark.parametrize(
    "path,expected",
    [
        ("users/1/id_verification/0a1b2c3d4e5f_national_id-photo.png", "national_id"),
        ("users/1/id_verification/0a1b2c3d4e5f_work_id-card.pdf", "work_id"),
        ("users/1/id_verification/0a1b2c3d4e5f_photo.png", None),
        ("users/1/id_verification/0a1b2c3d4e5f_my_passport.png", None),
    ],
)
def test_infer_id_document_type_from_storage_paths(path, expected):
    result = documents.infer_id_document_type(path)
    assert (result.value if result else None) == expected


def test_older_application_can_supply_residence_and_employer_via_respond(
    client, make_user, auth_header, apply_payload, workflow
):
    """Applications submitted before these were collected have NULLs; an
    officer asks, and the customer's response fills them in (old -> new kept)."""
    customer = make_user("customer")
    ch, oh = auth_header(customer), auth_header(make_user("loan_officer"))
    app_id = client.post("/api/loans/apply", headers=ch, json=apply_payload()).get_json()["id"]
    row = db.session.get(LoanApplication, app_id)
    row.residential_address = None
    row.employer_name = None
    db.session.commit()

    client.post(f"/api/loans/applications/{app_id}/officer-review", headers=oh)
    workflow.request_info(app_id, oh, reason="Please tell us where you live and who you work for.")
    r = workflow.respond(
        app_id, ch, note="Added.", residential_address="Boroko, NCD", employer_name="Digicel PNG"
    )
    assert r.status_code == 200, r.get_json()
    assert (r.get_json()["residential_address"], r.get_json()["employer_name"]) == ("Boroko, NCD", "Digicel PNG")
    detail = client.get(f"/api/officer/applications/{app_id}", headers=oh).get_json()
    changes = detail["information_requests"][0]["response"]["field_changes"]
    assert changes["residential_address"] == {"old": None, "new": "Boroko, NCD"}
    assert changes["employer_name"] == {"old": None, "new": "Digicel PNG"}
