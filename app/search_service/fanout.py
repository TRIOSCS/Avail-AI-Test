"""Search package — connector fan-out: config/credential cache, connector construction,
source-health summary, the parallel _fetch_fresh orchestrator with its smart-AI trigger,
and the shared budget/stat helpers.

W4.5a split of app/search_service.py — pure structural move (see cache.py header).
"""

import asyncio
import os
import time
from datetime import UTC, datetime
from typing import Final

from loguru import logger
from sqlalchemy.orm import Session

from ..connectors.ai_live_web import AIWebSearchConnector
from ..connectors.digikey import DigiKeyConnector
from ..connectors.ebay import EbayConnector
from ..connectors.element14 import Element14Connector
from ..connectors.mouser import MouserConnector
from ..connectors.oemsecrets import OEMSecretsConnector
from ..connectors.sourcengine import SourcengineConnector
from ..connectors.sources import BrokerBinConnector, NexarConnector, _redact_secrets
from ..constants import ApiSourceStatus, SourceRunStatus
from ..models import ApiSource, Sighting
from ..services.credential_service import get_credential, get_credentials_batch
from ..utils.normalization import normalize_mpn_key
from . import cache, mpn_expansion

# Map connector class names to ApiSource.name for stats tracking
# Map connector class names to ApiSource.name for stats tracking
_CONNECTOR_SOURCE_MAP = {
    "NexarConnector": "nexar",
    "BrokerBinConnector": "brokerbin",
    "EbayConnector": "ebay",
    "DigiKeyConnector": "digikey",
    "MouserConnector": "mouser",
    "OEMSecretsConnector": "oemsecrets",
    "SourcengineConnector": "sourcengine",
    "Element14Connector": "element14",
    "AIWebSearchConnector": "ai_live_web",
}


def should_trigger_ai_search(
    api_result_count: int,
    has_price_below_target: bool,
    is_obsolete: bool,
    months_since_last_sighting: float | None,
    manual_trigger: bool = False,
) -> bool:
    """Decide whether to fire the AI web search connector.

    Returns True when API results are thin, prices are above target, the part is
    obsolete, sightings are stale, or the user asked explicitly. This avoids wasting AI
    credits when conventional connectors already returned rich, actionable data.
    """
    if manual_trigger:
        return True
    if api_result_count < 5:
        return True
    if not has_price_below_target:
        return True
    if is_obsolete:
        return True
    if months_since_last_sighting is not None and months_since_last_sighting >= 6:
        return True
    return False


# ── Private helpers ──────────────────────────────────────────────────────


def _make_stat(source_name: str, status: SourceRunStatus | str, error: str | None = None) -> dict:
    """Build a source stat entry.

    Accepts a SourceRunStatus enum or its string value; normalizes to string for
    downstream JSON serialization.
    """
    status_str = status.value if isinstance(status, SourceRunStatus) else status
    return {"source": source_name, "results": 0, "ms": 0, "error": error, "status": status_str}


# In-process cache for the connector config lookups (disabled/errored source sets +
# batched credentials) that _build_connectors otherwise re-queries on EVERY search.
# 60s TTL bounds staleness: an operator who disables a source or rotates a credential
# waits at most this long to see it take effect on the next search (or call
# _reset_connector_config_cache() to force it immediately). A no-op under TESTING=1 so
# tests stay deterministic without needing the reset hook.
_CONNECTOR_CONFIG_TTL_S: Final[float] = 60.0
_connector_config_cache: dict | None = None
_connector_config_cache_at: float = 0.0


def _reset_connector_config_cache() -> None:
    """Clear the in-process connector-config cache.

    Call after any settings/credential/source-status mutation (Settings → Sources
    enable/disable, credential rotation) so the next search sees fresh config
    immediately instead of waiting out the 60s TTL. Also the test-fixture hook for
    forcing a clean cache between tests that DO want to exercise the cache itself.
    """
    global _connector_config_cache, _connector_config_cache_at
    _connector_config_cache = None
    _connector_config_cache_at = 0.0


def _load_connector_config(db: Session) -> dict:
    """Fetch the disabled/errored source sets + batched credentials _build_connectors
    needs, from a 60s in-process cache when fresh (no-op under TESTING=1).

    This is the 2-3 DB round trips _build_connectors previously ran on EVERY search
    (including every keystroke-driven interactive MPN search) — a buyer session with
    several searches in a row was re-querying identical, rarely-changing config each
    time.
    """
    global _connector_config_cache, _connector_config_cache_at
    testing = bool(os.environ.get("TESTING"))
    now = time.monotonic()
    if (
        not testing
        and _connector_config_cache is not None
        and (now - _connector_config_cache_at) < _CONNECTOR_CONFIG_TTL_S
    ):
        return _connector_config_cache

    disabled_sources = {src.name for src in db.query(ApiSource).filter_by(status=ApiSourceStatus.DISABLED.value).all()}
    errored_sources = {src.name for src in db.query(ApiSource).filter_by(status=ApiSourceStatus.ERROR.value).all()}

    # Batch-load all credentials in a single DB query
    creds = get_credentials_batch(
        db,
        [
            ("nexar", "NEXAR_CLIENT_ID"),
            ("nexar", "NEXAR_CLIENT_SECRET"),
            ("nexar", "OCTOPART_API_KEY"),
            ("brokerbin", "BROKERBIN_API_KEY"),
            ("brokerbin", "BROKERBIN_API_SECRET"),
            ("ebay", "EBAY_CLIENT_ID"),
            ("ebay", "EBAY_CLIENT_SECRET"),
            ("digikey", "DIGIKEY_CLIENT_ID"),
            ("digikey", "DIGIKEY_CLIENT_SECRET"),
            ("mouser", "MOUSER_API_KEY"),
            ("oemsecrets", "OEMSECRETS_API_KEY"),
            ("sourcengine", "SOURCENGINE_API_KEY"),
            ("element14", "ELEMENT14_API_KEY"),
        ],
    )

    config = {"disabled_sources": disabled_sources, "errored_sources": errored_sources, "creds": creds}
    if not testing:
        _connector_config_cache = config
        _connector_config_cache_at = now
    return config


def _build_connectors(db: Session) -> tuple[list, dict[str, dict], set[str]]:
    """Build enabled connectors with credentials, returning (connectors,
    source_stats_map, disabled_sources).

    Sources with status='disabled' or status='error' (set by health_monitor) are
    excluded; their entries are seeded into source_stats_map with 'disabled' or
    'error_skipped' chips so the UI renders them. Config (disabled/errored sets +
    credentials) comes from the 60s in-process cache in _load_connector_config —
    connector INSTANCES below are always constructed fresh per call.
    """
    config = _load_connector_config(db)
    disabled_sources = config["disabled_sources"]
    errored_sources = config["errored_sources"]
    creds = config["creds"]

    def _c(source_name, var_name):
        return creds.get((source_name, var_name))

    connectors = []
    source_stats_map: dict[str, dict] = {}

    def _add_or_skip(source_name, has_creds, connector_factory):
        if source_name in disabled_sources:
            source_stats_map[source_name] = _make_stat(source_name, SourceRunStatus.DISABLED)
        elif source_name in errored_sources:
            # health_monitor flipped status to 'error' on a prior raise — exclude
            # from this run so we don't keep DOSing a known-broken upstream.
            source_stats_map[source_name] = _make_stat(
                source_name,
                SourceRunStatus.ERROR_SKIPPED,
                "Skipped due to prior error — auto-recovers when next ping returns 200; rotate credentials if persistent",
            )
        elif not has_creds:
            source_stats_map[source_name] = _make_stat(source_name, SourceRunStatus.SKIPPED, "No API key configured")
        else:
            connectors.append(connector_factory())

    nexar_id = _c("nexar", "NEXAR_CLIENT_ID")
    nexar_sec = _c("nexar", "NEXAR_CLIENT_SECRET")
    octopart_key = _c("nexar", "OCTOPART_API_KEY")
    _add_or_skip(
        "nexar", nexar_id and nexar_sec or octopart_key, lambda: NexarConnector(nexar_id, nexar_sec, octopart_key)
    )

    bb_key = _c("brokerbin", "BROKERBIN_API_KEY")
    bb_sec = _c("brokerbin", "BROKERBIN_API_SECRET")
    # BrokerBin v2.x uses Bearer auth — only the API key is required. The
    # bb_sec slot is retained for legacy Basic-auth keys but is ignored at
    # request time.
    _add_or_skip("brokerbin", bb_key, lambda: BrokerBinConnector(bb_key, bb_sec))

    ebay_id = _c("ebay", "EBAY_CLIENT_ID")
    ebay_sec = _c("ebay", "EBAY_CLIENT_SECRET")
    _add_or_skip("ebay", ebay_id and ebay_sec, lambda: EbayConnector(ebay_id, ebay_sec))

    dk_id = _c("digikey", "DIGIKEY_CLIENT_ID")
    dk_sec = _c("digikey", "DIGIKEY_CLIENT_SECRET")
    _add_or_skip("digikey", dk_id and dk_sec, lambda: DigiKeyConnector(dk_id, dk_sec))

    mouser_key = _c("mouser", "MOUSER_API_KEY")
    _add_or_skip("mouser", mouser_key, lambda: MouserConnector(mouser_key))

    oem_key = _c("oemsecrets", "OEMSECRETS_API_KEY")
    _add_or_skip("oemsecrets", oem_key, lambda: OEMSecretsConnector(oem_key))

    src_key = _c("sourcengine", "SOURCENGINE_API_KEY")
    _add_or_skip("sourcengine", src_key, lambda: SourcengineConnector(src_key))

    e14_key = _c("element14", "ELEMENT14_API_KEY")
    _add_or_skip("element14", e14_key, lambda: Element14Connector(e14_key))

    return connectors, source_stats_map, disabled_sources


# Canonical display names for the live-market connectors (used by the dossier
# degraded-state banner). Keys must match _CONNECTOR_SOURCE_MAP values.
_MARKET_SOURCE_DISPLAY = {
    "nexar": "Nexar",
    "brokerbin": "BrokerBin",
    "ebay": "eBay",
    "digikey": "DigiKey",
    "mouser": "Mouser",
    "oemsecrets": "OEMSecrets",
    "sourcengine": "Sourcengine",
    "element14": "element14",
}


def get_market_source_health(db: Session) -> dict:
    """Summarize live-market connector health for the dossier degraded-state banner.

    Reuses _build_connectors so the truth is identical to what an actual search runs.
    Returns::

        {
          "available": int,          # market connectors that will run
          "total": int,              # configured market sources (available + down)
          "down": [{name, display, reason}],          # health_monitor flagged ERROR
          "unconfigured": [{name, display, reason}],   # no API key set
        }

    `down` sources are the actionable ones — auth/quota errors the operator must fix
    by rotating credentials (or restoring quota) in Settings → Sources. `disabled`
    sources are intentional operator choices and are NOT surfaced as a problem.

    Called by: routers/part_dossier.dossier_market (banner context).
    """
    connectors, source_stats_map, _disabled = _build_connectors(db)

    available = [
        _CONNECTOR_SOURCE_MAP.get(c.__class__.__name__, "")
        for c in connectors
        if _CONNECTOR_SOURCE_MAP.get(c.__class__.__name__, "") in _MARKET_SOURCE_DISPLAY
    ]

    down: list[dict] = []
    unconfigured: list[dict] = []
    for name, stat in source_stats_map.items():
        if name not in _MARKET_SOURCE_DISPLAY:
            continue
        entry = {"name": name, "display": _MARKET_SOURCE_DISPLAY[name], "reason": stat.get("error") or ""}
        status = stat.get("status")
        if status in (SourceRunStatus.ERROR_SKIPPED.value, SourceRunStatus.ERROR.value):
            down.append(entry)
        elif status == SourceRunStatus.SKIPPED.value:
            unconfigured.append(entry)
        # SourceRunStatus.DISABLED → intentional; not a degraded-state problem.

    return {
        "available": len(available),
        "total": len(available) + len(down),
        "down": down,
        "unconfigured": unconfigured,
    }


def _flatten_dedupe_filter_junk(raw: list[dict]) -> list[dict]:
    """Flatten already-collected raw connector hits, dedupe by (vendor, mpn_key, sku),
    and drop junk vendors (no-seller placeholders etc).

    Shared by ``_fetch_fresh`` (multi-connector x multi-PN fan-out) and
    ``stream_search_mpn`` (single-PN SSE fan-out) so both write byte-compatible
    payloads into the shared 15-min search-result Redis cache (``_search_cache_key`` /
    ``_get_search_cache`` / ``_set_search_cache``) — a streaming search's results become
    a cache hit for a later requisition search of the same MPN, and vice versa.
    """
    from ..shared_constants import JUNK_VENDORS

    seen: set[tuple] = set()
    out = []
    for r in raw:
        key = (
            r.get("vendor_name", "").lower(),
            normalize_mpn_key(r.get("mpn_matched", "")),
            str(r.get("vendor_sku") or "").lower(),
        )
        if key not in seen:
            seen.add(key)
            out.append(r)
    return [r for r in out if r.get("vendor_name", "").strip().lower() not in JUNK_VENDORS]


def _aggregate_source_stats(stats_updates: list[tuple[str, int, int, str | None]]) -> dict[str, dict]:
    """Aggregate per-connector-call (source, hits, ms, error) tuples into one row per
    source.

    A source that ran more than once in a single fan-out (e.g. multiple PNs, or
    multiple streaming rounds) reports summed results / max latency / first error.
    Shared by ``_fetch_fresh`` and ``stream_search_mpn`` so both compute identical
    per-source stats for the shared search-result cache and the ApiSource telemetry
    flush.
    """
    agg: dict[str, dict] = {}
    for source_name, hit_count, elapsed_ms, error in stats_updates:
        if not source_name:
            continue
        if source_name in agg:
            agg[source_name]["results"] += hit_count
            agg[source_name]["ms"] = max(agg[source_name]["ms"], elapsed_ms)
            if error and not agg[source_name]["error"]:
                agg[source_name]["error"] = error
                agg[source_name]["status"] = SourceRunStatus.ERROR.value
        else:
            agg[source_name] = {
                "source": source_name,
                "results": hit_count,
                "ms": elapsed_ms,
                "error": error,
                "status": SourceRunStatus.ERROR.value if error else SourceRunStatus.OK.value,
            }
    return agg


async def _fetch_fresh(pns: list[str], db: Session) -> tuple[list[dict], list[dict]]:
    """Run all enabled connectors against pns and return (results, source_stats).

    source_stats[i] follows SourceRunStatus: 'ok' (ran successfully), 'error' (this run
    failed), 'error_skipped' (excluded because health_monitor previously flipped
    api_sources.status to 'error' — auto-recovers on next ping success), 'skipped' (no
    creds), or 'disabled' (operator turned the source off).

    Each returned result dict carries ``_source_age_hours`` (0.0 for a live fetch; the
    real elapsed time since the write for a Redis cache HIT) so scoring can give a
    stale cache-served row honest freshness credit instead of always assuming age 0.
    """
    connectors, source_stats_map, disabled_sources = _build_connectors(db)

    # AI live web search — held back for conditional trigger (smart AI trigger)
    ai_key = get_credential(db, "anthropic_ai", "ANTHROPIC_API_KEY")
    has_ai_live = bool(ai_key) and not bool(os.environ.get("TESTING"))
    ai_connector = None
    if "ai_live_web" in disabled_sources:
        source_stats_map["ai_live_web"] = _make_stat("ai_live_web", SourceRunStatus.DISABLED)
    elif not has_ai_live:
        source_stats_map["ai_live_web"] = _make_stat("ai_live_web", SourceRunStatus.SKIPPED, "No API key configured")
    else:
        ai_connector = AIWebSearchConnector(ai_key)

    if not connectors:
        return [], list(source_stats_map.values())

    # Check search cache (keyed by PNs + active connector set)
    active_names = sorted(_CONNECTOR_SOURCE_MAP.get(c.__class__.__name__, "") for c in connectors)
    cache_key = cache._search_cache_key(pns, active_names)
    # Sync Redis GET off the event loop — a slow/unreachable Redis must not block
    # every other in-flight request on the single loop (PERF-2). The helper stays
    # best-effort (swallows RedisError internally), so no new exception escapes here.
    cached = await asyncio.to_thread(cache._get_search_cache, cache_key)
    if cached is not None:
        cached_results, cached_stats, cached_at_iso = cached
        cache_age_hours = cache._cache_age_hours(cached_at_iso)
        for r in cached_results:
            r["_source_age_hours"] = cache_age_hours
        # Merge cached stats with disabled/skipped entries
        cached_stats_map = {s["source"]: s for s in cached_stats}
        source_stats_map.update(cached_stats_map)
        logger.info(
            "Search cache HIT for {} ({} results, {:.2f}h old)",
            pns[0] if pns else "?",
            len(cached_results),
            cache_age_hours,
        )
        return cached_results, list(source_stats_map.values())

    # Run ALL connectors × ALL part numbers in parallel.
    # IMPORTANT: Stats are collected in a plain list (not written to DB) during
    # gather, because the SQLAlchemy session is not safe for concurrent access.
    stats_updates = []  # (source_name, hit_count, elapsed_ms, error_str|None)

    async def _run_one(conn, pn):
        """Run a single connector for a single PN.

        No DB access here.
        """
        source_name = _CONNECTOR_SOURCE_MAP.get(conn.__class__.__name__)
        start = time.time()
        try:
            hits = await conn.search(pn)
            elapsed_ms = int((time.time() - start) * 1000)
            for r in hits:
                r["mpn_matched"] = pn
            if source_name:
                stats_updates.append((source_name, len(hits), elapsed_ms, None))
            return hits
        except Exception as e:
            elapsed_ms = int((time.time() - start) * 1000)
            logger.opt(exception=True).error(
                "Search {} via {} failed ({}ms): {}", pn, conn.__class__.__name__, elapsed_ms, _redact_secrets(str(e))
            )
            if source_name:
                stats_updates.append((source_name, 0, elapsed_ms, _redact_secrets(str(e))[:500]))
            return []

    # Fire all connector×PN combos in parallel (with concurrency limit)
    from ..config import settings

    sem = asyncio.Semaphore(settings.search_concurrency_limit)

    async def _throttled(conn, pn):
        async with sem:
            return await _run_one(conn, pn)

    pairs = [(conn, pn) for pn in pns for conn in connectors]
    task_objs = [asyncio.create_task(_throttled(conn, pn)) for conn, pn in pairs]

    # Bounded deadline: one slow/hung connector must not block the orchestrator.
    # Tasks still pending when the budget expires are cancelled and recorded as
    # errored in stats_updates. CancelledError is a BaseException in 3.8+, so
    # _run_one's except-Exception doesn't swallow it — pending tasks finish
    # cancelled rather than returning [] and are skipped in results_lists below.
    if task_objs:
        _done, pending = await asyncio.wait(task_objs, timeout=settings.search_total_timeout_s)
    else:
        pending = set()
    if pending:
        logger.warning(
            "Search budget {:.1f}s exceeded; cancelling {}/{} pending connector tasks",
            settings.search_total_timeout_s,
            len(pending),
            len(task_objs),
        )
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        budget_ms = int(settings.search_total_timeout_s * 1000)
        pending_set = set(pending)
        for (conn, _pn), t in zip(pairs, task_objs):
            if t in pending_set:
                source_name = _CONNECTOR_SOURCE_MAP.get(conn.__class__.__name__)
                if source_name:
                    stats_updates.append((source_name, 0, budget_ms, "search budget exceeded"))

    results_lists: list = []
    for t in task_objs:
        if t.cancelled():
            continue
        exc = t.exception()
        if exc is not None:
            results_lists.append(exc)
        else:
            results_lists.append(t.result())

    # Apply stats to DB in one pass — safe, sequential, after gather completes
    try:
        source_names = {s[0] for s in stats_updates if s[0]}
        src_map = (
            {s.name: s for s in db.query(ApiSource).filter(ApiSource.name.in_(source_names)).all()}
            if source_names
            else {}
        )
        for source_name, hit_count, elapsed_ms, error in stats_updates:
            src = src_map.get(source_name)
            if not src:
                continue
            src.total_searches = (src.total_searches or 0) + 1
            src.total_results = (src.total_results or 0) + hit_count
            if not error:
                src.last_success = datetime.now(UTC)
                prev = src.avg_response_ms or elapsed_ms
                src.avg_response_ms = (prev * 3 + elapsed_ms) // 4
                src.status = ApiSourceStatus.LIVE.value
                src.last_error = None
            else:
                src.last_error = error
                src.last_error_at = datetime.now(UTC)
                src.error_count_24h = (src.error_count_24h or 0) + 1
        db.commit()
    except Exception as e:
        logger.warning("API source stats update failed: {}", e)
        db.rollback()

    # Flatten, dedupe, and drop junk vendors
    raw = []
    for result in results_lists:
        if isinstance(result, list):
            raw.extend(result)
        # If it's an exception from gather, skip it

    out = _flatten_dedupe_filter_junk(raw)
    seen = {
        (
            r.get("vendor_name", "").lower(),
            normalize_mpn_key(r.get("mpn_matched", "")),
            str(r.get("vendor_sku") or "").lower(),
        )
        for r in out
    }

    # ── Smart AI trigger: conditionally fire AI connector ────────────
    if ai_connector is not None:
        api_result_count = len(out)
        has_price_below_target = any(r.get("unit_price") is not None and r["unit_price"] > 0 for r in out)
        # Check obsolete status from MaterialCard if available
        is_obsolete = mpn_expansion._any_pn_obsolete(db, pns)

        # Months since last sighting for primary PN.
        # NOTE: Sighting has no `mpn` column — the stored fields are
        # `mpn_matched` (raw MPN as returned by the connector) and
        # `normalized_mpn` (canonical dedup key from normalize_mpn_key).
        # Use the normalized key + the indexed column so the lookup is both
        # correct and uses the Sighting.normalized_mpn index.
        months_since_last_sighting = None
        normalized_pns = [k for k in (normalize_mpn_key(pn) for pn in pns) if k]
        latest_sighting = (
            db.query(Sighting)
            .filter(Sighting.normalized_mpn.in_(normalized_pns))
            .order_by(Sighting.created_at.desc())
            .first()
            if normalized_pns
            else None
        )
        if latest_sighting and latest_sighting.created_at:
            delta = (
                datetime.now(UTC) - latest_sighting.created_at.replace(tzinfo=UTC)
                if latest_sighting.created_at.tzinfo is None
                else datetime.now(UTC) - latest_sighting.created_at
            )
            months_since_last_sighting = delta.days / 30.0

        trigger = should_trigger_ai_search(
            api_result_count=api_result_count,
            has_price_below_target=has_price_below_target,
            is_obsolete=is_obsolete,
            months_since_last_sighting=months_since_last_sighting,
        )

        if trigger:
            reasons = []
            if api_result_count < 5:
                reasons.append(f"few_results({api_result_count})")
            if not has_price_below_target:
                reasons.append("no_price_below_target")
            if is_obsolete:
                reasons.append("obsolete_part")
            if months_since_last_sighting is not None and months_since_last_sighting >= 6:
                reasons.append(f"stale_sightings({months_since_last_sighting:.1f}mo)")
            logger.info(
                "AI search TRIGGERED for {}: reasons={}",
                pns[0] if pns else "?",
                ", ".join(reasons) or "manual",
            )
            # Bounded budget: this gather previously ran AFTER the main
            # asyncio.wait deadline already returned, so a slow Claude web-search
            # call was bounded only by the connector's own 60s httpx timeout —
            # it could hold the whole _fetch_fresh call open for a minute past
            # every other connector. Mirror the main fan-out's cancel-on-timeout
            # pattern (settings.ai_search_timeout_s) so one hung AI task can no
            # longer blow the search past its intended budget.
            ai_task_objs = [asyncio.create_task(_throttled(ai_connector, pn)) for pn in pns]
            if ai_task_objs:
                _ai_done, ai_pending = await asyncio.wait(ai_task_objs, timeout=settings.ai_search_timeout_s)
            else:
                ai_pending = set()
            if ai_pending:
                logger.warning(
                    "AI search budget {:.1f}s exceeded; cancelling {}/{} pending AI task(s) for {}",
                    settings.ai_search_timeout_s,
                    len(ai_pending),
                    len(ai_task_objs),
                    pns[0] if pns else "?",
                )
                for t in ai_pending:
                    t.cancel()
                await asyncio.gather(*ai_pending, return_exceptions=True)
                ai_budget_ms = int(settings.ai_search_timeout_s * 1000)
                for t in ai_pending:
                    stats_updates.append(("ai_live_web", 0, ai_budget_ms, "AI search budget exceeded"))

            ai_results_lists: list = []
            for t in ai_task_objs:
                if t.cancelled():
                    continue
                exc = t.exception()
                ai_results_lists.append(exc if exc is not None else t.result())

            for result in ai_results_lists:
                if isinstance(result, list):
                    for r in result:
                        key = (
                            r.get("vendor_name", "").lower(),
                            normalize_mpn_key(r.get("mpn_matched", "")),
                            str(r.get("vendor_sku") or "").lower(),
                        )
                        if key not in seen:
                            seen.add(key)
                            out.append(r)
        else:
            logger.info(
                "AI search SKIPPED for {} ({} results, prices_ok={}, obsolete={}, stale={})",
                pns[0] if pns else "?",
                api_result_count,
                has_price_below_target,
                is_obsolete,
                months_since_last_sighting,
            )
            source_stats_map["ai_live_web"] = _make_stat("ai_live_web", SourceRunStatus.SKIPPED)

    # Build source_stats from stats_updates (connectors that actually ran),
    # aggregated per source (a connector may run for multiple PNs).
    agg = _aggregate_source_stats(stats_updates)
    # Merge with skipped/disabled entries
    source_stats_map.update(agg)

    # Every row here came from a live connector call (or the AI gather, also live)
    # this call — real age is 0. setdefault so a cache HIT (which returns early
    # above, already tagged) never reaches this line.
    for r in out:
        r.setdefault("_source_age_hours", 0.0)

    # Cache results for subsequent searches of the same PNs — sync Redis SETEX
    # off the event loop so a slow Redis doesn't stall the loop (PERF-2).
    connector_stats = list(agg.values())
    await asyncio.to_thread(cache._set_search_cache, cache_key, out, connector_stats)

    return out, list(source_stats_map.values())


async def _await_next_within_budget(
    pending: set[asyncio.Task],
    remaining: float,
) -> tuple[set[asyncio.Task], set[asyncio.Task], set[asyncio.Task]]:
    """Await the next connector completion(s), bounded by ``remaining`` seconds.

    Isolates the streaming search's aggregate-deadline arithmetic + straggler
    cancellation (mirrors the reference requisition path ``_fetch_fresh``) so the
    interactive SSE search inherits the same bounded budget — one hung/rate-limited
    connector can no longer delay the terminal ``done`` event for minutes. Extracted
    as a small pure-ish helper so the deadline logic is unit-testable without driving
    the full SSE generator.

    Returns ``(done, still_pending, timed_out)``:
      - ``done``          — tasks that completed this round (caller renders results)
      - ``still_pending`` — tasks to await next round (empty once the budget is spent)
      - ``timed_out``     — tasks cancelled because the budget expired with work still
        running; they are already cancelled + drained here, so the caller only needs
        to publish an error/timeout chip + telemetry for each and then stop.
    """
    if remaining <= 0:
        # Budget already spent before this round — treat all remaining work as timed out.
        done: set[asyncio.Task] = set()
        still_pending = set(pending)
    else:
        done, still_pending = await asyncio.wait(pending, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
    if not done and still_pending:
        # asyncio.wait returned with nothing completed → the timeout fired with tasks
        # still running. Cancel the stragglers and drain their CancelledError so they
        # don't leak, then hand them back for chip + telemetry publication.
        for t in still_pending:
            t.cancel()
        await asyncio.gather(*still_pending, return_exceptions=True)
        return set(), set(), set(still_pending)
    return done, still_pending, set()
