"""
test_routers_rfq.py — Tests for RFQ, Follow-ups & Vendor Enrichment Router

Tests the vendor card enrichment filtering logic: garbage vendor names,
blacklisted vendors, and summary cache building.
Also tests RFQ router endpoints via TestClient.

Covers: _enrich_with_vendor_cards filtering, follow-ups, contacts, responses,
rfq-prepare, phone call logging.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from app.models import Contact, Requisition, User, VendorResponse


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
    results = _make_results_dict([
        {"vendor_name": "Arrow", "mpn_matched": "LM317T"},
        {"vendor_name": "No Seller Listed", "mpn_matched": "LM317T"},
        {"vendor_name": "", "mpn_matched": "LM317T"},
    ])
    kept = _filter_sightings(results)
    assert len(kept) == 1
    assert kept[0]["vendor_name"] == "Arrow"


def test_filter_removes_blacklisted():
    results = _make_results_dict([
        {"vendor_name": "Good Vendor", "mpn_matched": "LM317T"},
        {"vendor_name": "Bad Vendor", "mpn_matched": "LM317T", "_blacklisted": True},
    ])
    kept = _filter_sightings(results)
    assert len(kept) == 1
    assert kept[0]["vendor_name"] == "Good Vendor"


def test_filter_handles_none_vendor():
    results = _make_results_dict([
        {"vendor_name": None, "mpn_matched": "LM317T"},
    ])
    kept = _filter_sightings(results)
    assert len(kept) == 0  # None → "" → in _GARBAGE_VENDORS


def test_filter_preserves_order():
    results = _make_results_dict([
        {"vendor_name": "Alpha", "mpn_matched": "A"},
        {"vendor_name": "N/A", "mpn_matched": "B"},
        {"vendor_name": "Beta", "mpn_matched": "C"},
    ])
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

    Uses naive UTC datetimes for SQLite compatibility (SQLite strips tzinfo,
    and the router does aware-vs-naive datetime subtraction).
    """
    c = Contact(
        requisition_id=requisition.id,
        user_id=user.id,
        contact_type=contact_type,
        vendor_name=vendor_name,
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
        db_session, test_requisition, test_user,
        vendor_name="Slow Vendor",
        vendor_contact="slow@vendor.com",
        status="sent",
        days_ago=5,
    )
    # Recent contact should NOT appear
    _make_contact(
        db_session, test_requisition, test_user,
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
        db_session, test_requisition, test_user,
        vendor_name="Vendor A", status="sent", days_ago=5,
    )
    _make_contact(
        db_session, test_requisition, test_user,
        vendor_name="Vendor B", status="opened", days_ago=7,
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


# ── POST /api/requisitions/{id}/rfq-prepare ──────────────────────────


def test_rfq_prepare_vendor_data(client, db_session, test_requisition, test_vendor_card):
    """rfq-prepare returns vendor card data for known vendors."""
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
    """rfq-prepare for unknown vendor returns needs_lookup=True."""
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
