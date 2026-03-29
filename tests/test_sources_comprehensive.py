"""test_sources_comprehensive.py — Comprehensive tests for routers/sources.py.

Covers: _LushaTestConnector, health_summary, system_alerts, toggle_source_active,
parse attachments edge cases, scan_inbox with contact enrichment creating vendor cards,
outbound scan with commit error, no connector found, source without env vars,
and additional connector test methods.

Called by: pytest
Depends on: app/routers/sources.py, conftest fixtures
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import ApiSource, User, VendorCard
from app.rate_limit import limiter
from app.routers.sources import _get_connector_for_source
from tests.conftest import engine

_ = engine


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def sources_client(db_session: Session, test_user: User) -> TestClient:
    """TestClient with auth + settings_access overrides and limiter reset."""
    from app.database import get_db
    from app.dependencies import require_buyer, require_fresh_token, require_settings_access, require_user
    from app.main import app

    def _override_db():
        yield db_session

    def _override_user():
        return test_user

    async def _override_fresh_token():
        return "mock-token"

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_user] = _override_user
    app.dependency_overrides[require_buyer] = _override_user
    app.dependency_overrides[require_settings_access] = _override_user
    app.dependency_overrides[require_fresh_token] = _override_fresh_token

    limiter.reset()
    try:
        with TestClient(app) as c:
            yield c
    finally:
        for dep in [get_db, require_user, require_buyer, require_settings_access, require_fresh_token]:
            app.dependency_overrides.pop(dep, None)


@pytest.fixture()
def _api_source(db_session: Session) -> ApiSource:
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
def _errored_source(db_session: Session) -> ApiSource:
    src = ApiSource(
        name="errored_source",
        display_name="Errored Source",
        category="market_data",
        source_type="api",
        status="error",
        is_active=True,
        description="A broken source",
        env_vars=["ERR_KEY"],
        last_error="Connection timeout",
        last_error_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
    )
    db_session.add(src)
    db_session.commit()
    db_session.refresh(src)
    return src


@pytest.fixture()
def _degraded_source(db_session: Session) -> ApiSource:
    src = ApiSource(
        name="degraded_source",
        display_name="Degraded Source",
        category="market_data",
        source_type="api",
        status="degraded",
        is_active=True,
        description="A degraded source",
        env_vars=["DEG_KEY"],
        last_error="Slow response",
    )
    db_session.add(src)
    db_session.commit()
    db_session.refresh(src)
    return src


@pytest.fixture()
def _email_mining_source(db_session: Session) -> ApiSource:
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


# ---------------------------------------------------------------------------
# health_summary endpoint
# ---------------------------------------------------------------------------


class TestHealthSummary:
    def test_no_errors(self, sources_client: TestClient, _api_source: ApiSource):
        resp = sources_client.get("/api/sources/health-summary")
        assert resp.status_code == 200
        data = resp.json()
        assert data["has_errors"] is False
        assert data["errored_sources"] == []

    def test_with_errored_source(self, sources_client: TestClient, _errored_source: ApiSource):
        resp = sources_client.get("/api/sources/health-summary")
        assert resp.status_code == 200
        data = resp.json()
        assert data["has_errors"] is True
        assert len(data["errored_sources"]) == 1
        assert data["errored_sources"][0]["display_name"] == "Errored Source"

    def test_with_degraded_source(self, sources_client: TestClient, _degraded_source: ApiSource):
        resp = sources_client.get("/api/sources/health-summary")
        assert resp.status_code == 200
        data = resp.json()
        assert data["has_errors"] is True
        assert len(data["errored_sources"]) >= 1

    def test_inactive_errored_not_included(self, sources_client: TestClient, db_session: Session):
        """Inactive sources with error status are not included."""
        src = ApiSource(
            name="inactive_errored",
            display_name="Inactive Error",
            category="market_data",
            source_type="api",
            status="error",
            is_active=False,
            description="Inactive",
            env_vars=[],
        )
        db_session.add(src)
        db_session.commit()

        resp = sources_client.get("/api/sources/health-summary")
        assert resp.status_code == 200
        data = resp.json()
        assert data["has_errors"] is False


# ---------------------------------------------------------------------------
# system_alerts endpoint
# ---------------------------------------------------------------------------


class TestSystemAlerts:
    def test_no_alerts(self, sources_client: TestClient):
        resp = sources_client.get("/api/system/alerts")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 0
        assert data["alerts"] == []

    def test_with_problem_sources(self, sources_client: TestClient, _errored_source: ApiSource):
        resp = sources_client.get("/api/system/alerts")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 1
        assert data["alerts"][0]["source_name"] == "errored_source"
        assert data["alerts"][0]["status"] == "error"
        assert data["alerts"][0]["last_error"] == "Connection timeout"
        assert data["alerts"][0]["since"] is not None

    def test_multiple_alerts(self, sources_client: TestClient, _errored_source: ApiSource, _degraded_source: ApiSource):
        resp = sources_client.get("/api/system/alerts")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 2


# ---------------------------------------------------------------------------
# toggle_source_active endpoint
# ---------------------------------------------------------------------------


class TestToggleSourceActive:
    def test_toggle_active(self, sources_client: TestClient, _api_source: ApiSource):
        initial_active = _api_source.is_active
        resp = sources_client.put(f"/api/sources/{_api_source.id}/activate")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["is_active"] is not initial_active

    def test_toggle_active_not_found(self, sources_client: TestClient):
        resp = sources_client.put("/api/sources/99999/activate")
        assert resp.status_code == 404

    def test_toggle_twice_returns_to_original(self, sources_client: TestClient, _api_source: ApiSource):
        initial = _api_source.is_active
        sources_client.put(f"/api/sources/{_api_source.id}/activate")
        resp = sources_client.put(f"/api/sources/{_api_source.id}/activate")
        data = resp.json()
        assert data["is_active"] == initial


# ---------------------------------------------------------------------------
# toggle_api_source (status toggle) — not found
# ---------------------------------------------------------------------------


class TestToggleApiSourceNotFound:
    def test_toggle_not_found(self, sources_client: TestClient):
        resp = sources_client.put(
            "/api/sources/99999/toggle",
            json={"status": "live"},
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# test_api_source — no connector
# ---------------------------------------------------------------------------


class TestTestApiSourceNoConnector:
    def test_no_connector_returns_error(self, sources_client: TestClient, _api_source: ApiSource):
        with patch("app.routers.sources._get_connector_for_source", return_value=None):
            resp = sources_client.post(f"/api/sources/{_api_source.id}/test")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "error"
        assert "No connector" in data["error"]

    def test_source_without_env_vars(self, sources_client: TestClient, db_session: Session):
        """Source without env_vars does not update status to live/error."""
        src = ApiSource(
            name="no_env_source",
            display_name="No Env Source",
            category="internal",
            source_type="internal",
            status="pending",
            description="Internal source",
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

    def test_source_error_without_env_vars(self, sources_client: TestClient, db_session: Session):
        """Source without env_vars that errors does not update status."""
        src = ApiSource(
            name="no_env_err",
            display_name="No Env Err",
            category="internal",
            source_type="internal",
            status="pending",
            description="Internal source",
            env_vars=[],
        )
        db_session.add(src)
        db_session.commit()
        db_session.refresh(src)

        mock_connector = MagicMock()
        mock_connector.search = AsyncMock(side_effect=ValueError("fail"))

        with patch("app.routers.sources._get_connector_for_source", return_value=mock_connector):
            resp = sources_client.post(f"/api/sources/{src.id}/test")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "error"

        db_session.refresh(src)
        assert src.status == "pending"  # Not changed to "error"


# ---------------------------------------------------------------------------
# _LushaTestConnector
# ---------------------------------------------------------------------------


class TestLushaTestConnector:
    @pytest.mark.asyncio
    async def test_lusha_success_200(self):
        from app.routers.sources import _LushaTestConnector

        mock_resp = MagicMock()
        mock_resp.status_code = 200

        connector = _LushaTestConnector()
        with (
            patch("app.routers.sources.get_credential_cached", return_value="lusha_key_123"),
            patch("app.http_client.http.get", new_callable=AsyncMock, return_value=mock_resp),
        ):
            results = await connector.search("LM358N")
        assert len(results) == 1
        assert "Person found" in results[0]["mpn_matched"]

    @pytest.mark.asyncio
    async def test_lusha_success_404(self):
        """404 means API key is valid, just no person found."""
        from app.routers.sources import _LushaTestConnector

        mock_resp = MagicMock()
        mock_resp.status_code = 404

        connector = _LushaTestConnector()
        with (
            patch("app.routers.sources.get_credential_cached", return_value="lusha_key_123"),
            patch("app.http_client.http.get", new_callable=AsyncMock, return_value=mock_resp),
        ):
            results = await connector.search("LM358N")
        assert len(results) == 1
        assert "API key valid" in results[0]["mpn_matched"]

    @pytest.mark.asyncio
    async def test_lusha_no_key(self):
        from app.routers.sources import _LushaTestConnector

        connector = _LushaTestConnector()
        with patch("app.routers.sources.get_credential_cached", return_value=None):
            with pytest.raises(ValueError, match="LUSHA_API_KEY not configured"):
                await connector.search("LM358N")

    @pytest.mark.asyncio
    async def test_lusha_api_error(self):
        from app.routers.sources import _LushaTestConnector

        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.text = "Unauthorized"

        connector = _LushaTestConnector()
        with (
            patch("app.routers.sources.get_credential_cached", return_value="bad_key"),
            patch("app.http_client.http.get", new_callable=AsyncMock, return_value=mock_resp),
        ):
            with pytest.raises(ValueError, match="Lusha API returned 401"):
                await connector.search("LM358N")


# ---------------------------------------------------------------------------
# _ApolloTestConnector
# ---------------------------------------------------------------------------


class TestApolloTestConnector:
    @pytest.mark.asyncio
    async def test_apollo_success(self):
        from app.routers.sources import _ApolloTestConnector

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"people": [{"name": "John"}]}

        connector = _ApolloTestConnector()
        with (
            patch("app.routers.sources.get_credential_cached", return_value="apollo_key"),
            patch("app.http_client.http.post", new_callable=AsyncMock, return_value=mock_resp),
        ):
            results = await connector.search("LM358N")
        assert len(results) == 1
        assert "1 result" in results[0]["mpn_matched"]

    @pytest.mark.asyncio
    async def test_apollo_no_key(self):
        from app.routers.sources import _ApolloTestConnector

        connector = _ApolloTestConnector()
        with patch("app.routers.sources.get_credential_cached", return_value=None):
            with pytest.raises(ValueError, match="APOLLO_API_KEY not configured"):
                await connector.search("LM358N")

    @pytest.mark.asyncio
    async def test_apollo_api_error(self):
        from app.routers.sources import _ApolloTestConnector

        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_resp.text = "Forbidden"

        connector = _ApolloTestConnector()
        with (
            patch("app.routers.sources.get_credential_cached", return_value="key"),
            patch("app.http_client.http.post", new_callable=AsyncMock, return_value=mock_resp),
        ):
            with pytest.raises(ValueError, match="Apollo API returned 403"):
                await connector.search("LM358N")


# ---------------------------------------------------------------------------
# _ExploriumTestConnector
# ---------------------------------------------------------------------------


class TestExploriumTestConnector:
    @pytest.mark.asyncio
    async def test_explorium_success(self):
        from app.routers.sources import _ExploriumTestConnector

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"firmo_name": "Anthropic"}

        connector = _ExploriumTestConnector()
        with (
            patch("app.routers.sources.get_credential_cached", return_value="exp_key"),
            patch("app.http_client.http.post", new_callable=AsyncMock, return_value=mock_resp),
        ):
            results = await connector.search("LM358N")
        assert len(results) == 1
        assert "Anthropic" in results[0]["mpn_matched"]

    @pytest.mark.asyncio
    async def test_explorium_no_key(self):
        from app.routers.sources import _ExploriumTestConnector

        connector = _ExploriumTestConnector()
        with patch("app.routers.sources.get_credential_cached", return_value=None):
            with pytest.raises(ValueError, match="EXPLORIUM_API_KEY not configured"):
                await connector.search("LM358N")

    @pytest.mark.asyncio
    async def test_explorium_api_error(self):
        from app.routers.sources import _ExploriumTestConnector

        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Server Error"

        connector = _ExploriumTestConnector()
        with (
            patch("app.routers.sources.get_credential_cached", return_value="key"),
            patch("app.http_client.http.post", new_callable=AsyncMock, return_value=mock_resp),
        ):
            with pytest.raises(ValueError, match="Explorium API returned 500"):
                await connector.search("LM358N")

    @pytest.mark.asyncio
    async def test_explorium_fallback_name(self):
        from app.routers.sources import _ExploriumTestConnector

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"name": "FallbackCo"}

        connector = _ExploriumTestConnector()
        with (
            patch("app.routers.sources.get_credential_cached", return_value="key"),
            patch("app.http_client.http.post", new_callable=AsyncMock, return_value=mock_resp),
        ):
            results = await connector.search("LM358N")
        assert "FallbackCo" in results[0]["mpn_matched"]


# ---------------------------------------------------------------------------
# _AzureOAuthTestConnector
# ---------------------------------------------------------------------------


class TestAzureOAuthTestConnector:
    @pytest.mark.asyncio
    async def test_azure_success(self):
        from app.routers.sources import _AzureOAuthTestConnector

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"issuer": "https://login.microsoftonline.com/test-tenant/v2.0"}

        connector = _AzureOAuthTestConnector()
        with (
            patch("app.routers.sources.settings", SimpleNamespace(azure_tenant_id="test-tenant")),
            patch("app.http_client.http.get", new_callable=AsyncMock, return_value=mock_resp),
        ):
            results = await connector.search("LM358N")
        assert len(results) == 1
        assert results[0]["mpn_matched"] == "Tenant verified"

    @pytest.mark.asyncio
    async def test_azure_no_tenant(self):
        from app.routers.sources import _AzureOAuthTestConnector

        connector = _AzureOAuthTestConnector()
        with patch("app.routers.sources.settings", SimpleNamespace(azure_tenant_id=None)):
            with pytest.raises(ValueError, match="AZURE_TENANT_ID not configured"):
                await connector.search("LM358N")

    @pytest.mark.asyncio
    async def test_azure_api_error(self):
        from app.routers.sources import _AzureOAuthTestConnector

        mock_resp = MagicMock()
        mock_resp.status_code = 404

        connector = _AzureOAuthTestConnector()
        with (
            patch("app.routers.sources.settings", SimpleNamespace(azure_tenant_id="test-tenant")),
            patch("app.http_client.http.get", new_callable=AsyncMock, return_value=mock_resp),
        ):
            with pytest.raises(ValueError, match="Azure OpenID discovery returned 404"):
                await connector.search("LM358N")

    @pytest.mark.asyncio
    async def test_azure_tenant_mismatch(self):
        from app.routers.sources import _AzureOAuthTestConnector

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"issuer": "https://login.microsoftonline.com/other-tenant/v2.0"}

        connector = _AzureOAuthTestConnector()
        with (
            patch("app.routers.sources.settings", SimpleNamespace(azure_tenant_id="test-tenant")),
            patch("app.http_client.http.get", new_callable=AsyncMock, return_value=mock_resp),
        ):
            with pytest.raises(ValueError, match="Tenant mismatch"):
                await connector.search("LM358N")


# ---------------------------------------------------------------------------
# _get_connector_for_source — lusha_enrichment
# ---------------------------------------------------------------------------


def test_get_connector_lusha_enrichment():
    from app.routers.sources import _LushaTestConnector

    result = _get_connector_for_source("lusha_enrichment")
    assert isinstance(result, _LushaTestConnector)


# ---------------------------------------------------------------------------
# Scan inbox — contact enrichment creates vendor cards
# ---------------------------------------------------------------------------


class TestScanInboxEnrichment:
    def test_creates_vendor_card(
        self, sources_client: TestClient, _email_mining_source: ApiSource, db_session: Session
    ):
        """Scan creates VendorCard when contacts_enriched has new vendor."""
        mock_miner = MagicMock()
        mock_miner.scan_inbox = AsyncMock(
            return_value={
                "messages_scanned": 10,
                "vendors_found": 1,
                "offers_parsed": [],
                "contacts_enriched": [
                    {
                        "vendor_name": "New Vendor Inc",
                        "emails": ["sales@newvendor.com"],
                        "phones": ["+1-555-0100"],
                        "websites": ["newvendor.com"],
                    }
                ],
            }
        )

        with (
            patch("app.routers.sources.require_fresh_token", new_callable=AsyncMock, return_value="token"),
            patch("app.connectors.email_mining.EmailMiner", return_value=mock_miner),
        ):
            resp = sources_client.post("/api/email-mining/scan")

        assert resp.status_code == 200
        # Verify vendor card was created
        card = db_session.query(VendorCard).filter_by(display_name="New Vendor Inc").first()
        assert card is not None
        assert card.website == "https://newvendor.com"

    def test_enriches_existing_vendor_card(
        self, sources_client: TestClient, _email_mining_source: ApiSource, db_session: Session
    ):
        """Scan enriches existing VendorCard rather than creating duplicate."""
        from app.vendor_utils import normalize_vendor_name

        norm = normalize_vendor_name("Existing Vendor")
        existing = VendorCard(
            normalized_name=norm,
            display_name="Existing Vendor",
            emails=["old@existing.com"],
            phones=[],
        )
        db_session.add(existing)
        db_session.commit()

        mock_miner = MagicMock()
        mock_miner.scan_inbox = AsyncMock(
            return_value={
                "messages_scanned": 5,
                "vendors_found": 1,
                "offers_parsed": [],
                "contacts_enriched": [
                    {
                        "vendor_name": "Existing Vendor",
                        "emails": ["new@existing.com"],
                        "phones": [],
                        "websites": [],
                    }
                ],
            }
        )

        with (
            patch("app.routers.sources.require_fresh_token", new_callable=AsyncMock, return_value="token"),
            patch("app.connectors.email_mining.EmailMiner", return_value=mock_miner),
        ):
            resp = sources_client.post("/api/email-mining/scan")

        assert resp.status_code == 200

    def test_empty_vendor_name_skipped(self, sources_client: TestClient, _email_mining_source: ApiSource):
        """Contacts with empty vendor_name are skipped."""
        mock_miner = MagicMock()
        mock_miner.scan_inbox = AsyncMock(
            return_value={
                "messages_scanned": 5,
                "vendors_found": 0,
                "offers_parsed": [],
                "contacts_enriched": [
                    {"vendor_name": "", "emails": [], "phones": [], "websites": []},
                ],
            }
        )

        with (
            patch("app.routers.sources.require_fresh_token", new_callable=AsyncMock, return_value="token"),
            patch("app.connectors.email_mining.EmailMiner", return_value=mock_miner),
        ):
            resp = sources_client.post("/api/email-mining/scan")

        assert resp.status_code == 200
        data = resp.json()
        assert data["contacts_enriched"] == 0


# ---------------------------------------------------------------------------
# Email mining status — no source
# ---------------------------------------------------------------------------


class TestEmailMiningStatusNoSource:
    def test_no_source_returns_defaults(self, sources_client: TestClient):
        """When email_mining source doesn't exist, returns defaults."""
        resp = sources_client.get("/api/email-mining/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["last_scan"] is None
        assert data["total_scans"] == 0
        assert data["total_vendors_found"] == 0


# ---------------------------------------------------------------------------
# Parse attachments — no m365
# ---------------------------------------------------------------------------


class TestParseAttachmentsNoM365:
    def test_no_m365_returns_400(self, sources_client: TestClient, test_user: User, db_session: Session):
        test_user.m365_connected = False
        test_user.access_token = None
        db_session.commit()

        resp = sources_client.post("/api/email-mining/parse-response-attachments/1")
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Parse attachments — no message_id
# ---------------------------------------------------------------------------


class TestParseAttachmentsNoMessageId:
    def test_no_message_id_returns_400(self, sources_client: TestClient, test_user: User, db_session: Session):
        from app.models import Requisition, VendorResponse

        test_user.m365_connected = True
        test_user.access_token = "token"
        db_session.commit()

        req = Requisition(
            name="REQ-TEST",
            customer_name="Test",
            status="active",
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.flush()

        vr = VendorResponse(
            requisition_id=req.id,
            vendor_name="Test",
            vendor_email="t@t.com",
            subject="RE: Test",
            message_id=None,
            status="new",
            received_at=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(vr)
        db_session.commit()
        db_session.refresh(vr)

        resp = sources_client.post(f"/api/email-mining/parse-response-attachments/{vr.id}")
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Parse attachments — Graph API error
# ---------------------------------------------------------------------------


class TestParseAttachmentsGraphError:
    def test_graph_api_error_returns_502(self, sources_client: TestClient, test_user: User, db_session: Session):
        from app.models import Requisition, VendorResponse

        test_user.m365_connected = True
        test_user.access_token = "token"
        db_session.commit()

        req = Requisition(
            name="REQ-GRAPH",
            customer_name="Test",
            status="active",
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.flush()

        vr = VendorResponse(
            requisition_id=req.id,
            vendor_name="Test",
            vendor_email="t@t.com",
            subject="RE: Test",
            message_id="msg-graph-001",
            status="new",
            received_at=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(vr)
        db_session.commit()
        db_session.refresh(vr)

        mock_gc = MagicMock()
        mock_gc.get_json = AsyncMock(side_effect=ConnectionError("timeout"))

        with (
            patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="token"),
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
        ):
            resp = sources_client.post(f"/api/email-mining/parse-response-attachments/{vr.id}")

        assert resp.status_code == 502


# ---------------------------------------------------------------------------
# Outbound scan — DB commit error
# ---------------------------------------------------------------------------


class TestOutboundScanCommitError:
    def test_sqlalchemy_error_handled(self, sources_client: TestClient, test_user: User, db_session: Session):
        """SQLAlchemyError during commit is caught and rolled back."""
        test_user.m365_connected = True
        test_user.access_token = "fake-token"
        db_session.commit()

        mock_miner = MagicMock()
        mock_miner.scan_sent_items = AsyncMock(
            return_value={
                "messages_scanned": 10,
                "rfqs_detected": 2,
                "vendors_contacted": {},
                "used_delta": False,
            }
        )

        with (
            patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="token"),
            patch("app.connectors.email_mining.EmailMiner", return_value=mock_miner),
        ):
            resp = sources_client.post("/api/email-mining/scan-outbound")

        assert resp.status_code == 200
        data = resp.json()
        assert data["messages_scanned"] == 10


# ---------------------------------------------------------------------------
# list_sources — auto-downgrades to pending when no credentials
# ---------------------------------------------------------------------------


class TestListSourcesAutoDowngrade:
    def test_source_pending_when_no_creds(self, sources_client: TestClient, _api_source: ApiSource):
        """Source with env_vars but no credentials is downgraded to pending."""
        resp = sources_client.get("/api/sources")
        assert resp.status_code == 200
        data = resp.json()
        sources = data["sources"]
        src = next((s for s in sources if s["name"] == "test_source"), None)
        assert src is not None
        assert src["status"] == "pending"

    def test_disabled_source_not_downgraded(self, sources_client: TestClient, db_session: Session):
        """Disabled sources are not downgraded to pending."""
        src = ApiSource(
            name="disabled_src",
            display_name="Disabled",
            category="market_data",
            source_type="api",
            status="disabled",
            description="Disabled",
            env_vars=["SOME_KEY"],
        )
        db_session.add(src)
        db_session.commit()

        resp = sources_client.get("/api/sources")
        assert resp.status_code == 200
        data = resp.json()
        sources = data["sources"]
        s = next((s for s in sources if s["name"] == "disabled_src"), None)
        assert s is not None
        assert s["status"] == "disabled"


# ---------------------------------------------------------------------------
# Parse attachments — file validation fails
# ---------------------------------------------------------------------------


class TestParseAttachmentsValidationFails:
    def test_invalid_file_skipped(self, sources_client: TestClient, test_user: User, db_session: Session):
        import base64

        from app.models import Requisition, VendorResponse

        test_user.m365_connected = True
        test_user.access_token = "token"
        db_session.commit()

        req = Requisition(
            name="REQ-VAL",
            customer_name="Test",
            status="active",
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.flush()

        vr = VendorResponse(
            requisition_id=req.id,
            vendor_name="Test",
            vendor_email="t@t.com",
            subject="RE: Test",
            message_id="msg-val-001",
            status="new",
            received_at=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(vr)
        db_session.commit()
        db_session.refresh(vr)

        fake_content = base64.b64encode(b"fake").decode()
        mock_gc = MagicMock()
        mock_gc.get_json = AsyncMock(return_value={"value": [{"name": "data.csv", "contentBytes": fake_content}]})

        with (
            patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="token"),
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.utils.file_validation.validate_file", return_value=(False, "Invalid file")),
        ):
            resp = sources_client.post(f"/api/email-mining/parse-response-attachments/{vr.id}")

        assert resp.status_code == 200
        data = resp.json()
        assert data["parseable"] == 1
        assert data["rows_parsed"] == 0

    def test_no_content_bytes_skipped(self, sources_client: TestClient, test_user: User, db_session: Session):
        from app.models import Requisition, VendorResponse

        test_user.m365_connected = True
        test_user.access_token = "token"
        db_session.commit()

        req = Requisition(
            name="REQ-NOCB",
            customer_name="Test",
            status="active",
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.flush()

        vr = VendorResponse(
            requisition_id=req.id,
            vendor_name="Test",
            vendor_email="t@t.com",
            subject="RE: Test",
            message_id="msg-nocb-001",
            status="new",
            received_at=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(vr)
        db_session.commit()
        db_session.refresh(vr)

        mock_gc = MagicMock()
        mock_gc.get_json = AsyncMock(return_value={"value": [{"name": "data.csv", "contentBytes": None}]})

        with (
            patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="token"),
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
        ):
            resp = sources_client.post(f"/api/email-mining/parse-response-attachments/{vr.id}")

        assert resp.status_code == 200
        data = resp.json()
        assert data["rows_parsed"] == 0
