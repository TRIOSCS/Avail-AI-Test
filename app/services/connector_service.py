"""Connector classification + status reconciliation for the Settings → Connectors page.

Pure helpers (no DB/IO): collapse an ApiSource's credentials + is_active + health
status into one display `state`, and classify its control type + display group.

Worker-backed sources (the browser-automation scrapers — TBF / NetComponents / ICsource)
have no direct API to ping: their work is done by a host systemd worker that writes a
heartbeat row. For these, status is computed from the worker heartbeat (worker_health)
rather than from credentials, so a worker-served source NEVER shows "no API"/"needs
setup"/"broken" just because it lacks a direct API key — it shows the worker's real
health (Worker active vs Worker down).

Called by: app/routers/htmx_views.py (settings_connectors_tab, connector_card_partial),
app/routers/sources.py (test-all). Depends on: nothing (heartbeat row is passed in).
"""

from datetime import datetime, timezone

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
_BROWSER = {"icsource", "netcomponents", "thebrokersite"}

# Explicit worker→source mapping. Each browser-backed source is served by a host
# systemd worker (avail-<key>-worker.service) that writes a heartbeat singleton row.
# The Connectors page reads that heartbeat to show the worker's real health instead of
# pinging a (non-existent) direct API. Keyed by ApiSource.name → short worker key used
# by the worker-status models / admin endpoint.
WORKER_BACKED_SOURCES: dict[str, str] = {
    "thebrokersite": "tbf",
    "netcomponents": "nc",
    "icsource": "ics",
}

# A worker heartbeat older than this is treated as stalled (the worker crashed or
# wedged). Mirrors settings.worker_heartbeat_stale_minutes (15) and the admin
# /api/admin/workers/status threshold. Kept here as a module default so the pure helper
# needs no settings import; the caller may override via stale_seconds=.
WORKER_HEARTBEAT_STALE_SECONDS = 15 * 60

_SCOPES = {"azure_oauth", "teams_notifications"}
_AI = {"anthropic_ai", "ai_live_web"}
_MANUAL = {"stock_list_import"}
# Comms providers by name (8x8/VoIP) + by category — voice/email/messaging/auth platform.
_COMMS_NAMES = {"eight_by_eight", "email_mining"}
_COMMS_CATEGORIES = {"email", "auth", "platform", "notifications", "voip", "comms"}

# Connectors on the roadmap — built-not-yet. Render as "Planned" cards with no
# credential form, no enable toggle, and no Test button.
_PLANNED = {"findchips", "future", "heilind", "lcsc", "rochester", "verical"}


def control_type(source) -> str:
    name = source.name
    # Planned check first — must take priority over key/keyless logic.
    if name in _PLANNED:
        return "planned"
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
    if name in _SCOPES or name in _COMMS_NAMES or cat in _COMMS_CATEGORIES:
        return "communications"
    return "part_sourcing"


def is_keyless(source) -> bool:
    return control_type(source) in ("keyless", "scopes") or not (source.env_vars or [])


def is_worker_backed(source) -> bool:
    """Return True when a host worker (not a direct API) serves this source."""
    return source.name in WORKER_BACKED_SOURCES


def worker_health(row, *, now: datetime | None = None, stale_seconds: int = WORKER_HEARTBEAT_STALE_SECONDS) -> dict:
    """Collapse a worker-status heartbeat row into a health verdict (pure, no IO).

    `row` is a *WorkerStatus singleton (TbfWorkerStatus / NcWorkerStatus / IcsWorkerStatus)
    or None when the row is absent. Returns a dict the connectors UI renders directly:

        healthy            — bool: heartbeat recent, running, breaker closed
        heartbeat_age_secs — int | None: seconds since last heartbeat (None if never)
        last_search_at     — datetime | None: last completed search (worker's "last run")
        problem            — str | None: human-readable reason it's unhealthy (else None)

    Unhealthy when: no row / no heartbeat / heartbeat stale / not running / breaker open.
    """
    now = now or datetime.now(timezone.utc)
    if row is None:
        return {
            "healthy": False,
            "heartbeat_age_secs": None,
            "last_search_at": None,
            "problem": "Worker has never reported in",
        }

    hb = row.last_heartbeat
    age = None
    if hb is not None:
        hb = hb if hb.tzinfo else hb.replace(tzinfo=timezone.utc)
        age = int((now - hb).total_seconds())

    last_search_at = getattr(row, "last_search_at", None)
    out = {"healthy": False, "heartbeat_age_secs": age, "last_search_at": last_search_at, "problem": None}

    if getattr(row, "circuit_breaker_open", False):
        out["problem"] = (getattr(row, "circuit_breaker_reason", None) or "").strip() or "Circuit breaker open"
        return out
    if age is None:
        out["problem"] = "Worker has never sent a heartbeat"
        return out
    if not getattr(row, "is_running", False):
        out["problem"] = "Worker is not running"
        return out
    if age > stale_seconds:
        mins = age // 60
        out["problem"] = f"No heartbeat for {mins} min — worker stalled"
        return out

    out["healthy"] = True
    return out


def connector_state(
    source,
    *,
    credential_set: bool,
    oauth_connected: bool,
    needs_reconnect: bool,
    keyless: bool,
    worker: dict | None = None,
) -> str:
    """Return the display state for a connector.

    For worker-backed sources (TBF / NetComponents / ICsource) the state is derived
    from the worker heartbeat verdict (`worker`, from worker_health()) — NOT from
    credentials — so a worker-served source never reads as "needs_setup"/"error" just
    for lacking a direct API key:
        worker_active — worker healthy (recent heartbeat, running, breaker closed)
        worker_down   — worker stalled / not running / breaker open / no heartbeat
        off           — the source is switched off by the operator (is_active=False)
    """
    # Planned connectors surface as a distinct "planned" state — no other checks apply.
    if control_type(source) == "planned":
        return "planned"

    # Worker-backed sources: health comes from the worker heartbeat, not from a key.
    if is_worker_backed(source):
        if not source.is_active:
            return "off"
        if worker and worker.get("healthy"):
            return "worker_active"
        return "worker_down"

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
