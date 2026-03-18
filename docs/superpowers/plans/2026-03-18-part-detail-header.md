# Part Detail Header + Inline Editing — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a persistent part detail header above tabs in the requisitions workspace right panel, with inline editing for key Requirement fields.

**Architecture:** New `#part-header` div inside the `selectedPartId` conditional in workspace.html. Three thin routes in htmx_views.py (display, edit cell, save) following the requisition inline edit pattern at line 954. One new template `parts/header.html`. Tab h3 headings removed as redundant.

**Tech Stack:** FastAPI, SQLAlchemy 2.0, Jinja2, HTMX, Alpine.js, Tailwind CSS

**Spec:** `docs/superpowers/specs/2026-03-18-part-detail-header-design.md`

---

## File Map

| Action | File | Responsibility |
|--------|------|---------------|
| Create | `app/templates/htmx/partials/parts/header.html` | Header template — display + inline edit cells |
| Modify | `app/templates/htmx/partials/parts/workspace.html` | Add `#part-header`, update `selectPart()` to preserve tab |
| Modify | `app/routers/htmx_views.py` | 3 new routes: header display, edit cell, inline save |
| Modify | `app/templates/htmx/partials/parts/tabs/offers.html` | Remove redundant h3 heading |
| Modify | `app/templates/htmx/partials/parts/tabs/sourcing.html` | Remove redundant h3 heading (keep Run Search button) |
| Modify | `app/templates/htmx/partials/parts/tabs/activity.html` | Remove redundant h3 heading |
| Modify | `app/templates/htmx/partials/parts/tabs/comms.html` | Remove redundant h3 heading |
| Create | `tests/test_part_header.py` | 7 endpoint tests |

---

### Task 1: Create Header Template + Display Endpoint

**Files:**
- Create: `app/templates/htmx/partials/parts/header.html`
- Modify: `app/routers/htmx_views.py` (add after `part_tab_sourcing` endpoint, ~line 7280)

- [ ] **Step 1: Create the header template**

```html
{# Part detail header — persistent context strip above tabs.
   Called by: GET /v2/partials/parts/{id}/header
   Depends on: requirement (Requirement model with joined Requisition)
#}

{% set status_colors = {
  'open': 'bg-blue-100 text-blue-700',
  'sourcing': 'bg-amber-100 text-amber-700',
  'offered': 'bg-emerald-100 text-emerald-700',
  'quoted': 'bg-violet-100 text-violet-700',
  'won': 'bg-emerald-200 text-emerald-800',
  'lost': 'bg-gray-200 text-gray-600',
  'archived': 'bg-gray-300 text-gray-600'
} %}

<div class="px-3 py-2 border-b border-gray-200 bg-white flex-shrink-0">
  <div class="flex items-center justify-between gap-4">

    {# Left: MPN + Brand + Requisition context #}
    <div class="min-w-0">
      <div class="flex items-baseline gap-2">
        <span class="text-lg font-bold text-gray-900 truncate">{{ requirement.primary_mpn or '—' }}</span>
        {# Brand — inline editable #}
        <span id="hdr-brand"
              hx-get="/v2/partials/parts/{{ requirement.id }}/header/edit/brand"
              hx-target="#hdr-brand" hx-swap="outerHTML"
              class="text-sm text-gray-500 cursor-pointer hover:text-brand-500 truncate"
              title="Click to edit brand">
          {{ requirement.brand or 'No brand' }}
        </span>
      </div>
      <p class="text-xs text-gray-400 truncate mt-0.5">
        {{ requirement.requisition.name if requirement.requisition else '—' }}
        {% if requirement.requisition and requirement.requisition.customer_name %}
          · {{ requirement.requisition.customer_name }}
        {% endif %}
      </p>
    </div>

    {# Right: Status + Condition + Qty + Price #}
    <div class="flex items-center gap-3 flex-shrink-0">

      {# Status badge — inline editable #}
      <span id="hdr-sourcing_status"
            hx-get="/v2/partials/parts/{{ requirement.id }}/header/edit/sourcing_status"
            hx-target="#hdr-sourcing_status" hx-swap="outerHTML"
            class="px-2 py-0.5 text-xs font-medium rounded-full cursor-pointer hover:ring-1 hover:ring-brand-300
                   {{ status_colors.get(requirement.sourcing_status, 'bg-gray-100 text-gray-600') }}"
            title="Click to change status">
        {{ (requirement.sourcing_status or 'open')|capitalize }}
      </span>

      {# Condition — inline editable #}
      <span id="hdr-condition"
            hx-get="/v2/partials/parts/{{ requirement.id }}/header/edit/condition"
            hx-target="#hdr-condition" hx-swap="outerHTML"
            class="text-xs text-gray-500 cursor-pointer hover:text-brand-500"
            title="Click to edit condition">
        {{ requirement.condition or '—' }}
      </span>

      {# Target Qty — inline editable #}
      <div class="text-right">
        <span id="hdr-target_qty"
              hx-get="/v2/partials/parts/{{ requirement.id }}/header/edit/target_qty"
              hx-target="#hdr-target_qty" hx-swap="outerHTML"
              class="text-sm font-medium text-gray-900 cursor-pointer hover:text-brand-500"
              title="Click to edit qty">
          {{ '{:,}'.format(requirement.target_qty) if requirement.target_qty else '—' }}
        </span>
        <span class="text-[10px] text-gray-400 block">qty</span>
      </div>

      {# Target Price — inline editable #}
      <div class="text-right">
        <span id="hdr-target_price"
              hx-get="/v2/partials/parts/{{ requirement.id }}/header/edit/target_price"
              hx-target="#hdr-target_price" hx-swap="outerHTML"
              class="text-sm font-medium text-gray-900 cursor-pointer hover:text-brand-500"
              title="Click to edit target price">
          {{ '${:,.4f}'.format(requirement.target_price) if requirement.target_price else '—' }}
        </span>
        <span class="text-[10px] text-gray-400 block">target</span>
      </div>

    </div>
  </div>
</div>
```

- [ ] **Step 2: Add the display header endpoint**

Add to `app/routers/htmx_views.py` after the `part_tab_sourcing` endpoint:

```python
@router.get("/v2/partials/parts/{requirement_id}/header", response_class=HTMLResponse)
async def part_header(
    requirement_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return the part detail header strip (display mode)."""
    req = db.get(Requirement, requirement_id)
    if not req:
        raise HTTPException(404, "Part not found")

    ctx = _base_ctx(request, user, "requisitions")
    ctx["requirement"] = req
    return templates.TemplateResponse("htmx/partials/parts/header.html", ctx)
```

- [ ] **Step 3: Run tests to verify no regressions**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_archive_system.py tests/test_scoring_helpers.py -q --tb=short`
Expected: All pass (existing tests unaffected)

- [ ] **Step 4: Commit**

```bash
git add app/templates/htmx/partials/parts/header.html app/routers/htmx_views.py
git commit -m "feat: add part detail header template and display endpoint"
```

---

### Task 2: Add Edit Cell + Save Endpoints

**Files:**
- Modify: `app/routers/htmx_views.py` (add after the header display endpoint)

- [ ] **Step 1: Add the edit cell endpoint**

```python
_PART_HEADER_EDITABLE = {"brand", "target_qty", "target_price", "condition", "sourcing_status", "notes", "date_codes", "packaging"}

_CONDITION_CHOICES = ["New", "Used", "Refurbished", "Any"]


@router.get("/v2/partials/parts/{requirement_id}/header/edit/{field}", response_class=HTMLResponse)
async def part_header_edit_cell(
    requirement_id: int,
    field: str,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return an inline edit input for a single header field."""
    if field not in _PART_HEADER_EDITABLE:
        return HTMLResponse("Invalid field", status_code=400)

    req = db.get(Requirement, requirement_id)
    if not req:
        raise HTTPException(404, "Part not found")

    current = getattr(req, field, "") or ""
    cell_id = f"hdr-{field}"
    cancel_url = f"/v2/partials/parts/{requirement_id}/header"
    save_url = f"/v2/partials/parts/{requirement_id}/header"

    # Status dropdown
    if field == "sourcing_status":
        statuses = ["open", "sourcing", "offered", "quoted", "won", "lost", "archived"]
        options = "".join(
            f'<option value="{s}" {"selected" if s == current else ""}>{s.capitalize()}</option>'
            for s in statuses
        )
        return HTMLResponse(
            f'<select name="value" id="{cell_id}" '
            f'hx-patch="{save_url}" hx-target="closest #part-header-wrap" hx-swap="innerHTML" '
            f'hx-vals=\'{{"field": "{field}"}}\' '
            f'class="text-xs px-1.5 py-0.5 rounded border border-brand-300 focus:ring-1 focus:ring-brand-500" '
            f'@keydown.escape="htmx.ajax(\'GET\', \'{cancel_url}\', {{target: \'#part-header-wrap\', swap: \'innerHTML\'}})">'
            f'{options}</select>',
            status_code=200,
        )

    # Condition dropdown
    if field == "condition":
        options = "".join(
            f'<option value="{c}" {"selected" if c == current else ""}>{c}</option>'
            for c in _CONDITION_CHOICES
        )
        return HTMLResponse(
            f'<select name="value" id="{cell_id}" '
            f'hx-patch="{save_url}" hx-target="closest #part-header-wrap" hx-swap="innerHTML" '
            f'hx-vals=\'{{"field": "{field}"}}\' '
            f'class="text-xs px-1.5 py-0.5 rounded border border-brand-300 focus:ring-1 focus:ring-brand-500" '
            f'@keydown.escape="htmx.ajax(\'GET\', \'{cancel_url}\', {{target: \'#part-header-wrap\', swap: \'innerHTML\'}})">'
            f'{options}</select>',
            status_code=200,
        )

    # Number fields
    input_type = "number" if field in ("target_qty", "target_price") else "text"
    step = ' step="0.0001"' if field == "target_price" else ""

    return HTMLResponse(
        f'<input type="{input_type}" name="value" id="{cell_id}" value="{current}" '
        f'hx-patch="{save_url}" hx-target="closest #part-header-wrap" hx-swap="innerHTML" '
        f'hx-vals=\'{{"field": "{field}"}}\' '
        f'hx-trigger="keyup[key==\'Enter\']" '
        f'@keydown.escape="htmx.ajax(\'GET\', \'{cancel_url}\', {{target: \'#part-header-wrap\', swap: \'innerHTML\'}})" '
        f'class="text-sm px-1.5 py-0.5 rounded border border-brand-300 focus:ring-1 focus:ring-brand-500 w-24"'
        f'{step} autofocus />',
        status_code=200,
    )
```

- [ ] **Step 2: Add the save endpoint**

```python
@router.patch("/v2/partials/parts/{requirement_id}/header", response_class=HTMLResponse)
async def part_header_save(
    requirement_id: int,
    request: Request,
    field: str = Form(...),
    value: str = Form(default=""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Save an inline header field edit and return the refreshed header."""
    if field not in _PART_HEADER_EDITABLE:
        return HTMLResponse("Invalid field", status_code=400)

    req = db.get(Requirement, requirement_id)
    if not req:
        raise HTTPException(404, "Part not found")

    # Status changes go through the transition service for validation + activity log
    if field == "sourcing_status":
        from app.services.requirement_status import transition_requirement

        ok = transition_requirement(req, value, db, user)
        if not ok:
            logger.warning("Status transition rejected: {} → {} for part {}", req.sourcing_status, value, requirement_id)
    elif field == "target_qty":
        req.target_qty = int(value) if value else None
    elif field == "target_price":
        from decimal import Decimal, InvalidOperation

        try:
            req.target_price = Decimal(value) if value else None
        except InvalidOperation:
            req.target_price = None
    else:
        setattr(req, field, value.strip() if value else None)

    db.commit()
    logger.info("Part {} header field '{}' updated by {}", requirement_id, field, user.email)

    ctx = _base_ctx(request, user, "requisitions")
    ctx["requirement"] = req
    response = templates.TemplateResponse("htmx/partials/parts/header.html", ctx)
    response.headers["HX-Trigger"] = json.dumps({"part-updated": {"id": requirement_id}})
    return response
```

- [ ] **Step 3: Run quick test**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ -q --tb=short -x` (stop on first failure)
Expected: All pass

- [ ] **Step 4: Commit**

```bash
git add app/routers/htmx_views.py
git commit -m "feat: add part header inline edit cell + save endpoints"
```

---

### Task 3: Wire Header into Workspace + Tab Memory

**Files:**
- Modify: `app/templates/htmx/partials/parts/workspace.html`

- [ ] **Step 1: Update workspace template**

Three changes in `workspace.html`:

**A) Update `selectPart()` to preserve active tab** (line 33–36):

Replace:
```javascript
selectPart(id) {
  this.selectedPartId = id;
  this.activeTab = 'offers';
},
```

With:
```javascript
selectPart(id) {
  this.selectedPartId = id;
  if (!this.activeTab) this.activeTab = 'offers';
},
```

**B) Add `#part-header-wrap` above the tab bar** (inside the `<template x-if="selectedPartId">` at line 73):

Replace lines 73-85:
```html
<template x-if="selectedPartId">
  <div class="border-b-2 border-brand-200 bg-gray-50/80 px-2 flex-shrink-0">
    <nav class="flex -mb-[2px]">
```

With:
```html
<template x-if="selectedPartId">
  <div>
    {# Part header — persistent context strip #}
    <div id="part-header-wrap"
         hx-get :hx-vals="JSON.stringify({})"
         x-init="htmx.ajax('GET', '/v2/partials/parts/' + selectedPartId + '/header', {target: '#part-header-wrap', swap: 'innerHTML'})"
         x-effect="if (selectedPartId) htmx.ajax('GET', '/v2/partials/parts/' + selectedPartId + '/header', {target: '#part-header-wrap', swap: 'innerHTML'})">
    </div>
    {# Tab bar #}
    <div class="border-b-2 border-brand-200 bg-gray-50/80 px-2 flex-shrink-0">
      <nav class="flex -mb-[2px]">
```

**C) Close the extra wrapper div** — after the `</nav></div>` (line 84), add `</div>` to close the wrapper:

After line 85 (`</template>`), the structure should be:
```html
      </nav>
    </div>
  </div>
</template>
```

- [ ] **Step 2: Verify the page loads correctly**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ -q --tb=short -x`
Expected: All pass

- [ ] **Step 3: Commit**

```bash
git add app/templates/htmx/partials/parts/workspace.html
git commit -m "feat: wire part header into workspace, preserve active tab on part switch"
```

---

### Task 4: Remove Redundant Tab Headings

**Files:**
- Modify: `app/templates/htmx/partials/parts/tabs/offers.html` (lines 7-12)
- Modify: `app/templates/htmx/partials/parts/tabs/sourcing.html` (lines 7-11)
- Modify: `app/templates/htmx/partials/parts/tabs/activity.html` (lines 7-10)
- Modify: `app/templates/htmx/partials/parts/tabs/comms.html` (lines 7-9)

- [ ] **Step 1: Remove h3 heading from offers.html**

Replace lines 7-12:
```html
  <div class="flex items-center justify-between mb-3">
    <div>
      <h3 class="text-lg font-semibold text-gray-900">{{ requirement.primary_mpn or 'Part' }}</h3>
      <p class="text-xs text-gray-500">{{ requirement.requisition.name if requirement.requisition else '' }} · {{ offers|length }} offer{{ 's' if offers|length != 1 else '' }}</p>
    </div>
  </div>
```

With:
```html
  <div class="flex items-center justify-between mb-2">
    <p class="text-xs text-gray-500">{{ offers|length }} offer{{ 's' if offers|length != 1 else '' }}</p>
  </div>
```

- [ ] **Step 2: Remove h3 heading from sourcing.html**

Replace lines 7-11:
```html
  <div class="flex items-center justify-between mb-3">
    <div>
      <h3 class="text-lg font-semibold text-gray-900">Sourcing — {{ requirement.primary_mpn or 'Part' }}</h3>
      <p class="text-xs text-gray-500">{{ summaries|length }} vendor{{ 's' if summaries|length != 1 else '' }}</p>
    </div>
```

With:
```html
  <div class="flex items-center justify-between mb-2">
    <p class="text-xs text-gray-500">{{ summaries|length }} vendor{{ 's' if summaries|length != 1 else '' }}</p>
```

Note: Keep the "Run Search" button that follows this section.

- [ ] **Step 3: Remove h3 heading from activity.html**

Replace lines 7-10:
```html
  <div class="mb-3">
    <h3 class="text-lg font-semibold text-gray-900">Activity — {{ requirement.primary_mpn or 'Part' }}</h3>
    <p class="text-xs text-gray-500">{{ activities|length }} event{{ 's' if activities|length != 1 else '' }}</p>
  </div>
```

With:
```html
  <div class="mb-2">
    <p class="text-xs text-gray-500">{{ activities|length }} event{{ 's' if activities|length != 1 else '' }}</p>
  </div>
```

- [ ] **Step 4: Remove h3 heading from comms.html**

Replace lines 7-9:
```html
  <div class="mb-4">
    <h3 class="text-lg font-semibold text-gray-900">Communications — {{ requirement.primary_mpn or 'Part' }}</h3>
  </div>
```

With nothing (remove entirely — the create task form immediately follows).

- [ ] **Step 5: Run full test suite**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ -q --tb=short`
Expected: All pass

- [ ] **Step 6: Commit**

```bash
git add app/templates/htmx/partials/parts/tabs/offers.html app/templates/htmx/partials/parts/tabs/sourcing.html app/templates/htmx/partials/parts/tabs/activity.html app/templates/htmx/partials/parts/tabs/comms.html
git commit -m "refactor: remove redundant h3 headings from tab templates — header provides context"
```

---

### Task 5: Write Tests

**Files:**
- Create: `tests/test_part_header.py`

- [ ] **Step 1: Create test file**

```python
# tests/test_part_header.py
"""Tests for part detail header endpoints.

Called by: pytest
Depends on: app.routers.htmx_views part_header, part_header_edit_cell, part_header_save
"""
from tests.conftest import engine  # noqa: F401


def _make_req_and_part(db_session, mpn="LM358", sourcing_status="open"):
    from app.models.sourcing import Requisition, Requirement

    req = Requisition(name="Test RFQ", customer_name="Acme", status="active")
    db_session.add(req)
    db_session.flush()
    part = Requirement(
        requisition_id=req.id,
        primary_mpn=mpn,
        brand="Texas Instruments",
        target_qty=1000,
        target_price=1.50,
        condition="New",
        sourcing_status=sourcing_status,
    )
    db_session.add(part)
    db_session.commit()
    return req, part


def test_get_part_header(client, db_session):
    """GET /v2/partials/parts/{id}/header returns 200 with part info."""
    _, part = _make_req_and_part(db_session)
    resp = client.get(f"/v2/partials/parts/{part.id}/header")
    assert resp.status_code == 200
    assert "LM358" in resp.text
    assert "1,000" in resp.text or "1000" in resp.text
    assert "Texas Instruments" in resp.text


def test_get_part_header_not_found(client, db_session):
    """GET /v2/partials/parts/99999/header returns 404."""
    resp = client.get("/v2/partials/parts/99999/header")
    assert resp.status_code == 404


def test_edit_cell_returns_input(client, db_session):
    """GET edit/{field} returns an input or select element."""
    _, part = _make_req_and_part(db_session)
    resp = client.get(f"/v2/partials/parts/{part.id}/header/edit/brand")
    assert resp.status_code == 200
    assert "input" in resp.text.lower() or "select" in resp.text.lower()


def test_edit_cell_invalid_field(client, db_session):
    """GET edit/bogus returns 400."""
    _, part = _make_req_and_part(db_session)
    resp = client.get(f"/v2/partials/parts/{part.id}/header/edit/bogus_field")
    assert resp.status_code == 400


def test_patch_header_updates_field(client, db_session):
    """PATCH saves target_qty and returns updated header."""
    _, part = _make_req_and_part(db_session)
    resp = client.patch(
        f"/v2/partials/parts/{part.id}/header",
        data={"field": "target_qty", "value": "5000"},
    )
    assert resp.status_code == 200
    db_session.refresh(part)
    assert part.target_qty == 5000


def test_patch_header_status_change(client, db_session):
    """PATCH sourcing_status persists the change."""
    _, part = _make_req_and_part(db_session, sourcing_status="open")
    resp = client.patch(
        f"/v2/partials/parts/{part.id}/header",
        data={"field": "sourcing_status", "value": "sourcing"},
    )
    assert resp.status_code == 200
    db_session.refresh(part)
    assert part.sourcing_status == "sourcing"


def test_patch_header_hx_trigger(client, db_session):
    """PATCH response includes HX-Trigger for list sync."""
    _, part = _make_req_and_part(db_session)
    resp = client.patch(
        f"/v2/partials/parts/{part.id}/header",
        data={"field": "brand", "value": "TI"},
    )
    assert resp.status_code == 200
    assert "part-updated" in resp.headers.get("hx-trigger", "")
```

- [ ] **Step 2: Run tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_part_header.py -v`
Expected: 7 passed

- [ ] **Step 3: Run full test suite for regressions**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ -q --tb=short`
Expected: All pass, no regressions

- [ ] **Step 4: Commit**

```bash
git add tests/test_part_header.py
git commit -m "test: add 7 tests for part detail header endpoints"
```

---

### Task 6: Left Panel Sync — Listen for part-updated Events

**Files:**
- Modify: `app/templates/htmx/partials/parts/list.html`

- [ ] **Step 1: Add part-updated event listener to the parts list**

In `list.html`, add to the root container `<div>` (the one with `x-data="partsListSelection()"`):

Add this attribute:
```html
@part-updated.window="htmx.ajax('GET', '/v2/partials/parts' + window.location.search, {target: '#parts-list', swap: 'innerHTML'})"
```

This refreshes the parts list whenever a header field is saved, keeping status badges, qty, and price in sync.

- [ ] **Step 2: Run full test suite**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ -q --tb=short`
Expected: All pass

- [ ] **Step 3: Commit**

```bash
git add app/templates/htmx/partials/parts/list.html
git commit -m "feat: parts list refreshes on part-updated events from header edits"
```
