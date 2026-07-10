"""connector_registry.py — resolve an ``ApiSource`` name to a live connector instance.

Single source of truth for "given this source name, build the connector that would
actually run its search/probe" — credential-aware (DB-stored credentials first, env
vars as fallback) and keyless-aware (a handful of sources have no API key but still
have a real Test path, e.g. ``ai_live_web``, ``teams_notifications``, ``clay_enrichment``).

Also owns the small ``TestConnector`` shims used ONLY by the Connectors-page "Test"
button for keyless/OAuth sources that have no real search connector of their own
(Anthropic, Teams, Lusha, Hunter, Clay, Explorium, Azure OAuth, email mining).

Called by: app.routers.sources (Test/health endpoints), app.services.health_monitor
    (scheduled ping/deep checks)
Depends on: app.config, app.services.admin_service, app.services.credential_service,
    app.connectors.* (lazy imports — avoids loading every connector module on startup)
"""

import os

from sqlalchemy.orm import Session

from ..config import settings
from ..services.admin_service import get_effective_flag
from ..services.credential_service import get_credential_cached


class EmailMiningTestConnector:
    """Thin wrapper so email_mining can be tested via the source test UI."""

    async def search(self, mpn: str) -> list[dict]:
        return [
            {
                "vendor_name": "Email Mining Active",
                "mpn_matched": "Inbox scanned every 30 min",
                "status": "ok",
            }
        ]


class AnthropicTestConnector:
    """Test Anthropic API key with a lightweight messages call."""

    async def search(self, mpn: str) -> list[dict]:
        from ..utils.claude_client import MODELS, claude_text

        result = await claude_text(
            prompt="Reply with only: OK",
            model_tier="fast",
            max_tokens=32,
            timeout=15,
        )
        if result is None:
            raise ValueError("Anthropic API returned no response")
        model = MODELS["fast"]
        return [{"vendor_name": "Anthropic AI", "mpn_matched": f"Connected — model: {model}", "status": "ok"}]


class TeamsTestConnector:
    """Test Teams webhook by posting a test adaptive card."""

    async def search(self, mpn: str) -> list[dict]:
        from ..http_client import http

        webhook_url = get_credential_cached("teams_notifications", "TEAMS_WEBHOOK_URL")
        if not webhook_url:
            raise ValueError("TEAMS_WEBHOOK_URL not configured")
        resp = await http.post(
            webhook_url,
            json={
                "type": "message",
                "attachments": [
                    {
                        "contentType": "application/vnd.microsoft.card.adaptive",
                        "content": {
                            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                            "type": "AdaptiveCard",
                            "version": "1.4",
                            "body": [{"type": "TextBlock", "text": "AVAIL connection test — OK", "wrap": True}],
                        },
                    }
                ],
            },
            timeout=15,
        )
        if resp.status_code not in (200, 202):
            raise ValueError(f"Teams webhook returned {resp.status_code}: {resp.text[:200]}")
        return [{"vendor_name": "Teams", "mpn_matched": "Message posted", "status": "ok"}]


class LushaTestConnector:
    """Test Lusha API key with a lightweight person lookup."""

    async def search(self, mpn: str) -> list[dict]:
        from ..http_client import http

        api_key = get_credential_cached("lusha_enrichment", "LUSHA_API_KEY")
        if not api_key:
            raise ValueError("LUSHA_API_KEY not configured")
        resp = await http.get(
            "https://api.lusha.com/v2/person",
            headers={"api_key": api_key, "Content-Type": "application/json"},
            params={"email": "test@example.com"},
            timeout=15,
        )
        # 200 = found, 404 = unknown person but API key works — both mean live
        if resp.status_code not in (200, 404):
            raise ValueError(f"Lusha API returned {resp.status_code}: {resp.text[:200]}")
        status_msg = "Person found" if resp.status_code == 200 else "API key valid (no match)"
        return [{"vendor_name": "Lusha", "mpn_matched": status_msg, "status": "ok"}]


class HunterTestConnector:
    """Test Hunter.io API key with a lightweight domain search."""

    async def search(self, mpn: str) -> list[dict]:
        api_key = get_credential_cached("hunter_enrichment", "HUNTER_API_KEY")
        if not api_key:
            raise ValueError("HUNTER_API_KEY not configured")
        from ..connectors.hunter import HunterConnector

        contacts = await HunterConnector(api_key).domain_search("anthropic.com", limit=1)
        count = len(contacts)
        return [
            {"vendor_name": "Hunter.io", "mpn_matched": f"API key valid — {count} contact(s) found", "status": "ok"}
        ]


class ClayTestConnector:
    """Test Clay MCP connectivity via a credits check (spends no enrichment credits).

    Runs the full OAuth + MCP handshake through clay_mcp._mcp_call and calls the get-
    credits-available tool. Raises (→ health status 'error') when Clay is not connected
    or the session cannot be established, so the connectors card honestly reflects an
    expired/disconnected Clay rather than silently going stale.
    """

    async def search(self, mpn: str) -> list[dict]:
        from ..connectors import clay_mcp
        from ..services import clay_oauth

        if not clay_oauth.is_connected():
            raise ValueError("Clay not connected — connect at Settings → Connectors")
        result = await clay_mcp._mcp_call("get-credits-available", {})
        if not result:
            raise ValueError("Clay MCP health check failed — reconnect may be required")
        return [{"vendor_name": "Clay", "mpn_matched": "MCP session OK — credits available", "status": "ok"}]


class ExploriumTestConnector:
    """Test Explorium API key with a business match call."""

    async def search(self, mpn: str) -> list[dict]:
        from ..http_client import http

        api_key = get_credential_cached("explorium_enrichment", "EXPLORIUM_API_KEY")
        if not api_key:
            raise ValueError("EXPLORIUM_API_KEY not configured")
        resp = await http.post(
            "https://api.explorium.ai/v1/match/business",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"domain": "anthropic.com"},
            timeout=15,
        )
        if resp.status_code != 200:
            raise ValueError(f"Explorium API returned {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        name = data.get("firmo_name", data.get("name", "matched"))
        return [{"vendor_name": "Explorium", "mpn_matched": f"Match: {name}", "status": "ok"}]


class AzureOAuthTestConnector:
    """Test Azure tenant by fetching OpenID configuration."""

    async def search(self, mpn: str) -> list[dict]:
        from ..http_client import http

        tenant_id = settings.azure_tenant_id
        if not tenant_id:
            raise ValueError("AZURE_TENANT_ID not configured")
        url = f"https://login.microsoftonline.com/{tenant_id}/v2.0/.well-known/openid-configuration"
        resp = await http.get(url, timeout=10)
        if resp.status_code != 200:
            raise ValueError(f"Azure OpenID discovery returned {resp.status_code}")
        issuer = resp.json().get("issuer", "")
        if tenant_id not in issuer:
            raise ValueError(f"Tenant mismatch in issuer: {issuer}")
        return [{"vendor_name": "Azure OAuth", "mpn_matched": "Tenant verified", "status": "ok"}]


def get_connector_for_source(name: str, db: Session = None):
    """Instantiate the right connector for a source name.

    Checks DB credentials first, falls back to env vars.
    """
    from ..connectors.digikey import DigiKeyConnector
    from ..connectors.ebay import EbayConnector
    from ..connectors.element14 import Element14Connector
    from ..connectors.mouser import MouserConnector
    from ..connectors.oemsecrets import OEMSecretsConnector
    from ..connectors.sourcengine import SourcengineConnector
    from ..connectors.sources import BrokerBinConnector, NexarConnector
    from ..services.credential_service import get_credential

    def _cred(var_name):
        if db:
            return get_credential(db, name, var_name)
        return os.getenv(var_name) or None

    nexar_id = _cred("NEXAR_CLIENT_ID")
    nexar_sec = _cred("NEXAR_CLIENT_SECRET")
    octopart_key = _cred("OCTOPART_API_KEY")
    if name == "nexar" and (nexar_id or octopart_key):
        return NexarConnector(nexar_id, nexar_sec, octopart_key)

    bb_key = _cred("BROKERBIN_API_KEY")
    bb_sec = _cred("BROKERBIN_API_SECRET")
    if name == "brokerbin" and bb_key:
        return BrokerBinConnector(bb_key, bb_sec)

    ebay_id = _cred("EBAY_CLIENT_ID")
    ebay_sec = _cred("EBAY_CLIENT_SECRET")
    if name == "ebay" and ebay_id:
        return EbayConnector(ebay_id, ebay_sec)

    dk_id = _cred("DIGIKEY_CLIENT_ID")
    dk_sec = _cred("DIGIKEY_CLIENT_SECRET")
    if name == "digikey" and dk_id:
        return DigiKeyConnector(dk_id, dk_sec)

    mouser_key = _cred("MOUSER_API_KEY")
    if name == "mouser" and mouser_key:
        return MouserConnector(mouser_key)

    oem_key = _cred("OEMSECRETS_API_KEY")
    if name == "oemsecrets" and oem_key:
        return OEMSecretsConnector(oem_key)

    src_key = _cred("SOURCENGINE_API_KEY")
    if name == "sourcengine" and src_key:
        return SourcengineConnector(src_key)

    e14_key = _cred("ELEMENT14_API_KEY")
    if name == "element14" and e14_key:
        return Element14Connector(e14_key)

    if name == "email_mining" and get_effective_flag(db, "email_mining_enabled", settings.email_mining_enabled):
        return EmailMiningTestConnector()

    # AI live web search — keyless from the operator's view, but it runs on the
    # Anthropic key (stored under the anthropic_ai source). Wire it so its Test
    # actually exercises the connector instead of silently resolving to None
    # (which the old code swallowed → a keyless card that falsely reported OK).
    if name == "ai_live_web":
        from ..connectors.ai_live_web import AIWebSearchConnector

        ai_key = get_credential(db, "anthropic_ai", "ANTHROPIC_API_KEY") if db else os.getenv("ANTHROPIC_API_KEY")
        if ai_key:
            return AIWebSearchConnector(ai_key)
        return None

    test_connector = {
        "anthropic_ai": AnthropicTestConnector,
        "teams_notifications": TeamsTestConnector,
        "lusha_enrichment": LushaTestConnector,
        "explorium_enrichment": ExploriumTestConnector,
        "azure_oauth": AzureOAuthTestConnector,
        "hunter_enrichment": HunterTestConnector,
        "clay_enrichment": ClayTestConnector,
    }.get(name)
    if test_connector:
        return test_connector()

    return None


def source_has_test_path(name: str, db: Session = None) -> bool:
    """True when a live Test probe can actually run for this source.

    Single source of truth for testability: a test path exists iff
    ``get_connector_for_source`` can build a connector (credential present, keyless
    test hook available, etc.). The connectors UI uses this to decide whether to show
    the Test button — previously any keyless source claimed testable, so ai_live_web /
    sam_gov_enrichment / stock_list_import rendered a Test button that silently no-op'd
    and falsely reported OK. Instantiation is cheap (no network) — it only reads
    credentials/flags — so this is safe to call per-source on render.
    """
    return get_connector_for_source(name, db) is not None
