"""Search service — runs requirements through all configured sources.

- Keeps ALL historical sightings (never deletes)
- Upserts vendor/part combos onto MaterialCards after each search
- Merges MaterialCard vendor history into results
- Vendor card enrichment (ratings, blacklist) happens in main.py
- Caches connector results in Redis (15-min TTL) to avoid redundant API calls
"""

import asyncio
import hashlib
import json
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Final

import redis
from loguru import logger
from sqlalchemy.orm import Session

from .connectors.ai_live_web import AIWebSearchConnector
from .connectors.digikey import DigiKeyConnector
from .connectors.ebay import EbayConnector
from .connectors.element14 import Element14Connector
from .connectors.mouser import MouserConnector
from .connectors.oemsecrets import OEMSecretsConnector
from .connectors.sourcengine import SourcengineConnector
from .connectors.sources import BrokerBinConnector, NexarConnector, _redact_secrets
from .constants import FRU_ALIAS_SOURCE, ActivityType, ApiSourceStatus, SourceRunStatus
from .database import SessionLocal
from .models import (
    ApiSource,
    MaterialCard,
    MaterialVendorHistory,
    Requirement,
    Sighting,
)
from .scoring import (
    classify_lead,
    confidence_color,
    explain_lead,
    is_weak_lead,
    score_sighting,
    score_sighting_v2,
    score_unified,
)
from .services.activity_service import log_activity
from .services.credential_service import get_credential, get_credentials_batch
from .services.fru_matrix_service import get_search_aliases
from .services.ics_worker.queue_manager import enqueue_for_ics_search
from .services.nc_worker.queue_manager import enqueue_for_nc_search
from .services.price_snapshot_service import record_price_snapshot
from .services.sourcing_leads import sync_leads_for_sightings
from .services.tbf_worker.queue_manager import enqueue_for_tbf_search
from .services.vendor_affinity_service import find_vendor_affinity
from .services.vendor_unavailability import apply_to_fresh_sightings
from .utils.async_helpers import safe_background_task
from .utils.normalization import (
    MAX_SUBSTITUTES,
    detect_currency,
    fuzzy_mpn_match,
    normalize_condition,
    normalize_date_code,
    normalize_lead_time,
    normalize_mpn,
    normalize_mpn_key,
    normalize_packaging,
    normalize_price,
    normalize_quantity,
)
from .utils.normalization_helpers import fix_encoding
from .vendor_utils import normalize_vendor_name

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


def _median(values: list[float]) -> float | None:
    """Return the median of a list of numbers, or None if empty."""
    if not values:
        return None
    s = sorted(values)
    return s[len(s) // 2]


# ── Search result cache (Redis, 15-min TTL) ─────────────────────────────

_SEARCH_CACHE_TTL = 900  # 15 minutes
_SEARCH_CACHE_PREFIX = "search:"
_search_redis = None
_search_redis_attempted = False


def _get_search_redis():
    """Lazy-init Redis for search caching.

    Returns client or None.
    """
    global _search_redis, _search_redis_attempted
    if _search_redis_attempted:
        return _search_redis
    _search_redis_attempted = True
    if os.environ.get("TESTING"):
        return None
    try:
        import redis

        from .config import settings

        _search_redis = redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=1,
            retry_on_timeout=True,
        )
        _search_redis.ping()
    except Exception as e:
        logger.warning("Search Redis unavailable, caching disabled: {}", e)
        _search_redis = None
    return _search_redis


def _search_cache_key(pns: list[str], connector_names: list[str]) -> str:
    """Deterministic cache key from sorted PNs + active connectors."""
    payload = json.dumps({"pns": sorted(pns), "connectors": sorted(connector_names)}, sort_keys=True)
    return _SEARCH_CACHE_PREFIX + hashlib.md5(payload.encode(), usedforsecurity=False).hexdigest()


def _get_search_cache(key: str) -> tuple[list[dict], list[dict]] | None:
    """Return (results, source_stats) from cache or None on miss."""
    r = _get_search_redis()
    if not r:
        return None
    try:
        data = r.get(key)
        if data:
            parsed = json.loads(data)
            return parsed["results"], parsed["source_stats"]
    except redis.RedisError as e:
        logger.error("Redis error reading search cache key {}: {}", key, e)
    except Exception as e:
        logger.warning("Search cache read failed: {}", e)
    return None


def _set_search_cache(key: str, results: list[dict], source_stats: list[dict]) -> None:
    """Store search results in Redis with TTL."""
    r = _get_search_redis()
    if not r:
        return
    try:
        r.setex(key, _SEARCH_CACHE_TTL, json.dumps({"results": results, "source_stats": source_stats}))
    except redis.RedisError as e:
        logger.error("Redis error writing search cache key {}: {}", key, e)
    except Exception as e:
        logger.warning("Search cache write failed: {}", e)


def get_all_pns(req: Requirement) -> list[str]:
    """Primary MPN + substitutes, deduplicated by canonical key.

    Returns display-normalized MPNs (uppercase, no spaces, keeps dashes).
    """
    pns = []
    seen_keys: set[str] = set()
    if req.primary_mpn and req.primary_mpn.strip():
        display = normalize_mpn(req.primary_mpn) or req.primary_mpn.strip()
        key = normalize_mpn_key(display)
        if key:
            pns.append(display)
            seen_keys.add(key)
    for sub in req.substitutes or []:
        if isinstance(sub, dict):
            s = (sub.get("mpn") or "").strip()
        else:
            s = str(sub).strip() if sub else ""
        if not s:
            continue
        display = normalize_mpn(s) or s
        key = normalize_mpn_key(display)
        if key and key not in seen_keys:
            pns.append(display)
            seen_keys.add(key)
    return pns


# Cap on FRU-crosswalk aliases injected per requirement as system-derived
# substitutes (optimization plan 2026-06-12 item 2.7). Priority order is
# fru_matrix_service.SEARCH_ALIAS_KINDS: mfg_model, drive_pn, option, ibm_11s.
MAX_FRU_ALIASES: Final[int] = 8


def _expand_fru_aliases(db: Session, req: Requirement) -> list[dict]:
    """New system-derived substitutes from the FRU crosswalk for req's primary MPN.

    Looks up fru_links in both directions (the primary may be the FRU side OR
    the related side — get_search_aliases handles both with one indexed query)
    and returns canonical substitute dicts
    ``{"mpn": ..., "manufacturer": ..., "source": FRU_ALIAS_SOURCE}`` deduped
    against the primary and the existing substitutes, capped at
    MAX_FRU_ALIASES without ever pushing the stored list past MAX_SUBSTITUTES.

    Read-only (safe on the caller's session); durable persistence happens in
    _persist_fru_aliases through its own write session.
    """
    primary = (req.primary_mpn or "").strip()
    if not primary:
        return []
    try:
        candidates = get_search_aliases(db, primary)
    except Exception as e:
        logger.warning("FRU alias lookup failed for {}: {}", primary, e)
        return []
    if not candidates:
        return []
    room = min(MAX_FRU_ALIASES, MAX_SUBSTITUTES - len(req.substitutes or []))
    if room <= 0:
        return []
    taken = {k for k in (normalize_mpn_key(pn) for pn in get_all_pns(req)) if k}
    aliases: list[dict] = []
    for cand in candidates:
        if len(aliases) >= room:
            break
        if not cand.norm or cand.norm in taken:
            continue
        taken.add(cand.norm)
        display = normalize_mpn(cand.mpn) or cand.mpn
        aliases.append({"mpn": display, "manufacturer": cand.manufacturer, "source": FRU_ALIAS_SOURCE})
    return aliases


def _persist_fru_aliases(db: Session, req_id: int, aliases: list[dict]) -> None:
    """Durably append crosswalk aliases to requirements.substitutes.

    Uses its own short-lived write session (same pattern as search_requirement's main
    write path) so it works on BOTH search paths — including the all-cached short-
    circuit, which never opens the main write session — and never dirties the caller's
    session. Re-deduplicates against the freshly loaded row so concurrent searches of
    the same requirement stay idempotent. Best-effort: a failure here must not break the
    search itself.
    """
    from sqlalchemy.orm import sessionmaker

    _WriteSession = sessionmaker(bind=db.get_bind(), autocommit=False, autoflush=False, expire_on_commit=False)
    session = _WriteSession()
    try:
        row = session.get(Requirement, req_id)
        if row is None:
            return
        current = list(row.substitutes or [])
        current_keys = {normalize_mpn_key(row.primary_mpn or "")}
        for sub in current:
            raw = (sub.get("mpn") if isinstance(sub, dict) else str(sub or "")) or ""
            key = normalize_mpn_key(raw)
            if key:
                current_keys.add(key)
        fresh = [a for a in aliases if normalize_mpn_key(a["mpn"]) not in current_keys]
        if not fresh:
            return
        # New list object → SQLAlchemy detects the JSON column change.
        row.substitutes = current + fresh
        session.commit()
        logger.info("Req {}: persisted {} FRU crosswalk substitute(s)", req_id, len(fresh))
    except Exception:
        session.rollback()
        logger.warning("FRU alias persistence failed for requirement {}", req_id, exc_info=True)
    finally:
        session.close()


# How long a MaterialCard.last_searched_at "shields" its MPN from being
# re-queried at supplier APIs. Per-MPN, not per-requirement, so two
# requirements that share an MPN don't each burn quota.
MPN_COOLDOWN_HOURS: Final[int] = 48


def _mpn_cooldown_partition(
    db: Session,
    pns: list[str],
    now: datetime | None = None,
) -> tuple[list[str], list[int]]:
    """Split a requirement's MPNs into (to_search, cached_card_ids).

    A display MPN goes into ``to_search`` when its MaterialCard either does
    not exist or has ``last_searched_at`` older than ``MPN_COOLDOWN_HOURS``.
    Otherwise its card id goes into ``cached_card_ids`` so the caller can
    surface existing sightings via material_card_id linkage.

    Lookups use ``normalize_mpn_key`` so case + packaging-suffix variations
    don't escape the cooldown.
    """
    if not pns:
        return [], []

    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=MPN_COOLDOWN_HOURS)

    keys_in_order = []
    key_to_display: dict[str, str] = {}
    for pn in pns:
        k = normalize_mpn_key(pn)
        if not k or k in key_to_display:
            continue
        keys_in_order.append(k)
        key_to_display[k] = pn

    cards = db.query(MaterialCard).filter(MaterialCard.normalized_mpn.in_(keys_in_order)).all()
    card_by_key = {c.normalized_mpn: c for c in cards}

    to_search: list[str] = []
    cached_ids: list[int] = []
    for key in keys_in_order:
        card = card_by_key.get(key)
        if card is None or card.last_searched_at is None:
            to_search.append(key_to_display[key])
            continue
        # MaterialCard.last_searched_at uses raw DateTime (not UTCDateTime),
        # so SQLite roundtrips strip tzinfo. Coerce to UTC for comparison.
        last = card.last_searched_at
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        # >= 48h old → search again (boundary is inclusive on the stale side)
        if last <= cutoff:
            to_search.append(key_to_display[key])
        else:
            cached_ids.append(card.id)
    return to_search, cached_ids


def _affinity_match_to_result(match: dict, mpn: str) -> dict:
    """Convert a single vendor-affinity match dict into a sighting-shaped result.

    Shared between the cached-only short-circuit path and the full search path
    in ``search_requirement`` so both surface affinity suggestions identically.
    """
    conf_pct = round(match.get("confidence", 0) * 100)
    return {
        "vendor_name": match.get("vendor_name", ""),
        "vendor_id": match.get("vendor_id"),
        "mpn": mpn,
        "mpn_matched": mpn,
        "source_type": "vendor_affinity",
        "source_badge": "Vendor Match",
        "is_historical": False,
        "is_material_history": False,
        "is_affinity": True,
        "confidence_pct": conf_pct,
        "confidence_color": confidence_color(conf_pct),
        "reasoning": match.get("reasoning", ""),
        "qty_available": None,
        "unit_price": None,
        "score": max(5, match.get("confidence", 0) * 20),
        "cross_references": [],
    }


def _find_affinity_in_thread(mpn: str) -> list[dict]:
    """Run the SYNC find_vendor_affinity on a worker thread with its OWN session.

    find_vendor_affinity falls through to an L3 fallback that makes a BLOCKING
    anthropic.Anthropic().messages.create(timeout=30) call, so running it directly on
    the event loop froze every concurrent request for up to 30s (PERF-1). We dispatch it
    via asyncio.to_thread; SQLAlchemy sessions are not thread-safe, so the request session
    never crosses the boundary — each call opens and closes a fresh SessionLocal (the
    established pattern, mirroring routers/sightings.py._find_affinity_in_thread).

    NB: find_vendor_affinity is referenced as the module-level name (NOT re-imported
    lazily) so tests patching app.search_service.find_vendor_affinity still take effect.
    """
    thread_db = SessionLocal()
    try:
        return find_vendor_affinity(mpn, thread_db)
    finally:
        thread_db.close()


async def search_requirement(req: Requirement, db: Session) -> dict:
    """Search APIs for stale MPNs only; surface cached sightings for fresh ones.

    The per-MPN 48h cooldown (``MaterialCard.last_searched_at``) gates which
    MPNs hit the connector layer. Cached MPNs are still surfaced via
    ``material_card_id`` linkage in the caller's detail panel.

    Affinity matches are returned on both the cached short-circuit path and
    the full search path; only the connector calls are gated by the cooldown.

    Returns ``{"sightings": [...], "source_stats": [...], "mpn_results": {mpn: "searched"|"cached"}}``.
    """
    pns = get_all_pns(req)
    if not pns:
        return {"sightings": [], "source_stats": [], "mpn_results": {}}

    # FRU crosswalk alias expansion (item 2.7): brokers list canonical
    # mfg_model/drive_pn numbers, not OEM spare numbers, so a FRU-shaped
    # primary fans out to its crosswalk equivalents (and vice versa — the
    # lookup is bidirectional). Aliases are persisted as system-derived
    # substitutes (source="fru_crosswalk") via a dedicated write session so
    # existing substitutes rendering and future searches carry them; this
    # call's fan-out includes them immediately. Appended after the explicit
    # pns so pns[0] stays the primary MPN.
    fru_aliases = _expand_fru_aliases(db, req)
    if fru_aliases:
        _persist_fru_aliases(db, req.id, fru_aliases)
        pns = pns + [a["mpn"] for a in fru_aliases]
        logger.info("Req {} ({}): injected {} FRU crosswalk alias(es) into search", req.id, pns[0], len(fru_aliases))

    now = datetime.now(timezone.utc)

    # 48h per-normalized-MPN cooldown. Split into MPNs that need a connector
    # call vs. ones whose MaterialCard.last_searched_at is recent enough.
    to_search, _cached_card_ids = _mpn_cooldown_partition(db, pns, now=now)

    searched_keys = {normalize_mpn_key(m) for m in to_search if normalize_mpn_key(m)}
    mpn_results: dict[str, str] = {}
    for pn in pns:
        key = normalize_mpn_key(pn)
        mpn_results[pn] = "searched" if key in searched_keys else "cached"

    # Vendor affinity is keyed on the requirement's primary MPN (no connector quota), so
    # we compute it on every path — including the cached-only short-circuit below. It is
    # NOT a pure-DB lookup: its L3 fallback makes a blocking 30s Anthropic call, so it runs
    # on a worker thread (asyncio.to_thread) to keep the event loop free (PERF-1).
    primary_mpn = pns[0]
    try:
        affinity_matches = await asyncio.to_thread(_find_affinity_in_thread, primary_mpn)
    except Exception as e:
        logger.warning("Vendor affinity lookup failed for {}: {}", primary_mpn, e)
        affinity_matches = []

    # Short-circuit: every MPN is within cooldown — no connector calls.
    # The detail panel surfaces cached sightings via material_card_id linkage
    # in its own query path; this function returns affinity suggestions only.
    if not to_search:
        affinity_results = [_affinity_match_to_result(m, primary_mpn) for m in affinity_matches]
        return {
            "sightings": affinity_results,
            "source_stats": [],
            "mpn_results": mpn_results,
        }

    # 1. Fetch + dedupe (parallel across stale-MPN connectors). Affinity was
    # already computed above so it's available to the merge step below.
    fresh, source_stats = await _fetch_fresh(to_search, db)

    # 2. Score + save — only replace sightings from connectors that succeeded
    # Use a dedicated DB session for writes so concurrent search_requirement()
    # calls (via asyncio.gather) don't share a single non-thread-safe session.
    from sqlalchemy.orm import sessionmaker

    req_id = req.id
    _WriteSession = sessionmaker(bind=db.get_bind(), autocommit=False, autoflush=False, expire_on_commit=False)
    write_db = _WriteSession()
    try:
        write_req = write_db.get(Requirement, req_id)
        if not write_req:
            logger.error("Requirement {} not found in write session", req_id)
            return {"sightings": [], "source_stats": source_stats, "mpn_results": mpn_results}

        succeeded_sources = {
            stat["source"]
            for stat in source_stats
            if stat["status"] == SourceRunStatus.OK.value and not stat.get("error")
        }
        sightings = _save_sightings(fresh, write_req, write_db, succeeded_sources)
        logger.info(f"Req {req_id} ({to_search[0]}): {len(sightings)} fresh sightings")

        # 3. Material card upsert (errors won't break search). Only upsert
        # cards for MPNs we actually searched; cached-side cards are already
        # surfaced via material_card_id linkage in the caller.
        # We also need to ensure the cooldown clock advances even when a
        # search yielded zero sightings — otherwise the next click immediately
        # re-burns the connector quota. `_upsert_material_card` returns None
        # when there were no sightings for that MPN, so we fall back to
        # `resolve_material_card` to guarantee a card exists, then stamp it.
        card_ids = set()
        primary_card_id = None
        for pn in to_search:
            try:
                card = _upsert_material_card(pn, sightings, write_db, now)
                if card is None:
                    card = resolve_material_card(pn, write_db)
                if card:
                    card_ids.add(card.id)
                    # Stamp the cooldown clock on every searched MPN's card.
                    if card.normalized_mpn in searched_keys:
                        card.last_searched_at = now
                    if pn == to_search[0] and not primary_card_id:
                        primary_card_id = card.id
            except Exception as e:
                logger.error("MATERIAL_CARD_UPSERT_FAIL: mpn={} error={}", pn, e)
                write_db.rollback()

        # Link requirement to its primary material card
        if primary_card_id and not write_req.material_card_id:
            write_req.material_card_id = primary_card_id

        # 3a. Inline deterministic passes over this search's card ids (same write
        # session, committed with the sightings below) — decoded facets/category are
        # queryable the moment the search returns, without waiting on the worker.
        # NO enrich_requested_at stamp here: search flow rides the existing
        # created_at fast lane + search_count demand ordering.
        # DELIBERATE spec deviation: card_ids covers EVERY searched MPN's card (the
        # spec said newly-created ids only). The passes are idempotent through the
        # F1 ladder and to_search is small (primary + substitutes), so re-searching
        # an old card backfills its decode at ~15ms/card — an improvement, kept.
        run_deterministic_passes(write_db, card_ids)

        # 3b. Fire background enrichment for cards without manufacturer
        await _schedule_background_enrichment(card_ids, write_db)

        # 4. Historical vendors from material cards
        fresh_vendors = {s.vendor_name.lower() for s in sightings}
        history = _get_material_history(list(card_ids), fresh_vendors, write_db)

        # Stamp per-requirement search timestamp only when the search
        # actually succeeded. "Success" means at least one connector
        # returned status=ok — i.e. there was a real response from an
        # upstream API (even if it had zero matches). If every connector
        # errored (auth failures, quota exceeded, network), we leave
        # last_searched_at alone so the 5-minute rate guard in
        # routers/sightings.py does not silently suppress the user's
        # next retry.
        if succeeded_sources:
            write_req.last_searched_at = now
        write_db.commit()

        # --- Spec-code resolver fallback (spec §6) ---
        # Trigger only on a hard zero from the synchronous fanout AND the
        # feature flag. The async ICS/NC workers run independently below
        # for the primary MPN regardless of resolver outcome.
        from .config import settings as _settings

        if _settings.spec_resolver_enabled and len(sightings) == 0 and write_req.primary_mpn:
            try:
                from app.services.spec_code_resolver import SpecCodeResolver

                # resolve() owns its own persistence SAVEPOINT and releases the DB
                # connection during the grounded LLM call, so we do NOT wrap it in a
                # transaction here — doing so would pin a pooled connection for the
                # call's ~60s duration. The sightings committed just above are durable
                # and unaffected, and a concurrent-insert race is recovered inside
                # resolve() (it reuses the winning row).
                resolver = SpecCodeResolver(write_db)
                resolution = await resolver.resolve(
                    write_req.primary_mpn,
                    oem=write_req.oem_hint or "IBM",
                )
            except Exception:
                logger.warning(
                    "spec_resolver: resolve() failed for req {} mpn {}",
                    req_id,
                    write_req.primary_mpn,
                    exc_info=True,
                )
                resolution = None

            if resolution is not None and resolution.status != "unresolved" and resolution.avl:
                avl_mpns = [entry["mpn"] for entry in resolution.avl if entry.get("mpn")]
                if avl_mpns:
                    # Issue 2 fix: AVL re-fanout must honor the same per-MPN
                    # cooldown that the primary path applies via
                    # ``_mpn_cooldown_partition`` above, otherwise every click on
                    # a zero-hit spec-code burns connector quota on the same AVL
                    # set. ``now`` is the search timestamp computed earlier in
                    # this call.
                    to_fetch_avl, _cached_avl = _mpn_cooldown_partition(write_db, avl_mpns, now=now)

                    # Issue 3 fix: explicit try/except distinguishes "connectors
                    # crashed" from "no AVL hits". Design intent: still enqueue
                    # async workers + write pending bookkeeping even when the
                    # live connectors fail — the buyer benefits from worker
                    # output independent of connector outages.
                    try:
                        if to_fetch_avl:
                            resolved_fresh, resolved_stats = await _fetch_fresh(to_fetch_avl, db)
                        else:
                            resolved_fresh, resolved_stats = [], []
                    except Exception:
                        logger.warning(
                            "spec_resolver: AVL fanout failed for req {} (spec_code={}); "
                            "still enqueueing workers for async pickup",
                            req_id,
                            write_req.primary_mpn,
                            exc_info=True,
                        )
                        resolved_fresh, resolved_stats = [], []

                    spec_code_tag = write_req.primary_mpn
                    for row in resolved_fresh:
                        row["resolved_via_spec_code"] = spec_code_tag
                        row["source_mpn"] = row.get("mpn") or row.get("mpn_matched")

                    resolved_succeeded = {
                        stat["source"]
                        for stat in resolved_stats
                        if stat.get("status") == SourceRunStatus.OK.value and not stat.get("error")
                    }
                    if resolved_fresh:
                        resolved_sightings = _save_sightings(resolved_fresh, write_req, write_db, resolved_succeeded)
                        sightings.extend(resolved_sightings)
                    else:
                        resolved_sightings = []
                    source_stats.extend(resolved_stats)
                    logger.info(
                        "spec_resolver: re-fanout produced {} sightings for req {} (spec_code={})",
                        len(resolved_sightings),
                        req_id,
                        spec_code_tag,
                    )

                    # Stamp the cooldown clock on every AVL MPN we actually
                    # searched. Without this, ``_mpn_cooldown_partition`` keeps
                    # returning the full AVL set as stale on every subsequent
                    # zero-hit click and re-burns connector quota — the bug the
                    # partition gate above is meant to prevent. Mirror the
                    # primary path: upsert a card from the AVL sightings, fall
                    # back to ``resolve_material_card`` when the fanout was empty
                    # so a card always exists to carry ``last_searched_at``.
                    for avl_pn in to_fetch_avl:
                        try:
                            avl_card = _upsert_material_card(avl_pn, resolved_sightings, write_db, now)
                            if avl_card is None:
                                avl_card = resolve_material_card(avl_pn, write_db)
                            if avl_card:
                                avl_card.last_searched_at = now
                        except Exception as e:
                            logger.error("AVL_MATERIAL_CARD_STAMP_FAIL: mpn={} error={}", avl_pn, e)
                            write_db.rollback()

                    # Enqueue each AVL MPN to ICS and NC workers in addition
                    # to the primary-MPN enqueue below.
                    for mpn in avl_mpns:
                        try:
                            enqueue_for_ics_search(
                                req_id,
                                write_db,
                                override_mpn=mpn,
                                resolved_via_spec_code=spec_code_tag,
                            )
                        except Exception:
                            logger.warning(
                                "spec_resolver: ICS AVL enqueue failed for req {} mpn {}",
                                req_id,
                                mpn,
                                exc_info=True,
                            )
                        try:
                            enqueue_for_nc_search(
                                req_id,
                                write_db,
                                override_mpn=mpn,
                                resolved_via_spec_code=spec_code_tag,
                            )
                        except Exception:
                            logger.warning(
                                "spec_resolver: NC AVL enqueue failed for req {} mpn {}",
                                req_id,
                                mpn,
                                exc_info=True,
                            )
                        try:
                            enqueue_for_tbf_search(
                                req_id,
                                write_db,
                                override_mpn=mpn,
                                resolved_via_spec_code=spec_code_tag,
                            )
                        except Exception:
                            logger.warning(
                                "spec_resolver: TBF AVL enqueue failed for req {} mpn {}",
                                req_id,
                                mpn,
                                exc_info=True,
                            )

                    # Record this requirement on the pending row so the admin
                    # UI can show which requirements consumed each speculative
                    # mapping (spec §4.2 ``used_in_requirement_ids``).
                    if resolution.status == "pending":
                        from app.models.sourcing import OemSpecCodePending

                        # Issue 1 fix: ``with_for_update`` takes a row-level lock
                        # on PG so concurrent ``search_requirement()`` calls for
                        # different requirements targeting the same (oem,
                        # spec_code) serialize on this row, eliminating the
                        # lost-update race on the JSONB list. SQLite ignores the
                        # lock but its single-threaded execution model means the
                        # existing test still passes.
                        oem_normalized = (write_req.oem_hint or "IBM").strip().upper()
                        spec_code_normalized = spec_code_tag.strip().upper()
                        pending_row = (
                            write_db.query(OemSpecCodePending)
                            .filter_by(
                                oem=oem_normalized,
                                spec_code=spec_code_normalized,
                            )
                            .with_for_update()
                            .one_or_none()
                        )
                        if pending_row is not None:
                            used = list(pending_row.used_in_requirement_ids or [])
                            if req_id not in used:
                                used.append(req_id)
                                pending_row.used_in_requirement_ids = used
                            write_db.commit()
        # --- end resolver block ---

        # Aggregated activity-timeline entry: one row per search batch,
        # never one per sighting. Skipped for zero-result searches so the
        # timeline stays free of noise. Logged after the resolver fallback so
        # the count reflects any AVL sightings the resolver appended.
        if sightings:
            _sighting_sources = sorted(succeeded_sources)
            log_activity(
                write_db,
                activity_type=ActivityType.SIGHTING_ADDED,
                requisition_id=write_req.requisition_id,
                requirement_id=write_req.id,
                user_id=None,
                channel="system",
                description=(
                    f"{len(sightings)} sighting(s) added"
                    + (f" from {', '.join(_sighting_sources)}" if _sighting_sources else "")
                ),
                details={"count": len(sightings), "sources": _sighting_sources},
            )
            write_db.commit()

        # Browser-automation workers: best-effort enqueue once per call. Both
        # workers key by requirement_id and internally normalize req.primary_mpn,
        # so per-substitute iteration would just round-trip dedup checks. Called
        # after write_db.commit() so the worker reads the same durable state we
        # just wrote.
        try:
            enqueue_for_ics_search(req_id, write_db)
        except Exception:
            logger.warning("ICS enqueue failed for requirement {}", req_id, exc_info=True)
        try:
            enqueue_for_nc_search(req_id, write_db)
        except Exception:
            logger.warning("NC enqueue failed for requirement {}", req_id, exc_info=True)
        try:
            enqueue_for_tbf_search(req_id, write_db)
        except Exception:
            logger.warning("TBF enqueue failed for requirement {}", req_id, exc_info=True)

        # Expunge sightings so they remain usable after session close
        for s in sightings:
            write_db.expunge(s)
    except Exception:
        write_db.rollback()
        raise
    finally:
        write_db.close()

    # 5. Combine + sort
    results = []
    for s in sightings:
        d = sighting_to_dict(s)
        d["is_historical"] = False
        d["is_material_history"] = False
        results.append(d)

    for h in history:
        results.append(_history_to_result(h, now))

    # 5b. Merge vendor affinity suggestions (skip vendors already in live results)
    live_vendors = {r.get("vendor_name", "").lower() for r in results}
    for match in affinity_matches:
        vendor_lower = match.get("vendor_name", "").lower()
        if vendor_lower in live_vendors:
            continue
        live_vendors.add(vendor_lower)
        results.append(_affinity_match_to_result(match, primary_mpn))
    if affinity_matches:
        kept = sum(1 for r in results if r.get("is_affinity"))
        logger.info(
            "Req {} ({}): merged {} affinity suggestions ({} after dedup)",
            req.id,
            primary_mpn,
            len(affinity_matches),
            kept,
        )

    # 6. Cross-references: group results by material_card_id to show alternate MPNs
    card_mpns: dict[int, set[str]] = {}
    for r in results:
        cid = r.get("material_card_id")
        mpn = r.get("mpn") or r.get("mpn_matched", "")
        if cid and mpn:
            card_mpns.setdefault(cid, set()).add(mpn.upper())
    for r in results:
        cid = r.get("material_card_id")
        mpn = (r.get("mpn") or r.get("mpn_matched", "")).upper()
        if cid and cid in card_mpns:
            xrefs = sorted(card_mpns[cid] - {mpn})
            r["cross_references"] = xrefs
        else:
            r["cross_references"] = []

    # 7. Flag price outliers — historical results 20x+ above fresh median
    fresh_prices = [r["unit_price"] for r in results if not r.get("is_material_history") and r.get("unit_price")]
    if fresh_prices:
        median_price = _median(fresh_prices)
        if median_price > 0:
            for r in results:
                p = r.get("unit_price")
                if p and p > median_price * 20:
                    r["price_outlier"] = True
                    r["score"] = max(5, r.get("score", 0) * 0.2)

    results = _deduplicate_sightings(results)

    before_count = len(results)
    results = [
        r
        for r in results
        if r.get("is_affinity")
        or not is_weak_lead(
            score=r.get("score", 0),
            is_authorized=r.get("is_authorized", False),
            has_price=r.get("unit_price") is not None,
            has_qty=r.get("qty_available") is not None,
            evidence_tier=r.get("evidence_tier"),
        )
    ]
    filtered_count = before_count - len(results)
    if filtered_count > 0:
        logger.info(f"Req {req.id}: filtered {filtered_count} weak leads ({before_count} -> {len(results)})")

    results.sort(key=lambda x: (x.get("confidence_pct", 0), x.get("score", 0)), reverse=True)
    return {"sightings": results, "source_stats": source_stats, "mpn_results": mpn_results}


async def quick_search_mpn(mpn: str, db: Session) -> dict:
    """Ad-hoc MPN search — hits supplier APIs without needing a Requirement.

    Returns live API results + material card history, scored and deduped.
    Does NOT persist sightings (read-only quick check).

    Called by: routers/materials.py (POST /api/quick-search)
    Depends on: _fetch_fresh, _get_material_history, scoring, normalization
    """
    from .evidence_tiers import tier_for_sighting

    clean_mpn = normalize_mpn(mpn) or mpn.strip().upper()
    if not clean_mpn:
        return {"sightings": [], "source_stats": [], "material_card": None}

    pns = [clean_mpn]
    now = datetime.now(timezone.utc)

    # 1. Hit all supplier APIs
    fresh, source_stats = await _fetch_fresh(pns, db)

    # 2. Build vendor score lookup
    needed_names = {normalize_vendor_name((r.get("vendor_name") or "").strip()) for r in fresh if r.get("vendor_name")}
    needed_names.discard("")
    vendor_score_map = {}
    if needed_names:
        from .models import VendorCard

        vendor_cards = (
            db.query(VendorCard.normalized_name, VendorCard.vendor_score)
            .filter(VendorCard.normalized_name.in_(needed_names))
            .all()
        )
        vendor_score_map = {vc.normalized_name: vc.vendor_score for vc in vendor_cards}

    # 3. Score raw results into sighting-like dicts (no DB persist)
    results = []
    for r in fresh:
        raw_mpn = r.get("mpn_matched")
        clean_mpn_r = normalize_mpn(raw_mpn) or raw_mpn
        raw_vendor = r.get("vendor_name", "Unknown")
        clean_vendor = fix_encoding((raw_vendor or "").strip()) or raw_vendor

        clean_qty = normalize_quantity(r.get("qty_available"))
        if clean_qty is None and isinstance(r.get("qty_available"), (int, float)) and r["qty_available"] > 0:
            clean_qty = int(r["qty_available"])

        clean_price = normalize_price(r.get("unit_price"))
        if clean_price is None and isinstance(r.get("unit_price"), (int, float)) and r["unit_price"] > 0:
            clean_price = float(r["unit_price"])

        raw_currency = r.get("currency") or "USD"
        clean_currency = detect_currency(raw_currency) if raw_currency else "USD"
        raw_conf = r.get("confidence", 0) or 0
        norm_conf = raw_conf / 5.0 if raw_conf > 1 else raw_conf
        is_auth = r.get("is_authorized", False)
        norm_name = normalize_vendor_name(clean_vendor)
        base_score = score_sighting(vendor_score_map.get(norm_name), is_auth)
        tier = tier_for_sighting(r.get("source_type"), is_auth)

        results.append(
            {
                "id": None,
                "requirement_id": None,
                "vendor_name": clean_vendor,
                "vendor_email": r.get("vendor_email"),
                "vendor_phone": r.get("vendor_phone"),
                "mpn_matched": clean_mpn_r,
                "manufacturer": r.get("manufacturer"),
                "qty_available": clean_qty,
                "unit_price": clean_price,
                "currency": clean_currency,
                "source_type": r.get("source_type"),
                "is_authorized": is_auth,
                "confidence": norm_conf,
                "score": base_score,
                "octopart_url": r.get("octopart_url"),
                "click_url": r.get("click_url"),
                "vendor_url": r.get("vendor_url"),
                "vendor_sku": r.get("vendor_sku"),
                "condition": normalize_condition(r.get("condition")),
                "moq": r.get("moq") if r.get("moq") and r.get("moq") > 0 else None,
                "date_code": normalize_date_code(r.get("date_code")),
                "packaging": normalize_packaging(r.get("packaging")),
                "lead_time_days": normalize_lead_time(r.get("lead_time")),
                "lead_time": r.get("lead_time"),
                "evidence_tier": tier,
                "created_at": now.isoformat(),
                "is_historical": False,
                "is_material_history": False,
                "country": r.get("country"),
                "lead_quality": classify_lead(
                    score=base_score,
                    is_authorized=is_auth,
                    has_price=clean_price is not None,
                    has_qty=clean_qty is not None,
                    has_contact=bool(r.get("vendor_email") or r.get("vendor_phone")),
                    evidence_tier=tier,
                ),
            }
        )

    # 4. v2 scoring with median price context
    prices = [r["unit_price"] for r in results if r.get("unit_price") and r["unit_price"] > 0]
    median_price = _median(prices)
    for r in results:
        norm_name = normalize_vendor_name(r["vendor_name"])
        v2_total, _ = score_sighting_v2(
            vendor_score=vendor_score_map.get(norm_name),
            is_authorized=r["is_authorized"],
            unit_price=r["unit_price"],
            median_price=median_price,
            qty_available=r["qty_available"],
            target_qty=None,
            age_hours=0.0,
            has_price=r["unit_price"] is not None,
            has_qty=r["qty_available"] is not None,
            has_lead_time=r.get("lead_time_days") is not None,
            has_condition=r.get("condition") is not None,
        )
        r["score"] = v2_total

    # 5. Material card history
    norm_key = normalize_mpn_key(clean_mpn)
    card = db.query(MaterialCard).filter_by(normalized_mpn=norm_key).filter(MaterialCard.deleted_at.is_(None)).first()
    card_ids = [card.id] if card else []
    fresh_vendors = {(r["vendor_name"] or "").lower() for r in results}
    history = _get_material_history(card_ids, fresh_vendors, db)
    for h in history:
        results.append(_history_to_result(h, now))

    # 6. Dedupe, filter weak leads, sort
    results = _deduplicate_sightings(results)
    results = [
        r
        for r in results
        if not is_weak_lead(
            score=r.get("score", 0),
            is_authorized=r.get("is_authorized", False),
            has_price=r.get("unit_price") is not None,
            has_qty=r.get("qty_available") is not None,
            evidence_tier=r.get("evidence_tier"),
        )
    ]
    results.sort(key=lambda x: (x.get("confidence_pct", 0), x.get("score", 0)), reverse=True)

    # 7. Material card summary (if exists)
    card_summary = None
    if card:
        from .models import Offer

        sighting_ct = db.query(Sighting).filter(Sighting.material_card_id == card.id).count()
        offer_ct = db.query(Offer).filter(Offer.material_card_id == card.id).count()
        card_summary = {
            "id": card.id,
            "mpn": card.display_mpn,
            "manufacturer": card.manufacturer,
            "description": card.description,
            "lifecycle_status": card.lifecycle_status,
            "sighting_count": sighting_ct,
            "offer_count": offer_ct,
        }

    return {"sightings": results, "source_stats": source_stats, "material_card": card_summary}


# ── Sighting deduplication ───────────────────────────────────────────────


def _deduplicate_sightings(sighting_dicts: list[dict]) -> list[dict]:
    """Deduplicate and merge sighting results for cleaner display.

    Rules:
    - Exclude sightings with qty_available=0 (confirmed zero stock)
    - Keep sightings with qty_available=None (unknown stock — part exists)
    - Same vendor + MPN + price → merge (sum quantities, keep best row)
    - Same vendor + MPN + different price → keep separate lines
    - Historical / material-history rows pass through untouched
    """
    kept: list[dict] = []
    groups: dict[tuple, list[dict]] = {}

    for d in sighting_dicts:
        # Pass through historical rows untouched
        if d.get("is_historical") or d.get("is_material_history"):
            kept.append(d)
            continue

        # Filter out rows with confirmed zero stock; keep None (unknown qty)
        qty = d.get("qty_available")
        if qty is not None and qty == 0:
            continue

        # Group key: vendor + mpn + price
        vendor = (d.get("vendor_name") or "").strip().lower()
        mpn = (d.get("mpn_matched") or "").strip().lower()
        price = d.get("unit_price")
        price_key = round(float(price), 4) if price is not None else None
        key = (vendor, mpn, price_key)
        groups.setdefault(key, []).append(d)

    # Merge each group
    for group in groups.values():
        if len(group) == 1:
            kept.append(group[0])
            continue

        # Pick the row with highest score as the "best"
        group.sort(key=lambda x: (x.get("confidence_pct", 0), x.get("score", 0)), reverse=True)
        best = dict(group[0])

        # Sum quantities across all rows in group; stay None if all unknown
        known_qtys = [g["qty_available"] for g in group if g.get("qty_available") is not None]
        best["qty_available"] = sum(known_qtys) if known_qtys else None

        # Keep best confidence
        best["confidence"] = max((g.get("confidence") or 0) for g in group)

        # Keep lowest MOQ (most favorable to buyer)
        moqs = [g["moq"] for g in group if g.get("moq")]
        if moqs:
            best["moq"] = min(moqs)

        # Collect merged source types (e.g. "nexar + digikey")
        sources = sorted({g.get("source_type", "") for g in group})
        if len(sources) > 1:
            best["merged_sources"] = sources

        best["merged_count"] = len(group)
        kept.append(best)

    return kept


def _deduplicate_sightings_aggressive(sighting_dicts: list[dict]) -> list[dict]:
    """Aggressive dedup: one entry per vendor+MPN. All price variants become sub_offers.

    Used by the search tab (not requisition search which uses _deduplicate_sightings).

    Called by: stream_search_mpn, search_run (search tab only)
    Depends on: vendor_utils.normalize_vendor_name
    """
    groups: dict[tuple, list[dict]] = {}

    for d in sighting_dicts:
        qty = d.get("qty_available")
        if qty is not None and qty == 0:
            continue

        vendor = normalize_vendor_name((d.get("vendor_name") or "").strip())
        mpn = (d.get("mpn_matched") or "").strip().lower()
        key = (vendor, mpn)
        groups.setdefault(key, []).append(d)

    results = []
    for group in groups.values():
        group.sort(key=lambda x: (x.get("score", 0), x.get("confidence", 0)), reverse=True)
        best = dict(group[0])

        # Sum quantities
        known_qtys = [g["qty_available"] for g in group if g.get("qty_available") is not None]
        best["qty_available"] = sum(known_qtys) if known_qtys else None

        # Best confidence
        best["confidence"] = max((g.get("confidence") or 0) for g in group)

        # Lowest MOQ
        moqs = [g["moq"] for g in group if g.get("moq")]
        if moqs:
            best["moq"] = min(moqs)

        # Collect sources
        best["sources_found"] = {g.get("source_type", "") for g in group}
        best["sources_found"].discard("")

        # Sub-offers (everything except the best)
        best["sub_offers"] = group[1:] if len(group) > 1 else []
        best["offer_count"] = len(group)

        results.append(best)

    results.sort(key=lambda x: (x.get("score", 0), x.get("confidence", 0)), reverse=True)
    return results


def _incremental_dedup(incoming: list[dict], existing: list[dict]) -> tuple[list[dict], list[dict]]:
    """Dedup incoming results against already-sent cards.

    Returns (new_cards, updated_cards) where:
    - new_cards: vendors not yet seen — append to DOM
    - updated_cards: vendors already sent — OOB swap to update card

    Mutates existing list in-place (adds new entries, updates existing ones).

    Called by: _run_streaming_search
    Depends on: vendor_utils.normalize_vendor_name
    """
    existing_map: dict[tuple, dict] = {}
    for card in existing:
        vendor = normalize_vendor_name((card.get("vendor_name") or "").strip())
        mpn = (card.get("mpn_matched") or "").strip().lower()
        existing_map[(vendor, mpn)] = card

    new_cards = []
    updated_cards = []

    for item in incoming:
        qty = item.get("qty_available")
        if qty is not None and qty == 0:
            continue

        vendor = normalize_vendor_name((item.get("vendor_name") or "").strip())
        mpn = (item.get("mpn_matched") or "").strip().lower()
        key = (vendor, mpn)

        if key in existing_map:
            card = existing_map[key]
            card.setdefault("sub_offers", []).append(item)
            card["offer_count"] = card.get("offer_count", 1) + 1
            card.setdefault("sources_found", set()).add(item.get("source_type", ""))

            # Update best offer if incoming is better
            if item.get("score", 0) > card.get("score", 0):
                old_best = {k: v for k, v in card.items() if k not in ("sub_offers", "offer_count", "sources_found")}
                card["sub_offers"].append(old_best)
                card["sub_offers"].remove(item)
                for k, v in item.items():
                    if k not in ("sub_offers", "offer_count", "sources_found"):
                        card[k] = v

            # Re-sum quantities
            all_offers = [card] + card.get("sub_offers", [])
            known_qtys = [o["qty_available"] for o in all_offers if o.get("qty_available") is not None]
            card["qty_available"] = sum(known_qtys) if known_qtys else None

            updated_cards.append(card)
        else:
            new_card = dict(item)
            new_card["sub_offers"] = []
            new_card["offer_count"] = 1
            new_card["sources_found"] = {item.get("source_type", "")}
            new_card["sources_found"].discard("")
            existing.append(new_card)
            existing_map[key] = new_card
            new_cards.append(new_card)

    return new_cards, updated_cards


def _render_search_vendor_cards_html(
    cards: list[dict],
    *,
    search_id: str,
    start_index: int = 0,
    swap_oob: bool = False,
) -> str:
    """Render vendor_card.html fragments for HTMX SSE (must be HTML, not JSON).

    Called by: stream_search_mpn (results + card-update events)
    Depends on: app.template_env.templates, htmx/partials/search/vendor_card.html
    """
    from .template_env import templates

    tmpl = templates.get_template("htmx/partials/search/vendor_card.html")
    parts: list[str] = []
    for i, card in enumerate(cards):
        parts.append(
            tmpl.render(
                card=card,
                card_index=start_index + i,
                search_id=search_id,
                swap_oob=swap_oob,
            )
        )
    return "".join(parts)


# ── Smart AI trigger ─────────────────────────────────────────────────────


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


def _build_connectors(db: Session) -> tuple[list, dict[str, dict], set[str]]:
    """Build enabled connectors with credentials, returning (connectors,
    source_stats_map, disabled_sources).

    Sources with status='disabled' or status='error' (set by health_monitor) are
    excluded; their entries are seeded into source_stats_map with 'disabled' or
    'error_skipped' chips so the UI renders them.
    """
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


def _any_pn_obsolete(db: Session, pns: list[str]) -> bool:
    """True if any of ``pns`` maps to a MaterialCard marked obsolete.

    ``pns`` are display-form MPNs (uppercase, dashes preserved) as produced by
    ``get_all_pns``, but ``MaterialCard.normalized_mpn`` stores the canonical
    KEY form (``normalize_mpn_key``: lowercase, non-alphanumerics stripped).
    Query with the key form — a raw display-form ``filter_by`` never matches.
    All keys are batched into a single indexed ``.in_()`` query to avoid an N+1.
    """
    keys = [k for k in (normalize_mpn_key(pn) for pn in pns) if k]
    if not keys:
        return False
    return (
        db.query(MaterialCard.id)
        .filter(
            MaterialCard.normalized_mpn.in_(keys),
            MaterialCard.lifecycle_status == "obsolete",
        )
        .first()
        is not None
    )


async def _fetch_fresh(pns: list[str], db: Session) -> tuple[list[dict], list[dict]]:
    """Run all enabled connectors against pns and return (results, source_stats).

    source_stats[i] follows SourceRunStatus: 'ok' (ran successfully), 'error' (this run
    failed), 'error_skipped' (excluded because health_monitor previously flipped
    api_sources.status to 'error' — auto-recovers on next ping success), 'skipped' (no
    creds), or 'disabled' (operator turned the source off).
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
    cache_key = _search_cache_key(pns, active_names)
    # Sync Redis GET off the event loop — a slow/unreachable Redis must not block
    # every other in-flight request on the single loop (PERF-2). The helper stays
    # best-effort (swallows RedisError internally), so no new exception escapes here.
    cached = await asyncio.to_thread(_get_search_cache, cache_key)
    if cached is not None:
        cached_results, cached_stats = cached
        # Merge cached stats with disabled/skipped entries
        cached_stats_map = {s["source"]: s for s in cached_stats}
        source_stats_map.update(cached_stats_map)
        logger.info("Search cache HIT for {} ({} results)", pns[0] if pns else "?", len(cached_results))
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
    from .config import settings

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
                src.last_success = datetime.now(timezone.utc)
                prev = src.avg_response_ms or elapsed_ms
                src.avg_response_ms = (prev * 3 + elapsed_ms) // 4
                src.status = ApiSourceStatus.LIVE.value
                src.last_error = None
            else:
                src.last_error = error
                src.last_error_at = datetime.now(timezone.utc)
                src.error_count_24h = (src.error_count_24h or 0) + 1
        db.commit()
    except Exception as e:
        logger.warning("API source stats update failed: {}", e)
        db.rollback()

    # Flatten and dedupe
    raw = []
    for result in results_lists:
        if isinstance(result, list):
            raw.extend(result)
        # If it's an exception from gather, skip it

    seen = set()
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

    # Filter out junk vendors — no sellers, blanks, placeholders
    from .shared_constants import JUNK_VENDORS

    out = [r for r in out if r.get("vendor_name", "").strip().lower() not in JUNK_VENDORS]

    # ── Smart AI trigger: conditionally fire AI connector ────────────
    if ai_connector is not None:
        api_result_count = len(out)
        has_price_below_target = any(r.get("unit_price") is not None and r["unit_price"] > 0 for r in out)
        # Check obsolete status from MaterialCard if available
        is_obsolete = _any_pn_obsolete(db, pns)

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
                datetime.now(timezone.utc) - latest_sighting.created_at.replace(tzinfo=timezone.utc)
                if latest_sighting.created_at.tzinfo is None
                else datetime.now(timezone.utc) - latest_sighting.created_at
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
            ai_tasks = [_throttled(ai_connector, pn) for pn in pns]
            ai_results_lists = await asyncio.gather(*ai_tasks, return_exceptions=True)
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

    # Build source_stats from stats_updates (connectors that actually ran)
    # Aggregate per source (a connector may run for multiple PNs)
    agg: dict[str, dict] = {}
    for source_name, hit_count, elapsed_ms, error in stats_updates:
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
    # Merge with skipped/disabled entries
    source_stats_map.update(agg)

    # Cache results for subsequent searches of the same PNs — sync Redis SETEX
    # off the event loop so a slow Redis doesn't stall the loop (PERF-2).
    connector_stats = list(agg.values())
    await asyncio.to_thread(_set_search_cache, cache_key, out, connector_stats)

    return out, list(source_stats_map.values())


def _save_sightings(
    fresh: list[dict],
    req: Requirement,
    db: Session,
    succeeded_sources: set[str] | None = None,
) -> list[Sighting]:
    from .models import VendorCard

    # Build vendor-name → vendor_score lookup (only for vendors in results)
    needed_names = {normalize_vendor_name((r.get("vendor_name") or "").strip()) for r in fresh if r.get("vendor_name")}
    needed_names.discard("")
    if needed_names:
        vendor_cards = (
            db.query(VendorCard.normalized_name, VendorCard.vendor_score)
            .filter(VendorCard.normalized_name.in_(needed_names))
            .all()
        )
        vendor_score_map = {vc.normalized_name: vc.vendor_score for vc in vendor_cards}
    else:
        vendor_score_map = {}

    # Connector-aware delete: only remove sightings from sources that returned
    # results.  Sightings from failed/timed-out connectors are preserved.
    # Map nexar → {nexar, octopart} since Octopart results come via NexarConnector
    _SOURCE_ALIASES = {"nexar": {"nexar", "octopart"}}
    expanded: set[str] = set()
    if succeeded_sources:
        for s in succeeded_sources:
            expanded.update(_SOURCE_ALIASES.get(s, {s}))
        db.query(Sighting).filter(
            Sighting.requirement_id == req.id,
            Sighting.source_type.in_(expanded),
        ).delete(synchronize_session="fetch")
    else:
        # Fallback: no source info → wipe all (legacy behaviour)
        db.query(Sighting).filter_by(requirement_id=req.id).delete()
    db.flush()

    sightings = []
    for r in fresh:
        # Normalize mpn_matched (uppercase, strip) and vendor_name (trim, fix encoding)
        raw_mpn = r.get("mpn_matched")
        clean_mpn = normalize_mpn(raw_mpn) or raw_mpn
        raw_vendor = r.get("vendor_name", "Unknown")
        clean_vendor = fix_encoding((raw_vendor or "").strip()) or raw_vendor

        # Normalize numeric and enum fields from raw connector data
        raw_qty = r.get("qty_available")
        clean_qty = normalize_quantity(raw_qty)
        if clean_qty is None and isinstance(raw_qty, (int, float)) and raw_qty > 0:
            clean_qty = int(raw_qty)

        raw_price = r.get("unit_price")
        clean_price = normalize_price(raw_price)
        if clean_price is None and isinstance(raw_price, (int, float)) and raw_price > 0:
            clean_price = float(raw_price)

        raw_currency = r.get("currency") or "USD"
        clean_currency = detect_currency(raw_currency) if raw_currency else "USD"

        clean_condition = normalize_condition(r.get("condition"))
        clean_packaging = normalize_packaging(r.get("packaging"))
        clean_date_code = normalize_date_code(r.get("date_code"))
        clean_lead_time_days = normalize_lead_time(r.get("lead_time"))

        # Normalize confidence to 0-1 range (connectors use 1-5 integer scale)
        raw_conf = r.get("confidence", 0) or 0
        norm_conf = raw_conf / 5.0 if raw_conf > 1 else raw_conf

        from .evidence_tiers import tier_for_sighting

        is_auth = r.get("is_authorized", False)
        s = Sighting(
            requirement_id=req.id,
            vendor_name=clean_vendor,
            vendor_name_normalized=normalize_vendor_name(clean_vendor),
            vendor_email=r.get("vendor_email"),
            vendor_phone=r.get("vendor_phone"),
            mpn_matched=clean_mpn,
            material_card_id=r.get("material_card_id"),
            manufacturer=r.get("manufacturer"),
            qty_available=clean_qty,
            unit_price=clean_price,
            currency=clean_currency,
            moq=r.get("moq") if r.get("moq") and r.get("moq") > 0 else None,
            source_type=r.get("source_type"),
            is_authorized=is_auth,
            confidence=norm_conf,
            condition=clean_condition,
            packaging=clean_packaging,
            date_code=clean_date_code,
            lead_time_days=clean_lead_time_days,
            lead_time=r.get("lead_time"),
            raw_data=r,
            evidence_tier=tier_for_sighting(r.get("source_type"), is_auth),
            # Spec-code resolver lineage (spec §6). Both null on the normal
            # path; populated by the search_requirement re-fanout block when
            # the sighting was discovered via an AVL MPN resolved from an
            # OEM spec code.
            resolved_via_spec_code=r.get("resolved_via_spec_code"),
            source_mpn=r.get("source_mpn"),
            created_at=datetime.now(timezone.utc),
        )
        norm_name = normalize_vendor_name(clean_vendor)
        s.score = score_sighting(vendor_score_map.get(norm_name), s.is_authorized)
        db.add(s)
        sightings.append(s)

    # PR 3: Compute multi-factor v2 scores with median price context
    prices = [s.unit_price for s in sightings if s.unit_price and s.unit_price > 0]
    median_price = _median(prices)
    target_qty = req.target_qty if req.target_qty else None
    for s in sightings:
        norm_name = s.vendor_name_normalized or ""
        v2_total, v2_comp = score_sighting_v2(
            vendor_score=vendor_score_map.get(norm_name),
            is_authorized=s.is_authorized,
            unit_price=s.unit_price,
            median_price=median_price,
            qty_available=s.qty_available,
            target_qty=target_qty,
            age_hours=0.0,  # Fresh search results are age=0
            has_price=s.unit_price is not None,
            has_qty=s.qty_available is not None,
            has_lead_time=s.lead_time_days is not None,
            has_condition=s.condition is not None,
        )
        s.score = v2_total
        s.score_components = v2_comp

    # Re-apply durable vendor+part unavailability knowledge before the rows
    # commit — a re-search (delete + recreate) must never resurrect a dead
    # vendor. Policy overrides O1/O2 are evaluated per row inside the service.
    apply_to_fresh_sightings(db, req, sightings)

    try:
        db.commit()
    except Exception as e:  # pragma: no cover
        # One bad row shouldn't kill the entire batch — rollback and retry
        # one-by-one, skipping any rows that violate constraints.
        logger.warning(f"Bulk sighting commit failed ({e}), retrying row-by-row")
        db.rollback()
        # Re-delete old sightings (rollback undid the delete above)
        if succeeded_sources and expanded:
            db.query(Sighting).filter(
                Sighting.requirement_id == req.id,
                Sighting.source_type.in_(expanded),
            ).delete(synchronize_session="fetch")
            db.flush()
        else:
            db.query(Sighting).filter_by(requirement_id=req.id).delete()
            db.flush()
        saved = []
        for s in sightings:
            try:
                db.merge(s)
                db.flush()
                saved.append(s)
            except Exception:
                db.rollback()
                logger.warning(f"Skipping bad sighting: {s.source_type}/{s.vendor_name}/{s.mpn_matched}")
        db.commit()
        sightings = saved

    # Dedup: if a vendor+MPN exists in both old (preserved) and fresh, keep fresh
    if succeeded_sources and expanded:
        fresh_keys = {(s.vendor_name.lower(), (s.mpn_matched or "").lower()) for s in sightings}
        old = (
            db.query(Sighting)
            .filter(
                Sighting.requirement_id == req.id,
                ~Sighting.source_type.in_(expanded),
            )
            .all()
        )
        for o in old:
            if (o.vendor_name.lower(), (o.mpn_matched or "").lower()) in fresh_keys:
                db.delete(o)
        db.commit()

    # Propagate vendor emails from search results to VendorContact records
    _propagate_vendor_emails(sightings, db)

    # Write-through canonical sourcing leads + evidence without changing read paths.
    try:
        sync_leads_for_sightings(db, req, sightings)
    except Exception:
        logger.warning("Sourcing lead write-through failed for requirement {}", req.id, exc_info=True)

    # Tag propagation: propagate material card tags to vendor entities
    try:
        from .models import VendorCard
        from .services.tagging import propagate_tags_to_entity

        # PERF-5: resolve every sighting's VendorCard in ONE IN(normalized_names)
        # query instead of one .first() per sighting. normalized_name is unique on
        # VendorCard, so the dict lookup returns exactly the row .first() would have
        # (or None). The (material_card_id, vn_norm) list preserves the original
        # sighting order, so propagate_tags_to_entity is called identically.
        tag_targets: list[tuple[int, str]] = []
        for s in sightings:
            if not s.material_card_id or not s.vendor_name:
                continue
            vn_norm = normalize_vendor_name(s.vendor_name)
            if not vn_norm:
                continue
            tag_targets.append((s.material_card_id, vn_norm))
        if tag_targets:
            norms = {vn for _, vn in tag_targets}
            card_by_norm = {
                vc.normalized_name: vc
                for vc in db.query(VendorCard).filter(VendorCard.normalized_name.in_(norms)).all()
            }
            for material_card_id, vn_norm in tag_targets:
                vc = card_by_norm.get(vn_norm)
                if vc:
                    propagate_tags_to_entity("vendor_card", vc.id, material_card_id, 1.0, db)
        db.commit()
    except Exception:
        logger.warning("Tag propagation failed for sightings", exc_info=True)

    # Rebuild vendor-level sighting summaries for aggregated display
    from .services.sighting_aggregation import rebuild_vendor_summaries_from_sightings

    rebuild_vendor_summaries_from_sightings(db, req.id, sightings)

    return sightings


def _propagate_vendor_emails(sightings: list[Sighting], db: Session):
    """Create VendorContact records from sighting emails (e.g. BrokerBin)."""
    from .models import VendorCard, VendorContact
    from .vendor_utils import merge_emails_into_card, normalize_vendor_name

    # Collect unique vendor_name -> email pairs
    email_map: dict[str, set[str]] = {}
    phone_map: dict[str, set[str]] = {}
    for s in sightings:
        if not s.vendor_email or "@" not in s.vendor_email:
            continue
        vn = (s.vendor_name or "").strip()
        if not vn:
            continue
        email_map.setdefault(vn, set()).add(s.vendor_email.strip().lower())
        if s.vendor_phone:
            phone_map.setdefault(vn, set()).add(s.vendor_phone.strip())

    if not email_map:
        return

    # PERF-5: resolve every vendor's VendorCard in ONE IN(normalized_names) query
    # instead of one .first() per vendor. normalized_name is unique on VendorCard,
    # so the dict lookup returns exactly the row .first() would have (or None).
    # Vendor names that normalize to the same key share the one card object, just as
    # repeated .first() calls returned the same identity-mapped instance.
    name_norms = {name: normalize_vendor_name(name) for name in email_map}
    wanted_norms = {n for n in name_norms.values() if n}
    card_by_norm = (
        {
            card.normalized_name: card
            for card in db.query(VendorCard).filter(VendorCard.normalized_name.in_(wanted_norms)).all()
        }
        if wanted_norms
        else {}
    )

    for vendor_name, emails in email_map.items():
        norm = name_norms.get(vendor_name)
        if not norm:
            continue

        card = card_by_norm.get(norm)
        if not card:
            continue

        # Merge emails into VendorCard.emails JSON array
        merge_emails_into_card(card, list(emails))

        # Create VendorContact records if not exists
        for email in emails:
            existing = db.query(VendorContact).filter_by(vendor_card_id=card.id, email=email).first()
            if existing:
                existing.last_seen_at = datetime.now(timezone.utc)
                continue

            contact = VendorContact(
                vendor_card_id=card.id,
                email=email,
                source="brokerbin",
                confidence=60,
                contact_type="company",
            )
            db.add(contact)

        # Also add phones if available
        phones = phone_map.get(vendor_name, set())
        if phones:
            from .vendor_utils import merge_phones_into_card

            merge_phones_into_card(card, list(phones))

    try:
        db.commit()
    except Exception as e:
        logger.warning("Failed to propagate vendor emails: {}", e)
        db.rollback()


def _get_material_history(material_card_ids: list[int], fresh_vendors: set, db: Session) -> list[dict]:
    """All vendor touchpoints from material cards, excluding vendors with fresh
    sightings."""
    if not material_card_ids:
        return []

    cards = (
        db.query(MaterialCard).filter(MaterialCard.id.in_(material_card_ids), MaterialCard.deleted_at.is_(None)).all()
    )
    if not cards:
        return []

    card_map = {c.id: c for c in cards}
    all_vh = db.query(MaterialVendorHistory).filter(MaterialVendorHistory.material_card_id.in_(material_card_ids)).all()

    from .vendor_utils import normalize_vendor_name as _nvn

    rows = []
    for vh in all_vh:
        vk = _nvn(vh.vendor_name) or vh.vendor_name.lower()
        if vk in fresh_vendors:
            continue
        card = card_map[vh.material_card_id]
        rows.append(
            {
                "vendor_name": vh.vendor_name,
                "mpn_matched": card.display_mpn,
                "manufacturer": vh.last_manufacturer,
                "qty_available": vh.last_qty,
                "unit_price": vh.last_price,
                "currency": vh.last_currency or "USD",
                "source_type": vh.source_type,
                "is_authorized": vh.is_authorized or False,
                "vendor_sku": vh.vendor_sku,
                "first_seen": vh.first_seen,
                "last_seen": vh.last_seen,
                "times_seen": vh.times_seen or 1,
                "material_card_id": card.id,
            }
        )
    return rows


def _history_to_result(h: dict, now: datetime) -> dict:
    last_seen = h["last_seen"]
    age_days = (now - last_seen).days if last_seen else 999

    if age_days < 7:
        base = 55
    elif age_days < 30:
        base = 45
    elif age_days < 90:
        base = 35
    else:
        base = 30
    bonus = min(15, (h["times_seen"] - 1) * 3)
    score = max(10, base + bonus - (age_days * 0.1))

    has_price = h["unit_price"] is not None
    has_qty = h["qty_available"] is not None

    quality = classify_lead(
        score=round(score, 1),
        is_authorized=h["is_authorized"],
        has_price=has_price,
        has_qty=has_qty,
        has_contact=False,
        evidence_tier="T7",
    )
    explanation = explain_lead(
        vendor_name=h["vendor_name"],
        is_authorized=h["is_authorized"],
        unit_price=h["unit_price"],
        qty_available=h["qty_available"],
        has_contact=False,
        evidence_tier="T7",
        age_days=age_days,
    )

    # Unified scoring for historical results
    age_hours = age_days * 24.0 if age_days is not None else None
    unified = score_unified(
        source_type="historical",
        is_authorized=h["is_authorized"],
        unit_price=h["unit_price"],
        qty_available=h["qty_available"],
        age_hours=age_hours,
        has_price=has_price,
        has_qty=has_qty,
        repeat_sighting_count=h.get("times_seen", 1),
    )

    return {
        "id": None,
        "requirement_id": None,
        "vendor_name": h["vendor_name"],
        "vendor_email": None,
        "vendor_phone": None,
        "mpn_matched": h["mpn_matched"],
        "manufacturer": h["manufacturer"],
        "qty_available": h["qty_available"],
        "unit_price": h["unit_price"],
        "currency": h["currency"],
        "source_type": h["source_type"],
        "is_authorized": h["is_authorized"],
        "confidence": 0,
        "score": round(score, 1),
        "source_badge": unified["source_badge"],
        "confidence_pct": unified["confidence_pct"],
        "confidence_color": unified["confidence_color"],
        "reasoning": None,
        "octopart_url": None,
        "click_url": None,
        "vendor_url": None,
        "vendor_sku": h["vendor_sku"],
        "condition": None,
        "moq": None,
        "date_code": None,
        "packaging": None,
        "lead_time_days": None,
        "lead_time": None,
        "evidence_tier": "T7",
        "created_at": last_seen.isoformat() if last_seen else None,
        "is_historical": False,
        "is_material_history": True,
        "is_stale": age_days > 90,
        "material_last_seen": last_seen.strftime("%b %d") if last_seen else None,
        "material_times_seen": h["times_seen"],
        "material_first_seen": h["first_seen"].strftime("%b %d, %Y") if h["first_seen"] else None,
        "material_card_id": h["material_card_id"],
        "lead_quality": quality,
        "lead_explanation": explanation,
    }


def _audit_card_created(db: Session, card: MaterialCard) -> None:
    """Log a 'created' audit entry for a new material card."""
    try:
        from .services.audit_service import log_audit

        log_audit(
            db, material_card_id=card.id, action="created", normalized_mpn=card.normalized_mpn, created_by="system"
        )
    except Exception:
        logger.warning("Audit log failed for card {}", getattr(card, "normalized_mpn", "unknown"), exc_info=True)


def resolve_material_card(mpn: str, db: Session, manufacturer: str = "") -> MaterialCard | None:
    """Find or create a MaterialCard for the given MPN.

    Returns the card (flushed, with id set) or None if MPN is too short.

    Uses atomic INSERT ... ON CONFLICT DO NOTHING on PostgreSQL to eliminate
    race conditions when concurrent requests create the same card.  Falls back
    to try/except for SQLite (tests).
    """
    norm = normalize_mpn_key(mpn)
    if not norm:
        return None

    # Fast path — card already exists (no write, cheapest possible check)
    card = db.query(MaterialCard).filter_by(normalized_mpn=norm).filter(MaterialCard.deleted_at.is_(None)).first()
    if card:
        if manufacturer and not card.manufacturer:
            card.manufacturer = manufacturer
        logger.debug("MC_METRIC: action=resolved mpn={} card_id={}", norm, card.id)
        return card

    display = normalize_mpn(mpn) or mpn.strip()

    dialect = db.bind.dialect.name if db.bind else ""
    if dialect == "postgresql":  # pragma: no cover
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        stmt = (
            pg_insert(MaterialCard)
            .values(
                normalized_mpn=norm,
                display_mpn=display,
                search_count=0,
                manufacturer=manufacturer,
            )
            .on_conflict_do_nothing(
                index_elements=["normalized_mpn"],
                index_where=MaterialCard.deleted_at.is_(None),
            )
        )
        result = db.execute(stmt)
        db.flush()
        # Re-fetch (unfiltered — may be soft-deleted and needs restoring)
        card = db.query(MaterialCard).filter_by(normalized_mpn=norm).first()
        if card is None:
            logger.error("MATERIAL_CARD_RESOLVE_FAIL: card missing after ON CONFLICT for mpn={}", norm)
        elif card.deleted_at is not None:
            # Restore soft-deleted card
            card.deleted_at = None
            logger.info("MC_METRIC: action=restored mpn={} card_id={}", norm, card.id)
            _audit_card_created(db, card)
        elif result.rowcount == 0:
            logger.info("MC_METRIC: action=race_resolved mpn={} card_id={}", norm, card.id)
        else:
            logger.info("MC_METRIC: action=created mpn={} card_id={}", norm, card.id)
            _audit_card_created(db, card)
        return card
    else:
        # SQLite / test fallback — use try/except on IntegrityError
        from sqlalchemy.exc import IntegrityError

        try:
            card = MaterialCard(normalized_mpn=norm, display_mpn=display, search_count=0, manufacturer=manufacturer)
            db.add(card)
            db.flush()
            logger.info("MC_METRIC: action=created mpn={} card_id={}", norm, card.id)
            _audit_card_created(db, card)
            return card
        except IntegrityError:
            db.rollback()
            logger.info("MC_METRIC: action=race_resolved mpn={}", norm)
            card = db.query(MaterialCard).filter_by(normalized_mpn=norm).first()
            # Restore if soft-deleted
            if card and card.deleted_at is not None:
                card.deleted_at = None
                db.flush()
                logger.info("MC_METRIC: action=restored mpn={} card_id={}", norm, card.id)
            return card


def _upsert_material_card(pn: str, sightings: list[Sighting], db: Session, now: datetime) -> MaterialCard | None:
    """Upsert material card + link sightings.

    Raises on error — caller handles rollback.
    """
    norm = normalize_mpn_key(pn)
    if not norm:
        return None
    pn_sightings = [s for s in sightings if normalize_mpn_key(s.mpn_matched or "") == norm]
    if not pn_sightings:
        return None

    card = resolve_material_card(pn, db)

    card.search_count = (card.search_count or 0) + 1
    card.last_searched_at = now
    if not card.manufacturer:
        for s in pn_sightings:
            if s.manufacturer:
                card.manufacturer = s.manufacturer
                break

    # Batch fetch all existing vendor histories for this card (avoids N+1).
    # Key by normalized vendor name so "ARROW", "Arrow", "arrow" all match.
    existing_vh = {
        normalize_vendor_name(vh.vendor_name): vh
        for vh in db.query(MaterialVendorHistory).filter_by(material_card_id=card.id).all()
    }

    for s in pn_sightings:
        if not s.vendor_name:
            continue
        raw = s.raw_data or {}
        vn_key = normalize_vendor_name(s.vendor_name)
        vh = existing_vh.get(vn_key)

        if vh:
            vh.last_seen = now
            vh.times_seen = (vh.times_seen or 1) + 1
            if s.qty_available is not None:
                vh.last_qty = s.qty_available
            if s.unit_price is not None:
                vh.last_price = s.unit_price
                record_price_snapshot(
                    db=db,
                    material_card_id=card.id,
                    vendor_name=s.vendor_name,
                    price=s.unit_price,
                    currency=s.currency or "USD",
                    quantity=s.qty_available,
                    source="api_sighting",
                )
            if s.currency:
                vh.last_currency = s.currency
            if s.manufacturer:
                vh.last_manufacturer = s.manufacturer
            if s.is_authorized:
                vh.is_authorized = True
            if raw.get("vendor_sku"):
                vh.vendor_sku = raw["vendor_sku"]
        else:
            vn_norm = normalize_vendor_name(s.vendor_name) or s.vendor_name
            new_vh = MaterialVendorHistory(
                material_card_id=card.id,
                vendor_name=vn_norm,
                vendor_name_normalized=vn_norm,
                source_type=s.source_type,
                is_authorized=s.is_authorized or False,
                first_seen=now,
                last_seen=now,
                times_seen=1,
                last_qty=s.qty_available,
                last_price=s.unit_price,
                last_currency=s.currency or "USD",
                last_manufacturer=s.manufacturer,
                vendor_sku=raw.get("vendor_sku"),
            )
            db.add(new_vh)
            record_price_snapshot(
                db=db,
                material_card_id=card.id,
                vendor_name=s.vendor_name,
                price=s.unit_price,
                currency=s.currency or "USD",
                quantity=s.qty_available,
                source="api_sighting",
            )
            existing_vh[vn_key] = new_vh  # Prevent dupe inserts within batch

    # Link sightings to material card + populate normalized_mpn
    for s in pn_sightings:
        if not s.material_card_id:
            s.material_card_id = card.id
        if not s.normalized_mpn and s.mpn_matched:
            s.normalized_mpn = normalize_mpn_key(s.mpn_matched)

    db.commit()

    # Tag classification: if manufacturer is now set, classify and tag the card
    try:
        if card.manufacturer:
            from .services.tagging import (
                classify_material_card,
                get_or_create_brand_tag,
                get_or_create_commodity_tag,
                tag_material_card,
            )

            result = classify_material_card(card.normalized_mpn, card.manufacturer, card.category)
            tags_to_apply = []
            if result.get("brand"):
                brand_tag = get_or_create_brand_tag(result["brand"]["name"], db)
                tags_to_apply.append(
                    {
                        "tag_id": brand_tag.id,
                        "source": result["brand"]["source"],
                        "confidence": result["brand"]["confidence"],
                    }
                )
            if result.get("commodity"):
                commodity_tag = get_or_create_commodity_tag(result["commodity"]["name"], db)
                if commodity_tag:
                    tags_to_apply.append(
                        {
                            "tag_id": commodity_tag.id,
                            "source": result["commodity"]["source"],
                            "confidence": result["commodity"]["confidence"],
                        }
                    )
            if tags_to_apply:
                tag_material_card(card.id, tags_to_apply, db)
                db.commit()
    except Exception:
        logger.warning("Tag classification failed for card {}", card.id, exc_info=True)

    return card


def run_deterministic_passes(db: Session, card_ids: list[int] | set[int]) -> None:
    """Run the three inline deterministic enrichment passes over *card_ids*.

    On-create pipeline (on-add enrichment): zero-network, pure CPU + local queries
    (~15ms/card), shared session, no commit — the caller owns the transaction. Order
    mirrors the enrichment worker's second pass (mpn_decode 85 → fru_matrix_decode 84 →
    desc_parse 83) but run order is NOT load-bearing: the F1 tier ladder
    (app/services/spec_tiers.py) arbitrates every write. Idempotent — re-running over
    an existing card re-asserts the same values through the ladder.

    Called by: POST /api/materials/add, the bulk part-number / stock imports
    (routers/materials.py), and search_requirement's write session below — every
    user-action card-creation path. Respects the same feature flags as the worker.
    """
    ids = sorted(int(i) for i in card_ids)
    if not ids:
        return
    from .config import settings as _settings

    def _run_pass(name: str, fn) -> None:
        # SAVEPOINT per pass: the writers carry per-card savepoints internally, but DB
        # errors escaping those (batched lookup queries, db.get loops, schema-cache
        # loads run outside them) abort the whole PostgreSQL transaction — every later
        # statement then raises InFailedSqlTransaction, so the caller's single commit
        # would 500 and roll back the just-created card(s)/import rows/sightings
        # despite this except "handling" the failure. Rolling back to the savepoint
        # confines a poisoned pass to its own writes. (SQLite tests cannot reproduce
        # the aborted-transaction mode — feedback_sqlite_masks_postgres — so this
        # savepoint IS the guard; verify behavior changes against live PG.)
        try:
            with db.begin_nested():
                logger.info("INLINE_ENRICH: {} {}", name, fn(db, ids))
        except Exception:
            logger.exception(
                "INLINE_ENRICH: {} failed over {} card(s) ids={} — pass rolled back, card creation proceeds",
                name,
                len(ids),
                ids[:50],
            )

    if _settings.mpn_decode_enabled:
        from .services.mpn_decoder.writer import decode_and_record_specs

        _run_pass("mpn-decode", decode_and_record_specs)
    if _settings.fru_crosswalk_enrich_enabled:
        from .services.fru_crosswalk_enrich import crosswalk_and_record_specs

        _run_pass("fru-crosswalk", crosswalk_and_record_specs)
    if _settings.desc_parse_enabled:
        from .services.desc_extractor.writer import extract_and_record_specs

        _run_pass("desc-parse", extract_and_record_specs)


async def _schedule_background_enrichment(card_ids: set[int], db: Session) -> None:
    """Fire background connector enrichment for cards missing a manufacturer."""
    if not card_ids:
        return

    cards_needing_enrichment = (
        db.query(MaterialCard.id, MaterialCard.normalized_mpn)
        .filter(MaterialCard.id.in_(card_ids))
        .filter(MaterialCard.manufacturer.is_(None) | (MaterialCard.manufacturer == ""))
        .all()
    )

    if not cards_needing_enrichment:
        return

    logger.info(f"Scheduling background enrichment for {len(cards_needing_enrichment)} cards")

    async def _enrich_cards():
        from .database import SessionLocal
        from .services.enrichment import _apply_enrichment_to_card, enrich_material_card

        session = SessionLocal()
        try:
            for card_id, mpn in cards_needing_enrichment:
                try:
                    result = await enrich_material_card(mpn, session)
                    if result:
                        card = session.get(MaterialCard, card_id)
                        if card:
                            _apply_enrichment_to_card(card, result, session)
                            session.commit()
                except Exception:
                    logger.warning("Background enrichment failed for {}", mpn, exc_info=True)
                    session.rollback()
        finally:
            session.close()

    await safe_background_task(_enrich_cards(), task_name="enrich_search_cards")


def sighting_to_dict(s: Sighting) -> dict:
    raw = s.raw_data or {}
    has_contact = bool(s.vendor_email or s.vendor_phone)
    has_price = s.unit_price is not None
    has_qty = s.qty_available is not None
    tier = getattr(s, "evidence_tier", None)
    score = s.score or 0

    age_days = None
    if s.created_at:
        ca = s.created_at.replace(tzinfo=timezone.utc) if s.created_at.tzinfo is None else s.created_at
        age_days = (datetime.now(timezone.utc) - ca).days

    quality = classify_lead(
        score=score,
        is_authorized=s.is_authorized,
        has_price=has_price,
        has_qty=has_qty,
        has_contact=has_contact,
        evidence_tier=tier,
    )
    explanation = explain_lead(
        vendor_name=s.vendor_name,
        is_authorized=s.is_authorized,
        vendor_score=None,
        unit_price=s.unit_price,
        qty_available=s.qty_available,
        has_contact=has_contact,
        evidence_tier=tier,
        source_type=s.source_type,
        age_days=age_days,
    )

    # Unified scoring — adds source_badge, confidence_pct, confidence_color
    age_hours = (age_days * 24.0) if age_days is not None else None
    unified = score_unified(
        source_type=s.source_type or "",
        is_authorized=s.is_authorized,
        unit_price=s.unit_price,
        qty_available=s.qty_available,
        age_hours=age_hours,
        has_price=has_price,
        has_qty=has_qty,
        has_lead_time=s.lead_time_days is not None or bool(s.lead_time),
        has_condition=bool(s.condition or (raw.get("condition"))),
        claude_confidence=s.confidence,
    )

    return {
        "id": s.id,
        "requirement_id": s.requirement_id,
        "vendor_name": s.vendor_name,
        "vendor_email": s.vendor_email,
        "vendor_phone": s.vendor_phone,
        "mpn_matched": s.mpn_matched,
        "manufacturer": s.manufacturer,
        "qty_available": s.qty_available,
        "unit_price": s.unit_price,
        "currency": s.currency,
        "source_type": s.source_type,
        "is_authorized": s.is_authorized,
        "confidence": s.confidence,
        "score": score,
        "source_badge": unified["source_badge"],
        "confidence_pct": unified["confidence_pct"],
        "confidence_color": unified["confidence_color"],
        "reasoning": None,
        "is_unavailable": getattr(s, "is_unavailable", False) or False,
        "octopart_url": raw.get("octopart_url"),
        "click_url": raw.get("click_url"),
        "vendor_url": raw.get("vendor_url"),
        "vendor_sku": raw.get("vendor_sku"),
        "condition": s.condition or raw.get("condition"),
        "country": raw.get("country"),
        "moq": s.moq,
        "date_code": s.date_code,
        "packaging": s.packaging,
        "lead_time_days": s.lead_time_days,
        "lead_time": s.lead_time,
        "evidence_tier": tier,
        "score_components": getattr(s, "score_components", None),
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "is_stale": (age_days or 0) > 90,
        "lead_quality": quality,
        "lead_explanation": explanation,
    }


# ── Streaming search ────────────────────────────────────────────────────


def _score_raw_hit(r: dict, vendor_score_map: dict) -> dict:
    """Normalize and score a single raw connector result for streaming search.

    Mirrors the scoring in _fetch_fresh but without v2/median context (not
    available incrementally). Produces fields needed by _incremental_dedup
    and vendor card rendering.

    Called by: stream_search_mpn
    Depends on: scoring, evidence_tiers, normalization utilities
    """
    from .evidence_tiers import tier_for_sighting

    raw_vendor = r.get("vendor_name", "Unknown")
    clean_vendor = fix_encoding((raw_vendor or "").strip()) or raw_vendor
    raw_mpn = r.get("mpn_matched")
    clean_mpn_r = normalize_mpn(raw_mpn) or raw_mpn

    clean_qty = normalize_quantity(r.get("qty_available"))
    if clean_qty is None and isinstance(r.get("qty_available"), (int, float)) and r["qty_available"] > 0:
        clean_qty = int(r["qty_available"])

    clean_price = normalize_price(r.get("unit_price"))
    if clean_price is None and isinstance(r.get("unit_price"), (int, float)) and r["unit_price"] > 0:
        clean_price = float(r["unit_price"])

    raw_currency = r.get("currency") or "USD"
    clean_currency = detect_currency(raw_currency) if raw_currency else "USD"
    raw_conf = r.get("confidence", 0) or 0
    norm_conf = raw_conf / 5.0 if raw_conf > 1 else raw_conf
    is_auth = r.get("is_authorized", False)
    norm_name = normalize_vendor_name(clean_vendor)
    base_score = score_sighting(vendor_score_map.get(norm_name), is_auth)
    tier = tier_for_sighting(r.get("source_type"), is_auth)

    return {
        "vendor_name": clean_vendor,
        "vendor_email": r.get("vendor_email"),
        "vendor_phone": r.get("vendor_phone"),
        "mpn_matched": clean_mpn_r,
        "manufacturer": r.get("manufacturer"),
        "qty_available": clean_qty,
        "unit_price": clean_price,
        "currency": clean_currency,
        "source_type": r.get("source_type"),
        "is_authorized": is_auth,
        "confidence": norm_conf,
        "score": base_score,
        "evidence_tier": tier,
        "octopart_url": r.get("octopart_url"),
        "click_url": r.get("click_url"),
        "vendor_url": r.get("vendor_url"),
        "vendor_sku": r.get("vendor_sku"),
        "condition": normalize_condition(r.get("condition")),
        "moq": r.get("moq") if r.get("moq") and r.get("moq") > 0 else None,
        "date_code": normalize_date_code(r.get("date_code")),
        "packaging": normalize_packaging(r.get("packaging")),
        "lead_time_days": normalize_lead_time(r.get("lead_time")),
        "lead_time": r.get("lead_time"),
        "country": r.get("country"),
        "lead_quality": classify_lead(
            score=base_score,
            is_authorized=is_auth,
            has_price=clean_price is not None,
            has_qty=clean_qty is not None,
            has_contact=bool(r.get("vendor_email") or r.get("vendor_phone")),
            evidence_tier=tier,
        ),
    }


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

    Called by: routers/htmx_views.py::search_run (POST /v2/partials/search/run)
    Depends on: _build_connectors, _incremental_dedup, services/sse_broker.broker
    """
    # Allow test mocks to override the broker via module-level patching
    import app.search_service as _self_mod

    from .config import settings
    from .services.sse_broker import broker as _broker

    active_broker = getattr(_self_mod, "broker", _broker)

    channel = f"search:{search_id}"
    accumulated: list[dict] = []
    total_results = 0
    off_target_total = 0  # hits excluded by the relevance guard (different MPN)
    sources_completed = 0
    t_start = time.time()

    db = None
    try:
        db = SessionLocal()
        try:
            connectors, source_stats_map, _disabled = _build_connectors(db)

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
            from .models import VendorCard

            vendor_cards = db.query(VendorCard.normalized_name, VendorCard.vendor_score).all()
            vendor_score_map = {vc.normalized_name: vc.vendor_score for vc in vendor_cards}

            # Create a task per connector, tagging with source_name
            task_map: dict[asyncio.Task, str] = {}
            for conn in connectors:
                source_name = getattr(
                    conn, "source_name", _CONNECTOR_SOURCE_MAP.get(conn.__class__.__name__, "unknown")
                )

                async def _run(c=conn, pn=mpn):
                    t0 = time.time()
                    hits = await c.search(pn)
                    elapsed = int((time.time() - t0) * 1000)
                    return hits, elapsed

                task = asyncio.create_task(_run())
                task_map[task] = source_name

            pending = set(task_map.keys())

            # Per-source telemetry accumulated across the run, flushed to ApiSource in
            # one guarded pass after the loop (mirrors _fetch_fresh) so streaming
            # failures/latency show up in admin health — the interactive path recorded
            # zero telemetry before. Tuples: (source, hit_count, elapsed_ms, error).
            stats_updates: list[tuple[str, int, int, str | None]] = []

            # Aggregate deadline: the interactive SSE search shares the requisition
            # path's budget. Track the remaining budget each round; when it is spent,
            # cancel the stragglers, publish a timeout chip for each, and stop — so one
            # hung/rate-limited connector cannot hold the browser spinner for minutes.
            budget_s = settings.search_total_timeout_s
            while pending:
                remaining = budget_s - (time.time() - t_start)
                done, pending, timed_out = await _await_next_within_budget(pending, remaining)

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

                        # Score and normalize each on-target hit
                        scored_hits = [_score_raw_hit(r, vendor_score_map) for r in on_target]

                        # Incremental dedup against accumulated results
                        new_cards, updated_cards = _incremental_dedup(scored_hits, accumulated)

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
                            cards_html = _render_search_vendor_cards_html(
                                new_cards,
                                search_id=search_id,
                                start_index=start_idx,
                                swap_oob=False,
                            )
                            await active_broker.publish(channel, "results", cards_html)

                        # Publish updated cards as OOB HTML so existing vendor-card nodes refresh
                        if updated_cards:
                            update_html = "".join(
                                _render_search_vendor_cards_html(
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
                        src.last_success = datetime.now(timezone.utc)
                        prev = src.avg_response_ms or elapsed_ms
                        src.avg_response_ms = (prev * 3 + elapsed_ms) // 4
                        src.status = ApiSourceStatus.LIVE.value
                        src.last_error = None
                    else:
                        src.last_error = error
                        src.last_error_at = datetime.now(timezone.utc)
                        src.error_count_24h = (src.error_count_24h or 0) + 1
                db.commit()
            except Exception as e:
                logger.warning("API source stats update failed (streaming): {}", e)
                db.rollback()

            # Cache results for filter endpoint (15-min TTL). Also write a per-MPN
            # pointer key (search:{key}:latest → this search_id, same TTL) so the Part
            # Dossier market section can find the freshest run for an MPN without knowing
            # the search_id (cache-hit path in routers/part_dossier.dossier_market).
            try:
                rc = _get_search_redis()
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
