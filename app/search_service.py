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
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy.orm import Session

from .connectors.ai_live_web import AIWebSearchConnector
from .connectors.digikey import DigiKeyConnector
from .connectors.ebay import EbayConnector
from .connectors.element14 import Element14Connector
from .connectors.mouser import MouserConnector
from .connectors.oemsecrets import OEMSecretsConnector
from .connectors.sourcengine import SourcengineConnector
from .connectors.sources import BrokerBinConnector, NexarConnector
from .models import (
    ApiSource,
    MaterialCard,
    MaterialVendorHistory,
    Requirement,
    Sighting,
)
from .scoring import classify_lead, explain_lead, is_weak_lead, score_sighting, score_sighting_v2
from .utils.normalization import (
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


# ── Search result cache (Redis, 15-min TTL) ─────────────────────────────

_SEARCH_CACHE_TTL = 900  # 15 minutes
_SEARCH_CACHE_PREFIX = "search:"
_search_redis = None
_search_redis_attempted = False


def _get_search_redis():
    """Lazy-init Redis for search caching. Returns client or None."""
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
    except Exception:
        _search_redis = None
    return _search_redis


def _search_cache_key(pns: list[str], connector_names: list[str]) -> str:
    """Deterministic cache key from sorted PNs + active connectors."""
    payload = json.dumps({"pns": sorted(pns), "connectors": sorted(connector_names)}, sort_keys=True)
    return _SEARCH_CACHE_PREFIX + hashlib.md5(payload.encode()).hexdigest()


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
    except Exception:
        pass
    return None


def _set_search_cache(key: str, results: list[dict], source_stats: list[dict]) -> None:
    """Store search results in Redis with TTL."""
    r = _get_search_redis()
    if not r:
        return
    try:
        r.setex(key, _SEARCH_CACHE_TTL, json.dumps({"results": results, "source_stats": source_stats}))
    except Exception:
        pass


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
        s = str(sub).strip() if sub else ""
        if not s:
            continue
        display = normalize_mpn(s) or s
        key = normalize_mpn_key(display)
        if key and key not in seen_keys:
            pns.append(display)
            seen_keys.add(key)
    return pns


async def search_requirement(req: Requirement, db: Session) -> dict:
    """Search APIs, upsert material cards, merge history.

    Returns {"sightings": [...], "source_stats": [...]}.
    """
    pns = get_all_pns(req)
    if not pns:
        return {"sightings": [], "source_stats": []}

    now = datetime.now(timezone.utc)

    # 1. Fetch + dedupe (parallel across all connectors)
    fresh, source_stats = await _fetch_fresh(pns, db)

    # 2. Score + save — only replace sightings from connectors that succeeded
    succeeded_sources = {stat["source"] for stat in source_stats if stat["status"] == "ok" and not stat.get("error")}
    sightings = _save_sightings(fresh, req, db, succeeded_sources)
    logger.info(f"Req {req.id} ({pns[0]}): {len(sightings)} fresh sightings")

    # 3. Material card upsert (errors won't break search)
    card_ids = set()
    for pn in pns:
        try:
            card = _upsert_material_card(pn, sightings, db, now)
            if card:
                card_ids.add(card.id)
        except Exception as e:
            logger.error("MATERIAL_CARD_UPSERT_FAIL: mpn=%s error=%s", pn, e)
            db.rollback()

    # 3b. Fire background enrichment for cards without manufacturer
    _schedule_background_enrichment(card_ids, db)

    # 4. Historical vendors from material cards
    fresh_vendors = {s.vendor_name.lower() for s in sightings}
    history = _get_material_history(list(card_ids), fresh_vendors, db)

    # 5. Combine + sort
    results = []
    for s in sightings:
        d = sighting_to_dict(s)
        d["is_historical"] = False
        d["is_material_history"] = False
        results.append(d)

    for h in history:
        results.append(_history_to_result(h, now))

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
        median_price = sorted(fresh_prices)[len(fresh_prices) // 2]
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
        if not is_weak_lead(
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

    results.sort(key=lambda x: x.get("score", 0), reverse=True)
    return {"sightings": results, "source_stats": source_stats}


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
    median_price = sorted(prices)[len(prices) // 2] if prices else None
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
    results.sort(key=lambda x: x.get("score", 0), reverse=True)

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
        group.sort(key=lambda x: x.get("score", 0), reverse=True)
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


# ── Private helpers ──────────────────────────────────────────────────────


async def _fetch_fresh(pns: list[str], db: Session) -> tuple[list[dict], list[dict]]:
    """Returns (results, source_stats) where source_stats is a list of
    {"source": name, "results": count, "ms": elapsed, "error": str|None, "status": "ok"|"error"|"skipped"|"disabled"}.
    """
    # Check which sources are disabled by the user
    disabled_sources = set()
    for src in db.query(ApiSource).filter_by(status="disabled").all():
        disabled_sources.add(src.name)

    connectors = []
    source_stats_map: dict[str, dict] = {}  # track per-connector status

    # Tier 1: Direct APIs (skip disabled). DB credentials first, env var fallback.
    from .services.credential_service import get_credential

    def _cred(source_name, var_name):
        return get_credential(db, source_name, var_name)

    def _add_or_skip(source_name, has_creds, connector_factory):
        if source_name in disabled_sources:
            source_stats_map[source_name] = {
                "source": source_name,
                "results": 0,
                "ms": 0,
                "error": None,
                "status": "disabled",
            }
        elif not has_creds:
            source_stats_map[source_name] = {
                "source": source_name,
                "results": 0,
                "ms": 0,
                "error": "No API key configured",
                "status": "skipped",
            }
        else:
            connectors.append(connector_factory())

    nexar_id = _cred("nexar", "NEXAR_CLIENT_ID")
    nexar_sec = _cred("nexar", "NEXAR_CLIENT_SECRET")
    octopart_key = _cred("nexar", "OCTOPART_API_KEY")
    _add_or_skip(
        "nexar", nexar_id and nexar_sec or octopart_key, lambda: NexarConnector(nexar_id, nexar_sec, octopart_key)
    )

    bb_key = _cred("brokerbin", "BROKERBIN_API_KEY")
    bb_sec = _cred("brokerbin", "BROKERBIN_API_SECRET")
    _add_or_skip("brokerbin", bb_key, lambda: BrokerBinConnector(bb_key, bb_sec))

    ebay_id = _cred("ebay", "EBAY_CLIENT_ID")
    ebay_sec = _cred("ebay", "EBAY_CLIENT_SECRET")
    _add_or_skip("ebay", ebay_id and ebay_sec, lambda: EbayConnector(ebay_id, ebay_sec))

    dk_id = _cred("digikey", "DIGIKEY_CLIENT_ID")
    dk_sec = _cred("digikey", "DIGIKEY_CLIENT_SECRET")
    _add_or_skip("digikey", dk_id and dk_sec, lambda: DigiKeyConnector(dk_id, dk_sec))

    mouser_key = _cred("mouser", "MOUSER_API_KEY")
    _add_or_skip("mouser", mouser_key, lambda: MouserConnector(mouser_key))

    oem_key = _cred("oemsecrets", "OEMSECRETS_API_KEY")
    _add_or_skip("oemsecrets", oem_key, lambda: OEMSecretsConnector(oem_key))

    src_key = _cred("sourcengine", "SOURCENGINE_API_KEY")
    _add_or_skip("sourcengine", src_key, lambda: SourcengineConnector(src_key))

    e14_key = _cred("element14", "ELEMENT14_API_KEY")
    _add_or_skip("element14", e14_key, lambda: Element14Connector(e14_key))

    # AI live web search source (disabled in TESTING to keep tests deterministic)
    ai_key = _cred("anthropic_ai", "ANTHROPIC_API_KEY")
    has_ai_live = bool(ai_key) and not bool(os.environ.get("TESTING"))
    _add_or_skip("ai_live_web", has_ai_live, lambda: AIWebSearchConnector(ai_key))

    if not connectors:
        return [], list(source_stats_map.values())

    # Check search cache (keyed by PNs + active connector set)
    active_names = sorted(_CONNECTOR_SOURCE_MAP.get(c.__class__.__name__, "") for c in connectors)
    cache_key = _search_cache_key(pns, active_names)
    cached = _get_search_cache(cache_key)
    if cached is not None:
        cached_results, cached_stats = cached
        # Merge cached stats with disabled/skipped entries
        cached_stats_map = {s["source"]: s for s in cached_stats}
        source_stats_map.update(cached_stats_map)
        logger.info("Search cache HIT for %s (%d results)", pns[0] if pns else "?", len(cached_results))
        return cached_results, list(source_stats_map.values())

    # Run ALL connectors × ALL part numbers in parallel.
    # IMPORTANT: Stats are collected in a plain list (not written to DB) during
    # gather, because the SQLAlchemy session is not safe for concurrent access.
    stats_updates = []  # (source_name, hit_count, elapsed_ms, error_str|None)

    async def _run_one(conn, pn):
        """Run a single connector for a single PN. No DB access here."""
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
            logger.warning(f"Search {pn} via {conn.__class__.__name__}: {e}")
            if source_name:
                stats_updates.append((source_name, 0, elapsed_ms, str(e)[:500]))
            return []

    # Fire all connector×PN combos in parallel (with concurrency limit)
    from .config import settings

    sem = asyncio.Semaphore(settings.search_concurrency_limit)

    async def _throttled(conn, pn):
        async with sem:
            return await _run_one(conn, pn)

    tasks = [_throttled(conn, pn) for pn in pns for conn in connectors]
    results_lists = await asyncio.gather(*tasks, return_exceptions=True)

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
                src.status = "live"
                src.last_error = None
            else:
                src.last_error = error
                src.last_error_at = datetime.now(timezone.utc)
                src.error_count_24h = (src.error_count_24h or 0) + 1
        db.commit()
    except Exception:
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
    JUNK_VENDORS = {
        "",
        "unknown",
        "(no sellers listed)",
        "no sellers listed",
        "n/a",
        "none",
        "(none)",
        "-",
        "no vendor",
        "no seller",
    }
    out = [r for r in out if r.get("vendor_name", "").strip().lower() not in JUNK_VENDORS]

    # Build source_stats from stats_updates (connectors that actually ran)
    # Aggregate per source (a connector may run for multiple PNs)
    agg: dict[str, dict] = {}
    for source_name, hit_count, elapsed_ms, error in stats_updates:
        if source_name in agg:
            agg[source_name]["results"] += hit_count
            agg[source_name]["ms"] = max(agg[source_name]["ms"], elapsed_ms)
            if error and not agg[source_name]["error"]:
                agg[source_name]["error"] = error
                agg[source_name]["status"] = "error"
        else:
            agg[source_name] = {
                "source": source_name,
                "results": hit_count,
                "ms": elapsed_ms,
                "error": error,
                "status": "error" if error else "ok",
            }
    # Merge with skipped/disabled entries
    for name, entry in agg.items():
        source_stats_map[name] = entry

    # Cache results for subsequent searches of the same PNs
    connector_stats = [v for k, v in agg.items()]
    _set_search_cache(cache_key, out, connector_stats)

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
            created_at=datetime.now(timezone.utc),
        )
        norm_name = normalize_vendor_name(clean_vendor)
        s.score = score_sighting(vendor_score_map.get(norm_name), s.is_authorized)
        db.add(s)
        sightings.append(s)

    # PR 3: Compute multi-factor v2 scores with median price context
    prices = [s.unit_price for s in sightings if s.unit_price and s.unit_price > 0]
    median_price = sorted(prices)[len(prices) // 2] if prices else None
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

    # Tag propagation: propagate material card tags to vendor entities
    try:
        from .models import VendorCard
        from .services.tagging import propagate_tags_to_entity

        for s in sightings:  # pragma: no cover
            if not s.material_card_id or not s.vendor_name:
                continue
            vn_norm = normalize_vendor_name(s.vendor_name)
            if not vn_norm:
                continue
            vc = db.query(VendorCard).filter_by(normalized_name=vn_norm).first()
            if vc:
                propagate_tags_to_entity("vendor_card", vc.id, s.material_card_id, 1.0, db)
        db.commit()
    except Exception:
        logger.debug("Tag propagation failed for sightings", exc_info=True)

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

    for vendor_name, emails in email_map.items():
        norm = normalize_vendor_name(vendor_name)
        if not norm:
            continue

        card = db.query(VendorCard).filter_by(normalized_name=norm).first()
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
        logger.warning("Failed to propagate vendor emails: %s", e)
        db.rollback()


def _get_material_history(material_card_ids: list[int], fresh_vendors: set, db: Session) -> list[dict]:
    """All vendor touchpoints from material cards, excluding vendors with fresh sightings."""
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
    age_days = (now.replace(tzinfo=None) - last_seen.replace(tzinfo=None)).days if last_seen else 999

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
    except Exception:  # pragma: no cover
        pass  # Audit should never break card creation


def resolve_material_card(mpn: str, db: Session) -> MaterialCard | None:
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
        logger.debug("MC_METRIC: action=resolved mpn=%s card_id=%d", norm, card.id)
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
            )
            .on_conflict_do_nothing(index_elements=["normalized_mpn"])
        )
        result = db.execute(stmt)
        db.flush()
        # Re-fetch (unfiltered — may be soft-deleted and needs restoring)
        card = db.query(MaterialCard).filter_by(normalized_mpn=norm).first()
        if card is None:
            logger.error("MATERIAL_CARD_RESOLVE_FAIL: card missing after ON CONFLICT for mpn=%s", norm)
        elif card.deleted_at is not None:
            # Restore soft-deleted card
            card.deleted_at = None
            logger.info("MC_METRIC: action=restored mpn=%s card_id=%d", norm, card.id)
            _audit_card_created(db, card)
        elif result.rowcount == 0:
            logger.info("MC_METRIC: action=race_resolved mpn=%s card_id=%d", norm, card.id)
        else:
            logger.info("MC_METRIC: action=created mpn=%s card_id=%d", norm, card.id)
            _audit_card_created(db, card)
        return card
    else:
        # SQLite / test fallback — use try/except on IntegrityError
        from sqlalchemy.exc import IntegrityError

        try:
            card = MaterialCard(normalized_mpn=norm, display_mpn=display, search_count=0)
            db.add(card)
            db.flush()
            logger.info("MC_METRIC: action=created mpn=%s card_id=%d", norm, card.id)
            _audit_card_created(db, card)
            return card
        except IntegrityError:
            db.rollback()
            logger.info("MC_METRIC: action=race_resolved mpn=%s", norm)
            card = db.query(MaterialCard).filter_by(normalized_mpn=norm).first()
            # Restore if soft-deleted
            if card and card.deleted_at is not None:
                card.deleted_at = None
                db.flush()
                logger.info("MC_METRIC: action=restored mpn=%s card_id=%d", norm, card.id)
            return card


def _upsert_material_card(pn: str, sightings: list[Sighting], db: Session, now: datetime) -> MaterialCard | None:
    """Upsert material card + link sightings. Raises on error — caller handles rollback."""
    norm = normalize_mpn_key(pn)
    if not norm:
        return None
    pn_key = normalize_mpn_key(pn)
    pn_sightings = [s for s in sightings if normalize_mpn_key(s.mpn_matched or "") == pn_key]
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
    from .vendor_utils import normalize_vendor_name as _nvn

    existing_vh = {
        _nvn(vh.vendor_name): vh for vh in db.query(MaterialVendorHistory).filter_by(material_card_id=card.id).all()
    }

    for s in pn_sightings:
        if not s.vendor_name:
            continue
        raw = s.raw_data or {}
        vn_key = _nvn(s.vendor_name)
        vh = existing_vh.get(vn_key)

        if vh:
            vh.last_seen = now
            vh.times_seen = (vh.times_seen or 1) + 1
            if s.qty_available is not None:
                vh.last_qty = s.qty_available
            if s.unit_price is not None:
                vh.last_price = s.unit_price
            if s.currency:
                vh.last_currency = s.currency
            if s.manufacturer:
                vh.last_manufacturer = s.manufacturer
            if s.is_authorized:
                vh.is_authorized = True
            if raw.get("vendor_sku"):
                vh.vendor_sku = raw["vendor_sku"]
        else:
            vn_norm = _nvn(s.vendor_name) or s.vendor_name
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
            if result.get("commodity"):  # pragma: no cover
                commodity_tag = get_or_create_commodity_tag(result["commodity"]["name"], db)
                if commodity_tag:
                    tags_to_apply.append(
                        {
                            "tag_id": commodity_tag.id,
                            "source": result["commodity"]["source"],
                            "confidence": result["commodity"]["confidence"],
                        }
                    )
            if tags_to_apply:  # pragma: no cover
                tag_material_card(card.id, tags_to_apply, db)
                db.commit()
    except Exception:  # pragma: no cover
        logger.debug("Tag classification failed for card %s", card.id, exc_info=True)

    return card


def _schedule_background_enrichment(card_ids: set[int], db: Session) -> None:
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
                    logger.debug("Background enrichment failed for %s", mpn, exc_info=True)
                    session.rollback()
        finally:
            session.close()

    try:
        asyncio.create_task(_enrich_cards())
    except RuntimeError:
        pass  # No event loop — skip silently (e.g., in tests)


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
