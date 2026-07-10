"""Qualify-with-AI: ask-the-vendor checklist + qual_request drafting.

Service layer — compute_qual_gaps (condition-aware gap checklist) and the
draft_email 'qual_request' kind (drafts a vendor reply asking only for the
chosen items, including user-added custom items the AI never suggested).
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from app.constants import UserRole
from app.models.crm import CustomerSite, SiteContact
from app.models.offers import Offer, VendorResponse
from app.models.sourcing import Requirement
from app.services import email_drafting
from app.services.offer_qualification import compute_qual_gaps
from app.utils.claude_errors import ClaudeError


# ── compute_qual_gaps ────────────────────────────────────────────────────────
def test_gaps_empty_when_no_condition():
    assert compute_qual_gaps({}, None) == []


def test_gaps_new_missing_manufacturer_prechecked():
    items = compute_qual_gaps({"condition": "new"}, "new")
    by = {i["label"]: i for i in items}
    assert by["Manufacturer"]["prechecked"] is True
    # date code / MOQ shown as meter items, prechecked when empty
    assert by["Date code"]["prechecked"] is True


def test_gaps_new_manufacturer_present_unchecked():
    items = compute_qual_gaps({"condition": "new", "manufacturer": "TI"}, "new")
    by = {i["label"]: i for i in items}
    assert by["Manufacturer"]["prechecked"] is False


def test_gaps_pulls_always_asks_usage_and_part_condition():
    # usage / part_condition are never auto-fillable → always pre-checked for pulls
    items = compute_qual_gaps({"condition": "pulls", "packaging": "Tray"}, "pulls")
    by = {i["label"]: i for i in items}
    assert by["Usage (boards/systems)"]["prechecked"] is True
    assert by["Part condition"]["prechecked"] is True
    assert by["Packaging"]["prechecked"] is False  # present


def test_gaps_refurb_asks_refurb_fields():
    items = compute_qual_gaps({"condition": "refurb"}, "refurb")
    by = {i["label"]: i for i in items}
    assert by["Refurbished by"]["prechecked"] is True
    assert by["Refurb process"]["prechecked"] is True


def test_gaps_only_show_rows_relevant_to_condition():
    labels = {i["label"] for i in compute_qual_gaps({"condition": "new"}, "new")}
    assert "Usage (boards/systems)" not in labels  # pulls-only
    assert "Refurbished by" not in labels  # refurb-only


# ── draft_email('qual_request') ──────────────────────────────────────────────
@pytest.mark.asyncio
async def test_qual_request_returns_subject_and_body():
    with patch.object(email_drafting, "claude_json", new_callable=AsyncMock) as m:
        m.return_value = {"body": "Could you confirm the date code and MOQ?"}
        result = await email_drafting.draft_email(
            "qual_request",
            {
                "vendor_name": "Acme",
                "subject": "RFQ - LM358N",
                "mpn": "LM358N",
                "items_requested": ["Date code", "MOQ"],
            },
        )
    assert result is not None
    assert result["subject"].lower().startswith("re:")
    assert "date code" in result["body"].lower()


@pytest.mark.asyncio
async def test_qual_request_includes_user_added_custom_items():
    # The user can add items the AI never suggested; they must reach the prompt.
    with patch.object(email_drafting, "claude_json", new_callable=AsyncMock) as m:
        m.return_value = {"body": "Please share the RoHS certificate and country of origin."}
        await email_drafting.draft_email(
            "qual_request",
            {
                "vendor_name": "Acme",
                "subject": "RFQ",
                "mpn": "LM358N",
                "items_requested": ["RoHS certificate", "Country of origin"],
            },
        )
    prompt = m.call_args[0][0]
    assert "RoHS certificate" in prompt
    assert "Country of origin" in prompt


@pytest.mark.asyncio
async def test_qual_request_none_on_claude_error():
    with patch.object(email_drafting, "claude_json", new_callable=AsyncMock) as m:
        m.side_effect = ClaudeError("boom")
        result = await email_drafting.draft_email("qual_request", {"vendor_name": "Acme", "items_requested": ["X"]})
    assert result is None


# ── Routes (GET prefill+gaps, POST draft-request, DNC send guard) ────────────
def _setup_offer_with_email(db, requisition, owner_id, *, condition="new", with_email=True):
    req = db.query(Requirement).filter(Requirement.requisition_id == requisition.id).first()
    vr = None
    if with_email:
        vr = VendorResponse(
            requisition_id=requisition.id,
            vendor_name="Acme",
            vendor_email="sales@acme.example",
            subject="RE: RFQ - LM317T",
            body="We can do 5000 pcs at $0.38.",
            classification="quote_provided",
            status="new",
            received_at=datetime.now(UTC),
        )
        db.add(vr)
        db.commit()
        db.refresh(vr)
    o = Offer(
        requisition_id=requisition.id,
        requirement_id=req.id,
        vendor_name="Acme",
        mpn="LM317T",
        unit_price=0.38,
        condition=condition,
        entered_by_id=owner_id,
        status="pending_review",
        vendor_response_id=(vr.id if vr else None),
        created_at=datetime.now(UTC),
    )
    db.add(o)
    db.commit()
    db.refresh(o)
    return req, vr, o


_PARSED = {
    "confidence": 0.9,
    "parts": [{"status": "quoted", "mpn": "LM317T", "unit_price": 0.38, "manufacturer": "TI", "date_code": None}],
}


def test_qualify_ai_get_renders_form_and_gaps(client, db_session, test_requisition, test_user):
    _req, _vr, o = _setup_offer_with_email(db_session, test_requisition, test_user.id, condition="new")
    with patch("app.services.response_parser.parse_vendor_response", new_callable=AsyncMock) as m:
        m.return_value = _PARSED
        resp = client.get(f"/v2/partials/sightings/{o.requirement_id}/offers/{o.id}/qualify-ai")
    assert resp.status_code == 200
    assert "Qualify with AI" in resp.text
    assert "Draft request" in resp.text
    assert "+ Add item" in resp.text  # user can add items the AI didn't suggest
    assert "Date code" in resp.text  # a computed gap row for condition=new


def test_qualify_ai_get_404_without_linked_email(client, db_session, test_requisition, test_user):
    _req, _vr, o = _setup_offer_with_email(db_session, test_requisition, test_user.id, with_email=False)
    resp = client.get(f"/v2/partials/sightings/{o.requirement_id}/offers/{o.id}/qualify-ai")
    assert resp.status_code == 404


def test_qualify_ai_get_blocks_non_owner_sales(client, db_session, test_requisition, test_user, admin_user):
    test_user.role = UserRole.SALES
    test_requisition.created_by = admin_user.id
    db_session.commit()
    _req, _vr, o = _setup_offer_with_email(db_session, test_requisition, admin_user.id)
    resp = client.get(f"/v2/partials/sightings/{o.requirement_id}/offers/{o.id}/qualify-ai")
    assert resp.status_code == 404


def test_qualify_ai_draft_request_merges_ai_and_custom_items(client, db_session, test_requisition, test_user):
    _req, _vr, o = _setup_offer_with_email(db_session, test_requisition, test_user.id)
    with patch("app.services.email_drafting.draft_email", new_callable=AsyncMock) as m:
        m.return_value = {"subject": "Re: RFQ", "body": "Please confirm date code and RoHS."}
        resp = client.post(
            f"/v2/partials/sightings/{o.requirement_id}/offers/{o.id}/qualify-ai/draft-request",
            data={"checked_items": ["Date code"], "custom_items": ["RoHS certificate", ""]},
        )
    assert resp.status_code == 200
    assert "Please confirm date code and RoHS." in resp.text
    assert "send-reply" in resp.text and "<textarea" in resp.text
    items = m.call_args[0][1]["items_requested"]
    assert "Date code" in items and "RoHS certificate" in items and "" not in items


def test_qualify_ai_draft_request_custom_only(client, db_session, test_requisition, test_user):
    _req, _vr, o = _setup_offer_with_email(db_session, test_requisition, test_user.id)
    with patch("app.services.email_drafting.draft_email", new_callable=AsyncMock) as m:
        m.return_value = {"subject": "Re: RFQ", "body": "Please share country of origin."}
        resp = client.post(
            f"/v2/partials/sightings/{o.requirement_id}/offers/{o.id}/qualify-ai/draft-request",
            data={"custom_items": ["Country of origin"]},
        )
    assert resp.status_code == 200
    assert m.call_args[0][1]["items_requested"] == ["Country of origin"]


def test_send_reply_blocks_dnc_vendor(client, db_session, test_requisition, test_company):
    site = CustomerSite(company_id=test_company.id, site_name="Acme")
    db_session.add(site)
    db_session.commit()
    db_session.refresh(site)
    vr = VendorResponse(
        requisition_id=test_requisition.id,
        vendor_name="Acme",
        vendor_email="dnc@acme.example",
        subject="RE: RFQ",
        body="x",
        classification="quote_provided",
        status="new",
        received_at=datetime.now(UTC),
    )
    db_session.add(vr)
    db_session.add(
        SiteContact(customer_site_id=site.id, full_name="Acme", email="dnc@acme.example", do_not_contact=True)
    )
    db_session.commit()
    db_session.refresh(vr)
    resp = client.post(
        f"/v2/partials/requisitions/{test_requisition.id}/responses/{vr.id}/send-reply",
        data={"subject": "Re: RFQ", "body": "asking for info"},
    )
    assert resp.status_code == 200
    assert "do-not-contact" in resp.text
    db_session.refresh(vr)
    assert vr.status != "reviewed"  # blocked, not sent
