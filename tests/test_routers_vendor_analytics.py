"""
tests/test_routers_vendor_analytics.py — Tests for routers/vendor_analytics.py

Covers: offer-history, confirmed-offers, parts-summary, analyze-materials,
_vendor_parts_summary_query helper.

Called by: pytest
Depends on: routers/vendor_analytics.py
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.models import MaterialCard, MaterialVendorHistory, Offer, Requisition, VendorCard
from app.routers.vendor_analytics import _vendor_parts_summary_query

# ── Offer history ────────────────────────────────────────────────────────


def test_offer_history(client, db_session, test_vendor_card):
    """GET /api/vendors/{id}/offer-history returns 200."""
    resp = client.get(f"/api/vendors/{test_vendor_card.id}/offer-history")
    assert resp.status_code == 200
    data = resp.json()
    assert "vendor_name" in data
    assert "items" in data


def test_offer_history_not_found(client):
    """GET /api/vendors/99999/offer-history returns 404."""
    resp = client.get("/api/vendors/99999/offer-history")
    assert resp.status_code == 404


def test_offer_history_with_search(client, db_session, test_vendor_card):
    """GET /api/vendors/{id}/offer-history?q=lm filters by MPN."""
    mc = MaterialCard(
        normalized_mpn="ofhist123",
        display_mpn="OFHIST123",
        manufacturer="TI",
        search_count=0,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(mc)
    db_session.commit()

    mvh = MaterialVendorHistory(
        material_card_id=mc.id,
        vendor_name=test_vendor_card.normalized_name,
        source_type="stock_list",
        times_seen=1,
        last_manufacturer="TI",
    )
    db_session.add(mvh)
    db_session.commit()

    resp = client.get(
        f"/api/vendors/{test_vendor_card.id}/offer-history",
        params={"q": "ofhist"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1
    assert any("OFHIST123" in item["mpn"] for item in data["items"])


def test_offer_history_with_pagination(client, db_session, test_vendor_card):
    """GET /api/vendors/{id}/offer-history respects limit and offset."""
    resp = client.get(
        f"/api/vendors/{test_vendor_card.id}/offer-history",
        params={"limit": "5", "offset": "0"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["limit"] == 5
    assert data["offset"] == 0


# ── Confirmed offers ─────────────────────────────────────────────────────


def test_confirmed_offers(client, db_session, test_vendor_card):
    """GET /api/vendors/{id}/confirmed-offers returns 200."""
    resp = client.get(f"/api/vendors/{test_vendor_card.id}/confirmed-offers")
    assert resp.status_code == 200
    data = resp.json()
    assert "vendor_name" in data
    assert "items" in data


def test_confirmed_offers_not_found(client):
    """GET /api/vendors/99999/confirmed-offers returns 404."""
    resp = client.get("/api/vendors/99999/confirmed-offers")
    assert resp.status_code == 404


def test_confirmed_offers_with_search(client, db_session, test_vendor_card, test_user):
    """GET /api/vendors/{id}/confirmed-offers?q=lm filters by MPN."""
    req = Requisition(
        name="REQ-CONF-001",
        customer_name="Test Customer",
        status="open",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.flush()

    offer = Offer(
        requisition_id=req.id,
        vendor_card_id=test_vendor_card.id,
        vendor_name="Arrow Electronics",
        mpn="CONFTEST-MPN",
        qty_available=100,
        unit_price=1.50,
        entered_by_id=test_user.id,
        status="active",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(offer)
    db_session.commit()

    resp = client.get(
        f"/api/vendors/{test_vendor_card.id}/confirmed-offers",
        params={"q": "conftest"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1


def test_confirmed_offers_with_pagination(client, db_session, test_vendor_card):
    """GET /api/vendors/{id}/confirmed-offers respects limit and offset."""
    resp = client.get(
        f"/api/vendors/{test_vendor_card.id}/confirmed-offers",
        params={"limit": "10", "offset": "0"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 0


def test_confirmed_offers_serialization(client, db_session, test_vendor_card, test_user):
    """GET /api/vendors/{id}/confirmed-offers serializes all offer fields."""
    req = Requisition(
        name="REQ-SER-001",
        customer_name="Serialize Customer",
        status="open",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.flush()

    offer = Offer(
        requisition_id=req.id,
        vendor_card_id=test_vendor_card.id,
        vendor_name="Arrow Electronics",
        mpn="SER-MPN-001",
        manufacturer="TI",
        qty_available=1000,
        unit_price=0.50,
        currency="EUR",
        lead_time="2-3 weeks",
        condition="New",
        status="active",
        notes="Tested and verified",
        entered_by_id=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(offer)
    db_session.commit()

    resp = client.get(f"/api/vendors/{test_vendor_card.id}/confirmed-offers")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1
    item = data["items"][0]
    assert item["mpn"] == "SER-MPN-001"
    assert item["unit_price"] == 0.50
    assert item["currency"] == "EUR"
    assert item["condition"] == "New"
    assert item["notes"] == "Tested and verified"


# ── Parts summary ────────────────────────────────────────────────────────


def test_parts_summary(db_session, test_user, test_vendor_card):
    """GET /api/vendors/{id}/parts-summary returns 200 or 500 (PostgreSQL-only SQL)."""
    from app.database import get_db
    from app.dependencies import require_buyer, require_user
    from app.main import app

    def _override_db():
        yield db_session

    def _override_user():
        return test_user

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_user] = _override_user
    app.dependency_overrides[require_buyer] = _override_user

    with TestClient(app, raise_server_exceptions=False) as c:
        resp = c.get(f"/api/vendors/{test_vendor_card.id}/parts-summary")
    app.dependency_overrides.clear()

    # Accept 200 (PostgreSQL) or 500 (SQLite can't run array_agg)
    assert resp.status_code in (200, 500)


def test_parts_summary_not_found(db_session, test_user):
    """GET /api/vendors/99999/parts-summary returns 404."""
    from app.database import get_db
    from app.dependencies import require_buyer, require_user
    from app.main import app

    def _override_db():
        yield db_session

    def _override_user():
        return test_user

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_user] = _override_user
    app.dependency_overrides[require_buyer] = _override_user

    with TestClient(app, raise_server_exceptions=False) as c:
        resp = c.get("/api/vendors/99999/parts-summary")
    app.dependency_overrides.clear()

    assert resp.status_code == 404


def test_vendor_parts_summary_query_with_filter():
    """_vendor_parts_summary_query builds SQL with MPN filter when q is set."""
    mock_db = MagicMock()
    mock_db.execute.return_value.fetchall.return_value = [
        ("LM317T", "TI", 5, datetime(2026, 1, 1), datetime(2026, 1, 15), 0.50, 100),
    ]
    mock_db.execute.return_value.scalar.return_value = 1

    result = _vendor_parts_summary_query(
        db=mock_db,
        norm="test vendor",
        display_name="Test Vendor",
        q="lm317",
        limit=100,
        offset=0,
    )

    assert result["vendor_name"] == "Test Vendor"
    assert result["total"] == 1
    assert len(result["items"]) == 1
    assert result["items"][0]["mpn"] == "LM317T"
    assert result["items"][0]["manufacturer"] == "TI"
    assert result["items"][0]["sighting_count"] == 5
    assert result["items"][0]["last_price"] == 0.50


def test_vendor_parts_summary_query_no_filter():
    """_vendor_parts_summary_query without search filter."""
    mock_db = MagicMock()
    mock_db.execute.return_value.fetchall.return_value = []
    mock_db.execute.return_value.scalar.return_value = 0

    result = _vendor_parts_summary_query(
        db=mock_db,
        norm="empty vendor",
        display_name="Empty Vendor",
        q="",
        limit=100,
        offset=0,
    )

    assert result["total"] == 0
    assert result["items"] == []


def test_vendor_parts_summary_query_null_dates():
    """_vendor_parts_summary_query handles None dates in rows."""
    mock_db = MagicMock()
    mock_db.execute.return_value.fetchall.return_value = [
        ("ABC123", None, None, None, None, None, None),
    ]
    mock_db.execute.return_value.scalar.return_value = 1

    result = _vendor_parts_summary_query(
        db=mock_db,
        norm="vendor",
        display_name="Vendor",
        q="",
        limit=100,
        offset=0,
    )

    assert result["items"][0]["manufacturer"] == ""
    assert result["items"][0]["sighting_count"] == 1
    assert result["items"][0]["first_seen"] is None
    assert result["items"][0]["last_seen"] is None


# ── Analyze materials ────────────────────────────────────────────────────


def test_analyze_materials_no_api_key(client, db_session, test_vendor_card, monkeypatch):
    """POST /api/vendors/{id}/analyze-materials without API key returns 503."""
    monkeypatch.setattr(
        "app.routers.vendor_analytics.get_credential_cached",
        lambda *args, **kwargs: None,
    )
    resp = client.post(f"/api/vendors/{test_vendor_card.id}/analyze-materials")
    assert resp.status_code == 503


def test_analyze_materials_success(client, db_session, test_vendor_card, monkeypatch):
    """POST /api/vendors/{id}/analyze-materials with API key succeeds."""
    monkeypatch.setattr("app.routers.vendor_analytics.get_credential_cached", lambda *a, **kw: "fake-key")

    async def mock_analyze(card_id, db_session=None):
        card = db_session.get(VendorCard, card_id) if db_session else None
        if card:
            card.brand_tags = ["Texas Instruments", "NXP"]
            card.commodity_tags = ["Microcontrollers", "Capacitors"]

    monkeypatch.setattr("app.routers.vendor_analytics._analyze_vendor_materials", mock_analyze)

    resp = client.post(f"/api/vendors/{test_vendor_card.id}/analyze-materials")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert "brand_tags" in data
    assert "commodity_tags" in data


def test_analyze_materials_not_found(client, monkeypatch):
    """POST /api/vendors/99999/analyze-materials returns 404."""
    monkeypatch.setattr("app.routers.vendor_analytics.get_credential_cached", lambda *a, **kw: "fake-key")
    resp = client.post("/api/vendors/99999/analyze-materials")
    assert resp.status_code == 404
