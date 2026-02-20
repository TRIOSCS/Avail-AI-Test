"""Search service — runs requirements through all configured sources.

- Keeps ALL historical sightings (never deletes)
- Upserts vendor/part combos onto MaterialCards after each search
- Merges MaterialCard vendor history into results
- Vendor card enrichment (ratings, blacklist) happens in main.py
"""

import asyncio
import logging
import time
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from .connectors.digikey import DigiKeyConnector
from .connectors.ebay import EbayConnector
from .connectors.element14 import Element14Connector
from .connectors.mouser import MouserConnector
from .connectors.oemsecrets import OEMSecretsConnector
from .connectors.sourcengine import SourcengineConnector
from .connectors.sources import BrokerBinConnector, NexarConnector
from .connectors.tme import TMEConnector
from .models import (
    ApiSource,
    MaterialCard,
    MaterialVendorHistory,
    Requirement,
    Sighting,
)
from .scoring import score_sighting
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
    "TMEConnector": "tme",
}

log = logging.getLogger(__name__)


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

    # Tier 1: Direct APIs (skip disabled). DB credentials first, env var fallback.
    from .services.credential_service import get_credential

    def _cred(source_name, var_name):
        return get_credential(db, source_name, var_name)

    nexar_id = _cred("nexar", "NEXAR_CLIENT_ID")
    nexar_sec = _cred("nexar", "NEXAR_CLIENT_SECRET")
    if "nexar" not in disabled_sources and nexar_id and nexar_sec:
        connectors.append(NexarConnector(nexar_id, nexar_sec))

    bb_key = _cred("brokerbin", "BROKERBIN_API_KEY")
    bb_sec = _cred("brokerbin", "BROKERBIN_API_SECRET")
    if "brokerbin" not in disabled_sources and bb_key:
        connectors.append(BrokerBinConnector(bb_key, bb_sec))

    ebay_id = _cred("ebay", "EBAY_CLIENT_ID")
    ebay_sec = _cred("ebay", "EBAY_CLIENT_SECRET")
    if "ebay" not in disabled_sources and ebay_id and ebay_sec:
        connectors.append(EbayConnector(ebay_id, ebay_sec))

    dk_id = _cred("digikey", "DIGIKEY_CLIENT_ID")
    dk_sec = _cred("digikey", "DIGIKEY_CLIENT_SECRET")
    if "digikey" not in disabled_sources and dk_id and dk_sec:
        connectors.append(DigiKeyConnector(dk_id, dk_sec))

    mouser_key = _cred("mouser", "MOUSER_API_KEY")
    if "mouser" not in disabled_sources and mouser_key:
        connectors.append(MouserConnector(mouser_key))

    oem_key = _cred("oemsecrets", "OEMSECRETS_API_KEY")
    if "oemsecrets" not in disabled_sources and oem_key:
        connectors.append(OEMSecretsConnector(oem_key))

    src_key = _cred("sourcengine", "SOURCENGINE_API_KEY")
    if "sourcengine" not in disabled_sources and src_key:
        connectors.append(SourcengineConnector(src_key))

    e14_key = _cred("element14", "ELEMENT14_API_KEY")
    if "element14" not in disabled_sources and e14_key:
        connectors.append(Element14Connector(e14_key))

    tme_token = _cred("tme", "TME_API_TOKEN")
    tme_secret = _cred("tme", "TME_API_SECRET")
    if "tme" not in disabled_sources and tme_token and tme_secret:
        connectors.append(TMEConnector(tme_token, tme_secret))

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
        src_map = (
            {
                s.name: s
                for s in db.query(ApiSource)
                .filter(ApiSource.name.in_(source_names))
                .all()
            }
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
            (r.get("vendor_sku") or "").lower(),
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
    out = [
        r for r in out if r.get("vendor_name", "").strip().lower() not in JUNK_VENDORS
    ]

    return out


def _save_sightings(fresh: list[dict], req: Requirement, db: Session) -> list[Sighting]:
    from .services.admin_service import get_scoring_weights

    weights = get_scoring_weights(db)

    # Delete previous sightings for this requirement to prevent duplicates on re-search
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

        raw_currency = r.get("currency") or r.get("unit_price")
        clean_currency = detect_currency(raw_currency) if raw_currency else "USD"

        clean_condition = normalize_condition(r.get("condition"))
        clean_packaging = normalize_packaging(r.get("packaging"))
        clean_date_code = normalize_date_code(r.get("date_code"))
        clean_lead_time_days = normalize_lead_time(r.get("lead_time"))

        s = Sighting(
            requirement_id=req.id,
            vendor_name=clean_vendor,
            vendor_email=r.get("vendor_email"),
            vendor_phone=r.get("vendor_phone"),
            mpn_matched=clean_mpn,
            manufacturer=r.get("manufacturer"),
            qty_available=clean_qty,
            unit_price=clean_price,
            currency=clean_currency,
            moq=r.get("moq"),
            source_type=r.get("source_type"),
            is_authorized=r.get("is_authorized", False),
            confidence=r.get("confidence", 0),
            condition=clean_condition,
            packaging=clean_packaging,
            date_code=clean_date_code,
            lead_time_days=clean_lead_time_days,
            lead_time=r.get("lead_time"),
            raw_data=r,
            created_at=datetime.now(timezone.utc),
        )
        s.score = score_sighting(s, req.target_qty or 1, weights)
        db.add(s)
        sightings.append(s)
    db.commit()

    # Propagate vendor emails from search results to VendorContact records
    _propagate_vendor_emails(sightings, db)

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
            existing = (
                db.query(VendorContact)
                .filter_by(vendor_card_id=card.id, email=email)
                .first()
            )
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
        log.warning("Failed to propagate vendor emails: %s", e)
        db.rollback()


def _get_material_history(
    pns: list[str], fresh_vendors: set, db: Session
) -> list[dict]:
    """Vendors from material cards NOT in fresh results."""
    if not pns:
        return []

    # Batch fetch all material cards for these PNs (1 query instead of N)
    norm_pns = [normalize_mpn_key(pn) for pn in pns if normalize_mpn_key(pn)]
    if not norm_pns:
        return []
    cards = (
        db.query(MaterialCard).filter(MaterialCard.normalized_mpn.in_(norm_pns)).all()
    )
    if not cards:
        return []

    # Batch fetch all vendor histories for these cards (1 query instead of N lazy loads)
    card_map = {c.id: c for c in cards}
    all_vh = (
        db.query(MaterialVendorHistory)
        .filter(MaterialVendorHistory.material_card_id.in_(card_map.keys()))
        .all()
    )

    rows = []
    seen = set()
    for vh in all_vh:
        vk = vh.vendor_name.lower()
        if vk in fresh_vendors or vk in seen:
            continue
        seen.add(vk)
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
    age_days = (
        (now.replace(tzinfo=None) - last_seen.replace(tzinfo=None)).days
        if last_seen
        else 999
    )

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
        "created_at": last_seen.isoformat() if last_seen else None,
        "is_historical": False,
        "is_material_history": True,
        "material_last_seen": last_seen.strftime("%b %d") if last_seen else None,
        "material_times_seen": h["times_seen"],
        "material_first_seen": h["first_seen"].strftime("%b %d, %Y")
        if h["first_seen"]
        else None,
        "material_card_id": h["material_card_id"],
    }


def _upsert_material_card(
    pn: str, sightings: list[Sighting], db: Session, now: datetime
):
    """Upsert material card. Raises on error — caller handles rollback."""
    norm = normalize_mpn_key(pn)
    if not norm:
        return
    pn_key = normalize_mpn_key(pn)
    pn_sightings = [s for s in sightings if normalize_mpn_key(s.mpn_matched or "") == pn_key]
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
        for vh in db.query(MaterialVendorHistory)
        .filter_by(material_card_id=card.id)
        .all()
    }

    for s in pn_sightings:
        if not s.vendor_name:
            continue
        raw = s.raw_data or {}
        vh = existing_vh.get(s.vendor_name)

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
            new_vh = MaterialVendorHistory(
                material_card_id=card.id,
                vendor_name=s.vendor_name,
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
            existing_vh[s.vendor_name] = new_vh  # Prevent dupe inserts within batch
    db.commit()


def sighting_to_dict(s: Sighting) -> dict:
    raw = s.raw_data or {}
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
        "score": s.score,
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
        "created_at": s.created_at.isoformat() if s.created_at else None,
    }
