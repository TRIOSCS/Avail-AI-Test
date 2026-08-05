"""test_sources_comprehensive.py — Comprehensive tests for routers/sources.py.

Covers: _LushaTestConnector, toggle_source_active, no connector found,
source without env vars, list_sources auto-downgrade, and additional
connector test methods.

Called by: pytest
Depends on: app/routers/sources.py, conftest fixtures
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import ApiSource, User
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

    # Source test/toggle/credential endpoints are gated on MANAGE_CONNECTORS (SET-06) —
    # no longer an interactive-role default — so grant it to the buyer test_user here to
    # exercise the capability-holder path.
    from app.constants import AccessKey

    test_user.access_overrides = {
        **(test_user.access_overrides or {}),
        AccessKey.MANAGE_CONNECTORS.value: True,
    }

    limiter.reset()
    try:
        with TestClient(app) as c:
            yield c
    finally:
        for dep in [get_db, require_user, require_buyer, require_settings_access]:
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
        """Phase-0 FIX C: a keyless source (no env_vars) that errors now persists
        status=error (the has_env_vars gate was dropped so keyless Test results are
        recorded instead of silently discarded)."""
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
        assert src.status == "error"  # FIX C: keyless result now persisted


# ---------------------------------------------------------------------------
# _LushaTestConnector
# ---------------------------------------------------------------------------


class TestLushaTestConnector:
    @pytest.mark.asyncio
    async def test_lusha_success_200(self):
        from app.services.connector_registry import LushaTestConnector as _LushaTestConnector

        mock_resp = MagicMock()
        mock_resp.status_code = 200

        connector = _LushaTestConnector()
        with (
            patch("app.services.connector_registry.get_credential_cached", return_value="lusha_key_123"),
            patch("app.http_client.http.get", new_callable=AsyncMock, return_value=mock_resp),
        ):
            results = await connector.search("LM358N")
        assert len(results) == 1
        assert "Person found" in results[0]["mpn_matched"]

    @pytest.mark.asyncio
    async def test_lusha_success_404(self):
        """404 means API key is valid, just no person found."""
        from app.services.connector_registry import LushaTestConnector as _LushaTestConnector

        mock_resp = MagicMock()
        mock_resp.status_code = 404

        connector = _LushaTestConnector()
        with (
            patch("app.services.connector_registry.get_credential_cached", return_value="lusha_key_123"),
            patch("app.http_client.http.get", new_callable=AsyncMock, return_value=mock_resp),
        ):
            results = await connector.search("LM358N")
        assert len(results) == 1
        assert "API key valid" in results[0]["mpn_matched"]

    @pytest.mark.asyncio
    async def test_lusha_no_key(self):
        from app.services.connector_registry import LushaTestConnector as _LushaTestConnector

        connector = _LushaTestConnector()
        with patch("app.services.connector_registry.get_credential_cached", return_value=None):
            with pytest.raises(ValueError, match="LUSHA_API_KEY not configured"):
                await connector.search("LM358N")

    @pytest.mark.asyncio
    async def test_lusha_api_error(self):
        from app.services.connector_registry import LushaTestConnector as _LushaTestConnector

        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.text = "Unauthorized"

        connector = _LushaTestConnector()
        with (
            patch("app.services.connector_registry.get_credential_cached", return_value="bad_key"),
            patch("app.http_client.http.get", new_callable=AsyncMock, return_value=mock_resp),
        ):
            with pytest.raises(ValueError, match="Lusha API returned 401"):
                await connector.search("LM358N")


# ---------------------------------------------------------------------------
# _ExploriumTestConnector
# ---------------------------------------------------------------------------


class TestExploriumTestConnector:
    @pytest.mark.asyncio
    async def test_explorium_success(self):
        from app.services.connector_registry import ExploriumTestConnector as _ExploriumTestConnector

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"firmo_name": "Anthropic"}

        connector = _ExploriumTestConnector()
        with (
            patch("app.services.connector_registry.get_credential_cached", return_value="exp_key"),
            patch("app.http_client.http.post", new_callable=AsyncMock, return_value=mock_resp),
        ):
            results = await connector.search("LM358N")
        assert len(results) == 1
        assert "Anthropic" in results[0]["mpn_matched"]

    @pytest.mark.asyncio
    async def test_explorium_no_key(self):
        from app.services.connector_registry import ExploriumTestConnector as _ExploriumTestConnector

        connector = _ExploriumTestConnector()
        with patch("app.services.connector_registry.get_credential_cached", return_value=None):
            with pytest.raises(ValueError, match="EXPLORIUM_API_KEY not configured"):
                await connector.search("LM358N")

    @pytest.mark.asyncio
    async def test_explorium_api_error(self):
        from app.services.connector_registry import ExploriumTestConnector as _ExploriumTestConnector

        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Server Error"

        connector = _ExploriumTestConnector()
        with (
            patch("app.services.connector_registry.get_credential_cached", return_value="key"),
            patch("app.http_client.http.post", new_callable=AsyncMock, return_value=mock_resp),
        ):
            with pytest.raises(ValueError, match="Explorium API returned 500"):
                await connector.search("LM358N")

    @pytest.mark.asyncio
    async def test_explorium_fallback_name(self):
        from app.services.connector_registry import ExploriumTestConnector as _ExploriumTestConnector

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"name": "FallbackCo"}

        connector = _ExploriumTestConnector()
        with (
            patch("app.services.connector_registry.get_credential_cached", return_value="key"),
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
        from app.services.connector_registry import AzureOAuthTestConnector as _AzureOAuthTestConnector

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"issuer": "https://login.microsoftonline.com/test-tenant/v2.0"}

        connector = _AzureOAuthTestConnector()
        with (
            patch("app.services.connector_registry.settings", SimpleNamespace(azure_tenant_id="test-tenant")),
            patch("app.http_client.http.get", new_callable=AsyncMock, return_value=mock_resp),
        ):
            results = await connector.search("LM358N")
        assert len(results) == 1
        assert results[0]["mpn_matched"] == "Tenant verified"

    @pytest.mark.asyncio
    async def test_azure_no_tenant(self):
        from app.services.connector_registry import AzureOAuthTestConnector as _AzureOAuthTestConnector

        connector = _AzureOAuthTestConnector()
        with patch("app.services.connector_registry.settings", SimpleNamespace(azure_tenant_id=None)):
            with pytest.raises(ValueError, match="AZURE_TENANT_ID not configured"):
                await connector.search("LM358N")

    @pytest.mark.asyncio
    async def test_azure_api_error(self):
        from app.services.connector_registry import AzureOAuthTestConnector as _AzureOAuthTestConnector

        mock_resp = MagicMock()
        mock_resp.status_code = 404

        connector = _AzureOAuthTestConnector()
        with (
            patch("app.services.connector_registry.settings", SimpleNamespace(azure_tenant_id="test-tenant")),
            patch("app.http_client.http.get", new_callable=AsyncMock, return_value=mock_resp),
        ):
            with pytest.raises(ValueError, match="Azure OpenID discovery returned 404"):
                await connector.search("LM358N")

    @pytest.mark.asyncio
    async def test_azure_tenant_mismatch(self):
        from app.services.connector_registry import AzureOAuthTestConnector as _AzureOAuthTestConnector

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"issuer": "https://login.microsoftonline.com/other-tenant/v2.0"}

        connector = _AzureOAuthTestConnector()
        with (
            patch("app.services.connector_registry.settings", SimpleNamespace(azure_tenant_id="test-tenant")),
            patch("app.http_client.http.get", new_callable=AsyncMock, return_value=mock_resp),
        ):
            with pytest.raises(ValueError, match="Tenant mismatch"):
                await connector.search("LM358N")


# ---------------------------------------------------------------------------
# _get_connector_for_source — lusha_enrichment
# ---------------------------------------------------------------------------


def test_get_connector_lusha_enrichment():
    from app.services.connector_registry import LushaTestConnector as _LushaTestConnector

    result = _get_connector_for_source("lusha_enrichment")
    assert isinstance(result, _LushaTestConnector)


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
