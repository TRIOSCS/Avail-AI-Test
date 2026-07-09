"""Vendor stock-list ingest service.

Parses an uploaded vendor stock-list file (CSV/TSV/XLSX) and upserts the rows as
``MaterialCard`` + ``MaterialVendorHistory`` (with price snapshots), creating/looking up
the owning ``VendorCard`` by normalized name. This is the single ingest path shared by
the standalone JSON endpoint (``POST /api/materials/import-stock``) and the Vendors-page
HTMX upload modal (``POST /v2/partials/vendors/import-stock``).

Called by: routers/materials.py (JSON), routers/htmx_views.py (HTMX modal).
Depends on: file_utils (parser), models (MaterialCard/MaterialVendorHistory/VendorCard),
            price_snapshot_service, search_service.run_deterministic_passes.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..cache.decorators import invalidate_prefix
from ..models import MaterialCard, MaterialVendorHistory, VendorCard
from ..services.price_snapshot_service import record_price_snapshot
from ..utils.normalization import normalize_mpn, normalize_mpn_key
from ..vendor_utils import normalize_vendor_name

ALLOWED_EXTENSIONS = {".csv", ".xlsx", ".xls", ".tsv"}
MAX_FILE_BYTES = 10_000_000


class StockListValidationError(Exception):
    """Raised for caller-recoverable validation failures (bad type, missing vendor,
    oversized file).

    Routers translate ``.status_code`` into the right HTTP response.
    """

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


@dataclass
class StockListResult:
    """Outcome of a stock-list ingest.

    ``enrich_vendor`` is set when a brand-new vendor
    with a domain should be enriched — the async router triggers that background task so
    this service stays sync and unit-testable.
    """

    imported_rows: int = 0
    skipped_rows: int = 0
    total_rows: int = 0
    vendor_name: str = ""
    vendor_card_id: int | None = None
    new_vendor: bool = False
    enrich_vendor: bool = False
    warnings: list[dict] = field(default_factory=list)


def validate_metadata(filename: str, vendor_name: str) -> str:
    """Validate the file extension and vendor name (no body needed). Returns the
    sanitized vendor name. Callers run this BEFORE reading the upload body so a bad type
    / missing vendor is rejected without buffering a (possibly large) file.

    Raises ``StockListValidationError`` (with an HTTP ``status_code``) on any failure so
    both the JSON and HTMX callers can surface a consistent message.
    """
    import os

    ext = os.path.splitext(filename or "")[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise StockListValidationError(f"Invalid file type '{ext}'. Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}")

    # Strip HTML before the length check so a tag-padded name can't slip past.
    clean = re.sub(r"<[^>]+>", "", (vendor_name or "")).strip()
    if not clean:
        raise StockListValidationError("Vendor name is required")
    if len(clean) > 255:
        raise StockListValidationError("Vendor name must be 255 characters or fewer")

    return clean


def validate_upload(filename: str, vendor_name: str, content: bytes) -> str:
    """Full validation (metadata + body size).

    Returns the sanitized vendor name.
    """
    clean = validate_metadata(filename, vendor_name)
    if len(content) > MAX_FILE_BYTES:
        raise StockListValidationError("File too large -- 10MB maximum", status_code=413)
    return clean


def ingest_stock_list(
    db: Session,
    *,
    filename: str,
    content: bytes,
    vendor_name: str,
    vendor_website: str = "",
) -> StockListResult:
    """Parse a vendor stock-list file and upsert MaterialCard + MaterialVendorHistory.

    Reuses the deterministic tabular parser (``parse_tabular_file`` + ``normalize_stock_row``)
    — no new parser. The owning ``VendorCard`` is resolved/created by normalized name.
    Validation is performed first; callers may also call ``validate_upload`` directly to
    fail fast before reading the body.
    """
    clean_vendor = validate_upload(filename, vendor_name, content)

    from ..file_utils import normalize_stock_row, parse_tabular_file
    from ..search_service import run_deterministic_passes

    rows = parse_tabular_file(content, filename or "upload.csv")

    norm_vendor = normalize_vendor_name(clean_vendor)
    vendor_card = db.query(VendorCard).filter_by(normalized_name=norm_vendor).first()
    new_vendor = False
    if not vendor_card:
        domain = ""
        website = (vendor_website or "").strip()
        if website:
            domain = website.replace("https://", "").replace("http://", "").replace("www.", "").split("/")[0].lower()
        vendor_card = VendorCard(
            normalized_name=norm_vendor,
            display_name=clean_vendor,
            domain=domain or None,
            emails=[],
            phones=[],
        )
        db.add(vendor_card)
        try:
            db.flush()
            new_vendor = True
        except IntegrityError:
            db.rollback()
            vendor_card = db.query(VendorCard).filter_by(normalized_name=norm_vendor).first()

    result = StockListResult(total_rows=len(rows), vendor_name=clean_vendor, new_vendor=new_vendor)
    card_ids: list[int] = []

    # Row numbers are 1-based SOURCE-file rows (header occupies row 1) so a warning's
    # `row` points at the spreadsheet line the user can actually open and fix.
    for row_no, raw_row in enumerate(rows, start=2):
        parsed = normalize_stock_row(raw_row)
        if not parsed:
            result.skipped_rows += 1
            result.warnings.append({"row": row_no, "field": "mpn", "reason": "no part number recognized in row"})
            continue

        norm = normalize_mpn_key(parsed["mpn"])
        if not norm or not normalize_mpn(parsed["mpn"]):
            # normalize_mpn (not the dedup key) owns the >=3-chars rule.
            result.skipped_rows += 1
            result.warnings.append(
                {"row": row_no, "field": "mpn", "reason": f"invalid MPN {parsed['mpn']!r} (min 3 chars)"}
            )
            continue

        card = db.query(MaterialCard).filter_by(normalized_mpn=norm).first()
        if not card:
            card = MaterialCard(
                normalized_mpn=norm,
                display_mpn=parsed["mpn"].strip(),
                manufacturer=parsed.get("manufacturer") or "",
            )
            db.add(card)
            try:
                db.flush()
            except IntegrityError:
                db.rollback()
                card = db.query(MaterialCard).filter_by(normalized_mpn=norm).first()
                if not card:
                    result.skipped_rows += 1
                    result.warnings.append(
                        {"row": row_no, "field": "mpn", "reason": f"could not create card for {norm!r}"}
                    )
                    continue
        card_ids.append(card.id)

        mvh = db.query(MaterialVendorHistory).filter_by(material_card_id=card.id, vendor_name=norm_vendor).first()
        if mvh:
            mvh.last_seen = datetime.now(timezone.utc)
            mvh.times_seen = (mvh.times_seen or 0) + 1
            if parsed.get("qty") is not None:
                mvh.last_qty = parsed["qty"]
            if parsed.get("price") is not None:
                mvh.last_price = parsed["price"]
                record_price_snapshot(
                    db=db,
                    material_card_id=card.id,
                    vendor_name=norm_vendor,
                    price=parsed.get("price"),
                    source="stock_list",
                )
            if parsed.get("manufacturer"):
                mvh.last_manufacturer = parsed["manufacturer"]
            mvh.source_type = "stock_list"
        else:
            mvh = MaterialVendorHistory(
                material_card_id=card.id,
                vendor_name=norm_vendor,
                vendor_name_normalized=norm_vendor,
                source_type="stock_list",
                source="stock_list",
                last_qty=parsed.get("qty"),
                last_price=parsed.get("price"),
                last_manufacturer=parsed.get("manufacturer") or "",
            )
            db.add(mvh)
            record_price_snapshot(
                db=db, material_card_id=card.id, vendor_name=norm_vendor, price=parsed.get("price"), source="stock_list"
            )

        result.imported_rows += 1

    # Inline deterministic passes over every touched card — same session, committed
    # together. NO enrich_requested_at stamp: stock imports ride the created_at fast lane
    # (a large vendor list must not monopolize the worker's priority lane).
    run_deterministic_passes(db, card_ids)

    vendor_card.sighting_count = (vendor_card.sighting_count or 0) + result.imported_rows
    db.commit()
    invalidate_prefix("material_list")

    result.vendor_card_id = vendor_card.id
    # Signal the (async) caller to fire vendor enrichment for a brand-new vendor with a
    # domain; the credential check + background task stay in the router.
    result.enrich_vendor = bool(new_vendor and vendor_card.domain and not vendor_card.last_enriched_at)

    logger.info(
        "Stock-list ingest: vendor={!r} imported={} skipped={} total={}",
        clean_vendor,
        result.imported_rows,
        result.skipped_rows,
        result.total_rows,
    )
    return result


async def maybe_trigger_vendor_enrichment(db: Session, result: StockListResult) -> bool:
    """Fire background vendor enrichment when the ingest flagged a brand-new vendor with
    a domain and an enrichment credential is configured.

    Used by the Vendors-page HTMX upload route. (The JSON ``import-stock`` route keeps its
    own equivalent trigger so its long-standing monkeypatch contract stays stable.)
    """
    if not result.enrich_vendor or result.vendor_card_id is None:
        return False

    from ..services.credential_service import get_credential_cached
    from ..utils.async_helpers import safe_background_task
    from ..utils.vendor_helpers import _background_enrich_vendor

    if not (
        get_credential_cached("explorium_enrichment", "EXPLORIUM_API_KEY")
        or get_credential_cached("anthropic_ai", "ANTHROPIC_API_KEY")
    ):
        return False

    vc = db.get(VendorCard, result.vendor_card_id)
    if not vc or not vc.domain:
        return False

    await safe_background_task(
        _background_enrich_vendor(vc.id, vc.domain, vc.display_name),
        task_name="enrich_vendor_bg",
    )
    return True
