# Quote Builder Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a full-screen two-panel Quote Builder modal that lets users select requirements, evaluate offers line-by-line, set sell prices, and export to Excel or TRIO PDF.

**Architecture:** Full-screen modal with Alpine.js client state (no per-line server calls). Data loads upfront via `joinedload`. Save creates Quote via builder's own endpoint that handles both initial creation and revision. Excel export via openpyxl.

**Tech Stack:** FastAPI, HTMX, Alpine.js, Tailwind CSS, openpyxl, Jinja2, WeasyPrint (existing PDF)

**Spec:** `docs/superpowers/specs/2026-03-22-quote-builder-design.md`

---

## File Structure

### New Files
| File | Responsibility |
|---|---|
| `app/schemas/quote_builder.py` | Pydantic schemas: `QuoteBuilderLine`, `QuoteBuilderSaveRequest` |
| `app/services/quote_builder_service.py` | `get_builder_data()`, `apply_smart_defaults()`, `build_excel_export()`, `save_quote_from_builder()` |
| `app/routers/quote_builder.py` | 4 endpoints: modal open, save, Excel export, PDF export |
| `app/templates/htmx/partials/quote_builder/modal.html` | Two-panel modal content |
| `app/templates/htmx/partials/shared/quote_builder_shell.html` | Full-screen modal overlay shell |
| `tests/test_quote_builder.py` | All tests |

### Modified Files
| File | Change |
|---|---|
| `app/main.py:623` | Add router import + `app.include_router` |
| `app/templates/htmx/base.html:178` | Add `{% include %}` for builder shell |
| `app/templates/htmx/partials/requisitions/tabs/parts.html:159` | Add checkboxes + "Build Quote" button |
| `app/static/htmx_app.js` | Add `Alpine.data('quoteBuilder', ...)` |
| `app/static/styles.css` | Add quote builder animations (~20 lines) |

---

### Task 1: Pydantic Schemas

**Files:**
- Create: `app/schemas/quote_builder.py`
- Test: `tests/test_quote_builder.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_quote_builder.py
"""tests/test_quote_builder.py — Quote Builder service, schemas, and endpoint tests.

Called by: pytest
Depends on: app.schemas.quote_builder, app.services.quote_builder_service, conftest.py
"""

from app.schemas.quote_builder import QuoteBuilderLine, QuoteBuilderSaveRequest


def test_builder_line_schema_valid():
    line = QuoteBuilderLine(
        requirement_id=1,
        offer_id=42,
        mpn="LM358DR",
        manufacturer="TI",
        qty=500,
        cost_price=0.24,
        sell_price=0.31,
        margin_pct=22.6,
    )
    assert line.mpn == "LM358DR"
    assert line.cost_price == 0.24


def test_builder_line_schema_optional_fields():
    line = QuoteBuilderLine(
        requirement_id=1,
        mpn="LM358DR",
        manufacturer="TI",
        qty=500,
        cost_price=0.24,
        sell_price=0.31,
        margin_pct=22.6,
    )
    assert line.offer_id is None
    assert line.lead_time is None
    assert line.notes is None


def test_builder_save_request_valid():
    req = QuoteBuilderSaveRequest(
        lines=[
            QuoteBuilderLine(
                requirement_id=1, mpn="LM358DR", manufacturer="TI",
                qty=500, cost_price=0.24, sell_price=0.31, margin_pct=22.6,
            )
        ],
        payment_terms="Net 30",
        shipping_terms="FCA",
        validity_days=7,
    )
    assert len(req.lines) == 1
    assert req.payment_terms == "Net 30"


def test_builder_save_request_empty_lines_rejected():
    import pytest as _pt
    with _pt.raises(Exception):
        QuoteBuilderSaveRequest(
            lines=[],
            payment_terms="Net 30",
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_quote_builder.py -v -x`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.schemas.quote_builder'`

- [ ] **Step 3: Write minimal implementation**

```python
# app/schemas/quote_builder.py
"""schemas/quote_builder.py — Pydantic schemas for Quote Builder endpoints.

Validates the save payload from the Alpine.js quote builder modal.
Field names match what create_quote handler internally uses (cost_price,
sell_price, margin_pct) — NOT the legacy QuoteLineItem schema.

Called by: app.routers.quote_builder
Depends on: pydantic
"""

from pydantic import BaseModel, Field


class QuoteBuilderLine(BaseModel):
    """Single line item in a builder save payload."""

    requirement_id: int
    offer_id: int | None = None
    mpn: str
    manufacturer: str
    qty: int
    cost_price: float
    sell_price: float
    margin_pct: float
    lead_time: str | None = None
    date_code: str | None = None
    condition: str | None = None
    packaging: str | None = None
    moq: int | None = None
    material_card_id: int | None = None
    notes: str | None = None


class QuoteBuilderSaveRequest(BaseModel):
    """Full save payload from the quote builder modal."""

    lines: list[QuoteBuilderLine] = Field(..., min_length=1)
    payment_terms: str | None = None
    shipping_terms: str | None = None
    validity_days: int = 7
    notes: str | None = None
    quote_id: int | None = None  # Set when re-saving (triggers revision)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_quote_builder.py -v -x`
Expected: 4 PASSED

- [ ] **Step 5: Commit**

```bash
cd /root/availai && git add app/schemas/quote_builder.py tests/test_quote_builder.py && git commit -m "feat(quote-builder): add Pydantic schemas for builder save payload"
```

---

### Task 2: Service Layer — get_builder_data and apply_smart_defaults

**Files:**
- Create: `app/services/quote_builder_service.py`
- Modify: `tests/test_quote_builder.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_quote_builder.py`:

```python
from unittest.mock import MagicMock
from app.services.quote_builder_service import apply_smart_defaults


def _mock_requirement(req_id, mpn, offers_count, target_qty=100, target_price=1.0):
    """Build a mock requirement dict as returned by get_builder_data."""
    offers = []
    for i in range(offers_count):
        offers.append({
            "id": req_id * 100 + i,
            "vendor_name": f"Vendor{i}",
            "unit_price": 0.50 + i * 0.10,
            "qty_available": 500,
            "lead_time": "2 weeks",
            "date_code": "2024+",
            "condition": "new",
            "packaging": None,
            "moq": 100,
            "confidence": 0.95 if i == 0 else None,
            "notes": None,
        })
    return {
        "requirement_id": req_id,
        "mpn": mpn,
        "manufacturer": "TI",
        "target_qty": target_qty,
        "target_price": target_price,
        "customer_pn": None,
        "date_codes": None,
        "condition": None,
        "packaging": None,
        "firmware": None,
        "hardware_codes": None,
        "sale_notes": None,
        "need_by_date": None,
        "offers": offers,
        "offer_count": offers_count,
        "status": "unknown",
        "selected_offer_id": None,
        "sell_price": None,
        "sell_price_manual": False,
        "buyer_notes": "",
        "pricing_history": None,
    }


def test_smart_defaults_single_offer_auto_decided():
    lines = [_mock_requirement(1, "LM358DR", 1)]
    apply_smart_defaults(lines)
    assert lines[0]["status"] == "decided"
    assert lines[0]["selected_offer_id"] == 100  # first offer id


def test_smart_defaults_multiple_offers_needs_review():
    lines = [_mock_requirement(2, "LM317T", 3)]
    apply_smart_defaults(lines)
    assert lines[0]["status"] == "needs_review"
    assert lines[0]["selected_offer_id"] is None


def test_smart_defaults_no_offers():
    lines = [_mock_requirement(3, "NE555P", 0)]
    apply_smart_defaults(lines)
    assert lines[0]["status"] == "no_offers"
    assert lines[0]["selected_offer_id"] is None


def test_smart_defaults_auto_pick_sets_sell_price():
    lines = [_mock_requirement(4, "SN74HC00N", 1)]
    apply_smart_defaults(lines)
    assert lines[0]["sell_price"] == lines[0]["offers"][0]["unit_price"]
    assert lines[0]["sell_price_manual"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_quote_builder.py::test_smart_defaults_single_offer_auto_decided -v -x`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.quote_builder_service'`

- [ ] **Step 3: Write minimal implementation**

```python
# app/services/quote_builder_service.py
"""services/quote_builder_service.py — Business logic for the Quote Builder.

Loads requirement + offer data for the builder modal, applies smart defaults,
generates Excel exports. Decoupled from HTTP.

Called by: app.routers.quote_builder
Depends on: app.models (Requirement, Offer, Quote), openpyxl
"""

from __future__ import annotations

from sqlalchemy.orm import Session, joinedload


def get_builder_data(
    req_id: int,
    db: Session,
    requirement_ids: list[int] | None = None,
) -> list[dict]:
    """Load requirements + offers for the quote builder modal.

    Returns a list of dicts, one per requirement, each containing the
    requirement fields, nested offers, offer_count, and empty slots for
    builder state (status, selected_offer_id, sell_price, etc.).
    """
    from app.models import Offer, Requirement

    query = (
        db.query(Requirement)
        .options(joinedload(Requirement.offers))
        .filter(Requirement.requisition_id == req_id)
    )
    if requirement_ids:
        query = query.filter(Requirement.id.in_(requirement_ids))
    query = query.order_by(Requirement.id)
    requirements = query.all()

    lines = []
    for r in requirements:
        active_offers = [o for o in r.offers if o.status == "active"]
        offers_data = []
        for o in active_offers:
            offers_data.append({
                "id": o.id,
                "vendor_name": o.vendor_name,
                "unit_price": float(o.unit_price) if o.unit_price else 0,
                "qty_available": o.qty_available or 0,
                "lead_time": o.lead_time,
                "date_code": o.date_code,
                "condition": o.condition,
                "packaging": o.packaging,
                "moq": o.moq,
                "confidence": o.parse_confidence,
                "notes": o.notes,
            })

        lines.append({
            "requirement_id": r.id,
            "mpn": r.primary_mpn,
            "manufacturer": r.manufacturer,
            "target_qty": r.target_qty or 0,
            "target_price": float(r.target_price) if r.target_price else None,
            "customer_pn": r.customer_pn,
            "date_codes": r.date_codes,
            "condition": r.condition,
            "packaging": r.packaging,
            "firmware": r.firmware,
            "hardware_codes": r.hardware_codes,
            "sale_notes": r.sale_notes,
            "need_by_date": str(r.need_by_date) if r.need_by_date else None,
            "offers": offers_data,
            "offer_count": len(offers_data),
            "status": "unknown",
            "selected_offer_id": None,
            "sell_price": None,
            "sell_price_manual": False,
            "buyer_notes": "",
            "pricing_history": None,
        })

    # Load pricing history for all MPNs in one pass
    try:
        from app.routers.crm._helpers import _preload_last_quoted_prices

        quoted_prices = _preload_last_quoted_prices(db)
        for line in lines:
            mpn_key = (line["mpn"] or "").upper().strip()
            lq = quoted_prices.get(mpn_key)
            if lq:
                line["pricing_history"] = {
                    "avg_price": lq.get("sell_price"),
                    "price_range": None,
                    "recent": [{"quote_number": lq.get("quote_number", ""),
                                "date": lq.get("date", ""),
                                "cost": lq.get("cost_price"),
                                "sell": lq.get("sell_price"),
                                "margin": lq.get("margin_pct"),
                                "result": lq.get("result")}],
                }
    except Exception:
        pass  # Pricing history is non-critical; degrade gracefully

    return lines


def apply_smart_defaults(lines: list[dict]) -> None:
    """Apply smart defaults to builder lines in-place.

    - 1 offer: auto-select, status = decided
    - Multiple offers: status = needs_review
    - 0 offers: status = no_offers
    """
    for line in lines:
        offers = line.get("offers", [])
        if len(offers) == 1:
            line["status"] = "decided"
            line["selected_offer_id"] = offers[0]["id"]
            line["sell_price"] = offers[0]["unit_price"]
            line["sell_price_manual"] = False
        elif len(offers) > 1:
            line["status"] = "needs_review"
            line["selected_offer_id"] = None
        else:
            line["status"] = "no_offers"
            line["selected_offer_id"] = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_quote_builder.py -v -x`
Expected: 8 PASSED

- [ ] **Step 5: Commit**

```bash
cd /root/availai && git add app/services/quote_builder_service.py tests/test_quote_builder.py && git commit -m "feat(quote-builder): add service layer with get_builder_data and smart defaults"
```

---

### Task 3: Service Layer — Excel Export

**Files:**
- Modify: `app/services/quote_builder_service.py`
- Modify: `tests/test_quote_builder.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_quote_builder.py`:

```python
from app.services.quote_builder_service import build_excel_export


def test_excel_export_produces_valid_xlsx():
    line_items = [
        {
            "mpn": "LM358DR",
            "manufacturer": "TI",
            "qty": 500,
            "sell_price": 0.31,
            "lead_time": "2 weeks",
            "date_code": "2024+",
            "condition": "new",
            "packaging": "Tape & Reel",
            "moq": 100,
            "vendor_name": "DigiKey",
        }
    ]
    xlsx_bytes = build_excel_export(
        line_items=line_items,
        quote_number="Q-2026-0042",
        customer_name="Acme Electronics",
    )
    assert isinstance(xlsx_bytes, bytes)
    assert len(xlsx_bytes) > 100
    # Verify it's a valid XLSX (starts with PK zip header)
    assert xlsx_bytes[:2] == b"PK"


def test_excel_export_has_correct_columns():
    import openpyxl
    from io import BytesIO

    line_items = [
        {
            "mpn": "LM358DR", "manufacturer": "TI", "qty": 500,
            "sell_price": 0.31, "lead_time": "2 weeks", "date_code": "2024+",
            "condition": "new", "packaging": "T&R", "moq": 100,
            "vendor_name": "DigiKey",
        }
    ]
    xlsx_bytes = build_excel_export(line_items, "Q-2026-0042", "Acme")
    wb = openpyxl.load_workbook(BytesIO(xlsx_bytes))
    ws = wb.active
    headers = [cell.value for cell in ws[1]]
    assert "MPN" in headers
    assert "Manufacturer" in headers
    assert "Qty" in headers
    assert "Unit Price" in headers
    assert "Extended Price" in headers
    assert "Lead Time" in headers
    assert "Vendor" in headers
    # Check data row
    assert ws.cell(row=2, column=1).value == "LM358DR"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_quote_builder.py::test_excel_export_produces_valid_xlsx -v -x`
Expected: FAIL — `ImportError: cannot import name 'build_excel_export'`

- [ ] **Step 3: Write minimal implementation**

Append to `app/services/quote_builder_service.py`:

```python
def build_excel_export(
    line_items: list[dict],
    quote_number: str,
    customer_name: str,
) -> bytes:
    """Generate a styled Excel workbook from quote line items.

    Returns raw bytes of the .xlsx file.
    """
    from io import BytesIO

    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = quote_number or "Quote"

    # Header style: brand blue background, white bold font
    header_fill = PatternFill(start_color="3D6895", end_color="3D6895", fill_type="solid")
    header_font = Font(bold=True, color="FFFFFF", size=11)

    columns = [
        ("MPN", 20),
        ("Manufacturer", 18),
        ("Qty", 10),
        ("Unit Price", 14),
        ("Extended Price", 16),
        ("Lead Time", 14),
        ("Date Codes", 14),
        ("Condition", 12),
        ("Packaging", 14),
        ("MOQ", 10),
        ("Vendor", 20),
    ]

    # Write headers
    for col_idx, (name, width) in enumerate(columns, 1):
        cell = ws.cell(row=1, column=col_idx, value=name)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center")
        ws.column_dimensions[get_column_letter(col_idx)].width = width

    # Write data rows
    for row_idx, item in enumerate(line_items, 2):
        qty = item.get("qty") or 0
        sell = item.get("sell_price") or 0
        ws.cell(row=row_idx, column=1, value=item.get("mpn", ""))
        ws.cell(row=row_idx, column=2, value=item.get("manufacturer", ""))
        ws.cell(row=row_idx, column=3, value=qty)
        price_cell = ws.cell(row=row_idx, column=4, value=sell)
        price_cell.number_format = "$#,##0.0000"
        ext_cell = ws.cell(row=row_idx, column=5, value=round(qty * sell, 2))
        ext_cell.number_format = "$#,##0.00"
        ws.cell(row=row_idx, column=6, value=item.get("lead_time", ""))
        ws.cell(row=row_idx, column=7, value=item.get("date_code", ""))
        ws.cell(row=row_idx, column=8, value=item.get("condition", ""))
        ws.cell(row=row_idx, column=9, value=item.get("packaging", ""))
        ws.cell(row=row_idx, column=10, value=item.get("moq", ""))
        ws.cell(row=row_idx, column=11, value=item.get("vendor_name", ""))

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_quote_builder.py -v -x`
Expected: 10 PASSED

- [ ] **Step 5: Commit**

```bash
cd /root/availai && git add app/services/quote_builder_service.py tests/test_quote_builder.py && git commit -m "feat(quote-builder): add Excel export service with openpyxl"
```

---

### Task 4: Service Layer — save_quote_from_builder

**Files:**
- Modify: `app/services/quote_builder_service.py`
- Modify: `tests/test_quote_builder.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_quote_builder.py`:

```python
from app.models import Quote, QuoteLine, Requisition, CustomerSite, Company, Requirement


def test_save_quote_from_builder_creates_quote(db_session, test_user):
    """Uses conftest fixtures for DB session and user."""
    from app.services.quote_builder_service import save_quote_from_builder
    from app.schemas.quote_builder import QuoteBuilderLine, QuoteBuilderSaveRequest

    # Seed a requisition with customer site
    company = Company(name="Acme Corp")
    db_session.add(company)
    db_session.flush()
    site = CustomerSite(company_id=company.id, site_name="HQ")
    db_session.add(site)
    db_session.flush()
    req = Requisition(name="Test Req", customer_site_id=site.id, created_by=test_user.id, status="active")
    db_session.add(req)
    db_session.flush()
    r1 = Requirement(requisition_id=req.id, primary_mpn="LM358DR", manufacturer="TI", target_qty=500)
    db_session.add(r1)
    db_session.commit()

    payload = QuoteBuilderSaveRequest(
        lines=[
            QuoteBuilderLine(
                requirement_id=r1.id, mpn="LM358DR", manufacturer="TI",
                qty=500, cost_price=0.24, sell_price=0.31, margin_pct=22.6,
            )
        ],
        payment_terms="Net 30",
    )
    result = save_quote_from_builder(db_session, req_id=req.id, payload=payload, user=test_user)
    assert result["ok"] is True
    assert "quote_id" in result
    assert "quote_number" in result

    # Verify Quote record exists
    quote = db_session.get(Quote, result["quote_id"])
    assert quote is not None
    assert quote.payment_terms == "Net 30"
    assert len(quote.line_items) == 1


def test_save_quote_revision(db_session, test_user):
    """Uses conftest fixtures for DB session and user."""
    from app.services.quote_builder_service import save_quote_from_builder
    from app.schemas.quote_builder import QuoteBuilderLine, QuoteBuilderSaveRequest

    # Seed
    company = Company(name="Acme Corp")
    db_session.add(company)
    db_session.flush()
    site = CustomerSite(company_id=company.id, site_name="HQ")
    db_session.add(site)
    db_session.flush()
    req = Requisition(name="Test Req", customer_site_id=site.id, created_by=test_user.id, status="active")
    db_session.add(req)
    db_session.flush()
    r1 = Requirement(requisition_id=req.id, primary_mpn="LM358DR", manufacturer="TI", target_qty=500)
    db_session.add(r1)
    db_session.commit()

    # First save
    payload1 = QuoteBuilderSaveRequest(
        lines=[
            QuoteBuilderLine(
                requirement_id=r1.id, mpn="LM358DR", manufacturer="TI",
                qty=500, cost_price=0.24, sell_price=0.31, margin_pct=22.6,
            )
        ],
    )
    result1 = save_quote_from_builder(db_session, req_id=req.id, payload=payload1, user=test_user)
    quote_id_1 = result1["quote_id"]

    # Second save (revision) — same quote_id passed
    payload2 = QuoteBuilderSaveRequest(
        lines=[
            QuoteBuilderLine(
                requirement_id=r1.id, mpn="LM358DR", manufacturer="TI",
                qty=500, cost_price=0.24, sell_price=0.40, margin_pct=40.0,
            )
        ],
        quote_id=quote_id_1,
    )
    result2 = save_quote_from_builder(db_session, req_id=req.id, payload=payload2, user=test_user)
    assert result2["ok"] is True
    assert result2["quote_id"] != quote_id_1  # New quote for revision

    # Old quote should be "revised"
    old_quote = db_session.get(Quote, quote_id_1)
    assert old_quote.status == "revised"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_quote_builder.py::test_save_quote_from_builder_creates_quote -v -x`
Expected: FAIL — `ImportError: cannot import name 'save_quote_from_builder'`

- [ ] **Step 3: Write minimal implementation**

Append to `app/services/quote_builder_service.py`:

```python
def save_quote_from_builder(
    db: Session,
    req_id: int,
    payload,
    user,
) -> dict:
    """Create or revise a Quote from the builder's save payload.

    If payload.quote_id is set, marks the old quote as 'revised' and creates
    a new revision with the updated line items. Otherwise creates a fresh quote.
    """
    from app.models import Quote, QuoteLine, Requisition
    from app.services.crm_service import next_quote_number

    req = db.get(Requisition, req_id)
    if not req:
        raise ValueError("Requisition not found")

    # Build line_items dicts (matching the format create_quote uses internally)
    line_items = []
    for li in payload.lines:
        line_items.append({
            "mpn": li.mpn,
            "manufacturer": li.manufacturer,
            "qty": li.qty,
            "cost_price": li.cost_price,
            "sell_price": li.sell_price,
            "margin_pct": li.margin_pct,
            "lead_time": li.lead_time,
            "date_code": li.date_code,
            "condition": li.condition,
            "packaging": li.packaging,
            "moq": li.moq,
            "offer_id": li.offer_id,
            "material_card_id": li.material_card_id,
            "notes": li.notes,
        })

    total_sell = sum(li["qty"] * li["sell_price"] for li in line_items)
    total_cost = sum(li["qty"] * li["cost_price"] for li in line_items)
    margin_pct = round((total_sell - total_cost) / total_sell * 100, 2) if total_sell > 0 else 0

    # Handle revision
    revision = 1
    quote_number = next_quote_number(db)
    if payload.quote_id:
        old_quote = db.get(Quote, payload.quote_id)
        if old_quote:
            old_revision = old_quote.revision or 1
            quote_number = old_quote.quote_number
            revision = old_revision + 1
            # Rename old quote to include revision suffix, then mark revised
            old_quote.quote_number = f"{quote_number}-R{old_revision}"
            old_quote.status = "revised"

    quote = Quote(
        requisition_id=req_id,
        customer_site_id=req.customer_site_id,
        quote_number=quote_number,
        revision=revision,
        line_items=line_items,
        subtotal=total_sell,
        total_cost=total_cost,
        total_margin_pct=margin_pct,
        payment_terms=payload.payment_terms,
        shipping_terms=payload.shipping_terms,
        validity_days=payload.validity_days,
        notes=payload.notes,
        created_by_id=user.id,
    )
    db.add(quote)
    db.flush()  # Get quote.id

    # Write structured QuoteLine rows
    for li in line_items:
        db.add(QuoteLine(
            quote_id=quote.id,
            material_card_id=li.get("material_card_id"),
            offer_id=li.get("offer_id"),
            mpn=li.get("mpn", ""),
            manufacturer=li.get("manufacturer"),
            qty=li.get("qty"),
            cost_price=li.get("cost_price"),
            sell_price=li.get("sell_price"),
            margin_pct=li.get("margin_pct"),
            currency="USD",
        ))

    db.commit()

    return {
        "ok": True,
        "quote_id": quote.id,
        "quote_number": quote.quote_number,
        "revision": quote.revision,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_quote_builder.py -v -x`
Expected: 12 PASSED

- [ ] **Step 5: Commit**

```bash
cd /root/availai && git add app/services/quote_builder_service.py tests/test_quote_builder.py && git commit -m "feat(quote-builder): add save_quote_from_builder with revision support"
```

---

### Task 5: Router — All 4 Endpoints

**Files:**
- Create: `app/routers/quote_builder.py`
- Modify: `app/main.py`
- Modify: `tests/test_quote_builder.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_quote_builder.py`:

```python
from fastapi.testclient import TestClient


def test_builder_modal_endpoint_404_bad_req(client):
    resp = client.get("/v2/partials/quote-builder/99999")
    assert resp.status_code == 404


def test_builder_modal_opens_successfully(client, db_session, test_user):
    """Verify the modal endpoint returns 200 with valid data."""
    company = Company(name="Test Co")
    db_session.add(company)
    db_session.flush()
    site = CustomerSite(company_id=company.id, site_name="HQ")
    db_session.add(site)
    db_session.flush()
    req = Requisition(name="Test Req", customer_site_id=site.id, created_by=test_user.id, status="active")
    db_session.add(req)
    db_session.commit()
    resp = client.get(f"/v2/partials/quote-builder/{req.id}")
    assert resp.status_code == 200
    assert "Quote Builder" in resp.text


def test_builder_save_endpoint_rejects_empty_lines(client):
    resp = client.post("/v2/partials/quote-builder/1/save", json={"lines": []})
    assert resp.status_code == 422


def test_builder_excel_export_404_bad_quote(client):
    resp = client.get("/v2/partials/quote-builder/1/export/excel?quote_id=99999")
    assert resp.status_code == 404


def test_builder_pdf_export_404_bad_quote(client):
    resp = client.get("/v2/partials/quote-builder/1/export/pdf?quote_id=99999")
    assert resp.status_code == 404
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_quote_builder.py::test_builder_modal_endpoint_404_bad_req -v -x`
Expected: FAIL — 404 (route not registered) or wrong response

- [ ] **Step 3: Write minimal implementation**

```python
# app/routers/quote_builder.py
"""routers/quote_builder.py — Quote Builder modal, save, and export endpoints.

Serves the full-screen two-panel quote builder modal, handles save (with
revision support), and streams Excel/PDF exports.

Called by: Parts tab "Build Quote" button (HTMX), Alpine.js fetch (save)
Depends on: app.services.quote_builder_service, app.schemas.quote_builder
"""

from io import BytesIO

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from loguru import logger
from sqlalchemy.orm import Session

from ..database import get_db
from ..dependencies import require_user
from ..models import User
from ..schemas.quote_builder import QuoteBuilderSaveRequest

router = APIRouter(tags=["quote-builder"])


@router.get("/v2/partials/quote-builder/{req_id}")
async def quote_builder_modal(
    req_id: int,
    request: Request,
    requirement_ids: str | None = None,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Open the quote builder modal with all requirement + offer data."""
    from ..dependencies import get_req_for_user
    from ..services.quote_builder_service import apply_smart_defaults, get_builder_data

    req = get_req_for_user(db, user, req_id)
    if not req:
        raise HTTPException(404, "Requisition not found")

    # Parse optional requirement_ids filter
    req_ids = None
    if requirement_ids:
        try:
            req_ids = [int(x.strip()) for x in requirement_ids.split(",") if x.strip()]
        except ValueError:
            req_ids = None

    lines = get_builder_data(req_id, db, requirement_ids=req_ids)
    apply_smart_defaults(lines)

    customer_name = ""
    has_customer_site = bool(req.customer_site_id)
    if has_customer_site:
        from ..models import CustomerSite

        site = db.get(CustomerSite, req.customer_site_id)
        if site and site.company:
            customer_name = site.company.name or ""

    from ..main import templates

    return templates.TemplateResponse(
        "htmx/partials/quote_builder/modal.html",
        {
            "request": request,
            "req": req,
            "lines": lines,
            "customer_name": customer_name,
            "has_customer_site": has_customer_site,
        },
    )


@router.post("/v2/partials/quote-builder/{req_id}/save")
async def quote_builder_save(
    req_id: int,
    payload: QuoteBuilderSaveRequest,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Save the quote from the builder modal."""
    from ..dependencies import get_req_for_user
    from ..services.quote_builder_service import save_quote_from_builder

    req = get_req_for_user(db, user, req_id)
    if not req:
        raise HTTPException(404, "Requisition not found")
    if not req.customer_site_id:
        raise HTTPException(400, "Requisition must be linked to a customer site before quoting")

    try:
        result = save_quote_from_builder(db, req_id=req_id, payload=payload, user=user)
    except Exception as e:
        logger.error("Quote builder save failed for req %d: %s", req_id, e)
        raise HTTPException(500, f"Save failed: {e}")

    return result


@router.get("/v2/partials/quote-builder/{req_id}/export/excel")
async def quote_builder_export_excel(
    req_id: int,
    quote_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Stream an Excel export of a saved quote."""
    from ..models import Quote

    quote = db.get(Quote, quote_id)
    if not quote or quote.requisition_id != req_id:
        raise HTTPException(404, "Quote not found")

    from ..services.quote_builder_service import build_excel_export

    customer_name = ""
    if quote.customer_site and quote.customer_site.company:
        customer_name = quote.customer_site.company.name or ""

    xlsx_bytes = build_excel_export(
        line_items=quote.line_items or [],
        quote_number=quote.quote_number,
        customer_name=customer_name,
    )

    filename = f"{quote.quote_number}.xlsx"
    return StreamingResponse(
        BytesIO(xlsx_bytes),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/v2/partials/quote-builder/{req_id}/export/pdf")
async def quote_builder_export_pdf(
    req_id: int,
    quote_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Stream a PDF export of a saved quote (reuses existing PDF generator)."""
    import asyncio

    from fastapi.responses import Response

    from ..models import Quote

    quote = db.get(Quote, quote_id)
    if not quote or quote.requisition_id != req_id:
        raise HTTPException(404, "Quote not found")

    from ..services.document_service import generate_quote_report_pdf

    try:
        loop = asyncio.get_running_loop()
        pdf_bytes = await loop.run_in_executor(None, generate_quote_report_pdf, quote.id, db)
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        logger.error("PDF generation failed for quote %d: %s", quote_id, e)
        raise HTTPException(500, "PDF generation failed")

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{quote.quote_number}.pdf"'},
    )
```

- [ ] **Step 4: Register router in main.py**

Add to `app/main.py` after the existing router imports (around line 623):

```python
from .routers.quote_builder import router as quote_builder_router
```

And in the `include_router` block:

```python
app.include_router(quote_builder_router)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_quote_builder.py -v -x`
Expected: All tests PASSED

- [ ] **Step 6: Commit**

```bash
cd /root/availai && git add app/routers/quote_builder.py app/main.py tests/test_quote_builder.py && git commit -m "feat(quote-builder): add router with modal, save, Excel, and PDF endpoints"
```

---

### Task 6: Full-Screen Modal Shell

**Files:**
- Create: `app/templates/htmx/partials/shared/quote_builder_shell.html`
- Modify: `app/templates/htmx/base.html`

- [ ] **Step 1: Create the modal shell template**

```html
{# quote_builder_shell.html — Full-screen modal overlay for Quote Builder.
   Listens on open-quote-builder / close-quote-builder window events.
   Contains #quote-builder-content as HTMX swap target.
   Called by: base.html (included after global modal).
   Depends on: Alpine.js, Tailwind CSS.
#}
<div x-data="{ qbOpen: false }"
     @open-quote-builder.window="qbOpen = true"
     @close-quote-builder.window="qbOpen = false"
     @keydown.escape.window.stop="if(qbOpen) { $event.stopPropagation(); }"
     x-show="qbOpen"
     x-cloak
     class="fixed inset-0 z-[60] flex items-stretch justify-stretch">

  {# Backdrop #}
  <div x-show="qbOpen"
       x-transition:enter="transition-opacity duration-500"
       x-transition:enter-start="opacity-0"
       x-transition:enter-end="opacity-100"
       x-transition:leave="transition-opacity duration-300"
       x-transition:leave-start="opacity-100"
       x-transition:leave-end="opacity-0"
       class="fixed inset-0 bg-brand-900/60 backdrop-blur-sm"
       @click="if(confirm('Close Quote Builder? Unsaved changes will be lost.')) qbOpen = false">
  </div>

  {# Modal shell #}
  <div x-show="qbOpen"
       x-transition:enter="transition ease-out duration-300"
       x-transition:enter-start="opacity-0 scale-95"
       x-transition:enter-end="opacity-100 scale-100"
       x-transition:leave="transition ease-in duration-200"
       x-transition:leave-start="opacity-100 scale-100"
       x-transition:leave-end="opacity-0 scale-95"
       x-trap.noscroll="qbOpen"
       class="relative z-10 m-4 flex flex-col flex-1 bg-white rounded-xl shadow-2xl overflow-hidden border border-brand-200">
    <div id="quote-builder-content" class="flex-1 min-h-0 flex flex-col overflow-hidden">
      {# Content loaded via HTMX #}
    </div>
  </div>
</div>
```

- [ ] **Step 2: Include in base.html**

In `app/templates/htmx/base.html`, after the existing modal div (around line 178), add:

```html
  {% include "htmx/partials/shared/quote_builder_shell.html" %}
```

- [ ] **Step 3: Verify template renders without errors**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_quote_builder.py -v -x`
Expected: All existing tests still pass (no template render errors on import)

- [ ] **Step 4: Commit**

```bash
cd /root/availai && git add app/templates/htmx/partials/shared/quote_builder_shell.html app/templates/htmx/base.html && git commit -m "feat(quote-builder): add full-screen modal shell with open/close events"
```

---

### Task 7: Quote Builder Modal Template

**Files:**
- Create: `app/templates/htmx/partials/quote_builder/modal.html`

This is the largest task — the full two-panel modal content. The Alpine.js component is defined in the next task (Task 8). This template references `quoteBuilder()` which will be registered in `htmx_app.js`.

- [ ] **Step 1: Create the modal template**

Create `app/templates/htmx/partials/quote_builder/modal.html` with the full two-panel layout. Key structure:

```html
{# quote_builder/modal.html — Two-panel quote builder modal content.
   Receives: req (Requisition), lines (list of dicts), customer_name (str),
             has_customer_site (bool).
   Called by: quote_builder.py quote_builder_modal endpoint.
   Depends on: Alpine.js quoteBuilder component, brand palette, compact-table CSS.
#}

<div class="flex flex-col h-full"
     x-data="quoteBuilder({{ lines | tojson }}, {{ req.id }}, {{ has_customer_site | tojson }})">

  {# ── Top Bar ──────────────────────────────────────────────── #}
  <div class="flex items-center justify-between px-6 py-3 bg-brand-50 border-b-2 border-brand-200 flex-shrink-0">
    <div class="flex items-center gap-3">
      <h2 class="text-lg font-bold text-brand-800 tracking-tight">Quote Builder</h2>
      <span x-show="saved" class="inline-flex px-2 py-0.5 text-xs font-medium rounded-full bg-emerald-100 text-emerald-700" x-text="quoteNumber" x-cloak></span>
      <span class="text-sm text-gray-500">
        for <span class="font-medium text-gray-700">{{ customer_name or req.name }}</span>
      </span>
    </div>
    <div class="flex items-center gap-3">
      {# Inline notification area #}
      <span x-show="saveError" x-cloak class="text-sm text-rose-600 font-medium" x-text="saveError"></span>
      <span x-show="saved && !saveError" x-cloak class="text-sm text-emerald-600 font-medium">Saved</span>
      <button @click="closeBuilder()"
              class="p-2 text-gray-400 hover:text-gray-600 hover:bg-white rounded-lg transition-colors"
              title="Close (Esc)">
        <svg class="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/>
        </svg>
      </button>
    </div>
  </div>

  {# ── No customer site warning ─────────────────────────────── #}
  {% if not has_customer_site %}
  <div class="px-6 py-3 bg-amber-50 border-b border-amber-200 flex items-center gap-2 flex-shrink-0">
    <svg class="w-4 h-4 text-amber-600" fill="none" viewBox="0 0 24 24" stroke="currentColor">
      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-2.5L13.732 4c-.77-.833-1.964-.833-2.732 0L4.082 16.5c-.77.833.192 2.5 1.732 2.5z"/>
    </svg>
    <span class="text-sm text-amber-800 font-medium">Link a customer to this requisition before saving.</span>
  </div>
  {% endif %}

  {# ── Two-panel body ───────────────────────────────────────── #}
  <div class="flex flex-1 min-h-0 overflow-hidden">

    {# LEFT PANEL: Progress tracker #}
    <div class="w-[320px] flex-shrink-0 border-r-2 border-brand-200 flex flex-col bg-white">
      {# Progress header #}
      <div class="px-4 py-3 border-b border-brand-100 flex-shrink-0">
        <div class="flex items-center justify-between mb-2">
          <span class="text-sm font-semibold text-gray-700">Progress</span>
          <span class="text-sm font-bold text-brand-600 tabular-nums"
                x-text="decidedCount + '/' + totalCount + ' decided' + (skippedCount ? ', ' + skippedCount + ' skipped' : '')"></span>
        </div>
        <div class="h-1.5 bg-brand-100 rounded-full overflow-hidden">
          <div class="h-full bg-emerald-500 rounded-full transition-all duration-500 ease-out"
               :style="'width:' + decidedPct + '%'"></div>
        </div>
      </div>

      {# Filter pills #}
      <div class="px-4 py-2.5 flex flex-wrap gap-1.5 border-b border-brand-100 flex-shrink-0">
        <template x-for="f in filterOptions" :key="f.key">
          <button @click="setFilter(f.key)"
                  :class="activeFilter === f.key ? 'bg-brand-500 text-white' : 'bg-gray-100 text-gray-600 hover:bg-gray-200'"
                  class="text-xs px-2.5 py-1 rounded-full font-medium transition-colors tabular-nums">
            <span x-text="f.label"></span>
            <span x-show="f.count > 0" class="ml-0.5" x-text="'(' + f.count + ')'"></span>
          </button>
        </template>
      </div>

      {# Scrollable requirement list #}
      <div class="flex-1 overflow-y-auto qb-list">
        <template x-for="(line, idx) in filteredLines" :key="line.requirement_id">
          <button @click="selectLine(idx)"
                  :class="{
                    'bg-brand-50 shadow-[inset_3px_0_0_#3d6895]': activeIdx === idx,
                    'hover:bg-gray-50': activeIdx !== idx
                  }"
                  class="w-full px-4 py-2.5 flex items-center gap-3 text-left border-b border-gray-100 transition-colors">
            <span class="w-2.5 h-2.5 rounded-full flex-shrink-0 transition-colors duration-300"
                  :class="{
                    'bg-emerald-500': line.status === 'decided',
                    'bg-amber-400': line.status === 'needs_review',
                    'bg-gray-300': line.status === 'no_offers',
                    'bg-brand-400': line.status === 'has_offers',
                    'bg-slate-400': line.status === 'skipped'
                  }"></span>
            <div class="flex-1 min-w-0">
              <p class="text-sm font-mono font-medium text-gray-900 truncate" x-text="line.mpn"></p>
              <p class="text-xs text-gray-400 truncate">
                <span x-text="line.target_qty + ' pcs'"></span>
                <span x-show="line.target_price" class="ml-1">@ $<span x-text="line.target_price?.toFixed(4)" class="tabular-nums"></span></span>
              </p>
            </div>
            <span x-show="line.offer_count > 0"
                  class="flex-shrink-0 px-1.5 py-0.5 text-[10px] font-semibold rounded bg-brand-100 text-brand-600 tabular-nums"
                  x-text="line.offer_count"></span>
          </button>
        </template>
        <div x-show="filteredLines.length === 0" class="px-4 py-8 text-center text-sm text-gray-400">
          No lines match this filter.
        </div>
      </div>

      {# Keyboard hints #}
      <div class="px-4 py-2 border-t border-brand-100 text-[10px] text-gray-400 flex gap-3 flex-shrink-0">
        <span><kbd class="px-1 py-0.5 bg-gray-100 rounded text-[9px] font-mono border border-gray-200">j</kbd>/<kbd class="px-1 py-0.5 bg-gray-100 rounded text-[9px] font-mono border border-gray-200">k</kbd> Nav</span>
        <span><kbd class="px-1 py-0.5 bg-gray-100 rounded text-[9px] font-mono border border-gray-200">1</kbd>-<kbd class="px-1 py-0.5 bg-gray-100 rounded text-[9px] font-mono border border-gray-200">9</kbd> Offer</span>
        <span><kbd class="px-1 py-0.5 bg-gray-100 rounded text-[9px] font-mono border border-gray-200">Enter</kbd> Confirm</span>
      </div>
    </div>

    {# RIGHT PANEL: Decision workspace #}
    <div class="flex-1 overflow-y-auto px-6 py-5 space-y-5" x-show="activeLine" x-cloak>

      {# Desktop-only guard #}
      <div class="lg:hidden flex items-center justify-center h-full p-8 text-center">
        <p class="text-sm text-gray-500 font-medium">Quote Builder requires a desktop browser (1024px+).</p>
      </div>

      <div class="hidden lg:block space-y-5">

        {# 1. Customer Specs #}
        <div class="bg-gray-50 rounded-lg border border-brand-100 px-4 py-3">
          <h3 class="text-[10px] font-semibold uppercase tracking-wider text-brand-400 mb-2">Customer Requirements</h3>
          <div class="grid grid-cols-2 md:grid-cols-4 gap-x-6 gap-y-1.5">
            <div>
              <span class="text-[10px] text-gray-400 uppercase tracking-wide">Qty Needed</span>
              <p class="text-sm font-mono font-semibold text-gray-900" x-text="activeLine.target_qty?.toLocaleString() || '—'"></p>
            </div>
            <div>
              <span class="text-[10px] text-gray-400 uppercase tracking-wide">Target Price</span>
              <p class="text-sm font-mono font-semibold text-gray-900" x-text="activeLine.target_price ? '$' + activeLine.target_price.toFixed(4) : '—'"></p>
            </div>
            <div>
              <span class="text-[10px] text-gray-400 uppercase tracking-wide">Date Codes</span>
              <p class="text-sm font-mono text-gray-900" x-text="activeLine.date_codes || '—'"></p>
            </div>
            <div>
              <span class="text-[10px] text-gray-400 uppercase tracking-wide">Condition</span>
              <p class="text-sm font-mono text-gray-900" x-text="activeLine.condition || '—'"></p>
            </div>
            <div>
              <span class="text-[10px] text-gray-400 uppercase tracking-wide">Packaging</span>
              <p class="text-sm font-mono text-gray-900" x-text="activeLine.packaging || '—'"></p>
            </div>
            <div>
              <span class="text-[10px] text-gray-400 uppercase tracking-wide">Firmware</span>
              <p class="text-sm font-mono text-gray-900" x-text="activeLine.firmware || '—'"></p>
            </div>
            <div>
              <span class="text-[10px] text-gray-400 uppercase tracking-wide">HW Codes</span>
              <p class="text-sm font-mono text-gray-900" x-text="activeLine.hardware_codes || '—'"></p>
            </div>
            <div>
              <span class="text-[10px] text-gray-400 uppercase tracking-wide">Customer PN</span>
              <p class="text-sm font-mono text-gray-900" x-text="activeLine.customer_pn || '—'"></p>
            </div>
          </div>
          <template x-if="activeLine.sale_notes">
            <div class="mt-2 pt-2 border-t border-brand-100">
              <span class="text-[10px] text-gray-400 uppercase tracking-wide">Notes</span>
              <p class="text-sm text-gray-700 mt-0.5" x-text="activeLine.sale_notes"></p>
            </div>
          </template>
        </div>

        {# 2. Offers Table #}
        <div>
          <h3 class="text-[10px] font-semibold uppercase tracking-wider text-brand-400 mb-2 flex items-center gap-2">
            Vendor Offers
            <span class="inline-flex px-1.5 py-0.5 text-[10px] font-semibold rounded bg-brand-100 text-brand-600"
                  x-text="activeLine.offers.length"></span>
          </h3>
          <div x-show="activeLine.offers.length > 0" class="rounded-lg border-2 border-brand-200 overflow-hidden">
            <table class="min-w-full divide-y divide-gray-200">
              <thead class="bg-gray-50">
                <tr>
                  <th class="w-8 px-2 py-2"></th>
                  <th class="px-3 py-2 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Vendor</th>
                  <th class="px-3 py-2 text-right text-xs font-medium text-gray-500 uppercase tracking-wider">Price</th>
                  <th class="px-3 py-2 text-right text-xs font-medium text-gray-500 uppercase tracking-wider">Qty Avail</th>
                  <th class="px-3 py-2 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Lead Time</th>
                  <th class="px-3 py-2 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Date Codes</th>
                  <th class="px-3 py-2 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Condition</th>
                  <th class="px-3 py-2 text-right text-xs font-medium text-gray-500 uppercase tracking-wider">MOQ</th>
                  <th class="px-3 py-2 text-right text-xs font-medium text-gray-500 uppercase tracking-wider">Confidence</th>
                </tr>
              </thead>
              <tbody class="divide-y divide-gray-200">
                <template x-for="(offer, oIdx) in activeLine.offers" :key="offer.id">
                  <tr @click="selectOffer(offer)"
                      :class="{
                        'bg-brand-50 shadow-[inset_2px_0_0_#3d6895]': activeLine.selected_offer_id === offer.id,
                        'opacity-50': offer.qty_available < activeLine.target_qty
                      }"
                      class="cursor-pointer hover:bg-gray-50 transition-colors">
                    <td class="px-2 py-2 text-center">
                      <div class="w-4 h-4 rounded-full border-2 flex items-center justify-center mx-auto transition-colors"
                           :class="activeLine.selected_offer_id === offer.id ? 'border-brand-500 bg-brand-500' : 'border-gray-300 bg-white'">
                        <div x-show="activeLine.selected_offer_id === offer.id"
                             x-transition:enter="transition-transform duration-150"
                             x-transition:enter-start="scale-0"
                             x-transition:enter-end="scale-100"
                             class="w-1.5 h-1.5 rounded-full bg-white"></div>
                      </div>
                    </td>
                    <td class="px-3 py-2 text-sm font-medium text-gray-900" x-text="offer.vendor_name"></td>
                    <td class="px-3 py-2 text-sm text-right font-mono tabular-nums" x-text="'$' + offer.unit_price.toFixed(4)"></td>
                    <td class="px-3 py-2 text-sm text-right tabular-nums">
                      <span x-text="offer.qty_available.toLocaleString()"></span>
                      <span x-show="offer.qty_available < activeLine.target_qty"
                            class="ml-1 text-[10px] text-amber-600 font-medium">Low</span>
                    </td>
                    <td class="px-3 py-2 text-sm text-gray-600" x-text="offer.lead_time || '—'"></td>
                    <td class="px-3 py-2 text-sm text-gray-600" x-text="offer.date_code || '—'"></td>
                    <td class="px-3 py-2 text-sm text-gray-600" x-text="offer.condition || '—'"></td>
                    <td class="px-3 py-2 text-sm text-right tabular-nums" x-text="offer.moq || '—'"></td>
                    <td class="px-3 py-2 text-sm text-right tabular-nums">
                      <span x-show="offer.confidence != null"
                            :class="offer.confidence >= 0.8 ? 'text-emerald-600' : offer.confidence >= 0.5 ? 'text-amber-600' : 'text-rose-600'"
                            class="font-semibold"
                            x-text="Math.round(offer.confidence * 100) + '%'"></span>
                      <span x-show="offer.confidence == null" class="text-gray-300">—</span>
                    </td>
                  </tr>
                </template>
              </tbody>
            </table>
          </div>
          <div x-show="activeLine.offers.length === 0" class="py-6 text-center text-sm text-gray-400">
            No offers for this part.
          </div>

          {# Price spread bar #}
          <template x-if="activeLine.offers.length > 1">
            <div class="mt-2 px-1">
              <div class="flex items-center gap-2 text-[10px] text-gray-400">
                <span class="font-mono tabular-nums" x-text="'$' + minPrice.toFixed(4)"></span>
                <div class="flex-1 relative h-4 flex items-center">
                  <div class="absolute inset-x-0 h-1 bg-gradient-to-r from-emerald-200 via-amber-200 to-rose-200 rounded-full"></div>
                  <template x-for="offer in activeLine.offers" :key="'spread-' + offer.id">
                    <div class="absolute w-2.5 h-2.5 rounded-full border-2 border-white shadow-sm transition-all duration-200"
                         :class="activeLine.selected_offer_id === offer.id ? 'bg-brand-500 scale-125 z-10' : 'bg-gray-400'"
                         :style="'left:' + pricePosition(offer.unit_price) + '%'"
                         :title="offer.vendor_name + ': $' + offer.unit_price.toFixed(4)"></div>
                  </template>
                </div>
                <span class="font-mono tabular-nums" x-text="'$' + maxPrice.toFixed(4)"></span>
              </div>
            </div>
          </template>
        </div>

        {# 3. Pricing History (collapsible) #}
        <template x-if="activeLine.pricing_history && activeLine.pricing_history.recent && activeLine.pricing_history.recent.length > 0">
          <div x-data="{ histOpen: false }">
            <button @click="histOpen = !histOpen"
                    class="text-sm text-brand-500 hover:text-brand-600 font-medium flex items-center gap-1">
              <svg class="w-4 h-4 transition-transform" :class="histOpen && 'rotate-90'" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7"/>
              </svg>
              Pricing History
              <span class="text-xs text-gray-400 ml-1" x-text="'(' + activeLine.pricing_history.recent.length + ' prior)'"></span>
            </button>
            <div x-show="histOpen" x-collapse x-cloak class="mt-2 rounded-lg border border-brand-100 overflow-hidden">
              <table class="min-w-full divide-y divide-gray-200">
                <thead class="bg-gray-50">
                  <tr>
                    <th class="px-3 py-2 text-left text-xs font-medium text-gray-500 uppercase">Quote</th>
                    <th class="px-3 py-2 text-left text-xs font-medium text-gray-500 uppercase">Date</th>
                    <th class="px-3 py-2 text-right text-xs font-medium text-gray-500 uppercase">Cost</th>
                    <th class="px-3 py-2 text-right text-xs font-medium text-gray-500 uppercase">Sell</th>
                    <th class="px-3 py-2 text-right text-xs font-medium text-gray-500 uppercase">Margin</th>
                    <th class="px-3 py-2 text-left text-xs font-medium text-gray-500 uppercase">Result</th>
                  </tr>
                </thead>
                <tbody class="divide-y divide-gray-200">
                  <template x-for="h in activeLine.pricing_history.recent" :key="h.quote_number">
                    <tr>
                      <td class="px-3 py-1.5 text-sm text-gray-700" x-text="h.quote_number"></td>
                      <td class="px-3 py-1.5 text-sm text-gray-500" x-text="h.date"></td>
                      <td class="px-3 py-1.5 text-sm text-right font-mono" x-text="'$' + (h.cost || 0).toFixed(4)"></td>
                      <td class="px-3 py-1.5 text-sm text-right font-mono" x-text="'$' + (h.sell || 0).toFixed(4)"></td>
                      <td class="px-3 py-1.5 text-sm text-right font-semibold tabular-nums"
                          :class="h.margin >= 25 ? 'text-emerald-600' : h.margin >= 15 ? 'text-amber-600' : 'text-rose-600'"
                          x-text="(h.margin || 0).toFixed(1) + '%'"></td>
                      <td class="px-3 py-1.5">
                        <span class="inline-flex px-1.5 py-0.5 text-[10px] font-semibold rounded-full"
                              :class="h.result === 'won' ? 'bg-emerald-50 text-emerald-700' : h.result === 'lost' ? 'bg-rose-50 text-rose-700' : 'bg-gray-100 text-gray-600'"
                              x-text="h.result || 'draft'"></span>
                      </td>
                    </tr>
                  </template>
                </tbody>
              </table>
            </div>
          </div>
        </template>

        {# 4. Decision Area #}
        <div class="rounded-lg border-2 p-4 space-y-3 transition-colors duration-300"
             :class="activeLine.status === 'decided' ? 'border-emerald-300 bg-emerald-50/30' : 'border-brand-200 bg-brand-50'">
          <h3 class="text-[10px] font-semibold uppercase tracking-wider mb-1 transition-colors duration-300"
              :class="activeLine.status === 'decided' ? 'text-emerald-600' : 'text-brand-400'">
            <span x-show="activeLine.status !== 'decided'">Set Price</span>
            <span x-show="activeLine.status === 'decided'">Decided</span>
          </h3>

          {# Selected offer summary #}
          <div x-show="selectedOffer" class="flex items-center gap-4 text-sm" x-cloak>
            <span class="text-gray-500">Vendor:</span>
            <span class="font-medium text-gray-900" x-text="selectedOffer?.vendor_name"></span>
            <span class="text-gray-500">Cost:</span>
            <span class="font-mono font-semibold text-gray-900" x-text="'$' + (selectedOffer?.unit_price || 0).toFixed(4)"></span>
            <span x-show="selectedOffer && selectedOffer.qty_available < activeLine.target_qty"
                  class="text-xs text-amber-600 font-medium" x-cloak>
              Offer has <span x-text="selectedOffer?.qty_available?.toLocaleString()"></span> pcs, need <span x-text="activeLine.target_qty?.toLocaleString()"></span>
            </span>
          </div>

          {# Sell price + margin #}
          <div class="flex items-end gap-4">
            <div>
              <label class="block text-[10px] text-gray-500 uppercase tracking-wide mb-1">Sell Price</label>
              <div class="relative">
                <span class="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400 text-sm">$</span>
                <input type="number" step="0.0001"
                       x-ref="sellPriceInput"
                       :value="activeLine.sell_price"
                       @input="activeLine.sell_price = parseFloat($event.target.value) || 0; activeLine.sell_price_manual = true;"
                       @keydown.enter.prevent="confirmDecision()"
                       :disabled="!activeLine.selected_offer_id"
                       class="w-36 pl-7 pr-3 py-2 text-sm font-mono border-2 rounded-lg focus:ring-2 focus:ring-brand-500 focus:border-brand-500 transition-colors disabled:opacity-50"
                       :class="activeLine.status === 'decided' ? 'border-emerald-300 bg-white' : 'border-brand-200'"
                       placeholder="0.0000">
              </div>
            </div>
            <div>
              <label class="block text-[10px] text-gray-500 uppercase tracking-wide mb-1">Margin</label>
              <p class="text-2xl font-bold tabular-nums py-0.5"
                 :class="{
                   'text-emerald-600': margin >= 25,
                   'text-amber-600': margin >= 15 && margin < 25,
                   'text-rose-600': margin > 0 && margin < 15,
                   'text-gray-300': !margin
                 }"
                 x-text="margin ? margin.toFixed(1) + '%' : '—'"></p>
            </div>
            <div class="flex gap-4 ml-auto text-center">
              <div>
                <span class="text-[10px] text-gray-400 uppercase tracking-wide">Ext Cost</span>
                <p class="text-sm font-mono font-semibold text-gray-900" x-text="'$' + extCost.toFixed(2)"></p>
              </div>
              <div>
                <span class="text-[10px] text-gray-400 uppercase tracking-wide">Ext Sell</span>
                <p class="text-sm font-mono font-semibold text-gray-900" x-text="'$' + extSell.toFixed(2)"></p>
              </div>
              <div>
                <span class="text-[10px] text-gray-400 uppercase tracking-wide">Line Profit</span>
                <p class="text-sm font-mono font-semibold"
                   :class="lineProfit > 0 ? 'text-emerald-600' : 'text-rose-600'"
                   x-text="'$' + lineProfit.toFixed(2)"></p>
              </div>
            </div>
          </div>

          {# Line notes #}
          <div>
            <label class="block text-[10px] text-gray-500 uppercase tracking-wide mb-1">Line Notes</label>
            <input type="text" x-model="activeLine.buyer_notes" placeholder="Optional notes..."
                   class="w-full px-3 py-1.5 text-sm border border-brand-100 rounded-lg focus:ring-2 focus:ring-brand-500 focus:border-brand-500 bg-white placeholder-gray-300">
          </div>

          {# Actions #}
          <div class="flex items-center justify-between pt-1">
            <div class="flex gap-3">
              <button x-show="activeLine.status === 'decided' || activeLine.status === 'skipped'" @click="undoDecision()"
                      class="text-xs text-gray-400 hover:text-gray-600 transition-colors" x-cloak>Undo</button>
              <button @click="skipLine()"
                      x-show="activeLine.status !== 'skipped'"
                      class="text-xs text-gray-400 hover:text-gray-600 transition-colors">Skip</button>
            </div>
            <button @click="confirmDecision()"
                    :disabled="!activeLine.selected_offer_id || !activeLine.sell_price"
                    class="px-4 py-1.5 text-sm font-medium text-white rounded-lg transition-colors flex items-center gap-1.5 disabled:opacity-50"
                    :class="activeLine.status === 'decided' ? 'bg-emerald-600 hover:bg-emerald-700' : 'bg-brand-500 hover:bg-brand-600'">
              <span x-text="activeLine.status === 'decided' ? 'Update & Next' : 'Confirm & Next'"></span>
              <svg class="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 7l5 5m0 0l-5 5m5-5H6"/>
              </svg>
            </button>
          </div>
        </div>
      </div>
    </div>

    {# Empty state when no line selected #}
    <div class="flex-1 flex items-center justify-center" x-show="!activeLine">
      <p class="text-sm text-gray-400">Select a requirement from the list.</p>
    </div>
  </div>

  {# ── Bottom Bar ───────────────────────────────────────────── #}
  <div class="flex-shrink-0 px-6 py-3 bg-white border-t-2 border-brand-200 flex items-center justify-between">
    <div class="flex gap-6">
      <div class="text-center">
        <p class="text-[10px] text-gray-400 uppercase tracking-wide">Decided</p>
        <p class="text-lg font-bold text-brand-600 tabular-nums">
          <span x-text="decidedCount"></span>/<span x-text="totalCount" class="text-gray-400"></span>
        </p>
      </div>
      <div class="text-center">
        <p class="text-[10px] text-gray-400 uppercase tracking-wide">Total Cost</p>
        <p class="text-lg font-bold text-gray-900 font-mono tabular-nums" x-text="'$' + totalCost.toFixed(2)"></p>
      </div>
      <div class="text-center">
        <p class="text-[10px] text-gray-400 uppercase tracking-wide">Total Sell</p>
        <p class="text-lg font-bold text-gray-900 font-mono tabular-nums" x-text="'$' + totalSell.toFixed(2)"></p>
      </div>
      <div class="text-center">
        <p class="text-[10px] text-gray-400 uppercase tracking-wide">Blended Margin</p>
        <p class="text-lg font-bold tabular-nums"
           :class="blendedMargin >= 25 ? 'text-emerald-600' : blendedMargin >= 15 ? 'text-amber-600' : 'text-rose-600'"
           x-text="blendedMargin.toFixed(1) + '%'"></p>
      </div>
      {# Bulk markup #}
      <div class="flex items-end gap-2">
        <div>
          <label class="block text-[10px] text-gray-400 uppercase tracking-wide mb-0.5">Markup %</label>
          <input type="number" step="0.1" x-model.number="bulkMarkupPct" value="25"
                 class="w-20 px-2 py-1 text-sm border border-brand-200 rounded focus:ring-brand-500 focus:border-brand-500">
        </div>
        <button @click="applyBulkMarkup()"
                :disabled="decidedCount === 0"
                class="px-3 py-1 text-xs font-medium text-brand-600 border border-brand-200 rounded hover:bg-brand-50 transition-colors disabled:opacity-50">
          Apply
        </button>
      </div>
    </div>
    <div class="flex items-center gap-2">
      <button @click="exportExcel()"
              :disabled="!saved"
              class="px-4 py-2 text-sm font-medium text-gray-600 border border-gray-300 rounded-lg hover:bg-gray-50 flex items-center gap-1.5 transition-colors disabled:opacity-50">
        <svg class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 10v6m0 0l-3-3m3 3l3-3m2 8H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"/></svg>
        Excel
      </button>
      <button @click="exportPdf()"
              :disabled="!saved"
              class="px-4 py-2 text-sm font-medium text-gray-600 border border-gray-300 rounded-lg hover:bg-gray-50 flex items-center gap-1.5 transition-colors disabled:opacity-50">
        <svg class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M7 21h10a2 2 0 002-2V9.414a1 1 0 00-.293-.707l-5.414-5.414A1 1 0 0012.586 3H7a2 2 0 00-2 2v14a2 2 0 002 2z"/></svg>
        TRIO PDF
      </button>
      <button @click="saveQuote()"
              :disabled="!hasCustomerSite || decidedCount === 0 || saving"
              class="px-4 py-2 text-sm font-medium text-white bg-brand-500 rounded-lg hover:bg-brand-600 flex items-center gap-1.5 transition-colors disabled:opacity-50">
        <svg x-show="saving" class="w-4 h-4 animate-spin" fill="none" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10" stroke="currentColor" stroke-width="3" stroke-dasharray="31.4" stroke-dashoffset="10"/></svg>
        <svg x-show="!saving" class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"/></svg>
        <span x-text="saved ? 'Update Quote' : 'Save Quote'"></span>
      </button>
    </div>
  </div>
</div>
```

- [ ] **Step 2: Commit**

```bash
cd /root/availai && mkdir -p app/templates/htmx/partials/quote_builder && git add app/templates/htmx/partials/quote_builder/modal.html && git commit -m "feat(quote-builder): add two-panel modal template with full UI"
```

---

### Task 8: Alpine.js Component

**Files:**
- Modify: `app/static/htmx_app.js`

- [ ] **Step 1: Add quoteBuilder Alpine.js component**

Add before `Alpine.start()` in `app/static/htmx_app.js`:

```javascript
Alpine.data('quoteBuilder', (initialLines, reqId, hasCustomerSite) => ({
  lines: initialLines,
  reqId: reqId,
  hasCustomerSite: hasCustomerSite,
  activeIdx: 0,
  activeFilter: 'has_offers',
  saving: false,
  saved: false,
  quoteId: null,
  quoteNumber: null,
  saveError: null,
  bulkMarkupPct: 25,

  init() {
    // Auto-select first line with offers
    const idx = this.filteredLines.findIndex(l => l.status === 'needs_review' || l.status === 'decided');
    if (idx >= 0) this.activeIdx = idx;
    // Keyboard handler
    this._keyHandler = (e) => this.handleKeydown(e);
    window.addEventListener('keydown', this._keyHandler);
  },
  destroy() {
    window.removeEventListener('keydown', this._keyHandler);
  },

  // ── Computed ──
  get activeLine() { return this.filteredLines[this.activeIdx] ?? null; },
  get selectedOffer() {
    if (!this.activeLine) return null;
    return this.activeLine.offers.find(o => o.id === this.activeLine.selected_offer_id) ?? null;
  },
  get margin() {
    if (!this.activeLine?.sell_price || !this.selectedOffer) return null;
    const sell = this.activeLine.sell_price;
    const cost = this.selectedOffer.unit_price;
    return sell > 0 ? ((sell - cost) / sell * 100) : 0;
  },
  get extCost() {
    if (!this.activeLine || !this.selectedOffer) return 0;
    return (this.activeLine.target_qty || 0) * this.selectedOffer.unit_price;
  },
  get extSell() {
    if (!this.activeLine) return 0;
    return (this.activeLine.target_qty || 0) * (this.activeLine.sell_price || 0);
  },
  get lineProfit() { return this.extSell - this.extCost; },
  get minPrice() {
    if (!this.activeLine?.offers.length) return 0;
    return Math.min(...this.activeLine.offers.map(o => o.unit_price));
  },
  get maxPrice() {
    if (!this.activeLine?.offers.length) return 0;
    return Math.max(...this.activeLine.offers.map(o => o.unit_price));
  },
  pricePosition(price) {
    const range = this.maxPrice - this.minPrice;
    if (range === 0) return 50;
    return Math.round(((price - this.minPrice) / range) * 100);
  },

  get filterOptions() {
    return [
      { key: 'all', label: 'All', count: this.lines.length },
      { key: 'has_offers', label: 'Has Offers', count: this.lines.filter(l => l.offer_count > 0).length },
      { key: 'needs_review', label: 'Needs Review', count: this.lines.filter(l => l.status === 'needs_review').length },
      { key: 'decided', label: 'Decided', count: this.decidedCount },
      { key: 'skipped', label: 'Skipped', count: this.skippedCount },
    ];
  },
  get filteredLines() {
    if (this.activeFilter === 'all') return this.lines;
    if (this.activeFilter === 'has_offers') return this.lines.filter(l => l.offer_count > 0);
    return this.lines.filter(l => l.status === this.activeFilter);
  },
  get decidedCount() { return this.lines.filter(l => l.status === 'decided').length; },
  get skippedCount() { return this.lines.filter(l => l.status === 'skipped').length; },
  get totalCount() { return this.lines.length; },
  get decidedPct() { return this.totalCount ? Math.round(this.decidedCount / this.totalCount * 100) : 0; },

  get totalCost() {
    return this.lines.filter(l => l.status === 'decided').reduce((s, l) => {
      const offer = l.offers.find(o => o.id === l.selected_offer_id);
      return s + (l.target_qty || 0) * (offer?.unit_price || 0);
    }, 0);
  },
  get totalSell() {
    return this.lines.filter(l => l.status === 'decided').reduce((s, l) => {
      return s + (l.target_qty || 0) * (l.sell_price || 0);
    }, 0);
  },
  get blendedMargin() {
    return this.totalSell > 0 ? ((this.totalSell - this.totalCost) / this.totalSell * 100) : 0;
  },

  // ── Actions ──
  selectLine(idx) { this.activeIdx = idx; },
  setFilter(f) { this.activeFilter = f; this.activeIdx = 0; },

  selectOffer(offer) {
    if (!this.activeLine) return;
    this.activeLine.selected_offer_id = offer.id;
    if (!this.activeLine.sell_price_manual) {
      this.activeLine.sell_price = offer.unit_price;
    }
  },

  confirmDecision() {
    if (!this.activeLine?.selected_offer_id || !this.activeLine?.sell_price) return;
    this.activeLine.status = 'decided';
    // Flash the left-panel row
    this.$nextTick(() => {
      const rows = this.$el.querySelectorAll('.qb-list button');
      if (rows[this.activeIdx]) {
        rows[this.activeIdx].classList.add('qb-decision-flash');
        setTimeout(() => rows[this.activeIdx]?.classList.remove('qb-decision-flash'), 800);
      }
    });
    // Auto-advance to next undecided
    this.advanceToNext();
  },

  skipLine() {
    if (!this.activeLine) return;
    this.activeLine.status = 'skipped';
    this.advanceToNext();
  },

  undoDecision() {
    if (!this.activeLine) return;
    this.activeLine.status = this.activeLine.offer_count > 0 ? 'needs_review' : 'no_offers';
    this.activeLine.selected_offer_id = null;
    this.activeLine.sell_price = null;
    this.activeLine.sell_price_manual = false;
  },

  advanceToNext() {
    const nextIdx = this.filteredLines.findIndex((l, i) => i > this.activeIdx && l.status === 'needs_review');
    if (nextIdx >= 0) {
      this.activeIdx = nextIdx;
    } else {
      // Wrap around or stay
      const wrapIdx = this.filteredLines.findIndex(l => l.status === 'needs_review');
      if (wrapIdx >= 0) this.activeIdx = wrapIdx;
    }
  },

  applyBulkMarkup() {
    const pct = this.bulkMarkupPct;
    if (!pct || pct <= 0) return;
    this.lines.forEach(l => {
      if (l.status === 'decided' && !l.sell_price_manual) {
        const offer = l.offers.find(o => o.id === l.selected_offer_id);
        if (offer) {
          l.sell_price = parseFloat((offer.unit_price * (1 + pct / 100)).toFixed(4));
        }
      }
    });
  },

  closeBuilder() {
    const hasChanges = this.lines.some(l => l.status === 'decided' || l.status === 'skipped');
    if (hasChanges && !this.saved) {
      if (!confirm('You have unsaved line decisions. Close anyway?')) return;
    }
    window.dispatchEvent(new CustomEvent('close-quote-builder'));
  },

  async saveQuote() {
    this.saving = true;
    this.saveError = null;
    const decided = this.lines.filter(l => l.status === 'decided');
    const linePayload = decided.map(l => {
      const offer = l.offers.find(o => o.id === l.selected_offer_id);
      const cost = offer?.unit_price || 0;
      const sell = l.sell_price || 0;
      const margin = sell > 0 ? parseFloat(((sell - cost) / sell * 100).toFixed(2)) : 0;
      return {
        requirement_id: l.requirement_id,
        offer_id: l.selected_offer_id,
        mpn: l.mpn,
        manufacturer: l.manufacturer,
        qty: l.target_qty,
        cost_price: cost,
        sell_price: sell,
        margin_pct: margin,
        lead_time: offer?.lead_time || null,
        date_code: offer?.date_code || null,
        condition: offer?.condition || null,
        packaging: offer?.packaging || null,
        moq: offer?.moq || null,
        material_card_id: offer?.material_card_id || null,
        notes: l.buyer_notes || null,
      };
    });
    try {
      const resp = await fetch(`/v2/partials/quote-builder/${this.reqId}/save`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          lines: linePayload,
          quote_id: this.quoteId,
        }),
      });
      const data = await resp.json();
      if (resp.ok && data.ok) {
        this.quoteId = data.quote_id;
        this.quoteNumber = data.quote_number;
        this.saved = true;
        Alpine.store('toast').message = `Quote ${data.quote_number} saved`;
        Alpine.store('toast').type = 'success';
        Alpine.store('toast').show = true;
      } else {
        this.saveError = data.error || 'Save failed';
      }
    } catch (e) {
      this.saveError = 'Network error';
    }
    this.saving = false;
  },

  exportExcel() {
    if (!this.quoteId) return;
    window.location.href = `/v2/partials/quote-builder/${this.reqId}/export/excel?quote_id=${this.quoteId}`;
  },
  exportPdf() {
    if (!this.quoteId) return;
    window.location.href = `/v2/partials/quote-builder/${this.reqId}/export/pdf?quote_id=${this.quoteId}`;
  },

  handleKeydown(e) {
    if (['INPUT', 'TEXTAREA', 'SELECT'].includes(e.target.tagName)) {
      if (e.key === 'Enter' && e.target.matches('[x-ref=sellPriceInput]')) {
        e.preventDefault();
        this.confirmDecision();
      }
      return;
    }
    if (e.key === 'j' || e.key === 'ArrowDown') { e.preventDefault(); this.activeIdx = Math.min(this.activeIdx + 1, this.filteredLines.length - 1); }
    if (e.key === 'k' || e.key === 'ArrowUp') { e.preventDefault(); this.activeIdx = Math.max(this.activeIdx - 1, 0); }
    if (e.key === 'Tab' && !e.shiftKey) { e.preventDefault(); this.$refs.sellPriceInput?.focus(); }
    if (e.key >= '1' && e.key <= '9') {
      const idx = parseInt(e.key) - 1;
      if (this.activeLine?.offers[idx]) this.selectOffer(this.activeLine.offers[idx]);
    }
    if (e.key === 's') this.skipLine();
    if (e.key === 'f') {
      const keys = this.filterOptions.map(f => f.key);
      const cur = keys.indexOf(this.activeFilter);
      this.setFilter(keys[(cur + 1) % keys.length]);
    }
  },
}));
```

- [ ] **Step 2: Commit**

```bash
cd /root/availai && git add app/static/htmx_app.js && git commit -m "feat(quote-builder): add Alpine.js quoteBuilder component with keyboard nav"
```

---

### Task 9: CSS Animations

**Files:**
- Modify: `app/static/styles.css`

- [ ] **Step 1: Add CSS animations**

Append to `app/static/styles.css`:

```css
/* ── Quote Builder ─────────────────────────────────────────── */
@keyframes qbPanelSlide {
  from { opacity: 0; transform: translateX(8px); }
  to { opacity: 1; transform: translateX(0); }
}
.qb-panel-enter {
  animation: qbPanelSlide 0.2s ease-out;
}

@keyframes qbDecisionFlash {
  0% { background-color: rgba(16, 185, 129, 0.15); }
  100% { background-color: transparent; }
}
.qb-decision-flash {
  animation: qbDecisionFlash 0.8s ease-out;
}

.qb-list::-webkit-scrollbar { width: 4px; }
.qb-list::-webkit-scrollbar-track { background: transparent; }
.qb-list::-webkit-scrollbar-thumb { background: #b7c7d8; border-radius: 2px; }
```

- [ ] **Step 2: Commit**

```bash
cd /root/availai && git add app/static/styles.css && git commit -m "feat(quote-builder): add CSS animations for panel transitions and decision flash"
```

---

### Task 10: Parts Tab Entry Point

**Files:**
- Modify: `app/templates/htmx/partials/requisitions/tabs/parts.html`

- [ ] **Step 1: Add Build Quote button and checkboxes**

In `app/templates/htmx/partials/requisitions/tabs/parts.html`, add a "Build Quote" button before the requirements table (around line 159, before `{% if requirements %}`):

```html
  {# Build Quote action #}
  {% if requirements %}
  <div class="flex items-center justify-between mb-3">
    <p class="text-xs text-gray-400">{{ requirements|length }} requirement{{ "s" if requirements|length != 1 }}. Double-click any row to edit inline.</p>
    <button hx-get="/v2/partials/quote-builder/{{ req.id }}"
            hx-target="#quote-builder-content"
            hx-swap="innerHTML"
            hx-on::after-swap="$dispatch('open-quote-builder')"
            class="px-3 py-1.5 text-sm font-medium text-white bg-brand-500 rounded-lg hover:bg-brand-600 transition-colors flex items-center gap-1.5">
      <svg class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-6 9l2 2 4-4"/>
      </svg>
      Build Quote
    </button>
  </div>
  {% endif %}
```

Remove the existing line 161 (`<p class="text-xs text-gray-400 mb-2">...`) since the count is now in the new flex header.

- [ ] **Step 2: Commit**

```bash
cd /root/availai && git add app/templates/htmx/partials/requisitions/tabs/parts.html && git commit -m "feat(quote-builder): add Build Quote button to parts tab"
```

---

### Task 11: Vite Build + Full Test Suite

**Files:**
- All changed files

- [ ] **Step 1: Rebuild Vite bundle**

Run: `cd /root/availai && npm run build` (or `docker compose exec app npm run build` if running in Docker)

- [ ] **Step 2: Run full test suite**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v --tb=short`
Expected: All tests PASS including new `test_quote_builder.py`

- [ ] **Step 3: Fix any failures and re-run**

If any failures, fix and re-run until green.

- [ ] **Step 4: Final commit if any fixes**

```bash
cd /root/availai && git add -u && git commit -m "fix: resolve test failures from quote builder integration"
```

---

### Task 12: Integration Smoke Test

**Files:**
- None (manual testing)

- [ ] **Step 1: Start the app**

Run: `cd /root/availai && docker compose up -d --build`

- [ ] **Step 2: Verify the flow**

1. Navigate to a requisition with requirements → Parts tab
2. Click "Build Quote" → modal opens
3. Verify left panel shows requirements with status dots
4. Click a requirement → right panel shows customer specs + offers
5. Select an offer → sell price auto-fills, margin calculates
6. Click "Confirm & Next" → line turns green, advances
7. "Save Quote" → creates quote record
8. "Excel" → downloads .xlsx file
9. "TRIO PDF" → downloads PDF
10. Close modal → quote appears in Quotes tab

- [ ] **Step 3: Check logs for errors**

Run: `docker compose logs -f app | head -100`
