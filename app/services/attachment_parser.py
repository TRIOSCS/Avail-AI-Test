"""Attachment Parser — AI-powered column detection for vendor stock lists.

Email Mining v2 Upgrade 2. Parses Excel/CSV attachments from vendor emails,
using Claude to detect column mappings when headers are ambiguous. Results
are cached by (vendor_domain, file_fingerprint) to avoid repeat AI calls.

Target fields extracted per row:
  mpn, manufacturer, qty, unit_price, currency, condition, date_code,
  lead_time, packaging, description
"""

import io
import logging
import re
from datetime import datetime, timezone

log = logging.getLogger(__name__)

# Standard column header patterns (deterministic, no AI needed)
HEADER_PATTERNS = {
    "mpn": re.compile(
        r"(?i)^(part\s*(?:no|number|#|num)|mpn|mfr?\s*part|mfg\s*p/?n|p/?n|item\s*(?:no|#))$"
    ),
    "manufacturer": re.compile(r"(?i)^(manufacturer|mfr|mfg|brand|make|vendor)$"),
    "qty": re.compile(
        r"(?i)^(qty|quantity|qoh|avail(?:able)?|stock|on\s*hand|inv(?:entory)?)$"
    ),
    "unit_price": re.compile(
        r"(?i)^(price|unit\s*price|cost|unit\s*cost|rate|ea|each|\$/ea|usd)$"
    ),
    "condition": re.compile(r"(?i)^(cond(?:ition)?|grade|quality|status)$"),
    "date_code": re.compile(r"(?i)^(date\s*code|dc|d/?c|lot|batch)$"),
    "lead_time": re.compile(r"(?i)^(lead\s*time|lt|delivery|tat|ard|eta|ship)$"),
    "packaging": re.compile(r"(?i)^(pack(?:aging|age)?|pkg|spq|form)$"),
    "description": re.compile(r"(?i)^(desc(?:ription)?|detail|spec|product)$"),
    "currency": re.compile(r"(?i)^(curr(?:ency)?|ccy)$"),
    "moq": re.compile(r"(?i)^(moq|min(?:imum)?\s*(?:order|qty)|min)$"),
}


def _match_headers_deterministic(headers: list[str]) -> dict[int, str]:
    """Try to match column headers to target fields using regex patterns.

    Returns: {col_index: field_name} for matched columns.
    """
    mapping = {}
    used_fields = set()

    for idx, raw in enumerate(headers):
        h = raw.strip()
        if not h:
            continue
        for field, pattern in HEADER_PATTERNS.items():
            if field in used_fields:
                continue
            if pattern.match(h):
                mapping[idx] = field
                used_fields.add(field)
                break

    return mapping


async def _ai_detect_columns(
    headers: list[str],
    sample_rows: list[list[str]],
    vendor_domain: str,
) -> dict[int, str]:
    """Use Claude to detect column mappings when deterministic matching fails.

    Returns: {col_index: field_name}
    """
    from app.utils.claude_client import claude_structured

    COLUMN_SCHEMA = {
        "name": "column_mapping",
        "description": "Map spreadsheet column indices to standard field names",
        "input_schema": {
            "type": "object",
            "properties": {
                "mappings": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "column_index": {
                                "type": "integer",
                                "description": "0-based column index",
                            },
                            "field_name": {
                                "type": "string",
                                "enum": [
                                    "mpn",
                                    "manufacturer",
                                    "qty",
                                    "unit_price",
                                    "currency",
                                    "condition",
                                    "date_code",
                                    "lead_time",
                                    "packaging",
                                    "description",
                                    "moq",
                                    "ignore",
                                ],
                            },
                            "confidence": {"type": "number", "description": "0.0-1.0"},
                        },
                        "required": ["column_index", "field_name", "confidence"],
                    },
                },
            },
            "required": ["mappings"],
        },
    }

    prompt = f"""Analyze this spreadsheet from vendor domain "{vendor_domain}".
Map each column to the correct electronic component field.

Headers: {headers}

Sample rows (first 5):
{chr(10).join(str(row) for row in sample_rows[:5])}

Rules:
- Map "mpn" to the column containing electronic part numbers (alphanumeric codes like LM7805, SN74HC595N)
- Map "qty" to available quantity/stock
- Map "unit_price" to price per unit
- Use "ignore" for columns that don't map to any standard field
- Only map if confidence > 0.5"""

    try:
        result = await claude_structured(
            prompt=prompt,
            tool_schema=COLUMN_SCHEMA,
            model_tier="fast",
        )
        if not result or "mappings" not in result:
            return {}

        mapping = {}
        for m in result["mappings"]:
            if m.get("field_name") != "ignore" and m.get("confidence", 0) > 0.5:
                mapping[m["column_index"]] = m["field_name"]
        return mapping

    except Exception as e:
        log.warning(f"AI column detection failed: {e}")
        return {}


async def _get_or_detect_mapping(
    headers: list[str],
    sample_rows: list[list[str]],
    vendor_domain: str,
    file_fingerprint: str,
    db=None,
) -> dict[int, str]:
    """Get column mapping — check cache, try deterministic, fall back to AI.

    Caching by (vendor_domain, file_fingerprint) so repeat files from
    the same vendor don't re-invoke AI.
    """
    # Step 1: Check cache
    if db and vendor_domain and file_fingerprint:
        from app.models import ColumnMappingCache

        cached = (
            db.query(ColumnMappingCache)
            .filter_by(
                vendor_domain=vendor_domain,
                file_fingerprint=file_fingerprint,
            )
            .first()
        )
        if cached and cached.mapping:
            log.info(
                f"Cache hit for column mapping: {vendor_domain}/{file_fingerprint[:8]}"
            )
            # Convert string keys back to int
            return {int(k): v for k, v in cached.mapping.items()}

    # Step 2: Deterministic header matching
    mapping = _match_headers_deterministic(headers)

    # Need at least MPN to be useful
    has_mpn = "mpn" in mapping.values()

    # Step 3: AI fallback if deterministic didn't find MPN
    if not has_mpn and headers:
        log.info(f"Deterministic matching insufficient for {vendor_domain}, trying AI")
        ai_mapping = await _ai_detect_columns(headers, sample_rows, vendor_domain)
        if ai_mapping:
            # Merge: AI fills gaps, deterministic takes priority
            for idx, field in ai_mapping.items():
                if idx not in mapping:
                    mapping[idx] = field

    # Step 4: Cache the result
    if db and vendor_domain and file_fingerprint and mapping:
        from app.models import ColumnMappingCache

        cache_entry = ColumnMappingCache(
            vendor_domain=vendor_domain,
            file_fingerprint=file_fingerprint,
            mapping={str(k): v for k, v in mapping.items()},  # JSON needs string keys
            confidence=0.9 if has_mpn else 0.7,
            created_at=datetime.now(timezone.utc),
        )
        try:
            from sqlalchemy.dialects.postgresql import insert

            stmt = (
                insert(ColumnMappingCache.__table__)
                .values(
                    vendor_domain=cache_entry.vendor_domain,
                    file_fingerprint=cache_entry.file_fingerprint,
                    mapping=cache_entry.mapping,
                    confidence=cache_entry.confidence,
                    created_at=cache_entry.created_at,
                )
                .on_conflict_do_update(
                    index_elements=["vendor_domain", "file_fingerprint"],
                    set_={
                        "mapping": cache_entry.mapping,
                        "confidence": cache_entry.confidence,
                    },
                )
            )
            db.execute(stmt)
            db.flush()
        except Exception as e:
            log.debug(f"Column mapping cache write failed: {e}")

    return mapping


def _parse_excel(file_bytes: bytes) -> tuple[list[str], list[list[str]]]:
    """Parse Excel file, return (headers, rows)."""
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
    ws = wb.active
    if not ws:
        return [], []

    rows = []
    for row in ws.iter_rows(values_only=True):
        rows.append([str(cell) if cell is not None else "" for cell in row])

    wb.close()

    if not rows:
        return [], []

    # First non-empty row is headers
    headers = rows[0]
    data_rows = rows[1:]

    return headers, data_rows


def _parse_csv(file_bytes: bytes, filename: str) -> tuple[list[str], list[list[str]]]:
    """Parse CSV/TSV file with encoding detection, return (headers, rows)."""
    import csv
    from app.utils.file_validation import detect_encoding

    encoding = detect_encoding(file_bytes)
    text = file_bytes.decode(encoding, errors="replace")

    # Auto-detect delimiter
    delimiter = "\t" if filename.lower().endswith(".tsv") else ","
    if delimiter == "," and text.count("\t") > text.count(","):
        delimiter = "\t"

    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows = []
    for row in reader:
        rows.append(row)
        if len(rows) > 10000:  # Safety cap
            break

    if not rows:
        return [], []

    headers = rows[0]
    data_rows = rows[1:]

    return headers, data_rows


def _extract_row(row: list[str], mapping: dict[int, str]) -> dict | None:
    """Extract a single row using the column mapping. Returns dict or None if no MPN."""
    from app.utils.normalization import (
        normalize_mpn,
        normalize_price,
        normalize_quantity,
        normalize_condition,
        normalize_date_code,
        normalize_lead_time,
        normalize_packaging,
        detect_currency,
        normalize_moq,
    )

    result = {}
    for col_idx, field in mapping.items():
        if col_idx < len(row):
            result[field] = row[col_idx].strip()

    # MPN is required
    raw_mpn = result.get("mpn", "").strip()
    if not raw_mpn or len(raw_mpn) < 3:
        return None

    # Normalize fields
    normalized = {
        "mpn": normalize_mpn(raw_mpn),
        "manufacturer": result.get("manufacturer", "").strip(),
        "description": result.get("description", "").strip(),
    }

    # Numeric fields with normalization
    if result.get("qty"):
        normalized["qty"] = normalize_quantity(result["qty"])
    if result.get("unit_price"):
        normalized["unit_price"] = normalize_price(result["unit_price"])
    if result.get("condition"):
        normalized["condition"] = normalize_condition(result["condition"])
    if result.get("date_code"):
        normalized["date_code"] = normalize_date_code(result["date_code"])
    if result.get("lead_time"):
        lt_days = normalize_lead_time(result["lead_time"])
        normalized["lead_time_days"] = lt_days
        normalized["lead_time"] = result["lead_time"].strip()
    if result.get("packaging"):
        normalized["packaging"] = normalize_packaging(result["packaging"])
    if result.get("currency"):
        normalized["currency"] = detect_currency(result["currency"])
    elif result.get("unit_price"):
        normalized["currency"] = detect_currency(result["unit_price"])
    if result.get("moq"):
        normalized["moq"] = normalize_moq(result["moq"])

    return normalized


async def parse_attachment(
    file_bytes: bytes,
    filename: str,
    vendor_domain: str = "",
    db=None,
) -> list[dict]:
    """Parse a vendor stock list attachment into structured rows.

    Uses file validation (H3), encoding detection (H4), deterministic
    header matching, and AI column detection (Upgrade 2) with caching.

    Returns: List of dicts with normalized electronic component fields.
    """
    from app.utils.file_validation import validate_file, file_fingerprint

    # H3: Validate file type
    is_valid, detected_type = validate_file(file_bytes, filename)
    if not is_valid:
        log.warning(f"File validation failed for {filename}: {detected_type}")
        return []

    fp = file_fingerprint(file_bytes)

    # Parse based on file type
    lower = filename.lower()
    if lower.endswith((".xlsx", ".xls")):
        headers, data_rows = _parse_excel(file_bytes)
    elif lower.endswith((".csv", ".tsv")):
        headers, data_rows = _parse_csv(file_bytes, filename)
    else:
        log.warning(f"Unsupported file type: {filename}")
        return []

    if not headers or not data_rows:
        return []

    # Get column mapping (cache → deterministic → AI)
    sample = data_rows[:5]
    mapping = await _get_or_detect_mapping(headers, sample, vendor_domain, fp, db)

    if not mapping or "mpn" not in mapping.values():
        log.warning(f"No MPN column detected in {filename}")
        return []

    # Extract rows
    results = []
    for row in data_rows:
        extracted = _extract_row(row, mapping)
        if extracted:
            results.append(extracted)

    log.info(
        f"Parsed {len(results)} rows from {filename} ({len(data_rows)} total, {len(mapping)} mapped columns)"
    )
    return results
