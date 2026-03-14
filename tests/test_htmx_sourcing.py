"""
test_htmx_sourcing.py — Tests for Phase 3 Task 5: Sourcing results with source filters and confidence badges.
Verifies sourcing results partial, filter/sort, material card detail, sighting rows, and SSE stream endpoint.
Called by: pytest
Depends on: app/routers/views.py, app/templates/partials/sourcing/
"""

import os

os.environ.setdefault("TESTING", "1")
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("USE_HTMX", "true")

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.models import MaterialCard, Requirement, Requisition, Sighting


@pytest.fixture()
def htmx_client(db_session, test_user):
    """TestClient with views router registered and auth overridden."""
    from app.database import get_db
    from app.dependencies import require_user
    from app.main import app
    from app.routers.views import router as views_router

    route_paths = [r.path for r in app.routes]
    if "/views/sourcing/{req_row_id}/results" not in route_paths:
        app.include_router(views_router)

    def _override_db():
        yield db_session

    def _override_user():
        return test_user

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_user] = _override_user

    with TestClient(app) as c:
        yield c

    app.dependency_overrides.clear()


@pytest.fixture()
def sourcing_data(db_session, test_user):
    """A requisition with a requirement and several sightings from different sources."""
    req = Requisition(
        name="Sourcing Test Req",
        customer_name="Test Corp",
        status="open",
        created_by=test_user.id,
        created_at=datetime(2026, 3, 10, tzinfo=timezone.utc),
    )
    db_session.add(req)
    db_session.flush()

    card = MaterialCard(
        normalized_mpn="lm317t",
        display_mpn="LM317T",
        manufacturer="Texas Instruments",
        description="Adjustable Voltage Regulator",
        search_count=5,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(card)
    db_session.flush()

    requirement = Requirement(
        requisition_id=req.id,
        primary_mpn="LM317T",
        material_card_id=card.id,
        target_qty=1000,
        target_price=0.50,
        sourcing_status="open",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(requirement)
    db_session.flush()

    # Add sightings from different source types
    sightings_data = [
        ("brokerbin", "Arrow Electronics", 5000, 0.45, 0.85),
        ("nexar", "Digi-Key", 10000, 0.42, 0.90),
        ("vendor_affinity", "Preferred Vendor", None, None, 0.60),
        ("material_history", "Old Source", 200, 0.55, 0.30),
    ]
    for source_type, vendor, qty, price, confidence in sightings_data:
        db_session.add(Sighting(
            requirement_id=requirement.id,
            material_card_id=card.id,
            vendor_name=vendor,
            mpn_matched="LM317T",
            manufacturer="Texas Instruments",
            qty_available=qty,
            unit_price=price,
            source_type=source_type,
            confidence=confidence,
            score=confidence * 100,
            is_authorized=(source_type in ("nexar", "brokerbin")),
            created_at=datetime.now(timezone.utc),
        ))

    db_session.commit()
    db_session.refresh(requirement)
    return {"req": req, "requirement": requirement, "card": card}


class TestSourcingResultsPartial:
    """Test GET /views/sourcing/{req_row_id}/results returns results HTML."""

    def test_sourcing_results_partial(self, htmx_client, sourcing_data):
        """Results endpoint returns HTML with sighting data."""
        req_row_id = sourcing_data["requirement"].id
        resp = htmx_client.get(f"/views/sourcing/{req_row_id}/results")
        assert resp.status_code == 200
        html = resp.text
        # Should contain result rows with vendor names
        assert "Arrow Electronics" in html
        assert "Digi-Key" in html
        # Should contain confidence values
        assert "90%" in html  # nexar confidence
        assert "85%" in html  # brokerbin confidence
        # Should contain source badges
        assert "source-badge" in html
        # Should contain filter pills
        assert "filter-pill" in html
        assert "Live Stock" in html
        assert "Historical" in html

    def test_sourcing_results_404_for_missing(self, htmx_client):
        """Returns 404 for nonexistent requirement."""
        resp = htmx_client.get("/views/sourcing/99999/results")
        assert resp.status_code == 404

    def test_sourcing_results_empty(self, htmx_client, db_session, test_user):
        """Returns empty state when no sightings exist."""
        req = Requisition(
            name="Empty Req", status="open", created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.flush()
        requirement = Requirement(
            requisition_id=req.id, primary_mpn="NOPART",
            target_qty=1, sourcing_status="open",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(requirement)
        db_session.commit()

        resp = htmx_client.get(f"/views/sourcing/{requirement.id}/results")
        assert resp.status_code == 200
        assert "No sourcing results yet" in resp.text


class TestSourcingFilter:
    """Test filter param narrows results."""

    def test_filter_live_only(self, htmx_client, sourcing_data):
        """Live filter shows only live stock sources."""
        req_row_id = sourcing_data["requirement"].id
        resp = htmx_client.get(f"/views/sourcing/{req_row_id}/results?filter=live")
        assert resp.status_code == 200
        html = resp.text
        assert "Arrow Electronics" in html
        assert "Digi-Key" in html
        # Historical and affinity should be filtered out
        assert "Old Source" not in html
        assert "Preferred Vendor" not in html

    def test_filter_affinity_only(self, htmx_client, sourcing_data):
        """Affinity filter shows only vendor affinity matches."""
        req_row_id = sourcing_data["requirement"].id
        resp = htmx_client.get(f"/views/sourcing/{req_row_id}/results?filter=affinity")
        assert resp.status_code == 200
        html = resp.text
        assert "Preferred Vendor" in html
        assert "Arrow Electronics" not in html

    def test_filter_historical_only(self, htmx_client, sourcing_data):
        """Historical filter shows only historical sightings."""
        req_row_id = sourcing_data["requirement"].id
        resp = htmx_client.get(f"/views/sourcing/{req_row_id}/results?filter=historical")
        assert resp.status_code == 200
        html = resp.text
        assert "Old Source" in html
        assert "Digi-Key" not in html

    def test_sort_price_asc(self, htmx_client, sourcing_data):
        """Price ascending sort puts cheapest first."""
        req_row_id = sourcing_data["requirement"].id
        resp = htmx_client.get(f"/views/sourcing/{req_row_id}/results?sort_by=price_asc")
        assert resp.status_code == 200
        html = resp.text
        # Digi-Key at 0.42 should appear before Arrow at 0.45
        digi_pos = html.find("Digi-Key")
        arrow_pos = html.find("Arrow Electronics")
        assert digi_pos < arrow_pos


class TestMaterialCardPartial:
    """Test material card detail renders."""

    def test_material_card_partial(self, htmx_client, sourcing_data):
        """Material card endpoint returns card detail with sightings."""
        card_id = sourcing_data["card"].id
        resp = htmx_client.get(f"/views/materials/{card_id}")
        assert resp.status_code == 200
        html = resp.text
        assert "LM317T" in html
        assert "Texas Instruments" in html
        assert "Adjustable Voltage Regulator" in html
        # Should show sighting history
        assert "Sighting History" in html
        assert "Arrow Electronics" in html

    def test_material_card_404(self, htmx_client):
        """Returns 404 for nonexistent material card."""
        resp = htmx_client.get("/views/materials/99999")
        assert resp.status_code == 404


class TestSightingRowRender:
    """Test that sighting rows render correctly within the material card."""

    def test_sighting_row_in_card(self, htmx_client, sourcing_data):
        """Material card includes sighting rows with expected fields."""
        card_id = sourcing_data["card"].id
        resp = htmx_client.get(f"/views/materials/{card_id}")
        assert resp.status_code == 200
        html = resp.text
        # Check sighting row content
        assert "sighting-row" in html
        assert "source-badge" in html
        # Price should be formatted
        assert "$0.4500" in html or "$0.4200" in html


class TestSSEStreamEndpoint:
    """Test SSE stream endpoint returns progress events."""

    def test_sse_stream_returns_progress(self, htmx_client, sourcing_data):
        """SSE stream endpoint returns search progress HTML."""
        req_row_id = sourcing_data["requirement"].id
        resp = htmx_client.get(f"/views/sourcing/{req_row_id}/stream")
        assert resp.status_code == 200
        html = resp.text
        assert "search-progress" in html
        # Should show source pills with counts
        assert "source-pill" in html
        assert "done" in html

    def test_sse_stream_404_for_missing(self, htmx_client):
        """SSE stream returns 404 for nonexistent requirement."""
        resp = htmx_client.get("/views/sourcing/99999/stream")
        assert resp.status_code == 404

    def test_sse_stream_empty_sources(self, htmx_client, db_session, test_user):
        """SSE stream with no sightings returns empty progress."""
        req = Requisition(
            name="No Sightings Req", status="open", created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.flush()
        requirement = Requirement(
            requisition_id=req.id, primary_mpn="EMPTY123",
            target_qty=1, sourcing_status="open",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(requirement)
        db_session.commit()

        resp = htmx_client.get(f"/views/sourcing/{requirement.id}/stream")
        assert resp.status_code == 200
        assert "search-progress" in resp.text
