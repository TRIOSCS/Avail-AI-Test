"""Connectors tab — card enrichment, single-card partial, and the Test-all sweep.

W4.8 split of the 1,032-line app/routers/htmx/settings.py — pure structural move: URLs
and behavior unchanged (same /v2/partials/settings/connectors* + connector-card paths,
same htmx-views tag); routes attach to the shared router imported from .common.

Called by: app/main.py (via the package router mount).
Depends on: app.constants, app.database, app.dependencies, app.models, app.services
    (clay_oauth, connector_service), app.template_env, routers.htmx._shared, .common,
    routers.sources (probe/persist helpers, function-level)
"""

import asyncio
import time

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ....constants import AccessKey
from ....database import get_db
from ....dependencies import require_access
from ....models import ApiSource, User
from ....services import clay_oauth
from ....template_env import template_response
from .._shared import _base_ctx
from .common import router, settings_toast

# Retired data providers — excluded from the connectors tab and the Test-all sweep.
# Single source of truth referenced by both _build_connector_groups and connectors_test_all.
_DEAD_CONNECTORS = frozenset({"rocketreach_enrichment", "clearbit_enrichment"})


def _build_connector_field(source, env_var: str, *, mask_fully: bool = False) -> dict:
    """Return {is_set, masked} for one env-var credential field.

    Reads directly from the already-loaded ``source`` (ApiSource) row rather than
    re-querying per env var. ``_build_connector_groups`` loads every ApiSource once, so
    the old ``credential_is_set``/``get_credential`` calls (a fresh SELECT each) were
    ~2 redundant queries per field, i.e. ~70 across a full render (O5). ``is_set_for`` /
    ``decrypt_from`` take the row and fall back to env vars without touching the DB.

    ``mask_fully`` renders dots ONLY (no last-4 tail) for password-type credentials —
    used for browser_login account logins (TBF/ICS). The default ``mask_value`` shows the
    last 4 chars to help identify an API key, but for a reused human account password even
    a 4-char tail in the DOM is a leak, so those are fully masked.
    """
    from ....services.credential_service import decrypt_from, is_set_for, mask_value

    is_set = is_set_for(source, env_var)
    masked = ""
    if is_set:
        if mask_fully:
            masked = "••••••••"
        else:
            plain = decrypt_from(source, env_var)
            masked = mask_value(plain) if plain else "••••••••"
    return {"is_set": is_set, "masked": masked}


def _worker_status_row(source_name: str, db):
    """Return the worker-status singleton for a worker-backed source (or None).

    Maps an ApiSource.name (thebrokersite/netcomponents/icsource) to its heartbeat model
    via connector_service.WORKER_BACKED_SOURCES, reading the id=1 singleton.
    """
    from ....models import IcsWorkerStatus, NcWorkerStatus, TbfWorkerStatus
    from ....services import connector_service

    worker_key = connector_service.WORKER_BACKED_SOURCES.get(source_name)
    model = {"tbf": TbfWorkerStatus, "nc": NcWorkerStatus, "ics": IcsWorkerStatus}.get(worker_key)
    if model is None:
        return None
    return db.get(model, 1)


def _enrich_source(source, db) -> dict:
    """Build the per-source context dict for the connectors tab."""
    from ....services import connector_service

    name = source.name
    ct = connector_service.control_type(source)
    keyless = connector_service.is_keyless(source)

    # Credential fields. browser_login logins (TBF/ICS account passwords) are fully
    # masked — no last-4 tail in the DOM.
    env_vars = source.env_vars or []
    mask_fully = ct == "browser_login"
    creds = {ev: _build_connector_field(source, ev, mask_fully=mask_fully) for ev in env_vars}
    credential_set = any(c["is_set"] for c in creds.values())

    # Clay OAuth state
    if name == "clay_enrichment":
        oauth_connected = clay_oauth.is_connected()
        needs_reconnect = clay_oauth.needs_reconnect()
    else:
        oauth_connected = False
        needs_reconnect = False

    # Worker-backed sources: derive status from the worker heartbeat, not a direct API.
    worker = None
    if connector_service.is_worker_backed(source):
        worker = connector_service.worker_health(_worker_status_row(name, db))

    state = connector_service.connector_state(
        source,
        credential_set=credential_set,
        oauth_connected=oauth_connected,
        needs_reconnect=needs_reconnect,
        keyless=keyless,
        worker=worker,
    )

    # Keyless note
    if ct == "keyless":
        if name == "ai_live_web":
            keyless_note = "No key required — uses your Anthropic key."
        elif name == "email_mining":
            # Flag connector: no credential to enter. Enablement lives in the
            # Email Mining setting on the System tab, not a key field here.
            keyless_note = "No key required — turn Email Mining on in System settings."
        else:
            keyless_note = "No key required — switch it on to use it."
    else:
        keyless_note = ""

    # Testability:
    #  - planned: never (no implementation yet)
    #  - worker-backed: never via the API-probe Test button — health is the heartbeat,
    #    not a synchronous search (the worker runs out-of-process on a schedule)
    #  - keyless: only when a real test path exists — some keyless sources
    #    (sam_gov_enrichment, stock_list_import) have no connector/test hook, so their
    #    Test button was a cosmetic no-op that falsely reported OK. Derive it from
    #    whether _get_connector_for_source can actually build a probe.
    #  - else (credentialed / oauth): has some form of access
    if ct == "planned" or worker is not None:
        testable = False
    elif keyless:
        from ....services.connector_registry import source_has_test_path

        testable = source_has_test_path(name, db)
    else:
        testable = bool(credential_set or oauth_connected)

    return {
        "id": source.id,
        "name": name,
        "display_name": source.display_name or name,
        "description": source.description or "",
        "is_active": source.is_active,
        "state": state,
        "control_type": ct,
        "env_vars": env_vars,
        "creds": creds,
        "oauth_connected": oauth_connected,
        "needs_reconnect": needs_reconnect,
        "status": source.status or "pending",
        "last_error": source.last_error or "",
        "last_success": source.last_success,
        "error_count_24h": getattr(source, "error_count_24h", 0) or 0,
        "keyless_note": keyless_note,
        "testable": testable,
        # Worker-backed health (None for direct-API/keyless/oauth sources).
        "worker": worker,
    }


def _build_connector_groups(db, request) -> list[dict]:
    """Return connector_groups list-of-group-dicts for the connectors tab context.

    Each group: {key, label, sources: [enriched source dict]}.
    Sources are bucketed by connector_service.connector_group, emitted in GROUP_ORDER,
    empty groups are dropped. Dead providers (rocketreach, clearbit) are excluded.
    """
    from ....services import connector_service

    sources = db.query(ApiSource).order_by(ApiSource.display_name).all()

    buckets: dict[str, list[dict]] = {key: [] for key, _ in connector_service.GROUP_ORDER}

    for src in sources:
        if src.name in _DEAD_CONNECTORS:
            continue
        group_key = connector_service.connector_group(src)
        if group_key not in buckets:
            group_key = "part_sourcing"
        buckets[group_key].append(_enrich_source(src, db))

    groups = []
    for key, label in connector_service.GROUP_ORDER:
        members = buckets.get(key, [])
        if members:
            groups.append({"key": key, "label": label, "sources": members})

    return groups


@router.get("/v2/partials/settings/connectors", response_class=HTMLResponse)
async def settings_connectors_tab(
    request: Request,
    user: User = Depends(require_access(AccessKey.MANAGE_CONNECTORS)),
    db: Session = Depends(get_db),
):
    """Unified Connectors tab — admins + MANAGE_CONNECTORS capability holders (SET-06).

    Replaces sources + api-keys tabs.
    """
    ctx = _base_ctx(request, user, "settings")
    ctx["connector_groups"] = _build_connector_groups(db, request)
    return template_response("htmx/partials/settings/connectors.html", ctx)


@router.get("/v2/partials/settings/connector-card/{source_id}", response_class=HTMLResponse)
async def connector_card_partial(
    source_id: int,
    request: Request,
    user: User = Depends(require_access(AccessKey.MANAGE_CONNECTORS)),
    db: Session = Depends(get_db),
):
    """Single connector card partial — used as the swap unit for toggle/test/save.

    Returns the rendered card macro for one source, or 404 if not found. Gated on
    MANAGE_CONNECTORS (admins always qualify) — this is re-GET after every card action,
    so it must honor the same gate as the tab (SET-06).
    """
    source = db.query(ApiSource).filter(ApiSource.id == source_id).first()
    if not source:
        raise HTTPException(404, f"Connector {source_id!r} not found")

    enriched = _enrich_source(source, db)
    ctx = _base_ctx(request, user, "settings")
    ctx["s"] = enriched
    return template_response("htmx/partials/settings/_connector_card_partial.html", ctx)


# Test-all budgets. Each probe is a real live search — most connectors finish in
# 15-30s, AI web search up to ~60s. Run them CONCURRENTLY (was: sequential, so >4 live
# connectors blew the client's 15s htmx timeout → the XHR aborted, every OOB card/summary
# was discarded, and the server kept burning paid quota). Bound each probe, bound the
# whole sweep under the button's raised hx-request timeout, and poll for client
# disconnect so an abandoned sweep stops burning quota.
_TEST_ALL_PROBE_TIMEOUT_S = 60.0
_TEST_ALL_OVERALL_BUDGET_S = 90.0
_TEST_ALL_DISCONNECT_POLL_S = 0.5
# Per-user Test-all cap. A sweep probes every connector at once (far heavier than a single
# 5/min per-source Test), so it gets a tighter per-minute budget to protect paid quota.
_TEST_ALL_MAX_PER_MIN = 3


@router.post("/v2/partials/settings/connectors/test-all", response_class=HTMLResponse)
async def connectors_test_all(
    request: Request,
    user: User = Depends(require_access(AccessKey.MANAGE_CONNECTORS)),
    db: Session = Depends(get_db),
):
    """Run Test for every testable + active source CONCURRENTLY and return an OOB bundle
    of refreshed cards.

    Gated on MANAGE_CONNECTORS (admins always qualify) — matches the per-source Test
    endpoint (SET-06). Non-testable / inactive / dead sources are skipped. Each probe is
    bounded by a per-probe timeout, the whole sweep by an overall budget, and the loop
    aborts early if the client disconnects — so an abandoned sweep stops burning paid
    quota. Per-source failures are tolerated (recorded as Error) and never abort the
    sweep. Network I/O overlaps across probes; status is persisted sequentially on this
    one session afterward (concurrent commits on a shared session would race).
    """
    from ....rate_limit import check_rate_limit
    from ...sources import _persist_test_result, _probe_source

    # Cost guard: a sweep fires a live probe at every connector, spending real paid quota.
    # The per-source Test is capped at 5/min (slowapi); Test-all previously had NO cap, so
    # it bypassed that entirely. Cap it per-user (a sweep is far heavier than one probe).
    if not check_rate_limit(user.id, "connectors_test_all", limit=_TEST_ALL_MAX_PER_MIN, window_seconds=60):
        resp = HTMLResponse("")
        settings_toast(
            resp,
            "Test-all is rate-limited — wait a minute before retrying (each run spends live API quota).",
            kind="error",
        )
        return resp

    sources = db.query(ApiSource).order_by(ApiSource.display_name).all()
    candidates = [
        src
        for src in sources
        if src.name not in _DEAD_CONNECTORS and src.is_active and _enrich_source(src, db)["testable"]
    ]

    async def _guarded(src):
        """Probe one source, bounded by the per-probe timeout.

        Never raises (except on outer cancellation, which marks the task cancelled).
        """
        try:
            return await asyncio.wait_for(_probe_source(src, db), timeout=_TEST_ALL_PROBE_TIMEOUT_S)
        except TimeoutError:
            ms = int(_TEST_ALL_PROBE_TIMEOUT_S * 1000)
            return {"results": [], "elapsed_ms": ms, "error": f"Test exceeded {int(_TEST_ALL_PROBE_TIMEOUT_S)}s"}

    tasks = {src.id: asyncio.create_task(_guarded(src)) for src in candidates}
    pending = set(tasks.values())
    deadline = time.monotonic() + _TEST_ALL_OVERALL_BUDGET_S
    while pending:
        if await request.is_disconnected():
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        _done, pending = await asyncio.wait(
            pending,
            timeout=min(remaining, _TEST_ALL_DISCONNECT_POLL_S),
            return_when=asyncio.FIRST_COMPLETED,
        )
    # Cancel any probes still running (budget hit or client gone) and drain them.
    for t in pending:
        t.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)

    # Persist sequentially on the shared session (no concurrent commits).
    tested: list[dict] = []
    for src in candidates:
        task = tasks[src.id]
        if task.cancelled() or not task.done():
            continue
        outcome = task.result()  # _guarded never raises for a completed task
        _persist_test_result(
            src, db, results=outcome["results"], elapsed_ms=outcome["elapsed_ms"], error=outcome["error"]
        )
        tested.append(_enrich_source(src, db))

    failed = sum(1 for s in tested if s["state"] == "error")
    ctx = _base_ctx(request, user, "settings")
    ctx["tested_sources"] = tested
    ctx["tested_count"] = len(tested)
    ctx["failed_count"] = failed
    return template_response("htmx/partials/settings/_connectors_testall.html", ctx)
