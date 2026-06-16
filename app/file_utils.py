"""Shared file parsing utilities for CSV/Excel imports.

Used by:
  - main.py: upload_requirements, import_stock_list
  - scheduler.py: _parse_stock_list_file
"""

import csv
import io

from loguru import logger


def _looks_like_html(content: bytes) -> bool:
    """Return True when the byte payload looks like an HTML document."""
    head = content[:512].lstrip().lower()
    return head.startswith((b"<head", b"<html", b"<table", b"<!doctype", b"<meta"))


def _parse_html_table(content: bytes) -> list[dict]:
    """Parse an HTML <table> export (e.g. ERP 'Excel' that is really HTML)."""
    from html.parser import HTMLParser

    class _T(HTMLParser):
        def __init__(self):
            super().__init__()
            self.rows: list[list[str]] = []
            self._cur: list[str] | None = None
            self._cell: list[str] | None = None

        def handle_starttag(self, tag, attrs):
            if tag == "tr":
                self._cur = []
            elif tag in ("td", "th"):
                self._cell = []

        def handle_endtag(self, tag):
            if tag == "tr" and self._cur is not None:
                self.rows.append(self._cur)
                self._cur = None
            elif tag in ("td", "th") and self._cell is not None and self._cur is not None:
                self._cur.append("".join(self._cell).strip())
                self._cell = None

        def handle_data(self, data):
            if self._cell is not None:
                self._cell.append(data)

    try:
        text = content.decode("iso-8859-1")
    except Exception:
        text = content.decode("utf-8", errors="replace")
    p = _T()
    p.feed(text)
    table = [r for r in p.rows if any(c.strip() for c in r)]
    if not table:
        return []
    headers = [str(c or "").strip().lower() for c in table[0]]
    out = []
    for row in table[1:]:
        if not any(c.strip() for c in row):
            continue
        out.append(dict(zip(headers, [str(v or "").strip() for v in row])))
    return out


def parse_tabular_file(content: bytes, filename: str) -> list[dict]:
    """Parse CSV/TSV/Excel file bytes into a list of row dicts.

    All header keys are stripped and lowercased. All values are stripped strings.
    Returns empty list on parse failure (logs warning).
    """
    fname = (filename or "").lower()
    rows = []

    try:
        if fname.endswith((".xlsx", ".xls")):
            if _looks_like_html(content):
                rows = _parse_html_table(content)
            else:
                rows = _parse_excel(content)
        elif _looks_like_html(content):
            rows = _parse_html_table(content)
        else:
            delimiter = "\t" if fname.endswith(".tsv") else ","
            rows = _parse_csv(content, delimiter)
    except Exception as e:
        logger.warning(f"File parse error ({filename}): {e}")

    return rows


def _parse_excel(content: bytes) -> list[dict]:
    """Parse Excel bytes into list of row dicts."""
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    ws = wb.active
    rows = []
    headers = []
    for i, row in enumerate(ws.iter_rows(values_only=True)):
        if i == 0:
            headers = [str(c or "").strip().lower() for c in row]
            continue
        if not headers or not any(row):
            continue
        rows.append(dict(zip(headers, [str(v or "").strip() for v in row])))
    wb.close()
    return rows


def _parse_csv(content: bytes, delimiter: str = ",") -> list[dict]:
    """Parse CSV/TSV bytes into list of row dicts."""
    text = content.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    rows = []
    for row in reader:
        rows.append({k.strip().lower(): v.strip() for k, v in row.items() if k})
    return rows


_MPN_COLUMN_NAMES = (
    "material: material name",
    "material name",
    "mpn",
    "part number",
    "part_number",
    "partnumber",
    "pn",
    "part#",
)


def extract_mpns_with_rows(rows: list[dict]) -> list[tuple[int, str]]:
    """Pull ``(file_row, part_number)`` pairs from parsed rows.

    ``file_row`` is the 1-based row in the SOURCE file (header = row 1, first data
    row = 2) so import warnings point at the spreadsheet line the user can actually
    open and fix. Blank MPN cells are skipped WITH their row number (numbering does
    not compress); only lines the parser dropped entirely (fully blank rows) are
    uncounted, so numbering can drift by those on malformed files.

    Prefers a recognized column name; otherwise uses the single column present.
    Preserves order, drops blanks.
    """
    if not rows:
        return []
    keys = list(rows[0].keys())
    col = next((k for k in keys if k in _MPN_COLUMN_NAMES), None)
    if col is None and len(keys) == 1:
        col = keys[0]
    if col is None:
        return []
    out = []
    for file_row, r in enumerate(rows, start=2):  # the header occupies file row 1
        v = (r.get(col) or "").strip()
        if v:
            out.append((file_row, v))
    return out


def extract_mpns(rows: list[dict]) -> list[str]:
    """Pull part numbers from parsed rows (order preserved, blanks dropped)."""
    return [mpn for _file_row, mpn in extract_mpns_with_rows(rows)]


# ── Stock list row normalization ────────────────────────────────────────

# Common header variations for stock list columns
MPN_HEADERS = {
    "mpn",
    "part number",
    "part_number",
    "pn",
    "partnumber",
    "part#",
    "part no",
    "part_no",
    "mfr part",
    "mfr_part",
    "manufacturer part",
    "component",
    "item",
    "item number",
    "sku",
    "model",
}
QTY_HEADERS = {
    "qty",
    "quantity",
    "avail",
    "available",
    "stock",
    "on hand",
    "on_hand",
    "inventory",
    "qty available",
    "qty_available",
}
PRICE_HEADERS = {
    "price",
    "unit price",
    "unit_price",
    "cost",
    "each",
    "unit cost",
    "unit_cost",
    "sell price",
    "sell_price",
    "usd",
}
MFR_HEADERS = {"manufacturer", "mfr", "mfg", "brand", "make", "vendor", "oem"}
CONDITION_HEADERS = {"condition", "cond", "quality", "grade"}
PACKAGING_HEADERS = {"packaging", "package", "pkg", "pack", "packing"}
DATE_CODE_HEADERS = {"date_code", "date code", "datecode", "dc", "date codes", "date_codes"}
LEAD_TIME_HEADERS = {"lead_time", "lead time", "leadtime", "lt", "delivery", "availability"}
CURRENCY_HEADERS = {"currency", "curr", "ccy"}


def _first_header_value(norm: dict, headers: set) -> str | None:
    """Return the first non-empty value in *norm* whose key is in *headers*."""
    for h in headers:
        if h in norm and norm[h]:
            return norm[h]
    return None


def _first_header_str(norm: dict, headers: set) -> str | None:
    """Like ``_first_header_value`` but stripped to a string (None if no match)."""
    raw = _first_header_value(norm, headers)
    return str(raw).strip() if raw is not None else None


def normalize_stock_row(r: dict) -> dict | None:
    """Extract mpn/qty/price/manufacturer/condition/packaging/date_code/lead_time from a
    row.

    Returns dict with normalized fields or None if no valid MPN found. Uses
    normalization functions for robust parsing.
    """
    from .utils.normalization import detect_currency, normalize_price, normalize_quantity

    norm = {k.strip().lower(): v for k, v in r.items() if k}

    mpn = _first_header_str(norm, MPN_HEADERS)
    if not mpn or len(mpn) < 3:
        return None

    price_raw = _first_header_value(norm, PRICE_HEADERS)
    currency = _first_header_value(norm, CURRENCY_HEADERS)

    return {
        "mpn": mpn,
        "qty": normalize_quantity(_first_header_value(norm, QTY_HEADERS)),
        "price": normalize_price(price_raw),
        "manufacturer": _first_header_str(norm, MFR_HEADERS),
        "condition": _first_header_str(norm, CONDITION_HEADERS),
        "packaging": _first_header_str(norm, PACKAGING_HEADERS),
        "date_code": _first_header_str(norm, DATE_CODE_HEADERS),
        "lead_time": _first_header_str(norm, LEAD_TIME_HEADERS),
        "currency": detect_currency(currency or price_raw),
    }
