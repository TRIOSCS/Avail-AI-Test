# REQ Detail Tab Rescope — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Compact the REQ Detail tab layout, fix review findings (archive guard, whitespace bug, condition duplication, redundant constants), and apply `compact-table` to the sibling table.

**Architecture:** All changes are in-place modifications to existing files. No new files, no new routes, no migrations. The template gets compacted (flex label:value, `compact-table`, `status_badge` macro), the routes get hardened (archive guard, whitespace fix, constant cleanup), and tests get extended.

**Tech Stack:** FastAPI, Jinja2, HTMX 2.x, Alpine.js 3.x, Tailwind CSS

**Spec:** `docs/superpowers/specs/2026-03-21-req-detail-tab-rescope-design.md`

---

### Task 1: Backend fixes — archive guard, whitespace bug, constant cleanup

**Files:**
- Modify: `app/routers/htmx_views.py:8860-8871` (header editable set)
- Modify: `app/routers/htmx_views.py:9013` (remove `_SPEC_EDITABLE`)
- Modify: `app/routers/htmx_views.py:9115-9172` (spec edit/save routes)
- Test: `tests/test_req_details_tab.py`

- [ ] **Step 1: Write failing test for archive guard**

In `tests/test_req_details_tab.py`, add:

```python
def test_spec_edit_blocked_on_archived_part(client, db_session, test_user):
    """Spec edit and save return 403 for archived parts."""
    reqn, parts = _make_requisition_and_parts(db_session, test_user, num_parts=1)
    parts[0].sourcing_status = "archived"
    db_session.commit()

    resp = client.get(f"/v2/partials/parts/{parts[0].id}/edit-spec/firmware")
    assert resp.status_code == 403

    resp = client.patch(
        f"/v2/partials/parts/{parts[0].id}/save-spec",
        data={"field": "firmware", "value": "v3.0"},
    )
    assert resp.status_code == 403
```

- [ ] **Step 2: Write failing test for whitespace-only value**

In `tests/test_req_details_tab.py`, add:

```python
def test_spec_save_whitespace_only_becomes_null(client, db_session, test_user):
    """PATCH with whitespace-only value sets field to None, not empty string."""
    reqn, parts = _make_requisition_and_parts(db_session, test_user, num_parts=1)

    resp = client.patch(
        f"/v2/partials/parts/{parts[0].id}/save-spec",
        data={"field": "firmware", "value": "   "},
    )
    assert resp.status_code == 200

    db_session.expire_all()
    from app.models import Requirement
    updated = db_session.get(Requirement, parts[0].id)
    assert updated.firmware is None
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_req_details_tab.py::test_spec_edit_blocked_on_archived_part tests/test_req_details_tab.py::test_spec_save_whitespace_only_becomes_null -v`
Expected: Both FAIL

- [ ] **Step 4: Implement archive guard in both spec endpoints**

In `app/routers/htmx_views.py`, in `part_spec_edit` (after the `db.get` at line ~9130), add:

```python
    if req.sourcing_status == "archived":
        return HTMLResponse("Cannot edit archived part", status_code=403)
```

Add the same guard in `part_spec_save` (after the `db.get` at line ~9157).

- [ ] **Step 5: Fix whitespace bug**

In `app/routers/htmx_views.py`, in `part_spec_save`, change:

```python
    clean = value.strip() if value else None
```

to:

```python
    clean = (value or "").strip() or None
```

- [ ] **Step 6: Delete `_SPEC_EDITABLE` set, use `_SPEC_LABELS`**

In `app/routers/htmx_views.py`, delete line 9013:

```python
_SPEC_EDITABLE = {"customer_pn", "condition", "date_codes", "packaging", "firmware", "hardware_codes"}
```

Then replace both occurrences of `_SPEC_EDITABLE` with `_SPEC_LABELS`:
- `if field not in _SPEC_EDITABLE` → `if field not in _SPEC_LABELS` (in `part_spec_edit`)
- `if field not in _SPEC_EDITABLE` → `if field not in _SPEC_LABELS` (in `part_spec_save`)

- [ ] **Step 7: Remove condition/date_codes/packaging from header editable set**

In `app/routers/htmx_views.py`, change `_PART_HEADER_EDITABLE` (line 8860) from:

```python
_PART_HEADER_EDITABLE = {
    "brand",
    "target_qty",
    "target_price",
    "condition",
    "sourcing_status",
    "notes",
    "date_codes",
    "packaging",
    "substitutes",
}
```

to:

```python
_PART_HEADER_EDITABLE = {
    "brand",
    "target_qty",
    "target_price",
    "sourcing_status",
    "notes",
    "substitutes",
}
```

- [ ] **Step 8: Pass `_CONDITION_CHOICES` to spec_edit template context**

> **Note:** This changes condition dropdown options from `['New', 'Refurbished', 'Used', 'As-Is']` to `['New', 'Used', 'Refurbished', 'Any']` — unifying with the header edit's `_CONDITION_CHOICES`. "As-Is" is removed, "Any" is added.

In `app/routers/htmx_views.py`, in `part_spec_edit`, add to the `ctx.update()` call:

```python
    ctx.update({
        "requirement": req,
        "field": field,
        "field_label": _SPEC_LABELS[field],
        "field_value": getattr(req, field, None) or "",
        "condition_choices": _CONDITION_CHOICES,
    })
```

- [ ] **Step 9: Run tests to verify they pass**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_req_details_tab.py -v`
Expected: All tests PASS

- [ ] **Step 10: Commit**

```bash
git add app/routers/htmx_views.py tests/test_req_details_tab.py
git commit -m "fix: archive guard, whitespace bug, remove redundant spec constants, consolidate header editable fields"
```

---

### Task 2: Update spec_edit.html to use context variable for condition choices

**Files:**
- Modify: `app/templates/htmx/partials/parts/spec_edit.html:23`

- [ ] **Step 1: Replace hardcoded choices with context variable**

In `app/templates/htmx/partials/parts/spec_edit.html`, change line 23 from:

```jinja2
    {% for c in ['New', 'Refurbished', 'Used', 'As-Is'] %}
```

to:

```jinja2
    {% for c in condition_choices %}
```

- [ ] **Step 2: Run tests to verify nothing broke**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_req_details_tab.py -v`
Expected: All PASS

- [ ] **Step 3: Commit**

```bash
git add app/templates/htmx/partials/parts/spec_edit.html
git commit -m "refactor: use condition_choices context var instead of hardcoded list in spec_edit"
```

---

### Task 3: Compact Part Specifications and Requisition Info grids

**Files:**
- Modify: `app/templates/htmx/partials/parts/tabs/req_details.html:17-114`

- [ ] **Step 1: Compact Part Specifications grid to flex label:value**

In `app/templates/htmx/partials/parts/tabs/req_details.html`, change the Part Specifications section (lines 17-41) to:

```jinja2
  {# ── Part Specifications (click-to-edit) ─────────────────── #}
  <h3 class="text-sm font-semibold text-gray-700 mb-2">Part Specifications</h3>

  <div class="grid grid-cols-2 gap-x-6 gap-y-1.5 text-sm">
    {% set spec_fields = [
      ('customer_pn', 'Customer PN', requirement.customer_pn),
      ('condition', 'Condition', requirement.condition),
      ('date_codes', 'Date Codes', requirement.date_codes),
      ('packaging', 'Packaging', requirement.packaging),
      ('firmware', 'Firmware', requirement.firmware),
      ('hardware_codes', 'Hardware', requirement.hardware_codes),
    ] %}
    {% for field_key, field_label, field_value in spec_fields %}
    <div class="flex items-center gap-1">
      <span class="text-xs text-gray-500 whitespace-nowrap">{{ field_label }}:</span>
      <div id="reqd-spec-{{ field_key }}"
           class="cursor-pointer hover:bg-brand-50 rounded px-1 py-0.5 transition-colors min-w-0"
           hx-get="/v2/partials/parts/{{ requirement.id }}/edit-spec/{{ field_key }}"
           hx-target="#reqd-spec-{{ field_key }}"
           hx-swap="innerHTML">
        <span class="{{ 'font-medium text-gray-900' if field_value else 'text-gray-400 italic' }} truncate">{{ field_value or '—' }}</span>
      </div>
    </div>
    {% endfor %}
  </div>

  <hr class="my-2">
```

- [ ] **Step 2: Compact Requisition Info grid to flex label:value**

Change the Requisition Info section (lines 43-114) to:

```jinja2
  {# ── Requisition Info ────────────────────────────────────── #}
  <h3 class="text-sm font-semibold text-gray-700 mb-2">Requisition Info</h3>

  <div class="grid grid-cols-2 gap-x-6 gap-y-1.5 text-sm">
    {# Name #}
    <div class="flex items-center gap-1">
      <span class="text-xs text-gray-500 whitespace-nowrap">Name:</span>
      <div id="reqd-name"
           class="cursor-pointer hover:bg-brand-50 rounded px-1 py-0.5 transition-colors min-w-0"
           hx-get="/v2/partials/requisitions/{{ req.id }}/edit/name?context=tab"
           hx-target="#reqd-name"
           hx-swap="innerHTML">
        <span class="font-medium text-gray-900 truncate">{{ req.name or '—' }}</span>
      </div>
    </div>

    {# Status #}
    <div class="flex items-center gap-1">
      <span class="text-xs text-gray-500 whitespace-nowrap">Status:</span>
      <div id="reqd-status"
           class="cursor-pointer hover:bg-brand-50 rounded px-1 py-0.5 transition-colors"
           hx-get="/v2/partials/requisitions/{{ req.id }}/edit/status?context=tab"
           hx-target="#reqd-status"
           hx-swap="innerHTML">
        {{ status_badge(req.status) }}
      </div>
    </div>

    {# Customer (read-only) #}
    <div class="flex items-center gap-1">
      <span class="text-xs text-gray-500 whitespace-nowrap">Customer:</span>
      <div class="px-1 py-0.5">
        <span class="text-gray-900">{{ req.customer_name or '—' }}</span>
      </div>
    </div>

    {# Urgency #}
    <div class="flex items-center gap-1">
      <span class="text-xs text-gray-500 whitespace-nowrap">Urgency:</span>
      <div id="reqd-urgency"
           class="cursor-pointer hover:bg-brand-50 rounded px-1 py-0.5 transition-colors"
           hx-get="/v2/partials/requisitions/{{ req.id }}/edit/urgency?context=tab"
           hx-target="#reqd-urgency"
           hx-swap="innerHTML">
        {{ urgency_badge(req.urgency or 'normal') }}
      </div>
    </div>

    {# Deadline #}
    <div class="flex items-center gap-1">
      <span class="text-xs text-gray-500 whitespace-nowrap">Deadline:</span>
      <div id="reqd-deadline"
           class="cursor-pointer hover:bg-brand-50 rounded px-1 py-0.5 transition-colors"
           hx-get="/v2/partials/requisitions/{{ req.id }}/edit/deadline?context=tab"
           hx-target="#reqd-deadline"
           hx-swap="innerHTML">
        <span class="text-gray-900">{{ req.deadline or '—' }}</span>
      </div>
    </div>

    {# Owner #}
    <div class="flex items-center gap-1">
      <span class="text-xs text-gray-500 whitespace-nowrap">Owner:</span>
      <div id="reqd-owner"
           class="cursor-pointer hover:bg-brand-50 rounded px-1 py-0.5 transition-colors"
           hx-get="/v2/partials/requisitions/{{ req.id }}/edit/owner?context=tab"
           hx-target="#reqd-owner"
           hx-swap="innerHTML">
        <span class="text-gray-900">{{ req.creator.name if req.creator else '—' }}</span>
      </div>
    </div>
  </div>

  <hr class="my-2">
```

- [ ] **Step 3: Run tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_req_details_tab.py -v`
Expected: All PASS

- [ ] **Step 4: Commit**

```bash
git add app/templates/htmx/partials/parts/tabs/req_details.html
git commit -m "style: compact specs and req info grids with inline flex label:value layout"
```

---

### Task 4: Tighten sibling table — compact-table + status_badge macro

**Files:**
- Modify: `app/templates/htmx/partials/parts/tabs/req_details.html:116-205`

- [ ] **Step 1: Replace sibling table with compact-table class and status_badge macro**

In `app/templates/htmx/partials/parts/tabs/req_details.html`, replace the sibling parts section (from `<h3>Parts on this Requisition</h3>` through the end of the template) with:

```jinja2
  {# ── Sibling parts table ──────────────────────────────────── #}
  <h3 class="text-sm font-semibold text-gray-700 mb-2">Parts on this Requisition</h3>

  <div x-data="{ selectedSiblings: [] }">
    {# Bulk actions bar #}
    <div x-show="selectedSiblings.length > 0"
         x-cloak
         class="flex items-center gap-3 mb-2 px-3 py-1.5 bg-brand-50 rounded-lg border border-brand-200 text-xs">
      <span class="text-gray-600" x-text="selectedSiblings.length + ' selected'"></span>
      <button type="button"
              class="px-2 py-0.5 text-xs font-medium rounded-md bg-red-100 text-red-700 hover:bg-red-200 transition-colors"
              hx-post="/v2/partials/parts/bulk-archive"
              :hx-vals="JSON.stringify({requirement_ids: selectedSiblings.join(',')})"
              hx-target="#part-detail"
              hx-swap="innerHTML"
              hx-confirm="Archive selected parts?">
        Archive
      </button>
    </div>

    {% if sibling_parts %}
    <div class="overflow-x-auto">
      <table class="compact-table w-full">
        <thead>
          <tr>
            <th class="w-6">
              <input type="checkbox"
                     class="h-3 w-3 rounded border-gray-300 text-brand-500"
                     @change="selectedSiblings = $el.checked ? [{% for p in sibling_parts %}{{ p.id }}{{ ',' if not loop.last }}{% endfor %}] : []">
            </th>
            <th>MPN</th>
            <th>Brand</th>
            <th>Status</th>
            <th>Qty</th>
            <th>Tgt $</th>
            <th>Cust PN</th>
            <th>Subs</th>
            <th>Offers</th>
          </tr>
        </thead>
        <tbody>
          {% for part in sibling_parts %}
          <tr class="{{ 'bg-brand-50' if part.id == requirement.id }}">
            <td @click.stop>
              <input type="checkbox"
                     class="h-3 w-3 rounded border-gray-300 text-brand-500"
                     value="{{ part.id }}"
                     :checked="selectedSiblings.includes({{ part.id }})"
                     @change="$el.checked ? selectedSiblings.push({{ part.id }}) : selectedSiblings = selectedSiblings.filter(id => id !== {{ part.id }})">
            </td>
            <td class="font-semibold">{{ part.primary_mpn or '—' }}</td>
            <td class="max-w-[100px] truncate" title="{{ part.brand or '' }}">{{ part.brand or '—' }}</td>
            <td>{{ status_badge(part.sourcing_status or 'open') }}</td>
            <td>{{ part.target_qty or '—' }}</td>
            <td>{{ '$%.2f'|format(part.target_price) if part.target_price else '—' }}</td>
            <td class="max-w-[100px] truncate" title="{{ part.customer_pn or '' }}">{{ part.customer_pn or '—' }}</td>
            <td>{{ (part.substitutes|length) if part.substitutes else '—' }}</td>
            {% set offer_count = part.offers|length %}
            {% set offer_prices = part.offers|map(attribute='unit_price')|reject('none')|list %}
            {% set best_price = offer_prices|min if offer_prices else none %}
            <td>
              {%- if offer_count -%}
                {{ offer_count }}{% if best_price %} / ${{ '%.2f'|format(best_price) }}{% endif %}
              {%- else -%}
                —
              {%- endif -%}
            </td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
    {% else %}
    <p class="text-xs text-gray-400 italic">No parts on this requisition.</p>
    {% endif %}
  </div>
</div>
```

- [ ] **Step 2: Run tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_req_details_tab.py -v`
Expected: All PASS

- [ ] **Step 3: Commit**

```bash
git add app/templates/htmx/partials/parts/tabs/req_details.html
git commit -m "style: apply compact-table to sibling parts, use status_badge macro"
```

---

### Task 5: Extend test helper and run full suite

**Files:**
- Modify: `tests/test_req_details_tab.py:11-41`

- [ ] **Step 1: Extend `_make_requisition_and_parts` with `**part_kwargs`**

In `tests/test_req_details_tab.py`, change the helper (lines 11-41) to:

```python
def _make_requisition_and_parts(db_session, test_user, num_parts=2, **part_kwargs):
    """Helper: create a requisition with sibling parts."""
    from app.models import Requirement, Requisition

    reqn = Requisition(
        name="Test Req",
        status="active",
        urgency="normal",
        customer_name="Acme Corp",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(reqn)
    db_session.commit()
    db_session.refresh(reqn)

    parts = []
    for i in range(num_parts):
        defaults = {
            "requisition_id": reqn.id,
            "primary_mpn": f"MPN-{i:03d}",
            "target_qty": (i + 1) * 100,
            "sourcing_status": "open",
        }
        if i == 0:
            defaults.update(part_kwargs)
        part = Requirement(**defaults)
        db_session.add(part)
        parts.append(part)
    db_session.commit()
    for p in parts:
        db_session.refresh(p)

    return reqn, parts
```

- [ ] **Step 2: Run full test suite**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_req_details_tab.py -v`
Expected: All PASS (17 tests — 9 original + 6 from prior session + 2 new)

- [ ] **Step 3: Commit**

```bash
git add tests/test_req_details_tab.py
git commit -m "test: extend _make_requisition_and_parts helper with **part_kwargs"
```
