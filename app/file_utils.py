"""Shared file parsing utilities for CSV/Excel imports.

Used by:
  - main.py: upload_requirements, import_stock_list
  - scheduler.py: _parse_stock_list_file
"""

import csv
import io
import logging

log = logging.getLogger(__name__)


def parse_tabular_file(content: bytes, filename: str) -> list[dict]:
    """Parse CSV/TSV/Excel file bytes into a list of row dicts.

    All header keys are stripped and lowercased.
    All values are stripped strings.
    Returns empty list on parse failure (logs warning).
    """
    fname = (filename or "").lower()
    rows = []

    try:
        if fname.endswith((".xlsx", ".xls")):
            rows = _parse_excel(content)
        else:
            delimiter = "\t" if fname.endswith(".tsv") else ","
            rows = _parse_csv(content, delimiter)
    except Exception as e:
        log.warning(f"File parse error ({filename}): {e}")

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


def normalize_stock_row(r: dict) -> dict | None:
    """Extract mpn/qty/price/manufacturer from a row with varying headers.

    Returns {"mpn": str, "qty": int|None, "price": float|None, "manufacturer": str|None}
    or None if no valid MPN found.
    """
    norm = {k.strip().lower(): v for k, v in r.items() if k}

    mpn = None
    for h in MPN_HEADERS:
        if h in norm and norm[h]:
            mpn = str(norm[h]).strip()
            break

    if not mpn or len(mpn) < 3:
        return None

    qty = None
    for h in QTY_HEADERS:
        if h in norm and norm[h]:
            try:
                qty = int(float(str(norm[h]).replace(",", "").strip()))
            except (ValueError, TypeError):
                pass
            break

    price = None
    for h in PRICE_HEADERS:
        if h in norm and norm[h]:
            try:
                price = float(str(norm[h]).replace("$", "").replace(",", "").strip())
            except (ValueError, TypeError):
                pass
            break

    mfr = None
    for h in MFR_HEADERS:
        if h in norm and norm[h]:
            mfr = str(norm[h]).strip()
            break

    return {"mpn": mpn, "qty": qty, "price": price, "manufacturer": mfr}


def parse_num(val) -> float | None:
    """Parse a string like '$1,234.56' into a float."""
    if not val:
        return None
    val = str(val).replace(",", "").replace("$", "").strip()
    try:
        return float(val)
    except (ValueError, TypeError):
        return None
