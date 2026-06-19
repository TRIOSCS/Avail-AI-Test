"""Enrichment worker — paced background loop.

Selects small batches of unenriched/retryable parts, runs each through
``enrich_card`` (verified → web_sourced → oem_sourced → ai_inferred →
not_found/not_catalogued), paces via a daily web-call budget + per-source
cooldowns, and heartbeats to ``enrichment_worker_status``.

Run: python -m app.services.enrichment_worker

Called by: enrichment-worker Docker container
Depends on: database, Redis (optional, falls back to Postgres cache)
"""

from __future__ import annotations

import asyncio
import signal
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from loguru import logger
from sqlalchemy import and_, or_
from sqlalchemy.exc import DataError, IntegrityError
from sqlalchemy.orm import Session

from app.cache import intel_cache
from app.constants import MaterialEnrichmentStatus
from app.services.authoritative_enrichment_service import (
    _connectors_in_order,
    enrich_card,
)
from app.services.enrichment_types import WebMeter
from app.utils.claude_errors import ClaudeError

if TYPE_CHECKING:
    from app.services.enrichment_worker.circuit_breaker import EnrichmentCircuitBreaker
    from app.services.enrichment_worker.config import EnrichmentWorkerConfig

_shutdown_requested = False


def _handle_shutdown(signum, frame):
    global _shutdown_requested
    _shutdown_requested = True
    logger.info(
        "Enrichment worker: shutdown signal received (signal {}) — finishing current batch then stopping",
        signum,
    )


signal.signal(signal.SIGTERM, _handle_shutdown)
signal.signal(signal.SIGINT, _handle_shutdown)


# ---------------------------------------------------------------------------
# Pure, testable helpers
# ---------------------------------------------------------------------------


def _record_heartbeat(db: Session, breaker: "EnrichmentCircuitBreaker") -> bool:
    """Refresh the liveness heartbeat on the status singleton — EVERY loop tick.

    Called at the top of the main loop (before any branch returns/sleeps) so
    ``last_heartbeat`` advances on idle, daily-cap, breaker-open and business-hours
    ticks too — not only after a non-empty batch. Without this, a perfectly alive
    worker can go ~1h between writes and a liveness monitor false-alarms "DOWN".

    Also keeps the persisted breaker flag honest: ``breaker.should_stop()`` is
    idempotent (auto-resets after cooldown), so writing it every tick clears a stale
    ``circuit_breaker_open=True`` even when the queue is empty (the non-empty-batch
    write that previously reset it never runs in that case).

    Returns the current ``breaker_open`` state so the caller can reuse it without a
    second ``should_stop()`` call.
    """
    from app.models.enrichment_worker_status import update_enrichment_worker_status

    breaker_open = breaker.should_stop()
    update_enrichment_worker_status(
        db,
        is_running=True,
        last_heartbeat=datetime.now(timezone.utc),
        circuit_breaker_open=breaker_open,
        circuit_breaker_reason=(breaker.get_trip_info()["trip_reason"] if breaker_open else None),
    )
    return breaker_open


def select_batch(db: Session, config: "EnrichmentWorkerConfig") -> list:
    """Return the next batch of cards eligible for enrichment.

    Anti-spin query — prevents re-hammering recently-failed parts:
    - ``unenriched``: always eligible.
    - ``not_found``: eligible only when ``enriched_at IS NULL`` OR older than
      ``not_found_retry_hours`` (self-heal as quotas reset daily).
    - ``not_catalogued``: eligible when ``enriched_at IS NULL`` OR older than
      ``not_catalogued_retry_days`` (long backoff — uncatalogued OEM service parts
      rarely become catalogued, so re-check infrequently).
    - ``is_internal_part``, ``deleted_at`` are excluded.
    - Ordered by ``enrich_requested_at ASC NULLS LAST, status=unenriched DESC,
      sourced_qty_90d DESC NULLS LAST, last_sourced_at DESC NULLS LAST, id``: the
      priority lane first — cards a user explicitly added (single-add stamps
      ``enrich_requested_at``; bulk/stock/email/search creation never does), FIFO among
      themselves so no stamped card starves another (ASC NULLS LAST alone yields
      stamped-first FIFO — the old leading ``IS NOT NULL DESC`` term was redundant and
      is dropped so the ordering matches ``ix_mc_demand_queue``, migration 105);
      ``run_one_batch`` clears the stamp on every batch card so a terminal
      ``not_found`` card cannot pin the lane. Then never-resolved parts drain before
      re-checks of already-terminal cards (so old ``unenriched`` parts aren't starved
      by the daily ``not_found`` re-check churn — at a raised daily cap the 22h
      re-check pool alone could otherwise consume most of a day's slots). Then TRIO's
      own demand telemetry wins: ``sourced_qty_90d`` (real 90-day sourcing volume from
      the SFDC export, one-shot import via app/management/import_demand_telemetry.py)
      then ``last_sourced_at`` recency, NULLS LAST so unmatched cards drain after
      every demanded card; ``id`` makes the order total/deterministic.
    """
    from app.models import MaterialCard

    # NOTE: eligibility is read from material_cards.enrichment_status — NOT the (unused)
    # enrichment_queue table. material_cards is the single source of enrichment state.
    now = datetime.now(timezone.utc)
    retry_cutoff = now - timedelta(hours=config.not_found_retry_hours)

    not_found_eligible = and_(
        MaterialCard.enrichment_status == MaterialEnrichmentStatus.NOT_FOUND,
        or_(
            MaterialCard.enriched_at.is_(None),
            MaterialCard.enriched_at < retry_cutoff,
        ),
    )

    not_catalogued_cutoff = now - timedelta(days=config.not_catalogued_retry_days)
    not_catalogued_eligible = and_(
        MaterialCard.enrichment_status == MaterialEnrichmentStatus.NOT_CATALOGUED,
        or_(
            MaterialCard.enriched_at.is_(None),
            MaterialCard.enriched_at < not_catalogued_cutoff,
        ),
    )

    return (
        db.query(MaterialCard)
        .filter(
            MaterialCard.deleted_at.is_(None),
            MaterialCard.is_internal_part.is_(False),
            or_(
                MaterialCard.enrichment_status == MaterialEnrichmentStatus.UNENRICHED,
                not_found_eligible,
                not_catalogued_eligible,
            ),
        )
        .order_by(
            # Priority lane: user-requested cards (single-add stamps enrich_requested_at)
            # jump the whole queue, FIFO among themselves — ASC NULLS LAST alone gives
            # stamped-first FIFO (the old redundant `IS NOT NULL DESC` leading term is
            # gone so this ORDER BY matches ix_mc_demand_queue, migration 105).
            # run_one_batch clears the stamp on every batch card, so a terminal
            # not_found card cannot pin the lane.
            MaterialCard.enrich_requested_at.asc().nullslast(),
            # Never-resolved parts drain before re-checks of already-terminal ones —
            # without this, the daily not_found re-check pool (22h backoff) would
            # compete demand-for-demand with the unenriched backlog and could consume
            # most of a day's cap on dead-enders.
            (MaterialCard.enrichment_status == MaterialEnrichmentStatus.UNENRICHED).desc(),
            # Demand telemetry (TRIO's SFDC export — migration 105, one-shot import):
            # real 90-day sourcing volume, then sourcing recency; NULLS LAST so every
            # demanded card drains before unmatched ones; id keeps the order total.
            MaterialCard.sourced_qty_90d.desc().nullslast(),
            MaterialCard.last_sourced_at.desc().nullslast(),
            MaterialCard.id,
        )
        .limit(config.batch_size)
        .all()
    )


async def _oem_resolution_pass(
    db: Session,
    batch: list,
    config: "EnrichmentWorkerConfig",
    breaker: "EnrichmentCircuitBreaker",
    web_state: dict[str, int],
    web_calls_today: int,
    web_cache_key: str,
    today_str: str,
) -> int:
    """Pass A — paced OEM spare→canonical resolution over this batch (network).

    Candidates are batch cards classified ``hpe`` (Phase A) with no fresh
    ``oem_crosswalk`` row (resolved rows are permanent; no_match rows block for 90
    days). At most ``config.oem_resolve_per_batch`` resolves per batch, each gated on
    BOTH daily caps — the web cap (every resolve is a billable web call, counted
    INSIDE ``web_daily_cap``) and the OEM sub-cap (``oem_resolve_daily_cap``, tallied
    at ``enrichment_worker:oem_resolves:{date}`` with the same max-of-cache-and-
    in-process defense as the web counter). Both counters are billed BEFORE the await
    via the atomic ``intel_cache.incr_count`` (safe against the concurrently-running
    drain CLI — no read-modify-write lost updates) and mirrored into ``web_state``
    up front, so a call that bills then raises — or a pass that dies mid-flight — is
    already counted everywhere.

    Outcomes are upserted into ``oem_crosswalk`` via the shared
    ``apply_resolution`` writer inside a SAVEPOINT: the flush is the one realistic
    non-ClaudeError raise site (a unique-key race with the drain CLI committing the
    same edge during the await, or a PG-only DataError) and must never poison the
    shared batch session — the savepoint rolls back only that row and the spare is
    skipped (the concurrent writer's row IS the desired end state).
    ``ClaudeError`` → ``breaker.record_claude_error``, NO row (the spare retries next
    batch for free). Worker-level queries between awaits are the established
    crosswalk-pass pattern — ``enrich_card``'s per-card no-query-after-await
    invariant is untouched.

    Returns the updated ``web_calls_today`` (also persisted into ``web_state``).
    """
    from app.services.enrichment_worker.oem_classifier import classify_oem_vendor
    from app.services.enrichment_worker.oem_crosswalk_resolver import resolve_oem_spare
    from app.services.oem_crosswalk_enrich import apply_resolution, pending_resolution
    from app.utils.normalization import normalize_mpn_key

    # Phase A: HP/HPE only. Dedupe by spare norm (one resolve covers every card
    # sharing it, forever).
    candidates: dict[str, str] = {}
    for card in batch:
        if classify_oem_vendor(card.display_mpn) == "hpe":
            norm = normalize_mpn_key(card.display_mpn)
            if norm and norm not in candidates:
                candidates[norm] = card.display_mpn
    if not candidates:
        return web_calls_today

    pending = pending_resolution(db, candidates.keys(), "hpe")
    if not pending:
        return web_calls_today

    oem_cache_key = f"enrichment_worker:oem_resolves:{today_str}"
    # Same defense in depth as the web counter: max of the cross-restart cache value
    # and the in-process tally, so the sub-cap holds even when the cache no-ops.
    oem_resolves_today = max(intel_cache.get_count(oem_cache_key), web_state.get("oem_resolves", 0))

    taken = 0
    for norm, stale_row in pending.items():
        if taken >= config.oem_resolve_per_batch:
            break
        if web_calls_today >= config.web_daily_cap:
            logger.info(
                "ENRICH_WORKER: oem-resolve skipped — web daily cap reached ({}/{})",
                web_calls_today,
                config.web_daily_cap,
            )
            break
        if oem_resolves_today >= config.oem_resolve_daily_cap:
            logger.info(
                "ENRICH_WORKER: oem-resolve daily cap reached ({}/{}) — pass no-ops until reset",
                oem_resolves_today,
                config.oem_resolve_daily_cap,
            )
            break
        taken += 1
        display_mpn = candidates[norm]
        # Bill BEFORE the await (the WebMeter reserve discipline, made durable):
        # incr_count advances the shared date counters atomically (no lost updates
        # against the drain CLI) and web_state mirrors them up front, so a call that
        # bills then raises — or a pass that dies mid-flight — is already counted.
        web_calls_today = max(intel_cache.incr_count(web_cache_key, ttl_days=1.0), web_calls_today + 1)
        oem_resolves_today = max(intel_cache.incr_count(oem_cache_key, ttl_days=1.0), oem_resolves_today + 1)
        web_state["web_calls"] = web_calls_today
        web_state["oem_resolves"] = oem_resolves_today
        try:
            result = await resolve_oem_spare(display_mpn, norm, "hpe")
        except ClaudeError as e:
            # Transient backend failure (incl. an unparseable response): feed the
            # breaker, write NO row — the spare is retried next batch for free.
            logger.warning("ENRICH_WORKER: oem-resolve Claude error for {}: {}", display_mpn, type(e).__name__)
            breaker.record_claude_error()
            continue
        breaker.record_claude_success()

        # Upsert via the shared writer (a stale no_match row is updated in place;
        # otherwise insert), inside a SAVEPOINT: the flush can raise IntegrityError
        # when the drain CLI committed the same edge during the await (or DataError
        # on PG), and that must roll back ONLY this row — never poison the shared
        # batch session. The batch-final commit owns durability.
        try:
            with db.begin_nested():
                row = apply_resolution(stale_row, result, display_mpn=display_mpn, spare_norm=norm, vendor="hpe")
                if stale_row is None:
                    db.add(row)
                # Flush so Pass B (same batch, same session) sees the row even on
                # autoflush=False sessions — a spare resolved this batch gets its
                # specs written in the SAME batch.
                db.flush()
        except (IntegrityError, DataError) as e:
            logger.warning(
                "ENRICH_WORKER: oem-resolve upsert for {} lost a write race or failed validation ({}) — "
                "skipping (a concurrent writer's row is the desired end state)",
                display_mpn,
                type(e).__name__,
            )
            continue
        logger.info("ENRICH_WORKER: oem-resolve {} -> {} ({})", display_mpn, result.status, result.canonical_mpn)
    return web_calls_today


def _record_partsurfer_negative(db: Session, record_negative, spare: str, norm: str, reason: str) -> None:
    """Upsert one PartSurfer-description miss inside a SAVEPOINT.

    ``record_negative`` does not flush, but the upsert can hit a unique-key race (the
    drain CLI or a sibling card committing the same spare during this batch) or a PG
    DataError; isolating it in a nested transaction keeps that off the shared batch
    session — the negative is best-effort throughput hygiene, never worth aborting the
    batch's real enrichment writes. A blank norm is a no-op inside record_negative.
    """
    if not norm:
        return
    try:
        with db.begin_nested():
            record_negative(db, spare, norm, reason)
            db.flush()
    except (IntegrityError, DataError):
        logger.warning("partsurfer-desc: negative-cache upsert lost a race for {} ({})", spare, reason)


async def _partsurfer_desc_pass(db: Session, batch: list) -> dict[str, int]:
    """PartSurfer description enrichment — categorize UNCATEGORIZED HP/HPE cards
    (network).

    For batch cards classified ``hpe`` (classify_oem_vendor) that are still UNCATEGORIZED
    (``not (card.category or "").strip()``), fetch the OEM's own verbatim description live
    from partsurfer.hpe.com and feed it into the desc grammar via ``categorize_and_record``
    at ``partsurfer_desc`` / tier 84 — so the ~70k uncategorized HP spares get a category +
    facets. PartSurfer's Product Number just echoes the spare, so the canonical-MPN
    crosswalk is useless for HP; the rich DESCRIPTION is the win.

    Placed AFTER the deterministic categorize/decode passes in run_one_batch so a fetch is
    only spent on cards STILL uncategorized. Candidates are deduped by display_mpn so the
    same spare is never fetched twice in a batch, and capped at
    ``settings.partsurfer_fetch_per_batch``. Politeness is 1 request / 2s — paced here with
    ``asyncio.sleep(2.0)`` BETWEEN fetches. ``categorize_and_record`` is fill-only (it
    no-ops on an already-categorized card), so a card a prior pass categorized this batch
    is skipped without a fetch via the NULL-category gate below.

    Resilient: on a throttle/outage ``fetch_partsurfer_description`` raises
    ``PartSurferTransient`` — the pass BREAKS (stops hammering the host this batch) rather
    than burning the remaining candidates against a struggling host; the descriptions
    already fetched are kept. Each card's ``categorize_and_record`` is wrapped per-card so
    one bad card (an IntegrityError/DataError on the shared session) can't abort the pass
    and waste the other fetched descriptions; ``categorize_and_record`` also wraps its own
    write in a per-card SAVEPOINT.

    Negative cache (durable, partsurfer_desc_negative): a spare with a FRESH negative row
    (a prior no-result / ungrammatical miss inside its retry window) is dropped BEFORE the
    fetch — so dead/ungrammatical HP spares are not re-queried every batch (the throughput
    win on the 145k not_found cards). A ``None`` no-result is cached long (90d); a fetched
    description the grammar declines is cached SHORT (14d) — a parse miss is not evidence
    the OEM lacks the part. A throttle (``PartSurferTransient``) is NEVER cached. Returns
    an aggregate {fetched, categorized, specs_written, failed, skipped_cached} summary.
    """
    from app.config import settings
    from app.services.desc_extractor._common import PARTSURFER_DESC_CONFIDENCE, PARTSURFER_DESC_SOURCE
    from app.services.desc_extractor.writer import categorize_and_record
    from app.services.enrichment_worker.oem_classifier import classify_oem_vendor
    from app.services.enrichment_worker.partsurfer_negative_cache import (
        blocked_spare_norms,
        record_negative,
    )
    from app.services.enrichment_worker.partsurfer_resolver import (
        PartSurferTransient,
        fetch_partsurfer_description,
    )
    from app.utils.normalization import normalize_mpn_key

    # Uncategorized HP/HPE cards only, deduped by display_mpn (one fetch covers every card
    # sharing the spare). dict preserves insertion order → deterministic fetch order.
    # spare_norm (one per spare) is the negative-cache key; cache the norm here so it is
    # computed once per spare and reused for both the block check and the miss write.
    candidates: dict[str, list] = {}
    spare_norm: dict[str, str] = {}
    for card in batch:
        if (card.category or "").strip():
            continue
        if classify_oem_vendor(card.display_mpn) != "hpe":
            continue
        candidates.setdefault(card.display_mpn, []).append(card)
        spare_norm.setdefault(card.display_mpn, normalize_mpn_key(card.display_mpn))
    if not candidates:
        return {"fetched": 0, "categorized": 0, "specs_written": 0, "failed": 0}

    # Drop spares with a FRESH negative row (a prior no-result / ungrammatical miss still
    # inside its retry window) so the worker never re-fetches a known-dead spare every
    # batch — the throughput win on the 145k not_found cards. A stale row is NOT blocked
    # (re-fetched; record_negative refreshes it in place).
    blocked = blocked_spare_norms(db, spare_norm.values())

    fetch_cap = settings.partsurfer_fetch_per_batch
    fetched = categorized = specs_written = failed = skipped_cached = 0
    paced = False  # whether a real fetch has run (so the 2s pace only spaces fetches)
    for spare, cards in candidates.items():
        if fetched >= fetch_cap:
            break
        norm = spare_norm[spare]
        if norm and norm in blocked:
            # Negative-cache hit — do NOT fetch (no network, no pace).
            skipped_cached += 1
            continue
        if paced:
            # 1 request / 2s politeness — between actual fetches only.
            await asyncio.sleep(2.0)
        paced = True
        try:
            desc = await fetch_partsurfer_description(spare)
        except PartSurferTransient as exc:
            # The host is throttling / down — stop hammering it for the rest of this batch.
            # The aborted fetch is NOT counted, and a throttle is NEVER negative-cached
            # (it is not a verdict on the spare); descriptions already fetched are kept.
            logger.warning("partsurfer-desc: backing off this batch — {}", exc)
            break
        fetched += 1
        if not desc:
            # Genuine no-result (404/3xx, missing/empty lblDescription) — cache long
            # (90d) so this dead spare is not re-fetched daily.
            _record_partsurfer_negative(db, record_negative, spare, norm, "no_result")
            continue
        spare_categorized = spare_failed = False
        for card in cards:
            # categorize_and_record is fill-only (no-op on an already-set category) and
            # wraps category + facets in one SAVEPOINT. Per-card try/except (mirrors
            # extract_and_record_specs) so one bad card never aborts the pass and wastes the
            # other fetched descriptions.
            try:
                was_categorized, written = categorize_and_record(
                    db, card, description=desc, source=PARTSURFER_DESC_SOURCE, confidence=PARTSURFER_DESC_CONFIDENCE
                )
            except Exception:
                failed += 1
                spare_failed = True
                logger.exception("partsurfer-desc: failed on card_id={}", card.id)
                continue
            if was_categorized:
                categorized += 1
                specs_written += written
                spare_categorized = True
        if not spare_categorized and not spare_failed:
            # A description came back but the grammar declined every card — ungrammatical/
            # opaque/truncated. NOT evidence the OEM lacks the part, so a SHORT (14d) retry,
            # never the permanent/long window. A DB failure (spare_failed) is not a grammar
            # verdict — do not cache it.
            _record_partsurfer_negative(db, record_negative, spare, norm, "ungrammatical")
    return {
        "fetched": fetched,
        "categorized": categorized,
        "specs_written": specs_written,
        "failed": failed,
        "skipped_cached": skipped_cached,
    }


async def run_one_batch(
    db: Session,
    config: "EnrichmentWorkerConfig",
    cooldown: dict[str, float],
    breaker: "EnrichmentCircuitBreaker",
    disabled: set[str] | None = None,
    web_state: dict[str, int] | None = None,
) -> dict[str, int]:
    """Enrich one batch of cards and return per-tier counts.

    LANE SPLIT (settings.enrichment_lane_split_enabled, default on): priority-lane
    cards (enrich_requested_at stamped by single-add — membership captured before the
    stamps are cleared) run the full enrich_card pipeline; bulk-lane cards run the
    FREE connectors only inside enrich_card (web/OEM/Opus tiers skipped) and still get
    every deterministic pass below (mpn-decode / fru-crosswalk / desc-parse / oem
    crosswalk Pass B) plus the in-batch spec pass when they land a real category.
    Call routing only — all writes still arbitrate through the F1 ladder.

    After the per-card core-enrichment loop completes, this also triggers a paced,
    second-pass PARAMETRIC SPEC extraction (``enrich_card_specs``) for the cards that
    landed a real category this batch (verified / web_sourced / ai_inferred) — so the
    materials filter tree gets MaterialSpecFacet + specs_structured data. It runs ONCE
    per batch (the extractor groups by category internally) on the same shared session.
    NOTE: ``enrich_card_specs`` commits PER CHUNK on that shared session (load-bearing —
    see the commit comment in spec_enrichment_service: long awaited Claude calls between
    chunks, and three callers with no commit of their own), so the batch's pending
    core-attr writes persist with its FIRST chunk commit; the batch-final commit below
    is the safety net for batches where the spec pass raises early or processes zero
    chunks — but NOT for a failed first-chunk COMMIT (the spec pass's rollback discards
    the batch's pending writes with it, leaving nothing for the safety net; those cards
    re-enter via the next batch's re-selection because enrichment_status rolled back
    too). Bounded by the worker's daily_cap (≤ daily_cap cards/day receive a spec
    pass).

    Returns an empty dict if the batch is empty (caller should idle-sleep).

    ``disabled`` accumulates sources that hit a quota/auth wall (connectors) and the
    ``"web_search"`` flag once the web budget is spent. It is owned by ``main()`` so the
    state PERSISTS across batches (a quota'd connector or an exhausted web budget stays
    disabled instead of being re-tried every 30s) and is cleared at the daily reset. When
    omitted (tests) a fresh per-call set is used.

    Web daily-budget gate: the per-day call count is read from
    ``enrichment_worker:web_calls:{date}`` in the intel_cache, but defended in depth by an
    in-process tally in ``web_state`` — the cache silently no-ops if Redis AND Postgres are
    both down, which would otherwise let WEB_DAILY_CAP be bypassed entirely. The gate is
    re-checked BEFORE each card (not once per batch). A single card may fire up to 3 billable
    web calls, so the per-card gate can overshoot the cap by at most 2; the meter is flushed
    in a finally so every dispatched call is billed even when a later tier raises. Once the
    cap is hit, ``"web_search"`` is added to ``disabled`` so ``enrich_card`` skips the web
    tier and falls through to Opus ai_inferred.

    After each card the dispatched web-call count is flushed into the cache counter and the
    ``web_state`` tally (TTL = 1 day).
    """
    batch = select_batch(db, config)
    if not batch:
        return {}
    # Captured BEFORE the per-card loop: the FRU crosswalk pass below runs over the FULL
    # batch (not enriched_ids) — FRU spare PNs are precisely the population connectors
    # miss, so they finish not_found and never reach enriched_ids.
    batch_ids = [int(c.id) for c in batch]
    # Lane membership, captured BEFORE the stamps are cleared below: priority-lane
    # cards (user single-add stamped enrich_requested_at) keep the full enrich_card
    # pipeline; bulk-lane cards run connectors + deterministic passes only when
    # settings.enrichment_lane_split_enabled (call routing only — the F1 ladder still
    # arbitrates every write).
    priority_ids = {int(c.id) for c in batch if c.enrich_requested_at is not None}

    def _clear_priority_stamps() -> None:
        # Clear the priority-lane stamp on EVERY batch card — attribute writes on
        # already-loaded ORM objects — so a card that finishes terminal (not_found)
        # cannot keep its stamp and pin the lane forever. Persisted by the batch-final
        # commit below, together with the enrichment results.
        for card in batch:
            if card.enrich_requested_at is not None:
                card.enrich_requested_at = None

    # Done immediately, BEFORE the first await (the worker's no-query-after-await
    # discipline). Re-applied after any rollback that discards the stamp clears.
    _clear_priority_stamps()

    if disabled is None:
        disabled = set()
    if web_state is None:
        web_state = {"web_calls": 0}

    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    web_cache_key = f"enrichment_worker:web_calls:{today_str}"
    # Defense in depth: take the max of the (cross-restart, cross-process) cache value
    # and the in-process tally so the cap holds even when the cache is unavailable and
    # silently no-ops.
    web_calls_today = max(intel_cache.get_count(web_cache_key), web_state.get("web_calls", 0))

    # One lazy import of the settings singleton (cheap Pydantic object), reused by every
    # feature-flag gate below — the import stays lazy to keep worker import-time light
    # and patchable, but is hoisted here so it isn't re-imported per pass.
    from app.config import settings

    # OEM web-resolution crosswalk (Pass A + Pass B) — BEFORE the per-card core loop,
    # per the crosswalk-pass pattern (worker-level queries between awaits are fine;
    # enrich_card's per-card no-query-after-await invariant is untouched).
    #
    # Pass A — paced resolution (network): resolve uncached HP/HPE spare PNs in this
    # batch via Claude-grounded PartSurfer lookup and upsert oem_crosswalk rows
    # (incl. 90-day-negative no_match rows). Bounded by oem_resolve_per_batch and
    # BOTH daily caps; every call bills the web counter (counted INSIDE web_daily_cap).
    #
    # Pass B — deterministic writer (zero network): cards whose display_mpn norm has a
    # resolved oem_crosswalk row inherit category + specs from the canonical MPN at
    # source partsurfer/psref (F1 ladder tier 80 — below fru_desc_parse 82, above
    # web_search 70; the ladder arbitrates, not run order) and get upgraded to
    # oem_sourced — which short-circuits enrich_card's early-return below and saves
    # up to 3 web calls per card. Same session, committed with the batch below.
    if batch_ids:
        if settings.oem_crosswalk_enrich_enabled:
            try:
                web_calls_today = await _oem_resolution_pass(
                    db, batch, config, breaker, web_state, web_calls_today, web_cache_key, today_str
                )
            except Exception:
                logger.exception("ENRICH_WORKER: oem-resolve pass failed over {} cards", len(batch_ids))
                if not db.is_active:
                    # The session is pending-rollback (a DB error escaped the pass's
                    # savepoint) — restore it so Pass B / _connectors_in_order / the
                    # core loop don't die on PendingRollbackError and abort the whole
                    # batch; then re-apply the stamp clears the rollback discarded.
                    db.rollback()
                    _clear_priority_stamps()
            # The pass bills the counters into web_state BEFORE each await — re-sync
            # the local tally so a swallowed pass exception can't clobber the OEM
            # increments at the per-card flushes / the web_state reconciliation below
            # (the in-process backstop must never under-count).
            web_calls_today = max(web_calls_today, web_state.get("web_calls", 0))

            from app.services.oem_crosswalk_enrich import oem_crosswalk_and_record_specs

            try:
                logger.info("ENRICH_WORKER: oem-crosswalk {}", dict(oem_crosswalk_and_record_specs(db, batch_ids)))
            except Exception:
                logger.exception("ENRICH_WORKER: oem-crosswalk failed over {} cards", len(batch_ids))

    lane_split = settings.enrichment_lane_split_enabled
    conns = _connectors_in_order(db)
    counts: dict[str, int] = {}
    # Cards that landed a real category this batch (verified / web_sourced / ai_inferred) —
    # the ones eligible for a second-pass parametric spec extraction. not_found and
    # exception (poison-pill) cards are deliberately excluded.
    enriched_ids: list[int] = []
    now = datetime.now(timezone.utc)

    for card in batch:
        # Per-card budget gate (exact — no batch_size overshoot). Once tripped, "web_search"
        # stays in the persistent disabled set until the daily reset re-enables it.
        if web_calls_today >= config.web_daily_cap and "web_search" not in disabled:
            logger.info(
                "ENRICH_WORKER: web daily cap reached ({}/{}) — disabling web tier",
                web_calls_today,
                config.web_daily_cap,
            )
            disabled.add("web_search")
        card_meter = WebMeter()
        try:
            status = await enrich_card(
                card,
                db,
                connectors=conns,
                disabled=disabled,
                cooldown=cooldown,
                web_meter=card_meter,
                # Bulk lane (no enrich_requested_at stamp): connectors + deterministic
                # passes only — web/OEM/Opus tiers are skipped inside enrich_card.
                # Priority lane keeps the full pipeline. Flag off = full pipeline for all.
                full_pipeline=(not lane_split) or int(card.id) in priority_ids,
            )
            card.enriched_at = now
            counts[status] = counts.get(status, 0) + 1

            if status not in (MaterialEnrichmentStatus.NOT_FOUND, MaterialEnrichmentStatus.NOT_CATALOGUED):
                # Landed a real category (verified / web_sourced / oem_sourced / ai_inferred)
                # — queue it for the second-pass parametric spec extraction below. not_found
                # and not_catalogued (terminal misses) are excluded, as are poison-pill cards.
                enriched_ids.append(int(card.id))

            # A Claude call (web/cross-ref/OEM/infer) returned without raising → backend
            # healthy. (A pure-connector VERIFIED hit makes no Claude call, so card_meter
            # .claude_ok stays False and must not reset the breaker.)
            if card_meter.claude_ok:
                breaker.record_claude_success()

        except ClaudeError as e:
            # Claude backend is failing — feed the circuit breaker so a sustained outage
            # trips it (sleep 1h) instead of silently marking the whole queue not_found and
            # burning API spend. The card is left unenriched and retried next batch.
            logger.warning(
                "ENRICH_WORKER: Claude error for {} ({}): {}",
                card.display_mpn,
                card.normalized_mpn,
                type(e).__name__,
            )
            breaker.record_claude_error()
        except Exception as e:
            # Non-Claude failure (a bug, a DB hiccup). Log loudly, but do NOT trip the
            # Claude-specific breaker. Quarantine the card as not_found + stamp enriched_at
            # so the not_found retry backoff applies — otherwise a poison-pill card (one
            # whose data deterministically triggers the failure) would be re-selected at
            # the front of EVERY batch (fast-lane) and spin forever. It self-heals at the
            # next retry window if the underlying bug is fixed.
            logger.error(
                "ENRICH_WORKER: enrich_card failed for {} ({}): {} — quarantining as not_found",
                card.display_mpn,
                card.normalized_mpn,
                e,
            )
            card.enrichment_status = MaterialEnrichmentStatus.NOT_FOUND
            card.enriched_at = now
            counts[MaterialEnrichmentStatus.NOT_FOUND] = counts.get(MaterialEnrichmentStatus.NOT_FOUND, 0) + 1
        finally:
            # Bill every web call that was DISPATCHED, even if a later tier raised — the
            # meter reserves before each await, so calls that fired then failed are still
            # counted (otherwise WEB_DAILY_CAP drifts over on a ClaudeError mid-card).
            # incr_count is atomic across processes (the drain CLI bills the same key);
            # the max() keeps the in-process floor when the cache no-ops.
            if card_meter.web_calls > 0:
                web_calls_today = max(
                    intel_cache.incr_count(web_cache_key, card_meter.web_calls, ttl_days=1.0),
                    web_calls_today + card_meter.web_calls,
                )

    web_state["web_calls"] = web_calls_today

    # Deterministic MPN→spec decode (storage/DRAM): zero-LLM, regex-gated, enum-validated by
    # record_spec. Run order is NOT load-bearing: record_spec's F1 tier ladder
    # (app/services/spec_tiers.py — mpn_decode 85 > fru_matrix_decode 84 > desc_parse 83 >
    # fru_desc_parse 82 > spec_extraction 60) arbitrates every write, so a downstream pass can never overwrite a
    # decode value regardless of the confidence it claims or which pass ran first. The old
    # per-writer "skip keys already held at higher confidence" pre-gates are gone — the
    # ladder owns arbitration in one place. Same session, committed together below.
    if enriched_ids:
        if settings.mpn_decode_enabled:
            from app.services.mpn_decoder.writer import decode_and_record_specs

            try:
                logger.info("ENRICH_WORKER: mpn-decode {}", decode_and_record_specs(db, enriched_ids))
            except Exception:
                logger.exception("ENRICH_WORKER: mpn-decode failed over {} cards", len(enriched_ids))

    # Deterministic FRU crosswalk enrichment — ONE pass, two channels over the same
    # batched fru_links query: (1) FRU spare PNs (IBM/Lenovo) inherit the
    # strict-intersected decode of their approved mfg_model links, source=
    # "fru_matrix_decode" (ladder tier 84 — below first-party mpn_decode 85, above
    # desc_parse 83); (2) the qual-sheet descriptions stored on their mfg_model +
    # drive_pn links run through the desc_extractor grammar and intersect, source=
    # "fru_desc_parse" (tier 82 — below desc_parse 83: the card's OWN description
    # outranks a linked row's prose; above partsurfer 80). record_spec's ladder
    # arbitrates, not run order. For cards in
    # enriched_ids, a missing category the crosswalk fills lets desc-parse pick them up
    # in the SAME batch — not_found FRU spares are NOT in enriched_ids, so they only
    # benefit from the crosswalk itself this batch. Scope is the FULL batch (batch_ids,
    # not enriched_ids): not_found/quarantined cards are safe to include — the pass is
    # deterministic and free, and it never touches enrichment_status. Same session,
    # committed together below.
    if batch_ids:
        if settings.fru_crosswalk_enrich_enabled:
            from app.services.fru_crosswalk_enrich import crosswalk_and_record_specs

            try:
                logger.info("ENRICH_WORKER: fru-crosswalk {}", crosswalk_and_record_specs(db, batch_ids))
            except Exception:
                logger.exception("ENRICH_WORKER: fru-crosswalk failed over {} cards", len(batch_ids))

    # Deterministic description→spec extraction (storage/DRAM token grammar): zero-LLM,
    # enum-validated by record_spec. source="desc_parse" (ladder tier 83): the F1 ladder —
    # not run order, and no per-writer pre-gates — guarantees it never overwrites
    # mpn_decode (85) / fru_matrix_decode (84) / vendor-API (90) values, always overrides
    # a linked-description fru_desc_parse (82) value, and always beats
    # the AI spec pass (spec_extraction, 60) regardless of the confidence either claims.
    # Same shared post-await session, committed together below.
    if enriched_ids:
        if settings.desc_parse_enabled:
            from app.services.desc_extractor.writer import extract_and_record_specs

            try:
                logger.info("ENRICH_WORKER: desc-parse {}", extract_and_record_specs(db, enriched_ids))
            except Exception:
                logger.exception("ENRICH_WORKER: desc-parse failed over {} cards", len(enriched_ids))

    # PartSurfer description enrichment (HTTP, paced): for STILL-uncategorized HP/HPE cards
    # in this batch, fetch the OEM's own verbatim description live from partsurfer.hpe.com
    # (robots-allowed, 1 GET / 2s) and feed it into the desc grammar to categorize them at
    # partsurfer_desc / tier 84. Placed AFTER the deterministic categorize/decode passes so
    # a billable fetch is only spent on cards nothing else could categorize. Gated by the
    # flag; bounded by partsurfer_fetch_per_batch. Same session, committed with the batch.
    if batch_ids:
        from app.config import settings

        if settings.partsurfer_desc_enabled:
            try:
                logger.info("ENRICH_WORKER: partsurfer-desc {}", await _partsurfer_desc_pass(db, batch))
            except Exception:
                logger.exception("ENRICH_WORKER: partsurfer-desc pass failed over {} cards", len(batch_ids))

    # Second pass: parametric spec extraction for cards that landed a real category this
    # batch. Runs ONCE per batch (the extractor groups by category internally) on the same
    # session. enrich_card_specs commits PER CHUNK (load-bearing — see its commit comment),
    # so the batch's pending writes above persist with its first chunk commit; the
    # batch-final commit below covers an early raise / zero-chunk run (NOT a failed
    # first-chunk commit — that rollback discards the batch's pending writes, and the
    # cards re-select next batch).
    if enriched_ids:
        from app.services.spec_enrichment_service import enrich_card_specs

        try:
            spec_stats = await enrich_card_specs(enriched_ids, db)
            logger.info("ENRICH_WORKER: specs {}", spec_stats)
        except ClaudeError as e:
            logger.warning("ENRICH_WORKER: spec extraction Claude error: {}", type(e).__name__)
            breaker.record_claude_error()
        except Exception as e:
            logger.error("ENRICH_WORKER: spec extraction failed: {}", e)

    try:
        db.commit()
    except Exception as e:
        logger.error("ENRICH_WORKER: commit failed for batch: {}", e)
        db.rollback()

    return counts


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


async def main() -> None:
    """Enrichment worker main loop.

    Mirrors ``app/services/ics_worker/worker.py`` structure:
    - SIGTERM/SIGINT → graceful shutdown after current batch
    - Startup heartbeat (is_running=True)
    - Per-iteration SessionLocal (fresh session each loop)
    - DAILY_CAP / circuit breaker → long sleep (1h)
    - Empty batch → idle sleep (IDLE_SLEEP_SECONDS)
    - Non-empty batch → loop sleep (LOOP_SLEEP_SECONDS)
    - Daily reset at UTC midnight archives yesterday's stats
    """
    from app.database import SessionLocal
    from app.models.enrichment_worker_status import update_enrichment_worker_status
    from app.services.enrichment_worker.circuit_breaker import EnrichmentCircuitBreaker
    from app.services.enrichment_worker.config import EnrichmentWorkerConfig

    config = EnrichmentWorkerConfig.from_env()
    breaker = EnrichmentCircuitBreaker(config)
    cooldown: dict[str, float] = {}
    # Persist across batches (cleared at the daily reset): connectors that hit a
    # quota/auth wall and the "web_search" flag once the daily web budget is spent —
    # so they stay disabled instead of being re-tried (and re-logged) every loop.
    disabled: set[str] = set()
    # In-process web-call + oem-resolve tallies — the durable backstop for
    # WEB_DAILY_CAP / OEM_RESOLVE_DAILY_CAP if the cache is unavailable. Reset at the
    # daily reset alongside the cache's date-keyed counters.
    web_state: dict[str, int] = {"web_calls": 0, "oem_resolves": 0}

    # Running totals for today
    enriched_today = 0
    web_sourced_today = 0
    oem_sourced_today = 0
    ai_inferred_today = 0
    not_found_today = 0
    not_catalogued_today = 0
    last_stats_date = None

    logger.info(
        "ENRICH_WORKER: starting up (batch_size={}, daily_cap={}, web_daily_cap={})",
        config.batch_size,
        config.daily_cap,
        config.web_daily_cap,
    )

    # Startup heartbeat
    db = SessionLocal()
    try:
        update_enrichment_worker_status(
            db,
            is_running=True,
            last_heartbeat=datetime.now(timezone.utc),
        )
    finally:
        db.close()

    try:
        while True:
            if _shutdown_requested:
                logger.info("ENRICH_WORKER: graceful shutdown — exiting main loop")
                break

            try:
                # Liveness heartbeat — EVERY tick, before any branch sleeps/continues, so
                # last_heartbeat never goes stale on idle/cap/breaker/business-hours paths.
                # Also reused below as the breaker-open gate (no second should_stop() call).
                db = SessionLocal()
                try:
                    breaker_open = _record_heartbeat(db, breaker)
                finally:
                    db.close()

                now_utc = datetime.now(timezone.utc)
                today_date = now_utc.date()

                # Daily reset at UTC midnight
                if last_stats_date != today_date:
                    if last_stats_date is not None:
                        logger.info(
                            "ENRICH_WORKER daily summary: enriched={}, web={}, oem={}, ai={}, "
                            "not_found={}, not_catalogued={}",
                            enriched_today,
                            web_sourced_today,
                            oem_sourced_today,
                            ai_inferred_today,
                            not_found_today,
                            not_catalogued_today,
                        )
                        db = SessionLocal()
                        try:
                            update_enrichment_worker_status(
                                db,
                                daily_stats_json={
                                    "date": str(last_stats_date),
                                    "enriched": enriched_today,
                                    "web_sourced": web_sourced_today,
                                    "oem_sourced": oem_sourced_today,
                                    "ai_inferred": ai_inferred_today,
                                    "not_found": not_found_today,
                                    "not_catalogued": not_catalogued_today,
                                },
                                enriched_today=0,
                                web_sourced_today=0,
                                oem_sourced_today=0,
                                ai_inferred_today=0,
                                not_found_today=0,
                                not_catalogued_today=0,
                            )
                        finally:
                            db.close()
                    enriched_today = 0
                    web_sourced_today = 0
                    oem_sourced_today = 0
                    ai_inferred_today = 0
                    not_found_today = 0
                    not_catalogued_today = 0
                    last_stats_date = today_date
                    # New day: quotas/budgets reset, so re-enable disabled sources and the
                    # web tier, and zero the in-process web + oem-resolve tallies (the
                    # cache counters are date-keyed and reset on their own).
                    disabled.clear()
                    web_state["web_calls"] = 0
                    web_state["oem_resolves"] = 0

                # Daily cap check — long sleep when exhausted
                if enriched_today >= config.daily_cap:
                    logger.info(
                        "ENRICH_WORKER: daily cap reached ({}/{}), sleeping 1h",
                        enriched_today,
                        config.daily_cap,
                    )
                    await asyncio.sleep(3600)
                    continue

                # Circuit breaker check — long sleep when open. Reuses the breaker_open
                # value already persisted by _record_heartbeat at the top of this tick, so
                # the status write is not duplicated here.
                if breaker_open:
                    logger.error(
                        "ENRICH_WORKER: circuit breaker open ({}), sleeping 1h",
                        breaker.get_trip_info()["trip_reason"],
                    )
                    await asyncio.sleep(3600)
                    continue

                # Run one batch. The session is closed exactly once (in finally) so the
                # connection is released before the idle/loop sleep below.
                db = SessionLocal()
                batch_counts: dict[str, int] = {}
                try:
                    batch_counts = await run_one_batch(db, config, cooldown, breaker, disabled, web_state)

                    if batch_counts:
                        # Accumulate daily totals
                        total_this_batch = sum(batch_counts.values())
                        enriched_today += total_this_batch
                        web_sourced_today += batch_counts.get(MaterialEnrichmentStatus.WEB_SOURCED, 0)
                        oem_sourced_today += batch_counts.get(MaterialEnrichmentStatus.OEM_SOURCED, 0)
                        ai_inferred_today += batch_counts.get(MaterialEnrichmentStatus.AI_INFERRED, 0)
                        not_found_today += batch_counts.get(MaterialEnrichmentStatus.NOT_FOUND, 0)
                        not_catalogued_today += batch_counts.get(MaterialEnrichmentStatus.NOT_CATALOGUED, 0)

                        # Heartbeat + counters
                        now_ts = datetime.now(timezone.utc)
                        update_enrichment_worker_status(
                            db,
                            last_heartbeat=now_ts,
                            last_enriched_at=now_ts,
                            enriched_today=enriched_today,
                            web_sourced_today=web_sourced_today,
                            oem_sourced_today=oem_sourced_today,
                            ai_inferred_today=ai_inferred_today,
                            not_found_today=not_found_today,
                            not_catalogued_today=not_catalogued_today,
                            circuit_breaker_open=False,
                            circuit_breaker_reason=None,
                        )

                        logger.info(
                            "ENRICH_WORKER: batch done {} (today: {}/{})",
                            batch_counts,
                            enriched_today,
                            config.daily_cap,
                        )

                except Exception as e:
                    logger.error("ENRICH_WORKER: batch error: {}", e)
                finally:
                    db.close()

                if not batch_counts:
                    # Queue empty — idle sleep (longer; connection already released).
                    logger.debug("ENRICH_WORKER: queue empty, sleeping {}s", config.idle_sleep_seconds)
                    await asyncio.sleep(config.idle_sleep_seconds)
                else:
                    await asyncio.sleep(config.loop_sleep_seconds)

            except Exception as e:
                logger.error("ENRICH_WORKER: unexpected error in main loop: {}", e)
                await asyncio.sleep(300)

    finally:
        logger.info(
            "ENRICH_WORKER shutting down: enriched_today={}, web={}, oem={}, ai={}, not_found={}, not_catalogued={}",
            enriched_today,
            web_sourced_today,
            oem_sourced_today,
            ai_inferred_today,
            not_found_today,
            not_catalogued_today,
        )
        db = SessionLocal()
        try:
            update_enrichment_worker_status(db, is_running=False)
        finally:
            db.close()
