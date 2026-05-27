# Excess Resell Phase 3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Transform the Excess tab from basic CRUD into a working resell workflow with import preview, demand matching against active requisitions, and bid recording/comparison.

**Architecture:** Three independent sub-projects. SP1 adds two-step import + auto-matching (creates Offers for active requirements). SP2 adds bid CRUD via modal UI. SP3 adds sort/filter/bulk-delete polish. SP3 has no dependencies and can run in parallel. SP1→SP2 is sequential.

**Tech Stack:** FastAPI, SQLAlchemy 2.0, Jinja2, HTMX 2.x, Alpine.js 3.x, Tailwind CSS, pytest

**Spec:** `docs/superpowers/specs/2026-03-20-excess-resell-phase3-design.md`

---

### File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `app/services/excess_service.py` | Modify | Add `preview_import()`, `confirm_import()`, `match_excess_demand()`, `create_bid()`, `list_bids()`, `accept_bid()`, `reject_bid()`, `bulk_delete_line_items()` |
| `app/routers/excess.py` | Modify | Add 6 API endpoints + 3 HTMX partial endpoints |
| `app/models/excess.py` | Modify | Add `demand_match_count` column to ExcessLineItem |
| `alembic/versions/xxx_add_demand_match_count.py` | Create | Migration for new column |
| `app/templates/htmx/partials/excess/import_preview.html` | Create | Read-only preview table with summary bar |
| `app/templates/htmx/partials/excess/bid_list.html` | Create | Modal content: sorted bid list with accept/reject |
| `app/templates/htmx/partials/excess/bid_form.html` | Create | Modal form: record a new bid |
| `app/templates/htmx/partials/excess/detail.html` | Modify | Add match badges, bid counts, bulk delete checkboxes |
| `app/templates/htmx/partials/excess/list.html` | Modify | Add sortable headers, owner filter |
| `app/templates/htmx/partials/excess/line_item_row.html` | Modify | Add match badge, bid count link, checkbox |
| `tests/test_excess_crud.py` | Modify | Add tests for preview, confirm, matching, bids, bulk delete, sort, filter |

---

## Pre-requisite: Test Fixtures

The existing `tests/test_excess_crud.py` uses helper functions `_make_company()` and `_make_user()` but lacks pytest fixtures. All new test classes use `company` and `trader` fixtures. Before starting Task 1, add these fixtures to `tests/test_excess_crud.py` (after the helpers, before the first test class):

```python
@pytest.fixture()
def company(db_session: Session) -> Company:
    return _make_company(db_session)


@pytest.fixture()
def trader(db_session: Session) -> User:
    return _make_user(db_session)
```

These fixtures already exist in the file if Phase 2 added them. Check first — only add if missing.

---

## SP1: Import Preview + Demand Matching

### Task 1: Migration — Add demand_match_count to ExcessLineItem

**Files:**
- Modify: `app/models/excess.py`
- Create: `alembic/versions/xxx_add_demand_match_count.py`

- [ ] **Step 1: Add column to model**

In `app/models/excess.py`, add to `ExcessLineItem` class (after `demand_score` line ~75):

```python
demand_match_count = Column(Integer, default=0)
```

- [ ] **Step 2: Generate migration**

Run inside Docker:
```bash
docker compose exec app alembic revision --autogenerate -m "add demand_match_count to excess_line_items"
```

- [ ] **Step 3: Review the generated migration**

Open the new file in `alembic/versions/`. Verify it contains:
- `op.add_column('excess_line_items', sa.Column('demand_match_count', sa.Integer(), nullable=True))`
- Downgrade drops the column

- [ ] **Step 4: Test migration round-trip**

```bash
docker compose exec app alembic upgrade head
docker compose exec app alembic downgrade -1
docker compose exec app alembic upgrade head
```

- [ ] **Step 5: Commit**

```bash
git add app/models/excess.py alembic/versions/*demand_match_count*
git commit -m "migration: add demand_match_count to excess_line_items"
```

---

### Task 2: Service — preview_import() and confirm_import()

**Files:**
- Modify: `app/services/excess_service.py`
- Modify: `tests/test_excess_crud.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_excess_crud.py`:

```python
from app.services.excess_service import preview_import, confirm_import


class TestPreviewImport:
    def test_parses_valid_rows(self, db_session, company, trader):
        el = create_excess_list(db_session, title="Preview", company_id=company.id, owner_id=trader.id)
        rows = [
            {"part_number": "LM358N", "quantity": "500", "asking_price": "0.45"},
            {"mpn": "NE555P", "qty": "1000", "manufacturer": "TI"},
        ]
        result = preview_import(rows)
        assert result["valid_count"] == 2
        assert result["error_count"] == 0
        assert len(result["preview_rows"]) == 2
        assert result["preview_rows"][0]["part_number"] == "LM358N"

    def test_flags_invalid_rows(self):
        rows = [
            {"part_number": "", "quantity": "500"},
            {"part_number": "LM358N", "quantity": "abc"},
            {"part_number": "NE555P", "quantity": "100"},
        ]
        result = preview_import(rows)
        assert result["valid_count"] == 1
        assert result["error_count"] == 2
        assert len(result["errors"]) == 2
        assert "Row 1" in result["errors"][0]

    def test_detects_column_mapping(self):
        rows = [{"mpn": "LM358N", "qty": "100", "cost": "0.50"}]
        result = preview_import(rows)
        mapping = result["column_mapping"]
        assert mapping["mpn"] == "part_number"
        assert mapping["qty"] == "quantity"
        assert mapping["cost"] == "asking_price"

    def test_limits_preview_to_10_rows(self):
        rows = [{"part_number": f"PART{i}", "quantity": "1"} for i in range(25)]
        result = preview_import(rows)
        assert len(result["preview_rows"]) == 10
        assert result["valid_count"] == 25


class TestConfirmImport:
    def test_imports_validated_rows(self, db_session, company, trader):
        el = create_excess_list(db_session, title="Confirm", company_id=company.id, owner_id=trader.id)
        validated_rows = [
            {"part_number": "LM358N", "quantity": 500, "asking_price": 0.45},
            {"part_number": "NE555P", "quantity": 1000, "manufacturer": "TI"},
        ]
        result = confirm_import(db_session, el.id, validated_rows)
        assert result["imported"] == 2
        db_session.refresh(el)
        assert el.total_line_items == 2

    def test_rejects_empty_rows(self, db_session, company, trader):
        el = create_excess_list(db_session, title="Empty", company_id=company.id, owner_id=trader.id)
        result = confirm_import(db_session, el.id, [])
        assert result["imported"] == 0
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_excess_crud.py::TestPreviewImport tests/test_excess_crud.py::TestConfirmImport -v
```
Expected: FAIL — `preview_import` and `confirm_import` not defined

- [ ] **Step 3: Implement preview_import()**

Add to `app/services/excess_service.py`:

```python
def preview_import(rows: list[dict]) -> dict:
    """Parse rows and return a preview with validation results.

    Does NOT touch the database — pure validation.
    Returns {valid_count, error_count, errors, preview_rows, column_mapping}.
    """
    valid_rows: list[dict] = []
    errors: list[str] = []
    column_mapping: dict[str, str] = {}

    for i, raw_row in enumerate(rows, start=1):
        # Track which headers map to which fields
        for key in raw_row:
            canonical = _HEADER_MAP.get(key.strip().lower().replace(" ", "_"))
            if canonical and key not in column_mapping:
                column_mapping[key] = canonical

        row = _normalize_row(raw_row)
        part_number = (row.get("part_number") or "").strip()
        if not part_number:
            errors.append(f"Row {i}: blank part_number — will be skipped")
            continue

        quantity = _parse_quantity(row.get("quantity"))
        if quantity is None:
            errors.append(f"Row {i}: invalid quantity — will be skipped")
            continue

        asking_price = _parse_price(row.get("asking_price"))
        manufacturer = (row.get("manufacturer") or "").strip() or None
        date_code = (row.get("date_code") or "").strip() or None
        condition = (row.get("condition") or "").strip() or "New"

        valid_rows.append({
            "part_number": part_number,
            "manufacturer": manufacturer,
            "quantity": quantity,
            "date_code": date_code,
            "condition": condition,
            "asking_price": float(asking_price) if asking_price is not None else None,
        })

    return {
        "valid_count": len(valid_rows),
        "error_count": len(errors),
        "errors": errors,
        "preview_rows": valid_rows[:10],
        "all_valid_rows": valid_rows,
        "column_mapping": column_mapping,
    }
```

- [ ] **Step 4: Implement confirm_import()**

Add to `app/services/excess_service.py`:

```python
def confirm_import(db: Session, list_id: int, validated_rows: list[dict]) -> dict:
    """Import pre-validated rows into an excess list.

    Accepts output from preview_import() — rows are already parsed and validated.
    Returns {imported: int}.
    """
    excess_list = get_excess_list(db, list_id)
    imported = 0

    for row in validated_rows:
        item = ExcessLineItem(
            excess_list_id=list_id,
            part_number=row["part_number"],
            manufacturer=row.get("manufacturer"),
            quantity=row["quantity"],
            date_code=row.get("date_code"),
            condition=row.get("condition", "New"),
            asking_price=row.get("asking_price"),
        )
        db.add(item)
        imported += 1

    if imported > 0:
        excess_list.total_line_items = (excess_list.total_line_items or 0) + imported
        _safe_commit(db, entity="excess line items")

    logger.info("Confirmed import of {} items into ExcessList id={}", imported, list_id)
    return {"imported": imported}
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_excess_crud.py::TestPreviewImport tests/test_excess_crud.py::TestConfirmImport -v
```
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add app/services/excess_service.py tests/test_excess_crud.py
git commit -m "feat(excess): add preview_import and confirm_import service functions"
```

---

### Task 3: Service — match_excess_demand()

**Files:**
- Modify: `app/services/excess_service.py`
- Modify: `tests/test_excess_crud.py`

**Context:** This function queries active requirements by normalized MPN and creates Offer records when excess parts match open demand. The Offer model has `requisition_id` (NOT NULL), `requirement_id`, `mpn`, `normalized_mpn`, `vendor_name`, `source`, `unit_price`, `qty_available`.

Requisition active statuses: `"active"`, `"open"`, `"sourcing"`.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_excess_crud.py`:

```python
from app.models.sourcing import Requirement, Requisition
from app.models.offers import Offer
from app.services.excess_service import match_excess_demand


class TestMatchExcessDemand:
    @pytest.fixture()
    def active_req(self, db_session, company, trader):
        """Create an active requisition with a requirement for LM358N."""
        req = Requisition(name="Test RFQ", status="active", created_by=trader.id, company_id=company.id)
        db_session.add(req)
        db_session.flush()
        from app.utils.normalization import normalize_mpn_key
        requirement = Requirement(
            requisition_id=req.id,
            primary_mpn="LM358N",
            normalized_mpn=normalize_mpn_key("LM358N"),
            target_qty=100,
        )
        db_session.add(requirement)
        db_session.commit()
        return req, requirement

    def test_creates_offer_on_match(self, db_session, company, trader, active_req):
        req, requirement = active_req
        el = create_excess_list(db_session, title="Match Test", company_id=company.id, owner_id=trader.id)
        from app.services.excess_service import confirm_import
        confirm_import(db_session, el.id, [
            {"part_number": "LM358N", "quantity": 500, "asking_price": 0.45},
        ])
        result = match_excess_demand(db_session, el.id, user_id=trader.id)
        assert result["matches_created"] >= 1
        offer = db_session.query(Offer).filter(Offer.source == "excess", Offer.requisition_id == req.id).first()
        assert offer is not None
        assert offer.mpn == "LM358N"
        assert float(offer.unit_price) == 0.45
        assert offer.vendor_name == company.name

    def test_updates_demand_match_count(self, db_session, company, trader, active_req):
        el = create_excess_list(db_session, title="Count Test", company_id=company.id, owner_id=trader.id)
        from app.services.excess_service import confirm_import
        confirm_import(db_session, el.id, [
            {"part_number": "LM358N", "quantity": 500},
        ])
        match_excess_demand(db_session, el.id, user_id=trader.id)
        item = db_session.query(ExcessLineItem).filter_by(excess_list_id=el.id).first()
        assert item.demand_match_count >= 1

    def test_no_match_for_unrelated_part(self, db_session, company, trader, active_req):
        el = create_excess_list(db_session, title="No Match", company_id=company.id, owner_id=trader.id)
        from app.services.excess_service import confirm_import
        confirm_import(db_session, el.id, [
            {"part_number": "XXXXXX", "quantity": 100},
        ])
        result = match_excess_demand(db_session, el.id, user_id=trader.id)
        assert result["matches_created"] == 0

    def test_skips_archived_requisitions(self, db_session, company, trader):
        """Archived reqs should NOT produce matches."""
        req = Requisition(name="Old RFQ", status="archived", created_by=trader.id, company_id=company.id)
        db_session.add(req)
        db_session.flush()
        from app.utils.normalization import normalize_mpn_key
        requirement = Requirement(
            requisition_id=req.id,
            primary_mpn="LM358N",
            normalized_mpn=normalize_mpn_key("LM358N"),
            target_qty=100,
        )
        db_session.add(requirement)
        db_session.commit()

        el = create_excess_list(db_session, title="Archived", company_id=company.id, owner_id=trader.id)
        from app.services.excess_service import confirm_import
        confirm_import(db_session, el.id, [{"part_number": "LM358N", "quantity": 500}])
        result = match_excess_demand(db_session, el.id, user_id=trader.id)
        assert result["matches_created"] == 0
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_excess_crud.py::TestMatchExcessDemand -v
```
Expected: FAIL — `match_excess_demand` not defined

- [ ] **Step 3: Implement match_excess_demand()**

Add to `app/services/excess_service.py`:

```python
from ..models.offers import Offer
from ..models.sourcing import Requirement, Requisition
from ..utils.normalization import normalize_mpn_key

_ACTIVE_REQ_STATUSES = {"active", "open", "sourcing"}


def match_excess_demand(db: Session, list_id: int, *, user_id: int) -> dict:
    """Match excess line items against active requirements and create Offers.

    For each excess line item, finds requirements with matching normalized_mpn
    on active requisitions. Creates one Offer per match.

    Returns {matches_created: int, items_matched: int}.
    """
    excess_list = get_excess_list(db, list_id)
    line_items = db.query(ExcessLineItem).filter_by(excess_list_id=list_id).all()

    matches_created = 0
    items_matched = 0

    for item in line_items:
        norm_key = normalize_mpn_key(item.part_number)
        if not norm_key:
            continue

        # Find active requirements with matching normalized MPN
        requirements = (
            db.query(Requirement)
            .join(Requisition, Requirement.requisition_id == Requisition.id)
            .filter(
                Requirement.normalized_mpn == norm_key,
                Requisition.status.in_(_ACTIVE_REQ_STATUSES),
            )
            .all()
        )

        if not requirements:
            continue

        item_matches = 0
        for req in requirements:
            # Check if this exact offer already exists (avoid duplicates)
            existing = (
                db.query(Offer)
                .filter(
                    Offer.source == "excess",
                    Offer.requisition_id == req.requisition_id,
                    Offer.normalized_mpn == norm_key,
                    Offer.vendor_name == excess_list.company.name,
                )
                .first()
            )
            if existing:
                continue

            offer = Offer(
                requisition_id=req.requisition_id,
                requirement_id=req.id,
                vendor_name=excess_list.company.name if excess_list.company else "Unknown",
                mpn=item.part_number,
                normalized_mpn=norm_key,
                manufacturer=item.manufacturer,
                qty_available=item.quantity,
                unit_price=item.asking_price,
                source="excess",
                condition=item.condition,
                date_code=item.date_code,
                entered_by_id=user_id,
                notes=f"Auto-matched from excess list: {excess_list.title}",
            )
            db.add(offer)
            item_matches += 1
            matches_created += 1

        if item_matches > 0:
            item.demand_match_count = (item.demand_match_count or 0) + item_matches
            items_matched += 1

    if matches_created > 0:
        _safe_commit(db, entity="excess demand matches")

    logger.info(
        "Demand matching for ExcessList id={}: {} matches across {} items",
        list_id, matches_created, items_matched,
    )
    return {"matches_created": matches_created, "items_matched": items_matched}
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_excess_crud.py::TestMatchExcessDemand -v
```
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add app/services/excess_service.py tests/test_excess_crud.py
git commit -m "feat(excess): demand matching — create Offers for active requirements"
```

---

### Task 4: API — Preview, Confirm, and Match Endpoints

**Files:**
- Modify: `app/routers/excess.py`
- Modify: `tests/test_excess_crud.py`

- [ ] **Step 1: Write failing API tests**

Append to `tests/test_excess_crud.py`:

```python
from fastapi.testclient import TestClient
from app.main import app as fastapi_app
from app.database import get_db
from app.dependencies import require_user


def _api_client(db_session, user):
    """Create a TestClient with auth + DB overrides."""
    fastapi_app.dependency_overrides[get_db] = lambda: (yield db_session)
    fastapi_app.dependency_overrides[require_user] = lambda: user
    client = TestClient(fastapi_app)
    yield client
    fastapi_app.dependency_overrides.clear()


class TestPreviewImportAPI:
    def test_preview_csv(self, db_session, company, trader):
        el = create_excess_list(db_session, title="API Preview", company_id=company.id, owner_id=trader.id)
        csv_content = "part_number,quantity,asking_price\nLM358N,500,0.45\nNE555P,1000,0.22\n"
        for client in _api_client(db_session, trader):
            resp = client.post(
                f"/api/excess-lists/{el.id}/preview-import",
                files={"file": ("test.csv", csv_content.encode(), "text/csv")},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["valid_count"] == 2
            assert data["error_count"] == 0


class TestConfirmImportAPI:
    def test_confirm_rows(self, db_session, company, trader):
        el = create_excess_list(db_session, title="API Confirm", company_id=company.id, owner_id=trader.id)
        payload = {
            "rows": [
                {"part_number": "LM358N", "quantity": 500, "asking_price": 0.45},
                {"part_number": "NE555P", "quantity": 1000},
            ]
        }
        for client in _api_client(db_session, trader):
            resp = client.post(f"/api/excess-lists/{el.id}/confirm-import", json=payload)
            assert resp.status_code == 200
            assert resp.json()["imported"] == 2
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_excess_crud.py::TestPreviewImportAPI tests/test_excess_crud.py::TestConfirmImportAPI -v
```
Expected: FAIL — 404 (routes not found)

- [ ] **Step 3: Add endpoints to router**

Add to `app/routers/excess.py` in the HTMX Partials section:

```python
from ..services.excess_service import (
    # ... existing imports ...
    preview_import,
    confirm_import,
    match_excess_demand,
)


@router.post("/api/excess-lists/{list_id}/preview-import")
async def api_preview_import(
    list_id: int,
    file: UploadFile,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Parse a file and return a preview without importing."""
    get_excess_list(db, list_id)  # verify exists
    filename = file.filename or ""
    ext = ""
    if "." in filename:
        ext = "." + filename.rsplit(".", 1)[-1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(400, f"Unsupported file type '{ext}'")
    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(400, "File too large")
    rows = parse_tabular_file(content, filename)
    if not rows:
        raise HTTPException(400, "No data rows found in file")
    result = preview_import(rows)
    result["list_id"] = list_id
    result["filename"] = filename
    return result


@router.post("/api/excess-lists/{list_id}/confirm-import")
async def api_confirm_import(
    list_id: int,
    payload: dict,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Import pre-validated rows and trigger demand matching."""
    rows = payload.get("rows", [])
    if not rows:
        raise HTTPException(400, "No rows to import")
    result = confirm_import(db, list_id, rows)
    # Auto-trigger demand matching
    match_result = match_excess_demand(db, list_id, user_id=user.id)
    result["matches_created"] = match_result["matches_created"]
    return result
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_excess_crud.py::TestPreviewImportAPI tests/test_excess_crud.py::TestConfirmImportAPI -v
```
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add app/routers/excess.py tests/test_excess_crud.py
git commit -m "feat(excess): preview-import and confirm-import API endpoints"
```

---

### Task 5: Frontend — Import Preview Template

**Files:**
- Create: `app/templates/htmx/partials/excess/import_preview.html`
- Modify: `app/routers/excess.py` (add HTMX partial endpoint)
- Modify: `app/templates/htmx/partials/excess/detail.html` (wire upload to preview)

- [ ] **Step 1: Create import_preview.html**

Create `app/templates/htmx/partials/excess/import_preview.html`:

Template receives: `list_id`, `filename`, `valid_count`, `error_count`, `errors`, `preview_rows`, `column_mapping`, `all_valid_rows_json` (JSON string of all validated rows for the confirm POST).

Content:
- Summary bar: green badge "X valid", red badge "Y errors" (if any)
- Static mapping summary: show each detected mapping as `"original_header → canonical_field"` pills
- Compact table showing `preview_rows` (max 10): Part Number, Manufacturer, Qty, Condition, Date Code, Price
- Error list: each error as a red text line
- "Import X rows" green button: `hx-post="/api/excess-lists/{{ list_id }}/confirm-import"` with `hx-ext="json-enc"`, sends `{rows: all_valid_rows}` as JSON. On success, refreshes the detail view via `hx-get="/v2/partials/excess/{{ list_id }}"` targeting `#main-content`.
- "Cancel" gray button: refreshes back to detail view

- [ ] **Step 2: Add HTMX partial endpoint for preview**

Add to `app/routers/excess.py`:

```python
import json

@router.post("/v2/partials/excess/{list_id}/import-preview", response_class=HTMLResponse)
async def partial_import_preview(
    request: Request,
    list_id: int,
    file: UploadFile,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Parse file and render the import preview template."""
    get_excess_list(db, list_id)
    filename = file.filename or ""
    ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(400, f"Unsupported file type '{ext}'")
    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(400, "File too large")
    rows = parse_tabular_file(content, filename)
    if not rows:
        raise HTTPException(400, "No data rows found")
    result = preview_import(rows)
    return templates.TemplateResponse("htmx/partials/excess/import_preview.html", {
        "request": request,
        "list_id": list_id,
        "filename": filename,
        **result,
        "all_valid_rows_json": json.dumps(result["all_valid_rows"]),
    })
```

- [ ] **Step 3: Update detail.html upload form to target preview**

In `app/templates/htmx/partials/excess/detail.html`, change the file upload form from:
```
hx-post="/api/excess-lists/{{ list.id }}/import"
```
to:
```
hx-post="/v2/partials/excess/{{ list.id }}/import-preview"
hx-target="#import-area"
```

And wrap the upload zone + preview area in a `<div id="import-area">` container.

- [ ] **Step 4: Test manually**

Navigate to an excess list detail, upload a CSV. Verify:
- Preview renders with correct row counts
- Column mapping shows
- Errors highlighted
- "Import" button works and returns to detail with line items

- [ ] **Step 5: Commit**

```bash
git add app/templates/htmx/partials/excess/import_preview.html app/routers/excess.py app/templates/htmx/partials/excess/detail.html
git commit -m "feat(excess): import preview UI with two-step upload"
```

---

### Task 6: Frontend — Demand Match Badges on Detail

**Files:**
- Modify: `app/templates/htmx/partials/excess/line_item_row.html`
- Modify: `app/templates/htmx/partials/excess/detail.html`

- [ ] **Step 1: Add match badge to line_item_row.html**

In the line item row, after the status badge column, add a "Matches" column. If `item.demand_match_count > 0`, show a badge like:

```html
{% if item.demand_match_count %}
<span class="inline-flex items-center gap-1 px-2 py-0.5 text-xs font-medium rounded-full bg-brand-100 text-brand-700"
      title="Matched against {{ item.demand_match_count }} active requirement(s). Original MPN: {{ item.part_number }}">
  <svg class="h-3 w-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M13 10V3L4 14h7v7l9-11h-7z"/></svg>
  {{ item.demand_match_count }}
</span>
{% else %}
<span class="text-xs text-gray-400">&mdash;</span>
{% endif %}
```

- [ ] **Step 2: Add "Matches" column header to detail.html table**

Add `<th>Matches</th>` after the Status column header.

- [ ] **Step 3: Commit**

```bash
git add app/templates/htmx/partials/excess/line_item_row.html app/templates/htmx/partials/excess/detail.html
git commit -m "feat(excess): show demand match count badges on line items"
```

---

## SP2: Bid Recording

### Task 7: Service — create_bid(), list_bids(), accept_bid()

**Files:**
- Modify: `app/services/excess_service.py`
- Modify: `tests/test_excess_crud.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_excess_crud.py`:

```python
from app.models.excess import Bid
from app.services.excess_service import create_bid, list_bids, accept_bid


class TestCreateBid:
    def test_creates_with_required_fields(self, db_session, company, trader):
        el = create_excess_list(db_session, title="Bid Test", company_id=company.id, owner_id=trader.id)
        from app.services.excess_service import confirm_import
        confirm_import(db_session, el.id, [{"part_number": "LM358N", "quantity": 500}])
        item = db_session.query(ExcessLineItem).filter_by(excess_list_id=el.id).first()

        buyer = _make_company(db_session, "Buyer Inc")
        bid = create_bid(
            db_session,
            line_item_id=item.id,
            list_id=el.id,
            unit_price=0.35,
            quantity_wanted=200,
            bidder_company_id=buyer.id,
            user_id=trader.id,
        )
        assert bid.id is not None
        assert float(bid.unit_price) == 0.35
        assert bid.status == "pending"

    def test_rejects_invalid_line_item(self, db_session, company, trader):
        el = create_excess_list(db_session, title="Bad Bid", company_id=company.id, owner_id=trader.id)
        with pytest.raises(HTTPException) as exc:
            create_bid(db_session, line_item_id=99999, list_id=el.id, unit_price=1.0, quantity_wanted=1, user_id=trader.id)
        assert exc.value.status_code == 404


class TestListBids:
    def test_returns_sorted_by_price(self, db_session, company, trader):
        el = create_excess_list(db_session, title="Sort Test", company_id=company.id, owner_id=trader.id)
        from app.services.excess_service import confirm_import
        confirm_import(db_session, el.id, [{"part_number": "LM358N", "quantity": 500}])
        item = db_session.query(ExcessLineItem).filter_by(excess_list_id=el.id).first()

        buyer1 = _make_company(db_session, "Cheap Buyer")
        buyer2 = _make_company(db_session, "Expensive Buyer")
        create_bid(db_session, line_item_id=item.id, list_id=el.id, unit_price=0.50, quantity_wanted=100, bidder_company_id=buyer2.id, user_id=trader.id)
        create_bid(db_session, line_item_id=item.id, list_id=el.id, unit_price=0.25, quantity_wanted=200, bidder_company_id=buyer1.id, user_id=trader.id)

        bids = list_bids(db_session, item.id, el.id)
        assert len(bids) == 2
        assert float(bids[0].unit_price) == 0.25  # cheapest first


class TestAcceptBid:
    def test_accepts_and_rejects_others(self, db_session, company, trader):
        el = create_excess_list(db_session, title="Accept Test", company_id=company.id, owner_id=trader.id)
        from app.services.excess_service import confirm_import
        confirm_import(db_session, el.id, [{"part_number": "LM358N", "quantity": 500}])
        item = db_session.query(ExcessLineItem).filter_by(excess_list_id=el.id).first()

        buyer1 = _make_company(db_session, "Winner")
        buyer2 = _make_company(db_session, "Loser")
        bid1 = create_bid(db_session, line_item_id=item.id, list_id=el.id, unit_price=0.25, quantity_wanted=200, bidder_company_id=buyer1.id, user_id=trader.id)
        bid2 = create_bid(db_session, line_item_id=item.id, list_id=el.id, unit_price=0.50, quantity_wanted=100, bidder_company_id=buyer2.id, user_id=trader.id)

        accept_bid(db_session, bid1.id, item.id, el.id)
        db_session.refresh(bid1)
        db_session.refresh(bid2)
        db_session.refresh(item)

        assert bid1.status == "accepted"
        assert bid2.status == "rejected"
        assert item.status == "awarded"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_excess_crud.py::TestCreateBid tests/test_excess_crud.py::TestListBids tests/test_excess_crud.py::TestAcceptBid -v
```
Expected: FAIL — functions not defined

- [ ] **Step 3: Implement bid service functions**

Add to `app/services/excess_service.py`:

```python
from ..models.excess import Bid


def create_bid(
    db: Session,
    *,
    line_item_id: int,
    list_id: int,
    unit_price: float,
    quantity_wanted: int,
    user_id: int,
    bidder_company_id: int | None = None,
    bidder_vendor_card_id: int | None = None,
    lead_time_days: int | None = None,
    source: str = "manual",
    notes: str | None = None,
) -> Bid:
    """Record a bid on an excess line item."""
    excess_list = get_excess_list(db, list_id)
    item = db.get(ExcessLineItem, line_item_id)
    if not item or item.excess_list_id != list_id:
        raise HTTPException(404, f"Line item {line_item_id} not found in list {list_id}")

    bid = Bid(
        excess_line_item_id=line_item_id,
        bidder_company_id=bidder_company_id,
        bidder_vendor_card_id=bidder_vendor_card_id,
        unit_price=unit_price,
        quantity_wanted=quantity_wanted,
        lead_time_days=lead_time_days,
        source=source,
        notes=notes,
        created_by=user_id,
    )
    db.add(bid)
    _safe_commit(db, entity="bid")
    db.refresh(bid)
    logger.info("Created Bid id={} on LineItem id={}", bid.id, line_item_id)
    return bid


def list_bids(db: Session, line_item_id: int, list_id: int) -> list[Bid]:
    """List bids for a line item, sorted by unit_price ascending (best first)."""
    get_excess_list(db, list_id)
    item = db.get(ExcessLineItem, line_item_id)
    if not item or item.excess_list_id != list_id:
        raise HTTPException(404, f"Line item {line_item_id} not found in list {list_id}")
    return (
        db.query(Bid)
        .filter(Bid.excess_line_item_id == line_item_id)
        .order_by(Bid.unit_price.asc())
        .all()
    )


def accept_bid(db: Session, bid_id: int, line_item_id: int, list_id: int) -> Bid:
    """Accept a bid: set to accepted, reject all other pending bids, set line item to awarded."""
    get_excess_list(db, list_id)
    bid = db.get(Bid, bid_id)
    if not bid or bid.excess_line_item_id != line_item_id:
        raise HTTPException(404, f"Bid {bid_id} not found for line item {line_item_id}")

    bid.status = "accepted"

    # Reject all other pending bids on this line item
    other_bids = (
        db.query(Bid)
        .filter(Bid.excess_line_item_id == line_item_id, Bid.id != bid_id, Bid.status == "pending")
        .all()
    )
    for other in other_bids:
        other.status = "rejected"

    # Update line item status
    item = db.get(ExcessLineItem, line_item_id)
    if item:
        item.status = "awarded"

    _safe_commit(db, entity="bid acceptance")
    db.refresh(bid)
    logger.info("Accepted Bid id={}, rejected {} others", bid_id, len(other_bids))
    return bid
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_excess_crud.py::TestCreateBid tests/test_excess_crud.py::TestListBids tests/test_excess_crud.py::TestAcceptBid -v
```
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add app/services/excess_service.py tests/test_excess_crud.py
git commit -m "feat(excess): bid CRUD — create, list (sorted), accept with cascade"
```

---

### Task 8: API — Bid Endpoints

**Files:**
- Modify: `app/routers/excess.py`
- Modify: `tests/test_excess_crud.py`

- [ ] **Step 1: Write failing API tests**

Append to `tests/test_excess_crud.py`:

```python
class TestBidAPI:
    def test_create_bid_via_api(self, db_session, company, trader):
        el = create_excess_list(db_session, title="Bid API", company_id=company.id, owner_id=trader.id)
        from app.services.excess_service import confirm_import
        confirm_import(db_session, el.id, [{"part_number": "LM358N", "quantity": 500}])
        item = db_session.query(ExcessLineItem).filter_by(excess_list_id=el.id).first()
        buyer = _make_company(db_session, "API Buyer")

        for client in _api_client(db_session, trader):
            resp = client.post(
                f"/api/excess-lists/{el.id}/line-items/{item.id}/bids",
                json={"unit_price": 0.35, "quantity_wanted": 200, "bidder_company_id": buyer.id},
            )
            assert resp.status_code == 201
            assert resp.json()["unit_price"] == 0.35

    def test_list_bids_via_api(self, db_session, company, trader):
        el = create_excess_list(db_session, title="List Bids", company_id=company.id, owner_id=trader.id)
        from app.services.excess_service import confirm_import
        confirm_import(db_session, el.id, [{"part_number": "LM358N", "quantity": 500}])
        item = db_session.query(ExcessLineItem).filter_by(excess_list_id=el.id).first()
        buyer = _make_company(db_session, "List Buyer")
        create_bid(db_session, line_item_id=item.id, list_id=el.id, unit_price=0.30, quantity_wanted=100, bidder_company_id=buyer.id, user_id=trader.id)

        for client in _api_client(db_session, trader):
            resp = client.get(f"/api/excess-lists/{el.id}/line-items/{item.id}/bids")
            assert resp.status_code == 200
            assert len(resp.json()["items"]) == 1

    def test_accept_bid_via_api(self, db_session, company, trader):
        el = create_excess_list(db_session, title="Accept API", company_id=company.id, owner_id=trader.id)
        from app.services.excess_service import confirm_import
        confirm_import(db_session, el.id, [{"part_number": "LM358N", "quantity": 500}])
        item = db_session.query(ExcessLineItem).filter_by(excess_list_id=el.id).first()
        buyer = _make_company(db_session, "Accept Buyer")
        bid = create_bid(db_session, line_item_id=item.id, list_id=el.id, unit_price=0.30, quantity_wanted=100, bidder_company_id=buyer.id, user_id=trader.id)

        for client in _api_client(db_session, trader):
            resp = client.patch(
                f"/api/excess-lists/{el.id}/line-items/{item.id}/bids/{bid.id}",
                json={"status": "accepted"},
            )
            assert resp.status_code == 200
            assert resp.json()["status"] == "accepted"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_excess_crud.py::TestBidAPI -v
```
Expected: FAIL — routes not found

- [ ] **Step 3: Add bid endpoints to router**

Add to `app/routers/excess.py`:

```python
from ..schemas.excess import BidCreateRequest, BidResponse, BidUpdate
from ..services.excess_service import create_bid, list_bids, accept_bid


@router.post("/api/excess-lists/{list_id}/line-items/{item_id}/bids", status_code=201)
async def api_create_bid(
    list_id: int,
    item_id: int,
    payload: BidCreateRequest,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Record a bid on an excess line item."""
    bid = create_bid(
        db,
        line_item_id=item_id,
        list_id=list_id,
        unit_price=payload.unit_price,
        quantity_wanted=payload.quantity_wanted,
        bidder_company_id=payload.bidder_company_id,
        bidder_vendor_card_id=payload.bidder_vendor_card_id,
        lead_time_days=payload.lead_time_days,
        source=payload.source or "manual",
        notes=payload.notes,
        user_id=user.id,
    )
    return BidResponse.model_validate(bid)


@router.get("/api/excess-lists/{list_id}/line-items/{item_id}/bids")
async def api_list_bids(
    list_id: int,
    item_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """List bids for a line item, sorted by price ascending."""
    bids = list_bids(db, item_id, list_id)
    return {"items": [BidResponse.model_validate(b) for b in bids], "total": len(bids)}


@router.patch("/api/excess-lists/{list_id}/line-items/{item_id}/bids/{bid_id}")
async def api_update_bid(
    list_id: int,
    item_id: int,
    bid_id: int,
    payload: BidUpdate,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Accept or reject a bid."""
    if payload.status == "accepted":
        bid = accept_bid(db, bid_id, item_id, list_id)
    else:
        bid = db.get(Bid, bid_id)
        if not bid or bid.excess_line_item_id != item_id:
            raise HTTPException(404, "Bid not found")
        updates = payload.model_dump(exclude_unset=True)
        for k, v in updates.items():
            if v is not None:
                setattr(bid, k, v)
        db.commit()
        db.refresh(bid)
    return BidResponse.model_validate(bid)
```

**Schema fix needed:** The existing `BidCreate` in `app/schemas/excess.py` has a required `excess_line_item_id: int` field, but the endpoint gets `item_id` from the URL path. Create a new schema `BidCreateRequest` without that field:

```python
# Add to app/schemas/excess.py
class BidCreateRequest(BaseModel):
    """Request body for creating a bid — excess_line_item_id comes from URL path."""
    unit_price: float = Field(ge=0)
    quantity_wanted: int = Field(ge=1)
    lead_time_days: int | None = Field(default=None, ge=0)
    bidder_company_id: int | None = None
    bidder_vendor_card_id: int | None = None
    source: Literal["manual", "phone"] | None = "manual"
    notes: str | None = None
```

Use `BidCreateRequest` (not `BidCreate`) in the `api_create_bid` endpoint.

- [ ] **Step 4: Run tests to verify they pass**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_excess_crud.py::TestBidAPI -v
```
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add app/routers/excess.py app/schemas/excess.py tests/test_excess_crud.py
git commit -m "feat(excess): bid API endpoints — create, list, accept/reject"
```

---

### Task 9: Frontend — Bid Modal Templates

**Files:**
- Create: `app/templates/htmx/partials/excess/bid_form.html`
- Create: `app/templates/htmx/partials/excess/bid_list.html`
- Modify: `app/routers/excess.py` (add HTMX partial endpoints)
- Modify: `app/templates/htmx/partials/excess/line_item_row.html` (add bid count link)

- [ ] **Step 1: Create bid_form.html**

Modal form to record a bid. Receives: `list_id`, `item_id`, `companies` (for bidder dropdown).

Fields: bidder company (dropdown), price per unit, quantity wanted, lead time (days), source (manual/phone), notes.

Submit: `hx-post="/api/excess-lists/{{ list_id }}/line-items/{{ item_id }}/bids"` with `hx-ext="json-enc"`. On success: close modal, show toast, refresh detail.

- [ ] **Step 2: Create bid_list.html**

Modal content showing all bids for a line item. Receives: `list_id`, `item_id`, `bids`, `item` (the ExcessLineItem).

Shows:
- Header: "Bids for {item.part_number}" with part number and qty
- Table sorted by price asc: bidder name (company or vendor), price, qty wanted, lead time, status badge, action buttons
- Status badges: pending=amber, accepted=emerald, rejected=gray
- Accepted bid: green highlight row
- Accept button: `hx-patch` with `{"status": "accepted"}`, refreshes modal content
- Reject button: `hx-patch` with `{"status": "rejected"}`
- "Record New Bid" button at bottom opens bid_form

- [ ] **Step 3: Add HTMX partial endpoints**

```python
@router.get("/v2/partials/excess/{list_id}/line-items/{item_id}/bid-form", response_class=HTMLResponse)
async def partial_bid_form(
    request: Request, list_id: int, item_id: int,
    user: User = Depends(require_user), db: Session = Depends(get_db),
):
    companies = db.query(Company).order_by(Company.name).all()
    return templates.TemplateResponse("htmx/partials/excess/bid_form.html", {
        "request": request, "list_id": list_id, "item_id": item_id, "companies": companies,
    })


@router.get("/v2/partials/excess/{list_id}/line-items/{item_id}/bids", response_class=HTMLResponse)
async def partial_bid_list(
    request: Request, list_id: int, item_id: int,
    user: User = Depends(require_user), db: Session = Depends(get_db),
):
    from ..services.excess_service import list_bids
    item = db.get(ExcessLineItem, item_id)
    bids = list_bids(db, item_id, list_id)
    companies = db.query(Company).order_by(Company.name).all()
    return templates.TemplateResponse("htmx/partials/excess/bid_list.html", {
        "request": request, "list_id": list_id, "item_id": item_id,
        "item": item, "bids": bids, "companies": companies,
    })
```

- [ ] **Step 4: Add bid count link to line_item_row.html**

After the status/match columns, add a "Bids" column with a count link:

```html
<td class="px-4 py-3 text-center">
  {% set bid_count = item.bids|length if item.bids else 0 %}
  {% if bid_count > 0 %}
  <a @click="$dispatch('open-modal')"
     hx-get="/v2/partials/excess/{{ item.excess_list_id }}/line-items/{{ item.id }}/bids"
     hx-target="#modal-content"
     class="text-brand-500 hover:text-brand-600 text-sm font-medium cursor-pointer">
    {{ bid_count }} bid{{ "s" if bid_count != 1 }}
  </a>
  {% else %}
  <a @click="$dispatch('open-modal')"
     hx-get="/v2/partials/excess/{{ item.excess_list_id }}/line-items/{{ item.id }}/bid-form"
     hx-target="#modal-content"
     class="text-gray-400 hover:text-brand-500 text-xs cursor-pointer">
    + Bid
  </a>
  {% endif %}
</td>
```

Also add `<th>Bids</th>` to the table header in detail.html.

- [ ] **Step 5: Commit**

```bash
git add app/templates/htmx/partials/excess/bid_form.html app/templates/htmx/partials/excess/bid_list.html app/routers/excess.py app/templates/htmx/partials/excess/line_item_row.html app/templates/htmx/partials/excess/detail.html
git commit -m "feat(excess): bid modal UI — form, list, accept/reject"
```

---

## SP3: List & Detail UX (Can Run in Parallel)

### Task 10: Service — Sortable list + Owner Filter

**Files:**
- Modify: `app/services/excess_service.py`
- Modify: `tests/test_excess_crud.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_excess_crud.py`:

```python
class TestListExcessSortAndFilter:
    def test_sort_by_title_asc(self, db_session, company, trader):
        create_excess_list(db_session, title="Zebra", company_id=company.id, owner_id=trader.id)
        create_excess_list(db_session, title="Alpha", company_id=company.id, owner_id=trader.id)
        result = list_excess_lists(db_session, sort_by="title", sort_dir="asc", limit=50, offset=0)
        titles = [el.title for el in result["items"]]
        assert titles[0] == "Alpha"
        assert titles[1] == "Zebra"

    def test_sort_by_created_at_desc(self, db_session, company, trader):
        el1 = create_excess_list(db_session, title="First", company_id=company.id, owner_id=trader.id)
        el2 = create_excess_list(db_session, title="Second", company_id=company.id, owner_id=trader.id)
        result = list_excess_lists(db_session, sort_by="created_at", sort_dir="desc", limit=50, offset=0)
        assert result["items"][0].title == "Second"

    def test_filter_by_owner(self, db_session, company, trader):
        other = _make_user(db_session, "other@test.com")
        create_excess_list(db_session, title="Mine", company_id=company.id, owner_id=trader.id)
        create_excess_list(db_session, title="Theirs", company_id=company.id, owner_id=other.id)
        result = list_excess_lists(db_session, owner_id=trader.id, limit=50, offset=0)
        assert result["total"] == 1
        assert result["items"][0].title == "Mine"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_excess_crud.py::TestListExcessSortAndFilter -v
```
Expected: FAIL — `list_excess_lists()` doesn't accept `sort_by`, `sort_dir`, `owner_id`

- [ ] **Step 3: Update list_excess_lists() to accept sort and owner params**

Update `list_excess_lists()` in `app/services/excess_service.py`:

```python
_SORTABLE_FIELDS = {
    "title": ExcessList.title,
    "status": ExcessList.status,
    "total_line_items": ExcessList.total_line_items,
    "created_at": ExcessList.created_at,
}


def list_excess_lists(
    db: Session,
    *,
    q: str = "",
    status: str | None = None,
    owner_id: int | None = None,
    sort_by: str = "created_at",
    sort_dir: str = "desc",
    limit: int = 50,
    offset: int = 0,
) -> dict:
    query = db.query(ExcessList)

    if q:
        query = query.filter(ExcessList.title.ilike(f"%{q}%"))
    if status:
        query = query.filter(ExcessList.status == status)
    if owner_id:
        query = query.filter(ExcessList.owner_id == owner_id)

    total = query.count()

    sort_col = _SORTABLE_FIELDS.get(sort_by, ExcessList.created_at)
    order = sort_col.asc() if sort_dir == "asc" else sort_col.desc()
    items = query.order_by(order).offset(offset).limit(limit).all()

    return {"items": items, "total": total, "limit": limit, "offset": offset}
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_excess_crud.py::TestListExcessSortAndFilter -v
```
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add app/services/excess_service.py tests/test_excess_crud.py
git commit -m "feat(excess): sortable list + owner filter in service layer"
```

---

### Task 11: API + Frontend — Sortable Headers, Owner Filter, Bulk Delete

**Files:**
- Modify: `app/routers/excess.py` (update partial + add bulk delete endpoint)
- Modify: `app/templates/htmx/partials/excess/list.html` (sortable headers, owner dropdown)
- Modify: `app/templates/htmx/partials/excess/detail.html` (checkbox column, action bar)
- Modify: `app/templates/htmx/partials/excess/line_item_row.html` (add checkbox)

- [ ] **Step 1: Update list partial endpoint to pass sort/owner params**

In `app/routers/excess.py`, update `partial_excess_list`:

```python
@router.get("/v2/partials/excess", response_class=HTMLResponse)
async def partial_excess_list(
    request: Request,
    q: str = "",
    status: str = "",
    owner_id: int | None = None,
    sort_by: str = "created_at",
    sort_dir: str = "desc",
    limit: int = 50,
    offset: int = 0,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    # Default to current user
    effective_owner = owner_id if owner_id is not None else user.id
    if owner_id == 0:  # "All" option sends 0
        effective_owner = None

    result = list_excess_lists(
        db, q=q, status=status or None, owner_id=effective_owner,
        sort_by=sort_by, sort_dir=sort_dir, limit=limit, offset=offset,
    )
    companies = db.query(Company).order_by(Company.name).all()
    # Get all owners for the filter dropdown
    from ..models import User as UserModel
    owners = db.query(UserModel).join(ExcessList, UserModel.id == ExcessList.owner_id).distinct().all()

    return templates.TemplateResponse("htmx/partials/excess/list.html", {
        "request": request, "user": user,
        "lists": result["items"], "total": result["total"],
        "limit": limit, "offset": offset,
        "companies": companies, "q": q,
        "status_filter": status or "",
        "owner_id": effective_owner,
        "owners": owners,
        "sort_by": sort_by, "sort_dir": sort_dir,
    })
```

- [ ] **Step 2: Update list.html with sortable headers and owner dropdown**

In `app/templates/htmx/partials/excess/list.html`:

- Add owner dropdown after status pills:
```html
<select name="owner_id"
        hx-get="/v2/partials/excess" hx-target="#main-content" hx-push-url="true"
        hx-include="[name='q'], [name='status']"
        class="px-3 py-1.5 border border-gray-200 rounded-lg text-sm">
  <option value="{{ user.id }}" {{ "selected" if owner_id == user.id }}>My Lists</option>
  <option value="0" {{ "selected" if owner_id is none }}>All</option>
  {% for o in owners %}
  {% if o.id != user.id %}
  <option value="{{ o.id }}" {{ "selected" if owner_id == o.id }}>{{ o.name }}</option>
  {% endif %}
  {% endfor %}
</select>
```

- Make table headers clickable with sort params:
```html
{% macro sort_header(label, field) %}
<th class="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider cursor-pointer hover:text-brand-600"
    hx-get="/v2/partials/excess?sort_by={{ field }}&sort_dir={{ 'asc' if sort_by == field and sort_dir == 'desc' else 'desc' }}&q={{ q }}&status={{ status_filter }}&owner_id={{ owner_id or '' }}"
    hx-target="#main-content" hx-push-url="true">
  {{ label }}
  {% if sort_by == field %}
  <span class="ml-0.5">{{ "▲" if sort_dir == "asc" else "▼" }}</span>
  {% endif %}
</th>
{% endmacro %}
```

- [ ] **Step 3: Add bulk delete endpoint**

Add to `app/routers/excess.py`:

```python
@router.post("/api/excess-lists/{list_id}/line-items/bulk-delete")
async def api_bulk_delete_line_items(
    list_id: int,
    payload: dict,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Delete multiple line items by ID."""
    from ..services.excess_service import bulk_delete_line_items
    ids = payload.get("ids", [])
    if not ids:
        raise HTTPException(400, "No IDs provided")
    result = bulk_delete_line_items(db, list_id, ids)
    return result
```

Add to `app/services/excess_service.py`:

```python
def bulk_delete_line_items(db: Session, list_id: int, item_ids: list[int]) -> dict:
    """Delete multiple line items from an excess list."""
    excess_list = get_excess_list(db, list_id)
    deleted = 0
    for item_id in item_ids:
        item = db.get(ExcessLineItem, item_id)
        if item and item.excess_list_id == list_id:
            db.delete(item)
            deleted += 1
    if deleted > 0:
        excess_list.total_line_items = max((excess_list.total_line_items or 0) - deleted, 0)
        _safe_commit(db, entity="bulk delete")
    logger.info("Bulk deleted {} items from ExcessList id={}", deleted, list_id)
    return {"deleted": deleted}
```

- [ ] **Step 4: Add bulk delete UI to detail.html**

In `app/templates/htmx/partials/excess/detail.html`:

Add Alpine.js `x-data` for checkbox state:
```html
<div x-data="{ selected: [] }">
```

Add checkbox column to table header and rows. Add action bar above table:
```html
<div x-show="selected.length > 0" x-cloak class="mb-3 px-4 py-2 bg-rose-50 border border-rose-200 rounded-lg flex items-center justify-between">
  <span class="text-sm text-rose-700" x-text="`${selected.length} item(s) selected`"></span>
  <button @click="if(confirm('Delete selected items?')) { fetch('/api/excess-lists/{{ list.id }}/line-items/bulk-delete', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ids: selected})}).then(() => { selected=[]; htmx.ajax('GET', '/v2/partials/excess/{{ list.id }}', '#main-content') }) }"
          class="text-sm font-medium text-rose-700 hover:text-rose-800">
    Delete Selected
  </button>
</div>
```

- [ ] **Step 5: Test manually + run test suite**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_excess_crud.py -v
```

- [ ] **Step 6: Commit**

```bash
git add app/routers/excess.py app/services/excess_service.py app/templates/htmx/partials/excess/list.html app/templates/htmx/partials/excess/detail.html app/templates/htmx/partials/excess/line_item_row.html tests/test_excess_crud.py
git commit -m "feat(excess): sortable columns, owner filter, bulk delete"
```

---

### Task 12: Full Test Suite + Deploy

- [ ] **Step 1: Run full test suite**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v
```
Expected: No new failures (pre-existing proactive matching failures are acceptable)

- [ ] **Step 2: Push and deploy**

```bash
git push origin main
docker compose up -d --build
docker compose logs app --tail 20
```
Verify: App starts cleanly, migration runs, `/v2/excess` loads.

- [ ] **Step 3: Verify features**

Manual checks:
- Upload a CSV → see preview → confirm import → line items appear
- Line items with matching active requirements show demand match badges
- Record a bid on a line item → bid count appears
- Open bid list modal → accept a bid → others auto-rejected
- Sort list by title, status, created date
- Owner filter defaults to current user, can switch to All
- Bulk select + delete line items works
