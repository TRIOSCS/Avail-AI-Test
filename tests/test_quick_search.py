"""Tests for the quick-search endpoint (POST /api/quick-search).

Covers: validation, mock API search, material card history lookup,
empty results, and error handling.

Called by: pytest
Depends on: app/routers/materials.py, app/search_service.py
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models import MaterialCard, MaterialVendorHistory, VendorCard


# ── Validation tests ──────────────────────────────────────────────────────


def test_quick_search_missing_mpn(client):
    """Should reject request with missing MPN."""
    resp = client.post("/api/quick-search", json={})
    assert resp.status_code == 400
    assert "MPN is required" in resp.json()["error"]


def test_quick_search_short_mpn(client):
    """Should reject MPN shorter than 2 characters."""
    resp = client.post("/api/quick-search", json={"mpn": "A"})
    assert resp.status_code == 400
    assert "at least 2 characters" in resp.json()["error"]


# ── Successful search (mocked connectors) ────────────────────────────────


@patch("app.search_service._fetch_fresh", new_callable=AsyncMock)
def test_quick_search_returns_results(mock_fetch, client, db_session: Session):
    """Should return scored sightings from supplier APIs."""
    mock_fetch.return_value = (
        [
            {
                "vendor_name": "Acme Parts",
                "mpn_matched": "LM358DR",
                "manufacturer": "Texas Instruments",
                "qty_available": 5000,
                "unit_price": 0.45,
                "currency": "USD",
                "source_type": "broker",
                "is_authorized": False,
                "confidence": 3,
            },
            {
                "vendor_name": "DigiKey",
                "mpn_matched": "LM358DR",
                "manufacturer": "Texas Instruments",
                "qty_available": 12000,
                "unit_price": 0.52,
                "currency": "USD",
                "source_type": "digikey",
                "is_authorized": True,
                "confidence": 5,
            },
        ],
        [
            {"source": "brokerbin", "results": 1, "ms": 200, "error": None, "status": "ok"},
            {"source": "digikey", "results": 1, "ms": 150, "error": None, "status": "ok"},
        ],
    )

    resp = client.post("/api/quick-search", json={"mpn": "LM358DR"})
    assert resp.status_code == 200
    data = resp.json()

    assert "sightings" in data
    assert "source_stats" in data
    assert "material_card" in data
    assert len(data["sightings"]) == 2
    assert data["sightings"][0]["mpn_matched"] == "LM358DR"
    # Results should be sorted by score descending
    scores = [s["score"] for s in data["sightings"]]
    assert scores == sorted(scores, reverse=True)

    mock_fetch.assert_called_once()
    call_args = mock_fetch.call_args
    assert "LM358DR" in call_args[0][0]


@patch("app.search_service._fetch_fresh", new_callable=AsyncMock)
def test_quick_search_empty_results(mock_fetch, client):
    """Should return empty sightings when no results found."""
    mock_fetch.return_value = ([], [
        {"source": "nexar", "results": 0, "ms": 300, "error": None, "status": "ok"},
    ])

    resp = client.post("/api/quick-search", json={"mpn": "NONEXISTENT123"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["sightings"] == []
    assert data["material_card"] is None


@patch("app.search_service._fetch_fresh", new_callable=AsyncMock)
def test_quick_search_includes_material_card(mock_fetch, client, db_session: Session):
    """Should include material card summary when card exists."""
    card = MaterialCard(
        normalized_mpn="lm358dr",
        display_mpn="LM358DR",
        manufacturer="Texas Instruments",
        description="Dual Op-Amp",
        lifecycle_status="Active",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(card)
    db_session.commit()
    db_session.refresh(card)

    mock_fetch.return_value = ([], [])

    resp = client.post("/api/quick-search", json={"mpn": "LM358DR"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["material_card"] is not None
    assert data["material_card"]["mpn"] == "LM358DR"
    assert data["material_card"]["manufacturer"] == "Texas Instruments"
    assert data["material_card"]["lifecycle_status"] == "Active"


@patch("app.search_service._fetch_fresh", new_callable=AsyncMock)
def test_quick_search_includes_vendor_history(mock_fetch, client, db_session: Session):
    """Should include material vendor history when no fresh results overlap."""
    card = MaterialCard(
        normalized_mpn="lm358dr",
        display_mpn="LM358DR",
        manufacturer="Texas Instruments",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(card)
    db_session.flush()

    vh = MaterialVendorHistory(
        material_card_id=card.id,
        vendor_name="Old Vendor Co",
        source_type="email_mining",
        is_authorized=False,
        first_seen=datetime(2025, 6, 1, tzinfo=timezone.utc),
        last_seen=datetime(2026, 1, 15, tzinfo=timezone.utc),
        times_seen=4,
        last_qty=2000,
        last_price=0.38,
        last_currency="USD",
    )
    db_session.add(vh)
    db_session.commit()

    mock_fetch.return_value = ([], [])

    resp = client.post("/api/quick-search", json={"mpn": "LM358DR"})
    assert resp.status_code == 200
    data = resp.json()
    # Should have at least one result from material history
    hist = [s for s in data["sightings"] if s.get("is_material_history")]
    assert len(hist) >= 1
    assert hist[0]["vendor_name"] == "Old Vendor Co"
