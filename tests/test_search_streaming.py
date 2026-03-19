"""Tests for search streaming, aggressive dedup, and shortlist features.

Called by: pytest
Depends on: app/search_service.py, app/connectors/sources.py
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.connectors.mouser import MouserConnector
from app.connectors.sources import NexarConnector


def test_base_connector_has_source_name():
    """Each connector exposes a source_name property matching its source_type."""
    nexar = NexarConnector.__new__(NexarConnector)
    assert hasattr(nexar, "source_name")
    assert isinstance(nexar.source_name, str)
    assert len(nexar.source_name) > 0


def test_build_connectors_all_skipped_when_no_creds(db_session):
    """_build_connectors skips all sources when no credentials are configured."""
    from app.search_service import _build_connectors

    with patch("app.search_service.get_credentials_batch", return_value={}):
        connectors, stats, disabled = _build_connectors(db_session)

    assert isinstance(connectors, list)
    assert isinstance(stats, dict)
    assert isinstance(disabled, set)
    assert len(connectors) == 0
    assert any(s["status"] in ("skipped", "disabled") for s in stats.values())


def test_build_connectors_instantiates_with_creds(db_session):
    """_build_connectors creates connector instances when credentials exist."""
    from app.search_service import _build_connectors

    fake_creds = {("mouser", "MOUSER_API_KEY"): "fake-mouser-key"}
    with patch("app.search_service.get_credentials_batch", return_value=fake_creds):
        connectors, stats, disabled = _build_connectors(db_session)

    assert len(connectors) == 1
    assert isinstance(connectors[0], MouserConnector)
    # Mouser should not appear in stats (it was instantiated, not skipped)
    assert "mouser" not in stats
    # Other sources should be skipped
    assert stats["nexar"]["status"] == "skipped"


# ── Aggressive dedup tests ──────────────────────────────────────────────


def test_aggressive_dedup_groups_by_vendor():
    """Same vendor with different prices should merge into one entry with sub_offers."""
    from app.search_service import _deduplicate_sightings_aggressive

    sightings = [
        {
            "vendor_name": "Arrow",
            "mpn_matched": "LM317T",
            "unit_price": 0.45,
            "qty_available": 1000,
            "score": 80,
            "confidence": 0.8,
            "source_type": "nexar",
            "is_authorized": True,
            "moq": 1,
        },
        {
            "vendor_name": "Arrow",
            "mpn_matched": "LM317T",
            "unit_price": 0.48,
            "qty_available": 500,
            "score": 70,
            "confidence": 0.7,
            "source_type": "digikey",
            "is_authorized": True,
            "moq": 10,
        },
        {
            "vendor_name": "Mouser",
            "mpn_matched": "LM317T",
            "unit_price": 0.50,
            "qty_available": 2000,
            "score": 75,
            "confidence": 0.75,
            "source_type": "mouser",
            "is_authorized": True,
            "moq": 1,
        },
    ]
    result = _deduplicate_sightings_aggressive(sightings)

    # Should produce 2 entries: Arrow (merged) and Mouser
    assert len(result) == 2
    arrow = next(r for r in result if "arrow" in r["vendor_name"].lower())
    assert arrow["unit_price"] == 0.45  # best offer (highest score)
    assert arrow["qty_available"] == 1500  # summed
    assert len(arrow["sub_offers"]) == 1  # the other Arrow offer
    assert arrow["offer_count"] == 2
    assert "nexar" in arrow["sources_found"]
    assert "digikey" in arrow["sources_found"]


def test_aggressive_dedup_filters_zero_qty():
    """Sightings with qty_available=0 are excluded."""
    from app.search_service import _deduplicate_sightings_aggressive

    sightings = [
        {
            "vendor_name": "Arrow",
            "mpn_matched": "LM317T",
            "unit_price": 0.45,
            "qty_available": 0,
            "score": 80,
            "confidence": 0.8,
            "source_type": "nexar",
            "is_authorized": True,
        },
    ]
    result = _deduplicate_sightings_aggressive(sightings)
    assert len(result) == 0


def test_incremental_dedup_new_vendor():
    """New vendor results in new_cards list."""
    from app.search_service import _incremental_dedup

    existing = []
    incoming = [
        {
            "vendor_name": "Arrow",
            "mpn_matched": "LM317T",
            "unit_price": 0.45,
            "qty_available": 1000,
            "score": 80,
            "source_type": "nexar",
        },
    ]
    new_cards, updated_cards = _incremental_dedup(incoming, existing)
    assert len(new_cards) == 1
    assert len(updated_cards) == 0


def test_incremental_dedup_existing_vendor():
    """Existing vendor results in updated_cards list with merged sub_offers."""
    from app.search_service import _incremental_dedup

    existing = [
        {
            "vendor_name": "Arrow",
            "mpn_matched": "LM317T",
            "unit_price": 0.45,
            "qty_available": 1000,
            "score": 80,
            "source_type": "nexar",
            "sub_offers": [],
            "offer_count": 1,
            "sources_found": {"nexar"},
        },
    ]
    incoming = [
        {
            "vendor_name": "Arrow",
            "mpn_matched": "LM317T",
            "unit_price": 0.48,
            "qty_available": 500,
            "score": 70,
            "source_type": "digikey",
        },
    ]
    new_cards, updated_cards = _incremental_dedup(incoming, existing)
    assert len(new_cards) == 0
    assert len(updated_cards) == 1
    assert updated_cards[0]["offer_count"] == 2


# ── Streaming search tests ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stream_search_publishes_events(db_session):
    """stream_search_mpn publishes source-status and results events to the SSE
    broker."""
    from app.search_service import stream_search_mpn

    published_events = []

    async def mock_publish(channel, event, data=""):
        published_events.append({"channel": channel, "event": event, "data": data})

    # Mock broker and connectors
    with (
        patch("app.search_service.broker", create=True) as mock_broker,
        patch("app.search_service._build_connectors") as mock_build,
    ):
        mock_broker.publish = mock_publish

        # One fake connector that returns one result
        fake_connector = MagicMock()
        fake_connector.source_name = "nexar"
        fake_connector.search = AsyncMock(
            return_value=[
                {
                    "vendor_name": "Arrow",
                    "mpn_matched": "LM317T",
                    "unit_price": 0.45,
                    "qty_available": 1000,
                    "source_type": "nexar",
                    "is_authorized": True,
                }
            ]
        )
        mock_build.return_value = ([fake_connector], {}, set())

        await stream_search_mpn("test-search-id", "LM317T", db_session)

    # Should have published source-status + results + done events
    event_types = [e["event"] for e in published_events]
    assert "source-status" in event_types
    assert "results" in event_types
    assert "done" in event_types
    assert all(e["channel"] == "search:test-search-id" for e in published_events)

    # Verify done event stats have correct keys
    done_event = next(e for e in published_events if e["event"] == "done")
    done_data = json.loads(done_event["data"])
    assert "total_results" in done_data
    assert "elapsed_seconds" in done_data


# ── Route tests ───────────────────────────────────────────────────────


def test_search_run_returns_shell_html(client, db_session):
    """POST /v2/partials/search/run should return results shell with SSE connection."""
    with patch("app.search_service.stream_search_mpn", new_callable=AsyncMock):
        resp = client.post(
            "/v2/partials/search/run",
            data={"mpn": "LM317T"},
            headers={"HX-Request": "true"},
        )
    assert resp.status_code == 200
    html = resp.text
    assert "sse-connect" in html
    assert "source-chip" in html or "source-progress" in html


def test_vendor_card_template_renders():
    """vendor_card.html renders without errors with sample data."""
    from jinja2 import Environment, FileSystemLoader

    env = Environment(loader=FileSystemLoader("app/templates"))
    tpl = env.get_template("htmx/partials/search/vendor_card.html")
    html = tpl.render(
        card={
            "vendor_name": "Arrow Electronics",
            "mpn_matched": "LM317T",
            "manufacturer": "Texas Instruments",
            "unit_price": 0.45,
            "qty_available": 12450,
            "moq": 1,
            "confidence_color": "green",
            "confidence_pct": 85,
            "lead_quality": "strong",
            "source_badge": "Live Stock",
            "is_authorized": True,
            "source_type": "nexar",
            "sub_offers": [],
            "offer_count": 1,
            "sources_found": ["nexar"],
            "reason": "Authorized distributor with confirmed stock",
        },
        card_index=0,
        search_id="test-123",
    )
    assert "Arrow Electronics" in html
    assert "LM317T" in html
    assert "0.4500" in html
    assert "12,450" in html
    assert "AUTH" in html
    assert "85%" in html
    assert "nexar" in html
    assert "Texas Instruments" in html


def test_vendor_card_template_renders_no_price():
    """vendor_card.html renders gracefully when unit_price is None."""
    from jinja2 import Environment, FileSystemLoader

    env = Environment(loader=FileSystemLoader("app/templates"))
    tpl = env.get_template("htmx/partials/search/vendor_card.html")
    html = tpl.render(
        card={
            "vendor_name": "Unknown Vendor",
            "mpn_matched": "ABC123",
            "manufacturer": None,
            "unit_price": None,
            "qty_available": 0,
            "moq": None,
            "confidence_color": "red",
            "confidence_pct": 20,
            "lead_quality": "",
            "is_authorized": False,
            "source_type": "brokerbin",
            "sub_offers": [],
            "offer_count": 1,
            "sources_found": ["brokerbin"],
            "reason": "",
        },
        card_index=3,
        search_id="test-456",
    )
    assert "Unknown Vendor" in html
    assert "No price" in html
    assert "AUTH" not in html


def test_vendor_card_template_renders_sub_offers():
    """vendor_card.html renders expandable sub-offers table."""
    from jinja2 import Environment, FileSystemLoader

    env = Environment(loader=FileSystemLoader("app/templates"))
    tpl = env.get_template("htmx/partials/search/vendor_card.html")
    html = tpl.render(
        card={
            "vendor_name": "Mouser",
            "mpn_matched": "LM317T",
            "manufacturer": "TI",
            "unit_price": 0.50,
            "qty_available": 3000,
            "moq": 10,
            "confidence_color": "amber",
            "confidence_pct": 60,
            "lead_quality": "fair",
            "is_authorized": True,
            "source_type": "mouser",
            "sub_offers": [
                {"source_type": "digikey", "unit_price": 0.55, "qty_available": 1000},
                {"source_type": "nexar", "unit_price": 0.48, "qty_available": 2000},
            ],
            "offer_count": 3,
            "sources_found": ["mouser", "digikey", "nexar"],
            "reason": "Multiple sources",
        },
        card_index=1,
        search_id="test-789",
    )
    assert "3 offers" in html
    assert "digikey" in html
    assert "0.5500" in html
    assert "2,000" in html


def test_shortlist_bar_template_renders():
    """shortlist_bar.html renders with Alpine.js directives."""
    from jinja2 import Environment, FileSystemLoader

    env = Environment(loader=FileSystemLoader("app/templates"))
    tpl = env.get_template("htmx/partials/search/shortlist_bar.html")
    html = tpl.render()
    assert "$store.shortlist" in html
    assert "Add to Requisition" in html


def test_search_run_empty_mpn_returns_error(client):
    """POST /v2/partials/search/run with empty MPN returns error message."""
    resp = client.post(
        "/v2/partials/search/run",
        data={"mpn": ""},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    assert "Please enter a part number" in resp.text


def test_search_filter_reads_from_cache(client, db_session):
    """GET /v2/partials/search/filter returns re-rendered cards from cached results."""
    cached_results = [
        {
            "vendor_name": "Arrow",
            "mpn_matched": "LM317T",
            "unit_price": 0.45,
            "confidence_color": "green",
            "confidence_pct": 85,
            "lead_quality": "strong",
            "source_type": "nexar",
            "sub_offers": [],
            "offer_count": 1,
            "sources_found": ["nexar"],
            "score": 80,
            "is_authorized": True,
        },
    ]

    with patch(
        "app.routers.htmx_views._get_cached_search_results",
        return_value=cached_results,
    ):
        resp = client.get(
            "/v2/partials/search/filter?search_id=test-123&confidence=high",
            headers={"HX-Request": "true"},
        )
    assert resp.status_code == 200
    assert "Arrow" in resp.text


def test_search_filter_expired_returns_message(client, db_session):
    """GET /v2/partials/search/filter with no cached data returns expiry message."""
    with patch(
        "app.routers.htmx_views._get_cached_search_results",
        return_value=None,
    ):
        resp = client.get(
            "/v2/partials/search/filter?search_id=expired-123",
            headers={"HX-Request": "true"},
        )
    assert resp.status_code == 200
    assert "expired" in resp.text.lower() or "search again" in resp.text.lower()


def test_search_filter_confidence_filters(client, db_session):
    """GET /v2/partials/search/filter with confidence=high filters out non-green
    results."""
    cached_results = [
        {
            "vendor_name": "Arrow",
            "mpn_matched": "LM317T",
            "unit_price": 0.45,
            "confidence_color": "green",
            "confidence_pct": 85,
            "lead_quality": "strong",
            "source_type": "nexar",
            "sub_offers": [],
            "offer_count": 1,
            "sources_found": ["nexar"],
            "score": 80,
            "is_authorized": True,
        },
        {
            "vendor_name": "Shady Broker",
            "mpn_matched": "LM317T",
            "unit_price": 0.20,
            "confidence_color": "red",
            "confidence_pct": 20,
            "lead_quality": "",
            "source_type": "brokerbin",
            "sub_offers": [],
            "offer_count": 1,
            "sources_found": ["brokerbin"],
            "score": 30,
            "is_authorized": False,
        },
    ]

    with patch(
        "app.routers.htmx_views._get_cached_search_results",
        return_value=cached_results,
    ):
        resp = client.get(
            "/v2/partials/search/filter?search_id=test-123&confidence=high",
            headers={"HX-Request": "true"},
        )
    assert resp.status_code == 200
    assert "Arrow" in resp.text
    assert "Shady Broker" not in resp.text


# ── Add to Requisition tests ────────────────────────────────────────────


def test_add_to_requisition_creates_sightings(client, db_session):
    """POST /v2/partials/search/add-to-requisition creates Requirement + Sighting
    rows."""
    from app.models.sourcing import Requirement, Requisition, Sighting

    req = Requisition(name="Test Req", customer_name="Test Co")
    db_session.add(req)
    db_session.commit()
    db_session.refresh(req)

    resp = client.post(
        "/v2/partials/search/add-to-requisition",
        headers={"HX-Request": "true", "Content-Type": "application/json"},
        json={
            "requisition_id": req.id,
            "mpn": "LM317T",
            "items": [
                {
                    "vendor_name": "Arrow",
                    "mpn_matched": "LM317T",
                    "unit_price": 0.45,
                    "qty_available": 1000,
                    "source_type": "nexar",
                    "is_authorized": True,
                    "confidence": 0.8,
                    "score": 80,
                }
            ],
        },
    )
    assert resp.status_code == 200
    assert "Added 1 result" in resp.text

    requirement = db_session.query(Requirement).filter_by(requisition_id=req.id, primary_mpn="LM317T").first()
    assert requirement is not None
    assert requirement.normalized_mpn == "LM317T"

    sighting = db_session.query(Sighting).filter_by(requirement_id=requirement.id).first()
    assert sighting is not None
    assert sighting.vendor_name == "Arrow"
    assert sighting.unit_price == 0.45


def test_add_to_requisition_reuses_existing_requirement(client, db_session):
    """Adding to a requisition with an existing Requirement reuses it."""
    from app.models.sourcing import Requirement, Requisition, Sighting

    req = Requisition(name="Existing Req", customer_name="Acme")
    db_session.add(req)
    db_session.commit()
    db_session.refresh(req)

    # Pre-create a Requirement
    requirement = Requirement(
        requisition_id=req.id,
        primary_mpn="LM317T",
        normalized_mpn="LM317T",
        sourcing_status="open",
    )
    db_session.add(requirement)
    db_session.commit()
    db_session.refresh(requirement)

    resp = client.post(
        "/v2/partials/search/add-to-requisition",
        headers={"HX-Request": "true", "Content-Type": "application/json"},
        json={
            "requisition_id": req.id,
            "mpn": "LM317T",
            "items": [{"vendor_name": "Mouser", "source_type": "mouser", "score": 70}],
        },
    )
    assert resp.status_code == 200

    # Should still be exactly one Requirement
    count = db_session.query(Requirement).filter_by(requisition_id=req.id, primary_mpn="LM317T").count()
    assert count == 1

    sighting = db_session.query(Sighting).filter_by(requirement_id=requirement.id).first()
    assert sighting is not None
    assert sighting.vendor_name == "Mouser"


def test_add_to_requisition_missing_fields(client):
    """POST with missing fields returns 400."""
    resp = client.post(
        "/v2/partials/search/add-to-requisition",
        headers={"HX-Request": "true", "Content-Type": "application/json"},
        json={"requisition_id": None, "mpn": "", "items": []},
    )
    assert resp.status_code == 400
    assert "Missing required fields" in resp.text


def test_add_to_requisition_not_found(client):
    """POST with nonexistent requisition returns 404."""
    resp = client.post(
        "/v2/partials/search/add-to-requisition",
        headers={"HX-Request": "true", "Content-Type": "application/json"},
        json={"requisition_id": 999999, "mpn": "LM317T", "items": [{"vendor_name": "X"}]},
    )
    assert resp.status_code == 404
    assert "Requisition not found" in resp.text


def test_requisition_picker_renders(client, db_session):
    """GET /v2/partials/search/requisition-picker returns the modal HTML."""
    from app.models.sourcing import Requisition

    req = Requisition(name="Pick Me", customer_name="TestCo")
    db_session.add(req)
    db_session.commit()

    resp = client.get(
        "/v2/partials/search/requisition-picker?mpn=LM317T&items=[]",
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    assert "Pick Me" in resp.text
    assert "Add to Requisition" in resp.text


def test_lead_detail_reads_from_cache(client, db_session):
    """Lead detail route reads vendor data from Redis cache."""
    cached_results = [
        {
            "vendor_name": "Arrow",
            "mpn_matched": "LM317T",
            "unit_price": 0.45,
            "confidence_color": "green",
            "confidence_pct": 85,
            "lead_quality": "strong",
            "source_type": "nexar",
            "reason": "Authorized distributor",
            "sub_offers": [
                {"unit_price": 0.48, "source_type": "digikey", "qty_available": 500},
            ],
            "offer_count": 2,
            "sources_found": ["nexar", "digikey"],
            "is_authorized": True,
            "qty_available": 1000,
        },
    ]

    with patch(
        "app.routers.htmx_views._get_cached_search_results",
        return_value=cached_results,
    ):
        resp = client.get(
            "/v2/partials/search/lead-detail?search_id=test-123&vendor_key=arrow",
            headers={"HX-Request": "true"},
        )
    assert resp.status_code == 200
    assert "Arrow" in resp.text


def test_lead_detail_cache_miss_returns_not_found(client, db_session):
    """Lead detail route returns friendly message when cache is empty."""
    with patch(
        "app.routers.htmx_views._get_cached_search_results",
        return_value=None,
    ):
        resp = client.get(
            "/v2/partials/search/lead-detail?search_id=test-123&vendor_key=arrow",
            headers={"HX-Request": "true"},
        )
    assert resp.status_code == 200
    assert "not found" in resp.text.lower() or "search again" in resp.text.lower()


def test_lead_detail_cache_vendor_not_matched(client, db_session):
    """Lead detail returns not-found when vendor_key doesn't match any cached result."""
    cached_results = [
        {"vendor_name": "Mouser", "mpn_matched": "LM317T", "unit_price": 0.50},
    ]

    with patch(
        "app.routers.htmx_views._get_cached_search_results",
        return_value=cached_results,
    ):
        resp = client.get(
            "/v2/partials/search/lead-detail?search_id=test-123&vendor_key=nonexistent",
            headers={"HX-Request": "true"},
        )
    assert resp.status_code == 200
    assert "not found" in resp.text.lower() or "search again" in resp.text.lower()


# ── Integration smoke test ────────────────────────────────────────────


def test_full_search_flow_smoke(client, db_session):
    """Smoke test: search form → shell → filter → add-to-req."""
    # 1. Submit search (returns shell with SSE connection)
    with patch("app.search_service.stream_search_mpn", new_callable=AsyncMock):
        resp = client.post(
            "/v2/partials/search/run",
            data={"mpn": "LM317T"},
            headers={"HX-Request": "true"},
        )
    assert resp.status_code == 200
    assert "sse-connect" in resp.text

    # 2. Filter with cached results
    cached = [
        {
            "vendor_name": "Arrow",
            "mpn_matched": "LM317T",
            "unit_price": 0.45,
            "confidence_color": "green",
            "confidence_pct": 85,
            "lead_quality": "strong",
            "source_type": "nexar",
            "sub_offers": [],
            "offer_count": 1,
            "sources_found": ["nexar"],
            "score": 80,
            "is_authorized": True,
            "qty_available": 1000,
        },
    ]
    with patch("app.routers.htmx_views._get_cached_search_results", return_value=cached):
        resp = client.get(
            "/v2/partials/search/filter?search_id=test-123",
            headers={"HX-Request": "true"},
        )
    assert resp.status_code == 200
    assert "Arrow" in resp.text
