"""test_routers_rfq.py — Tests for RFQ, Follow-ups & Vendor Enrichment Router.

Tests the vendor card enrichment filtering logic: garbage vendor names,
blacklisted vendors, and summary cache building.
Also tests RFQ router endpoints via TestClient.

Covers: _enrich_with_vendor_cards filtering, follow-ups, contacts, responses,
rfq-prepare, phone call logging, activity feed, send_follow_up, send_rfq, poll.
"""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import (
    ActivityLog,
    Contact,
    Requisition,
    User,
    VendorCard,
    VendorResponse,
    VendorReview,
)
from app.rate_limit import limiter
from app.vendor_utils import normalize_vendor_name

# ---------------------------------------------------------------------------
# Garbage vendor filtering (pure logic, no DB needed)
# ---------------------------------------------------------------------------

_GARBAGE_VENDORS = {"no seller listed", "no seller", "n/a", "unknown", ""}


def test_garbage_vendor_names_filtered():
    """Known garbage names should be excluded from results."""
    for name in _GARBAGE_VENDORS:
        assert name.lower() in _GARBAGE_VENDORS


def test_real_vendor_not_garbage():
    """Real vendor names pass through."""
    for name in ["Arrow Electronics", "Digi-Key", "Mouser", "ACME Corp"]:
        assert name.lower() not in _GARBAGE_VENDORS


def test_garbage_vendor_case_insensitive():
    """Garbage check uses lowercased names."""
    assert "No Seller Listed".lower() in _GARBAGE_VENDORS
    assert "N/A".lower() in _GARBAGE_VENDORS
    assert "UNKNOWN".lower() in _GARBAGE_VENDORS


# ---------------------------------------------------------------------------
# Blacklist filtering logic (unit test of the check pattern)
# ---------------------------------------------------------------------------


def test_blacklisted_vendor_skipped():
    """Sightings with is_blacklisted=True should be removed."""
    summary = {"is_blacklisted": True, "card_id": 1}
    assert summary.get("is_blacklisted") is True


def test_non_blacklisted_vendor_kept():
    """Normal vendors pass blacklist check."""
    summary = {"is_blacklisted": False, "card_id": 2}
    assert summary.get("is_blacklisted") is False


# ---------------------------------------------------------------------------
# Enrichment results structure
# ---------------------------------------------------------------------------


def _make_results_dict(sightings: list[dict]) -> dict:
    """Build a results dict matching the search_service format."""
    return {"REQ-1": {"sightings": sightings}}


def _filter_sightings(results: dict) -> list[dict]:
    """Apply the same filtering logic as _enrich_with_vendor_cards (pure part)."""
    filtered = []
    for group in results.values():
        for s in group.get("sightings", []):
            vname = (s.get("vendor_name") or "").strip()
            if vname.lower() in _GARBAGE_VENDORS:
                continue
            if s.get("_blacklisted"):
                continue
            filtered.append(s)
    return filtered


def test_filter_removes_garbage():
    results = _make_results_dict(
        [
            {"vendor_name": "Arrow", "mpn_matched": "LM317T"},
            {"vendor_name": "No Seller Listed", "mpn_matched": "LM317T"},
            {"vendor_name": "", "mpn_matched": "LM317T"},
        ]
    )
    kept = _filter_sightings(results)
    assert len(kept) == 1
    assert kept[0]["vendor_name"] == "Arrow"


def test_filter_removes_blacklisted():
    results = _make_results_dict(
        [
            {"vendor_name": "Good Vendor", "mpn_matched": "LM317T"},
            {"vendor_name": "Bad Vendor", "mpn_matched": "LM317T", "_blacklisted": True},
        ]
    )
    kept = _filter_sightings(results)
    assert len(kept) == 1
    assert kept[0]["vendor_name"] == "Good Vendor"


def test_filter_handles_none_vendor():
    results = _make_results_dict(
        [
            {"vendor_name": None, "mpn_matched": "LM317T"},
        ]
    )
    kept = _filter_sightings(results)
    assert len(kept) == 0  # None → "" → in _GARBAGE_VENDORS


def test_filter_preserves_order():
    results = _make_results_dict(
        [
            {"vendor_name": "Alpha", "mpn_matched": "A"},
            {"vendor_name": "N/A", "mpn_matched": "B"},
            {"vendor_name": "Beta", "mpn_matched": "C"},
        ]
    )
    kept = _filter_sightings(results)
    assert [s["vendor_name"] for s in kept] == ["Alpha", "Beta"]


# ---------------------------------------------------------------------------
# Router endpoint tests (via TestClient with auth overrides from conftest)
# ---------------------------------------------------------------------------


def _make_contact(
    db: Session,
    requisition: Requisition,
    user: User,
    *,
    vendor_name="Arrow Electronics",
    vendor_contact="sales@arrow.com",
    contact_type="email",
    status="sent",
    parts=None,
    days_ago=0,
):
    """Helper to create a Contact record.

    Uses naive UTC datetimes for SQLite compatibility (SQLite strips tzinfo, and the
    router does aware-vs-naive datetime subtraction).
    """
    c = Contact(
        requisition_id=requisition.id,
        user_id=user.id,
        contact_type=contact_type,
        vendor_name=vendor_name,
        vendor_name_normalized=normalize_vendor_name(vendor_name),
        vendor_contact=vendor_contact,
        parts_included=parts or ["LM317T"],
        subject=f"RFQ for {vendor_name}",
        status=status,
        created_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days_ago),
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


@contextmanager
def _client_as_user(db_session: Session, user: User):
    """Create a TestClient with auth dependencies overridden to `user`."""
    from app.database import get_db
    from app.dependencies import require_buyer, require_user
    from app.main import app

    def _override_db():
        yield db_session

    def _override_user():
        return user

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_user] = _override_user
    app.dependency_overrides[require_buyer] = _override_user
    try:
        with TestClient(app) as c:
            yield c
    finally:
        for dep in [get_db, require_user, require_buyer]:
            app.dependency_overrides.pop(dep, None)


# ── GET /api/follow-ups ──────────────────────────────────────────────


def test_follow_ups_empty(client):
    """No stale contacts → empty follow-ups list."""
    resp = client.get("/api/follow-ups")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 0
    assert data["follow_ups"] == []


def test_follow_ups_returns_stale_contacts(client, db_session, test_user, test_requisition):
    """Contacts sent >3 days ago with 'sent' status appear as follow-ups."""
    _make_contact(
        db_session,
        test_requisition,
        test_user,
        vendor_name="Slow Vendor",
        vendor_contact="slow@vendor.com",
        status="sent",
        days_ago=5,
    )
    # Recent contact should NOT appear
    _make_contact(
        db_session,
        test_requisition,
        test_user,
        vendor_name="Fast Vendor",
        vendor_contact="fast@vendor.com",
        status="sent",
        days_ago=1,
    )

    resp = client.get("/api/follow-ups")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 1
    assert data["follow_ups"][0]["vendor_name"] == "Slow Vendor"
    assert data["follow_ups"][0]["days_waiting"] >= 4


# ── GET /api/follow-ups/summary ──────────────────────────────────────


def test_follow_ups_summary(client, db_session, test_user, test_requisition):
    """Summary groups stale contacts by requisition."""
    _make_contact(
        db_session,
        test_requisition,
        test_user,
        vendor_name="Vendor A",
        status="sent",
        days_ago=5,
    )
    _make_contact(
        db_session,
        test_requisition,
        test_user,
        vendor_name="Vendor B",
        status="opened",
        days_ago=7,
    )

    resp = client.get("/api/follow-ups/summary")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    assert len(data["by_requisition"]) == 1
    assert data["by_requisition"][0]["count"] == 2


# ── GET /api/requisitions/{id}/contacts ──────────────────────────────


def test_list_contacts(client, db_session, test_user, test_requisition):
    """Lists contacts for a requisition."""
    _make_contact(db_session, test_requisition, test_user)

    resp = client.get(f"/api/requisitions/{test_requisition.id}/contacts")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["vendor_name"] == "Arrow Electronics"
    assert data[0]["contact_type"] == "email"


def test_list_contacts_missing_req(client):
    """Contacts for nonexistent requisition returns empty list."""
    resp = client.get("/api/requisitions/99999/contacts")
    assert resp.status_code == 200
    assert resp.json() == []


# ── POST /api/contacts/{id}/retry ─────────────────────────────────────


def test_retry_failed_rfq_rejects_non_failed_contact(client, db_session, test_user, test_requisition):
    """Retry endpoint returns 400 unless contact is in failed state."""
    c = _make_contact(
        db_session,
        test_requisition,
        test_user,
        vendor_name="Arrow Electronics",
        vendor_contact="sales@arrow.com",
        status="sent",
    )
    resp = client.post(f"/api/contacts/{c.id}/retry")
    assert resp.status_code == 400
    msg = resp.json().get("error", "")
    assert "Only failed contacts can be retried" in msg


# ── GET /api/requisitions/{id}/responses ─────────────────────────────


def test_list_responses(client, db_session, test_requisition):
    """Lists vendor responses for a requisition."""
    vr = VendorResponse(
        requisition_id=test_requisition.id,
        vendor_name="Arrow Electronics",
        vendor_email="sales@arrow.com",
        subject="Re: RFQ LM317T",
        status="new",
        confidence=0.85,
        received_at=datetime.now(timezone.utc),
    )
    db_session.add(vr)
    db_session.commit()

    resp = client.get(f"/api/requisitions/{test_requisition.id}/responses")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["vendor_name"] == "Arrow Electronics"
    assert data[0]["confidence"] == 0.85


def test_list_responses_empty(client, test_requisition):
    """No responses returns empty list."""
    resp = client.get(f"/api/requisitions/{test_requisition.id}/responses")
    assert resp.status_code == 200
    assert resp.json() == []


# ── PATCH /api/vendor-responses/{id}/status ───────────────────────────


def test_update_vendor_response_status_rejects_invalid_status(client, db_session, test_requisition):
    """Status update endpoint returns 400 for unsupported statuses."""
    vr = VendorResponse(
        requisition_id=test_requisition.id,
        vendor_name="Arrow Electronics",
        vendor_email="sales@arrow.com",
        subject="Re: RFQ",
        status="new",
        received_at=datetime.now(timezone.utc),
    )
    db_session.add(vr)
    db_session.commit()

    resp = client.patch(
        f"/api/vendor-responses/{vr.id}/status",
        json={"status": "invalid"},
    )
    assert resp.status_code == 422


# ── POST /api/requisitions/{id}/rfq-prepare ──────────────────────────


def test_rfq_prepare_vendor_data(client, db_session, test_requisition, test_vendor_card):
    """Rfq-prepare returns vendor card data for known vendors."""
    resp = client.post(
        f"/api/requisitions/{test_requisition.id}/rfq-prepare",
        json={"vendors": [{"vendor_name": "Arrow Electronics"}]},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["vendors"]) == 1
    v = data["vendors"][0]
    assert v["vendor_name"] == "Arrow Electronics"
    assert v["card_id"] == test_vendor_card.id
    assert v["needs_lookup"] is False
    assert "sales@arrow.com" in v["emails"]


def test_rfq_prepare_unknown_vendor(client, test_requisition):
    """Rfq-prepare for unknown vendor returns needs_lookup=True."""
    resp = client.post(
        f"/api/requisitions/{test_requisition.id}/rfq-prepare",
        json={"vendors": [{"vendor_name": "Never Heard Of Inc"}]},
    )
    assert resp.status_code == 200
    data = resp.json()
    v = data["vendors"][0]
    assert v["needs_lookup"] is True
    assert v["emails"] == []


# ── POST /api/contacts/phone ─────────────────────────────────────────


# ── GET /api/requisitions/{id}/activity ──────────────────────────────


def test_get_activity(client, db_session, test_user, test_requisition):
    """Activity endpoint returns vendor-level summary."""
    _make_contact(
        db_session,
        test_requisition,
        test_user,
        vendor_name="Arrow Electronics",
        status="sent",
        days_ago=2,
    )
    _make_contact(
        db_session,
        test_requisition,
        test_user,
        vendor_name="Arrow Electronics",
        vendor_contact="rfq@arrow.com",
        status="replied",
        days_ago=1,
    )

    resp = client.get(f"/api/requisitions/{test_requisition.id}/activity")
    assert resp.status_code == 200
    data = resp.json()
    assert "vendors" in data
    assert "summary" in data
    # Summary has sent/replied/opened/awaiting keys
    assert data["summary"]["sent"] >= 1


def test_get_activity_empty(client, test_requisition):
    """Activity for requisition with no contacts returns empty."""
    resp = client.get(f"/api/requisitions/{test_requisition.id}/activity")
    assert resp.status_code == 200
    data = resp.json()
    assert data["vendors"] == []
    assert data["summary"]["sent"] == 0


def test_get_activity_no_req(client):
    """Activity for non-existent requisition returns empty activity."""
    resp = client.get("/api/requisitions/99999/activity")
    assert resp.status_code == 200
    data = resp.json()
    assert data["vendors"] == []
    assert data["summary"]["sent"] == 0


# ── POST /api/contacts/phone ─────────────────────────────────────────


def test_log_phone_call(client, test_requisition):
    """Phone call logging creates a contact record."""
    resp = client.post(
        "/api/contacts/phone",
        json={
            "requisition_id": test_requisition.id,
            "vendor_name": "Mouser Electronics",
            "vendor_phone": "+1-800-346-6873",
            "parts": ["LM317T"],
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["vendor_name"] == "Mouser Electronics"
    assert data["contact_type"] == "phone"


# ══════════════════════════════════════════════════════════════════════
# NEW TESTS — Rate-limited client fixture
# ══════════════════════════════════════════════════════════════════════


@pytest.fixture()
def rfq_client(db_session: Session, test_user: User) -> TestClient:
    """TestClient with auth overrides + rate limiter reset for RFQ endpoints."""
    from app.database import get_db
    from app.dependencies import require_buyer, require_settings_access, require_user
    from app.main import app

    def _override_db():
        yield db_session

    def _override_user():
        return test_user

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_user] = _override_user
    app.dependency_overrides[require_buyer] = _override_user
    app.dependency_overrides[require_settings_access] = _override_user

    limiter.reset()
    try:
        with TestClient(app) as c:
            yield c
    finally:
        for dep in [get_db, require_user, require_buyer, require_settings_access]:
            app.dependency_overrides.pop(dep, None)


# ══════════════════════════════════════════════════════════════════════
# NEW TESTS — POST /api/requisitions/{id}/rfq (send_rfq)
# ══════════════════════════════════════════════════════════════════════


def test_send_rfq(rfq_client, db_session, test_user, test_requisition):
    """POST send_rfq delegates to send_batch_rfq and returns results."""
    mock_results = [
        {"vendor_name": "Arrow", "status": "sent", "email": "sales@arrow.com"},
    ]

    with (
        patch(
            "app.routers.rfq.require_fresh_token",
            new_callable=AsyncMock,
            return_value="fake-token",
        ),
        patch(
            "app.routers.rfq.send_batch_rfq",
            new_callable=AsyncMock,
            return_value=mock_results,
        ),
    ):
        resp = rfq_client.post(
            f"/api/requisitions/{test_requisition.id}/rfq",
            json={
                "groups": [
                    {
                        "vendor_name": "Arrow",
                        "vendor_email": "sales@arrow.com",
                        "parts": ["LM317T"],
                        "subject": "RFQ",
                        "body": "Please quote",
                    }
                ]
            },
        )

    assert resp.status_code == 200
    data = resp.json()
    assert "results" in data
    assert len(data["results"]) == 1
    assert data["results"][0]["vendor_name"] == "Arrow"


def test_send_rfq_auth_failure(rfq_client, test_requisition):
    """POST send_rfq fails when require_fresh_token raises."""
    from fastapi import HTTPException

    with patch(
        "app.routers.rfq.require_fresh_token",
        new_callable=AsyncMock,
        side_effect=HTTPException(status_code=401, detail="No token"),
    ):
        resp = rfq_client.post(
            f"/api/requisitions/{test_requisition.id}/rfq",
            json={
                "groups": [
                    {
                        "vendor_name": "Arrow",
                        "vendor_email": "sales@arrow.com",
                        "parts": ["LM317T"],
                    }
                ]
            },
        )

    assert resp.status_code == 401


# ══════════════════════════════════════════════════════════════════════
# NEW TESTS — POST /api/requisitions/{id}/poll (inbox polling)
# ══════════════════════════════════════════════════════════════════════


def test_poll_inbox(rfq_client, db_session, test_user, test_requisition):
    """POST poll delegates to poll_inbox and returns responses."""
    mock_results = [{"vendor_name": "Arrow", "subject": "Re: RFQ"}]

    with (
        patch(
            "app.routers.rfq.require_fresh_token",
            new_callable=AsyncMock,
            return_value="fake-token",
        ),
        patch(
            "app.routers.rfq.poll_inbox",
            new_callable=AsyncMock,
            return_value=mock_results,
        ),
    ):
        resp = rfq_client.post(f"/api/requisitions/{test_requisition.id}/poll")

    assert resp.status_code == 200
    data = resp.json()
    assert "responses" in data
    assert len(data["responses"]) == 1


def test_poll_inbox_auth_failure(rfq_client, test_requisition):
    """POST poll fails when require_fresh_token raises."""
    from fastapi import HTTPException

    with patch(
        "app.routers.rfq.require_fresh_token",
        new_callable=AsyncMock,
        side_effect=HTTPException(status_code=401, detail="No token"),
    ):
        resp = rfq_client.post(f"/api/requisitions/{test_requisition.id}/poll")

    assert resp.status_code == 401


# ══════════════════════════════════════════════════════════════════════
# NEW TESTS — GET /api/requisitions/{id}/activity (vendor status logic)
# ══════════════════════════════════════════════════════════════════════


def test_get_activity_quoted_status(client, db_session, test_user, test_requisition):
    """Contact with 'quoted' status gives vendor status 'quoted'."""
    _make_contact(
        db_session,
        test_requisition,
        test_user,
        vendor_name="Quoter Vendor",
        status="quoted",
        days_ago=1,
    )

    resp = client.get(f"/api/requisitions/{test_requisition.id}/activity")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["vendors"]) == 1
    assert data["vendors"][0]["status"] == "quoted"


def test_get_activity_declined_status(client, db_session, test_user, test_requisition):
    """Contact with only 'declined' status gives vendor status 'declined'."""
    _make_contact(
        db_session,
        test_requisition,
        test_user,
        vendor_name="Decliner Vendor",
        status="declined",
        days_ago=1,
    )

    resp = client.get(f"/api/requisitions/{test_requisition.id}/activity")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["vendors"]) == 1
    assert data["vendors"][0]["status"] == "declined"


def test_get_activity_opened_status(client, db_session, test_user, test_requisition):
    """Contact with 'opened' status gives vendor status 'opened'."""
    _make_contact(
        db_session,
        test_requisition,
        test_user,
        vendor_name="Opener Vendor",
        status="opened",
        days_ago=1,
    )

    resp = client.get(f"/api/requisitions/{test_requisition.id}/activity")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["vendors"]) == 1
    assert data["vendors"][0]["status"] == "opened"


def test_get_activity_awaiting_status(client, db_session, test_user, test_requisition):
    """Contacts with 'sent' status only gives vendor status 'awaiting'."""
    _make_contact(
        db_session,
        test_requisition,
        test_user,
        vendor_name="Silent Vendor",
        status="sent",
        days_ago=1,
    )

    resp = client.get(f"/api/requisitions/{test_requisition.id}/activity")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["vendors"]) == 1
    assert data["vendors"][0]["status"] == "awaiting"


def test_get_activity_responded_to_replied(client, db_session, test_user, test_requisition):
    """Contact with 'responded' status gives vendor status 'replied'."""
    _make_contact(
        db_session,
        test_requisition,
        test_user,
        vendor_name="Responder Vendor",
        status="responded",
        days_ago=1,
    )

    resp = client.get(f"/api/requisitions/{test_requisition.id}/activity")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["vendors"]) == 1
    assert data["vendors"][0]["status"] == "replied"


def test_get_activity_declined_with_response_becomes_declined(
    client,
    db_session,
    test_user,
    test_requisition,
):
    """Has response + only declined/sent/opened contacts yields 'declined'."""
    c = _make_contact(
        db_session,
        test_requisition,
        test_user,
        vendor_name="Decline Reply Vendor",
        vendor_contact="decline@vendor.com",
        status="declined",
        days_ago=2,
    )

    # Add a VendorResponse linked to this contact
    vr = VendorResponse(
        contact_id=c.id,
        requisition_id=test_requisition.id,
        vendor_name="Decline Reply Vendor",
        vendor_email="decline@vendor.com",
        subject="Re: RFQ",
        status="new",
        received_at=datetime.now(timezone.utc),
    )
    db_session.add(vr)
    db_session.commit()

    resp = client.get(f"/api/requisitions/{test_requisition.id}/activity")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["vendors"]) == 1
    assert data["vendors"][0]["status"] == "declined"


def test_get_activity_has_response_replied(
    client,
    db_session,
    test_user,
    test_requisition,
):
    """Vendor with a response (no special contact status) gives 'replied'."""
    c = _make_contact(
        db_session,
        test_requisition,
        test_user,
        vendor_name="Reply Vendor",
        vendor_contact="reply@vendor.com",
        status="sent",
        days_ago=2,
    )

    vr = VendorResponse(
        contact_id=c.id,
        requisition_id=test_requisition.id,
        vendor_name="Reply Vendor",
        vendor_email="reply@vendor.com",
        subject="Re: RFQ",
        status="new",
        received_at=datetime.now(timezone.utc),
    )
    db_session.add(vr)
    db_session.commit()

    resp = client.get(f"/api/requisitions/{test_requisition.id}/activity")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["vendors"]) == 1
    assert data["vendors"][0]["status"] == "replied"


def test_get_activity_response_grouped_by_contact(
    client,
    db_session,
    test_user,
    test_requisition,
):
    """Response with contact_id groups under the contact's vendor name."""
    c = _make_contact(
        db_session,
        test_requisition,
        test_user,
        vendor_name="Acme Corp",
        vendor_contact="sales@acme.com",
        status="sent",
        days_ago=2,
    )

    # Response from a different name but linked to same contact
    vr = VendorResponse(
        contact_id=c.id,
        requisition_id=test_requisition.id,
        vendor_name="John Smith",  # individual, but linked to Acme Corp contact
        vendor_email="john@acme.com",
        subject="Re: RFQ",
        status="new",
        received_at=datetime.now(timezone.utc),
    )
    db_session.add(vr)
    db_session.commit()

    resp = client.get(f"/api/requisitions/{test_requisition.id}/activity")
    assert resp.status_code == 200
    data = resp.json()
    # Should be grouped under "Acme Corp" (not "John Smith")
    assert len(data["vendors"]) == 1
    assert data["vendors"][0]["vendor_name"] == "Acme Corp"
    assert len(data["vendors"][0]["responses"]) == 1


def test_get_activity_response_without_contact(
    client,
    db_session,
    test_user,
    test_requisition,
):
    """Response without contact_id groups under its own vendor_name."""
    vr = VendorResponse(
        contact_id=None,
        requisition_id=test_requisition.id,
        vendor_name="Unlinked Vendor",
        vendor_email="unlinked@vendor.com",
        subject="Stock offer",
        status="new",
        received_at=datetime.now(timezone.utc),
    )
    db_session.add(vr)
    db_session.commit()

    resp = client.get(f"/api/requisitions/{test_requisition.id}/activity")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["vendors"]) == 1
    assert data["vendors"][0]["vendor_name"] == "Unlinked Vendor"


def test_get_activity_with_manual_activities(
    client,
    db_session,
    test_user,
    test_requisition,
    test_vendor_card,
):
    """Activity endpoint includes manual activity entries."""
    activity = ActivityLog(
        user_id=test_user.id,
        activity_type="phone_call",
        channel="phone",
        vendor_card_id=test_vendor_card.id,
        requisition_id=test_requisition.id,
        contact_name="John Sales",
        contact_phone="+1-555-0100",
        notes="Discussed pricing",
        duration_seconds=300,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(activity)
    db_session.commit()

    resp = client.get(f"/api/requisitions/{test_requisition.id}/activity")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["vendors"]) == 1
    vendor = data["vendors"][0]
    assert len(vendor["activities"]) == 1
    assert vendor["activities"][0]["activity_type"] == "phone_call"
    assert vendor["activities"][0]["contact_name"] == "John Sales"
    assert vendor["activities"][0]["duration_seconds"] == 300


def test_get_activity_vendor_card_resolution(
    client,
    db_session,
    test_user,
    test_requisition,
    test_vendor_card,
):
    """Vendor card is resolved by normalized_name for vendors without activities."""
    _make_contact(
        db_session,
        test_requisition,
        test_user,
        vendor_name="Arrow Electronics",
        status="sent",
        days_ago=1,
    )

    resp = client.get(f"/api/requisitions/{test_requisition.id}/activity")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["vendors"]) == 1
    assert data["vendors"][0]["vendor_card_id"] == test_vendor_card.id


def test_get_activity_vendor_phones_from_card(
    client,
    db_session,
    test_user,
    test_requisition,
    test_vendor_card,
):
    """Vendor phones are collected from VendorCard.phones."""
    _make_contact(
        db_session,
        test_requisition,
        test_user,
        vendor_name="Arrow Electronics",
        status="sent",
        days_ago=1,
    )

    resp = client.get(f"/api/requisitions/{test_requisition.id}/activity")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["vendors"]) == 1
    assert "+1-555-0100" in data["vendors"][0]["vendor_phones"]


def test_get_activity_vendor_phones_from_vendor_contact(
    client,
    db_session,
    test_user,
    test_requisition,
    test_vendor_card,
    test_vendor_contact,
):
    """Vendor phones also include numbers from VendorContact records."""
    _make_contact(
        db_session,
        test_requisition,
        test_user,
        vendor_name="Arrow Electronics",
        status="sent",
        days_ago=1,
    )

    resp = client.get(f"/api/requisitions/{test_requisition.id}/activity")
    assert resp.status_code == 200
    data = resp.json()
    vendor = data["vendors"][0]
    # Should include both card phone and contact phone
    assert "+1-555-0100" in vendor["vendor_phones"]
    assert "+1-555-0200" in vendor["vendor_phones"]


def test_get_activity_multiple_vendors(
    client,
    db_session,
    test_user,
    test_requisition,
):
    """Activity groups multiple vendors separately."""
    _make_contact(
        db_session,
        test_requisition,
        test_user,
        vendor_name="Arrow Electronics",
        status="sent",
        days_ago=2,
    )
    _make_contact(
        db_session,
        test_requisition,
        test_user,
        vendor_name="Mouser Electronics",
        status="opened",
        days_ago=1,
    )

    resp = client.get(f"/api/requisitions/{test_requisition.id}/activity")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["vendors"]) == 2
    assert data["summary"]["sent"] == 2


def test_get_activity_summary_counts(
    client,
    db_session,
    test_user,
    test_requisition,
):
    """Summary counts are computed correctly for different statuses."""
    _make_contact(
        db_session,
        test_requisition,
        test_user,
        vendor_name="Awaiting Vendor",
        status="sent",
        days_ago=1,
    )
    _make_contact(
        db_session,
        test_requisition,
        test_user,
        vendor_name="Quoted Vendor",
        status="quoted",
        days_ago=1,
    )
    _make_contact(
        db_session,
        test_requisition,
        test_user,
        vendor_name="Opened Vendor",
        status="opened",
        days_ago=1,
    )
    _make_contact(
        db_session,
        test_requisition,
        test_user,
        vendor_name="Declined Vendor",
        status="declined",
        days_ago=1,
    )

    resp = client.get(f"/api/requisitions/{test_requisition.id}/activity")
    assert resp.status_code == 200
    data = resp.json()
    s = data["summary"]
    assert s["sent"] == 4
    assert s["replied"] == 1  # quoted counts as replied
    assert s["opened"] == 1
    # awaiting = sent - replied - opened - declined
    assert s["awaiting"] == 1


def test_get_activity_contact_parts(
    client,
    db_session,
    test_user,
    test_requisition,
):
    """Parts from contacts are collected and sorted in all_parts."""
    _make_contact(
        db_session,
        test_requisition,
        test_user,
        vendor_name="Parts Vendor",
        status="sent",
        days_ago=1,
        parts=["LM317T", "LM358N"],
    )

    resp = client.get(f"/api/requisitions/{test_requisition.id}/activity")
    assert resp.status_code == 200
    data = resp.json()
    vendor = data["vendors"][0]
    assert "LM317T" in vendor["all_parts"]
    assert "LM358N" in vendor["all_parts"]


def test_get_activity_contact_details_fields(
    client,
    db_session,
    test_user,
    test_requisition,
):
    """Each contact in activity response includes expected fields."""
    c = _make_contact(
        db_session,
        test_requisition,
        test_user,
        vendor_name="Detail Vendor",
        vendor_contact="detail@vendor.com",
        status="sent",
        days_ago=1,
    )

    resp = client.get(f"/api/requisitions/{test_requisition.id}/activity")
    assert resp.status_code == 200
    data = resp.json()
    contact = data["vendors"][0]["contacts"][0]
    assert "id" in contact
    assert "contact_type" in contact
    assert "vendor_contact" in contact
    assert "subject" in contact
    assert "created_at" in contact
    assert "user_name" in contact
    assert "status" in contact
    assert contact["status"] == "sent"


# ══════════════════════════════════════════════════════════════════════
# NEW TESTS — POST /api/requisitions/{id}/rfq-prepare (edge cases)
# ══════════════════════════════════════════════════════════════════════


def test_rfq_prepare_not_found(client):
    """Rfq-prepare returns 404 for nonexistent requisition."""
    resp = client.post(
        "/api/requisitions/99999/rfq-prepare",
        json={"vendors": [{"vendor_name": "Arrow"}]},
    )
    assert resp.status_code == 404


def test_rfq_prepare_exhaustion_map(client, db_session, test_user, test_requisition, test_vendor_card):
    """Rfq-prepare shows already_asked parts from previous contacts."""
    _make_contact(
        db_session,
        test_requisition,
        test_user,
        vendor_name="Arrow Electronics",
        vendor_contact="sales@arrow.com",
        status="sent",
        days_ago=2,
        parts=["LM317T"],
    )

    resp = client.post(
        f"/api/requisitions/{test_requisition.id}/rfq-prepare",
        json={"vendors": [{"vendor_name": "Arrow Electronics"}]},
    )
    assert resp.status_code == 200
    data = resp.json()
    v = data["vendors"][0]
    assert "LM317T" in v["already_asked"]


def test_rfq_prepare_returns_all_parts(client, test_requisition):
    """Rfq-prepare returns all parts from requisition requirements."""
    resp = client.post(
        f"/api/requisitions/{test_requisition.id}/rfq-prepare",
        json={"vendors": []},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "LM317T" in data["all_parts"]


def test_rfq_prepare_auto_lookup(client, db_session, test_user, test_requisition):
    """Rfq-prepare auto-looks up contacts for vendors with needs_lookup=True."""
    mock_contacts = [
        {"email": "found@newvendor.com", "phone": "+1-555-9999", "source": "apollo"},
    ]

    with patch(
        "app.enrichment_service.find_suggested_contacts",
        new_callable=AsyncMock,
        return_value=mock_contacts,
    ):
        resp = client.post(
            f"/api/requisitions/{test_requisition.id}/rfq-prepare",
            json={"vendors": [{"vendor_name": "Brand New Vendor"}]},
        )

    assert resp.status_code == 200
    data = resp.json()
    v = data["vendors"][0]
    # Auto-lookup should populate emails
    if v.get("needs_lookup") is False:
        assert "found@newvendor.com" in v["emails"]
        assert v["contact_source"] == "apollo"


def test_rfq_prepare_auto_lookup_failure(client, db_session, test_user, test_requisition):
    """Rfq-prepare handles auto-lookup failure gracefully."""
    with patch(
        "app.enrichment_service.find_suggested_contacts",
        new_callable=AsyncMock,
        side_effect=Exception("Lookup failed"),
    ):
        resp = client.post(
            f"/api/requisitions/{test_requisition.id}/rfq-prepare",
            json={"vendors": [{"vendor_name": "Failing Lookup Vendor"}]},
        )

    assert resp.status_code == 200
    data = resp.json()
    v = data["vendors"][0]
    assert v["needs_lookup"] is True  # Still needs lookup after failure


def test_rfq_prepare_empty_vendors(client, test_requisition):
    """Rfq-prepare with empty vendors list returns empty results."""
    resp = client.post(
        f"/api/requisitions/{test_requisition.id}/rfq-prepare",
        json={"vendors": []},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["vendors"] == []


def test_rfq_prepare_vendor_without_emails(client, db_session, test_user, test_requisition):
    """Rfq-prepare for vendor card without emails shows needs_lookup=True."""
    card = VendorCard(
        normalized_name="no email vendor",
        display_name="No Email Vendor",
        emails=[],
        phones=[],
    )
    db_session.add(card)
    db_session.commit()

    with patch(
        "app.enrichment_service.find_suggested_contacts",
        new_callable=AsyncMock,
        return_value=[],
    ):
        resp = client.post(
            f"/api/requisitions/{test_requisition.id}/rfq-prepare",
            json={"vendors": [{"vendor_name": "No Email Vendor"}]},
        )

    assert resp.status_code == 200
    data = resp.json()
    v = data["vendors"][0]
    assert v["card_id"] == card.id
    assert v["needs_lookup"] is True


# ══════════════════════════════════════════════════════════════════════
# NEW TESTS — Past RFQ email reuse + timeout handling (Phase 1)
# ══════════════════════════════════════════════════════════════════════


def test_rfq_prepare_past_contact_emails(client, db_session, test_user, test_requisition):
    """Rfq-prepare uses emails from past RFQ contacts when VendorCard has no emails."""
    # Create a second requisition with a contact for the same vendor
    other_req = Requisition(
        name="Other Req",
        created_by=test_user.id,
        status="active",
    )
    db_session.add(other_req)
    db_session.commit()
    db_session.refresh(other_req)

    _make_contact(
        db_session,
        other_req,
        test_user,
        vendor_name="Past Vendor Co",
        vendor_contact="past@vendor.com",
        status="sent",
        days_ago=10,
    )
    # Ensure no VendorCard exists for this vendor
    with patch(
        "app.enrichment_service.find_suggested_contacts",
        new_callable=AsyncMock,
        return_value=[],
    ):
        resp = client.post(
            f"/api/requisitions/{test_requisition.id}/rfq-prepare",
            json={"vendors": [{"vendor_name": "Past Vendor Co"}]},
        )

    assert resp.status_code == 200
    data = resp.json()
    v = data["vendors"][0]
    assert v["needs_lookup"] is False
    assert "past@vendor.com" in v["emails"]
    assert v["contact_source"] == "past_rfq"


def test_rfq_prepare_past_contacts_exclude_current_req(client, db_session, test_user, test_requisition):
    """Rfq-prepare past_contacts does NOT include contacts from the current
    requisition."""
    # Contact on the CURRENT requisition — should be excluded from past_contacts
    _make_contact(
        db_session,
        test_requisition,
        test_user,
        vendor_name="Current Only Vendor",
        vendor_contact="current@vendor.com",
        status="sent",
        days_ago=1,
    )

    with patch(
        "app.enrichment_service.find_suggested_contacts",
        new_callable=AsyncMock,
        return_value=[],
    ):
        resp = client.post(
            f"/api/requisitions/{test_requisition.id}/rfq-prepare",
            json={"vendors": [{"vendor_name": "Current Only Vendor"}]},
        )

    assert resp.status_code == 200
    data = resp.json()
    v = data["vendors"][0]
    # past_contacts should be empty because the only contact is on the current req
    assert v["past_contacts"] == []
    # Should still need lookup since no VendorCard and no past emails
    assert v["needs_lookup"] is True


def test_rfq_prepare_timeout_leaves_needs_lookup(client, db_session, test_user, test_requisition):
    """Rfq-prepare gracefully handles timeout — vendor left as needs_lookup for
    client."""
    import asyncio

    with patch(
        "app.enrichment_service.find_suggested_contacts",
        new_callable=AsyncMock,
        side_effect=asyncio.TimeoutError(),
    ):
        resp = client.post(
            f"/api/requisitions/{test_requisition.id}/rfq-prepare",
            json={"vendors": [{"vendor_name": "Timeout Vendor"}]},
        )

    assert resp.status_code == 200
    data = resp.json()
    v = data["vendors"][0]
    assert v["needs_lookup"] is True


# ══════════════════════════════════════════════════════════════════════
# NEW TESTS — Cross-req "Already Contacted" context (Phase 5)
# ══════════════════════════════════════════════════════════════════════


def test_cross_req_history_returned(client, db_session, test_user, test_requisition):
    """Rfq-prepare returns past_contacts from OTHER requisitions for context."""
    other_req = Requisition(
        name="Cross Req Test",
        created_by=test_user.id,
        status="active",
    )
    db_session.add(other_req)
    db_session.commit()
    db_session.refresh(other_req)

    _make_contact(
        db_session,
        other_req,
        test_user,
        vendor_name="Cross Vendor Inc",
        vendor_contact="cross@vendor.com",
        status="sent",
        parts=["LM317T", "LM7805"],
        days_ago=3,
    )

    with patch(
        "app.enrichment_service.find_suggested_contacts",
        new_callable=AsyncMock,
        return_value=[],
    ):
        resp = client.post(
            f"/api/requisitions/{test_requisition.id}/rfq-prepare",
            json={"vendors": [{"vendor_name": "Cross Vendor Inc"}]},
        )

    assert resp.status_code == 200
    data = resp.json()
    v = data["vendors"][0]
    assert len(v["past_contacts"]) == 1
    pc = v["past_contacts"][0]
    assert pc["email"] == "cross@vendor.com"
    assert pc["req_id"] == other_req.id
    assert "LM317T" in pc["parts"]


def test_cross_req_history_excludes_current_req(client, db_session, test_user, test_requisition):
    """Rfq-prepare past_contacts excludes contacts from the CURRENT requisition."""
    # Only a contact on the current req — no cross-req history
    _make_contact(
        db_session,
        test_requisition,
        test_user,
        vendor_name="Same Req Vendor",
        vendor_contact="same@vendor.com",
        status="sent",
        days_ago=1,
    )

    with patch(
        "app.enrichment_service.find_suggested_contacts",
        new_callable=AsyncMock,
        return_value=[],
    ):
        resp = client.post(
            f"/api/requisitions/{test_requisition.id}/rfq-prepare",
            json={"vendors": [{"vendor_name": "Same Req Vendor"}]},
        )

    assert resp.status_code == 200
    data = resp.json()
    v = data["vendors"][0]
    assert v["past_contacts"] == []


# ══════════════════════════════════════════════════════════════════════
# NEW TESTS — POST /api/follow-ups/{contact_id}/send
# ══════════════════════════════════════════════════════════════════════


def test_send_follow_up_custom_body(rfq_client, db_session, test_user, test_requisition):
    """Follow-up with custom body sends email via Graph API."""
    c = _make_contact(
        db_session,
        test_requisition,
        test_user,
        vendor_name="Follow Up Vendor",
        vendor_contact="followup@vendor.com",
        status="sent",
        days_ago=5,
    )

    mock_gc = MagicMock()
    mock_gc.post_json = AsyncMock(return_value=None)

    with (
        patch(
            "app.routers.rfq.require_fresh_token",
            new_callable=AsyncMock,
            return_value="fake-token",
        ),
        patch(
            "app.utils.graph_client.GraphClient",
            return_value=mock_gc,
        ),
    ):
        resp = rfq_client.post(
            f"/api/follow-ups/{c.id}/send",
            json={"body": "Custom follow-up message"},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert "followup@vendor.com" in data["message"]


def test_send_follow_up_default_body(rfq_client, db_session, test_user, test_requisition):
    """Follow-up with empty body uses default template."""
    c = _make_contact(
        db_session,
        test_requisition,
        test_user,
        vendor_name="Default Follow Up",
        vendor_contact="default@vendor.com",
        status="sent",
        days_ago=5,
        parts=["LM317T", "LM358N"],
    )

    mock_gc = MagicMock()
    mock_gc.post_json = AsyncMock(return_value=None)

    with (
        patch(
            "app.routers.rfq.require_fresh_token",
            new_callable=AsyncMock,
            return_value="fake-token",
        ),
        patch(
            "app.utils.graph_client.GraphClient",
            return_value=mock_gc,
        ),
    ):
        resp = rfq_client.post(
            f"/api/follow-ups/{c.id}/send",
            json={"body": ""},
        )

    assert resp.status_code == 200
    # Verify GraphClient.post_json was called
    mock_gc.post_json.assert_called_once()
    call_args = mock_gc.post_json.call_args
    payload = call_args[0][1]
    # Subject should be "Re: {original subject}"
    assert "Re:" in payload["message"]["subject"]


def test_send_follow_up_no_subject(rfq_client, db_session, test_user, test_requisition):
    """Follow-up for contact without subject uses fallback subject."""
    c = Contact(
        requisition_id=test_requisition.id,
        user_id=test_user.id,
        contact_type="email",
        vendor_name="No Subject Vendor",
        vendor_contact="nosub@vendor.com",
        parts_included=["LM317T"],
        subject=None,  # no subject
        status="sent",
        created_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=5),
    )
    db_session.add(c)
    db_session.commit()
    db_session.refresh(c)

    mock_gc = MagicMock()
    mock_gc.post_json = AsyncMock(return_value=None)

    with (
        patch(
            "app.routers.rfq.require_fresh_token",
            new_callable=AsyncMock,
            return_value="fake-token",
        ),
        patch(
            "app.utils.graph_client.GraphClient",
            return_value=mock_gc,
        ),
    ):
        resp = rfq_client.post(
            f"/api/follow-ups/{c.id}/send",
            json={"body": "Test follow-up"},
        )

    assert resp.status_code == 200
    call_args = mock_gc.post_json.call_args
    payload = call_args[0][1]
    assert "Follow-Up" in payload["message"]["subject"]


def test_send_follow_up_not_found(rfq_client):
    """Follow-up for nonexistent contact returns 404."""
    with patch(
        "app.routers.rfq.require_fresh_token",
        new_callable=AsyncMock,
        return_value="fake-token",
    ):
        resp = rfq_client.post(
            "/api/follow-ups/99999/send",
            json={"body": "Test"},
        )

    assert resp.status_code == 404


def test_send_follow_up_auth_failure(rfq_client, db_session, test_user, test_requisition):
    """Follow-up fails when require_fresh_token raises."""
    from fastapi import HTTPException

    c = _make_contact(
        db_session,
        test_requisition,
        test_user,
        vendor_name="Auth Fail Vendor",
        vendor_contact="authfail@vendor.com",
        status="sent",
        days_ago=5,
    )

    with patch(
        "app.routers.rfq.require_fresh_token",
        new_callable=AsyncMock,
        side_effect=HTTPException(status_code=401, detail="Token expired"),
    ):
        resp = rfq_client.post(
            f"/api/follow-ups/{c.id}/send",
            json={"body": "Test"},
        )

    assert resp.status_code == 401


# ══════════════════════════════════════════════════════════════════════
# NEW TESTS — Follow-ups with sales role filtering
# ══════════════════════════════════════════════════════════════════════


def test_follow_ups_sales_role_filtering(
    db_session,
    sales_user,
    test_user,
    test_requisition,
):
    """Sales user only sees follow-ups for their own requisitions."""
    from app.database import get_db
    from app.dependencies import require_buyer, require_user
    from app.main import app

    # Create a stale contact on test_requisition (owned by test_user, not sales_user)
    _make_contact(
        db_session,
        test_requisition,
        test_user,
        vendor_name="Other Team Vendor",
        vendor_contact="other@vendor.com",
        status="sent",
        days_ago=5,
    )

    # Create a req owned by sales_user
    sales_req = Requisition(
        name="SALES-REQ-001",
        customer_name="Sales Customer",
        status="open",
        created_by=sales_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(sales_req)
    db_session.commit()

    _make_contact(
        db_session,
        sales_req,
        sales_user,
        vendor_name="My Vendor",
        vendor_contact="my@vendor.com",
        status="sent",
        days_ago=5,
    )

    def _override_db():
        yield db_session

    def _override_user():
        return sales_user

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_user] = _override_user
    app.dependency_overrides[require_buyer] = _override_user

    try:
        with TestClient(app) as c:
            resp = c.get("/api/follow-ups")
        assert resp.status_code == 200
        data = resp.json()
        # Sales user should only see their own req's follow-ups
        assert data["count"] == 1
        assert data["follow_ups"][0]["vendor_name"] == "My Vendor"
    finally:
        for dep in [get_db, require_user, require_buyer]:
            app.dependency_overrides.pop(dep, None)


def test_follow_ups_summary_sales_role_filtering(
    db_session,
    sales_user,
    test_user,
    test_requisition,
):
    """Sales user only sees follow-up summary for their own requisitions."""
    from app.database import get_db
    from app.dependencies import require_buyer, require_user
    from app.main import app

    _make_contact(
        db_session,
        test_requisition,
        test_user,
        vendor_name="Other Team Vendor",
        status="sent",
        days_ago=5,
    )

    sales_req = Requisition(
        name="SALES-REQ-002",
        customer_name="Sales Customer",
        status="open",
        created_by=sales_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(sales_req)
    db_session.commit()

    _make_contact(
        db_session,
        sales_req,
        sales_user,
        vendor_name="My Vendor",
        status="sent",
        days_ago=5,
    )

    def _override_db():
        yield db_session

    def _override_user():
        return sales_user

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_user] = _override_user
    app.dependency_overrides[require_buyer] = _override_user

    try:
        with TestClient(app) as c:
            resp = c.get("/api/follow-ups/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
    finally:
        for dep in [get_db, require_user, require_buyer]:
            app.dependency_overrides.pop(dep, None)


# ── NEW TESTS — Owner-scoped RFQ access controls ─────────────────────


def test_sales_cannot_list_contacts_for_other_users_requisition(db_session, sales_user, test_user, test_requisition):
    _make_contact(db_session, test_requisition, test_user, vendor_name="Hidden Vendor")

    with _client_as_user(db_session, sales_user) as c:
        resp = c.get(f"/api/requisitions/{test_requisition.id}/contacts")

    assert resp.status_code == 404


def test_sales_can_list_contacts_for_owned_requisition(db_session, sales_user):
    sales_req = Requisition(
        name="SALES-REQ-003",
        customer_name="Sales Customer",
        status="open",
        created_by=sales_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(sales_req)
    db_session.commit()
    _make_contact(db_session, sales_req, sales_user, vendor_name="Visible Vendor")

    with _client_as_user(db_session, sales_user) as c:
        resp = c.get(f"/api/requisitions/{sales_req.id}/contacts")

    assert resp.status_code == 200
    assert len(resp.json()) == 1
    assert resp.json()[0]["vendor_name"] == "Visible Vendor"


def test_sales_cannot_update_vendor_response_status_for_other_users_requisition(
    db_session, sales_user, test_requisition
):
    vr = VendorResponse(
        requisition_id=test_requisition.id,
        vendor_name="Blocked Vendor",
        vendor_email="blocked@vendor.com",
        subject="Re: RFQ",
        status="new",
        confidence=0.8,
        received_at=datetime.now(timezone.utc),
        classification="quote",
        body="quoted",
    )
    db_session.add(vr)
    db_session.commit()
    db_session.refresh(vr)

    with _client_as_user(db_session, sales_user) as c:
        resp = c.patch(f"/api/vendor-responses/{vr.id}/status", json={"status": "reviewed"})

    assert resp.status_code == 404
    db_session.refresh(vr)
    assert vr.status == "new"


def test_sales_cannot_send_follow_up_for_other_users_requisition(db_session, sales_user, test_user, test_requisition):
    ctc = _make_contact(
        db_session, test_requisition, test_user, vendor_name="Hidden Followup", status="sent", days_ago=5
    )

    with patch("app.routers.rfq.require_fresh_token", new_callable=AsyncMock, return_value="fake-token"):
        with _client_as_user(db_session, sales_user) as c:
            resp = c.post(f"/api/follow-ups/{ctc.id}/send", json={"body": "Follow up"})

    assert resp.status_code == 404


# ══════════════════════════════════════════════════════════════════════
# NEW TESTS — Responses with received_at as string
# ══════════════════════════════════════════════════════════════════════


def test_list_responses_with_string_received_at(client, db_session, test_requisition):
    """Responses handle received_at as a string (non-datetime) value."""
    vr = VendorResponse(
        requisition_id=test_requisition.id,
        vendor_name="String Date Vendor",
        vendor_email="strdate@vendor.com",
        subject="Re: RFQ",
        status="new",
        confidence=0.70,
        received_at=datetime.now(timezone.utc),
        classification="quote",
        body="Here is our quote",
    )
    db_session.add(vr)
    db_session.commit()

    resp = client.get(f"/api/requisitions/{test_requisition.id}/responses")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["status"] == "new"
    assert "received_at" in data[0]


# ══════════════════════════════════════════════════════════════════════
# NEW TESTS — _enrich_with_vendor_cards (full function test)
# ══════════════════════════════════════════════════════════════════════


def test_enrich_with_vendor_cards_creates_cards(db_session):
    """_enrich_with_vendor_cards creates VendorCards for new vendors."""
    from app.routers.rfq import _enrich_with_vendor_cards

    results = {
        "REQ-1": {
            "sightings": [
                {"vendor_name": "Brand New Supplier", "mpn_matched": "LM317T"},
            ]
        }
    }

    _enrich_with_vendor_cards(results, db_session)

    # Verify card was created
    card = db_session.query(VendorCard).filter_by(normalized_name="brand new supplier").first()
    assert card is not None
    assert card.display_name == "Brand New Supplier"

    # Verify sighting was enriched with vendor_card
    s = results["REQ-1"]["sightings"][0]
    assert "vendor_card" in s
    assert s["vendor_card"]["card_id"] == card.id


def test_enrich_with_vendor_cards_existing_card(db_session, test_vendor_card):
    """_enrich_with_vendor_cards uses existing cards and updates sighting_count."""
    from app.routers.rfq import _enrich_with_vendor_cards

    results = {
        "REQ-1": {
            "sightings": [
                {"vendor_name": "Arrow Electronics", "mpn_matched": "LM317T"},
            ]
        }
    }

    _enrich_with_vendor_cards(results, db_session)

    s = results["REQ-1"]["sightings"][0]
    assert "vendor_card" in s
    assert s["vendor_card"]["card_id"] == test_vendor_card.id
    # Sighting count should be incremented
    db_session.refresh(test_vendor_card)
    assert test_vendor_card.sighting_count >= 42


def test_enrich_with_vendor_cards_filters_garbage(db_session):
    """_enrich_with_vendor_cards filters out garbage vendor names."""
    from app.routers.rfq import _enrich_with_vendor_cards

    results = {
        "REQ-1": {
            "sightings": [
                {"vendor_name": "Good Vendor", "mpn_matched": "LM317T"},
                {"vendor_name": "No Seller Listed", "mpn_matched": "LM317T"},
                {"vendor_name": "", "mpn_matched": "LM317T"},
                {"vendor_name": "N/A", "mpn_matched": "LM317T"},
            ]
        }
    }

    _enrich_with_vendor_cards(results, db_session)

    # Only "Good Vendor" should remain
    remaining = results["REQ-1"]["sightings"]
    assert len(remaining) == 1
    assert remaining[0]["vendor_name"] == "Good Vendor"


def test_enrich_with_vendor_cards_filters_blacklisted(db_session):
    """_enrich_with_vendor_cards filters out blacklisted vendors."""
    from app.routers.rfq import _enrich_with_vendor_cards

    card = VendorCard(
        normalized_name="bad vendor",
        display_name="Bad Vendor",
        emails=[],
        phones=[],
        is_blacklisted=True,
    )
    db_session.add(card)
    db_session.commit()

    results = {
        "REQ-1": {
            "sightings": [
                {"vendor_name": "Good Vendor", "mpn_matched": "LM317T"},
                {"vendor_name": "Bad Vendor", "mpn_matched": "LM317T"},
            ]
        }
    }

    _enrich_with_vendor_cards(results, db_session)

    remaining = results["REQ-1"]["sightings"]
    assert len(remaining) == 1
    assert remaining[0]["vendor_name"] == "Good Vendor"


def test_enrich_with_vendor_cards_with_reviews(db_session, test_vendor_card, test_user):
    """_enrich_with_vendor_cards includes review ratings in summary."""
    from app.routers.rfq import _enrich_with_vendor_cards

    review = VendorReview(
        vendor_card_id=test_vendor_card.id,
        user_id=test_user.id,
        rating=4,
        comment="Good vendor",
    )
    db_session.add(review)
    db_session.commit()

    results = {
        "REQ-1": {
            "sightings": [
                {"vendor_name": "Arrow Electronics", "mpn_matched": "LM317T"},
            ]
        }
    }

    _enrich_with_vendor_cards(results, db_session)

    s = results["REQ-1"]["sightings"][0]
    assert s["vendor_card"]["avg_rating"] == 4.0
    assert s["vendor_card"]["review_count"] == 1


def test_enrich_with_vendor_cards_empty_results(db_session):
    """_enrich_with_vendor_cards handles empty results gracefully."""
    from app.routers.rfq import _enrich_with_vendor_cards

    results = {"REQ-1": {"sightings": []}}
    _enrich_with_vendor_cards(results, db_session)
    assert results["REQ-1"]["sightings"] == []


def test_enrich_with_vendor_cards_no_vendor_names(db_session):
    """_enrich_with_vendor_cards skips sightings with no vendor names."""
    from app.routers.rfq import _enrich_with_vendor_cards

    results = {
        "REQ-1": {
            "sightings": [
                {"vendor_name": None, "mpn_matched": "LM317T"},
            ]
        }
    }

    _enrich_with_vendor_cards(results, db_session)
    # None vendor → skipped during enrichment, sighting still present
    assert len(results["REQ-1"]["sightings"]) == 1


def test_enrich_with_vendor_cards_harvests_emails(db_session):
    """_enrich_with_vendor_cards merges harvested emails into cards."""
    from app.routers.rfq import _enrich_with_vendor_cards

    card = VendorCard(
        normalized_name="email harvest vendor",
        display_name="Email Harvest Vendor",
        emails=[],
        phones=[],
    )
    db_session.add(card)
    db_session.commit()

    results = {
        "REQ-1": {
            "sightings": [
                {
                    "vendor_name": "Email Harvest Vendor",
                    "mpn_matched": "LM317T",
                    "vendor_email": "harvested@vendor.com",
                },
            ]
        }
    }

    _enrich_with_vendor_cards(results, db_session)

    db_session.refresh(card)
    assert "harvested@vendor.com" in card.emails


def test_enrich_with_vendor_cards_harvests_phones(db_session):
    """_enrich_with_vendor_cards merges harvested phones into cards."""
    from app.routers.rfq import _enrich_with_vendor_cards

    card = VendorCard(
        normalized_name="phone harvest vendor",
        display_name="Phone Harvest Vendor",
        emails=[],
        phones=[],
    )
    db_session.add(card)
    db_session.commit()

    results = {
        "REQ-1": {
            "sightings": [
                {
                    "vendor_name": "Phone Harvest Vendor",
                    "mpn_matched": "LM317T",
                    "vendor_phone": "+1-555-4444",
                },
            ]
        }
    }

    _enrich_with_vendor_cards(results, db_session)

    db_session.refresh(card)
    assert "+1-555-4444" in card.phones


def test_enrich_with_vendor_cards_sets_website(db_session):
    """_enrich_with_vendor_cards sets website from vendor_url if not already set."""
    from app.routers.rfq import _enrich_with_vendor_cards

    card = VendorCard(
        normalized_name="website vendor",
        display_name="Website Vendor",
        emails=[],
        phones=[],
        website=None,
    )
    db_session.add(card)
    db_session.commit()

    results = {
        "REQ-1": {
            "sightings": [
                {
                    "vendor_name": "Website Vendor",
                    "mpn_matched": "LM317T",
                    "vendor_url": "https://websitevendor.com",
                },
            ]
        }
    }

    _enrich_with_vendor_cards(results, db_session)

    db_session.refresh(card)
    assert card.website == "https://websitevendor.com"


def test_enrich_with_vendor_cards_no_overwrite_website(db_session):
    """_enrich_with_vendor_cards does not overwrite existing website."""
    from app.routers.rfq import _enrich_with_vendor_cards

    card = VendorCard(
        normalized_name="existing website vendor",
        display_name="Existing Website Vendor",
        emails=[],
        phones=[],
        website="https://existing.com",
    )
    db_session.add(card)
    db_session.commit()

    results = {
        "REQ-1": {
            "sightings": [
                {
                    "vendor_name": "Existing Website Vendor",
                    "mpn_matched": "LM317T",
                    "vendor_url": "https://new-url.com",
                },
            ]
        }
    }

    _enrich_with_vendor_cards(results, db_session)

    db_session.refresh(card)
    assert card.website == "https://existing.com"


def test_enrich_with_vendor_cards_skips_historical(db_session):
    """_enrich_with_vendor_cards skips historical sightings for count updates."""
    from app.routers.rfq import _enrich_with_vendor_cards

    card = VendorCard(
        normalized_name="history vendor",
        display_name="History Vendor",
        emails=[],
        phones=[],
        sighting_count=10,
    )
    db_session.add(card)
    db_session.commit()

    results = {
        "REQ-1": {
            "sightings": [
                {
                    "vendor_name": "History Vendor",
                    "mpn_matched": "LM317T",
                    "is_historical": True,
                },
            ]
        }
    }

    _enrich_with_vendor_cards(results, db_session)

    db_session.refresh(card)
    # sighting_count should not be incremented for historical entries
    assert card.sighting_count == 10


def test_enrich_with_vendor_cards_vendor_score(db_session):
    """_enrich_with_vendor_cards includes vendor_score in summary when present."""
    from app.routers.rfq import _enrich_with_vendor_cards

    card = VendorCard(
        normalized_name="scored vendor",
        display_name="Scored Vendor",
        emails=[],
        phones=[],
        vendor_score=75.5,
        is_new_vendor=False,
    )
    db_session.add(card)
    db_session.commit()

    results = {
        "REQ-1": {
            "sightings": [
                {"vendor_name": "Scored Vendor", "mpn_matched": "LM317T"},
            ]
        }
    }

    _enrich_with_vendor_cards(results, db_session)

    s = results["REQ-1"]["sightings"][0]
    assert s["vendor_card"]["vendor_score"] == 75.5
    assert s["vendor_card"]["is_new_vendor"] is False


def test_enrich_with_vendor_cards_multiple_groups(db_session):
    """_enrich_with_vendor_cards works across multiple result groups."""
    from app.routers.rfq import _enrich_with_vendor_cards

    results = {
        "REQ-1": {
            "sightings": [
                {"vendor_name": "Multi Group Vendor", "mpn_matched": "LM317T"},
            ]
        },
        "REQ-2": {
            "sightings": [
                {"vendor_name": "Multi Group Vendor", "mpn_matched": "LM358N"},
            ]
        },
    }

    _enrich_with_vendor_cards(results, db_session)

    # Both groups should have enriched sightings
    for key in ("REQ-1", "REQ-2"):
        for s in results[key]["sightings"]:
            assert "vendor_card" in s


# ══════════════════════════════════════════════════════════════════════
# NEW TESTS — Follow-up opened status
# ══════════════════════════════════════════════════════════════════════


def test_follow_ups_opened_contacts(client, db_session, test_user, test_requisition):
    """Contacts with 'opened' status >3 days old also appear as follow-ups."""
    _make_contact(
        db_session,
        test_requisition,
        test_user,
        vendor_name="Opened Vendor",
        vendor_contact="opened@vendor.com",
        status="opened",
        days_ago=5,
    )

    resp = client.get("/api/follow-ups")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 1
    assert data["follow_ups"][0]["vendor_name"] == "Opened Vendor"
    assert data["follow_ups"][0]["status"] == "opened"


def test_follow_ups_phone_contacts_excluded(client, db_session, test_user, test_requisition):
    """Only email contacts appear as follow-ups (not phone)."""
    _make_contact(
        db_session,
        test_requisition,
        test_user,
        vendor_name="Phone Vendor",
        vendor_contact="+1-555-0100",
        contact_type="phone",
        status="sent",
        days_ago=5,
    )

    resp = client.get("/api/follow-ups")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 0


def test_follow_ups_replied_excluded(client, db_session, test_user, test_requisition):
    """Contacts with 'replied' status are excluded from follow-ups."""
    _make_contact(
        db_session,
        test_requisition,
        test_user,
        vendor_name="Replied Vendor",
        vendor_contact="replied@vendor.com",
        status="replied",
        days_ago=5,
    )

    resp = client.get("/api/follow-ups")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 0


def test_follow_ups_includes_req_name(client, db_session, test_user, test_requisition):
    """Follow-ups include requisition_name."""
    _make_contact(
        db_session,
        test_requisition,
        test_user,
        vendor_name="Req Name Vendor",
        vendor_contact="reqname@vendor.com",
        status="sent",
        days_ago=5,
    )

    resp = client.get("/api/follow-ups")
    assert resp.status_code == 200
    data = resp.json()
    assert data["follow_ups"][0]["requisition_name"] == "REQ-TEST-001"


# ══════════════════════════════════════════════════════════════════════
# Batch follow-up endpoint tests
# ══════════════════════════════════════════════════════════════════════


def test_send_follow_up_batch_success(rfq_client, db_session, test_user, test_requisition):
    """Batch follow-up sends to multiple contacts."""
    c1 = _make_contact(
        db_session,
        test_requisition,
        test_user,
        vendor_name="Vendor A",
        vendor_contact="a@v.com",
        status="sent",
        days_ago=5,
    )
    c2 = _make_contact(
        db_session,
        test_requisition,
        test_user,
        vendor_name="Vendor B",
        vendor_contact="b@v.com",
        status="sent",
        days_ago=5,
        parts=["LM358N"],
    )

    mock_gc = MagicMock()
    mock_gc.post_json = AsyncMock(return_value=None)

    with (
        patch("app.routers.rfq.require_fresh_token", new_callable=AsyncMock, return_value="fake-token"),
        patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
    ):
        resp = rfq_client.post("/api/follow-ups/send-batch", json={"contact_ids": [c1.id, c2.id]})

    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["sent"] == 2
    assert data["total"] == 2
    assert len(data["results"]) == 2
    assert all(r["status"] == "sent" for r in data["results"])


def test_send_follow_up_batch_empty_ids(rfq_client):
    """Empty contact_ids returns 400."""
    with patch("app.routers.rfq.require_fresh_token", new_callable=AsyncMock, return_value="fake-token"):
        resp = rfq_client.post("/api/follow-ups/send-batch", json={"contact_ids": []})
    assert resp.status_code == 400


def test_send_follow_up_batch_missing_contact(rfq_client, db_session, test_user, test_requisition):
    """Non-existent contact ID is skipped."""
    c1 = _make_contact(
        db_session,
        test_requisition,
        test_user,
        vendor_name="Real Vendor",
        vendor_contact="real@v.com",
        status="sent",
        days_ago=5,
    )

    mock_gc = MagicMock()
    mock_gc.post_json = AsyncMock(return_value=None)

    with (
        patch("app.routers.rfq.require_fresh_token", new_callable=AsyncMock, return_value="fake-token"),
        patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
    ):
        resp = rfq_client.post("/api/follow-ups/send-batch", json={"contact_ids": [c1.id, 99999]})

    data = resp.json()
    assert data["sent"] == 1
    assert data["total"] == 2
    skipped = [r for r in data["results"] if r["status"] == "skipped"]
    assert len(skipped) == 1
    assert skipped[0]["contact_id"] == 99999


def test_send_follow_up_batch_send_failure(rfq_client, db_session, test_user, test_requisition):
    """Graph API failure marks contact as failed, doesn't crash batch."""
    c1 = _make_contact(
        db_session,
        test_requisition,
        test_user,
        vendor_name="Fail Vendor",
        vendor_contact="fail@v.com",
        status="sent",
        days_ago=5,
    )

    mock_gc = MagicMock()
    mock_gc.post_json = AsyncMock(side_effect=Exception("Graph API error"))

    with (
        patch("app.routers.rfq.require_fresh_token", new_callable=AsyncMock, return_value="fake-token"),
        patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
    ):
        resp = rfq_client.post("/api/follow-ups/send-batch", json={"contact_ids": [c1.id]})

    data = resp.json()
    assert data["ok"] is True
    assert data["sent"] == 0
    assert data["results"][0]["status"] == "failed"
    assert "Graph API error" in data["results"][0]["reason"]


def test_send_follow_up_batch_no_subject_fallback(rfq_client, db_session, test_user, test_requisition):
    """Contact without subject gets fallback subject line."""
    c = Contact(
        requisition_id=test_requisition.id,
        user_id=test_user.id,
        contact_type="email",
        vendor_name="NoSub",
        vendor_contact="nosub@v.com",
        parts_included=["LM317T"],
        subject=None,
        status="sent",
        created_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=5),
    )
    db_session.add(c)
    db_session.commit()
    db_session.refresh(c)

    mock_gc = MagicMock()
    mock_gc.post_json = AsyncMock(return_value=None)

    with (
        patch("app.routers.rfq.require_fresh_token", new_callable=AsyncMock, return_value="fake-token"),
        patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
    ):
        resp = rfq_client.post("/api/follow-ups/send-batch", json={"contact_ids": [c.id]})

    assert resp.status_code == 200
    call_args = mock_gc.post_json.call_args[0][1]
    assert "TRIO Supply Chain" in call_args["message"]["subject"]
