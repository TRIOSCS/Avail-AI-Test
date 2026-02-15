"""Search service — runs requirements through all configured sources.

- Keeps ALL historical sightings (never deletes)
- Upserts vendor/part combos onto MaterialCards after each search
- Merges MaterialCard vendor history into results
- Vendor card enrichment (ratings, blacklist) happens in main.py
"""
import logging, asyncio, time
from datetime import datetime, timezone
from sqlalchemy.orm import Session

from .config import settings
from .models import (
    Requirement, Sighting,
    MaterialCard, MaterialVendorHistory, ApiSource,
)
from .scoring import score_sighting
from .connectors.sources import NexarConnector, BrokerBinConnector
from .connectors.ebay import EbayConnector
from .connectors.digikey import DigiKeyConnector
from .connectors.mouser import MouserConnector
from .connectors.oemsecrets import OEMSecretsConnector
from .connectors.sourcengine import SourcengineConnector

# Map connector class names to ApiSource.name for stats tracking
_CONNECTOR_SOURCE_MAP = {
    "NexarConnector": "nexar",
    "BrokerBinConnector": "brokerbin",
    "EbayConnector": "ebay",
    "DigiKeyConnector": "digikey",
    "MouserConnector": "mouser",
    "OEMSecretsConnector": "oemsecrets",
    "SourcengineConnector": "sourcengine",
}

log = logging.getLogger(__name__)


def normalize_mpn(mpn: str) -> str:
    if not mpn:
        return ""
    return mpn.strip().lower()


def get_all_pns(req: Requirement) -> list[str]:
    """Primary MPN + substitutes, deduplicated."""
    pns = []
    if req.primary_mpn and req.primary_mpn.strip():
        pns.append(req.primary_mpn.strip())
    for sub in (req.substitutes or []):
        s = str(sub).strip() if sub else ""
        if s and s not in pns:
            pns.append(s)
    return pns


async def search_requirement(req: Requirement, db: Session) -> list[dict]:
    """Search APIs, upsert material cards, merge history."""
    pns = get_all_pns(req)
    if not pns:
        return []

    now = datetime.now(timezone.utc)

    # 1. Fetch + dedupe (parallel across all connectors)
    fresh = await _fetch_fresh(pns, db)

    # 2. Score + save
    sightings = _save_sightings(fresh, req, db)
    log.info(f"Req {req.id} ({pns[0]}): {len(sightings)} fresh sightings")

    # 3. Material card upsert (errors won't break search)
    for pn in pns:
        try:
            _upsert_material_card(pn, sightings, db, now)
        except Exception as e:
            log.error(f"Material card upsert failed for '{pn}': {e}")
            db.rollback()

    # 4. Historical vendors from material cards
    fresh_vendors = {s.vendor_name.lower() for s in sightings}
    history = _get_material_history(pns, fresh_vendors, db)

    # 5. Combine + sort
    results = []
    for s in sightings:
        d = sighting_to_dict(s)
        d["is_historical"] = False
        d["is_material_history"] = False
        results.append(d)

    for h in history:
        results.append(_history_to_result(h, now))

    results.sort(key=lambda x: x.get("score", 0), reverse=True)
    return results


# ── Private helpers ──────────────────────────────────────────────────────

async def _fetch_fresh(pns: list[str], db: Session) -> list[dict]:
    # Check which sources are disabled by the user
    disabled_sources = set()
    for src in db.query(ApiSource).filter_by(status="disabled").all():
        disabled_sources.add(src.name)

    connectors = []

    # Tier 1: Direct APIs (skip disabled)
    if "nexar" not in disabled_sources and settings.nexar_client_id and settings.nexar_client_secret:
        connectors.append(NexarConnector(settings.nexar_client_id, settings.nexar_client_secret))
    if "brokerbin" not in disabled_sources and settings.brokerbin_api_key:
        connectors.append(BrokerBinConnector(settings.brokerbin_api_key, settings.brokerbin_api_secret))
    if "ebay" not in disabled_sources and settings.ebay_client_id and settings.ebay_client_secret:
        connectors.append(EbayConnector(settings.ebay_client_id, settings.ebay_client_secret))
    if "digikey" not in disabled_sources and settings.digikey_client_id and settings.digikey_client_secret:
        connectors.append(DigiKeyConnector(settings.digikey_client_id, settings.digikey_client_secret))
    if "mouser" not in disabled_sources and settings.mouser_api_key:
        connectors.append(MouserConnector(settings.mouser_api_key))
    if "oemsecrets" not in disabled_sources and settings.oemsecrets_api_key:
        connectors.append(OEMSecretsConnector(settings.oemsecrets_api_key))
    if "sourcengine" not in disabled_sources and settings.sourcengine_api_key:
        connectors.append(SourcengineConnector(settings.sourcengine_api_key))

    if not connectors:
        return []

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
            log.warning(f"Search {pn} via {conn.__class__.__name__}: {e}")
            if source_name:
                stats_updates.append((source_name, 0, elapsed_ms, str(e)[:500]))
            return []

    # Fire all connector×PN combos in parallel
    tasks = [_run_one(conn, pn) for pn in pns for conn in connectors]
    results_lists = await asyncio.gather(*tasks, return_exceptions=True)

    # Apply stats to DB in one pass — safe, sequential, after gather completes
    try:
        source_names = {s[0] for s in stats_updates if s[0]}
        src_map = {s.name: s for s in db.query(ApiSource).filter(ApiSource.name.in_(source_names)).all()} if source_names else {}
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
        key = (r.get("vendor_name", "").lower(),
               r.get("mpn_matched", "").lower(),
               (r.get("vendor_sku") or "").lower())
        if key not in seen:
            seen.add(key)
            out.append(r)

    # Filter out junk vendors — no sellers, blanks, placeholders
    JUNK_VENDORS = {
        "", "unknown", "(no sellers listed)", "no sellers listed",
        "n/a", "none", "(none)", "-", "no vendor", "no seller",
    }
    out = [r for r in out if r.get("vendor_name", "").strip().lower() not in JUNK_VENDORS]

    return out


def _save_sightings(fresh: list[dict], req: Requirement, db: Session) -> list[Sighting]:
    weights = {
        "recency": settings.weight_recency,
        "quantity": settings.weight_quantity,
        "vendor_reliability": settings.weight_vendor_reliability,
        "data_completeness": settings.weight_data_completeness,
        "source_credibility": settings.weight_source_credibility,
        "price": settings.weight_price,
    }

    # Delete previous sightings for this requirement to prevent duplicates on re-search
    db.query(Sighting).filter_by(requirement_id=req.id).delete()
    db.flush()

    sightings = []
    for r in fresh:
        s = Sighting(
            requirement_id=req.id,
            vendor_name=r.get("vendor_name", "Unknown"),
            vendor_email=r.get("vendor_email"),
            vendor_phone=r.get("vendor_phone"),
            mpn_matched=r.get("mpn_matched"),
            manufacturer=r.get("manufacturer"),
            qty_available=r.get("qty_available"),
            unit_price=r.get("unit_price"),
            currency=r.get("currency", "USD"),
            moq=r.get("moq"),
            source_type=r.get("source_type"),
            is_authorized=r.get("is_authorized", False),
            confidence=r.get("confidence", 0),
            raw_data=r,
            created_at=datetime.now(timezone.utc),
        )
        s.score = score_sighting(s, req.target_qty or 1, weights)
        db.add(s)
        sightings.append(s)
    db.commit()
    return sightings


def _get_material_history(pns: list[str], fresh_vendors: set, db: Session) -> list[dict]:
    """Vendors from material cards NOT in fresh results."""
    if not pns:
        return []

    # Batch fetch all material cards for these PNs (1 query instead of N)
    norm_pns = [normalize_mpn(pn) for pn in pns if normalize_mpn(pn)]
    if not norm_pns:
        return []
    cards = db.query(MaterialCard).filter(MaterialCard.normalized_mpn.in_(norm_pns)).all()
    if not cards:
        return []

    # Batch fetch all vendor histories for these cards (1 query instead of N lazy loads)
    card_map = {c.id: c for c in cards}
    all_vh = db.query(MaterialVendorHistory).filter(
        MaterialVendorHistory.material_card_id.in_(card_map.keys())
    ).all()

    rows = []
    seen = set()
    for vh in all_vh:
        vk = vh.vendor_name.lower()
        if vk in fresh_vendors or vk in seen:
            continue
        seen.add(vk)
        card = card_map[vh.material_card_id]
        rows.append({
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
        })
    return rows


def _history_to_result(h: dict, now: datetime) -> dict:
    last_seen = h["last_seen"]
    age_days = (now.replace(tzinfo=None) - last_seen.replace(tzinfo=None)).days if last_seen else 999

    if age_days < 7:     base = 55
    elif age_days < 30:  base = 45
    elif age_days < 90:  base = 35
    else:                base = 30
    bonus = min(15, (h["times_seen"] - 1) * 3)
    score = max(10, base + bonus - (age_days * 0.1))

    return {
        "id": None, "requirement_id": None,
        "vendor_name": h["vendor_name"],
        "vendor_email": None, "vendor_phone": None,
        "mpn_matched": h["mpn_matched"],
        "manufacturer": h["manufacturer"],
        "qty_available": h["qty_available"],
        "unit_price": h["unit_price"],
        "currency": h["currency"],
        "source_type": h["source_type"],
        "is_authorized": h["is_authorized"],
        "confidence": 0, "score": round(score, 1),
        "octopart_url": None, "click_url": None, "vendor_url": None,
        "vendor_sku": h["vendor_sku"],
        "created_at": last_seen.isoformat() if last_seen else None,
        "is_historical": False,
        "is_material_history": True,
        "material_last_seen": last_seen.strftime("%b %d") if last_seen else None,
        "material_times_seen": h["times_seen"],
        "material_first_seen": h["first_seen"].strftime("%b %d, %Y") if h["first_seen"] else None,
        "material_card_id": h["material_card_id"],
    }


def _upsert_material_card(pn: str, sightings: list[Sighting], db: Session, now: datetime):
    """Upsert material card. Raises on error — caller handles rollback."""
    norm = normalize_mpn(pn)
    if not norm:
        return
    pn_sightings = [s for s in sightings if (s.mpn_matched or "").lower() == pn.lower()]
    if not pn_sightings:
        return

    card = db.query(MaterialCard).filter_by(normalized_mpn=norm).first()
    if not card:
        card = MaterialCard(normalized_mpn=norm, display_mpn=pn, search_count=0)
        db.add(card)
        db.flush()

    card.search_count = (card.search_count or 0) + 1
    card.last_searched_at = now
    if not card.manufacturer:
        for s in pn_sightings:
            if s.manufacturer:
                card.manufacturer = s.manufacturer
                break

    # Batch fetch all existing vendor histories for this card (avoids N+1)
    existing_vh = {
        vh.vendor_name: vh
        for vh in db.query(MaterialVendorHistory).filter_by(material_card_id=card.id).all()
    }

    for s in pn_sightings:
        if not s.vendor_name:
            continue
        raw = s.raw_data or {}
        vh = existing_vh.get(s.vendor_name)

        if vh:
            vh.last_seen = now
            vh.times_seen = (vh.times_seen or 1) + 1
            if s.qty_available is not None: vh.last_qty = s.qty_available
            if s.unit_price is not None:    vh.last_price = s.unit_price
            if s.currency:                  vh.last_currency = s.currency
            if s.manufacturer:              vh.last_manufacturer = s.manufacturer
            if s.is_authorized:             vh.is_authorized = True
            if raw.get("vendor_sku"):       vh.vendor_sku = raw["vendor_sku"]
        else:
            new_vh = MaterialVendorHistory(
                material_card_id=card.id, vendor_name=s.vendor_name,
                source_type=s.source_type, is_authorized=s.is_authorized or False,
                first_seen=now, last_seen=now, times_seen=1,
                last_qty=s.qty_available, last_price=s.unit_price,
                last_currency=s.currency or "USD", last_manufacturer=s.manufacturer,
                vendor_sku=raw.get("vendor_sku"),
            )
            db.add(new_vh)
            existing_vh[s.vendor_name] = new_vh  # Prevent dupe inserts within batch
    db.commit()


def sighting_to_dict(s: Sighting) -> dict:
    raw = s.raw_data or {}
    return {
        "id": s.id, "requirement_id": s.requirement_id,
        "vendor_name": s.vendor_name,
        "vendor_email": s.vendor_email, "vendor_phone": s.vendor_phone,
        "mpn_matched": s.mpn_matched, "manufacturer": s.manufacturer,
        "qty_available": s.qty_available, "unit_price": s.unit_price,
        "currency": s.currency, "source_type": s.source_type,
        "is_authorized": s.is_authorized, "confidence": s.confidence,
        "score": s.score,
        "is_unavailable": getattr(s, "is_unavailable", False) or False,
        "octopart_url": raw.get("octopart_url"),
        "click_url": raw.get("click_url"),
        "vendor_url": raw.get("vendor_url"),
        "vendor_sku": raw.get("vendor_sku"),
        "condition": raw.get("condition"),
        "country": raw.get("country"),
        "created_at": s.created_at.isoformat() if s.created_at else None,
    }
