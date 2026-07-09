"""Tests for the AI email-drafting HTMX endpoints.

Covers the four endpoints that surface the unified drafting service:
- RFQ rephrase           POST /v2/partials/requisitions/{req_id}/ai-rephrase-email
- follow-up draft        POST /v2/partials/follow-ups/{contact_id}/ai-draft
- vendor reply draft     POST /v2/partials/requisitions/{req_id}/responses/{rid}/ai-draft-reply
- vendor reply send      POST /v2/partials/requisitions/{req_id}/responses/{rid}/send-reply

draft_email is mocked; the send path relies on the TESTING=1 bypass (no real Graph call).
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

from sqlalchemy.orm import Session

from app.constants import UserRole
from app.models.offers import Contact, VendorResponse


def _make_contact(db: Session, req_id: int, user_id: int) -> Contact:
    c = Contact(
        requisition_id=req_id,
        user_id=user_id,
        contact_type="rfq",
        vendor_name="Acme Electronics",
        vendor_contact="sales@acme.example",
        subject="RFQ - LM358N",
        parts_included=["LM358N"],
        status="sent",
        created_at=datetime.now(UTC),
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def _make_response(db: Session, req_id: int) -> VendorResponse:
    vr = VendorResponse(
        requisition_id=req_id,
        vendor_name="Acme Electronics",
        vendor_email="sales@acme.example",
        subject="RE: RFQ - LM358N",
        body="We have 5000 pcs at $0.38, 2 week lead.",
        classification="quote_provided",
        parsed_data={"mpn": "LM358N", "qty": 5000, "price": 0.38},
        status="new",
        received_at=datetime.now(UTC),
    )
    db.add(vr)
    db.commit()
    db.refresh(vr)
    return vr


# ── RFQ rephrase ────────────────────────────────────────────────────────────
def test_ai_rephrase_email_fills_textarea(client, db_session, test_requisition):
    with patch("app.services.email_drafting.draft_email", new_callable=AsyncMock) as m:
        m.return_value = {"body": "Hello team — kindly quote the parts below."}
        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/ai-rephrase-email",
            data={"body": "pls quote these parts"},
        )
    assert resp.status_code == 200
    assert "Hello team" in resp.text
    assert "rfq-body-textarea" in resp.text
    m.assert_awaited_once()


def test_ai_rephrase_email_empty_body_does_not_call_ai(client, db_session, test_requisition):
    with patch("app.services.email_drafting.draft_email", new_callable=AsyncMock) as m:
        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/ai-rephrase-email",
            data={"body": "   "},
        )
    assert resp.status_code == 200
    m.assert_not_awaited()


# ── Follow-up draft ─────────────────────────────────────────────────────────
def test_follow_up_ai_draft_fills_textarea(client, db_session, test_requisition, test_user):
    contact = _make_contact(db_session, test_requisition.id, test_user.id)
    with patch("app.services.email_drafting.draft_email", new_callable=AsyncMock) as m:
        m.return_value = {"body": "Hi Acme, following up on LM358N after 7 days."}
        resp = client.post(f"/v2/partials/follow-ups/{contact.id}/ai-draft")
    assert resp.status_code == 200
    assert "following up" in resp.text.lower()
    assert f"follow-up-body-{contact.id}" in resp.text
    m.assert_awaited_once()


# ── Vendor reply draft ──────────────────────────────────────────────────────
def test_vendor_reply_ai_draft_renders_editable_reply(client, db_session, test_requisition):
    vr = _make_response(db_session, test_requisition.id)
    with patch("app.services.email_drafting.draft_email", new_callable=AsyncMock) as m:
        m.return_value = {"subject": "Re: RFQ - LM358N", "body": "Thanks, we accept."}
        resp = client.post(f"/v2/partials/requisitions/{test_requisition.id}/responses/{vr.id}/ai-draft-reply")
    assert resp.status_code == 200
    assert "Thanks, we accept." in resp.text
    # An editable reply compose block with a send action is rendered.
    assert "send-reply" in resp.text
    assert "<textarea" in resp.text


def test_vendor_reply_ai_draft_none_still_offers_manual_compose(client, db_session, test_requisition):
    vr = _make_response(db_session, test_requisition.id)
    with patch("app.services.email_drafting.draft_email", new_callable=AsyncMock) as m:
        m.return_value = None
        resp = client.post(f"/v2/partials/requisitions/{test_requisition.id}/responses/{vr.id}/ai-draft-reply")
    assert resp.status_code == 200
    # Falls back to a blank editable compose box, not an error.
    assert "<textarea" in resp.text


# ── Vendor reply send ───────────────────────────────────────────────────────
def test_send_reply_marks_and_returns_success(client, db_session, test_requisition):
    vr = _make_response(db_session, test_requisition.id)
    resp = client.post(
        f"/v2/partials/requisitions/{test_requisition.id}/responses/{vr.id}/send-reply",
        data={"subject": "Re: RFQ - LM358N", "body": "Thanks, we accept."},
    )
    assert resp.status_code == 200
    # Response is marked reviewed once a reply has been sent.
    db_session.refresh(vr)
    assert vr.status == "reviewed"


def test_send_reply_rejects_empty_body(client, db_session, test_requisition):
    vr = _make_response(db_session, test_requisition.id)
    resp = client.post(
        f"/v2/partials/requisitions/{test_requisition.id}/responses/{vr.id}/send-reply",
        data={"subject": "Re: x", "body": "   "},
    )
    assert resp.status_code == 400


# ── Authorization (IDOR): SALES/TRADER may only act on their own requisitions ─
def test_vendor_reply_ai_draft_blocks_non_owner_sales(client, db_session, test_requisition, test_user, admin_user):
    test_user.role = UserRole.SALES
    test_requisition.created_by = admin_user.id  # owned by someone else
    db_session.commit()
    vr = _make_response(db_session, test_requisition.id)
    resp = client.post(f"/v2/partials/requisitions/{test_requisition.id}/responses/{vr.id}/ai-draft-reply")
    assert resp.status_code == 404


def test_send_reply_blocks_non_owner_sales(client, db_session, test_requisition, test_user, admin_user):
    test_user.role = UserRole.SALES
    test_requisition.created_by = admin_user.id
    db_session.commit()
    vr = _make_response(db_session, test_requisition.id)
    resp = client.post(
        f"/v2/partials/requisitions/{test_requisition.id}/responses/{vr.id}/send-reply",
        data={"subject": "Re: x", "body": "should not send"},
    )
    assert resp.status_code == 404
    db_session.refresh(vr)
    assert vr.status == "new"  # nothing was sent or mutated


def test_follow_up_ai_draft_blocks_non_owner_sales(client, db_session, test_requisition, test_user, admin_user):
    test_user.role = UserRole.SALES
    test_requisition.created_by = admin_user.id
    db_session.commit()
    contact = _make_contact(db_session, test_requisition.id, admin_user.id)
    resp = client.post(f"/v2/partials/follow-ups/{contact.id}/ai-draft")
    assert resp.status_code == 404


# ── Same-class gap on the pre-existing sibling endpoints (review + follow-up send) ──
def test_review_response_blocks_non_owner_sales(client, db_session, test_requisition, test_user, admin_user):
    test_user.role = UserRole.SALES
    test_requisition.created_by = admin_user.id
    db_session.commit()
    vr = _make_response(db_session, test_requisition.id)
    resp = client.post(
        f"/v2/partials/requisitions/{test_requisition.id}/responses/{vr.id}/review",
        data={"status": "reviewed"},
    )
    assert resp.status_code == 404
    db_session.refresh(vr)
    assert vr.status == "new"  # not mutated


def test_send_follow_up_blocks_non_owner_sales(client, db_session, test_requisition, test_user, admin_user):
    test_user.role = UserRole.SALES
    test_requisition.created_by = admin_user.id
    db_session.commit()
    contact = _make_contact(db_session, test_requisition.id, admin_user.id)
    resp = client.post(f"/v2/partials/follow-ups/{contact.id}/send", data={"body": "should not send"})
    assert resp.status_code == 404
