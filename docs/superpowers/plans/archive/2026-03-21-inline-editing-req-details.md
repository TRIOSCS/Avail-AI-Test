# Inline Editing + Req Details Tab Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add inline cell editing for status/qty/price in the parts table, and a "Req Details" tab on the right panel for editing parent requisition fields and bulk-managing sibling parts.

**Architecture:** Reuse the existing inline edit pattern (click cell → GET edit form → PATCH save → swap display cell). Table cells use a new endpoint pair (`/cell/edit/{field}` and `/cell/save`) that returns `<td>` elements instead of header spans. The Req Details tab follows the same tab pattern as offers/sourcing/notes.

**Tech Stack:** FastAPI, HTMX, Alpine.js, Jinja2, Tailwind CSS, SQLAlchemy

---

## File Structure

### New Files
- `app/templates/htmx/partials/parts/cell_edit.html` — Jinja2 template for inline table cell edit inputs
- `app/templates/htmx/partials/parts/cell_display.html` — Jinja2 template for display-mode table cell (after save)
- `app/templates/htmx/partials/parts/tabs/req_details.html` — Req Details tab content

### Modified Files
- `app/routers/htmx_views.py` — New endpoints for cell edit/save and req-details tab
- `app/templates/htmx/partials/parts/list.html` — Make status/qty/price cells clickable for inline edit
- `app/templates/htmx/partials/parts/workspace.html` — Add "Req Details" tab to the tab bar

---

## Task 1: Inline Table Cell Edit — Backend Endpoints

**Files:**
- Modify: `app/routers/htmx_views.py` (add two new endpoints near existing `part_header_edit_cell`)
- Create: `app/templates/htmx/partials/parts/cell_edit.html`
- Create: `app/templates/htmx/partials/parts/cell_display.html`
- Test: `tests/test_inline_cell_edit.py`

### Design Notes

The existing header edit pattern generates HTML inline in Python (f-strings). For table cells, use proper Jinja2 templates instead — cleaner and easier to maintain. The save endpoint reuses the same `part_header_save` logic by calling it internally, or shares the same field validation/coercion code.

**Editable table cells:** `sourcing_status`, `target_qty`, `target_price`

**Cell edit endpoint:** `GET /v2/partials/parts/{id}/cell/edit/{field}` — returns a `<td>` containing an input/select form.

**Cell save endpoint:** `PATCH /v2/partials/parts/{id}/cell` — saves the field, returns the display `<td>`. Also triggers `part-updated` event so the right panel header refreshes if open.

The cell `<td>` needs a stable ID like `cell-{field}-{req_id}` for HTMX targeting. The edit form swaps `outerHTML` on the `<td>`.

- [ ] **Step 1: Write failing tests for cell edit endpoints**

```python
# tests/test_inline_cell_edit.py
"""Tests for inline table cell editing on the parts workspace."""

import pytest
from fastapi.testclient import TestClient

from tests.conftest import engine  # noqa: F401


@pytest.fixture
def part_id(client: TestClient, db_session):
    """Create a requisition with a requirement and return the requirement ID."""
    from app.models.sourcing import Requisition, Requirement
    from datetime import datetime, timezone

    reqn = Requisition(name="Test RFQ", customer_name="Acme", status="active")
    db_session.add(reqn)
    db_session.flush()
    req = Requirement(
        requisition_id=reqn.id,
        primary_mpn="TEST-001",
        brand="TestBrand",
        target_qty=100,
        target_price=10.5000,
        sourcing_status="open",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.commit()
    return req.id


class TestCellEditGet:
    """GET /v2/partials/parts/{id}/cell/edit/{field}"""

    def test_status_returns_select(self, client, part_id):
        resp = client.get(f"/v2/partials/parts/{part_id}/cell/edit/sourcing_status")
        assert resp.status_code == 200
        assert "<select" in resp.text
        assert "open" in resp.text

    def test_qty_returns_number_input(self, client, part_id):
        resp = client.get(f"/v2/partials/parts/{part_id}/cell/edit/target_qty")
        assert resp.status_code == 200
        assert 'type="number"' in resp.text
        assert "100" in resp.text

    def test_price_returns_number_input(self, client, part_id):
        resp = client.get(f"/v2/partials/parts/{part_id}/cell/edit/target_price")
        assert resp.status_code == 200
        assert 'type="number"' in resp.text

    def test_invalid_field_returns_400(self, client, part_id):
        resp = client.get(f"/v2/partials/parts/{part_id}/cell/edit/primary_mpn")
        assert resp.status_code == 400

    def test_missing_part_returns_404(self, client):
        resp = client.get("/v2/partials/parts/99999/cell/edit/target_qty")
        assert resp.status_code == 404


class TestCellSave:
    """PATCH /v2/partials/parts/{id}/cell"""

    def test_save_qty(self, client, part_id, db_session):
        from app.models.sourcing import Requirement

        resp = client.patch(
            f"/v2/partials/parts/{part_id}/cell",
            data={"field": "target_qty", "value": "250"},
        )
        assert resp.status_code == 200
        db_session.expire_all()
        req = db_session.get(Requirement, part_id)
        assert req.target_qty == 250

    def test_save_price(self, client, part_id, db_session):
        from app.models.sourcing import Requirement

        resp = client.patch(
            f"/v2/partials/parts/{part_id}/cell",
            data={"field": "target_price", "value": "25.5000"},
        )
        assert resp.status_code == 200
        db_session.expire_all()
        req = db_session.get(Requirement, part_id)
        assert float(req.target_price) == 25.5

    def test_save_status(self, client, part_id):
        resp = client.patch(
            f"/v2/partials/parts/{part_id}/cell",
            data={"field": "sourcing_status", "value": "sourcing"},
        )
        assert resp.status_code == 200

    def test_save_invalid_field(self, client, part_id):
        resp = client.patch(
            f"/v2/partials/parts/{part_id}/cell",
            data={"field": "primary_mpn", "value": "HACKED"},
        )
        assert resp.status_code == 400

    def test_save_triggers_part_updated(self, client, part_id):
        resp = client.patch(
            f"/v2/partials/parts/{part_id}/cell",
            data={"field": "target_qty", "value": "500"},
        )
        assert "part-updated" in resp.headers.get("HX-Trigger", "")
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_inline_cell_edit.py -v
```

Expected: FAIL — endpoints don't exist yet.

- [ ] **Step 3: Create cell_display.html template**

```html
{# app/templates/htmx/partials/parts/cell_display.html
   Display-mode table cell for an inline-editable field.
   Called by: PATCH /v2/partials/parts/{id}/cell (after save)
   Depends on: requirement, field, cell_id, status_colors dict
#}

{% set status_colors = {
  'open': 'bg-blue-600 text-white',
  'sourcing': 'bg-amber-500 text-white',
  'offered': 'bg-emerald-600 text-white',
  'quoted': 'bg-violet-600 text-white',
  'won': 'bg-emerald-700 text-white',
  'lost': 'bg-gray-400 text-white',
  'archived': 'bg-gray-300 text-gray-600'
} %}

<td id="{{ cell_id }}" @click.stop
    hx-get="/v2/partials/parts/{{ requirement.id }}/cell/edit/{{ field }}"
    hx-target="#{{ cell_id }}" hx-swap="outerHTML"
    class="cursor-pointer hover:bg-brand-50 transition-colors"
    title="Click to edit">
  {% if field == 'sourcing_status' %}
    <span class="badge-primary {{ status_colors.get(requirement.sourcing_status, 'bg-gray-400 text-white') }}">
      {{ requirement.sourcing_status or 'open' }}
    </span>
  {% elif field == 'target_qty' %}
    {{ '{:,}'.format(requirement.target_qty) if requirement.target_qty else '—' }}
  {% elif field == 'target_price' %}
    {{ '${:,.4f}'.format(requirement.target_price) if requirement.target_price else '—' }}
  {% endif %}
</td>
```

- [ ] **Step 4: Create cell_edit.html template**

```html
{# app/templates/htmx/partials/parts/cell_edit.html
   Inline edit input for a table cell.
   Called by: GET /v2/partials/parts/{id}/cell/edit/{field}
   Depends on: requirement, field, cell_id
#}

<td id="{{ cell_id }}" @click.stop class="!p-0.5">
  {% if field == 'sourcing_status' %}
    <select name="value"
            hx-patch="/v2/partials/parts/{{ requirement.id }}/cell"
            hx-target="#{{ cell_id }}" hx-swap="outerHTML"
            hx-vals='{"field": "{{ field }}"}'
            @keydown.escape.prevent="htmx.ajax('GET', '/v2/partials/parts/{{ requirement.id }}/cell/display/{{ field }}', {target: '#{{ cell_id }}', swap: 'outerHTML'})"
            class="text-[11px] px-1 py-0.5 rounded border border-brand-300 focus:ring-1 focus:ring-brand-500"
            x-init="$el.focus()" autofocus>
      {% for s in ['open', 'sourcing', 'offered', 'quoted', 'won', 'lost', 'archived'] %}
        <option value="{{ s }}" {{ 'selected' if s == requirement.sourcing_status }}>{{ s|capitalize }}</option>
      {% endfor %}
    </select>

  {% elif field == 'target_qty' %}
    <input type="number" name="value" value="{{ requirement.target_qty or '' }}"
           hx-patch="/v2/partials/parts/{{ requirement.id }}/cell"
           hx-target="#{{ cell_id }}" hx-swap="outerHTML"
           hx-vals='{"field": "{{ field }}"}'
           hx-trigger="keyup[key=='Enter']"
           @keydown.escape.prevent="htmx.ajax('GET', '/v2/partials/parts/{{ requirement.id }}/cell/display/{{ field }}', {target: '#{{ cell_id }}', swap: 'outerHTML'})"
           class="text-[11px] font-mono px-1 py-0.5 rounded border border-brand-300 focus:ring-1 focus:ring-brand-500 w-16"
           x-init="$el.focus(); $el.select()" autofocus />

  {% elif field == 'target_price' %}
    <input type="number" name="value" step="0.0001"
           value="{{ requirement.target_price if requirement.target_price else '' }}"
           hx-patch="/v2/partials/parts/{{ requirement.id }}/cell"
           hx-target="#{{ cell_id }}" hx-swap="outerHTML"
           hx-vals='{"field": "{{ field }}"}'
           hx-trigger="keyup[key=='Enter']"
           @keydown.escape.prevent="htmx.ajax('GET', '/v2/partials/parts/{{ requirement.id }}/cell/display/{{ field }}', {target: '#{{ cell_id }}', swap: 'outerHTML'})"
           class="text-[11px] font-mono px-1 py-0.5 rounded border border-brand-300 focus:ring-1 focus:ring-brand-500 w-20"
           x-init="$el.focus(); $el.select()" autofocus />
  {% endif %}
</td>
```

- [ ] **Step 5: Add backend endpoints to htmx_views.py**

Add these three endpoints near the existing `part_header_edit_cell` (around line 8887):

```python
# Inline table cell editable fields (subset of header fields)
_CELL_EDITABLE = {"sourcing_status", "target_qty", "target_price"}


@router.get("/v2/partials/parts/{requirement_id}/cell/edit/{field}", response_class=HTMLResponse)
async def part_cell_edit(
    requirement_id: int,
    field: str,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return an inline edit input for a table cell."""
    if field not in _CELL_EDITABLE:
        return HTMLResponse("Invalid field", status_code=400)

    req = db.get(Requirement, requirement_id)
    if not req:
        raise HTTPException(404, "Part not found")

    ctx = _base_ctx(request, user, "requisitions")
    ctx.update({
        "requirement": req,
        "field": field,
        "cell_id": f"cell-{field}-{requirement_id}",
    })
    return templates.TemplateResponse("htmx/partials/parts/cell_edit.html", ctx)


@router.get("/v2/partials/parts/{requirement_id}/cell/display/{field}", response_class=HTMLResponse)
async def part_cell_display(
    requirement_id: int,
    field: str,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return display-mode table cell (cancel handler for escape key)."""
    if field not in _CELL_EDITABLE:
        return HTMLResponse("Invalid field", status_code=400)

    req = db.get(Requirement, requirement_id)
    if not req:
        raise HTTPException(404, "Part not found")

    ctx = _base_ctx(request, user, "requisitions")
    ctx.update({
        "requirement": req,
        "field": field,
        "cell_id": f"cell-{field}-{requirement_id}",
    })
    return templates.TemplateResponse("htmx/partials/parts/cell_display.html", ctx)


@router.patch("/v2/partials/parts/{requirement_id}/cell", response_class=HTMLResponse)
async def part_cell_save(
    requirement_id: int,
    request: Request,
    field: str = Form(...),
    value: str = Form(default=""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Save an inline table cell edit and return the display cell."""
    if field not in _CELL_EDITABLE:
        return HTMLResponse("Invalid field", status_code=400)

    req = db.get(Requirement, requirement_id)
    if not req:
        raise HTTPException(404, "Part not found")

    # Reuse same coercion logic as header save
    if field == "sourcing_status":
        from app.services.requirement_status import transition_requirement
        transition_requirement(req, value, db, user)
    elif field == "target_qty":
        try:
            req.target_qty = int(value) if value else None
        except (ValueError, TypeError):
            req.target_qty = None
    elif field == "target_price":
        from decimal import Decimal, InvalidOperation
        try:
            req.target_price = Decimal(value) if value else None
        except InvalidOperation:
            req.target_price = None

    db.commit()
    logger.info("Part {} cell '{}' updated by {}", requirement_id, field, user.email)

    ctx = _base_ctx(request, user, "requisitions")
    ctx.update({
        "requirement": req,
        "field": field,
        "cell_id": f"cell-{field}-{requirement_id}",
    })
    response = templates.TemplateResponse("htmx/partials/parts/cell_display.html", ctx)
    response.headers["HX-Trigger"] = json.dumps({"part-updated": {"id": requirement_id}})
    return response
```

- [ ] **Step 6: Run tests to verify they pass**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_inline_cell_edit.py -v
```

Expected: All PASS.

- [ ] **Step 7: Commit**

```bash
git add tests/test_inline_cell_edit.py app/routers/htmx_views.py \
  app/templates/htmx/partials/parts/cell_edit.html \
  app/templates/htmx/partials/parts/cell_display.html
git commit -m "feat: add inline table cell edit endpoints for status/qty/price"
```

---

## Task 2: Wire Up Inline Editing in Parts Table

**Files:**
- Modify: `app/templates/htmx/partials/parts/list.html` (change status/qty/price `<td>` cells to use inline edit)

### Design Notes

Replace the three static `<td>` cells for status, qty, and target_price with the `cell_display.html` include pattern.

**IMPORTANT:** The current template opens `<td>` unconditionally at line 149 before the `{% if col_key %}` block. For editable columns, we must restructure so the editable columns output their own complete `<td>...</td>` with id/hx-get attributes. The cleanest approach: split the inner loop so editable columns (`status`, `qty`, `target_price`) have their own `{% elif %}` blocks that output a full `<td id="cell-..." ...>` with a closing `</td>`, and the generic `<td>` opening on line 149 is wrapped in a conditional that skips it for editable columns.

Each editable cell gets:
- A stable `id="cell-{field}-{req.id}"`
- `@click.stop` to prevent row selection when clicking to edit
- `hx-get` to fetch the edit form
- `hx-target` pointing to itself, `hx-swap="outerHTML"`

- [ ] **Step 1: Update status cell in list.html**

Replace the status `<td>` block (around line 158-162) with:

```html
{% elif col_key == 'status' %}
  {% set sc = {'open': 'bg-blue-600 text-white', 'sourcing': 'bg-amber-500 text-white', 'offered': 'bg-emerald-600 text-white', 'quoted': 'bg-violet-600 text-white', 'won': 'bg-emerald-700 text-white', 'lost': 'bg-gray-400 text-white', 'archived': 'bg-gray-300 text-gray-600'} %}
</td>{# close non-editable td #}
<td id="cell-sourcing_status-{{ req.id }}" @click.stop
    hx-get="/v2/partials/parts/{{ req.id }}/cell/edit/sourcing_status"
    hx-target="#cell-sourcing_status-{{ req.id }}" hx-swap="outerHTML"
    class="cursor-pointer hover:bg-brand-50 transition-colors"
    title="Click to edit status">
  <span class="badge-primary {{ sc.get(req.sourcing_status, 'bg-gray-400 text-white') }}">
    {{ req.sourcing_status or 'open' }}
  </span>
```

Note: This requires restructuring the template's `<td>` opening. The current pattern opens `<td>` before the `{% if %}` block. We need to handle editable cells differently — they need their own `<td>` with id/hx-get attributes. The cleanest approach: close the generic `<td>` early and open a new one with the editable attributes for these three fields. Alternatively, restructure the loop so editable cells get their own `<td>` block entirely.

**Recommended approach:** Change the column rendering loop so editable columns (status, qty, target_price) output their own complete `<td>...</td>` with the inline edit attributes, while non-editable columns use the existing generic `<td>`.

- [ ] **Step 2: Update qty cell**

Same pattern — add `id="cell-target_qty-{{ req.id }}"`, `@click.stop`, `hx-get`, `hx-target`, `hx-swap="outerHTML"`.

- [ ] **Step 3: Update target_price cell**

Same pattern for target_price.

- [ ] **Step 4: Test in browser**

1. Load the Reqs tab
2. Click a status badge in the table — should become a dropdown
3. Change status — should save and show updated badge
4. Click qty — should become number input
5. Press Enter — should save
6. Press Escape — should cancel
7. Verify right panel header updates when cell is saved (via `part-updated` event)

- [ ] **Step 5: Commit**

```bash
git add app/templates/htmx/partials/parts/list.html
git commit -m "feat: wire inline editing for status/qty/price in parts table"
```

---

## Task 3: Req Details Tab — Backend Endpoint

**Files:**
- Modify: `app/routers/htmx_views.py` (add req-details tab endpoint)
- Create: `app/templates/htmx/partials/parts/tabs/req_details.html`
- Test: `tests/test_req_details_tab.py`

### Design Notes

The tab shows the parent requisition's editable fields and a list of all sibling parts on the same requisition. Reuses the existing requisition inline edit infrastructure (`/v2/partials/requisitions/{id}/edit/{field}` and `PATCH /v2/partials/requisitions/{id}/inline`).

The edit flow: click a field → GET the inline edit form from the existing requisition edit endpoint (with `context=tab`) → PATCH saves → returns refreshed field display. We use `context=tab` so the save response can return just the field cell, not the whole detail page.

**IMPORTANT:** The existing `requisition_inline_save` endpoint only handles `context=row` and `context=header`. A new `context=tab` branch must be added that returns just the individual field's display `<div>` (matching the `#reqd-{field}` target), not the full detail page or row template.

**Editable requisition fields shown:** name, status, urgency, deadline, owner.

**Sibling parts section:** A compact table showing all parts on the same requisition with checkboxes for bulk status change and bulk archive.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_req_details_tab.py
"""Tests for the Req Details tab on the parts workspace."""

import pytest
from fastapi.testclient import TestClient
from datetime import datetime, timezone

from tests.conftest import engine  # noqa: F401


@pytest.fixture
def req_with_parts(client: TestClient, db_session):
    """Create a requisition with multiple requirements."""
    from app.models.sourcing import Requisition, Requirement

    reqn = Requisition(
        name="Test RFQ",
        customer_name="Acme Corp",
        status="active",
        urgency="normal",
    )
    db_session.add(reqn)
    db_session.flush()
    parts = []
    for mpn in ["MPN-001", "MPN-002", "MPN-003"]:
        req = Requirement(
            requisition_id=reqn.id,
            primary_mpn=mpn,
            brand="TestBrand",
            target_qty=100,
            sourcing_status="open",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        parts.append(req)
    db_session.commit()
    return {"requisition": reqn, "parts": parts}


class TestReqDetailsTab:
    """GET /v2/partials/parts/{id}/tab/req-details"""

    def test_returns_200(self, client, req_with_parts):
        part_id = req_with_parts["parts"][0].id
        resp = client.get(f"/v2/partials/parts/{part_id}/tab/req-details")
        assert resp.status_code == 200

    def test_shows_requisition_name(self, client, req_with_parts):
        part_id = req_with_parts["parts"][0].id
        resp = client.get(f"/v2/partials/parts/{part_id}/tab/req-details")
        assert "Test RFQ" in resp.text

    def test_shows_sibling_parts(self, client, req_with_parts):
        part_id = req_with_parts["parts"][0].id
        resp = client.get(f"/v2/partials/parts/{part_id}/tab/req-details")
        assert "MPN-001" in resp.text
        assert "MPN-002" in resp.text
        assert "MPN-003" in resp.text

    def test_shows_customer(self, client, req_with_parts):
        part_id = req_with_parts["parts"][0].id
        resp = client.get(f"/v2/partials/parts/{part_id}/tab/req-details")
        assert "Acme Corp" in resp.text

    def test_missing_part_returns_404(self, client):
        resp = client.get("/v2/partials/parts/99999/tab/req-details")
        assert resp.status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_req_details_tab.py -v
```

Expected: FAIL — endpoint doesn't exist.

- [ ] **Step 3: Add the req-details tab endpoint**

Add to `app/routers/htmx_views.py` near the other tab endpoints:

```python
@router.get("/v2/partials/parts/{requirement_id}/tab/req-details", response_class=HTMLResponse)
async def part_tab_req_details(
    requirement_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return the Req Details tab showing parent requisition fields and sibling parts."""
    req = db.get(Requirement, requirement_id)
    if not req:
        raise HTTPException(404, "Part not found")

    requisition = req.requisition
    sibling_parts = (
        db.query(Requirement)
        .filter(Requirement.requisition_id == requisition.id)
        .order_by(Requirement.primary_mpn)
        .all()
    )
    users_list = db.query(User).filter(User.is_active.is_(True)).order_by(User.name).all()

    ctx = _base_ctx(request, user, "requisitions")
    ctx.update({
        "requirement": req,
        "requisition": requisition,
        "sibling_parts": sibling_parts,
        "users": users_list,
    })
    return templates.TemplateResponse("htmx/partials/parts/tabs/req_details.html", ctx)
```

- [ ] **Step 4: Create the req_details.html template**

```html
{# app/templates/htmx/partials/parts/tabs/req_details.html
   Req Details tab — edit parent requisition fields + manage sibling parts.
   Called by: GET /v2/partials/parts/{id}/tab/req-details
   Depends on: requisition, sibling_parts, users, requirement (current part)
#}

{% set req_status_colors = {
  'draft': 'bg-gray-100 text-gray-600',
  'active': 'bg-blue-100 text-blue-700',
  'sourcing': 'bg-amber-100 text-amber-700',
  'offers': 'bg-emerald-100 text-emerald-700',
  'quoting': 'bg-violet-100 text-violet-700',
  'quoted': 'bg-violet-200 text-violet-800',
  'won': 'bg-emerald-200 text-emerald-800',
  'lost': 'bg-gray-200 text-gray-600',
  'archived': 'bg-gray-300 text-gray-600'
} %}

<div class="space-y-4" x-data="{ selectedSiblings: [] }">

  {# ── Requisition Fields ─────────────────────────────────── #}
  <div class="space-y-2">
    <h3 class="text-[10px] font-semibold uppercase tracking-wide text-gray-400">Requisition</h3>

    <div class="grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
      {# Name — click to edit #}
      <div>
        <span class="text-[10px] text-gray-400 uppercase tracking-wide">Name</span>
        <div id="reqd-name"
             hx-get="/v2/partials/requisitions/{{ requisition.id }}/edit/name?context=tab"
             hx-target="#reqd-name" hx-swap="innerHTML"
             class="text-sm font-medium text-gray-900 cursor-pointer hover:text-brand-500"
             title="Click to edit">
          {{ requisition.name or '—' }}
        </div>
      </div>

      {# Status — click to edit #}
      <div>
        <span class="text-[10px] text-gray-400 uppercase tracking-wide">Status</span>
        <div id="reqd-status"
             hx-get="/v2/partials/requisitions/{{ requisition.id }}/edit/status?context=tab"
             hx-target="#reqd-status" hx-swap="innerHTML"
             class="cursor-pointer"
             title="Click to edit">
          <span class="badge-primary {{ req_status_colors.get(requisition.status, 'bg-gray-100 text-gray-600') }}">
            {{ requisition.status or 'draft' }}
          </span>
        </div>
      </div>

      {# Customer — read-only #}
      <div>
        <span class="text-[10px] text-gray-400 uppercase tracking-wide">Customer</span>
        <div class="text-sm text-gray-900">{{ requisition.customer_name or '—' }}</div>
      </div>

      {# Urgency — click to edit #}
      <div>
        <span class="text-[10px] text-gray-400 uppercase tracking-wide">Urgency</span>
        <div id="reqd-urgency"
             hx-get="/v2/partials/requisitions/{{ requisition.id }}/edit/urgency?context=tab"
             hx-target="#reqd-urgency" hx-swap="innerHTML"
             class="text-sm cursor-pointer hover:text-brand-500"
             title="Click to edit">
          {% if requisition.urgency == 'hot' %}
            <span class="text-amber-600 font-semibold">Hot</span>
          {% elif requisition.urgency == 'critical' %}
            <span class="text-red-600 font-semibold">Critical</span>
          {% else %}
            <span class="text-gray-600">Normal</span>
          {% endif %}
        </div>
      </div>

      {# Deadline — click to edit #}
      <div>
        <span class="text-[10px] text-gray-400 uppercase tracking-wide">Deadline</span>
        <div id="reqd-deadline"
             hx-get="/v2/partials/requisitions/{{ requisition.id }}/edit/deadline?context=tab"
             hx-target="#reqd-deadline" hx-swap="innerHTML"
             class="text-sm text-gray-900 cursor-pointer hover:text-brand-500"
             title="Click to edit">
          {{ requisition.deadline or '—' }}
        </div>
      </div>

      {# Owner — click to edit #}
      <div>
        <span class="text-[10px] text-gray-400 uppercase tracking-wide">Owner</span>
        <div id="reqd-owner"
             hx-get="/v2/partials/requisitions/{{ requisition.id }}/edit/owner?context=tab"
             hx-target="#reqd-owner" hx-swap="innerHTML"
             class="text-sm text-gray-900 cursor-pointer hover:text-brand-500"
             title="Click to edit">
          {% if requisition.claimed_by %}
            {{ requisition.claimed_by.name }}
          {% elif requisition.creator %}
            {{ requisition.creator.name }}
          {% else %}
            —
          {% endif %}
        </div>
      </div>
    </div>
  </div>

  {# ── Sibling Parts ──────────────────────────────────────── #}
  <div>
    <div class="flex items-center justify-between mb-1">
      <h3 class="text-[10px] font-semibold uppercase tracking-wide text-gray-400">
        Parts on this Req ({{ sibling_parts|length }})
      </h3>
      {# Bulk actions — visible when siblings selected #}
      <div x-show="selectedSiblings.length > 0" class="flex items-center gap-2">
        <span class="text-[10px] text-brand-600 font-semibold" x-text="selectedSiblings.length + ' selected'"></span>
        <button type="button"
                hx-post="/v2/partials/parts/bulk-archive"
                hx-target="#parts-list"
                hx-swap="innerHTML"
                :hx-vals="JSON.stringify({ids: selectedSiblings.join(',')})"
                class="px-2 py-0.5 text-[10px] font-semibold bg-gray-500 text-white rounded hover:bg-gray-600">
          Archive
        </button>
      </div>
    </div>

    <table class="w-full text-xs">
      <thead>
        <tr class="text-[10px] text-gray-400 uppercase tracking-wide border-b border-gray-200">
          <th class="w-5 py-1"><input type="checkbox"
              class="h-3 w-3 rounded border-gray-300 text-brand-500"
              @change="selectedSiblings = $event.target.checked ? {{ sibling_parts|map(attribute='id')|list|tojson }} : []"></th>
          <th class="py-1 text-left">MPN</th>
          <th class="py-1 text-left">Status</th>
          <th class="py-1 text-right">Qty</th>
        </tr>
      </thead>
      <tbody>
        {% for part in sibling_parts %}
        <tr class="border-b border-gray-100 {{ 'bg-brand-50' if part.id == requirement.id }}">
          <td class="py-1">
            <input type="checkbox" class="h-3 w-3 rounded border-gray-300 text-brand-500"
                   :checked="selectedSiblings.includes({{ part.id }})"
                   @change="$event.target.checked ? selectedSiblings.push({{ part.id }}) : selectedSiblings = selectedSiblings.filter(id => id !== {{ part.id }})">
          </td>
          <td class="py-1 font-mono font-medium {{ 'text-brand-600' if part.id == requirement.id else 'text-gray-900' }}">
            {{ part.primary_mpn or '—' }}
          </td>
          <td class="py-1">
            {% set sc = {'open': 'bg-blue-600 text-white', 'sourcing': 'bg-amber-500 text-white', 'offered': 'bg-emerald-600 text-white', 'quoted': 'bg-violet-600 text-white', 'won': 'bg-emerald-700 text-white', 'lost': 'bg-gray-400 text-white', 'archived': 'bg-gray-300 text-gray-600'} %}
            <span class="badge-primary {{ sc.get(part.sourcing_status, 'bg-gray-400 text-white') }}" style="font-size: 9px;">
              {{ part.sourcing_status or 'open' }}
            </span>
          </td>
          <td class="py-1 text-right tabular-nums">{{ '{:,}'.format(part.target_qty) if part.target_qty else '—' }}</td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
</div>
```

- [ ] **Step 5: Run tests**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_req_details_tab.py -v
```

Expected: All PASS.

- [ ] **Step 6: Commit**

```bash
git add app/routers/htmx_views.py \
  app/templates/htmx/partials/parts/tabs/req_details.html \
  tests/test_req_details_tab.py
git commit -m "feat: add Req Details tab with requisition editing and sibling parts"
```

---

## Task 4: Add Req Details Tab to Workspace UI

**Files:**
- Modify: `app/templates/htmx/partials/parts/workspace.html` (add tab button)

- [ ] **Step 1: Add "Req Details" to the tab bar**

In `workspace.html`, the tab bar is at line 81. Add `('req-details', 'Req Details')` to the tab list:

```html
{% for tab_key, tab_label in [('offers', 'Offers'), ('sourcing', 'Sourcing'), ('notes', 'Sales Notes'), ('activity', 'Activity'), ('comms', 'Comms'), ('req-details', 'Req Details')] %}
```

- [ ] **Step 2: Test in browser**

1. Load the Reqs tab
2. Click a part in the left panel
3. Click the "Req Details" tab
4. Verify requisition fields are shown with correct values
5. Click a field (name, status, urgency) — verify inline edit works
6. Verify sibling parts are listed with the current part highlighted
7. Select sibling parts and verify bulk archive button appears

- [ ] **Step 3: Commit**

```bash
git add app/templates/htmx/partials/parts/workspace.html
git commit -m "feat: add Req Details tab to workspace tab bar"
```

---

## Task 5: Integration Testing & Edge Cases

**Files:**
- Test: `tests/test_inline_cell_edit.py` (extend with edge cases)
- Test: `tests/test_req_details_tab.py` (extend with edge cases)

- [ ] **Step 1: Add edge case tests**

```python
# Add to tests/test_inline_cell_edit.py

class TestCellEditEdgeCases:
    def test_save_empty_qty_sets_null(self, client, part_id, db_session):
        from app.models.sourcing import Requirement

        resp = client.patch(
            f"/v2/partials/parts/{part_id}/cell",
            data={"field": "target_qty", "value": ""},
        )
        assert resp.status_code == 200
        db_session.expire_all()
        req = db_session.get(Requirement, part_id)
        assert req.target_qty is None

    def test_save_invalid_qty_sets_null(self, client, part_id, db_session):
        from app.models.sourcing import Requirement

        resp = client.patch(
            f"/v2/partials/parts/{part_id}/cell",
            data={"field": "target_qty", "value": "abc"},
        )
        assert resp.status_code == 200
        db_session.expire_all()
        req = db_session.get(Requirement, part_id)
        assert req.target_qty is None

    def test_save_invalid_price_sets_null(self, client, part_id, db_session):
        from app.models.sourcing import Requirement

        resp = client.patch(
            f"/v2/partials/parts/{part_id}/cell",
            data={"field": "target_price", "value": "not-a-number"},
        )
        assert resp.status_code == 200
        db_session.expire_all()
        req = db_session.get(Requirement, part_id)
        assert req.target_price is None

    def test_display_endpoint_returns_cell(self, client, part_id):
        resp = client.get(f"/v2/partials/parts/{part_id}/cell/display/sourcing_status")
        assert resp.status_code == 200
        assert "<td" in resp.text
```

- [ ] **Step 2: Run full test suite for affected areas**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_inline_cell_edit.py tests/test_req_details_tab.py tests/test_archive_system.py tests/test_part_header.py -v
```

Expected: All PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/test_inline_cell_edit.py tests/test_req_details_tab.py
git commit -m "test: add edge case tests for inline cell editing and req details tab"
```

---

## Dependency Graph

```
Task 1 (cell edit endpoints) ──┬── Task 2 (wire up in table)
                                │
Task 3 (req details tab) ──────┤── Task 4 (add tab to workspace)
                                │
                                └── Task 5 (integration + edge cases)
```

Tasks 1 and 3 are independent and can run in parallel. Tasks 2 and 4 depend on 1 and 3 respectively. Task 5 depends on all prior tasks.
