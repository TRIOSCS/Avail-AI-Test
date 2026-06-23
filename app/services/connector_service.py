"""Connector classification + status reconciliation for the Settings → Connectors page.

Pure helpers (no DB/IO): collapse an ApiSource's credentials + is_active + health
status into one display `state`, and classify its control type + display group.

Called by: app/routers/htmx_views.py (settings_connectors_tab, connector_card_partial),
app/routers/sources.py (test-all). Depends on: nothing.
"""

GROUP_ORDER: list[tuple[str, str]] = [
    ("part_sourcing", "Part Sourcing"),
    ("enrichment", "Enrichment"),
    ("ai", "AI"),
    ("communications", "Communications"),
    ("browser_workers", "Browser Workers"),
    ("manual", "Manual"),
]

_OAUTH_CLAY = {"clay_enrichment"}
_MULTI_FIELD = {"eight_by_eight"}
_BROWSER = {"icsource", "netcomponents"}
_SCOPES = {"azure_oauth", "teams"}
_AI = {"anthropic", "ai_live_web"}
_MANUAL = {"stock_list"}


def control_type(source) -> str:
    name = source.name
    if name in _OAUTH_CLAY:
        return "oauth_clay"
    if name in _MULTI_FIELD:
        return "multi_field"
    if name in _BROWSER:
        return "browser_login"
    if name in _SCOPES:
        return "scopes"
    if not (source.env_vars or []):
        return "keyless"
    return "key"


def connector_group(source) -> str:
    name, cat = source.name, (source.category or "")
    if name in _BROWSER:
        return "browser_workers"
    if name in _MANUAL:
        return "manual"
    if name in _AI:
        return "ai"
    if cat == "enrichment":
        return "enrichment"
    if name in _SCOPES or cat in ("email", "auth", "platform", "notifications"):
        return "communications"
    return "part_sourcing"


def is_keyless(source) -> bool:
    return control_type(source) in ("keyless", "scopes") or not (source.env_vars or [])


def connector_state(
    source, *, credential_set: bool, oauth_connected: bool, needs_reconnect: bool, keyless: bool
) -> str:
    if needs_reconnect:
        return "needs_reconnect"
    has_access = credential_set or oauth_connected or keyless
    if not has_access:
        return "needs_setup"
    if not source.is_active:
        return "off"
    if source.status == "error" or source.last_error:
        return "error"
    if source.status in ("live", "active"):
        return "live"
    return "untested"  # pending / unknown
