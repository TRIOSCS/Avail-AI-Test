"""tests/test_routers_sources.py — Tests for Sources Router.

Tests connector factory, test connector shims, and source management
endpoints (list, per-source test, activate toggle).

Called by: pytest
Depends on: routers/sources.py
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import ApiSource, User
from app.rate_limit import limiter
from app.routers.sources import _get_connector_for_source
from app.services.connector_registry import EmailMiningTestConnector as _EmailMiningTestConnector

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


@pytest.mark.parametrize(
    ("enabled", "expect_connector"),
    [
        pytest.param(True, True, id="enabled"),
        pytest.param(False, False, id="disabled"),
    ],
)
def test_get_connector_email_mining(monkeypatch, enabled, expect_connector):
    """Email mining returns the test connector only when enabled."""
    monkeypatch.setattr(
        "app.services.connector_registry.settings",
        SimpleNamespace(
            email_mining_enabled=enabled,
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
    if expect_connector:
        assert isinstance(result, _EmailMiningTestConnector)
    else:
        assert result is None


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

    # Source test/toggle/credential endpoints are gated on MANAGE_CONNECTORS
    # (SET-06) — which is no longer an interactive-role default — so grant it to
    # the buyer test_user here to exercise the capability-holder path.
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


def test_test_source_emits_showtoast_trigger(sources_client: TestClient, _api_source: ApiSource):
    """Phase-0 FIX C (UX half): a single Test sets HX-Trigger showToast so the button —
    which uses hx-swap=none and discards the JSON body — gives real pass/fail
    feedback."""
    import json

    mock_connector = MagicMock()
    mock_connector.search = AsyncMock(return_value=[{"vendor_name": "Test", "status": "ok"}])

    with patch("app.routers.sources._get_connector_for_source", return_value=mock_connector):
        resp = sources_client.post(f"/api/sources/{_api_source.id}/test")

    assert resp.status_code == 200
    trigger = json.loads(resp.headers["HX-Trigger"])
    assert trigger["showToast"]["type"] == "success"
    assert "Test Source" in trigger["showToast"]["message"]


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


# ══════════════════════════════════════════════════════════════════════
# NEW TESTS — Connector factory (all branches)
# ══════════════════════════════════════════════════════════════════════


def test_get_connector_nexar_with_octopart_key():
    """Nexar source returns NexarConnector when OCTOPART_API_KEY is set."""
    with patch("app.services.credential_service.get_credential", return_value="key123"):
        with patch("app.connectors.sources.NexarConnector"):
            result = _get_connector_for_source("nexar", db=MagicMock())
            assert result is not None


@pytest.mark.parametrize(
    ("source", "connector_path", "creds"),
    [
        pytest.param(
            "nexar",
            "app.connectors.sources.NexarConnector",
            {
                "NEXAR_CLIENT_ID": "nid",
                "NEXAR_CLIENT_SECRET": "nsec",
                "OCTOPART_API_KEY": None,
            },
            id="nexar-client-id",
        ),
        pytest.param(
            "brokerbin",
            "app.connectors.sources.BrokerBinConnector",
            {
                "NEXAR_CLIENT_ID": None,
                "NEXAR_CLIENT_SECRET": None,
                "OCTOPART_API_KEY": None,
                "BROKERBIN_API_KEY": "bb_key",
                "BROKERBIN_API_SECRET": "bb_sec",
            },
            id="brokerbin",
        ),
        pytest.param(
            "ebay",
            "app.connectors.ebay.EbayConnector",
            {
                "NEXAR_CLIENT_ID": None,
                "NEXAR_CLIENT_SECRET": None,
                "OCTOPART_API_KEY": None,
                "BROKERBIN_API_KEY": None,
                "BROKERBIN_API_SECRET": None,
                "EBAY_CLIENT_ID": "ebay_id",
                "EBAY_CLIENT_SECRET": "ebay_sec",
            },
            id="ebay",
        ),
        pytest.param(
            "digikey",
            "app.connectors.digikey.DigiKeyConnector",
            {
                "NEXAR_CLIENT_ID": None,
                "NEXAR_CLIENT_SECRET": None,
                "OCTOPART_API_KEY": None,
                "BROKERBIN_API_KEY": None,
                "BROKERBIN_API_SECRET": None,
                "EBAY_CLIENT_ID": None,
                "EBAY_CLIENT_SECRET": None,
                "DIGIKEY_CLIENT_ID": "dk_id",
                "DIGIKEY_CLIENT_SECRET": "dk_sec",
            },
            id="digikey",
        ),
        pytest.param(
            "mouser",
            "app.connectors.mouser.MouserConnector",
            {
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
            },
            id="mouser",
        ),
        pytest.param(
            "oemsecrets",
            "app.connectors.oemsecrets.OEMSecretsConnector",
            {
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
            },
            id="oemsecrets",
        ),
        pytest.param(
            "sourcengine",
            "app.connectors.sourcengine.SourcengineConnector",
            {
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
            },
            id="sourcengine",
        ),
        pytest.param(
            "element14",
            "app.connectors.element14.Element14Connector",
            {
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
            },
            id="newark",
        ),
    ],
)
def test_get_connector_for_keyed_source(source, connector_path, creds):
    """Each keyed source returns its connector when the relevant credential is set."""

    def fake_cred(db, name, var):
        return creds.get(var)

    with patch("app.services.credential_service.get_credential", side_effect=fake_cred):
        with patch(connector_path):
            result = _get_connector_for_source(source, db=MagicMock())
            assert result is not None


@pytest.mark.parametrize(
    ("source", "connector_attr"),
    [
        pytest.param("anthropic_ai", "_AnthropicTestConnector", id="anthropic_ai"),
        pytest.param("teams_notifications", "_TeamsTestConnector", id="teams_notifications"),
        pytest.param("explorium_enrichment", "_ExploriumTestConnector", id="explorium_enrichment"),
        pytest.param("azure_oauth", "_AzureOAuthTestConnector", id="azure_oauth"),
        pytest.param("hunter_enrichment", "_HunterTestConnector", id="hunter_enrichment"),
        pytest.param("clay_enrichment", "_ClayTestConnector", id="clay_enrichment"),
    ],
)
def test_get_connector_for_test_only_source(source, connector_attr):
    """Built-in test-only sources return their dedicated test connector (no env_vars
    needed)."""
    import app.services.connector_registry as connector_registry_module

    connector_cls = getattr(connector_registry_module, connector_attr.lstrip("_"))
    result = _get_connector_for_source(source)
    assert isinstance(result, connector_cls)


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
async def test_clay_test_connector_search_success(monkeypatch):
    """_ClayTestConnector succeeds when Clay is connected and the credits check
    returns."""
    from app.services.connector_registry import ClayTestConnector as _ClayTestConnector

    monkeypatch.setattr("app.services.clay_oauth.is_connected", lambda: True)

    async def fake_call(tool, args):
        assert tool == "get-credits-available"
        return {"hasWorkspaceCredits": True}

    monkeypatch.setattr("app.connectors.clay_mcp._mcp_call", fake_call)
    results = await _ClayTestConnector().search("LM358N")
    assert results and results[0]["status"] == "ok"


@pytest.mark.asyncio
async def test_clay_test_connector_raises_when_not_connected(monkeypatch):
    """_ClayTestConnector raises (→ health 'error') when Clay OAuth isn't connected."""
    from app.services.connector_registry import ClayTestConnector as _ClayTestConnector

    monkeypatch.setattr("app.services.clay_oauth.is_connected", lambda: False)
    with pytest.raises(ValueError, match="not connected"):
        await _ClayTestConnector().search("LM358N")


@pytest.mark.asyncio
async def test_clay_test_connector_raises_when_mcp_unhealthy(monkeypatch):
    """_ClayTestConnector raises when the MCP session/credits call returns nothing."""
    from app.services.connector_registry import ClayTestConnector as _ClayTestConnector

    monkeypatch.setattr("app.services.clay_oauth.is_connected", lambda: True)

    async def fake_call(tool, args):
        return {}

    monkeypatch.setattr("app.connectors.clay_mcp._mcp_call", fake_call)
    with pytest.raises(ValueError, match="health check failed"):
        await _ClayTestConnector().search("LM358N")


@pytest.mark.asyncio
async def test_anthropic_test_connector_search_success():
    """_AnthropicTestConnector succeeds when claude_text returns a response."""
    from app.services.connector_registry import AnthropicTestConnector as _AnthropicTestConnector

    connector = _AnthropicTestConnector()
    with patch("app.utils.claude_client.claude_text", new_callable=AsyncMock, return_value="OK"):
        results = await connector.search("LM358N")
    assert len(results) == 1
    assert "Connected" in results[0]["mpn_matched"]


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["no_key", "api_error"])
async def test_anthropic_test_connector_no_response(scenario):
    """_AnthropicTestConnector raises when claude_text returns None (no API key / API
    error)."""
    from app.services.connector_registry import AnthropicTestConnector as _AnthropicTestConnector

    connector = _AnthropicTestConnector()
    with patch("app.utils.claude_client.claude_text", new_callable=AsyncMock, return_value=None):
        with pytest.raises(ValueError, match="Anthropic API returned no response"):
            await connector.search("LM358N")


@pytest.mark.asyncio
async def test_teams_test_connector_success():
    """_TeamsTestConnector succeeds with 200 response."""
    from app.services.connector_registry import TeamsTestConnector as _TeamsTestConnector

    mock_resp = MagicMock()
    mock_resp.status_code = 200

    connector = _TeamsTestConnector()
    with (
        patch("app.services.connector_registry.get_credential_cached", return_value="https://webhook.example.com"),
        patch("app.http_client.http.post", new_callable=AsyncMock, return_value=mock_resp),
    ):
        results = await connector.search("LM358N")
    assert len(results) == 1
    assert results[0]["mpn_matched"] == "Message posted"


@pytest.mark.asyncio
async def test_teams_test_connector_no_webhook():
    """_TeamsTestConnector raises if no webhook URL."""
    from app.services.connector_registry import TeamsTestConnector as _TeamsTestConnector

    connector = _TeamsTestConnector()
    with patch("app.services.connector_registry.get_credential_cached", return_value=None):
        with pytest.raises(ValueError, match="TEAMS_WEBHOOK_URL not configured"):
            await connector.search("LM358N")


@pytest.mark.asyncio
async def test_teams_test_connector_api_error():
    """_TeamsTestConnector raises on non-200/202 response."""
    from app.services.connector_registry import TeamsTestConnector as _TeamsTestConnector

    mock_resp = MagicMock()
    mock_resp.status_code = 500
    mock_resp.text = "Internal error"

    connector = _TeamsTestConnector()
    with (
        patch("app.services.connector_registry.get_credential_cached", return_value="https://webhook.example.com"),
        patch("app.http_client.http.post", new_callable=AsyncMock, return_value=mock_resp),
    ):
        with pytest.raises(ValueError, match="Teams webhook returned 500"):
            await connector.search("LM358N")


@pytest.mark.asyncio
async def test_teams_test_connector_202_accepted():
    """_TeamsTestConnector succeeds with 202 response (accepted)."""
    from app.services.connector_registry import TeamsTestConnector as _TeamsTestConnector

    mock_resp = MagicMock()
    mock_resp.status_code = 202

    connector = _TeamsTestConnector()
    with (
        patch("app.services.connector_registry.get_credential_cached", return_value="https://webhook.example.com"),
        patch("app.http_client.http.post", new_callable=AsyncMock, return_value=mock_resp),
    ):
        results = await connector.search("LM358N")
    assert len(results) == 1
    assert results[0]["status"] == "ok"


@pytest.mark.asyncio
async def test_explorium_test_connector_success():
    """_ExploriumTestConnector succeeds when API returns 200."""
    from app.services.connector_registry import ExploriumTestConnector as _ExploriumTestConnector

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"firmo_name": "Anthropic Inc"}

    connector = _ExploriumTestConnector()
    with (
        patch("app.services.connector_registry.get_credential_cached", return_value="explorium_key"),
        patch("app.http_client.http.post", new_callable=AsyncMock, return_value=mock_resp),
    ):
        results = await connector.search("LM358N")
    assert len(results) == 1
    assert "Anthropic" in results[0]["mpn_matched"]


@pytest.mark.asyncio
async def test_explorium_test_connector_fallback_name():
    """_ExploriumTestConnector falls back to 'name' when 'firmo_name' missing."""
    from app.services.connector_registry import ExploriumTestConnector as _ExploriumTestConnector

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"name": "Some Corp"}

    connector = _ExploriumTestConnector()
    with (
        patch("app.services.connector_registry.get_credential_cached", return_value="explorium_key"),
        patch("app.http_client.http.post", new_callable=AsyncMock, return_value=mock_resp),
    ):
        results = await connector.search("LM358N")
    assert "Some Corp" in results[0]["mpn_matched"]


@pytest.mark.asyncio
async def test_explorium_test_connector_no_name():
    """_ExploriumTestConnector falls back to 'matched' when both name keys missing."""
    from app.services.connector_registry import ExploriumTestConnector as _ExploriumTestConnector

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {}

    connector = _ExploriumTestConnector()
    with (
        patch("app.services.connector_registry.get_credential_cached", return_value="explorium_key"),
        patch("app.http_client.http.post", new_callable=AsyncMock, return_value=mock_resp),
    ):
        results = await connector.search("LM358N")
    assert "matched" in results[0]["mpn_matched"]


@pytest.mark.asyncio
async def test_explorium_test_connector_no_key():
    """_ExploriumTestConnector raises if no API key."""
    from app.services.connector_registry import ExploriumTestConnector as _ExploriumTestConnector

    connector = _ExploriumTestConnector()
    with patch("app.services.connector_registry.get_credential_cached", return_value=None):
        with pytest.raises(ValueError, match="EXPLORIUM_API_KEY not configured"):
            await connector.search("LM358N")


@pytest.mark.asyncio
async def test_explorium_test_connector_api_error():
    """_ExploriumTestConnector raises on non-200 response."""
    from app.services.connector_registry import ExploriumTestConnector as _ExploriumTestConnector

    mock_resp = MagicMock()
    mock_resp.status_code = 500
    mock_resp.text = "Server error"

    connector = _ExploriumTestConnector()
    with (
        patch("app.services.connector_registry.get_credential_cached", return_value="explorium_key"),
        patch("app.http_client.http.post", new_callable=AsyncMock, return_value=mock_resp),
    ):
        with pytest.raises(ValueError, match="Explorium API returned 500"):
            await connector.search("LM358N")


@pytest.mark.asyncio
async def test_azure_oauth_test_connector_success():
    """_AzureOAuthTestConnector succeeds with valid tenant."""
    from app.services.connector_registry import AzureOAuthTestConnector as _AzureOAuthTestConnector

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"issuer": "https://login.microsoftonline.com/test-tenant-id/v2.0"}

    connector = _AzureOAuthTestConnector()
    mock_settings = SimpleNamespace(azure_tenant_id="test-tenant-id")
    with (
        patch("app.services.connector_registry.settings", mock_settings),
        patch("app.http_client.http.get", new_callable=AsyncMock, return_value=mock_resp),
    ):
        results = await connector.search("LM358N")
    assert len(results) == 1
    assert results[0]["mpn_matched"] == "Tenant verified"


@pytest.mark.asyncio
async def test_azure_oauth_test_connector_no_tenant():
    """_AzureOAuthTestConnector raises if no tenant ID configured."""
    from app.services.connector_registry import AzureOAuthTestConnector as _AzureOAuthTestConnector

    connector = _AzureOAuthTestConnector()
    mock_settings = SimpleNamespace(azure_tenant_id=None)
    with patch("app.services.connector_registry.settings", mock_settings):
        with pytest.raises(ValueError, match="AZURE_TENANT_ID not configured"):
            await connector.search("LM358N")


@pytest.mark.asyncio
async def test_azure_oauth_test_connector_api_error():
    """_AzureOAuthTestConnector raises on non-200 response."""
    from app.services.connector_registry import AzureOAuthTestConnector as _AzureOAuthTestConnector

    mock_resp = MagicMock()
    mock_resp.status_code = 404

    connector = _AzureOAuthTestConnector()
    mock_settings = SimpleNamespace(azure_tenant_id="bad-tenant")
    with (
        patch("app.services.connector_registry.settings", mock_settings),
        patch("app.http_client.http.get", new_callable=AsyncMock, return_value=mock_resp),
    ):
        with pytest.raises(ValueError, match="Azure OpenID discovery returned 404"):
            await connector.search("LM358N")


@pytest.mark.asyncio
async def test_azure_oauth_test_connector_tenant_mismatch():
    """_AzureOAuthTestConnector raises when issuer doesn't match tenant."""
    from app.services.connector_registry import AzureOAuthTestConnector as _AzureOAuthTestConnector

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"issuer": "https://login.microsoftonline.com/other-tenant/v2.0"}

    connector = _AzureOAuthTestConnector()
    mock_settings = SimpleNamespace(azure_tenant_id="my-tenant-id")
    with (
        patch("app.services.connector_registry.settings", mock_settings),
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
        last_success=datetime(2026, 2, 10, 12, 0, 0, tzinfo=UTC),
        total_searches=10,
        total_results=50,
        avg_response_ms=150,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
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
    """Phase-0 FIX C: a keyless source (no env_vars) that FAILS its probe now records
    status=error. The old has_env_vars gate discarded keyless results, so the card
    falsely stayed OK — this test now asserts the corrected persistence."""
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
    # Status IS now persisted for keyless sources (has_env_vars gate dropped).
    db_session.refresh(src)
    assert src.status == "error"
    assert "Test error" in (src.last_error or "")


def test_test_source_success_no_env_vars(sources_client: TestClient, db_session: Session):
    """Phase-0 FIX C: a keyless source (no env_vars) that PASSES its probe now records
    status=live (was: not persisted, so a keyless Test was zero-feedback)."""
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
    # Status IS now persisted for keyless sources (has_env_vars gate dropped).
    db_session.refresh(src)
    assert src.status == "live"
    assert src.last_success is not None


# ══════════════════════════════════════════════════════════════════════
# API Health: is_active flag, activate toggle
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
