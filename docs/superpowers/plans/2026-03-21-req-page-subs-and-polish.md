# REQ Page: Substitute Parts Visibility + Tech Debt Polish — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make substitute parts always visible in the left panel as expanded sub-rows, and fix three tech debt items in the REQ Detail tab.

**Architecture:** Template-only changes — no backend or data model modifications. Two Jinja2 templates are modified: `list.html` (left panel) and `req_details.html` (REQ Detail tab). Sub-rows use the same Alpine.js scope from the parent `workspace.html`.

**Tech Stack:** Jinja2 templates, HTMX, Alpine.js, Tailwind CSS

**Spec:** `docs/superpowers/specs/2026-03-21-req-page-subs-and-polish-design.md`

---

### Task 1: Remove "Subs" Column from Left Panel

**Files:**
- Modify: `app/templates/htmx/partials/parts/list.html:95-110` (col_defs)
- Modify: `app/templates/htmx/partials/parts/list.html:196-197` (substitutes display logic)

- [ ] **Step 1: Remove `substitutes` from `col_defs`**

In `app/templates/htmx/partials/parts/list.html`, change the `col_defs` dict (line 95-110) to remove line 105:

```jinja2
{% set col_defs = {
  'mpn': ('MPN', 'mpn'),
  'brand': ('Brand', 'brand'),
  'status': ('Status', 'status'),
  'qty': ('Qty', 'qty'),
  'target_price': ('Tgt $', 'target_price'),
  'offers': ('Ofrs', None),
  'best_price': ('Best $', None),
  'need_by_date': ('Bid Due', None),
  'specs': ('FW/HW/DC', None),
  'requisition': ('Req', 'requisition'),
  'customer': ('Customer', 'customer'),
  'owner': ('Owner', None),
  'created': ('Created', 'created'),
} %}
```

- [ ] **Step 2: Remove the substitutes display block**

Remove the `{% elif col_key == 'substitutes' %}` block (lines 196-197):

```jinja2
              {% elif col_key == 'substitutes' %}
                {{ req.substitutes|join(', ') if req.substitutes else '—' }}
```

- [ ] **Step 3: Verify the page loads**

Run: `docker compose up -d --build && docker compose logs -f app`
Expected: App starts without template errors, left panel renders without "Subs" column.

- [ ] **Step 4: Commit**

```bash
git add app/templates/htmx/partials/parts/list.html
git commit -m "feat(reqs): remove redundant Subs column from left panel

Sub-rows will replace this column for better visibility."
```

---

### Task 2: Add Substitute Sub-Rows to Left Panel

**Files:**
- Modify: `app/templates/htmx/partials/parts/list.html:211` (after primary row `</tr>`, before `{% endfor %}`)

- [ ] **Step 1: Add sub-rows after each primary row**

In `app/templates/htmx/partials/parts/list.html`, immediately after the closing `</tr>` of the primary row (line 211) and before `{% endfor %}` (line 212), insert:

```jinja2
          {# ── Substitute rows — always expanded beneath primary ── #}
          {% if req.substitutes %}
            {% for sub_mpn in req.substitutes %}
            <tr class="bg-amber-50/30 hover:bg-amber-100/40 cursor-pointer border-t-0"
                :class="selectedPartId === {{ req.id }} ? 'bg-amber-100/50' : ''"
                title="Sources as alternative for {{ req.primary_mpn }}"
                @click="selectPart({{ req.id }}); htmx.ajax('GET', '/v2/partials/parts/{{ req.id }}/tab/' + activeTab, {target: '#part-detail'})">
              <td></td>
              <td colspan="{{ col_defs|length }}" class="!py-0.5 !pl-6">
                <div class="flex items-center gap-1.5">
                  <span class="border-l-2 border-amber-300 h-4 -ml-2"></span>
                  <span class="text-[9px] font-semibold text-amber-500 uppercase tracking-wider">Sub</span>
                  <span class="inline-flex px-1.5 py-0.5 text-[11px] font-mono font-medium rounded bg-amber-50 text-amber-700 border border-amber-200">{{ sub_mpn }}</span>
                </div>
              </td>
            </tr>
            {% endfor %}
          {% endif %}
```

Key details:
- Empty `<td>` for the checkbox column (no checkbox — subs share parent lifecycle)
- `colspan="{{ col_defs|length }}"` spans all remaining columns (currently 13 after removing Subs)
- `:class` highlights sub-rows when parent is selected
- `@click` selects the parent requirement, same as clicking the primary row
- `!py-0.5` reduces vertical padding for compact sub-rows
- `!pl-6` indents content to visually nest under the primary MPN
- Amber connector line + "SUB" badge + amber chip for the MPN (matches header styling)

- [ ] **Step 2: Verify sub-rows render correctly**

Run: `docker compose up -d --build && docker compose logs -f app`
Expected: Requirements with substitutes show amber sub-rows beneath them. Requirements without substitutes show no sub-rows. Clicking a sub-row selects the parent requirement and loads its detail tabs.

- [ ] **Step 3: Commit**

```bash
git add app/templates/htmx/partials/parts/list.html
git commit -m "feat(reqs): add always-visible substitute sub-rows in left panel

Sub MPNs render as expanded amber rows beneath their primary requirement.
Clicking selects the parent requirement. Visual treatment matches the
header's amber chip styling for consistent 'amber = substitute' language."
```

---

### Task 3: Add Archive Guard to REQ Detail Tab

**Files:**
- Modify: `app/templates/htmx/partials/parts/tabs/req_details.html:8-41` (add archive flag, guard spec fields)
- Modify: `app/templates/htmx/partials/parts/tabs/req_details.html:48-116` (guard requisition info fields)

- [ ] **Step 1: Add archive status flags after the `req` alias**

In `app/templates/htmx/partials/parts/tabs/req_details.html`, after line 8 (`{% set req = requisition %}`), add:

```jinja2
{% set is_archived = req.status == 'archived' or requirement.sourcing_status == 'archived' %}
```

- [ ] **Step 2: Guard Part Specifications fields**

Replace lines 29-40 (the spec fields loop body) with a conditional that renders read-only when archived:

```jinja2
    {% for field_key, field_label, field_value in spec_fields %}
    <div class="flex items-center gap-1">
      <span class="text-xs text-gray-500 whitespace-nowrap">{{ field_label }}:</span>
      {% if not is_archived %}
      <div id="reqd-spec-{{ field_key }}"
           class="cursor-pointer hover:bg-brand-50 rounded px-1 py-0.5 transition-colors min-w-0"
           hx-get="/v2/partials/parts/{{ requirement.id }}/edit-spec/{{ field_key }}"
           hx-target="#reqd-spec-{{ field_key }}"
           hx-swap="innerHTML">
        <span class="{{ 'font-medium text-gray-900' if field_value else 'text-gray-400 italic' }} truncate">{{ field_value or '—' }}</span>
      </div>
      {% else %}
      <div class="px-1 py-0.5 min-w-0">
        <span class="{{ 'font-medium text-gray-900' if field_value else 'text-gray-400' }} truncate">{{ field_value or '—' }}</span>
      </div>
      {% endif %}
    </div>
    {% endfor %}
```

- [ ] **Step 3: Guard Requisition Info fields**

For each of the 5 editable fields (name, status, urgency, deadline, owner), wrap the click-to-edit `<div>` in an `{% if not is_archived %}` / `{% else %}` block. The read-only version removes `cursor-pointer`, `hover:bg-brand-50`, and all `hx-*` attributes.

**Name field** (lines 52-58) — replace with:

```jinja2
      {% if not is_archived %}
      <div id="reqd-name"
           class="cursor-pointer hover:bg-brand-50 rounded px-1 py-0.5 transition-colors min-w-0"
           hx-get="/v2/partials/requisitions/{{ req.id }}/edit/name?context=tab"
           hx-target="#reqd-name"
           hx-swap="innerHTML">
        <span class="font-medium text-gray-900 truncate">{{ req.name or '—' }}</span>
      </div>
      {% else %}
      <div class="px-1 py-0.5 min-w-0">
        <span class="font-medium text-gray-900 truncate">{{ req.name or '—' }}</span>
      </div>
      {% endif %}
```

**Status field** (lines 64-70) — replace with:

```jinja2
      {% if not is_archived %}
      <div id="reqd-status"
           class="cursor-pointer hover:bg-brand-50 rounded px-1 py-0.5 transition-colors"
           hx-get="/v2/partials/requisitions/{{ req.id }}/edit/status?context=tab"
           hx-target="#reqd-status"
           hx-swap="innerHTML">
        {{ status_badge(req.status) }}
      </div>
      {% else %}
      <div class="px-1 py-0.5">
        {{ status_badge(req.status) }}
      </div>
      {% endif %}
```

**Urgency field** (lines 84-90) — replace with:

```jinja2
      {% if not is_archived %}
      <div id="reqd-urgency"
           class="cursor-pointer hover:bg-brand-50 rounded px-1 py-0.5 transition-colors"
           hx-get="/v2/partials/requisitions/{{ req.id }}/edit/urgency?context=tab"
           hx-target="#reqd-urgency"
           hx-swap="innerHTML">
        {{ urgency_badge(req.urgency or 'normal') }}
      </div>
      {% else %}
      <div class="px-1 py-0.5">
        {{ urgency_badge(req.urgency or 'normal') }}
      </div>
      {% endif %}
```

**Deadline field** (lines 96-102) — replace with:

```jinja2
      {% if not is_archived %}
      <div id="reqd-deadline"
           class="cursor-pointer hover:bg-brand-50 rounded px-1 py-0.5 transition-colors"
           hx-get="/v2/partials/requisitions/{{ req.id }}/edit/deadline?context=tab"
           hx-target="#reqd-deadline"
           hx-swap="innerHTML">
        <span class="text-gray-900">{{ req.deadline or '—' }}</span>
      </div>
      {% else %}
      <div class="px-1 py-0.5">
        <span class="text-gray-900">{{ req.deadline or '—' }}</span>
      </div>
      {% endif %}
```

**Owner field** (lines 108-114) — replace with:

```jinja2
      {% if not is_archived %}
      <div id="reqd-owner"
           class="cursor-pointer hover:bg-brand-50 rounded px-1 py-0.5 transition-colors"
           hx-get="/v2/partials/requisitions/{{ req.id }}/edit/owner?context=tab"
           hx-target="#reqd-owner"
           hx-swap="innerHTML">
        <span class="text-gray-900">{{ req.creator.name if req.creator else '—' }}</span>
      </div>
      {% else %}
      <div class="px-1 py-0.5">
        <span class="text-gray-900">{{ req.creator.name if req.creator else '—' }}</span>
      </div>
      {% endif %}
```

- [ ] **Step 4: Verify archive guard works**

Run: `docker compose up -d --build && docker compose logs -f app`
Expected: When viewing an archived requisition or archived requirement, all spec and requisition info fields render as plain text without click-to-edit affordances.

- [ ] **Step 5: Commit**

```bash
git add app/templates/htmx/partials/parts/tabs/req_details.html
git commit -m "fix(reqs): add archive guard to all editable fields in REQ Detail tab

Blocks click-to-edit on Part Specifications and Requisition Info fields
when either the requisition or requirement is archived. Backend routes
already return 403; this prevents the confusing edit affordance."
```

---

### Task 4: Make Sibling Rows Clickable in REQ Detail Tab

**Files:**
- Modify: `app/templates/htmx/partials/parts/tabs/req_details.html:161-162` (sibling row `<tr>`)

- [ ] **Step 1: Add click handler and hover styling to sibling rows**

Replace line 162:

```jinja2
          <tr class="{{ 'bg-brand-50' if part.id == requirement.id }}">
```

With:

```jinja2
          <tr class="{{ 'bg-brand-50 font-medium' if part.id == requirement.id else 'cursor-pointer hover:bg-gray-50 transition-colors' }}"
              {% if part.id != requirement.id %}
              @click="selectPart({{ part.id }}); htmx.ajax('GET', '/v2/partials/parts/{{ part.id }}/tab/' + activeTab, {target: '#part-detail'})"
              {% endif %}>
```

Key details:
- Current part row: highlighted with `bg-brand-50 font-medium`, no click handler
- Other sibling rows: `cursor-pointer` + `hover:bg-gray-50` + click handler that switches selection
- `selectPart()` and `activeTab` are accessible from the parent `workspace.html` Alpine scope
- Checkbox `@click.stop` on line 163 already prevents click propagation for checkboxes

- [ ] **Step 2: Verify sibling row click works**

Run: `docker compose up -d --build && docker compose logs -f app`
Expected: In the REQ Detail tab, clicking a sibling row (not the current part) navigates to that part's detail view. Current part row stays highlighted and is not clickable.

- [ ] **Step 3: Commit**

```bash
git add app/templates/htmx/partials/parts/tabs/req_details.html
git commit -m "feat(reqs): make sibling rows clickable in REQ Detail tab

Clicking a sibling part row switches selection and loads that part's
active tab, matching the left panel's click behavior."
```

---

### Task 5: Verify All Changes Together

- [ ] **Step 1: Rebuild and verify**

Run: `docker compose up -d --build && docker compose logs -f app`
Expected: Clean startup, no template errors.

- [ ] **Step 2: Manual verification checklist**

Verify in the running app:
1. Left panel: requirements with substitutes show amber sub-rows beneath them
2. Left panel: "Subs" column is gone
3. Left panel: clicking a sub-row selects the parent requirement
4. Left panel: sub-rows highlight when parent is selected
5. REQ Detail: archived requisition/requirement shows read-only fields
6. REQ Detail: clicking a sibling row navigates to that part

- [ ] **Step 3: Run related tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_htmx_views.py -v -k "req_detail" --tb=short`
Expected: All existing tests pass (template changes are backwards-compatible).

- [ ] **Step 4: Run full test suite**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v --tb=short`
Expected: All tests pass.
