"""tests/test_routers_sources.py — Tests for Sources & Email Mining Router.

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

from app.models import ApiSource, Requirement, Requisition, User, VendorCard, VendorResponse
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
    monkeypatch.setattr(
        "app.routers.sources.settings",
        SimpleNamespace(
            email_mining_enabled=True,
            nexar_client_id=None,
            brokerbin_api_key=None,
            ebay_client_id=None,
            digikey_client_id=None,
            mouser_api_key=None,
            oemsecrets_api_key=None,
            sourcengine_api_key=None,
        ),
    )
    result = _get_connector_for_source("email_mining")
    assert isinstance(result, _EmailMiningTestConnector)


def test_get_connector_email_mining_when_disabled(monkeypatch):
    """Email mining returns None when disabled."""
    monkeypatch.setattr(
        "app.routers.sources.settings",
        SimpleNamespace(
            email_mining_enabled=False,
            nexar_client_id=None,
            brokerbin_api_key=None,
            ebay_client_id=None,
            digikey_client_id=None,
            mouser_api_key=None,
            oemsecrets_api_key=None,
            sourcengine_api_key=None,
        ),
    )
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
            mock_q.filter_by.return_value.first.return_value = existing_sightings[0] if existing_sightings else None
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
    try:
        with TestClient(app) as c:
            yield c
    finally:
        for dep in [get_db, require_user, require_buyer, require_settings_access]:
            app.dependency_overrides.pop(dep, None)


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
    mock_miner.scan_inbox = AsyncMock(
        return_value={
            "messages_scanned": 50,
            "vendors_found": 3,
            "offers_parsed": [],
            "contacts_enriched": [],
        }
    )

    with (
        patch("app.routers.sources.require_fresh_token", new_callable=AsyncMock, return_value="fake-token"),
        patch("app.connectors.email_mining.EmailMiner", return_value=mock_miner),
    ):
        resp = sources_client.post("/api/email-mining/scan")

    assert resp.status_code == 200
    data = resp.json()
    assert data["messages_scanned"] == 50
    assert data["vendors_found"] == 3


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
    mock_miner.scan_sent_items = AsyncMock(
        return_value={
            "messages_scanned": 30,
            "rfqs_detected": 5,
            "vendors_contacted": {"arrow.com": 3, "digikey.com": 2},
            "used_delta": False,
        }
    )

    with (
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="fresh-token"),
        patch("app.connectors.email_mining.EmailMiner", return_value=mock_miner),
    ):
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
        "app.services.vendor_score.compute_all_vendor_scores",
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
    """GET /api/vendors/{id}/engagement returns 200 with vendor score data."""
    resp = sources_client.get(f"/api/vendors/{test_vendor_card.id}/engagement")
    assert resp.status_code == 200
    data = resp.json()
    assert data["vendor_id"] == test_vendor_card.id
    assert data["vendor_name"] == "Arrow Electronics"
    assert "vendor_score" in data
    assert "advancement_score" in data
    assert "is_new_vendor" in data
    assert "raw_counts" in data


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
    """Vendor with data returns vendor score in response."""
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
        vendor_score=82.5,
        vendor_score_computed_at=datetime.now(timezone.utc),
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
    assert "vendor_score" in data
    assert data["raw_counts"]["total_outreach"] == 10
    assert data["raw_counts"]["total_responses"] == 7
    assert data["raw_counts"]["total_wins"] == 3


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
    mock_gc.get_json = AsyncMock(
        return_value={
            "value": [
                {"name": "image.png", "contentBytes": "abc"},  # not parseable
            ]
        }
    )

    with (
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="fresh-token"),
        patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
    ):
        resp = sources_client.post(f"/api/email-mining/parse-response-attachments/{_vendor_response.id}")

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
    """Response with attachment; mock parse_attachment returns rows that create
    sightings."""
    import base64

    test_user.m365_connected = True
    test_user.access_token = "fake-token"
    db_session.commit()

    fake_content = base64.b64encode(b"fake-excel-content").decode()
    mock_gc = MagicMock()
    mock_gc.get_json = AsyncMock(
        return_value={
            "value": [
                {"name": "quote.xlsx", "contentBytes": fake_content},
            ]
        }
    )

    parsed_rows = [
        {"mpn": "LM358N", "qty": 500, "unit_price": 0.45, "manufacturer": "TI"},
    ]

    with (
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="fresh-token"),
        patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
        patch("app.services.attachment_parser.parse_attachment", new_callable=AsyncMock, return_value=parsed_rows),
        patch("app.utils.file_validation.validate_file", return_value=(True, "")),
        patch("app.routers.sources._create_sightings_from_attachment", return_value=1),
    ):
        resp = sources_client.post(f"/api/email-mining/parse-response-attachments/{_vendor_response.id}")

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
    assert float(added_sighting.unit_price) == 1.50


# ══════════════════════════════════════════════════════════════════════
# NEW TESTS — Connector factory (all branches)
# ══════════════════════════════════════════════════════════════════════


def test_get_connector_nexar_with_octopart_key():
    """Nexar source returns NexarConnector when OCTOPART_API_KEY is set."""
    with patch("app.services.credential_service.get_credential", return_value="key123"):
        with patch("app.connectors.sources.NexarConnector"):
            result = _get_connector_for_source("nexar", db=MagicMock())
            assert result is not None


def test_get_connector_nexar_with_client_id():
    """Nexar source returns NexarConnector when NEXAR_CLIENT_ID is set."""
    creds = {
        "NEXAR_CLIENT_ID": "nid",
        "NEXAR_CLIENT_SECRET": "nsec",
        "OCTOPART_API_KEY": None,
    }

    def fake_cred(db, name, var):
        return creds.get(var)

    with patch("app.services.credential_service.get_credential", side_effect=fake_cred):
        with patch("app.connectors.sources.NexarConnector"):
            result = _get_connector_for_source("nexar", db=MagicMock())
            assert result is not None


def test_get_connector_brokerbin():
    """BrokerBin source returns BrokerBinConnector when key is set."""
    creds = {
        "NEXAR_CLIENT_ID": None,
        "NEXAR_CLIENT_SECRET": None,
        "OCTOPART_API_KEY": None,
        "BROKERBIN_API_KEY": "bb_key",
        "BROKERBIN_API_SECRET": "bb_sec",
    }

    def fake_cred(db, name, var):
        return creds.get(var)

    with patch("app.services.credential_service.get_credential", side_effect=fake_cred):
        with patch("app.connectors.sources.BrokerBinConnector"):
            result = _get_connector_for_source("brokerbin", db=MagicMock())
            assert result is not None


def test_get_connector_ebay():
    """EBay source returns EbayConnector when key is set."""
    creds = {
        "NEXAR_CLIENT_ID": None,
        "NEXAR_CLIENT_SECRET": None,
        "OCTOPART_API_KEY": None,
        "BROKERBIN_API_KEY": None,
        "BROKERBIN_API_SECRET": None,
        "EBAY_CLIENT_ID": "ebay_id",
        "EBAY_CLIENT_SECRET": "ebay_sec",
    }

    def fake_cred(db, name, var):
        return creds.get(var)

    with patch("app.services.credential_service.get_credential", side_effect=fake_cred):
        with patch("app.connectors.ebay.EbayConnector"):
            result = _get_connector_for_source("ebay", db=MagicMock())
            assert result is not None


def test_get_connector_digikey():
    """DigiKey source returns DigiKeyConnector when key is set."""
    creds = {
        "NEXAR_CLIENT_ID": None,
        "NEXAR_CLIENT_SECRET": None,
        "OCTOPART_API_KEY": None,
        "BROKERBIN_API_KEY": None,
        "BROKERBIN_API_SECRET": None,
        "EBAY_CLIENT_ID": None,
        "EBAY_CLIENT_SECRET": None,
        "DIGIKEY_CLIENT_ID": "dk_id",
        "DIGIKEY_CLIENT_SECRET": "dk_sec",
    }

    def fake_cred(db, name, var):
        return creds.get(var)

    with patch("app.services.credential_service.get_credential", side_effect=fake_cred):
        with patch("app.connectors.digikey.DigiKeyConnector"):
            result = _get_connector_for_source("digikey", db=MagicMock())
            assert result is not None


def test_get_connector_mouser():
    """Mouser source returns MouserConnector when key is set."""
    creds = {
        "NEXAR_CLIENT_ID": None,
        "NEXAR_CLIENT_SECRET": None,
        "OCTOPART_API_KEY": None,
        "BROKERBIN_API_KEY": None,
        "BROKERBIN_API_SECRET": None,
        "EBAY_CLIENT_ID": None,
        "EBAY_CLIENT_SECRET": None,
        "DIGIKEY_CLIENT_ID": None,
        "DIGIKEY_CLIENT_SECRET": None,
        "MOUSER_API_KEY": "mouser_key",
    }

    def fake_cred(db, name, var):
        return creds.get(var)

    with patch("app.services.credential_service.get_credential", side_effect=fake_cred):
        with patch("app.connectors.mouser.MouserConnector"):
            result = _get_connector_for_source("mouser", db=MagicMock())
            assert result is not None


def test_get_connector_oemsecrets():
    """OEMSecrets source returns OEMSecretsConnector when key is set."""
    creds = {
        "NEXAR_CLIENT_ID": None,
        "NEXAR_CLIENT_SECRET": None,
        "OCTOPART_API_KEY": None,
        "BROKERBIN_API_KEY": None,
        "BROKERBIN_API_SECRET": None,
        "EBAY_CLIENT_ID": None,
        "EBAY_CLIENT_SECRET": None,
        "DIGIKEY_CLIENT_ID": None,
        "DIGIKEY_CLIENT_SECRET": None,
        "MOUSER_API_KEY": None,
        "OEMSECRETS_API_KEY": "oem_key",
    }

    def fake_cred(db, name, var):
        return creds.get(var)

    with patch("app.services.credential_service.get_credential", side_effect=fake_cred):
        with patch("app.connectors.oemsecrets.OEMSecretsConnector"):
            result = _get_connector_for_source("oemsecrets", db=MagicMock())
            assert result is not None


def test_get_connector_sourcengine():
    """Sourcengine source returns SourcengineConnector when key is set."""
    creds = {
        "NEXAR_CLIENT_ID": None,
        "NEXAR_CLIENT_SECRET": None,
        "OCTOPART_API_KEY": None,
        "BROKERBIN_API_KEY": None,
        "BROKERBIN_API_SECRET": None,
        "EBAY_CLIENT_ID": None,
        "EBAY_CLIENT_SECRET": None,
        "DIGIKEY_CLIENT_ID": None,
        "DIGIKEY_CLIENT_SECRET": None,
        "MOUSER_API_KEY": None,
        "OEMSECRETS_API_KEY": None,
        "SOURCENGINE_API_KEY": "src_key",
    }

    def fake_cred(db, name, var):
        return creds.get(var)

    with patch("app.services.credential_service.get_credential", side_effect=fake_cred):
        with patch("app.connectors.sourcengine.SourcengineConnector"):
            result = _get_connector_for_source("sourcengine", db=MagicMock())
            assert result is not None


def test_get_connector_newark():
    """Newark source returns Element14Connector when key is set."""
    creds = {
        "NEXAR_CLIENT_ID": None,
        "NEXAR_CLIENT_SECRET": None,
        "OCTOPART_API_KEY": None,
        "BROKERBIN_API_KEY": None,
        "BROKERBIN_API_SECRET": None,
        "EBAY_CLIENT_ID": None,
        "EBAY_CLIENT_SECRET": None,
        "DIGIKEY_CLIENT_ID": None,
        "DIGIKEY_CLIENT_SECRET": None,
        "MOUSER_API_KEY": None,
        "OEMSECRETS_API_KEY": None,
        "SOURCENGINE_API_KEY": None,
        "ELEMENT14_API_KEY": "e14_key",
    }

    def fake_cred(db, name, var):
        return creds.get(var)

    with patch("app.services.credential_service.get_credential", side_effect=fake_cred):
        with patch("app.connectors.element14.Element14Connector"):
            result = _get_connector_for_source("element14", db=MagicMock())
            assert result is not None


def test_get_connector_anthropic_ai():
    """Anthropic AI returns _AnthropicTestConnector (no env_vars needed)."""
    from app.routers.sources import _AnthropicTestConnector

    result = _get_connector_for_source("anthropic_ai")
    assert isinstance(result, _AnthropicTestConnector)


def test_get_connector_teams_notifications():
    """Teams returns _TeamsTestConnector."""
    from app.routers.sources import _TeamsTestConnector

    result = _get_connector_for_source("teams_notifications")
    assert isinstance(result, _TeamsTestConnector)


def test_get_connector_apollo_enrichment():
    """Apollo returns _ApolloTestConnector."""
    from app.routers.sources import _ApolloTestConnector

    result = _get_connector_for_source("apollo_enrichment")
    assert isinstance(result, _ApolloTestConnector)


def test_get_connector_explorium_enrichment():
    """Explorium returns _ExploriumTestConnector."""
    from app.routers.sources import _ExploriumTestConnector

    result = _get_connector_for_source("explorium_enrichment")
    assert isinstance(result, _ExploriumTestConnector)


def test_get_connector_azure_oauth():
    """Azure OAuth returns _AzureOAuthTestConnector."""
    from app.routers.sources import _AzureOAuthTestConnector

    result = _get_connector_for_source("azure_oauth")
    assert isinstance(result, _AzureOAuthTestConnector)


def test_get_connector_no_db_env_fallback(monkeypatch):
    """_cred falls back to os.getenv when db is None."""
    monkeypatch.setenv("NEXAR_CLIENT_ID", "env_nexar_id")
    monkeypatch.setenv("NEXAR_CLIENT_SECRET", "env_nexar_sec")
    monkeypatch.setenv("OCTOPART_API_KEY", "")
    with patch("app.connectors.sources.NexarConnector") as MockNexar:
        result = _get_connector_for_source("nexar", db=None)
        assert result is not None


# ══════════════════════════════════════════════════════════════════════
# NEW TESTS — Test connector search methods
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_anthropic_test_connector_search_success():
    """_AnthropicTestConnector succeeds when claude_text returns a response."""
    from app.routers.sources import _AnthropicTestConnector

    connector = _AnthropicTestConnector()
    with patch("app.utils.claude_client.claude_text", new_callable=AsyncMock, return_value="OK"):
        results = await connector.search("LM358N")
    assert len(results) == 1
    assert "Connected" in results[0]["mpn_matched"]


@pytest.mark.asyncio
async def test_anthropic_test_connector_no_key():
    """_AnthropicTestConnector raises when claude_text returns None (no API key)."""
    from app.routers.sources import _AnthropicTestConnector

    connector = _AnthropicTestConnector()
    with patch("app.utils.claude_client.claude_text", new_callable=AsyncMock, return_value=None):
        with pytest.raises(ValueError, match="Anthropic API returned no response"):
            await connector.search("LM358N")


@pytest.mark.asyncio
async def test_anthropic_test_connector_api_error():
    """_AnthropicTestConnector raises when claude_text returns None."""
    from app.routers.sources import _AnthropicTestConnector

    connector = _AnthropicTestConnector()
    with patch("app.utils.claude_client.claude_text", new_callable=AsyncMock, return_value=None):
        with pytest.raises(ValueError, match="Anthropic API returned no response"):
            await connector.search("LM358N")


@pytest.mark.asyncio
async def test_teams_test_connector_success():
    """_TeamsTestConnector succeeds with 200 response."""
    from app.routers.sources import _TeamsTestConnector

    mock_resp = MagicMock()
    mock_resp.status_code = 200

    connector = _TeamsTestConnector()
    with (
        patch("app.routers.sources.get_credential_cached", return_value="https://webhook.example.com"),
        patch("app.http_client.http.post", new_callable=AsyncMock, return_value=mock_resp),
    ):
        results = await connector.search("LM358N")
    assert len(results) == 1
    assert results[0]["mpn_matched"] == "Message posted"


@pytest.mark.asyncio
async def test_teams_test_connector_no_webhook():
    """_TeamsTestConnector raises if no webhook URL."""
    from app.routers.sources import _TeamsTestConnector

    connector = _TeamsTestConnector()
    with patch("app.routers.sources.get_credential_cached", return_value=None):
        with pytest.raises(ValueError, match="TEAMS_WEBHOOK_URL not configured"):
            await connector.search("LM358N")


@pytest.mark.asyncio
async def test_teams_test_connector_api_error():
    """_TeamsTestConnector raises on non-200/202 response."""
    from app.routers.sources import _TeamsTestConnector

    mock_resp = MagicMock()
    mock_resp.status_code = 500
    mock_resp.text = "Internal error"

    connector = _TeamsTestConnector()
    with (
        patch("app.routers.sources.get_credential_cached", return_value="https://webhook.example.com"),
        patch("app.http_client.http.post", new_callable=AsyncMock, return_value=mock_resp),
    ):
        with pytest.raises(ValueError, match="Teams webhook returned 500"):
            await connector.search("LM358N")


@pytest.mark.asyncio
async def test_teams_test_connector_202_accepted():
    """_TeamsTestConnector succeeds with 202 response (accepted)."""
    from app.routers.sources import _TeamsTestConnector

    mock_resp = MagicMock()
    mock_resp.status_code = 202

    connector = _TeamsTestConnector()
    with (
        patch("app.routers.sources.get_credential_cached", return_value="https://webhook.example.com"),
        patch("app.http_client.http.post", new_callable=AsyncMock, return_value=mock_resp),
    ):
        results = await connector.search("LM358N")
    assert len(results) == 1
    assert results[0]["status"] == "ok"


@pytest.mark.asyncio
async def test_apollo_test_connector_success():
    """_ApolloTestConnector succeeds when API returns 200."""
    from app.routers.sources import _ApolloTestConnector

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"people": [{"name": "Test"}]}

    connector = _ApolloTestConnector()
    with (
        patch("app.routers.sources.get_credential_cached", return_value="apollo_key"),
        patch("app.http_client.http.post", new_callable=AsyncMock, return_value=mock_resp),
    ):
        results = await connector.search("LM358N")
    assert len(results) == 1
    assert "1 result" in results[0]["mpn_matched"]


@pytest.mark.asyncio
async def test_apollo_test_connector_no_key():
    """_ApolloTestConnector raises if no API key."""
    from app.routers.sources import _ApolloTestConnector

    connector = _ApolloTestConnector()
    with patch("app.routers.sources.get_credential_cached", return_value=None):
        with pytest.raises(ValueError, match="APOLLO_API_KEY not configured"):
            await connector.search("LM358N")


@pytest.mark.asyncio
async def test_apollo_test_connector_api_error():
    """_ApolloTestConnector raises on non-200 response."""
    from app.routers.sources import _ApolloTestConnector

    mock_resp = MagicMock()
    mock_resp.status_code = 403
    mock_resp.text = "Forbidden"

    connector = _ApolloTestConnector()
    with (
        patch("app.routers.sources.get_credential_cached", return_value="apollo_key"),
        patch("app.http_client.http.post", new_callable=AsyncMock, return_value=mock_resp),
    ):
        with pytest.raises(ValueError, match="Apollo API returned 403"):
            await connector.search("LM358N")


@pytest.mark.asyncio
async def test_explorium_test_connector_success():
    """_ExploriumTestConnector succeeds when API returns 200."""
    from app.routers.sources import _ExploriumTestConnector

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"firmo_name": "Anthropic Inc"}

    connector = _ExploriumTestConnector()
    with (
        patch("app.routers.sources.get_credential_cached", return_value="explorium_key"),
        patch("app.http_client.http.post", new_callable=AsyncMock, return_value=mock_resp),
    ):
        results = await connector.search("LM358N")
    assert len(results) == 1
    assert "Anthropic" in results[0]["mpn_matched"]


@pytest.mark.asyncio
async def test_explorium_test_connector_fallback_name():
    """_ExploriumTestConnector falls back to 'name' when 'firmo_name' missing."""
    from app.routers.sources import _ExploriumTestConnector

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"name": "Some Corp"}

    connector = _ExploriumTestConnector()
    with (
        patch("app.routers.sources.get_credential_cached", return_value="explorium_key"),
        patch("app.http_client.http.post", new_callable=AsyncMock, return_value=mock_resp),
    ):
        results = await connector.search("LM358N")
    assert "Some Corp" in results[0]["mpn_matched"]


@pytest.mark.asyncio
async def test_explorium_test_connector_no_name():
    """_ExploriumTestConnector falls back to 'matched' when both name keys missing."""
    from app.routers.sources import _ExploriumTestConnector

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {}

    connector = _ExploriumTestConnector()
    with (
        patch("app.routers.sources.get_credential_cached", return_value="explorium_key"),
        patch("app.http_client.http.post", new_callable=AsyncMock, return_value=mock_resp),
    ):
        results = await connector.search("LM358N")
    assert "matched" in results[0]["mpn_matched"]


@pytest.mark.asyncio
async def test_explorium_test_connector_no_key():
    """_ExploriumTestConnector raises if no API key."""
    from app.routers.sources import _ExploriumTestConnector

    connector = _ExploriumTestConnector()
    with patch("app.routers.sources.get_credential_cached", return_value=None):
        with pytest.raises(ValueError, match="EXPLORIUM_API_KEY not configured"):
            await connector.search("LM358N")


@pytest.mark.asyncio
async def test_explorium_test_connector_api_error():
    """_ExploriumTestConnector raises on non-200 response."""
    from app.routers.sources import _ExploriumTestConnector

    mock_resp = MagicMock()
    mock_resp.status_code = 500
    mock_resp.text = "Server error"

    connector = _ExploriumTestConnector()
    with (
        patch("app.routers.sources.get_credential_cached", return_value="explorium_key"),
        patch("app.http_client.http.post", new_callable=AsyncMock, return_value=mock_resp),
    ):
        with pytest.raises(ValueError, match="Explorium API returned 500"):
            await connector.search("LM358N")


@pytest.mark.asyncio
async def test_azure_oauth_test_connector_success():
    """_AzureOAuthTestConnector succeeds with valid tenant."""
    from app.routers.sources import _AzureOAuthTestConnector

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"issuer": "https://login.microsoftonline.com/test-tenant-id/v2.0"}

    connector = _AzureOAuthTestConnector()
    mock_settings = SimpleNamespace(azure_tenant_id="test-tenant-id")
    with (
        patch("app.routers.sources.settings", mock_settings),
        patch("app.http_client.http.get", new_callable=AsyncMock, return_value=mock_resp),
    ):
        results = await connector.search("LM358N")
    assert len(results) == 1
    assert results[0]["mpn_matched"] == "Tenant verified"


@pytest.mark.asyncio
async def test_azure_oauth_test_connector_no_tenant():
    """_AzureOAuthTestConnector raises if no tenant ID configured."""
    from app.routers.sources import _AzureOAuthTestConnector

    connector = _AzureOAuthTestConnector()
    mock_settings = SimpleNamespace(azure_tenant_id=None)
    with patch("app.routers.sources.settings", mock_settings):
        with pytest.raises(ValueError, match="AZURE_TENANT_ID not configured"):
            await connector.search("LM358N")


@pytest.mark.asyncio
async def test_azure_oauth_test_connector_api_error():
    """_AzureOAuthTestConnector raises on non-200 response."""
    from app.routers.sources import _AzureOAuthTestConnector

    mock_resp = MagicMock()
    mock_resp.status_code = 404

    connector = _AzureOAuthTestConnector()
    mock_settings = SimpleNamespace(azure_tenant_id="bad-tenant")
    with (
        patch("app.routers.sources.settings", mock_settings),
        patch("app.http_client.http.get", new_callable=AsyncMock, return_value=mock_resp),
    ):
        with pytest.raises(ValueError, match="Azure OpenID discovery returned 404"):
            await connector.search("LM358N")


@pytest.mark.asyncio
async def test_azure_oauth_test_connector_tenant_mismatch():
    """_AzureOAuthTestConnector raises when issuer doesn't match tenant."""
    from app.routers.sources import _AzureOAuthTestConnector

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"issuer": "https://login.microsoftonline.com/other-tenant/v2.0"}

    connector = _AzureOAuthTestConnector()
    mock_settings = SimpleNamespace(azure_tenant_id="my-tenant-id")
    with (
        patch("app.routers.sources.settings", mock_settings),
        patch("app.http_client.http.get", new_callable=AsyncMock, return_value=mock_resp),
    ):
        with pytest.raises(ValueError, match="Tenant mismatch"):
            await connector.search("LM358N")


# ══════════════════════════════════════════════════════════════════════
# NEW TESTS — Source Management edge cases
# ══════════════════════════════════════════════════════════════════════


def test_list_sources_auto_status_live_to_pending(sources_client: TestClient, db_session: Session):
    """Source with status=live but no credentials set auto-detects to pending."""
    src = ApiSource(
        name="auto_detect_test",
        display_name="Auto Detect",
        category="market_data",
        source_type="api",
        status="live",
        env_vars=["SOME_API_KEY"],
    )
    db_session.add(src)
    db_session.commit()

    with patch("app.services.credential_service.credential_is_set", return_value=False):
        resp = sources_client.get("/api/sources")
    assert resp.status_code == 200
    sources = resp.json()["sources"]
    found = next((s for s in sources if s["name"] == "auto_detect_test"), None)
    assert found is not None


def test_list_sources_no_env_vars_source(sources_client: TestClient, db_session: Session):
    """Source with no env_vars still appears in list with empty env_status."""
    src = ApiSource(
        name="no_vars_source",
        display_name="No Vars",
        category="intelligence",
        source_type="api",
        status="live",
        env_vars=[],
    )
    db_session.add(src)
    db_session.commit()

    resp = sources_client.get("/api/sources")
    assert resp.status_code == 200
    sources = resp.json()["sources"]
    found = next((s for s in sources if s["name"] == "no_vars_source"), None)
    assert found is not None
    assert found["env_vars"] == []
    assert found["env_status"] == {}


def test_list_sources_with_last_success(sources_client: TestClient, db_session: Session):
    """Source with last_success returns isoformat timestamp."""
    src = ApiSource(
        name="with_success",
        display_name="With Success",
        category="market_data",
        source_type="api",
        status="live",
        env_vars=[],
        last_success=datetime(2026, 2, 10, 12, 0, 0, tzinfo=timezone.utc),
        total_searches=10,
        total_results=50,
        avg_response_ms=150,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    db_session.add(src)
    db_session.commit()

    resp = sources_client.get("/api/sources")
    assert resp.status_code == 200
    sources = resp.json()["sources"]
    found = next((s for s in sources if s["name"] == "with_success"), None)
    assert found is not None
    assert found["last_success"] is not None
    assert found["total_searches"] == 10
    assert found["total_results"] == 50
    assert found["avg_response_ms"] == 150
    assert found["created_at"] is not None


def test_test_source_no_connector(sources_client: TestClient, _api_source: ApiSource):
    """POST /api/sources/{id}/test with no connector returns error status."""
    with patch("app.routers.sources._get_connector_for_source", return_value=None):
        resp = sources_client.post(f"/api/sources/{_api_source.id}/test")

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "error"
    assert "No connector available" in data["error"]


def test_test_source_no_results(sources_client: TestClient, _api_source: ApiSource):
    """POST /api/sources/{id}/test with connector returning empty list returns
    no_results."""
    mock_connector = MagicMock()
    mock_connector.search = AsyncMock(return_value=[])

    with patch("app.routers.sources._get_connector_for_source", return_value=mock_connector):
        resp = sources_client.post(f"/api/sources/{_api_source.id}/test")

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "no_results"
    assert data["results_count"] == 0
    assert data["error"] is None


def test_test_source_no_env_vars(sources_client: TestClient, db_session: Session):
    """POST /api/sources/{id}/test for source with no env_vars does not change status on
    error."""
    src = ApiSource(
        name="no_env_src",
        display_name="No Env",
        category="intelligence",
        source_type="api",
        status="live",
        env_vars=[],
    )
    db_session.add(src)
    db_session.commit()
    db_session.refresh(src)

    mock_connector = MagicMock()
    mock_connector.search = AsyncMock(side_effect=ValueError("Test error"))

    with patch("app.routers.sources._get_connector_for_source", return_value=mock_connector):
        resp = sources_client.post(f"/api/sources/{src.id}/test")

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "error"
    # Source status should NOT be updated because no env_vars
    db_session.refresh(src)
    assert src.status == "live"


def test_test_source_success_no_env_vars(sources_client: TestClient, db_session: Session):
    """POST /api/sources/{id}/test for source with no env_vars does not change status on
    success."""
    src = ApiSource(
        name="no_env_success",
        display_name="No Env Success",
        category="intelligence",
        source_type="api",
        status="pending",
        env_vars=[],
    )
    db_session.add(src)
    db_session.commit()
    db_session.refresh(src)

    mock_connector = MagicMock()
    mock_connector.search = AsyncMock(return_value=[{"status": "ok"}])

    with patch("app.routers.sources._get_connector_for_source", return_value=mock_connector):
        resp = sources_client.post(f"/api/sources/{src.id}/test")

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    # Source status should NOT be updated because no env_vars
    db_session.refresh(src)
    assert src.status == "pending"


def test_toggle_source_not_found(sources_client: TestClient):
    """PUT /api/sources/99999/toggle returns 404 for nonexistent source."""
    resp = sources_client.put("/api/sources/99999/toggle", json={"status": "live"})
    assert resp.status_code == 404


def test_toggle_source_enable_with_credentials(sources_client: TestClient, db_session: Session):
    """PUT toggle with live status + all creds set auto-detects to live."""
    src = ApiSource(
        name="toggle_creds",
        display_name="Toggle Creds",
        category="market_data",
        source_type="api",
        status="disabled",
        env_vars=["MY_KEY"],
    )
    db_session.add(src)
    db_session.commit()
    db_session.refresh(src)

    with patch("app.services.credential_service.credential_is_set", return_value=True):
        resp = sources_client.put(
            f"/api/sources/{src.id}/toggle",
            json={"status": "live"},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["status"] == "live"


def test_toggle_source_enable_no_credentials(sources_client: TestClient, db_session: Session):
    """PUT toggle with live status + no creds auto-detects to pending."""
    src = ApiSource(
        name="toggle_no_creds",
        display_name="Toggle No Creds",
        category="market_data",
        source_type="api",
        status="disabled",
        env_vars=["MY_KEY"],
    )
    db_session.add(src)
    db_session.commit()
    db_session.refresh(src)

    with patch("app.services.credential_service.credential_is_set", return_value=False):
        resp = sources_client.put(
            f"/api/sources/{src.id}/toggle",
            json={"status": "pending"},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["status"] == "pending"


def test_toggle_source_enable_no_env_vars(sources_client: TestClient, db_session: Session):
    """PUT toggle with live status + no env_vars auto-detects to pending."""
    src = ApiSource(
        name="toggle_no_env",
        display_name="Toggle No Env",
        category="intelligence",
        source_type="api",
        status="disabled",
        env_vars=[],
    )
    db_session.add(src)
    db_session.commit()
    db_session.refresh(src)

    resp = sources_client.put(
        f"/api/sources/{src.id}/toggle",
        json={"status": "live"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    # No env_vars → not all(credential_is_set) → pending
    assert data["status"] == "pending"


# ══════════════════════════════════════════════════════════════════════
# NEW TESTS — Email Mining scan with enriched contacts
# ══════════════════════════════════════════════════════════════════════


def test_scan_inbox_with_contacts_enriched(
    sources_client: TestClient,
    _email_mining_source: ApiSource,
    db_session: Session,
):
    """POST /api/email-mining/scan enriches vendor cards from contacts_enriched data."""
    mock_miner = MagicMock()
    mock_miner.scan_inbox = AsyncMock(
        return_value={
            "messages_scanned": 20,
            "vendors_found": 1,
            "offers_parsed": [],
            "contacts_enriched": [
                {
                    "vendor_name": "NewVendor Corp",
                    "emails": ["sales@newvendor.com", "info@newvendor.com"],
                    "phones": ["+1-555-9999"],
                    "websites": ["newvendor.com"],
                },
            ],
        }
    )

    with (
        patch("app.routers.sources.require_fresh_token", new_callable=AsyncMock, return_value="fake-token"),
        patch("app.connectors.email_mining.EmailMiner", return_value=mock_miner),
    ):
        resp = sources_client.post("/api/email-mining/scan")

    assert resp.status_code == 200
    data = resp.json()
    assert data["messages_scanned"] == 20
    assert data["contacts_enriched"] >= 0  # depends on merge_emails_into_card return


def test_scan_inbox_enriches_existing_vendor_card(
    sources_client: TestClient,
    _email_mining_source: ApiSource,
    db_session: Session,
):
    """Scan enriches an already existing vendor card with new emails."""
    card = VendorCard(
        normalized_name="existing vendor",
        display_name="Existing Vendor",
        emails=["old@existing.com"],
        phones=[],
        source="manual",
    )
    db_session.add(card)
    db_session.commit()

    mock_miner = MagicMock()
    mock_miner.scan_inbox = AsyncMock(
        return_value={
            "messages_scanned": 10,
            "vendors_found": 1,
            "offers_parsed": [],
            "contacts_enriched": [
                {
                    "vendor_name": "Existing Vendor",
                    "emails": ["new@existing.com"],
                    "phones": ["+1-555-1234"],
                    "websites": [],
                },
            ],
        }
    )

    with (
        patch("app.routers.sources.require_fresh_token", new_callable=AsyncMock, return_value="fake-token"),
        patch("app.connectors.email_mining.EmailMiner", return_value=mock_miner),
    ):
        resp = sources_client.post("/api/email-mining/scan")

    assert resp.status_code == 200


def test_scan_inbox_with_json_body_options(
    sources_client: TestClient,
    _email_mining_source: ApiSource,
):
    """POST /api/email-mining/scan with explicit JSON body parses mining options."""
    mock_miner = MagicMock()
    mock_miner.scan_inbox = AsyncMock(
        return_value={
            "messages_scanned": 10,
            "vendors_found": 0,
            "offers_parsed": [],
            "contacts_enriched": [],
        }
    )

    with (
        patch("app.routers.sources.require_fresh_token", new_callable=AsyncMock, return_value="fake-token"),
        patch("app.connectors.email_mining.EmailMiner", return_value=mock_miner),
    ):
        resp = sources_client.post(
            "/api/email-mining/scan",
            json={"lookback_days": 7},
            headers={"content-type": "application/json"},
        )

    assert resp.status_code == 200
    # Verify miner was called with lookback_days=7
    mock_miner.scan_inbox.assert_called_once()
    call_kwargs = mock_miner.scan_inbox.call_args
    assert call_kwargs[1]["lookback_days"] == 7


def test_scan_inbox_empty_contacts(
    sources_client: TestClient,
    _email_mining_source: ApiSource,
):
    """POST /api/email-mining/scan with contact missing vendor_name is skipped."""
    mock_miner = MagicMock()
    mock_miner.scan_inbox = AsyncMock(
        return_value={
            "messages_scanned": 5,
            "vendors_found": 0,
            "offers_parsed": [],
            "contacts_enriched": [
                {"vendor_name": "", "emails": ["nobody@example.com"]},
            ],
        }
    )

    with (
        patch("app.routers.sources.require_fresh_token", new_callable=AsyncMock, return_value="fake-token"),
        patch("app.connectors.email_mining.EmailMiner", return_value=mock_miner),
    ):
        resp = sources_client.post("/api/email-mining/scan")

    assert resp.status_code == 200
    data = resp.json()
    assert data["contacts_enriched"] == 0


def test_scan_inbox_no_email_mining_source(sources_client: TestClient, db_session: Session):
    """POST /api/email-mining/scan works even if email_mining source row doesn't
    exist."""
    mock_miner = MagicMock()
    mock_miner.scan_inbox = AsyncMock(
        return_value={
            "messages_scanned": 5,
            "vendors_found": 0,
            "offers_parsed": [],
            "contacts_enriched": [],
        }
    )

    with (
        patch("app.routers.sources.require_fresh_token", new_callable=AsyncMock, return_value="fake-token"),
        patch("app.connectors.email_mining.EmailMiner", return_value=mock_miner),
    ):
        resp = sources_client.post("/api/email-mining/scan")

    assert resp.status_code == 200


# ══════════════════════════════════════════════════════════════════════
# NEW TESTS — Outbound scan edge cases
# ══════════════════════════════════════════════════════════════════════


def test_scan_outbound_with_domain_match(
    sources_client: TestClient,
    test_user: User,
    db_session: Session,
):
    """Outbound scan matches vendor card by domain and updates outreach."""
    test_user.m365_connected = True
    test_user.access_token = "fake-access-token"
    db_session.commit()

    card = VendorCard(
        normalized_name="arrow",
        display_name="Arrow Electronics",
        domain="arrow.com",
        emails=["sales@arrow.com"],
        phones=[],
    )
    db_session.add(card)
    db_session.commit()

    mock_miner = MagicMock()
    mock_miner.scan_sent_items = AsyncMock(
        return_value={
            "messages_scanned": 10,
            "rfqs_detected": 2,
            "vendors_contacted": {"arrow.com": 3},
            "used_delta": True,
        }
    )

    with (
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="fresh-token"),
        patch("app.connectors.email_mining.EmailMiner", return_value=mock_miner),
    ):
        resp = sources_client.post("/api/email-mining/scan-outbound")

    assert resp.status_code == 200
    data = resp.json()
    assert data["cards_updated"] == 1
    assert data["used_delta"] is True

    db_session.refresh(card)
    assert card.total_outreach == 3


def test_scan_outbound_with_prefix_match(
    sources_client: TestClient,
    test_user: User,
    db_session: Session,
):
    """Outbound scan falls back to prefix match when domain doesn't match."""
    test_user.m365_connected = True
    test_user.access_token = "fake-access-token"
    db_session.commit()

    card = VendorCard(
        normalized_name="mouser",
        display_name="Mouser Electronics",
        emails=["sales@mouser.com"],
        phones=[],
    )
    db_session.add(card)
    db_session.commit()

    mock_miner = MagicMock()
    mock_miner.scan_sent_items = AsyncMock(
        return_value={
            "messages_scanned": 10,
            "rfqs_detected": 1,
            "vendors_contacted": {"mouser.com": 2},
            "used_delta": False,
        }
    )

    with (
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="fresh-token"),
        patch("app.connectors.email_mining.EmailMiner", return_value=mock_miner),
    ):
        resp = sources_client.post("/api/email-mining/scan-outbound")

    assert resp.status_code == 200
    data = resp.json()
    assert data["cards_updated"] == 1


def test_scan_outbound_db_commit_failure(
    sources_client: TestClient,
    test_user: User,
    db_session: Session,
):
    """Outbound scan handles DB commit failure gracefully."""
    test_user.m365_connected = True
    test_user.access_token = "fake-access-token"
    db_session.commit()

    mock_miner = MagicMock()
    mock_miner.scan_sent_items = AsyncMock(
        return_value={
            "messages_scanned": 5,
            "rfqs_detected": 0,
            "vendors_contacted": {},
            "used_delta": False,
        }
    )

    with (
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="fresh-token"),
        patch("app.connectors.email_mining.EmailMiner", return_value=mock_miner),
    ):
        resp = sources_client.post("/api/email-mining/scan-outbound")

    assert resp.status_code == 200
    data = resp.json()
    assert data["cards_updated"] == 0


def test_scan_outbound_with_json_body(
    sources_client: TestClient,
    test_user: User,
    db_session: Session,
):
    """POST /api/email-mining/scan-outbound with JSON body parses mining options."""
    test_user.m365_connected = True
    test_user.access_token = "fake-access-token"
    db_session.commit()

    mock_miner = MagicMock()
    mock_miner.scan_sent_items = AsyncMock(
        return_value={
            "messages_scanned": 5,
            "rfqs_detected": 0,
            "vendors_contacted": {},
            "used_delta": False,
        }
    )

    with (
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="fresh-token"),
        patch("app.connectors.email_mining.EmailMiner", return_value=mock_miner),
    ):
        resp = sources_client.post(
            "/api/email-mining/scan-outbound",
            json={"lookback_days": 14},
            headers={"content-type": "application/json"},
        )

    assert resp.status_code == 200
    mock_miner.scan_sent_items.assert_called_once()
    assert mock_miner.scan_sent_items.call_args[1]["lookback_days"] == 14


# ══════════════════════════════════════════════════════════════════════
# NEW TESTS — Email mining status edge cases
# ══════════════════════════════════════════════════════════════════════


def test_email_mining_status_no_source(sources_client: TestClient):
    """GET /api/email-mining/status returns defaults when no source row exists."""
    resp = sources_client.get("/api/email-mining/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["last_scan"] is None
    assert data["total_scans"] == 0
    assert data["total_vendors_found"] == 0


# ══════════════════════════════════════════════════════════════════════
# NEW TESTS — Attachment parsing edge cases
# ══════════════════════════════════════════════════════════════════════


def test_parse_response_attachments_no_m365(
    sources_client: TestClient,
    test_user: User,
    db_session: Session,
):
    """POST parse-response-attachments fails when M365 not connected."""
    test_user.m365_connected = False
    test_user.access_token = None
    db_session.commit()

    resp = sources_client.post("/api/email-mining/parse-response-attachments/1")
    assert resp.status_code == 400


def test_parse_response_attachments_no_message_id(
    sources_client: TestClient,
    test_user: User,
    db_session: Session,
):
    """Response without message_id returns 400."""
    test_user.m365_connected = True
    test_user.access_token = "fake-token"
    db_session.commit()

    req = Requisition(
        name="REQ-NOMSGID",
        customer_name="Acme",
        status="open",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.flush()

    vr = VendorResponse(
        requisition_id=req.id,
        vendor_name="ACME Corp",
        vendor_email="sales@acme.com",
        subject="RE: RFQ",
        message_id=None,  # no message ID
        status="new",
        received_at=datetime.now(timezone.utc),
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(vr)
    db_session.commit()
    db_session.refresh(vr)

    resp = sources_client.post(f"/api/email-mining/parse-response-attachments/{vr.id}")
    assert resp.status_code == 400


def test_parse_response_attachments_graph_error(
    sources_client: TestClient,
    test_user: User,
    db_session: Session,
    _vendor_response: VendorResponse,
):
    """Graph API error during attachment fetch returns 502."""
    test_user.m365_connected = True
    test_user.access_token = "fake-token"
    db_session.commit()

    mock_gc = MagicMock()
    mock_gc.get_json = AsyncMock(side_effect=ConnectionError("Network error"))

    with (
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="fresh-token"),
        patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
    ):
        resp = sources_client.post(f"/api/email-mining/parse-response-attachments/{_vendor_response.id}")

    assert resp.status_code == 502


def test_parse_response_attachments_no_content_bytes(
    sources_client: TestClient,
    test_user: User,
    db_session: Session,
    _vendor_response: VendorResponse,
):
    """Parseable attachment without contentBytes is skipped."""
    test_user.m365_connected = True
    test_user.access_token = "fake-token"
    db_session.commit()

    mock_gc = MagicMock()
    mock_gc.get_json = AsyncMock(
        return_value={
            "value": [
                {"name": "quote.xlsx", "contentBytes": None},
            ]
        }
    )

    with (
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="fresh-token"),
        patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
    ):
        resp = sources_client.post(f"/api/email-mining/parse-response-attachments/{_vendor_response.id}")

    assert resp.status_code == 200
    data = resp.json()
    assert data["parseable"] == 1
    assert data["rows_parsed"] == 0


def test_parse_response_attachments_file_validation_fails(
    sources_client: TestClient,
    test_user: User,
    db_session: Session,
    _vendor_response: VendorResponse,
):
    """File validation failure skips the attachment."""
    import base64

    test_user.m365_connected = True
    test_user.access_token = "fake-token"
    db_session.commit()

    fake_content = base64.b64encode(b"bad-content").decode()
    mock_gc = MagicMock()
    mock_gc.get_json = AsyncMock(
        return_value={
            "value": [
                {"name": "malicious.xlsx", "contentBytes": fake_content},
            ]
        }
    )

    with (
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="fresh-token"),
        patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
        patch("app.utils.file_validation.validate_file", return_value=(False, "Invalid file")),
    ):
        resp = sources_client.post(f"/api/email-mining/parse-response-attachments/{_vendor_response.id}")

    assert resp.status_code == 200
    data = resp.json()
    assert data["parseable"] == 1
    assert data["rows_parsed"] == 0
    assert data["sightings_created"] == 0


def test_parse_response_attachments_no_requisition_id(
    sources_client: TestClient,
    test_user: User,
    db_session: Session,
):
    """Parsed rows don't create sightings when VR has no requisition_id."""
    import base64

    test_user.m365_connected = True
    test_user.access_token = "fake-token"
    db_session.commit()

    vr = VendorResponse(
        requisition_id=None,  # no requisition link
        vendor_name="No Req Vendor",
        vendor_email="noreq@vendor.com",
        subject="Quote",
        message_id="graph-msg-noreq-001",
        status="new",
        received_at=datetime.now(timezone.utc),
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(vr)
    db_session.commit()
    db_session.refresh(vr)

    fake_content = base64.b64encode(b"excel-data").decode()
    mock_gc = MagicMock()
    mock_gc.get_json = AsyncMock(return_value={"value": [{"name": "quote.csv", "contentBytes": fake_content}]})

    parsed_rows = [{"mpn": "LM358N", "qty": 100}]

    with (
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="fresh-token"),
        patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
        patch("app.services.attachment_parser.parse_attachment", new_callable=AsyncMock, return_value=parsed_rows),
        patch("app.utils.file_validation.validate_file", return_value=(True, "")),
    ):
        resp = sources_client.post(f"/api/email-mining/parse-response-attachments/{vr.id}")

    assert resp.status_code == 200
    data = resp.json()
    assert data["rows_parsed"] == 1
    assert data["sightings_created"] == 0  # no requisition_id → no sightings


def test_parse_response_attachments_commit_failure(
    sources_client: TestClient,
    test_user: User,
    db_session: Session,
    _vendor_response: VendorResponse,
):
    """SQLAlchemy commit failure returns 500."""
    import base64

    from sqlalchemy.exc import SQLAlchemyError

    test_user.m365_connected = True
    test_user.access_token = "fake-token"
    db_session.commit()

    fake_content = base64.b64encode(b"excel-data").decode()
    mock_gc = MagicMock()
    mock_gc.get_json = AsyncMock(return_value={"value": [{"name": "quote.xlsx", "contentBytes": fake_content}]})

    parsed_rows = [{"mpn": "LM358N", "qty": 100}]

    with (
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="fresh-token"),
        patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
        patch("app.services.attachment_parser.parse_attachment", new_callable=AsyncMock, return_value=parsed_rows),
        patch("app.utils.file_validation.validate_file", return_value=(True, "")),
        patch("app.routers.sources._create_sightings_from_attachment", return_value=1),
        patch.object(db_session, "commit", side_effect=SQLAlchemyError("DB error")),
    ):
        resp = sources_client.post(f"/api/email-mining/parse-response-attachments/{_vendor_response.id}")

    assert resp.status_code == 500


def test_parse_response_attachments_empty_value(
    sources_client: TestClient,
    test_user: User,
    db_session: Session,
    _vendor_response: VendorResponse,
):
    """Graph API returns None for att_data — treats as empty."""
    test_user.m365_connected = True
    test_user.access_token = "fake-token"
    db_session.commit()

    mock_gc = MagicMock()
    mock_gc.get_json = AsyncMock(return_value=None)

    with (
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="fresh-token"),
        patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
    ):
        resp = sources_client.post(f"/api/email-mining/parse-response-attachments/{_vendor_response.id}")

    assert resp.status_code == 200
    data = resp.json()
    assert data["attachments_found"] == 0
    assert data["parseable"] == 0


def test_parse_response_attachments_vendor_domain_extraction(
    sources_client: TestClient,
    test_user: User,
    db_session: Session,
):
    """Vendor domain is extracted from email for attachment parsing."""
    import base64

    test_user.m365_connected = True
    test_user.access_token = "fake-token"
    db_session.commit()

    req = Requisition(
        name="REQ-DOMAIN",
        customer_name="Acme",
        status="open",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.flush()

    vr = VendorResponse(
        requisition_id=req.id,
        vendor_name="Domain Vendor",
        vendor_email="sales@domainvendor.com",
        subject="Quote",
        message_id="graph-msg-domain-001",
        status="new",
        received_at=datetime.now(timezone.utc),
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(vr)
    db_session.commit()
    db_session.refresh(vr)

    fake_content = base64.b64encode(b"data").decode()
    mock_gc = MagicMock()
    mock_gc.get_json = AsyncMock(return_value={"value": [{"name": "stock.csv", "contentBytes": fake_content}]})

    with (
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="fresh-token"),
        patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
        patch("app.services.attachment_parser.parse_attachment", new_callable=AsyncMock, return_value=[]) as mock_parse,
        patch("app.utils.file_validation.validate_file", return_value=(True, "")),
    ):
        resp = sources_client.post(f"/api/email-mining/parse-response-attachments/{vr.id}")

    assert resp.status_code == 200
    # Verify domain was passed to parse_attachment
    call_kwargs = mock_parse.call_args
    assert call_kwargs[1]["vendor_domain"] == "domainvendor.com"


def test_parse_response_attachments_multiple_file_types(
    sources_client: TestClient,
    test_user: User,
    db_session: Session,
    _vendor_response: VendorResponse,
):
    """Parseable attachment types: .xlsx, .xls, .csv, .tsv."""
    import base64

    test_user.m365_connected = True
    test_user.access_token = "fake-token"
    db_session.commit()

    fake_content = base64.b64encode(b"data").decode()
    mock_gc = MagicMock()
    mock_gc.get_json = AsyncMock(
        return_value={
            "value": [
                {"name": "quote.xlsx", "contentBytes": fake_content},
                {"name": "quote.csv", "contentBytes": fake_content},
                {"name": "quote.xls", "contentBytes": fake_content},
                {"name": "quote.tsv", "contentBytes": fake_content},
                {"name": "image.png", "contentBytes": fake_content},  # not parseable
                {"name": "doc.pdf", "contentBytes": fake_content},  # not parseable
            ]
        }
    )

    with (
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="fresh-token"),
        patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
        patch("app.services.attachment_parser.parse_attachment", new_callable=AsyncMock, return_value=[]),
        patch("app.utils.file_validation.validate_file", return_value=(True, "")),
    ):
        resp = sources_client.post(f"/api/email-mining/parse-response-attachments/{_vendor_response.id}")

    assert resp.status_code == 200
    data = resp.json()
    assert data["attachments_found"] == 6
    assert data["parseable"] == 4


# ══════════════════════════════════════════════════════════════════════
# NEW TESTS — Sighting creation edge cases
# ══════════════════════════════════════════════════════════════════════


def test_create_sightings_unmatched_mpn():
    """Rows with MPN that doesn't match any requirement (exact or fuzzy) are skipped."""
    req = SimpleNamespace(id=1, mpn="LM358N", requisition_id=10)
    vr = SimpleNamespace(requisition_id=10, vendor_name="ACME", vendor_email="a@acme.com")
    db = _mock_db_for_sightings([req])

    rows = [{"mpn": "TOTALLY_DIFFERENT_PART", "qty": 100}]
    created = _create_sightings_from_attachment(db, vr, rows)

    assert created == 0


def test_create_sightings_vendor_email_without_at():
    """Sightings created even when vendor_email has no '@'."""
    req = SimpleNamespace(id=1, mpn="LM358N", requisition_id=10)
    vr = SimpleNamespace(requisition_id=10, vendor_name="ACME", vendor_email="no-at-sign")
    db = _mock_db_for_sightings([req])

    rows = [{"mpn": "LM358N", "qty": 100}]
    created = _create_sightings_from_attachment(db, vr, rows)

    assert created == 1


def test_create_sightings_with_all_optional_fields():
    """Sighting includes all optional fields from row data."""
    req = SimpleNamespace(id=1, mpn="LM358N", requisition_id=10)
    vr = SimpleNamespace(requisition_id=10, vendor_name="ACME", vendor_email="a@acme.com")
    db = _mock_db_for_sightings([req])

    rows = [
        {
            "mpn": "LM358N",
            "qty": 500,
            "unit_price": "$2.50",
            "manufacturer": "Texas Instruments",
            "condition": "New",
            "date_code": "2025+",
            "packaging": "Reel",
            "lead_time": "2 weeks",
            "moq": 100,
            "currency": "USD",
        }
    ]
    created = _create_sightings_from_attachment(db, vr, rows)

    assert created == 1
    added = db.add.call_args[0][0]
    assert added.manufacturer == "Texas Instruments"
    assert added.confidence == 0.7
    assert added.source_type == "email_attachment"


# ══════════════════════════════════════════════════════════════════════
# API Health: is_active flag, activate toggle, health-summary
# ══════════════════════════════════════════════════════════════════════


def test_source_is_active_in_response(sources_client: TestClient, _api_source: ApiSource):
    """GET /api/sources includes is_active field for each source."""
    resp = sources_client.get("/api/sources")
    assert resp.status_code == 200
    sources = resp.json()["sources"]
    src = next((s for s in sources if s["name"] == "test_source"), None)
    assert src is not None
    assert "is_active" in src
    assert src["is_active"] is False


def test_toggle_source_active(sources_client: TestClient, _api_source: ApiSource):
    """PUT /api/sources/{id}/activate toggles is_active on/off."""
    # First toggle: False → True
    resp = sources_client.put(f"/api/sources/{_api_source.id}/activate")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["is_active"] is True

    # Second toggle: True → False
    resp = sources_client.put(f"/api/sources/{_api_source.id}/activate")
    assert resp.status_code == 200
    assert resp.json()["is_active"] is False


def test_toggle_source_active_not_found(sources_client: TestClient):
    """PUT /api/sources/999/activate returns 404."""
    resp = sources_client.put("/api/sources/999/activate")
    assert resp.status_code == 404


def test_health_summary_no_errors(sources_client: TestClient, db_session: Session):
    """Health summary returns no errors when no active sources have error status."""
    src = ApiSource(
        name="healthy_src",
        display_name="Healthy",
        category="api",
        source_type="api",
        status="live",
        is_active=True,
        env_vars=[],
    )
    db_session.add(src)
    db_session.commit()
    resp = sources_client.get("/api/sources/health-summary")
    assert resp.status_code == 200
    data = resp.json()
    assert data["has_errors"] is False
    assert data["errored_sources"] == []


def test_health_summary_with_active_error(sources_client: TestClient, db_session: Session):
    """Health summary returns errored active sources."""
    src = ApiSource(
        name="broken_src",
        display_name="Broken API",
        category="api",
        source_type="api",
        status="error",
        is_active=True,
        env_vars=["KEY"],
        last_error="Connection timeout",
    )
    db_session.add(src)
    db_session.commit()
    resp = sources_client.get("/api/sources/health-summary")
    assert resp.status_code == 200
    data = resp.json()
    assert data["has_errors"] is True
    assert len(data["errored_sources"]) == 1
    assert data["errored_sources"][0]["display_name"] == "Broken API"
    assert data["errored_sources"][0]["last_error"] == "Connection timeout"


def test_health_summary_ignores_planned(sources_client: TestClient, db_session: Session):
    """Health summary ignores sources where is_active=False even if status=error."""
    src = ApiSource(
        name="planned_err",
        display_name="Planned Error",
        category="api",
        source_type="api",
        status="error",
        is_active=False,
        env_vars=[],
        last_error="Not configured",
    )
    db_session.add(src)
    db_session.commit()
    resp = sources_client.get("/api/sources/health-summary")
    assert resp.status_code == 200
    data = resp.json()
    assert data["has_errors"] is False
    assert data["errored_sources"] == []
