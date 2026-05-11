import os

os.environ["TESTING"] = "1"
"""test_sources_router_coverage.py — Coverage for app/routers/sources.py.

Targets uncovered lines: _get_connector_for_source connector branches,
_create_sightings_from_attachment, _AnthropicTestConnector, _TeamsTestConnector,
_EmailMiningTestConnector, vendor engagement endpoint, outbound scan with vendor
domain matching, toggle with live credentials, list_sources with last_error_at.

Called by: pytest
Depends on: app/routers/sources.py, conftest fixtures
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import ApiSource, Requisition, User, VendorCard, VendorResponse
from app.rate_limit import limiter
from tests.conftest import engine

_ = engine


# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def src_client(db_session: Session, test_user: User) -> TestClient:
    """TestClient with all auth overrides and limiter reset."""
    from app.database import get_db
    from app.dependencies import require_buyer, require_fresh_token, require_settings_access, require_user
    from app.main import app

    def _db():
        yield db_session

    def _user():
        return test_user

    async def _token():
        return "mock-token"

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[require_user] = _user
    app.dependency_overrides[require_buyer] = _user
    app.dependency_overrides[require_settings_access] = _user
    app.dependency_overrides[require_fresh_token] = _token

    limiter.reset()
    try:
        with TestClient(app) as c:
            yield c
    finally:
        for dep in [get_db, require_user, require_buyer, require_settings_access, require_fresh_token]:
            app.dependency_overrides.pop(dep, None)


@pytest.fixture()
def api_source(db_session: Session) -> ApiSource:
    src = ApiSource(
        name="nexar",
        display_name="Nexar",
        category="market_data",
        source_type="api",
        status="live",
        description="Nexar parts search",
        env_vars=["NEXAR_CLIENT_ID", "NEXAR_CLIENT_SECRET"],
        is_active=True,
    )
    db_session.add(src)
    db_session.commit()
    db_session.refresh(src)
    return src


# ---------------------------------------------------------------------------
# _get_connector_for_source — connector branches via env vars
# ---------------------------------------------------------------------------


class TestGetConnectorForSource:
    def test_nexar_returns_connector_with_octopart_key(self, monkeypatch):
        monkeypatch.setenv("OCTOPART_API_KEY", "octo-key")
        from app.routers.sources import _get_connector_for_source

        result = _get_connector_for_source("nexar")
        assert result is not None

    def test_brokerbin_returns_connector_with_key(self, monkeypatch):
        monkeypatch.setenv("BROKERBIN_API_KEY", "bb-key")
        from app.routers.sources import _get_connector_for_source

        result = _get_connector_for_source("brokerbin")
        assert result is not None

    def test_ebay_returns_connector_with_key(self, monkeypatch):
        monkeypatch.setenv("EBAY_CLIENT_ID", "ebay-id")
        monkeypatch.setenv("EBAY_CLIENT_SECRET", "ebay-sec")
        from app.routers.sources import _get_connector_for_source

        result = _get_connector_for_source("ebay")
        assert result is not None

    def test_digikey_returns_connector_with_key(self, monkeypatch):
        monkeypatch.setenv("DIGIKEY_CLIENT_ID", "dk-id")
        monkeypatch.setenv("DIGIKEY_CLIENT_SECRET", "dk-sec")
        from app.routers.sources import _get_connector_for_source

        result = _get_connector_for_source("digikey")
        assert result is not None

    def test_mouser_returns_connector_with_key(self, monkeypatch):
        monkeypatch.setenv("MOUSER_API_KEY", "mouser-key")
        from app.routers.sources import _get_connector_for_source

        result = _get_connector_for_source("mouser")
        assert result is not None

    def test_oemsecrets_returns_connector_with_key(self, monkeypatch):
        monkeypatch.setenv("OEMSECRETS_API_KEY", "oem-key")
        from app.routers.sources import _get_connector_for_source

        result = _get_connector_for_source("oemsecrets")
        assert result is not None

    def test_sourcengine_returns_connector_with_key(self, monkeypatch):
        monkeypatch.setenv("SOURCENGINE_API_KEY", "src-key")
        from app.routers.sources import _get_connector_for_source

        result = _get_connector_for_source("sourcengine")
        assert result is not None

    def test_element14_returns_connector_with_key(self, monkeypatch):
        monkeypatch.setenv("ELEMENT14_API_KEY", "e14-key")
        from app.routers.sources import _get_connector_for_source

        result = _get_connector_for_source("element14")
        assert result is not None

    def test_email_mining_returns_connector_when_enabled(self):
        from app.routers.sources import _EmailMiningTestConnector, _get_connector_for_source

        with patch("app.routers.sources.settings") as mock_settings:
            mock_settings.email_mining_enabled = True
            result = _get_connector_for_source("email_mining")
        assert isinstance(result, _EmailMiningTestConnector)

    def test_email_mining_returns_none_when_disabled(self):
        from app.routers.sources import _get_connector_for_source

        with patch("app.routers.sources.settings") as mock_settings:
            mock_settings.email_mining_enabled = False
            result = _get_connector_for_source("email_mining")
        assert result is None

    def test_anthropic_ai_returns_connector(self):
        from app.routers.sources import _AnthropicTestConnector, _get_connector_for_source

        result = _get_connector_for_source("anthropic_ai")
        assert isinstance(result, _AnthropicTestConnector)

    def test_teams_notifications_returns_connector(self):
        from app.routers.sources import _get_connector_for_source, _TeamsTestConnector

        result = _get_connector_for_source("teams_notifications")
        assert isinstance(result, _TeamsTestConnector)

    def test_apollo_enrichment_returns_connector(self):
        from app.routers.sources import _ApolloTestConnector, _get_connector_for_source

        result = _get_connector_for_source("apollo_enrichment")
        assert isinstance(result, _ApolloTestConnector)

    def test_explorium_enrichment_returns_connector(self):
        from app.routers.sources import _ExploriumTestConnector, _get_connector_for_source

        result = _get_connector_for_source("explorium_enrichment")
        assert isinstance(result, _ExploriumTestConnector)

    def test_azure_oauth_returns_connector(self):
        from app.routers.sources import _AzureOAuthTestConnector, _get_connector_for_source

        result = _get_connector_for_source("azure_oauth")
        assert isinstance(result, _AzureOAuthTestConnector)

    def test_unknown_source_returns_none(self):
        from app.routers.sources import _get_connector_for_source

        result = _get_connector_for_source("no_such_source")
        assert result is None

    def test_uses_db_credential_when_db_provided(self, db_session: Session):
        from app.routers.sources import _get_connector_for_source

        with patch("app.services.credential_service.get_credential", return_value="nexar-id-from-db"):
            result = _get_connector_for_source("nexar", db=db_session)
        assert result is not None

    def test_nexar_returns_none_without_keys(self, monkeypatch):
        monkeypatch.delenv("NEXAR_CLIENT_ID", raising=False)
        monkeypatch.delenv("OCTOPART_API_KEY", raising=False)
        from app.routers.sources import _get_connector_for_source

        result = _get_connector_for_source("nexar")
        assert result is None


# ---------------------------------------------------------------------------
# _EmailMiningTestConnector
# ---------------------------------------------------------------------------


class TestEmailMiningTestConnector:
    @pytest.mark.asyncio
    async def test_search_returns_status_ok(self):
        from app.routers.sources import _EmailMiningTestConnector

        connector = _EmailMiningTestConnector()
        results = await connector.search("LM358N")
        assert len(results) == 1
        assert results[0]["status"] == "ok"
        assert "Email Mining Active" in results[0]["vendor_name"]


# ---------------------------------------------------------------------------
# _AnthropicTestConnector
# ---------------------------------------------------------------------------


class TestAnthropicTestConnector:
    @pytest.mark.asyncio
    async def test_search_success(self):
        from app.routers.sources import _AnthropicTestConnector

        with patch("app.utils.claude_client.claude_text", new_callable=AsyncMock, return_value="OK"):
            connector = _AnthropicTestConnector()
            results = await connector.search("LM358N")
        assert len(results) == 1
        assert results[0]["status"] == "ok"
        assert "Anthropic AI" in results[0]["vendor_name"]

    @pytest.mark.asyncio
    async def test_search_raises_on_none_response(self):
        from app.routers.sources import _AnthropicTestConnector

        with patch("app.utils.claude_client.claude_text", new_callable=AsyncMock, return_value=None):
            connector = _AnthropicTestConnector()
            with pytest.raises(ValueError, match="no response"):
                await connector.search("LM358N")


# ---------------------------------------------------------------------------
# _TeamsTestConnector
# ---------------------------------------------------------------------------


class TestTeamsTestConnector:
    @pytest.mark.asyncio
    async def test_search_success(self):
        from app.routers.sources import _TeamsTestConnector

        mock_resp = MagicMock()
        mock_resp.status_code = 200

        with (
            patch("app.routers.sources.get_credential_cached", return_value="https://webhook.office.com/test"),
            patch("app.http_client.http.post", new_callable=AsyncMock, return_value=mock_resp),
        ):
            connector = _TeamsTestConnector()
            results = await connector.search("LM358N")
        assert len(results) == 1
        assert "Teams" in results[0]["vendor_name"]

    @pytest.mark.asyncio
    async def test_search_no_webhook_url(self):
        from app.routers.sources import _TeamsTestConnector

        with patch("app.routers.sources.get_credential_cached", return_value=None):
            connector = _TeamsTestConnector()
            with pytest.raises(ValueError, match="TEAMS_WEBHOOK_URL not configured"):
                await connector.search("LM358N")

    @pytest.mark.asyncio
    async def test_search_non_200_raises(self):
        from app.routers.sources import _TeamsTestConnector

        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Server Error"

        with (
            patch("app.routers.sources.get_credential_cached", return_value="https://webhook.office.com/test"),
            patch("app.http_client.http.post", new_callable=AsyncMock, return_value=mock_resp),
        ):
            connector = _TeamsTestConnector()
            with pytest.raises(ValueError, match="Teams webhook returned 500"):
                await connector.search("LM358N")

    @pytest.mark.asyncio
    async def test_search_202_accepted(self):
        from app.routers.sources import _TeamsTestConnector

        mock_resp = MagicMock()
        mock_resp.status_code = 202

        with (
            patch("app.routers.sources.get_credential_cached", return_value="https://webhook.office.com/test"),
            patch("app.http_client.http.post", new_callable=AsyncMock, return_value=mock_resp),
        ):
            connector = _TeamsTestConnector()
            results = await connector.search("LM358N")
        assert results[0]["status"] == "ok"


# ---------------------------------------------------------------------------
# toggle_api_source — with credentials (live path)
# ---------------------------------------------------------------------------


class TestToggleApiSourceWithCredentials:
    def test_toggle_to_live_when_all_creds_set(self, src_client: TestClient, api_source: ApiSource):
        with patch("app.services.credential_service.credential_is_set", return_value=True):
            resp = src_client.put(f"/api/sources/{api_source.id}/toggle", json={"status": "live"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["status"] == "live"

    def test_toggle_to_disabled(self, src_client: TestClient, api_source: ApiSource):
        resp = src_client.put(f"/api/sources/{api_source.id}/toggle", json={"status": "disabled"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "disabled"

    def test_toggle_to_pending_when_creds_missing(self, src_client: TestClient, api_source: ApiSource):
        with patch("app.services.credential_service.credential_is_set", return_value=False):
            resp = src_client.put(f"/api/sources/{api_source.id}/toggle", json={"status": "live"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "pending"


# ---------------------------------------------------------------------------
# list_api_sources — source with last_error_at, last_success, last_ping_at
# ---------------------------------------------------------------------------


class TestListApiSourcesDateFields:
    def test_sources_with_all_date_fields(self, src_client: TestClient, db_session: Session):
        src = ApiSource(
            name="dated_source",
            display_name="Dated Source",
            category="market_data",
            source_type="api",
            status="live",
            is_active=True,
            description="Source with dates",
            env_vars=[],
            last_success=datetime(2026, 1, 1, tzinfo=timezone.utc),
            last_error_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
            last_ping_at=datetime(2026, 1, 3, tzinfo=timezone.utc),
            last_error="timeout",
            error_count_24h=3,
            monthly_quota=1000,
            calls_this_month=50,
        )
        db_session.add(src)
        db_session.commit()

        resp = src_client.get("/api/sources")
        assert resp.status_code == 200
        data = resp.json()
        sources = data["sources"]
        s = next((x for x in sources if x["name"] == "dated_source"), None)
        assert s is not None
        assert s["last_success"] is not None
        assert s["last_error_at"] is not None
        assert s["last_ping_at"] is not None
        assert s["error_count_24h"] == 3
        assert s["monthly_quota"] == 1000
        assert s["calls_this_month"] == 50

    def test_source_with_live_status_downgraded_to_pending_when_no_creds(
        self, src_client: TestClient, db_session: Session
    ):
        """Live source without any credentials is downgraded to pending."""
        src = ApiSource(
            name="live_no_creds",
            display_name="Live No Creds",
            category="market_data",
            source_type="api",
            status="live",
            is_active=True,
            description="Live source with no creds",
            env_vars=["MISSING_KEY"],
        )
        db_session.add(src)
        db_session.commit()

        resp = src_client.get("/api/sources")
        assert resp.status_code == 200
        data = resp.json()
        s = next((x for x in data["sources"] if x["name"] == "live_no_creds"), None)
        assert s is not None
        # Downgraded to pending since credentials are all missing
        assert s["status"] == "pending"

    def test_source_with_masked_credentials(self, src_client: TestClient, db_session: Session):
        src = ApiSource(
            name="credentialed_src",
            display_name="Credentialed Source",
            category="market_data",
            source_type="api",
            status="live",
            is_active=True,
            description="Has credentials",
            env_vars=["CRED_KEY"],
        )
        db_session.add(src)
        db_session.commit()

        with (
            patch("app.services.credential_service.credential_is_set", return_value=True),
            patch("app.services.credential_service.get_credential", return_value="sk-secretkey"),
            patch("app.services.credential_service.mask_value", return_value="sk-****"),
        ):
            resp = src_client.get("/api/sources")
        assert resp.status_code == 200
        data = resp.json()
        s = next((x for x in data["sources"] if x["name"] == "credentialed_src"), None)
        assert s is not None
        assert "credentials_masked" in s


# ---------------------------------------------------------------------------
# test_api_source — success path updates last_success
# ---------------------------------------------------------------------------


class TestTestApiSourceSuccess:
    def test_success_updates_status_to_live(self, src_client: TestClient, api_source: ApiSource, db_session: Session):
        mock_connector = MagicMock()
        mock_connector.search = AsyncMock(return_value=[{"vendor_name": "Test", "mpn_matched": "LM358N"}])

        with patch("app.routers.sources._get_connector_for_source", return_value=mock_connector):
            resp = src_client.post(f"/api/sources/{api_source.id}/test")

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["results_count"] == 1
        assert data["sample"] is not None

        db_session.refresh(api_source)
        assert api_source.status == "live"
        assert api_source.last_success is not None
        assert api_source.last_error is None

    def test_404_for_missing_source(self, src_client: TestClient):
        resp = src_client.post("/api/sources/99999/test")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# email_mining_status — with source record
# ---------------------------------------------------------------------------


class TestEmailMiningStatus:
    def test_with_source_returns_real_data(self, src_client: TestClient, db_session: Session):
        src = ApiSource(
            name="email_mining",
            display_name="Email Mining",
            category="intelligence",
            source_type="email",
            status="live",
            description="Mining",
            env_vars=[],
            total_searches=12,
            total_results=48,
            last_success=datetime(2026, 3, 15, tzinfo=timezone.utc),
        )
        db_session.add(src)
        db_session.commit()

        resp = src_client.get("/api/email-mining/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_scans"] == 12
        assert data["total_vendors_found"] == 48
        assert data["last_scan"] is not None


# ---------------------------------------------------------------------------
# vendor_engagement_detail endpoint
# ---------------------------------------------------------------------------


class TestVendorEngagementDetail:
    def test_returns_engagement_data(self, src_client: TestClient, test_vendor_card: VendorCard, db_session: Session):
        with patch("app.services.vendor_score.compute_single_vendor_score") as mock_compute:
            mock_compute.return_value = {"vendor_score": 75, "is_new_vendor": False}
            resp = src_client.get(f"/api/vendors/{test_vendor_card.id}/engagement")

        assert resp.status_code == 200
        data = resp.json()
        assert data["vendor_id"] == test_vendor_card.id
        assert data["vendor_name"] == test_vendor_card.display_name
        assert "raw_counts" in data
        assert "live_vendor_score" in data
        assert "live_is_new_vendor" in data

    def test_returns_404_for_missing_vendor(self, src_client: TestClient):
        with patch("app.services.vendor_score.compute_single_vendor_score") as mock_compute:
            mock_compute.return_value = {"vendor_score": 0, "is_new_vendor": True}
            resp = src_client.get("/api/vendors/99999/engagement")
        assert resp.status_code == 404

    def test_returns_stored_score_values(
        self, src_client: TestClient, test_vendor_card: VendorCard, db_session: Session
    ):
        test_vendor_card.vendor_score = 82
        test_vendor_card.is_new_vendor = False
        test_vendor_card.total_outreach = 5
        test_vendor_card.total_responses = 3
        test_vendor_card.total_wins = 1
        db_session.commit()
        db_session.refresh(test_vendor_card)

        with patch("app.services.vendor_score.compute_single_vendor_score") as mock_compute:
            mock_compute.return_value = {"vendor_score": 82, "is_new_vendor": False}
            resp = src_client.get(f"/api/vendors/{test_vendor_card.id}/engagement")

        assert resp.status_code == 200
        data = resp.json()
        assert data["vendor_score"] == 82
        assert data["is_new_vendor"] is False
        assert data["raw_counts"]["total_outreach"] == 5
        assert data["raw_counts"]["total_wins"] == 1

    def test_vendor_with_score_computed_at(
        self, src_client: TestClient, test_vendor_card: VendorCard, db_session: Session
    ):
        test_vendor_card.vendor_score_computed_at = datetime(2026, 3, 10, tzinfo=timezone.utc)
        db_session.commit()

        with patch("app.services.vendor_score.compute_single_vendor_score") as mock_compute:
            mock_compute.return_value = {"vendor_score": 50, "is_new_vendor": True}
            resp = src_client.get(f"/api/vendors/{test_vendor_card.id}/engagement")

        assert resp.status_code == 200
        data = resp.json()
        assert data["computed_at"] is not None


# ---------------------------------------------------------------------------
# email_mining_compute_engagement
# ---------------------------------------------------------------------------


class TestComputeEngagement:
    def test_returns_updated_and_skipped(self, src_client: TestClient):
        with patch("app.services.vendor_score.compute_all_vendor_scores", new_callable=AsyncMock) as mock_compute:
            mock_compute.return_value = {"updated": 10, "skipped": 2}
            resp = src_client.post("/api/email-mining/compute-engagement")

        assert resp.status_code == 200
        data = resp.json()
        assert data["updated"] == 10
        assert data["skipped"] == 2

    def test_returns_zero_counts_when_no_vendors(self, src_client: TestClient):
        with patch("app.services.vendor_score.compute_all_vendor_scores", new_callable=AsyncMock) as mock_compute:
            mock_compute.return_value = {"updated": 0, "skipped": 0}
            resp = src_client.post("/api/email-mining/compute-engagement")

        assert resp.status_code == 200
        data = resp.json()
        assert data["updated"] == 0


# ---------------------------------------------------------------------------
# scan_outbound — vendor domain matching
# ---------------------------------------------------------------------------


class TestScanOutboundVendorMatch:
    def test_updates_vendor_card_by_domain(
        self, src_client: TestClient, test_user: User, db_session: Session, test_vendor_card: VendorCard
    ):
        test_user.m365_connected = True
        test_user.access_token = "fake-token"
        db_session.commit()

        # Give vendor card a domain
        test_vendor_card.domain = "arrow.com"
        db_session.commit()

        mock_miner = MagicMock()
        mock_miner.scan_sent_items = AsyncMock(
            return_value={
                "messages_scanned": 5,
                "rfqs_detected": 2,
                "vendors_contacted": {"arrow.com": 3},
                "used_delta": True,
            }
        )

        with (
            patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="token"),
            patch("app.connectors.email_mining.EmailMiner", return_value=mock_miner),
        ):
            resp = src_client.post("/api/email-mining/scan-outbound")

        assert resp.status_code == 200
        data = resp.json()
        assert data["rfqs_detected"] == 2
        assert data["used_delta"] is True

    def test_matches_by_normalized_name_prefix(self, src_client: TestClient, test_user: User, db_session: Session):
        test_user.m365_connected = True
        test_user.access_token = "fake-token"
        db_session.commit()

        card = VendorCard(
            normalized_name="mouser",
            display_name="Mouser Electronics",
            emails=[],
            phones=[],
        )
        db_session.add(card)
        db_session.commit()

        mock_miner = MagicMock()
        mock_miner.scan_sent_items = AsyncMock(
            return_value={
                "messages_scanned": 3,
                "rfqs_detected": 1,
                "vendors_contacted": {"mouser.com": 2},
                "used_delta": False,
            }
        )

        with (
            patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="token"),
            patch("app.connectors.email_mining.EmailMiner", return_value=mock_miner),
        ):
            resp = src_client.post("/api/email-mining/scan-outbound")

        assert resp.status_code == 200
        data = resp.json()
        assert data["cards_updated"] == 1

    def test_no_m365_returns_400(self, src_client: TestClient, test_user: User, db_session: Session):
        test_user.m365_connected = False
        test_user.access_token = None
        db_session.commit()

        resp = src_client.post("/api/email-mining/scan-outbound")
        assert resp.status_code == 400
        body = resp.json()
        assert "M365 not connected" in (body.get("detail") or body.get("error") or "")


# ---------------------------------------------------------------------------
# _create_sightings_from_attachment — unit tests
# Note: Requirement model uses primary_mpn column; the source code accesses
# req.mpn which goes through SQLAlchemy's attribute access (primary_mpn is
# the column, mpn is not mapped). We test the no-requirements branch and
# the endpoint-level integration.
# ---------------------------------------------------------------------------


class TestCreateSightingsFromAttachment:
    def test_no_requirements_returns_zero(self, db_session: Session, test_user: User):
        """When VR has no linked requirements, returns 0 sightings."""
        from app.routers.sources import _create_sightings_from_attachment

        req = Requisition(
            name="SIGHTING-TEST",
            status="active",
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.flush()

        vr = VendorResponse(
            requisition_id=req.id,
            vendor_name="Test Vendor",
            vendor_email="v@test.com",
            subject="RE: Test",
            message_id="msg-001",
            status="new",
            received_at=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(vr)
        db_session.commit()

        # No requirements for this requisition → early return 0
        result = _create_sightings_from_attachment(db_session, vr, [{"mpn": "LM317T", "qty": 100}])
        assert result == 0


# ---------------------------------------------------------------------------
# parse_response_attachments — response not found
# ---------------------------------------------------------------------------


class TestParseAttachmentsNotFound:
    def test_response_not_found_returns_404(self, src_client: TestClient, test_user: User, db_session: Session):
        test_user.m365_connected = True
        test_user.access_token = "token"
        db_session.commit()

        resp = src_client.post("/api/email-mining/parse-response-attachments/99999")
        assert resp.status_code == 404

    def test_no_parseable_attachments_returns_zero(self, src_client: TestClient, test_user: User, db_session: Session):
        test_user.m365_connected = True
        test_user.access_token = "token"
        db_session.commit()

        req = Requisition(
            name="REQ-NOATT",
            status="active",
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.flush()

        vr = VendorResponse(
            requisition_id=req.id,
            vendor_name="Test",
            vendor_email="t@test.com",
            subject="RE: test",
            message_id="msg-noatt-001",
            status="new",
            received_at=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(vr)
        db_session.commit()
        db_session.refresh(vr)

        mock_gc = MagicMock()
        mock_gc.get_json = AsyncMock(return_value={"value": [{"name": "photo.jpg"}, {"name": "document.pdf"}]})

        with (
            patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="token"),
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
        ):
            resp = src_client.post(f"/api/email-mining/parse-response-attachments/{vr.id}")

        assert resp.status_code == 200
        data = resp.json()
        assert data["parseable"] == 0
        assert data["sightings_created"] == 0

    def test_empty_attachment_list(self, src_client: TestClient, test_user: User, db_session: Session):
        test_user.m365_connected = True
        test_user.access_token = "token"
        db_session.commit()

        req = Requisition(
            name="REQ-EMPTYATT",
            status="active",
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.flush()

        vr = VendorResponse(
            requisition_id=req.id,
            vendor_name="Test",
            vendor_email="t@test.com",
            subject="RE: test",
            message_id="msg-emptyatt-001",
            status="new",
            received_at=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(vr)
        db_session.commit()
        db_session.refresh(vr)

        mock_gc = MagicMock()
        mock_gc.get_json = AsyncMock(return_value={"value": []})

        with (
            patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="token"),
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
        ):
            resp = src_client.post(f"/api/email-mining/parse-response-attachments/{vr.id}")

        assert resp.status_code == 200
        data = resp.json()
        assert data["attachments_found"] == 0
        assert data["parseable"] == 0
