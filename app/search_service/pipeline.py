"""Search package — the two orchestrating entry points: requirement search
(search_requirement) and the ad-hoc quick MPN search, plus the threaded vendor-affinity
lookup.

W4.5a split of app/search_service.py — pure structural move (see cache.py header).
"""

import asyncio
from datetime import UTC, datetime

from loguru import logger
from sqlalchemy.orm import Session

from ..constants import ActivityType, SourceRunStatus
from ..database import SessionLocal
from ..models import MaterialCard, Requirement, Sighting
from ..scoring import classify_lead, is_weak_lead, score_sighting_v2
from ..services.activity_service import log_activity
from ..services.ics_worker.queue_manager import enqueue_for_ics_search
from ..services.nc_worker.queue_manager import enqueue_for_nc_search
from ..services.tbf_worker.queue_manager import enqueue_for_tbf_search
from ..services.vendor_affinity_service import find_vendor_affinity
from ..utils.currency import to_usd
from ..utils.normalization import (
    detect_currency,
    normalize_condition,
    normalize_date_code,
    normalize_lead_time,
    normalize_mpn,
    normalize_mpn_key,
    normalize_packaging,
    normalize_price,
    normalize_quantity,
)
from ..utils.normalization_helpers import fix_encoding
from ..vendor_utils import normalize_vendor_name
from . import cache, dedupe, fanout, material_cards, mpn_expansion, persistence, presentation


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
    pns = mpn_expansion.get_all_pns(req)
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
    fru_aliases = mpn_expansion._expand_fru_aliases(db, req)
    if fru_aliases:
        mpn_expansion._persist_fru_aliases(db, req.id, fru_aliases)
        pns = pns + [a["mpn"] for a in fru_aliases]
        logger.info("Req {} ({}): injected {} FRU crosswalk alias(es) into search", req.id, pns[0], len(fru_aliases))

    now = datetime.now(UTC)

    # 48h per-normalized-MPN cooldown. Split into MPNs that need a connector
    # call vs. ones whose MaterialCard.last_searched_at is recent enough.
    to_search, _cached_card_ids = mpn_expansion._mpn_cooldown_partition(db, pns, now=now)

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
        affinity_results = [presentation._affinity_match_to_result(m, primary_mpn) for m in affinity_matches]
        return {
            "sightings": affinity_results,
            "source_stats": [],
            "mpn_results": mpn_results,
        }

    # 1. Fetch + dedupe (parallel across stale-MPN connectors). Affinity was
    # already computed above so it's available to the merge step below.
    fresh, source_stats = await fanout._fetch_fresh(to_search, db)

    # 2. Score + save — only replace sightings from connectors that succeeded.
    # The whole save+upsert+deterministic-pass chain is synchronous DB work with
    # no network I/O; running it directly on the event loop stalled every other
    # in-flight request for its duration (PERF-3). It runs on a worker thread via
    # persistence._persist_search_write, which opens its OWN dedicated write session bound to
    # this session's connection — SQLAlchemy sessions are not thread-safe, so
    # write_db is created and used ENTIRELY inside the thread (mirrors the
    # vendor-affinity fix at _find_affinity_in_thread, PERF-1). Only plain
    # dicts/ids cross back — no ORM object survives the thread boundary.
    req_id = req.id
    succeeded_sources = {
        stat["source"] for stat in source_stats if stat["status"] == SourceRunStatus.OK.value and not stat.get("error")
    }
    write_bind = db.get_bind()
    persisted = await asyncio.to_thread(
        persistence._persist_search_write, req_id, fresh, to_search, succeeded_sources, searched_keys, now, write_bind
    )
    if persisted is None:
        return {"sightings": [], "source_stats": source_stats, "mpn_results": mpn_results}

    sighting_dicts: list[dict] = persisted["sighting_dicts"]
    card_ids: set[int] = set(persisted["card_ids"])
    requisition_id = persisted["requisition_id"]

    # 3b. Fire background enrichment for cards without manufacturer. Async I/O
    # (spins its own SessionLocal internally for the actual work) so it stays on
    # the event loop; the read query it runs first is safe against the request
    # session `db` since _persist_search_write already committed the cards.
    await material_cards._schedule_background_enrichment(card_ids, db)

    # 4. Historical vendors from material cards (read-only — uses the request
    # session, which sees the committed writes from the thread above).
    fresh_vendors = {d["vendor_name"].lower() for d in sighting_dicts if d.get("vendor_name")}
    history = material_cards._get_material_history(list(card_ids), fresh_vendors, db)

    # --- Spec-code resolver fallback (spec §6) ---
    # Trigger only on a hard zero from the synchronous fanout AND the feature
    # flag. The async ICS/NC workers run independently below for the primary
    # MPN regardless of resolver outcome. Kept UNTHREADED (unlike the primary
    # save path above): it makes its own ~60s grounded Claude call already, so
    # it can't avoid blocking briefly regardless, and it only fires on the rare
    # zero-hit + feature-flagged path — not worth the added complexity of a
    # second threaded write session for a cold path. Opens its OWN write
    # session (mirrors _persist_fru_aliases) since the thread's write_db above
    # is already closed.
    from ..config import settings as _settings

    resolved_dicts: list[dict] = []
    if _settings.spec_resolver_enabled and len(sighting_dicts) == 0 and req.primary_mpn:
        from sqlalchemy.orm import sessionmaker as _sessionmaker

        _ResolverSession = _sessionmaker(bind=write_bind, autocommit=False, autoflush=False, expire_on_commit=False)
        resolver_db = _ResolverSession()
        try:
            resolver_write_req = resolver_db.get(Requirement, req_id)
            if resolver_write_req is None:
                logger.warning("spec_resolver: requirement {} not found in resolver session", req_id)
            else:
                try:
                    from app.services.spec_code_resolver import SpecCodeResolver

                    # resolve() owns its own persistence SAVEPOINT and releases the DB
                    # connection during the grounded LLM call, so we do NOT wrap it in a
                    # transaction here — doing so would pin a pooled connection for the
                    # call's ~60s duration. The sightings committed just above are durable
                    # and unaffected, and a concurrent-insert race is recovered inside
                    # resolve() (it reuses the winning row).
                    resolver = SpecCodeResolver(resolver_db)
                    resolution = await resolver.resolve(
                        resolver_write_req.primary_mpn,
                        oem=resolver_write_req.oem_hint or "IBM",
                    )
                except Exception:
                    logger.warning(
                        "spec_resolver: resolve() failed for req {} mpn {}",
                        req_id,
                        resolver_write_req.primary_mpn,
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
                        to_fetch_avl, _cached_avl = mpn_expansion._mpn_cooldown_partition(
                            resolver_db, avl_mpns, now=now
                        )

                        # Issue 3 fix: explicit try/except distinguishes "connectors
                        # crashed" from "no AVL hits". Design intent: still enqueue
                        # async workers + write pending bookkeeping even when the
                        # live connectors fail — the buyer benefits from worker
                        # output independent of connector outages.
                        try:
                            if to_fetch_avl:
                                resolved_fresh, resolved_stats = await fanout._fetch_fresh(to_fetch_avl, db)
                            else:
                                resolved_fresh, resolved_stats = [], []
                        except Exception:
                            logger.warning(
                                "spec_resolver: AVL fanout failed for req {} (spec_code={}); "
                                "still enqueueing workers for async pickup",
                                req_id,
                                resolver_write_req.primary_mpn,
                                exc_info=True,
                            )
                            resolved_fresh, resolved_stats = [], []

                        spec_code_tag = resolver_write_req.primary_mpn
                        for row in resolved_fresh:
                            row["resolved_via_spec_code"] = spec_code_tag
                            row["source_mpn"] = row.get("mpn") or row.get("mpn_matched")

                        resolved_succeeded = {
                            stat["source"]
                            for stat in resolved_stats
                            if stat.get("status") == SourceRunStatus.OK.value and not stat.get("error")
                        }
                        if resolved_fresh:
                            resolved_sightings = persistence._save_sightings(
                                resolved_fresh, resolver_write_req, resolver_db, resolved_succeeded
                            )
                            resolved_dicts = [presentation.sighting_to_dict(s) for s in resolved_sightings]
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
                                avl_card = material_cards._upsert_material_card(
                                    avl_pn, resolved_sightings, resolver_db, now
                                )
                                if avl_card is None:
                                    avl_card = material_cards.resolve_material_card(avl_pn, resolver_db)
                                if avl_card:
                                    avl_card.last_searched_at = now
                            except Exception as e:
                                logger.error("AVL_MATERIAL_CARD_STAMP_FAIL: mpn={} error={}", avl_pn, e)
                                resolver_db.rollback()

                        # Enqueue each AVL MPN to ICS and NC workers in addition
                        # to the primary-MPN enqueue below.
                        for mpn in avl_mpns:
                            try:
                                enqueue_for_ics_search(
                                    req_id,
                                    resolver_db,
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
                                    resolver_db,
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
                                    resolver_db,
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
                            oem_normalized = (resolver_write_req.oem_hint or "IBM").strip().upper()
                            spec_code_normalized = spec_code_tag.strip().upper()
                            pending_row = (
                                resolver_db.query(OemSpecCodePending)
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
                                resolver_db.commit()
        except Exception:
            resolver_db.rollback()
            raise
        finally:
            resolver_db.close()
    # --- end resolver block ---

    all_sighting_dicts = sighting_dicts + resolved_dicts

    # Aggregated activity-timeline entry: one row per search batch, never one
    # per sighting. Skipped for zero-result searches so the timeline stays free
    # of noise. Logged after the resolver fallback so the count reflects any
    # AVL sightings the resolver appended.
    if all_sighting_dicts:
        _sighting_sources = sorted(succeeded_sources)
        log_activity(
            db,
            activity_type=ActivityType.SIGHTING_ADDED,
            requisition_id=requisition_id,
            requirement_id=req_id,
            user_id=None,
            channel="system",
            description=(
                f"{len(all_sighting_dicts)} sighting(s) added"
                + (f" from {', '.join(_sighting_sources)}" if _sighting_sources else "")
            ),
            details={"count": len(all_sighting_dicts), "sources": _sighting_sources},
        )
        db.commit()

    # Browser-automation workers: best-effort enqueue once per call. Both
    # workers key by requirement_id and internally normalize req.primary_mpn,
    # so per-substitute iteration would just round-trip dedup checks. Called
    # after the write thread's commit, so the worker reads the same durable
    # state we just wrote.
    try:
        enqueue_for_ics_search(req_id, db)
    except Exception:
        logger.warning("ICS enqueue failed for requirement {}", req_id, exc_info=True)
    try:
        enqueue_for_nc_search(req_id, db)
    except Exception:
        logger.warning("NC enqueue failed for requirement {}", req_id, exc_info=True)
    try:
        enqueue_for_tbf_search(req_id, db)
    except Exception:
        logger.warning("TBF enqueue failed for requirement {}", req_id, exc_info=True)

    # 5. Combine + sort
    results = []
    for d in all_sighting_dicts:
        d = dict(d)
        d["is_historical"] = False
        d["is_material_history"] = False
        results.append(d)

    for h in history:
        results.append(material_cards._history_to_result(h, now))

    # 5b. Merge vendor affinity suggestions (skip vendors already in live results)
    live_vendors = {r.get("vendor_name", "").lower() for r in results}
    for match in affinity_matches:
        vendor_lower = match.get("vendor_name", "").lower()
        if vendor_lower in live_vendors:
            continue
        live_vendors.add(vendor_lower)
        results.append(presentation._affinity_match_to_result(match, primary_mpn))
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

    # 7. Flag price outliers — historical results 20x+ above fresh median. Flag only
    # (spec §9): the old 0.2x score mutation re-derived a display number that was
    # never written back, so the screen disagreed with the persisted v2 score.
    fresh_prices = [r["unit_price"] for r in results if not r.get("is_material_history") and r.get("unit_price")]
    if fresh_prices:
        median_price = cache._median(fresh_prices)
        if median_price is not None and median_price > 0:
            for r in results:
                p = r.get("unit_price")
                if p and p > median_price * 20:
                    r["price_outlier"] = True

    results = dedupe._deduplicate_sightings(results)

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

    results.sort(key=lambda x: (x.get("confidence_pct") or 0, x.get("score") or 0), reverse=True)
    return {"sightings": results, "source_stats": source_stats, "mpn_results": mpn_results}


async def quick_search_mpn(mpn: str, db: Session) -> dict:
    """Ad-hoc MPN search — hits supplier APIs without needing a Requirement.

    Returns live API results + material card history, scored and deduped.
    Does NOT persist sightings (read-only quick check).

    Called by: routers/materials.py (POST /api/quick-search)
    Depends on: _fetch_fresh, _get_material_history, scoring, normalization
    """
    from ..evidence_tiers import tier_for_sighting

    clean_mpn = normalize_mpn(mpn) or mpn.strip().upper()
    if not clean_mpn:
        return {"sightings": [], "source_stats": [], "material_card": None}

    pns = [clean_mpn]
    now = datetime.now(UTC)

    # 1. Hit all supplier APIs
    fresh, source_stats = await fanout._fetch_fresh(pns, db)

    # 2. Build vendor score lookup
    needed_names = {normalize_vendor_name((r.get("vendor_name") or "").strip()) for r in fresh if r.get("vendor_name")}
    needed_names.discard("")
    vendor_score_map = {}
    if needed_names:
        from ..models import VendorCard

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
        tier = tier_for_sighting(r.get("source_type"), is_auth)
        raw_moq = r.get("moq")

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
                "octopart_url": r.get("octopart_url"),
                "click_url": r.get("click_url"),
                "vendor_url": r.get("vendor_url"),
                "vendor_sku": r.get("vendor_sku"),
                "condition": normalize_condition(r.get("condition")),
                "moq": raw_moq if raw_moq and raw_moq > 0 else None,
                "date_code": normalize_date_code(r.get("date_code")),
                "packaging": normalize_packaging(r.get("packaging")),
                "lead_time_days": normalize_lead_time(r.get("lead_time")),
                "lead_time": r.get("lead_time"),
                "evidence_tier": tier,
                "created_at": now.isoformat(),
                "is_historical": False,
                "is_material_history": False,
                "country": r.get("country"),
            }
        )

    # 4. The ONE v2 scoring pass, with median price context (spec §9 — the old v1
    # trust-only base_score seed is gone, so lead_quality is classified from the
    # same v2 number the row displays). Prices are converted to USD before
    # the median and the per-offer comparison so a search mixing e.g. JPY and USD
    # listings doesn't compare raw numbers across currencies (currency-blind price
    # scoring bug). `results` is built 1:1 from `fresh` above (no filtering), so
    # zip pairs each scored row with its source dict — `fresh`'s real freshness tag
    # (`_source_age_hours`, 0.0 for a live fetch, >0 for a Redis-cache-served row)
    # replaces the previously-hardcoded age_hours=0.0.
    prices_usd = [
        p
        for p in (
            to_usd(r["unit_price"], r.get("currency")) for r in results if r.get("unit_price") and r["unit_price"] > 0
        )
        if p is not None
    ]
    median_price = cache._median(prices_usd)
    for r, src in zip(results, fresh):
        norm_name = normalize_vendor_name(r["vendor_name"])
        v2_total, v2_comp = score_sighting_v2(
            vendor_score=vendor_score_map.get(norm_name),
            is_authorized=r["is_authorized"],
            unit_price=to_usd(r["unit_price"], r.get("currency")),
            median_price=median_price,
            qty_available=r["qty_available"],
            target_qty=None,
            age_hours=src.get("_source_age_hours", 0.0),
            has_price=r["unit_price"] is not None,
            has_qty=r["qty_available"] is not None,
            has_lead_time=r.get("lead_time_days") is not None,
            has_condition=r.get("condition") is not None,
        )
        r["score"] = v2_total
        r["score_components"] = v2_comp
        r["lead_quality"] = classify_lead(
            score=v2_total,
            is_authorized=r["is_authorized"],
            has_price=r["unit_price"] is not None,
            has_qty=r["qty_available"] is not None,
            has_contact=bool(r.get("vendor_email") or r.get("vendor_phone")),
            evidence_tier=r.get("evidence_tier"),
        )

    # 5. Material card history
    norm_key = normalize_mpn_key(clean_mpn)
    card = db.query(MaterialCard).filter_by(normalized_mpn=norm_key).filter(MaterialCard.deleted_at.is_(None)).first()
    card_ids = [card.id] if card else []
    fresh_vendors = {(r["vendor_name"] or "").lower() for r in results}
    history = material_cards._get_material_history(card_ids, fresh_vendors, db)
    for h in history:
        results.append(material_cards._history_to_result(h, now))

    # 6. Dedupe, filter weak leads, sort
    results = dedupe._deduplicate_sightings(results)
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
    results.sort(key=lambda x: (x.get("confidence_pct") or 0, x.get("score") or 0), reverse=True)

    # 7. Material card summary (if exists)
    card_summary = None
    if card:
        from ..models import Offer

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
