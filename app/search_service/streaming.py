"""Search package — the interactive SSE streaming search (stream_search_mpn).

W4.5a split of app/search_service.py — pure structural move (see cache.py header).
"""

import asyncio
import json
import time
from datetime import UTC, datetime

from loguru import logger

from ..connectors.sources import _redact_secrets
from ..constants import ApiSourceStatus, SourceRunStatus
from ..database import SessionLocal, engine
from ..models import ApiSource
from ..utils.normalization import fuzzy_mpn_match, normalize_mpn_key
from . import cache, dedupe, fanout, persistence, presentation


async def stream_search_mpn(search_id: str, mpn: str) -> None:
    """Stream search results via SSE as each connector completes.

    Instead of waiting for all connectors (like _fetch_fresh with asyncio.gather),
    this fires all connectors as tasks and uses asyncio.wait(FIRST_COMPLETED) to
    publish results incrementally via the SSE broker.

    Opens its own SessionLocal() so the worker is not tied to the caller's
    request session (which FastAPI closes once the response is sent).

    Always publishes a terminal "done" event so the SSE client can stop
    waiting — even on uncaught exceptions (pool exhaustion, broker errors,
    template render failures). Without this guarantee any failure mode that
    bypasses the per-connector handler would leave the browser spinner
    hanging indefinitely (the same user-visible symptom as the original
    request-session bug).

    A live (non-cache-hit) run also persists its on-target hits as
    requirement-less Sightings via ``_persist_interactive_sightings``, fired
    with ``asyncio.to_thread`` AFTER the terminal "done" event so persistence
    never delays SSE output. A cache-hit run does NOT re-persist.

    Called by: routers/htmx_views.py::search_run (POST /v2/partials/search/run)
    Depends on: _build_connectors, _incremental_dedup, services/sse_broker.broker,
                _persist_interactive_sightings
    """
    # Allow test mocks to override the broker via module-level patching
    import app.search_service as _self_mod

    from ..config import settings
    from ..services.sse_broker import broker as _broker

    active_broker = getattr(_self_mod, "broker", _broker)

    channel = f"search:{search_id}"
    accumulated: list[dict] = []
    total_results = 0
    off_target_total = 0  # hits excluded by the relevance guard (different MPN)
    sources_completed = 0
    t_start = time.time()
    # Set only on a live (non-cache-hit) run — (hits_to_persist, succeeded_source_names).
    persist_payload: tuple[list[dict], set[str]] | None = None

    db = None
    try:
        db = SessionLocal()
        try:
            connectors, source_stats_map, _disabled = fanout._build_connectors(db)

            # Publish source-status SSE events for every non-ok source so the
            # chip strip renders the right state immediately. Without this the
            # operator never sees error_skipped / disabled / skipped chips —
            # only connectors that actually run later emit per-source events.
            for _src_name, _stat in source_stats_map.items():
                _status = _stat.get("status")
                if _status and _status != SourceRunStatus.OK.value:
                    await active_broker.publish(
                        channel,
                        "source-status",
                        json.dumps(
                            {
                                "source": _stat.get("source", _src_name),
                                "status": _status,
                                "error": _stat.get("error"),
                                "results": _stat.get("results", 0),
                                "ms": _stat.get("ms", 0),
                            },
                            default=str,
                        ),
                    )

            if not connectors:
                await active_broker.publish(
                    channel,
                    "done",
                    json.dumps({"total_results": 0, "sources": 0, "elapsed_seconds": 0, "off_target": 0}),
                )
                return

            # Build vendor score lookup for scoring raw results
            from ..models import VendorCard

            vendor_cards = db.query(VendorCard.normalized_name, VendorCard.vendor_score).all()
            vendor_score_map = {vc.normalized_name: vc.vendor_score for vc in vendor_cards}

            # Shared 15-min search-result cache (the same one _fetch_fresh reads/writes
            # for requisition searches — _search_cache_key is keyed on PNs + active
            # connector set). Before this fix the interactive streaming path never
            # consulted it, so every interactive search re-hit live supplier APIs even
            # moments after an identical requisition (or streaming) search had already
            # cached the answer.
            active_names = sorted(fanout._CONNECTOR_SOURCE_MAP.get(c.__class__.__name__, "") for c in connectors)
            shared_cache_key = cache._search_cache_key([mpn], active_names)
            shared_cached = await asyncio.to_thread(cache._get_search_cache, shared_cache_key)

            stats_updates: list[tuple[str, int, int, str | None]] = []

            if shared_cached is not None:
                cached_results, cached_stats, cached_at_iso = shared_cached
                cache_age_hours = cache._cache_age_hours(cached_at_iso)
                logger.info(
                    "Streaming search cache HIT for {} ({} results, {:.2f}h old) search_id={}",
                    mpn,
                    len(cached_results),
                    cache_age_hours,
                    search_id,
                )

                on_target = []
                for r in cached_results:
                    r.setdefault("mpn_matched", mpn)
                    if fuzzy_mpn_match(mpn, r.get("mpn_matched")):
                        on_target.append(r)
                    else:
                        off_target_total += 1

                scored_hits = [presentation._score_raw_hit(r, vendor_score_map) for r in on_target]
                new_cards, _updated_cards = dedupe._incremental_dedup(scored_hits, accumulated)
                total_results = len(on_target)
                sources_completed = len(cached_stats)

                for stat in cached_stats:
                    await active_broker.publish(
                        channel,
                        "source-status",
                        json.dumps(
                            {
                                "source": stat.get("source"),
                                "status": stat.get("status", SourceRunStatus.OK.value),
                                "error": stat.get("error"),
                                "results": stat.get("results", 0),
                                "ms": stat.get("ms", 0),
                            },
                            default=str,
                        ),
                    )

                if new_cards:
                    cards_html = presentation._render_search_vendor_cards_html(
                        new_cards, search_id=search_id, start_index=0, swap_oob=False
                    )
                    await active_broker.publish(channel, "results", cards_html)
                # No ApiSource telemetry write on a cache hit — mirrors _fetch_fresh,
                # which only touches ApiSource on a live fetch.
            else:
                # Create a task per connector, tagging with source_name
                task_map: dict[asyncio.Task, str] = {}
                for conn in connectors:
                    source_name = getattr(
                        conn, "source_name", fanout._CONNECTOR_SOURCE_MAP.get(conn.__class__.__name__, "unknown")
                    )

                    async def _run(c=conn, pn=mpn):
                        t0 = time.time()
                        hits = await c.search(pn)
                        elapsed = int((time.time() - t0) * 1000)
                        return hits, elapsed

                    task = asyncio.create_task(_run())
                    task_map[task] = source_name

                pending = set(task_map.keys())

                # Raw (pre-score) on-target hits collected across the whole run — written
                # into the shared search-result cache below in the SAME flat, unscored
                # shape _fetch_fresh's cache-miss path produces, so a later requisition
                # search of this MPN (or another streaming search) gets a cache HIT.
                raw_out: list[dict] = []

                # Aggregate deadline: the interactive SSE search shares the requisition
                # path's budget. Track the remaining budget each round; when it is spent,
                # cancel the stragglers, publish a timeout chip for each, and stop — so one
                # hung/rate-limited connector cannot hold the browser spinner for minutes.
                budget_s = settings.search_total_timeout_s
                while pending:
                    remaining = budget_s - (time.time() - t_start)
                    done, pending, timed_out = await fanout._await_next_within_budget(pending, remaining)

                    if timed_out:
                        budget_ms = int(budget_s * 1000)
                        logger.warning(
                            "Streaming search budget {:.1f}s exceeded; cancelling {} pending source(s) search_id={} mpn={}",
                            budget_s,
                            len(timed_out),
                            search_id,
                            mpn,
                        )
                        for task in timed_out:
                            source_name = task_map[task]
                            sources_completed += 1
                            stats_updates.append((source_name, 0, budget_ms, "search budget exceeded"))
                            await active_broker.publish(
                                channel,
                                "source-status",
                                json.dumps(
                                    {
                                        "source": source_name,
                                        "status": SourceRunStatus.ERROR.value,
                                        "error": "search budget exceeded",
                                        "results": 0,
                                        "ms": budget_ms,
                                    },
                                    default=str,
                                ),
                            )
                        break

                    for task in done:
                        source_name = task_map[task]
                        sources_completed += 1

                        try:
                            hits, elapsed_ms = task.result()

                            # Relevance guard: keep only hits whose matched MPN is the
                            # searched part (or a close revision of it). Keyword-matching
                            # connectors — e.g. component distributors hit with a storage
                            # FRU — return rows under a DIFFERENT mpn; those are catalog
                            # noise, not offers for this part, so we exclude them rather
                            # than render a $100 component as an "offer" for an HDD.
                            # Cross-references (alternate/FRU part numbers) live in the
                            # "What we know" panel, not the live-market offer list.
                            on_target = []
                            for r in hits:
                                r.setdefault("mpn_matched", mpn)
                                if fuzzy_mpn_match(mpn, r.get("mpn_matched")):
                                    on_target.append(r)
                                else:
                                    off_target_total += 1
                            hit_count = len(on_target)
                            raw_out.extend(on_target)

                            # Score and normalize each on-target hit
                            scored_hits = [presentation._score_raw_hit(r, vendor_score_map) for r in on_target]

                            # Incremental dedup against accumulated results
                            new_cards, updated_cards = dedupe._incremental_dedup(scored_hits, accumulated)

                            # Publish source status
                            await active_broker.publish(
                                channel,
                                "source-status",
                                json.dumps(
                                    {
                                        "source": source_name,
                                        "status": SourceRunStatus.OK.value,
                                        "results": hit_count,
                                        "ms": elapsed_ms,
                                    },
                                    default=str,
                                ),
                            )

                            # Publish new result cards (HTML for sse-swap="results" — not JSON)
                            if new_cards:
                                start_idx = len(accumulated) - len(new_cards)
                                cards_html = presentation._render_search_vendor_cards_html(
                                    new_cards,
                                    search_id=search_id,
                                    start_index=start_idx,
                                    swap_oob=False,
                                )
                                await active_broker.publish(channel, "results", cards_html)

                            # Publish updated cards as OOB HTML so existing vendor-card nodes refresh
                            if updated_cards:
                                update_html = "".join(
                                    presentation._render_search_vendor_cards_html(
                                        [card],
                                        search_id=search_id,
                                        start_index=0,
                                        swap_oob=True,
                                    )
                                    for card in updated_cards
                                )
                                await active_broker.publish(channel, "card-update", update_html)

                            total_results += hit_count
                            stats_updates.append((source_name, hit_count, elapsed_ms, None))

                        except Exception as e:
                            stats_updates.append((source_name, 0, 0, _redact_secrets(str(e))[:500]))
                            logger.exception(
                                "Streaming connector failed: source={} search_id={} mpn={}",
                                source_name,
                                search_id,
                                mpn,
                            )
                            await active_broker.publish(
                                channel,
                                "source-status",
                                json.dumps(
                                    {
                                        "source": source_name,
                                        "status": SourceRunStatus.ERROR.value,
                                        "error": _redact_secrets(str(e))[:500],
                                        "results": 0,
                                        "ms": 0,
                                    },
                                    default=str,
                                ),
                            )

                # Flush per-source telemetry to ApiSource in one guarded pass (mirrors
                # _fetch_fresh) — records searches/results/latency + errors (including
                # budget-exceeded timeouts) so the interactive path is visible in admin
                # health. Best-effort: a telemetry failure must never abort the search.
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
                    logger.warning("API source stats update failed (streaming): {}", e)
                    db.rollback()

                # Write the shared search-result cache in the same flat/unscored shape
                # _fetch_fresh's cache-miss path writes, so a later requisition search
                # (or another streaming search) of this MPN gets a cache HIT.
                shared_results = fanout._flatten_dedupe_filter_junk(raw_out)
                shared_stats = list(fanout._aggregate_source_stats(stats_updates).values())
                await asyncio.to_thread(cache._set_search_cache, shared_cache_key, shared_results, shared_stats)

                # Persist after the stream finishes: a live run's deduped on-target
                # hits become requirement-less Sightings (fired post-"done" below).
                # A cache-hit run (the `if` branch above) never reaches here, so it
                # never re-persists the same results.
                succeeded_source_names = {s[0] for s in stats_updates if s[0] and not s[3]}
                persist_payload = (shared_results, succeeded_source_names)

            # Cache results for filter endpoint (15-min TTL). Also write a per-MPN
            # pointer key (search:{key}:latest → this search_id, same TTL) so the Part
            # Dossier market section can find the freshest run for an MPN without knowing
            # the search_id (cache-hit path in routers/part_dossier.dossier_market).
            try:
                rc = cache._get_search_redis()
                if rc:
                    cache_key = f"search:{search_id}:results"
                    rc.setex(cache_key, 900, json.dumps(accumulated, default=str))
                    latest_key = normalize_mpn_key(mpn)
                    if latest_key:
                        rc.setex(f"search:{latest_key}:latest", 900, search_id)
            except Exception:
                logger.exception(
                    "Failed to cache search results: search_id={} accumulated={}",
                    search_id,
                    len(accumulated),
                )

            # All connectors done
            elapsed_total = round(time.time() - t_start, 1)
            await active_broker.publish(
                channel,
                "done",
                json.dumps(
                    {
                        "total_results": total_results,
                        "sources": sources_completed,
                        "elapsed_seconds": elapsed_total,
                        "off_target": off_target_total,
                    },
                    default=str,
                ),
            )

            # Persist AFTER the terminal "done" event so this never delays SSE
            # output. Best-effort: a persistence failure must not affect a
            # search the buyer already saw complete. Never runs on a cache hit.
            if persist_payload is not None:
                hits_to_persist, succeeded_source_names = persist_payload
                if hits_to_persist:
                    try:
                        await asyncio.to_thread(
                            persistence._persist_interactive_sightings,
                            mpn,
                            hits_to_persist,
                            succeeded_source_names,
                            datetime.now(UTC),
                            engine,
                        )
                    except Exception:
                        logger.exception(
                            "Interactive sighting persistence failed: search_id={} mpn={}",
                            search_id,
                            mpn,
                        )
        finally:
            db.close()
    except Exception as e:
        # Worker died before reaching the success-path "done" publish (pool
        # exhaustion, broker outage, template render error, etc.). Without a
        # terminal event the SSE client spins forever — same symptom as the
        # original request-session bug, different trigger.
        logger.exception(
            "stream_search_mpn failed: search_id={} mpn={}",
            search_id,
            mpn,
        )
        try:
            await active_broker.publish(
                channel,
                "done",
                json.dumps(
                    {
                        "total_results": total_results,
                        "sources": sources_completed,
                        "elapsed_seconds": round(time.time() - t_start, 1),
                        "off_target": off_target_total,
                        "error": str(e)[:500],
                    },
                    default=str,
                ),
            )
        except Exception:
            logger.exception("Failed to publish error done event: search_id={}", search_id)
