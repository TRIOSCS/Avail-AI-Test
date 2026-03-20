# Requirement & Offer Fields + Column Picker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose all hidden Requirement/Offer model fields in UI forms, add 3 new columns (customer_pn, need_by_date, spq), and wire up column picker on both tables.

**Architecture:** Single Alembic migration adds 5 columns. Schema updates expose missing fields. Router handlers accept new Form params. Templates get expanded form grids and `data-col-key` attributes. Column picker reuses existing `column_picker.html` component with localStorage + server persistence.

**Tech Stack:** FastAPI, SQLAlchemy 2.0, Alembic, Pydantic v2, Jinja2 + HTMX + Alpine.js, Tailwind CSS

**Spec:** `docs/superpowers/specs/2026-03-20-requirement-offer-fields-design.md`

---

## File Map

| Action | File | Responsibility |
|--------|------|----------------|
| Modify | `app/models/sourcing.py` | Add `customer_pn`, `need_by_date` to Requirement |
| Modify | `app/models/offers.py` | Add `spq` to Offer |
| Modify | `app/models/auth.py` | Add `requirements_column_prefs`, `offers_column_prefs` to User |
| Create | `alembic/versions/XXX_add_req_offer_fields.py` | Migration for all 5 columns |
| Modify | `app/schemas/requisitions.py` | Add fields to RequirementCreate/Update/Out + add `brand` |
| Modify | `app/schemas/crm.py` | Add `spq` to OfferCreate/Update, expand OfferOut |
| Modify | `app/routers/htmx_views.py` | Add Form params to handlers, column picker endpoints/constants |
| Modify | `app/templates/htmx/partials/requisitions/tabs/parts.html` | Expand add form, add column picker, add `data-col-key` |
| Modify | `app/templates/htmx/partials/requisitions/tabs/req_row.html` | Add new cells with `data-col-key`, expand inline edit |
| Modify | `app/templates/htmx/partials/requisitions/add_offer_form.html` | Expand to 17 fields |
| Modify | `app/templates/htmx/partials/requisitions/edit_offer_form.html` | Add missing fields |
| Modify | `app/templates/htmx/partials/requisitions/tabs/offers.html` | Add column picker, `data-col-key` on headers |
| Create | `tests/test_req_offer_fields.py` | All tests for new fields, schemas, column prefs |

---

### Task 1: Alembic Migration — Add 5 New Columns

**Files:**
- Modify: `app/models/sourcing.py:73-108` (Requirement class)
- Modify: `app/models/offers.py:24-114` (Offer class)
- Modify: `app/models/auth.py:12-50` (User class)
- Create: `alembic/versions/XXX_add_req_offer_fields.py`

- [ ] **Step 1: Add columns to models**

In `app/models/sourcing.py`, add after `sale_notes` (around line 93):
```python
customer_pn = Column(String(255))
need_by_date = Column(Date)
```

In `app/models/offers.py`, add after `moq` (around line 60):
```python
spq = Column(Integer)  # Standard Pack Quantity
```

In `app/models/auth.py`, add after `parts_column_prefs` (around line 36):
```python
requirements_column_prefs = Column(JSON, default=list)
offers_column_prefs = Column(JSON, default=list)
```

- [ ] **Step 2: Generate migration**

```bash
docker compose exec app alembic revision --autogenerate -m "add customer_pn, need_by_date, spq, column prefs"
```

- [ ] **Step 3: Review generated migration**

Verify it contains exactly 5 `add_column` ops and downgrade drops them in reverse. Remove any unintended changes.

- [ ] **Step 4: Test migration up/down**

```bash
docker compose exec app alembic upgrade head
docker compose exec app alembic downgrade -1
docker compose exec app alembic upgrade head
```

- [ ] **Step 5: Commit**

```bash
git add app/models/sourcing.py app/models/offers.py app/models/auth.py alembic/versions/*add_req_offer_fields*
git commit -m "feat: add customer_pn, need_by_date, spq, column pref columns"
```

---

### Task 2: Schema Updates — RequirementCreate/Update/Out

**Files:**
- Modify: `app/schemas/requisitions.py:96-191`
- Create: `tests/test_req_offer_fields.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_req_offer_fields.py`:
```python
"""Tests for requirement & offer field expansions.

Covers: RequirementCreate/Update/Out schema fields, OfferCreate/Update/Out schema fields,
        column preference endpoints.
Depends on: app/schemas/requisitions.py, app/schemas/crm.py
"""

import datetime

import pytest

from app.schemas.requisitions import RequirementCreate, RequirementOut, RequirementUpdate


class TestRequirementCreateSchema:
    def test_brand_accepted(self):
        r = RequirementCreate(primary_mpn="LM358DR", brand="Texas Instruments")
        assert r.brand == "Texas Instruments"

    def test_customer_pn_accepted(self):
        r = RequirementCreate(primary_mpn="LM358DR", customer_pn="CUST-001")
        assert r.customer_pn == "CUST-001"

    def test_need_by_date_accepted(self):
        d = datetime.date(2026, 4, 15)
        r = RequirementCreate(primary_mpn="LM358DR", need_by_date=d)
        assert r.need_by_date == d

    def test_all_new_fields_default_none(self):
        r = RequirementCreate(primary_mpn="LM358DR")
        assert r.brand is None
        assert r.customer_pn is None
        assert r.need_by_date is None


class TestRequirementUpdateSchema:
    def test_brand_update(self):
        r = RequirementUpdate(brand="Analog Devices")
        assert r.brand == "Analog Devices"

    def test_customer_pn_update(self):
        r = RequirementUpdate(customer_pn="CUST-002")
        assert r.customer_pn == "CUST-002"

    def test_need_by_date_update(self):
        d = datetime.date(2026, 5, 1)
        r = RequirementUpdate(need_by_date=d)
        assert r.need_by_date == d


class TestRequirementOutSchema:
    def test_includes_all_fields(self):
        data = {
            "id": 1,
            "primary_mpn": "LM358DR",
            "target_qty": 100,
            "target_price": 0.55,
            "substitutes": [],
            "sighting_count": 3,
            "brand": "TI",
            "customer_pn": "CUST-001",
            "need_by_date": datetime.date(2026, 4, 15),
            "condition": "new",
            "date_codes": "2025+",
            "firmware": None,
            "hardware_codes": None,
            "packaging": "Tape & Reel",
            "notes": "Urgent",
        }
        r = RequirementOut(**data)
        assert r.brand == "TI"
        assert r.customer_pn == "CUST-001"
        assert r.condition == "new"
        assert r.packaging == "Tape & Reel"
        assert r.notes == "Urgent"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_req_offer_fields.py::TestRequirementCreateSchema -v
```
Expected: FAIL — `brand` not a valid field

- [ ] **Step 3: Update schemas**

In `app/schemas/requisitions.py`:

Add `from datetime import date` to imports.

Add to `RequirementCreate` (after `notes` field, line 106):
```python
brand: str | None = None
customer_pn: str | None = None
need_by_date: date | None = None
```

Add to `RequirementUpdate` (after `sale_notes`, line 151):
```python
brand: str | None = None
customer_pn: str | None = None
need_by_date: date | None = None
```

Expand `RequirementOut` (lines 185-191):
```python
class RequirementOut(BaseModel):
    id: int
    primary_mpn: str
    target_qty: int = 1
    target_price: float | None = None
    substitutes: list[str] = Field(default_factory=list)
    sighting_count: int = 0
    brand: str | None = None
    customer_pn: str | None = None
    need_by_date: date | None = None
    condition: str | None = None
    date_codes: str | None = None
    firmware: str | None = None
    hardware_codes: str | None = None
    packaging: str | None = None
    notes: str | None = None
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_req_offer_fields.py -v
```
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add app/schemas/requisitions.py tests/test_req_offer_fields.py
git commit -m "feat: add brand, customer_pn, need_by_date to Requirement schemas"
```

---

### Task 3: Schema Updates — OfferCreate/Update/Out

**Files:**
- Modify: `app/schemas/crm.py:337-440`
- Modify: `tests/test_req_offer_fields.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_req_offer_fields.py`:
```python
from app.schemas.crm import OfferCreate, OfferOut, OfferUpdate


class TestOfferCreateSchema:
    def test_spq_accepted(self):
        o = OfferCreate(mpn="LM358DR", vendor_name="Acme", spq=100)
        assert o.spq == 100

    def test_spq_defaults_none(self):
        o = OfferCreate(mpn="LM358DR", vendor_name="Acme")
        assert o.spq is None

    def test_spq_rejects_zero(self):
        with pytest.raises(Exception):
            OfferCreate(mpn="LM358DR", vendor_name="Acme", spq=0)


class TestOfferUpdateSchema:
    def test_spq_update(self):
        o = OfferUpdate(spq=50)
        assert o.spq == 50

    def test_valid_until_update(self):
        o = OfferUpdate(valid_until=datetime.date(2026, 6, 1))
        assert o.valid_until == datetime.date(2026, 6, 1)


class TestOfferOutSchema:
    def test_includes_all_fields(self):
        data = {
            "id": 1,
            "vendor_name": "Acme",
            "mpn": "LM358DR",
            "manufacturer": "TI",
            "qty_available": 500,
            "unit_price": 0.45,
            "lead_time": "2-3 weeks",
            "date_code": "2025+",
            "condition": "new",
            "packaging": "Tape & Reel",
            "moq": 100,
            "spq": 50,
            "firmware": "v2.1",
            "hardware_code": "REV-B",
            "warranty": "1 year",
            "country_of_origin": "US",
            "notes": "In stock",
            "status": "active",
        }
        o = OfferOut(**data)
        assert o.spq == 50
        assert o.manufacturer == "TI"
        assert o.status == "active"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_req_offer_fields.py::TestOfferCreateSchema -v
```
Expected: FAIL — `spq` not valid

- [ ] **Step 3: Update schemas**

In `app/schemas/crm.py`:

Add `from datetime import date` to imports.

Add to `OfferCreate` after `moq` (line 351):
```python
spq: int | None = Field(default=None, ge=1)
```

Add to `OfferUpdate` after `moq` (around line 408):
```python
spq: int | None = None
valid_until: date | None = None
```

Expand `OfferOut` (lines 436-439):
```python
class OfferOut(BaseModel):
    id: int
    vendor_name: str
    mpn: str
    manufacturer: str | None = None
    qty_available: int | None = None
    unit_price: float | None = None
    lead_time: str | None = None
    date_code: str | None = None
    condition: str | None = None
    packaging: str | None = None
    firmware: str | None = None
    hardware_code: str | None = None
    moq: int | None = None
    spq: int | None = None
    warranty: str | None = None
    country_of_origin: str | None = None
    valid_until: date | None = None
    notes: str | None = None
    status: str | None = None
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_req_offer_fields.py -v
```
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add app/schemas/crm.py tests/test_req_offer_fields.py
git commit -m "feat: add spq to Offer schemas, expand OfferOut"
```

---

### Task 4: Router — Expand Requirement Add Handler

**Files:**
- Modify: `app/routers/htmx_views.py:549-586`

- [ ] **Step 1: Update handler signature**

Add all missing Form params to `add_requirement` (lines 549-558):

```python
@router.post("/v2/partials/requisitions/{req_id}/requirements", response_class=HTMLResponse)
async def add_requirement(
    request: Request,
    req_id: int,
    primary_mpn: str = Form(...),
    target_qty: int = Form(1),
    brand: str = Form(""),
    substitutes: str = Form(""),
    target_price: float | None = Form(None),
    condition: str = Form(""),
    date_codes: str = Form(""),
    firmware: str = Form(""),
    hardware_codes: str = Form(""),
    packaging: str = Form(""),
    notes: str = Form(""),
    customer_pn: str = Form(""),
    need_by_date: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
```

- [ ] **Step 2: Pass new fields to Requirement constructor**

Update the Requirement creation (lines 569-576):

```python
    from datetime import date as date_type

    r = Requirement(
        requisition_id=req_id,
        primary_mpn=primary_mpn,
        target_qty=target_qty,
        brand=brand or None,
        substitutes=sub_list,
        target_price=target_price,
        condition=condition or None,
        date_codes=date_codes or None,
        firmware=firmware or None,
        hardware_codes=hardware_codes or None,
        packaging=packaging or None,
        notes=notes or None,
        customer_pn=customer_pn or None,
        need_by_date=date_type.fromisoformat(need_by_date) if need_by_date else None,
        sourcing_status="open",
    )
```

- [ ] **Step 3: Run existing requisition tests to verify no regression**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_requisition_service.py tests/test_integration_requisitions.py -v --tb=short
```

- [ ] **Step 4: Commit**

```bash
git add app/routers/htmx_views.py
git commit -m "feat: expand add_requirement handler with all form fields"
```

---

### Task 5: Router — Expand Offer Add/Edit Handlers

**Files:**
- Modify: `app/routers/htmx_views.py:1353-1400` (add offer)
- Modify: `app/routers/htmx_views.py:1452-1502` (edit offer)

- [ ] **Step 1: Update add_offer handler**

Add missing fields to the Offer constructor (lines 1377-1395):

```python
    from datetime import date as date_type

    offer = Offer(
        requisition_id=req_id,
        vendor_name=vendor_name,
        vendor_name_normalized=normalize_vendor_name(vendor_name),
        mpn=mpn,
        normalized_mpn=normalize_mpn(mpn),
        qty_available=int(form["qty_available"]) if form.get("qty_available") else None,
        unit_price=float(form["unit_price"]) if form.get("unit_price") else None,
        lead_time=form.get("lead_time") or None,
        date_code=form.get("date_code") or None,
        condition=form.get("condition") or None,
        moq=int(form["moq"]) if form.get("moq") else None,
        manufacturer=form.get("manufacturer") or None,
        spq=int(form["spq"]) if form.get("spq") else None,
        packaging=form.get("packaging") or None,
        firmware=form.get("firmware") or None,
        hardware_code=form.get("hardware_code") or None,
        warranty=form.get("warranty") or None,
        country_of_origin=form.get("country_of_origin") or None,
        valid_until=date_type.fromisoformat(form["valid_until"]) if form.get("valid_until") else None,
        notes=form.get("notes") or None,
        requirement_id=int(form["requirement_id"]) if form.get("requirement_id") else None,
        source="manual",
        status="active",
        entered_by_id=user.id,
        created_at=datetime.now(timezone.utc),
    )
```

- [ ] **Step 2: Update edit offer handler trackable fields**

Find the edit offer handler (around line 1452). Expand `trackable`:
```python
trackable = [
    "vendor_name", "qty_available", "unit_price", "lead_time",
    "condition", "date_code", "moq", "notes",
    "manufacturer", "spq", "packaging", "firmware",
    "hardware_code", "warranty", "country_of_origin", "valid_until",
]
```

Add int conversion for `spq` and date conversion for `valid_until` in the update loop (follow `moq` pattern).

- [ ] **Step 3: Run existing offer tests**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_offers_overhaul.py tests/test_sprint2_offer_mgmt.py -v --tb=short
```

- [ ] **Step 4: Commit**

```bash
git add app/routers/htmx_views.py
git commit -m "feat: expand offer add/edit handlers with all fields"
```

---

### Task 6: Requirement Add Form Template

**Files:**
- Modify: `app/templates/htmx/partials/requisitions/tabs/parts.html`

- [ ] **Step 1: Read the current form template and expand it**

Expand from 5 fields to 11 + notes. Use `grid grid-cols-2 md:grid-cols-4 gap-3` layout:

- **Row 1**: MPN* (required), Qty, Brand, Target Price
- **Row 2**: Customer PN, Need-by Date (date input), Condition (select: New, New Surplus, ETN, Refurbished, Used, Pulls, As-Is), Packaging
- **Row 3**: Date Codes, Firmware, Hardware Codes, Substitutes
- **Full-width**: Notes (textarea, 2 rows)

Use existing styling classes:
- Labels: `block text-xs text-gray-500 mb-1`
- Inputs: `w-full px-2 py-1.5 text-sm border border-gray-300 rounded focus:ring-brand-500 focus:border-brand-500`

- [ ] **Step 2: Verify form renders**

```bash
docker compose up -d --build
```
Navigate to requisition detail → Parts tab → verify form.

- [ ] **Step 3: Commit**

```bash
git add app/templates/htmx/partials/requisitions/tabs/parts.html
git commit -m "feat: expand requirement add form to 11 fields + notes"
```

---

### Task 7: Offer Add/Edit Form Templates

**Files:**
- Modify: `app/templates/htmx/partials/requisitions/add_offer_form.html`
- Modify: `app/templates/htmx/partials/requisitions/edit_offer_form.html`

- [ ] **Step 1: Expand add_offer_form.html**

Add missing fields in grid layout:
- **Row 1**: Vendor Name*, MPN*, Qty Available, Unit Price
- **Row 2**: Manufacturer, Lead Time, Date Code, Condition (select)
- **Row 3**: MOQ, SPQ, Packaging, Firmware
- **Row 4**: Hardware Code, Warranty, Country of Origin, Valid Until (date)
- **Linked Requirement** (select, existing)
- **Full-width**: Notes (textarea, 2 rows)

- [ ] **Step 2: Expand edit_offer_form.html**

Add same fields pre-populated with `{{ offer.field_name }}` values.

- [ ] **Step 3: Verify both forms render**

```bash
docker compose up -d --build
```

- [ ] **Step 4: Commit**

```bash
git add app/templates/htmx/partials/requisitions/add_offer_form.html app/templates/htmx/partials/requisitions/edit_offer_form.html
git commit -m "feat: expand offer add/edit forms with all fields"
```

---

### Task 8: Requirements Table — Column Picker + data-col-key

**Files:**
- Modify: `app/templates/htmx/partials/requisitions/tabs/parts.html`
- Modify: `app/templates/htmx/partials/requisitions/tabs/req_row.html`
- Modify: `app/routers/htmx_views.py`

- [ ] **Step 1: Define column constants in router**

Add near existing `_ALL_PARTS_COLUMNS` (around line 7610):

```python
_ALL_REQ_COLUMNS = [
    ("mpn", "MPN"),
    ("brand", "Brand"),
    ("qty", "Qty"),
    ("target_price", "Target Price"),
    ("customer_pn", "Customer PN"),
    ("need_by_date", "Need-by Date"),
    ("condition", "Condition"),
    ("date_codes", "Date Codes"),
    ("firmware", "Firmware"),
    ("hardware_codes", "Hardware Codes"),
    ("packaging", "Packaging"),
    ("notes", "Notes"),
    ("substitutes", "Substitutes"),
    ("status", "Status"),
    ("sightings", "Sightings"),
]

_DEFAULT_REQ_COLUMNS = [
    "mpn", "brand", "qty", "target_price", "customer_pn",
    "need_by_date", "status", "sightings",
]
```

- [ ] **Step 2: Pass columns to template context**

In the requisition tab handler, when rendering "parts" tab, add:
```python
ctx["req_visible_cols"] = user.requirements_column_prefs or _DEFAULT_REQ_COLUMNS
ctx["req_all_columns"] = [{"key": k, "label": l, "default": k in _DEFAULT_REQ_COLUMNS} for k, l in _ALL_REQ_COLUMNS]
```

- [ ] **Step 3: Add column picker to parts.html and data-col-key to headers**

Include `column_picker.html` with `picker_id="requirements"` and `columns=req_all_columns`. Add `data-col-key` to each `<th>`.

- [ ] **Step 4: Add data-col-key to req_row.html + new cells**

Add `data-col-key` to each `<td>`. Add new cells for customer_pn, need_by_date, condition, date_codes, firmware, hardware_codes, packaging, notes, substitutes.

- [ ] **Step 5: Add column prefs save endpoint**

```python
@router.post("/v2/partials/requisitions/{req_id}/req-column-prefs", response_class=HTMLResponse)
async def save_req_column_prefs(
    request: Request,
    req_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    form = await request.form()
    cols = [c for c in form.getlist("columns") if c in dict(_ALL_REQ_COLUMNS)]
    if not cols:
        cols = _DEFAULT_REQ_COLUMNS
    user.requirements_column_prefs = cols
    db.commit()
    return await requisition_tab(request=request, req_id=req_id, tab="parts", user=user, db=db)
```

- [ ] **Step 6: Verify and commit**

```bash
docker compose up -d --build
git add app/routers/htmx_views.py app/templates/htmx/partials/requisitions/tabs/parts.html app/templates/htmx/partials/requisitions/tabs/req_row.html
git commit -m "feat: add column picker to requirements table"
```

---

### Task 9: Offers Table — Column Picker + data-col-key

**Files:**
- Modify: `app/templates/htmx/partials/requisitions/tabs/offers.html`
- Modify: `app/routers/htmx_views.py`

- [ ] **Step 1: Define offer column constants**

```python
_ALL_OFFER_COLUMNS = [
    ("vendor", "Vendor"),
    ("mpn", "MPN"),
    ("qty", "Qty"),
    ("price", "Price"),
    ("condition", "Condition"),
    ("date_code", "Date Code"),
    ("lead_time", "Lead Time"),
    ("manufacturer", "Manufacturer"),
    ("moq", "MOQ"),
    ("spq", "SPQ"),
    ("packaging", "Packaging"),
    ("firmware", "Firmware"),
    ("hardware_code", "Hardware Code"),
    ("warranty", "Warranty"),
    ("country", "Country"),
    ("valid_until", "Valid Until"),
    ("notes", "Notes"),
    ("status", "Status"),
]

_DEFAULT_OFFER_COLUMNS = [
    "vendor", "mpn", "qty", "price", "condition",
    "date_code", "lead_time", "status",
]
```

- [ ] **Step 2: Pass to template and add column picker**

Same pattern as Task 8 — pass `offer_visible_cols` and `offer_all_columns` to template, include `column_picker.html`, add `data-col-key` to all `<th>` and `<td>`.

- [ ] **Step 3: Add offer column prefs save endpoint**

```python
@router.post("/v2/partials/requisitions/{req_id}/offer-column-prefs", response_class=HTMLResponse)
async def save_offer_column_prefs(
    request: Request,
    req_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    form = await request.form()
    cols = [c for c in form.getlist("columns") if c in dict(_ALL_OFFER_COLUMNS)]
    if not cols:
        cols = _DEFAULT_OFFER_COLUMNS
    user.offers_column_prefs = cols
    db.commit()
    return await requisition_tab(request=request, req_id=req_id, tab="offers", user=user, db=db)
```

- [ ] **Step 4: Verify and commit**

```bash
docker compose up -d --build
git add app/routers/htmx_views.py app/templates/htmx/partials/requisitions/tabs/offers.html
git commit -m "feat: add column picker to offers table"
```

---

### Task 10: Inline Edit — Requirement Row

**Files:**
- Modify: `app/templates/htmx/partials/requisitions/tabs/req_row.html`

- [ ] **Step 1: Read the current inline edit template**

Understand how double-click toggles between display and input modes.

- [ ] **Step 2: Add editable inputs for new fields**

Add inline edit inputs for: customer_pn, need_by_date, condition (select), date_codes, firmware, hardware_codes, packaging, notes. Follow existing inline edit pattern.

- [ ] **Step 3: Verify and commit**

```bash
docker compose up -d --build
git add app/templates/htmx/partials/requisitions/tabs/req_row.html
git commit -m "feat: add inline edit for new requirement fields"
```

---

### Task 11: Column Prefs Tests + Full Suite

**Files:**
- Modify: `tests/test_req_offer_fields.py`

- [ ] **Step 1: Write column prefs tests**

Append to `tests/test_req_offer_fields.py`:
```python
from app.models import Requisition, User


class TestReqColumnPrefs:
    def test_save_req_column_prefs(self, client, db_session):
        req = Requisition(title="Test", status="open", created_by_id=1, created_at=datetime.datetime.now(datetime.timezone.utc))
        db_session.add(req)
        db_session.commit()
        resp = client.post(f"/v2/partials/requisitions/{req.id}/req-column-prefs", data={"columns": ["mpn", "qty", "status"]})
        assert resp.status_code == 200
        user = db_session.query(User).filter(User.id == 1).first()
        assert user.requirements_column_prefs == ["mpn", "qty", "status"]

    def test_invalid_column_filtered(self, client, db_session):
        req = Requisition(title="Test", status="open", created_by_id=1, created_at=datetime.datetime.now(datetime.timezone.utc))
        db_session.add(req)
        db_session.commit()
        resp = client.post(f"/v2/partials/requisitions/{req.id}/req-column-prefs", data={"columns": ["mpn", "INVALID"]})
        assert resp.status_code == 200
        user = db_session.query(User).filter(User.id == 1).first()
        assert "INVALID" not in (user.requirements_column_prefs or [])


class TestOfferColumnPrefs:
    def test_save_offer_column_prefs(self, client, db_session):
        req = Requisition(title="Test", status="open", created_by_id=1, created_at=datetime.datetime.now(datetime.timezone.utc))
        db_session.add(req)
        db_session.commit()
        resp = client.post(f"/v2/partials/requisitions/{req.id}/offer-column-prefs", data={"columns": ["vendor", "mpn", "price"]})
        assert resp.status_code == 200
        user = db_session.query(User).filter(User.id == 1).first()
        assert user.offers_column_prefs == ["vendor", "mpn", "price"]
```

- [ ] **Step 2: Run full suite**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v --tb=short
```
Expected: No new failures

- [ ] **Step 3: Commit**

```bash
git add tests/test_req_offer_fields.py
git commit -m "test: add column prefs tests for requirements and offers"
```

---

### Task 12: Final Verification & Deploy

- [ ] **Step 1: Run full test suite**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v --tb=short
```

- [ ] **Step 2: Deploy**

```bash
cd /root/availai && git push origin main && docker compose up -d --build
```

- [ ] **Step 3: Smoke test in browser**

1. Open requisition → Parts tab → verify expanded add form (11 fields + notes)
2. Fill all fields → submit → verify row shows data
3. Gear icon → toggle columns → verify hide/show
4. Offers tab → Add Offer → verify 17 fields + notes
5. Add offer with all fields → verify stored
6. Gear icon on offers → toggle columns
7. Double-click requirement → verify inline edit shows new fields
