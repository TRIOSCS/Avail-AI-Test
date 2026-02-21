"""
tests/test_routers_sources.py — Tests for Sources & Email Mining Router

Tests connector factory, sighting creation from attachments,
email mining test connector, source management endpoints,
email mining endpoints, and attachment parsing.

Called by: pytest
Depends on: routers/sources.py
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import ApiSource, Requirement, Requisition, Sighting, User, VendorCard, VendorResponse
from app.rate_limit import limiter
from app.routers.sources import (
    _create_sightings_from_attachment,
    _EmailMiningTestConnector,
    _get_connector_for_source,
)

# ── _EmailMiningTestConnector ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_email_mining_test_connector_returns_status():
    connector = _EmailMiningTestConnector()
    results = await connector.search("LM358N")
    assert len(results) == 1
    assert results[0]["status"] == "ok"


# ── _get_connector_for_source ─────────────────────────────────────────


def test_get_connector_unknown_source():
    """Unknown source name returns None."""
    result = _get_connector_for_source("nonexistent_source")
    assert result is None


def test_get_connector_email_mining_when_enabled(monkeypatch):
    """Email mining returns test connector when enabled."""
    monkeypatch.setattr("app.routers.sources.settings", SimpleNamespace(
        email_mining_enabled=True,
        nexar_client_id=None, brokerbin_api_key=None, ebay_client_id=None,
        digikey_client_id=None, mouser_api_key=None, oemsecrets_api_key=None,
        sourcengine_api_key=None,
    ))
    result = _get_connector_for_source("email_mining")
    assert isinstance(result, _EmailMiningTestConnector)


def test_get_connector_email_mining_when_disabled(monkeypatch):
    """Email mining returns None when disabled."""
    monkeypatch.setattr("app.routers.sources.settings", SimpleNamespace(
        email_mining_enabled=False,
        nexar_client_id=None, brokerbin_api_key=None, ebay_client_id=None,
        digikey_client_id=None, mouser_api_key=None, oemsecrets_api_key=None,
        sourcengine_api_key=None,
    ))
    result = _get_connector_for_source("email_mining")
    assert result is None


# ── _create_sightings_from_attachment ────────────────────────────────


def _mock_db_for_sightings(requirements: list, existing_sightings: list | None = None):
    """Build a mock db session for sighting creation tests."""
    db = MagicMock()

    def query_side_effect(model):
        mock_q = MagicMock()
        model_name = model.__name__ if hasattr(model, "__name__") else str(model)
        if model_name == "Requirement":
            mock_q.filter_by.return_value.all.return_value = requirements
        elif model_name == "Sighting":
            mock_q.filter_by.return_value.first.return_value = (
                existing_sightings[0] if existing_sightings else None
            )
        return mock_q

    db.query.side_effect = query_side_effect
    db.add = MagicMock()
    db.flush = MagicMock()
    return db


def test_create_sightings_exact_mpn_match():
    """Rows with exact MPN match create sightings."""
    req = SimpleNamespace(id=1, mpn="LM358N", requisition_id=10)
    vr = SimpleNamespace(requisition_id=10, vendor_name="ACME", vendor_email="a@acme.com")
    db = _mock_db_for_sightings([req])

    rows = [{"mpn": "LM358N", "qty": 100, "unit_price": 0.50}]
    created = _create_sightings_from_attachment(db, vr, rows)

    assert created == 1
    assert db.add.call_count == 1


def test_create_sightings_no_requirements():
    """No requirements → 0 sightings."""
    vr = SimpleNamespace(requisition_id=10, vendor_name="ACME", vendor_email="a@acme.com")
    db = _mock_db_for_sightings([])

    rows = [{"mpn": "LM358N", "qty": 100}]
    created = _create_sightings_from_attachment(db, vr, rows)

    assert created == 0


def test_create_sightings_skips_empty_mpn():
    """Rows with no MPN are skipped."""
    req = SimpleNamespace(id=1, mpn="LM358N", requisition_id=10)
    vr = SimpleNamespace(requisition_id=10, vendor_name="ACME", vendor_email="a@acme.com")
    db = _mock_db_for_sightings([req])

    rows = [{"mpn": "", "qty": 100}, {"mpn": None, "qty": 200}]
    created = _create_sightings_from_attachment(db, vr, rows)

    assert created == 0


def test_create_sightings_skips_duplicates():
    """Existing sighting prevents duplicate creation."""
    req = SimpleNamespace(id=1, mpn="LM358N", requisition_id=10)
    existing = SimpleNamespace(id=99)  # Already exists
    vr = SimpleNamespace(requisition_id=10, vendor_name="ACME", vendor_email="a@acme.com")
    db = _mock_db_for_sightings([req], existing_sightings=[existing])

    rows = [{"mpn": "LM358N", "qty": 100}]
    created = _create_sightings_from_attachment(db, vr, rows)

    assert created == 0


def test_create_sightings_case_insensitive_mpn():
    """MPN matching is case-insensitive (both uppercased)."""
    req = SimpleNamespace(id=1, mpn="lm358n", requisition_id=10)
    vr = SimpleNamespace(requisition_id=10, vendor_name="ACME", vendor_email="a@acme.com")
    db = _mock_db_for_sightings([req])

    rows = [{"mpn": "LM358N", "qty": 100}]
    created = _create_sightings_from_attachment(db, vr, rows)

    assert created == 1


# ══════════════════════════════════════════════════════════════════════
# Source Management (HTTP endpoint tests)
# ══════════════════════════════════════════════════════════════════════


@pytest.fixture()
def _api_source(db_session: Session) -> ApiSource:
    """Create a sample ApiSource row for endpoint tests."""
    src = ApiSource(
        name="test_source",
        display_name="Test Source",
        category="market_data",
        source_type="api",
        status="pending",
        description="A test source",
        env_vars=["TEST_API_KEY"],
        total_searches=0,
        total_results=0,
        avg_response_ms=0,
    )
    db_session.add(src)
    db_session.commit()
    db_session.refresh(src)
    return src


@pytest.fixture()
def _email_mining_source(db_session: Session) -> ApiSource:
    """Create the email_mining ApiSource row used by status/scan endpoints."""
    src = ApiSource(
        name="email_mining",
        display_name="Email Mining",
        category="intelligence",
        source_type="email",
        status="live",
        description="Email inbox intelligence",
        env_vars=[],
        total_searches=5,
        total_results=20,
        avg_response_ms=0,
        last_success=datetime(2026, 2, 1, tzinfo=timezone.utc),
    )
    db_session.add(src)
    db_session.commit()
    db_session.refresh(src)
    return src


@pytest.fixture()
def sources_client(db_session: Session, test_user: User) -> TestClient:
    """TestClient with auth + settings_access overrides and limiter reset."""
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
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ── 1. test_list_sources ─────────────────────────────────────────────


def test_list_sources(sources_client: TestClient, _api_source: ApiSource):
    """GET /api/sources returns 200 with a list of source dicts."""
    resp = sources_client.get("/api/sources")
    assert resp.status_code == 200
    data = resp.json()
    assert "sources" in data
    assert isinstance(data["sources"], list)
    assert len(data["sources"]) >= 1


# ── 2. test_list_sources_includes_status ─────────────────────────────


def test_list_sources_includes_status(sources_client: TestClient, _api_source: ApiSource):
    """Each source dict contains id, name, status, and display_name fields."""
    resp = sources_client.get("/api/sources")
    assert resp.status_code == 200
    sources = resp.json()["sources"]
    src = next((s for s in sources if s["name"] == "test_source"), None)
    assert src is not None
    assert "id" in src
    assert "name" in src
    assert "status" in src
    assert "display_name" in src
    assert src["name"] == "test_source"


# ── 3. test_test_source_success ──────────────────────────────────────


def test_test_source_success(sources_client: TestClient, _api_source: ApiSource):
    """POST /api/sources/{id}/test with a mock connector returns ok status."""
    mock_connector = MagicMock()
    mock_connector.search = AsyncMock(return_value=[{"vendor_name": "Test", "status": "ok"}])

    with patch("app.routers.sources._get_connector_for_source", return_value=mock_connector):
        resp = sources_client.post(f"/api/sources/{_api_source.id}/test")

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["results_count"] == 1
    assert data["error"] is None


# ── 4. test_test_source_failure ──────────────────────────────────────


def test_test_source_failure(sources_client: TestClient, _api_source: ApiSource):
    """POST /api/sources/{id}/test with failing connector returns error status."""
    mock_connector = MagicMock()
    mock_connector.search = AsyncMock(side_effect=ValueError("API key invalid"))

    with patch("app.routers.sources._get_connector_for_source", return_value=mock_connector):
        resp = sources_client.post(f"/api/sources/{_api_source.id}/test")

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "error"
    assert "API key invalid" in data["error"]


# ── 5. test_test_source_not_found ────────────────────────────────────


def test_test_source_not_found(sources_client: TestClient):
    """POST /api/sources/99999/test returns 404 for nonexistent source."""
    resp = sources_client.post("/api/sources/99999/test")
    assert resp.status_code == 404


# ── 6. test_toggle_source_enable ─────────────────────────────────────


def test_toggle_source_enable(sources_client: TestClient, _api_source: ApiSource):
    """PUT /api/sources/{id}/toggle with status=live updates status."""
    resp = sources_client.put(
        f"/api/sources/{_api_source.id}/toggle",
        json={"status": "live"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    # Since env_vars exist but no credentials are set, status auto-detects to "pending"
    assert data["status"] in ("live", "pending")


# ── 7. test_toggle_source_disable ────────────────────────────────────


def test_toggle_source_disable(sources_client: TestClient, _api_source: ApiSource):
    """PUT /api/sources/{id}/toggle with status=disabled disables source."""
    resp = sources_client.put(
        f"/api/sources/{_api_source.id}/toggle",
        json={"status": "disabled"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["status"] == "disabled"


# ── 8. test_email_mining_status ──────────────────────────────────────


def test_email_mining_status(sources_client: TestClient, _email_mining_source: ApiSource):
    """GET /api/email-mining/status returns 200 with status info."""
    resp = sources_client.get("/api/email-mining/status")
    assert resp.status_code == 200
    data = resp.json()
    assert "enabled" in data
    assert "last_scan" in data
    assert "total_scans" in data
    assert "total_vendors_found" in data
    assert data["total_scans"] == 5
    assert data["total_vendors_found"] == 20


# ══════════════════════════════════════════════════════════════════════
# Email Mining (HTTP endpoint tests)
# ══════════════════════════════════════════════════════════════════════


# ── 9. test_scan_inbox ───────────────────────────────────────────────


def test_scan_inbox(sources_client: TestClient, _email_mining_source: ApiSource):
    """POST /api/email-mining/scan with mocked deps returns scan results."""
    mock_miner = MagicMock()
    mock_miner.scan_inbox = AsyncMock(return_value={
        "messages_scanned": 50,
        "vendors_found": 3,
        "offers_parsed": [],
        "contacts_enriched": [],
        "stock_lists_found": 1,
    })

    with patch("app.routers.sources.require_fresh_token", new_callable=AsyncMock, return_value="fake-token"), \
         patch("app.connectors.email_mining.EmailMiner", return_value=mock_miner):
        resp = sources_client.post("/api/email-mining/scan")

    assert resp.status_code == 200
    data = resp.json()
    assert data["messages_scanned"] == 50
    assert data["vendors_found"] == 3
    assert data["stock_lists_found"] == 1


# ── 10. test_scan_inbox_no_m365 ──────────────────────────────────────


def test_scan_inbox_no_m365(sources_client: TestClient):
    """POST /api/email-mining/scan fails when require_fresh_token raises 401."""
    from fastapi import HTTPException

    with patch(
        "app.routers.sources.require_fresh_token",
        new_callable=AsyncMock,
        side_effect=HTTPException(status_code=401, detail="No access token"),
    ):
        resp = sources_client.post("/api/email-mining/scan")

    assert resp.status_code == 401


# ── 11. test_scan_outbound ───────────────────────────────────────────


def test_scan_outbound(
    sources_client: TestClient,
    test_user: User,
    db_session: Session,
):
    """POST /api/email-mining/scan-outbound with mocked deps returns results."""
    # Ensure user has m365 connected and access_token
    test_user.m365_connected = True
    test_user.access_token = "fake-access-token"
    db_session.commit()

    mock_miner = MagicMock()
    mock_miner.scan_sent_items = AsyncMock(return_value={
        "messages_scanned": 30,
        "rfqs_detected": 5,
        "vendors_contacted": {"arrow.com": 3, "digikey.com": 2},
        "used_delta": False,
    })

    with patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="fresh-token"), \
         patch("app.connectors.email_mining.EmailMiner", return_value=mock_miner):
        resp = sources_client.post("/api/email-mining/scan-outbound")

    assert resp.status_code == 200
    data = resp.json()
    assert data["messages_scanned"] == 30
    assert data["rfqs_detected"] == 5
    assert data["vendors_contacted"] == 2


# ── 12. test_scan_outbound_no_m365 ───────────────────────────────────


def test_scan_outbound_no_m365(
    sources_client: TestClient,
    test_user: User,
    db_session: Session,
):
    """POST /api/email-mining/scan-outbound fails when M365 not connected."""
    test_user.m365_connected = False
    test_user.access_token = None
    db_session.commit()

    resp = sources_client.post("/api/email-mining/scan-outbound")
    assert resp.status_code == 400


# ── 13. test_compute_engagement ──────────────────────────────────────


def test_compute_engagement(sources_client: TestClient):
    """POST /api/email-mining/compute-engagement triggers recomputation."""
    with patch(
        "app.services.engagement_scorer.compute_all_engagement_scores",
        new_callable=AsyncMock,
        return_value={"updated": 5, "skipped": 0},
    ):
        resp = sources_client.post("/api/email-mining/compute-engagement")

    assert resp.status_code == 200
    data = resp.json()
    assert data["updated"] == 5
    assert data["skipped"] == 0


# ── 14. test_vendor_engagement_detail ────────────────────────────────


def test_vendor_engagement_detail(
    sources_client: TestClient,
    test_vendor_card: VendorCard,
):
    """GET /api/vendors/{id}/engagement returns 200 with engagement data."""
    resp = sources_client.get(f"/api/vendors/{test_vendor_card.id}/engagement")
    assert resp.status_code == 200
    data = resp.json()
    assert data["vendor_id"] == test_vendor_card.id
    assert data["vendor_name"] == "Arrow Electronics"
    assert "engagement_score" in data
    assert "metrics" in data
    assert "raw_counts" in data
    assert "response_rate" in data["metrics"]
    assert "ghost_rate" in data["metrics"]


# ── 15. test_vendor_engagement_not_found ─────────────────────────────


def test_vendor_engagement_not_found(sources_client: TestClient):
    """GET /api/vendors/99999/engagement returns 404 for nonexistent vendor."""
    resp = sources_client.get("/api/vendors/99999/engagement")
    assert resp.status_code == 404


# ── 16. test_vendor_engagement_detail_with_data ──────────────────────


def test_vendor_engagement_detail_with_data(
    sources_client: TestClient,
    db_session: Session,
):
    """Vendor with engagement data returns computed score in response."""
    card = VendorCard(
        normalized_name="engaged vendor",
        display_name="Engaged Vendor",
        emails=["sales@engaged.com"],
        phones=[],
        total_outreach=10,
        total_responses=7,
        total_wins=3,
        response_velocity_hours=2.5,
        last_contact_at=datetime.now(timezone.utc),
        engagement_score=82.5,
        engagement_computed_at=datetime.now(timezone.utc),
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(card)
    db_session.commit()
    db_session.refresh(card)

    resp = sources_client.get(f"/api/vendors/{card.id}/engagement")
    assert resp.status_code == 200
    data = resp.json()
    assert data["vendor_id"] == card.id
    assert data["engagement_score"] > 0
    assert data["raw_counts"]["total_outreach"] == 10
    assert data["raw_counts"]["total_responses"] == 7
    assert data["raw_counts"]["total_wins"] == 3
    assert data["computed_at"] is not None


# ══════════════════════════════════════════════════════════════════════
# Attachment Parsing (HTTP endpoint tests)
# ══════════════════════════════════════════════════════════════════════


@pytest.fixture()
def _vendor_response(db_session: Session, test_user: User) -> VendorResponse:
    """Create a VendorResponse linked to a requisition with a requirement."""
    req = Requisition(
        name="REQ-ATT-001",
        customer_name="Acme",
        status="open",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.flush()

    requirement = Requirement(
        requisition_id=req.id,
        primary_mpn="LM358N",
        target_qty=500,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(requirement)
    db_session.flush()

    vr = VendorResponse(
        requisition_id=req.id,
        vendor_name="ACME Corp",
        vendor_email="sales@acme.com",
        subject="RE: RFQ LM358N",
        message_id="graph-msg-att-001",
        status="new",
        received_at=datetime.now(timezone.utc),
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(vr)
    db_session.commit()
    db_session.refresh(vr)
    return vr


# ── 17. test_parse_response_attachments_missing_id ───────────────────


def test_parse_response_attachments_missing_id(
    sources_client: TestClient,
    test_user: User,
    db_session: Session,
):
    """POST parse-response-attachments/99999 returns 404 for nonexistent response."""
    test_user.m365_connected = True
    test_user.access_token = "fake-token"
    db_session.commit()

    resp = sources_client.post("/api/email-mining/parse-response-attachments/99999")
    assert resp.status_code == 404


# ── 18. test_parse_response_attachments_no_attachments ───────────────


def test_parse_response_attachments_no_attachments(
    sources_client: TestClient,
    test_user: User,
    db_session: Session,
    _vendor_response: VendorResponse,
):
    """Response exists but Graph API returns no parseable files."""
    test_user.m365_connected = True
    test_user.access_token = "fake-token"
    db_session.commit()

    mock_gc = MagicMock()
    mock_gc.get_json = AsyncMock(return_value={
        "value": [
            {"name": "image.png", "contentBytes": "abc"},  # not parseable
        ]
    })

    with patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="fresh-token"), \
         patch("app.utils.graph_client.GraphClient", return_value=mock_gc):
        resp = sources_client.post(
            f"/api/email-mining/parse-response-attachments/{_vendor_response.id}"
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["attachments_found"] == 1
    assert data["parseable"] == 0
    assert data["sightings_created"] == 0


# ── 19. test_parse_response_attachments_success ──────────────────────


def test_parse_response_attachments_success(
    sources_client: TestClient,
    test_user: User,
    db_session: Session,
    _vendor_response: VendorResponse,
):
    """Response with attachment; mock parse_attachment returns rows that create sightings."""
    import base64

    test_user.m365_connected = True
    test_user.access_token = "fake-token"
    db_session.commit()

    fake_content = base64.b64encode(b"fake-excel-content").decode()
    mock_gc = MagicMock()
    mock_gc.get_json = AsyncMock(return_value={
        "value": [
            {"name": "quote.xlsx", "contentBytes": fake_content},
        ]
    })

    parsed_rows = [
        {"mpn": "LM358N", "qty": 500, "unit_price": 0.45, "manufacturer": "TI"},
    ]

    with patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="fresh-token"), \
         patch("app.utils.graph_client.GraphClient", return_value=mock_gc), \
         patch("app.services.attachment_parser.parse_attachment", new_callable=AsyncMock, return_value=parsed_rows), \
         patch("app.utils.file_validation.validate_file", return_value=(True, "")), \
         patch("app.routers.sources._create_sightings_from_attachment", return_value=1):
        resp = sources_client.post(
            f"/api/email-mining/parse-response-attachments/{_vendor_response.id}"
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["attachments_found"] == 1
    assert data["parseable"] == 1
    assert data["rows_parsed"] == 1
    assert data["sightings_created"] == 1


# ══════════════════════════════════════════════════════════════════════
# Sighting creation unit tests (fuzzy match, multiple reqs, price parsing)
# ══════════════════════════════════════════════════════════════════════


# ── 20. test_create_sightings_fuzzy_mpn_match ────────────────────────


def test_create_sightings_fuzzy_mpn_match():
    """Rows with slightly different MPN still match requirements via fuzzy logic."""
    # "LM358N" vs "LM358-N" — fuzzy_mpn_match strips dashes
    req = SimpleNamespace(id=1, mpn="LM358N", requisition_id=10)
    vr = SimpleNamespace(requisition_id=10, vendor_name="ACME", vendor_email="a@acme.com")
    db = _mock_db_for_sightings([req])

    rows = [{"mpn": "LM358-N", "qty": 100, "unit_price": 0.50}]
    created = _create_sightings_from_attachment(db, vr, rows)

    # The row MPN "LM358-N" uppercased is "LM358-N", which doesn't exactly match
    # "LM358N" in req_map. Fuzzy matching should still match after normalization.
    assert created == 1
    assert db.add.call_count == 1


# ── 21. test_create_sightings_multiple_requirements ──────────────────


def test_create_sightings_multiple_requirements():
    """Multiple matching requirements create sightings for each matched row."""
    req1 = SimpleNamespace(id=1, mpn="LM358N", requisition_id=10)
    req2 = SimpleNamespace(id=2, mpn="LM317T", requisition_id=10)
    vr = SimpleNamespace(requisition_id=10, vendor_name="ACME", vendor_email="a@acme.com")
    db = _mock_db_for_sightings([req1, req2])

    rows = [
        {"mpn": "LM358N", "qty": 100, "unit_price": 0.50},
        {"mpn": "LM317T", "qty": 200, "unit_price": 0.30},
    ]
    created = _create_sightings_from_attachment(db, vr, rows)

    assert created == 2
    assert db.add.call_count == 2


# ── 22. test_create_sightings_price_parsing ──────────────────────────


def test_create_sightings_price_parsing():
    """Rows with string prices like '$1.50' are parsed correctly via normalize_price."""
    req = SimpleNamespace(id=1, mpn="LM358N", requisition_id=10)
    vr = SimpleNamespace(requisition_id=10, vendor_name="ACME", vendor_email="a@acme.com")
    db = _mock_db_for_sightings([req])

    rows = [{"mpn": "LM358N", "qty": 100, "unit_price": "$1.50"}]
    created = _create_sightings_from_attachment(db, vr, rows)

    assert created == 1
    # Verify the Sighting was added — check the args passed to db.add
    added_sighting = db.add.call_args[0][0]
    assert added_sighting.unit_price == 1.50
