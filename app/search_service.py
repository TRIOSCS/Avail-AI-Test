"""
Search & Upload Service

- Search: fans out to Octopart + BrokerBin in parallel, merges with database,
  scores everything, deduplicates, returns ranked results.
- Upload: parses CSV/Excel vendor stock lists, auto-detects columns,
  creates sightings.
"""
import asyncio
import re
import io
import time
import structlog
import pandas as pd
from datetime import datetime, timezone, timedelta
from typing import Optional
from uuid import UUID
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import func

from app.models import Sighting, Vendor, VendorAlias, OutreachLog, Upload, SearchLog
from app.scoring import normalize_part_number, normalize_vendor_name, score_sighting
from app.connectors.sources import OctopartConnector, BrokerBinConnector
from app.config import get_settings

logger = structlog.get_logger()


# ═══════════════════════════════════════════════════════════════════════════════
# SEARCH
# ═══════════════════════════════════════════════════════════════════════════════

async def search_parts(
    db: AsyncSession,
    part_numbers: list[str],
    user_id: Optional[UUID] = None,
    target_qty: Optional[int] = None,
    include_historical: bool = True,
) -> dict:
    """Main search: query all sources → store → score → rank → return."""
    start = time.time()
    settings = get_settings()
    normalized = [normalize_part_number(pn) for pn in part_numbers]
    sources_used = []

    # --- Step 1: Fan out to API connectors in parallel ---
    connectors = []
    if settings.octopart_api_key:
        connectors.append(OctopartConnector(settings.octopart_api_key))
        sources_used.append("octopart")
    if settings.brokerbin_api_key:
        connectors.append(BrokerBinConnector(settings.brokerbin_api_key, settings.brokerbin_api_secret))
        sources_used.append("brokerbin")

    api_results = []
    if connectors:
        tasks = []
        for conn in connectors:
            for pn in part_numbers:
                tasks.append(conn.search(pn))
        all_results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in all_results:
            if isinstance(r, list):
                api_results.extend(r)

    # --- Step 2: Store new sightings from API results ---
    for r in api_results:
        vendor = await _get_or_create_vendor(db, r)
        sighting = Sighting(
            vendor_id=vendor.id,
            part_number=r["part_number"],
            part_number_normalized=normalize_part_number(r["part_number"]),
            manufacturer=r.get("manufacturer"),
            quantity=r.get("quantity"),
            price=r.get("price"),
            condition=r.get("condition"),
            lead_time_days=r.get("lead_time_days"),
            lead_time_text=r.get("lead_time_text"),
            source_type=r["source_type"],
            source_url=r.get("source_url"),
            confidence=r.get("confidence", 3),
            evidence_type=r.get("evidence_type", "active_listing"),
            seen_at=datetime.now(timezone.utc),
        )
        db.add(sighting)
    await db.commit()
    sources_used.append("database")

    # --- Step 3: Load sightings from database ---
    query = select(Sighting).where(Sighting.part_number_normalized.in_(normalized))
    if not include_historical:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        query = query.where(Sighting.seen_at >= cutoff)
    result = await db.execute(query.order_by(Sighting.seen_at.desc()))
    sightings = list(result.scalars().all())

    # --- Step 4: Load vendors + outreach exclusions ---
    vendor_ids = list({s.vendor_id for s in sightings})
    vendors_by_id = {}
    if vendor_ids:
        vr = await db.execute(select(Vendor).where(Vendor.id.in_(vendor_ids)))
        for v in vr.scalars():
            vendors_by_id[v.id] = v

    cooldown = datetime.now(timezone.utc) - timedelta(days=settings.outreach_cooldown_days)
    excluded_keys = set()
    if vendor_ids and normalized:
        er = await db.execute(
            select(OutreachLog.vendor_id, OutreachLog.part_number_normalized).where(
                and_(
                    OutreachLog.vendor_id.in_(vendor_ids),
                    OutreachLog.part_number_normalized.in_(normalized),
                    OutreachLog.sent_at >= cooldown,
                )
            )
        )
        excluded_keys = {(row[0], row[1]) for row in er.fetchall()}

    # --- Step 5: Collect prices for relative scoring ---
    all_prices = [float(s.price) for s in sightings if s.price]

    # --- Step 6: Score, deduplicate, rank ---
    seen_keys = {}
    for s in sightings:
        v = vendors_by_id.get(s.vendor_id)
        if not v:
            continue

        bd = score_sighting(
            seen_at=s.seen_at, quantity=s.quantity,
            price=float(s.price) if s.price else None,
            lead_time_days=s.lead_time_days, condition=s.condition,
            source_type=s.source_type, source_url=s.source_url,
            total_outreach=v.total_outreach, total_responses=v.total_responses,
            total_wins=v.total_wins, tier=v.tier, is_authorized=v.is_authorized,
            red_flags=v.red_flags, has_email=bool(v.email), is_blocked=v.is_blocked,
            all_prices=all_prices, target_qty=target_qty,
        )

        key = (v.id, s.part_number_normalized)
        is_excluded = any((v.id, pn) in excluded_keys for pn in normalized)

        entry = {
            "vendor_id": str(v.id),
            "vendor_name": v.name,
            "vendor_type": v.vendor_type,
            "vendor_tier": v.tier,
            "vendor_is_authorized": v.is_authorized,
            "part_number": s.part_number,
            "manufacturer": s.manufacturer,
            "quantity": s.quantity,
            "price": float(s.price) if s.price else None,
            "condition": s.condition,
            "lead_time_days": s.lead_time_days,
            "source": s.source_type,
            "sources_found_on": [s.source_type],
            "source_url": s.source_url,
            "confidence": s.confidence,
            "seen_at": s.seen_at.isoformat() if s.seen_at else None,
            "score": round(bd.final_score, 1),
            "score_breakdown": bd.to_dict(),
            "excluded": is_excluded,
            "exclusion_reason": "Recently contacted" if is_excluded else None,
        }

        if key in seen_keys:
            existing = seen_keys[key]
            existing["sources_found_on"] = list(set(existing["sources_found_on"] + [s.source_type]))
            if entry["score"] > existing["score"]:
                entry["sources_found_on"] = existing["sources_found_on"]
                seen_keys[key] = entry
        else:
            seen_keys[key] = entry

    # Sort: non-excluded first, then by score descending
    results = sorted(seen_keys.values(), key=lambda x: (not x["excluded"], x["score"]), reverse=True)

    # Log the search
    duration = int((time.time() - start) * 1000)
    db.add(SearchLog(
        user_id=user_id,
        part_numbers=part_numbers,
        result_count=len(results),
        sources_queried=sources_used,
        duration_ms=duration,
    ))
    await db.commit()

    return {
        "query": part_numbers,
        "target_qty": target_qty,
        "result_count": len(results),
        "results": results,
        "searched_at": datetime.now(timezone.utc).isoformat(),
    }


async def _get_or_create_vendor(db: AsyncSession, result: dict) -> Vendor:
    """Find existing vendor or create a new one."""
    name_norm = normalize_vendor_name(result["vendor_name"])
    if not name_norm:
        name_norm = "unknown"

    # Check main table
    r = await db.execute(select(Vendor).where(Vendor.name_normalized == name_norm))
    vendor = r.scalar_one_or_none()
    if vendor:
        return vendor

    # Check aliases
    r = await db.execute(
        select(VendorAlias).where(VendorAlias.alias_normalized == name_norm)
    )
    alias = r.scalar_one_or_none()
    if alias:
        r = await db.execute(select(Vendor).where(Vendor.id == alias.vendor_id))
        return r.scalar_one()

    # Create new vendor
    vendor = Vendor(
        name=result["vendor_name"],
        name_normalized=name_norm,
        vendor_type=result.get("vendor_type", "broker"),
        is_authorized=result.get("is_authorized", False),
    )
    db.add(vendor)
    await db.flush()
    return vendor


# ═══════════════════════════════════════════════════════════════════════════════
# UPLOAD
# ═══════════════════════════════════════════════════════════════════════════════

# Column name aliases — the system auto-detects which column is which
COLUMN_ALIASES = {
    "part": ["part", "pn", "part_number", "mpn", "mfr_part", "partnumber", "part #",
             "component", "item", "sku", "part no", "mfg part", "mfg pn", "mfr pn",
             "p/n", "partno", "part_no"],
    "quantity": ["qty", "quantity", "stock", "avail", "available", "inventory",
                 "on_hand", "oh", "qoh", "amt", "amount"],
    "price": ["price", "unit_price", "cost", "unit price", "unit cost", "ea",
              "each", "usd", "sell", "sell price", "selling price"],
    "vendor": ["vendor", "supplier", "company", "seller", "source", "distributor",
               "broker", "sold by", "mfr rep"],
    "manufacturer": ["mfr", "manufacturer", "mfg", "brand", "make", "vendor mfr"],
    "lead_time": ["lead", "lead_time", "leadtime", "lead time", "delivery",
                  "tat", "eta", "ship", "days"],
    "condition": ["cond", "condition", "quality", "grade", "type", "status"],
    "date_code": ["dc", "date_code", "datecode", "date code", "lot", "batch"],
}


def _detect_columns(columns: list[str]) -> dict[str, str]:
    """Auto-detect which spreadsheet columns map to which fields."""
    mapping = {}
    cols_lower = {c: c.lower().strip() for c in columns}

    for field, aliases in COLUMN_ALIASES.items():
        for col, col_lower in cols_lower.items():
            if col_lower in aliases and field not in mapping:
                mapping[field] = col
                break

    return mapping


def _parse_number(val) -> Optional[int]:
    """Parse quantity strings: '5K' → 5000, '1.5M' → 1500000"""
    if pd.isna(val) or val is None:
        return None
    s = str(val).strip().upper().replace(",", "")
    if not s:
        return None
    try:
        if s.endswith("K"):
            return int(float(s[:-1]) * 1_000)
        if s.endswith("M"):
            return int(float(s[:-1]) * 1_000_000)
        return int(float(s))
    except (ValueError, TypeError):
        return None


def _parse_price(val) -> Optional[float]:
    """Parse price strings: '$1,234.56' → 1234.56"""
    if pd.isna(val) or val is None:
        return None
    s = str(val).strip()
    s = re.sub(r"[^\d.\-]", "", s)
    try:
        return float(s) if s else None
    except (ValueError, TypeError):
        return None


async def process_upload(
    db: AsyncSession,
    file_bytes: bytes,
    filename: str,
    user_id: Optional[UUID] = None,
) -> dict:
    """Parse a CSV/Excel file and create sightings from it."""
    upload = Upload(
        user_id=user_id,
        filename=filename,
        file_size_bytes=len(file_bytes),
        status="processing",
    )
    db.add(upload)
    await db.flush()

    try:
        # Read file into DataFrame
        if filename.lower().endswith((".xlsx", ".xls")):
            df = pd.read_excel(io.BytesIO(file_bytes))
        else:
            for enc in ["utf-8", "latin-1", "cp1252"]:
                try:
                    df = pd.read_csv(io.BytesIO(file_bytes), encoding=enc)
                    break
                except UnicodeDecodeError:
                    continue
            else:
                raise ValueError("Could not decode file — try saving as UTF-8")

        if df.empty:
            raise ValueError("File is empty")

        # Detect columns
        mapping = _detect_columns(list(df.columns))
        if "part" not in mapping:
            raise ValueError(f"Could not find a part number column. Found columns: {list(df.columns)}")

        upload.column_mapping = mapping
        upload.row_count = len(df)

        # Process each row
        sighting_count = 0
        error_count = 0

        for _, row in df.iterrows():
            try:
                pn = str(row[mapping["part"]]).strip()
                if not pn or pn.lower() in ("nan", "none", ""):
                    continue

                vendor_name = str(row[mapping["vendor"]]).strip() if "vendor" in mapping else "Upload"
                vendor = await _get_or_create_vendor(db, {
                    "vendor_name": vendor_name or "Upload",
                    "vendor_type": "broker",
                    "is_authorized": False,
                })

                sighting = Sighting(
                    vendor_id=vendor.id,
                    part_number=pn,
                    part_number_normalized=normalize_part_number(pn),
                    manufacturer=str(row[mapping["manufacturer"]]).strip() if "manufacturer" in mapping and not pd.isna(row.get(mapping.get("manufacturer"))) else None,
                    quantity=_parse_number(row.get(mapping.get("quantity"))) if "quantity" in mapping else None,
                    price=_parse_price(row.get(mapping.get("price"))) if "price" in mapping else None,
                    condition=str(row[mapping["condition"]]).strip() if "condition" in mapping and not pd.isna(row.get(mapping.get("condition"))) else None,
                    date_code=str(row[mapping["date_code"]]).strip() if "date_code" in mapping and not pd.isna(row.get(mapping.get("date_code"))) else None,
                    source_type="upload",
                    confidence=3,
                    evidence_type="active_listing",
                    upload_id=upload.id,
                )
                db.add(sighting)
                sighting_count += 1
            except Exception:
                error_count += 1

        upload.sighting_count = sighting_count
        upload.error_count = error_count
        upload.status = "complete"
        await db.commit()

        return {
            "id": str(upload.id),
            "filename": filename,
            "status": "complete",
            "row_count": upload.row_count,
            "sighting_count": sighting_count,
            "error_count": error_count,
            "column_mapping": mapping,
        }

    except Exception as e:
        upload.status = "failed"
        upload.error_message = str(e)
        await db.commit()
        return {
            "id": str(upload.id),
            "filename": filename,
            "status": "failed",
            "error_message": str(e),
            "row_count": 0,
            "sighting_count": 0,
            "error_count": 0,
        }
