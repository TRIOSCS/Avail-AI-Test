# Excess & Bid Collection Phase 2: CRUD + CSV Import

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build ExcessList CRUD API, CSV/Excel import for line items, and frontend templates (list + detail views) for managing customer excess inventory.

**Architecture:** Thin router (`routers/excess.py`) delegates to service layer (`services/excess_service.py`). CSV/Excel import reuses existing `file_utils.parse_tabular_file()`. Frontend uses HTMX partials + Alpine.js following the established v2 pattern. Nav entry added to `base.html`.

**Tech Stack:** FastAPI, SQLAlchemy 2.0, Jinja2, HTMX 2.x, Alpine.js 3.x, Tailwind CSS

**Spec:** See parent spec at top of user's original message (Phase 2 of Excess & Bid Collection).

**Phase 1 artifacts (already done):**
- Models: `app/models/excess.py` — ExcessList, ExcessLineItem, BidSolicitation, Bid
- Schemas: `app/schemas/excess.py` — Create/Update/Response for all models + ImportRow
- Migration: `alembic/versions/29a41f5a248c_...py`

---

### File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `app/services/excess_service.py` | Create | Business logic: CRUD, import parsing, validation |
| `app/routers/excess.py` | Create | API endpoints + HTMX partial routes |
| `app/routers/htmx_views.py` | Modify | Add `/v2/excess` page routes + nav entry logic |
| `app/templates/htmx/base.html` | Modify | Add "Excess" to bottom nav items |
| `app/templates/htmx/partials/excess/list.html` | Create | List view with filters, table, create modal |
| `app/templates/htmx/partials/excess/detail.html` | Create | Detail view: header + line items table |
| `app/templates/htmx/partials/excess/row.html` | Create | Single table row partial (for HTMX swap) |
| `app/templates/htmx/partials/excess/line_item_row.html` | Create | Single line item row partial |
| `app/main.py` | Modify | Register excess router |
| `tests/test_excess_crud.py` | Create | API + service tests |

---

### Task 1: Service Layer — ExcessList CRUD + Import

**Files:**
- Create: `app/services/excess_service.py`
- Test: `tests/test_excess_crud.py`

- [ ] **Step 1: Write failing tests for the service**

```python
# tests/test_excess_crud.py
"""Tests for Excess CRUD service and API endpoints."""

from decimal import Decimal
import pytest
from sqlalchemy.orm import Session
from app.models import Company, User
from app.models.excess import ExcessList, ExcessLineItem
from app.services.excess_service import (
    create_excess_list,
    get_excess_list,
    list_excess_lists,
    update_excess_list,
    delete_excess_list,
    import_line_items,
)
from tests.conftest import engine
_ = engine


@pytest.fixture()
def company(db_session: Session) -> Company:
    co = Company(name="Seller Corp")
    db_session.add(co)
    db_session.commit()
    db_session.refresh(co)
    return co


@pytest.fixture()
def trader(db_session: Session) -> User:
    user = User(email="excess-svc@trioscs.com", name="Svc Trader", role="trader", azure_id="svc-trader-001", m365_connected=True)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


class TestCreateExcessList:
    def test_creates_with_required_fields(self, db_session, company, trader):
        el = create_excess_list(db_session, title="Q1 Excess", company_id=company.id, owner_id=trader.id)
        assert el.id is not None
        assert el.title == "Q1 Excess"
        assert el.status == "draft"

    def test_invalid_company_raises_404(self, db_session, trader):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc:
            create_excess_list(db_session, title="Bad", company_id=99999, owner_id=trader.id)
        assert exc.value.status_code == 404


class TestListExcessLists:
    def test_returns_paginated(self, db_session, company, trader):
        for i in range(3):
            create_excess_list(db_session, title=f"List {i}", company_id=company.id, owner_id=trader.id)
        result = list_excess_lists(db_session, limit=2, offset=0)
        assert result["total"] == 3
        assert len(result["items"]) == 2

    def test_search_filter(self, db_session, company, trader):
        create_excess_list(db_session, title="Alpha Excess", company_id=company.id, owner_id=trader.id)
        create_excess_list(db_session, title="Beta Excess", company_id=company.id, owner_id=trader.id)
        result = list_excess_lists(db_session, q="alpha", limit=50, offset=0)
        assert result["total"] == 1


class TestUpdateExcessList:
    def test_updates_title(self, db_session, company, trader):
        el = create_excess_list(db_session, title="Old", company_id=company.id, owner_id=trader.id)
        updated = update_excess_list(db_session, el.id, title="New Title")
        assert updated.title == "New Title"

    def test_not_found_raises_404(self, db_session):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc:
            update_excess_list(db_session, 99999, title="X")
        assert exc.value.status_code == 404


class TestDeleteExcessList:
    def test_hard_deletes(self, db_session, company, trader):
        el = create_excess_list(db_session, title="Doomed", company_id=company.id, owner_id=trader.id)
        eid = el.id
        delete_excess_list(db_session, eid)
        assert db_session.get(ExcessList, eid) is None


class TestImportLineItems:
    def test_imports_valid_rows(self, db_session, company, trader):
        el = create_excess_list(db_session, title="Import Test", company_id=company.id, owner_id=trader.id)
        rows = [
            {"part_number": "LM358N", "quantity": "500", "asking_price": "0.45"},
            {"part_number": "NE555P", "quantity": "1000", "manufacturer": "TI"},
        ]
        result = import_line_items(db_session, el.id, rows)
        assert result["imported"] == 2
        assert result["skipped"] == 0
        db_session.refresh(el)
        assert el.total_line_items == 2

    def test_skips_invalid_rows(self, db_session, company, trader):
        el = create_excess_list(db_session, title="Import Test 2", company_id=company.id, owner_id=trader.id)
        rows = [
            {"part_number": "", "quantity": "500"},  # blank part number
            {"part_number": "LM358N", "quantity": "abc"},  # bad quantity
            {"part_number": "NE555P", "quantity": "100"},  # valid
        ]
        result = import_line_items(db_session, el.id, rows)
        assert result["imported"] == 1
        assert result["skipped"] == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_excess_crud.py -v`
Expected: FAIL — `excess_service` module does not exist

- [ ] **Step 3: Implement the service**

Create `app/services/excess_service.py`:

```python
"""excess_service.py — Business logic for Excess Inventory lifecycle.

Handles CRUD operations for ExcessLists and line item import from CSV/Excel.

Called by: routers/excess.py
Depends on: models/excess.py, schemas/excess.py
"""

from fastapi import HTTPException
from loguru import logger
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Company
from app.models.excess import ExcessLineItem, ExcessList


def _safe_commit(db: Session, *, entity: str = "record") -> None:
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        logger.warning("IntegrityError on {}: {}", entity, exc)
        raise HTTPException(409, f"Duplicate or conflicting {entity}") from exc


def create_excess_list(
    db: Session,
    *,
    title: str,
    company_id: int,
    owner_id: int,
    customer_site_id: int | None = None,
    notes: str | None = None,
    source_filename: str | None = None,
) -> ExcessList:
    if not db.get(Company, company_id):
        raise HTTPException(404, f"Company {company_id} not found")
    el = ExcessList(
        title=title,
        company_id=company_id,
        owner_id=owner_id,
        customer_site_id=customer_site_id,
        notes=notes,
        source_filename=source_filename,
    )
    db.add(el)
    _safe_commit(db, entity="excess_list")
    db.refresh(el)
    logger.info("Created ExcessList id={} title={!r}", el.id, el.title)
    return el


def get_excess_list(db: Session, list_id: int) -> ExcessList:
    el = db.get(ExcessList, list_id)
    if not el:
        raise HTTPException(404, f"ExcessList {list_id} not found")
    return el


def list_excess_lists(
    db: Session,
    *,
    q: str = "",
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    query = db.query(ExcessList)
    if q:
        query = query.filter(ExcessList.title.ilike(f"%{q}%"))
    if status:
        query = query.filter(ExcessList.status == status)
    total = query.count()
    items = query.order_by(ExcessList.created_at.desc()).limit(limit).offset(offset).all()
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def update_excess_list(db: Session, list_id: int, **kwargs) -> ExcessList:
    el = get_excess_list(db, list_id)
    update_data = {k: v for k, v in kwargs.items() if v is not None}
    for k, v in update_data.items():
        setattr(el, k, v)
    _safe_commit(db, entity="excess_list")
    db.refresh(el)
    return el


def delete_excess_list(db: Session, list_id: int) -> None:
    el = get_excess_list(db, list_id)
    db.delete(el)
    _safe_commit(db, entity="excess_list")
    logger.info("Deleted ExcessList id={}", list_id)


def import_line_items(db: Session, list_id: int, rows: list[dict]) -> dict:
    el = get_excess_list(db, list_id)
    imported = 0
    skipped = 0
    errors = []

    for i, row in enumerate(rows):
        pn = (row.get("part_number") or row.get("part number") or row.get("mpn") or "").strip()
        if not pn:
            skipped += 1
            errors.append(f"Row {i+1}: missing part number")
            continue

        qty_raw = row.get("quantity") or row.get("qty") or "1"
        try:
            qty = int(qty_raw)
            if qty < 1:
                raise ValueError
        except (ValueError, TypeError):
            skipped += 1
            errors.append(f"Row {i+1}: invalid quantity '{qty_raw}'")
            continue

        price_raw = row.get("asking_price") or row.get("price") or row.get("unit_price")
        asking_price = None
        if price_raw:
            try:
                asking_price = float(str(price_raw).replace("$", "").replace(",", "").strip())
            except (ValueError, TypeError):
                pass  # price is optional — skip silently

        li = ExcessLineItem(
            excess_list_id=el.id,
            part_number=pn,
            manufacturer=(row.get("manufacturer") or row.get("mfr") or "").strip() or None,
            quantity=qty,
            date_code=(row.get("date_code") or row.get("dc") or "").strip() or None,
            condition=(row.get("condition") or "New").strip(),
            asking_price=asking_price,
        )
        db.add(li)
        imported += 1

    if imported > 0:
        el.total_line_items = (el.total_line_items or 0) + imported
        _safe_commit(db, entity="excess_line_items")

    logger.info("Imported {} line items into ExcessList {} (skipped {})", imported, list_id, skipped)
    return {"imported": imported, "skipped": skipped, "errors": errors}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_excess_crud.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add app/services/excess_service.py tests/test_excess_crud.py
git commit -m "feat(excess): Phase 2.1 — service layer with CRUD + CSV import"
```

---

### Task 2: API Router — REST Endpoints + File Upload

**Files:**
- Create: `app/routers/excess.py`
- Modify: `app/main.py` (add `include_router`)
- Test: `tests/test_excess_crud.py` (add API tests)

- [ ] **Step 1: Write failing API tests**

Append to `tests/test_excess_crud.py`:

```python
from fastapi.testclient import TestClient
from app.main import app
from app.database import get_db
from app.dependencies import require_user

# Override auth + DB for API tests
def _override_db(db_session):
    def _get_db():
        yield db_session
    return _get_db

def _override_user(user):
    def _require_user():
        return user
    return _require_user


class TestExcessListAPI:
    def test_create_via_api(self, db_session, company, trader):
        app.dependency_overrides[get_db] = _override_db(db_session)
        app.dependency_overrides[require_user] = _override_user(trader)
        client = TestClient(app)
        resp = client.post("/api/excess-lists", json={"title": "API Test", "company_id": company.id})
        assert resp.status_code == 200
        assert resp.json()["title"] == "API Test"
        app.dependency_overrides.clear()

    def test_list_via_api(self, db_session, company, trader):
        create_excess_list(db_session, title="L1", company_id=company.id, owner_id=trader.id)
        app.dependency_overrides[get_db] = _override_db(db_session)
        app.dependency_overrides[require_user] = _override_user(trader)
        client = TestClient(app)
        resp = client.get("/api/excess-lists")
        assert resp.status_code == 200
        assert resp.json()["total"] >= 1
        app.dependency_overrides.clear()

    def test_get_detail_via_api(self, db_session, company, trader):
        el = create_excess_list(db_session, title="Detail", company_id=company.id, owner_id=trader.id)
        app.dependency_overrides[get_db] = _override_db(db_session)
        app.dependency_overrides[require_user] = _override_user(trader)
        client = TestClient(app)
        resp = client.get(f"/api/excess-lists/{el.id}")
        assert resp.status_code == 200
        assert resp.json()["title"] == "Detail"
        app.dependency_overrides.clear()

    def test_upload_csv(self, db_session, company, trader):
        el = create_excess_list(db_session, title="Upload", company_id=company.id, owner_id=trader.id)
        csv_content = "part_number,quantity,asking_price\nLM358N,500,0.45\nNE555P,1000,0.22\n"
        app.dependency_overrides[get_db] = _override_db(db_session)
        app.dependency_overrides[require_user] = _override_user(trader)
        client = TestClient(app)
        resp = client.post(
            f"/api/excess-lists/{el.id}/import",
            files={"file": ("excess.csv", csv_content.encode(), "text/csv")},
        )
        assert resp.status_code == 200
        assert resp.json()["imported"] == 2
        app.dependency_overrides.clear()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_excess_crud.py::TestExcessListAPI -v`
Expected: FAIL — route not found (404)

- [ ] **Step 3: Create the router**

Create `app/routers/excess.py`:

```python
"""excess.py — API endpoints for Excess Inventory & Bid Collection.

REST endpoints for ExcessList CRUD, line item management, and CSV/Excel import.

Called by: main.py (include_router)
Depends on: services/excess_service.py, models/excess.py, schemas/excess.py
"""

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import require_user
from app.file_utils import parse_tabular_file
from app.models import User
from app.models.excess import ExcessLineItem
from app.schemas.excess import (
    ExcessLineItemCreate,
    ExcessLineItemResponse,
    ExcessListCreate,
    ExcessListResponse,
    ExcessListUpdate,
)
from app.services.excess_service import (
    create_excess_list,
    delete_excess_list,
    get_excess_list,
    import_line_items,
    list_excess_lists,
    update_excess_list,
)

router = APIRouter(tags=["excess"])


@router.get("/api/excess-lists")
async def api_list_excess(
    q: str = Query("", description="Search title"),
    status: str | None = Query(None),
    limit: int = Query(50, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    return list_excess_lists(db, q=q, status=status, limit=limit, offset=offset)


@router.post("/api/excess-lists", response_model=ExcessListResponse)
async def api_create_excess(
    payload: ExcessListCreate,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    el = create_excess_list(
        db,
        title=payload.title,
        company_id=payload.company_id,
        owner_id=user.id,
        customer_site_id=payload.customer_site_id,
        notes=payload.notes,
    )
    return ExcessListResponse.model_validate(el)


@router.get("/api/excess-lists/{list_id}", response_model=ExcessListResponse)
async def api_get_excess(
    list_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    el = get_excess_list(db, list_id)
    return ExcessListResponse.model_validate(el)


@router.patch("/api/excess-lists/{list_id}", response_model=ExcessListResponse)
async def api_update_excess(
    list_id: int,
    payload: ExcessListUpdate,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    update_data = payload.model_dump(exclude_unset=True)
    el = update_excess_list(db, list_id, **update_data)
    return ExcessListResponse.model_validate(el)


@router.delete("/api/excess-lists/{list_id}")
async def api_delete_excess(
    list_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    delete_excess_list(db, list_id)
    return {"id": list_id, "status": "deleted"}


@router.post("/api/excess-lists/{list_id}/import")
async def api_import_excess(
    list_id: int,
    file: UploadFile = File(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    if not file or not file.filename:
        raise HTTPException(400, "No file uploaded")
    ext = "." + (file.filename or "").rsplit(".", 1)[-1].lower()
    if ext not in {".csv", ".xlsx", ".xls", ".tsv"}:
        raise HTTPException(400, f"Invalid file type '{ext}'. Use CSV, TSV, or Excel.")
    content = await file.read()
    if len(content) > 10_000_000:
        raise HTTPException(413, "File too large — 10MB maximum")
    rows = parse_tabular_file(content, file.filename or "upload.csv")
    if not rows:
        raise HTTPException(400, "No data rows found in file")
    result = import_line_items(db, list_id, rows)
    return result


@router.post("/api/excess-lists/{list_id}/line-items", response_model=ExcessLineItemResponse)
async def api_add_line_item(
    list_id: int,
    payload: ExcessLineItemCreate,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    el = get_excess_list(db, list_id)
    li = ExcessLineItem(
        excess_list_id=el.id,
        part_number=payload.part_number,
        manufacturer=payload.manufacturer,
        quantity=payload.quantity,
        date_code=payload.date_code,
        condition=payload.condition,
        asking_price=payload.asking_price,
        notes=payload.notes,
    )
    db.add(li)
    el.total_line_items = (el.total_line_items or 0) + 1
    db.commit()
    db.refresh(li)
    return ExcessLineItemResponse.model_validate(li)


@router.get("/api/excess-lists/{list_id}/line-items")
async def api_list_line_items(
    list_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    el = get_excess_list(db, list_id)
    items = db.query(ExcessLineItem).filter_by(excess_list_id=el.id).order_by(ExcessLineItem.id).all()
    return {"items": [ExcessLineItemResponse.model_validate(i) for i in items], "total": len(items)}
```

- [ ] **Step 4: Register router in main.py**

In `app/main.py`, add with the other router imports (alphabetically near the E's):
```python
from .routers.excess import router as excess_router
```
And in the `include_router` block:
```python
app.include_router(excess_router)
```

- [ ] **Step 5: Run tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_excess_crud.py -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add app/routers/excess.py app/main.py tests/test_excess_crud.py
git commit -m "feat(excess): Phase 2.2 — API router with CRUD + file upload"
```

---

### Task 3: Frontend — Nav Entry + Page Routes

**Files:**
- Modify: `app/templates/htmx/base.html` (add nav item)
- Modify: `app/routers/htmx_views.py` (add page routes + `current_view` mapping)

- [ ] **Step 1: Add "Excess" to bottom nav in `base.html`**

In `app/templates/htmx/base.html`, find the `bottom_items` list (around line 114) and add an entry after `buy-plans`:

```python
('excess', 'Excess', '/v2/excess', '/v2/partials/excess', 'M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2'),
```

(Clipboard/document icon — represents an inventory list)

- [ ] **Step 2: Add page routes in htmx_views.py**

Add route decorators for `/v2/excess` and `/v2/excess/{list_id:int}` to the `v2_page` handler (after the existing `/v2/follow-ups` route decorator, around line 170):

```python
@router.get("/v2/excess", response_class=HTMLResponse)
@router.get("/v2/excess/{list_id:int}", response_class=HTMLResponse)
```

Then add the `current_view` mapping in the if/elif chain:
```python
elif "/excess" in path:
    current_view = "excess"
```

And add the detail URL parsing:
```python
elif current_view == "excess" and "/excess/" in path:
    parts = path.split("/excess/")
    if len(parts) > 1 and parts[1].isdigit():
        partial_url = f"/v2/partials/excess/{parts[1]}"
```

- [ ] **Step 3: Commit**

```bash
git add app/templates/htmx/base.html app/routers/htmx_views.py
git commit -m "feat(excess): Phase 2.3 — nav entry + page routes"
```

---

### Task 4: Frontend — List Template

**Files:**
- Create: `app/templates/htmx/partials/excess/list.html`
- Create: `app/templates/htmx/partials/excess/row.html`
- Modify: `app/routers/excess.py` (add partial endpoints)

- [ ] **Step 1: Create list partial**

Create `app/templates/htmx/partials/excess/list.html` with:
- Title "Excess Inventory" + "New List" button (opens create modal)
- Filter bar: search input + status pills (draft, active, bidding, closed)
- Table: title, company, status, line items count, created date, actions
- Create modal with form: title, company dropdown, notes
- Filters use `hx-get="/v2/partials/excess"` with `hx-include`
- "New List" form submits via `hx-post="/api/excess-lists"`, swaps list on success

Follow the exact pattern from `requisitions/list.html` or `companies/list.html`.

- [ ] **Step 2: Create row partial**

Create `app/templates/htmx/partials/excess/row.html` — single `<tr>` for the table with:
- Title (clickable link to detail)
- Company name
- Status badge (color-coded)
- Line items count
- Created date
- Delete button with `hx-confirm`

- [ ] **Step 3: Add HTMX partial endpoints to router**

Add to `app/routers/excess.py`:

```python
from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory="app/templates")

@router.get("/v2/partials/excess", response_class=HTMLResponse)
async def partial_excess_list(
    request: Request,
    q: str = "",
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    result = list_excess_lists(db, q=q, status=status, limit=limit, offset=offset)
    companies = db.query(Company).order_by(Company.name).all()
    return templates.TemplateResponse("htmx/partials/excess/list.html", {
        "request": request,
        "user": user,
        "lists": result["items"],
        "total": result["total"],
        "companies": companies,
        "q": q,
        "status_filter": status or "",
    })
```

- [ ] **Step 4: Test in browser**

Navigate to `/v2/excess`. Verify:
- Page loads with empty state or existing lists
- "New List" modal works
- Search filter works
- Status pills filter correctly

- [ ] **Step 5: Commit**

```bash
git add app/templates/htmx/partials/excess/ app/routers/excess.py
git commit -m "feat(excess): Phase 2.4 — list template with filters + create modal"
```

---

### Task 5: Frontend — Detail Template + Import UI

**Files:**
- Create: `app/templates/htmx/partials/excess/detail.html`
- Create: `app/templates/htmx/partials/excess/line_item_row.html`
- Modify: `app/routers/excess.py` (add detail partial endpoint)

- [ ] **Step 1: Create detail partial**

Create `app/templates/htmx/partials/excess/detail.html` with:
- Breadcrumb: Excess > List Title (OOB swap)
- Header card: title (inline editable), company, status badge, owner, notes
- Status change buttons (draft → active → bidding → closed)
- File upload area: drag-drop or click to select CSV/Excel
  - `hx-post="/api/excess-lists/{{ list.id }}/import"` with `hx-encoding="multipart/form-data"`
  - Shows import result (imported X, skipped Y)
- "Add Line Item" button (opens inline form or modal)
- Line items table: part number, manufacturer, qty, condition, date code, asking price, status, actions
- Each row is a `line_item_row.html` include

- [ ] **Step 2: Create line item row partial**

Create `app/templates/htmx/partials/excess/line_item_row.html`:
- Single `<tr>` with all line item fields
- Status badge (color-coded: green=available, yellow=bidding, blue=awarded, gray=withdrawn)
- Delete button with `hx-confirm`

- [ ] **Step 3: Add detail partial endpoint**

Add to `app/routers/excess.py`:

```python
@router.get("/v2/partials/excess/{list_id}", response_class=HTMLResponse)
async def partial_excess_detail(
    request: Request,
    list_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    el = get_excess_list(db, list_id)
    items = db.query(ExcessLineItem).filter_by(excess_list_id=el.id).order_by(ExcessLineItem.id).all()
    return templates.TemplateResponse("htmx/partials/excess/detail.html", {
        "request": request,
        "user": user,
        "list": el,
        "line_items": items,
    })
```

- [ ] **Step 4: Test in browser**

- Click into an excess list from the list view
- Verify header displays correctly
- Upload a CSV file, verify line items appear
- Add a single line item via the form
- Verify status badges render correctly

- [ ] **Step 5: Commit**

```bash
git add app/templates/htmx/partials/excess/ app/routers/excess.py
git commit -m "feat(excess): Phase 2.5 — detail template with import UI + line items"
```

---

### Task 6: Run Full Test Suite + Deploy

- [ ] **Step 1: Run full test suite**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v`
Expected: All pass, no regressions

- [ ] **Step 2: Commit any remaining changes**

- [ ] **Step 3: Push and deploy**

```bash
git push origin main
docker compose up -d --build
docker compose logs app --tail 20
```

Verify: App starts cleanly, migration runs, `/v2/excess` loads in browser.
