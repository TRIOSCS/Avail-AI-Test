# Plan 2: Core Pages — Requisitions, Companies, Vendors

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild requisitions, companies, and vendors pages with all filters, bulk operations, inline editing, tabs, analytics, enrich, and click-to-call.

**Architecture:** Server-rendered Jinja2 partials with HTMX for requests/swaps and Alpine.js for local UI state. All templates use brand color palette from Plan 1.

**Tech Stack:** HTMX 2.x, Alpine.js 3.x, Jinja2, FastAPI, Tailwind CSS (brand palette)

**Spec:** `docs/superpowers/specs/2026-03-15-htmx-frontend-rebuild-design.md` (Sections 3, 4, 5)

**Depends on:** Plan 1 (Foundation) must be complete first.

---

## Existing State Summary

**Templates that exist and will be REPLACED (overwritten):**
- `app/templates/htmx/partials/requisitions/list.html` — basic list, no bulk ops, no urgency/owner/date filters, uses blue-600
- `app/templates/htmx/partials/requisitions/detail.html` — flat requirements table only, no tabs, no inline edit
- `app/templates/htmx/partials/companies/list.html` — basic table, no avatar circles styled with brand
- `app/templates/htmx/partials/companies/detail.html` — flat layout, no tabs, no enrich, no contacts
- `app/templates/htmx/partials/vendors/list.html` — card grid (spec requires table), no blacklisted toggle
- `app/templates/htmx/partials/vendors/detail.html` — flat layout, no tabs, no enrich, no click-to-call

**Templates that must be CREATED (new files):**
- `app/templates/partials/requisitions/req_row.html`
- `app/templates/partials/requisitions/create_modal.html`
- `app/templates/partials/requisitions/tabs/parts.html`
- `app/templates/partials/requisitions/tabs/offers.html`
- `app/templates/partials/requisitions/tabs/quotes.html`
- `app/templates/partials/requisitions/tabs/buy_plans.html`
- `app/templates/partials/requisitions/tabs/tasks.html`
- `app/templates/partials/requisitions/tabs/activity.html`
- `app/templates/partials/shared/safety_review.html`

**Routes that exist (EXISTING — update):**
- `GET /v2/partials/requisitions` — needs owner, urgency, date_from, date_to, sort, dir params
- `GET /v2/partials/requisitions/{id}` — needs to pass tab data
- `POST /v2/partials/requisitions/create` — needs to return new row, not full list
- `GET /v2/partials/vendors` — needs hide_blacklisted, sort, dir params
- `GET /v2/partials/vendors/{id}` — needs tab support, safety data
- `GET /v2/partials/companies` — works, needs brand colors
- `GET /v2/partials/companies/{id}` — needs tabs, contacts, enrich

**Routes to ADD (NEW):**
- `GET /v2/partials/requisitions/create-form`
- `GET /v2/partials/requisitions/{id}/tab/{tab}`
- `POST /v2/partials/requisitions/bulk/{action}`
- `GET /v2/partials/vendors/{id}/tab/{tab}`
- `GET /v2/partials/companies/{id}/tab/{tab}`

**Existing router with bulk ops / inline edit logic (reuse patterns):**
- `app/routers/requisitions2.py` — has bulk actions, inline editing, SSE, filter parsing
- `app/routers/vendor_analytics.py` — has offer-history, parts-summary, confirmed-offers endpoints
- `app/routers/task.py` — has CRUD for RequisitionTask

---

## Task 1: Requisition Row Partial

**Files to create:**
- `app/templates/partials/requisitions/req_row.html`

**Steps:**

- [x] **1.1** Create `app/templates/partials/requisitions/req_row.html` — a single `<tr>` that receives `req` template var. Used for initial render rows, new row after create (prepend), and search/filter results.

```html
{# req_row.html — Single requisition table row.
   Receives: req (Requisition object with req_count, offer_count attrs).
   Called by: requisitions/list.html loop, create endpoint (prepend).
   Depends on: brand color palette from tailwind.config.js.
#}
<tr class="hover:bg-brand-50 cursor-pointer group"
    hx-get="/v2/partials/requisitions/{{ req.id }}"
    hx-target="#main-content"
    hx-push-url="/v2/requisitions/{{ req.id }}">
  {# Checkbox (stops row click propagation) #}
  <td class="px-3 py-3 w-10" @click.stop>
    <input type="checkbox" :value="{{ req.id }}"
           @change="$event.target.checked ? selectedIds.add({{ req.id }}) : selectedIds.delete({{ req.id }}); selectedIds = new Set(selectedIds)"
           :checked="selectedIds.has({{ req.id }})"
           class="h-4 w-4 rounded border-gray-200 text-brand-500 focus:ring-brand-500">
  </td>
  <td class="px-4 py-3 text-sm font-medium text-brand-500">{{ req.name }}</td>
  <td class="px-4 py-3 text-sm text-gray-500">{{ req.customer_name or "\u2014" }}</td>
  <td class="px-4 py-3 text-sm text-gray-500">{{ req.creator.name if req.creator else "\u2014" }}</td>
  <td class="px-4 py-3 text-sm text-gray-500">{{ req.req_count }}</td>
  <td class="px-4 py-3 text-sm text-gray-500">{{ req.offer_count|default(0) }}</td>
  <td class="px-4 py-3">
    {% set status_colors = {
      "active": "bg-emerald-50 text-emerald-700",
      "draft": "bg-brand-100 text-brand-600",
      "sourcing": "bg-brand-100 text-brand-600",
      "won": "bg-emerald-50 text-emerald-700",
      "lost": "bg-rose-50 text-rose-700",
      "archived": "bg-gray-100 text-gray-600",
      "awarded": "bg-emerald-50 text-emerald-700"
    } %}
    <span class="inline-flex px-2 py-0.5 text-xs font-medium rounded-full {{ status_colors.get(req.status, 'bg-gray-100 text-gray-600') }}">
      {{ req.status|capitalize }}
    </span>
  </td>
  <td class="px-4 py-3">
    {% if req.urgency == "critical" %}
      <span class="inline-flex px-2 py-0.5 text-xs font-medium rounded-full bg-rose-50 text-rose-700">Critical</span>
    {% elif req.urgency == "hot" %}
      <span class="inline-flex px-2 py-0.5 text-xs font-medium rounded-full bg-amber-50 text-amber-700">Hot</span>
    {% else %}
      <span class="text-sm text-gray-500">Normal</span>
    {% endif %}
  </td>
  <td class="px-4 py-3 text-sm text-gray-500">{{ req.created_at.strftime('%b %d, %Y') if req.created_at else "\u2014" }}</td>
</tr>
```

- [x] **1.2** Verify template renders without errors by running:
```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_htmx_views.py -v -k "requisition" --no-header -q 2>&1 | head -30
```

- [x] **1.3** Commit:
```bash
git add app/templates/partials/requisitions/req_row.html
git commit -m "Add requisition row partial for HTMX list rendering"
```

---

## Task 2: Requisition Create Modal

**Files to create:**
- `app/templates/partials/requisitions/create_modal.html`

**Files to modify:**
- `app/routers/htmx_views.py` — add `GET /v2/partials/requisitions/create-form` route; update `POST /v2/partials/requisitions/create` to return a single row + close-modal trigger instead of full list

**Steps:**

- [x] **2.1** Create `app/templates/partials/requisitions/create_modal.html`:

```html
{# create_modal.html — Requisition creation form loaded into #modal-content.
   Called by: "New Requisition" button dispatching open-modal + hx-get.
   Depends on: modal.html shell (from Plan 1), brand palette.
#}
<div class="p-6">
  <h2 class="text-lg font-semibold text-gray-900 mb-4">New Requisition</h2>
  <form hx-post="/v2/partials/requisitions/create"
        hx-target="#req-table-body"
        hx-swap="afterbegin"
        @htmx:after-request.camel="if(event.detail.successful) { $dispatch('close-modal'); Alpine.store('toast').message='Requisition created'; Alpine.store('toast').type='success'; Alpine.store('toast').show=true; }">
    <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
      <div>
        <label class="block text-sm font-medium text-gray-900 mb-1">Name <span class="text-rose-500">*</span></label>
        <input type="text" name="name" required placeholder="e.g. Q1 Capacitor Order"
               class="w-full px-3 py-2 border border-gray-200 rounded-lg text-sm focus:ring-2 focus:ring-brand-500 focus:border-brand-500">
      </div>
      <div>
        <label class="block text-sm font-medium text-gray-900 mb-1">Customer</label>
        <input type="text" name="customer_name" placeholder="Company name"
               class="w-full px-3 py-2 border border-gray-200 rounded-lg text-sm focus:ring-2 focus:ring-brand-500 focus:border-brand-500">
      </div>
      <div>
        <label class="block text-sm font-medium text-gray-900 mb-1">Deadline</label>
        <input type="date" name="deadline"
               class="w-full px-3 py-2 border border-gray-200 rounded-lg text-sm focus:ring-2 focus:ring-brand-500 focus:border-brand-500">
      </div>
      <div>
        <label class="block text-sm font-medium text-gray-900 mb-1">Urgency</label>
        <select name="urgency" class="w-full px-3 py-2 border border-gray-200 rounded-lg text-sm focus:ring-2 focus:ring-brand-500 focus:border-brand-500">
          <option value="normal">Normal</option>
          <option value="hot">Hot</option>
          <option value="critical">Critical</option>
        </select>
      </div>
    </div>
    <div class="mt-4">
      <label class="block text-sm font-medium text-gray-900 mb-1">Parts <span class="text-xs text-gray-500">(one per line: MPN, Qty)</span></label>
      <textarea name="parts_text" rows="4" placeholder="LM358N, 500&#10;TL074CN, 200&#10;NE555P, 1000"
                class="w-full px-3 py-2 border border-gray-200 rounded-lg text-sm font-mono focus:ring-2 focus:ring-brand-500 focus:border-brand-500"></textarea>
    </div>
    <div class="mt-6 flex justify-end gap-3">
      <button type="button" @click="$dispatch('close-modal')"
              class="px-4 py-2 text-sm font-medium text-gray-900 bg-white border border-gray-200 rounded-lg hover:bg-gray-50">Cancel</button>
      <button type="submit"
              class="px-4 py-2 text-sm font-medium text-white bg-brand-500 rounded-lg hover:bg-brand-600">Create Requisition</button>
    </div>
  </form>
</div>
```

- [x] **2.2** Add `GET /v2/partials/requisitions/create-form` route to `app/routers/htmx_views.py`:

```python
@router.get("/v2/partials/requisitions/create-form", response_class=HTMLResponse)
async def requisition_create_form(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return the create requisition modal form."""
    ctx = _base_ctx(request, user, "requisitions")
    return templates.TemplateResponse("partials/requisitions/create_modal.html", ctx)
```

- [x] **2.3** Update `POST /v2/partials/requisitions/create` in `app/routers/htmx_views.py` to return a single row partial instead of the full list. After `db.commit()`, render `req_row.html` with the new req and set `HX-Trigger: showToast` header. Add `req.req_count` and `req.offer_count = 0` before rendering.

- [x] **2.4** Write test for create-form route:
```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_htmx_views.py -v -k "create" --no-header -q
```

- [x] **2.5** Commit:
```bash
git add app/templates/partials/requisitions/create_modal.html app/routers/htmx_views.py
git commit -m "Add requisition create modal with MPN/Qty parsing"
```

---

## Task 3: Requisition List Rebuild (Filters, Bulk Ops, Brand Colors)

**Files to modify:**
- `app/templates/htmx/partials/requisitions/list.html` — full rewrite
- `app/routers/htmx_views.py` — update `requisitions_list_partial` to accept owner, urgency, date_from, date_to, sort, dir params

**Steps:**

- [x] **3.1** Update `requisitions_list_partial` in `app/routers/htmx_views.py` to accept these additional query params:

```python
@router.get("/v2/partials/requisitions", response_class=HTMLResponse)
async def requisitions_list_partial(
    request: Request,
    q: str = "",
    status: str = "",
    owner: int = Query(0, ge=0),
    urgency: str = "",
    date_from: str = "",
    date_to: str = "",
    sort: str = "created_at",
    dir: str = "desc",
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
```

Add filter logic for owner (`Requisition.created_by == owner`), urgency (`Requisition.urgency == urgency`), date_from/date_to (`Requisition.created_at >= / <=`). Add sort logic mapping column name + direction. For sales role, filter `Requisition.created_by == user.id`. Fetch team users for owner dropdown (only if not sales role). Attach `req.offer_count = len(req.offers) if req.offers else 0` for each req. Pass `users`, `user_role`, `sort`, `dir`, `owner`, `urgency`, `date_from`, `date_to` to template context.

- [x] **3.2** Rewrite `app/templates/htmx/partials/requisitions/list.html` with:

The template should include:
1. OOB breadcrumb div: `<div id="breadcrumb" hx-swap-oob="true">Requisitions</div>`
2. Page header with count + "New Requisition" button that dispatches `open-modal` and loads create form via `hx-get="/v2/partials/requisitions/create-form" hx-target="#modal-content"`
3. Filter bar wrapped in `<form id="req-filters">`:
   - Search input with `hx-trigger="keyup changed delay:300ms"` and `hx-include="#req-filters"`
   - Quick filter pills (All, Open, Awarded, Archived) — each is a button that sets hidden `status` input via Alpine and triggers form submit
   - Owner dropdown (hidden for sales role via `{% if user_role != 'sales' %}`) — `<select name="owner">` with team users
   - Urgency filter pills: Normal / Hot / Critical toggle
   - Date range: `date_from` and `date_to` inputs
   - All elements use `hx-get="/v2/partials/requisitions"` `hx-target="#main-content"` `hx-push-url="true"` `hx-include="#req-filters"`
4. Alpine `x-data="{ selectedIds: new Set() }"` wrapper
5. Bulk action bar (shown when `selectedIds.size > 0`): Archive, Assign (with owner dropdown), Activate buttons. Each posts to `/v2/partials/requisitions/bulk/{action}` with comma-separated IDs.
6. Table with sortable headers — each header is an `<a>` with `hx-get` passing `sort=column&dir=asc/desc` and `hx-include="#req-filters"`. Active sort column shows arrow indicator.
7. Columns: checkbox, Name, Customer, Owner, Parts, Offers, Status, Urgency, Created
8. Table body `id="req-table-body"` using `{% include "partials/requisitions/req_row.html" %}` for each row
9. Pagination using `{% include "partials/shared/pagination.html" %}` or inline prev/next
10. Empty state using `{% include "partials/shared/empty_state.html" %}`
11. All brand colors: `brand-500` buttons, `brand-600` hover, `emerald` success, `amber` warning, `rose` danger

- [x] **3.3** Add bulk action route to `app/routers/htmx_views.py`:

```python
@router.post("/v2/partials/requisitions/bulk/{action}", response_class=HTMLResponse)
async def requisitions_bulk_action(
    request: Request,
    action: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
```

Parse `ids` from form data (comma-separated). Validate action is one of: archive, activate, assign. For assign, also parse `owner_id`. Apply action to each requisition (reuse logic from `requisitions2.py`). Max 200 per bulk action. Return refreshed list partial.

- [x] **3.4** Run tests:
```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_htmx_views.py -v -k "requisition" --no-header -q
```

- [x] **3.5** Commit:
```bash
git add app/templates/htmx/partials/requisitions/list.html app/routers/htmx_views.py
git commit -m "Rebuild requisitions list with filters, bulk ops, and brand palette"
```

---

## Task 4: Requisition Detail — Tabbed Layout with Header

**Files to modify:**
- `app/templates/htmx/partials/requisitions/detail.html` — full rewrite to tabbed layout
- `app/routers/htmx_views.py` — update detail route to pass more context

**Files to create:**
- `app/templates/partials/requisitions/tabs/parts.html`
- `app/templates/partials/requisitions/tabs/offers.html`
- `app/templates/partials/requisitions/tabs/quotes.html`
- `app/templates/partials/requisitions/tabs/buy_plans.html`
- `app/templates/partials/requisitions/tabs/tasks.html`
- `app/templates/partials/requisitions/tabs/activity.html`

**Steps:**

- [x] **4.1** Rewrite `app/templates/htmx/partials/requisitions/detail.html`:

The template should include:
1. OOB breadcrumb: `<div id="breadcrumb" hx-swap-oob="true"><a hx-get="/v2/partials/requisitions" hx-target="#main-content" hx-push-url="/v2/requisitions" class="text-brand-500 hover:text-brand-600 cursor-pointer">Requisitions</a> <span class="text-gray-500">></span> <span class="text-gray-900">{{ req.name }}</span></div>`
2. Header card with:
   - Name (inline editable: click shows input, `hx-put` to save, blur auto-saves)
   - Customer, due date, created by info line
   - Status badge (brand semantic colors)
   - Urgency badge
3. Tab bar using Alpine `x-data="{ activeTab: 'parts' }"`:
   - Tab buttons: Parts (default), Offers, Quotes, Buy Plans, Tasks, Activity
   - Active tab: `brand-500` text + bottom border
   - Each tab button: `@click="activeTab = 'tabname'"` + `hx-get="/v2/partials/requisitions/{{ req.id }}/tab/tabname"` + `hx-target="#tab-content"` + `hx-trigger="click"` (with `hx-swap="innerHTML"`)
   - Use `x-bind:class` for active styling
4. Tab content area: `<div id="tab-content">` — initially loads parts tab inline (no extra request)
5. Parts tab content rendered inline on first load (the other tabs lazy-load via HTMX)

- [x] **4.2** Add tab route to `app/routers/htmx_views.py`:

```python
@router.get("/v2/partials/requisitions/{req_id}/tab/{tab}", response_class=HTMLResponse)
async def requisition_tab(
    request: Request,
    req_id: int,
    tab: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
```

Valid tabs: `parts`, `offers`, `quotes`, `buy_plans`, `tasks`, `activity`. Load relevant data for each tab. Render corresponding `partials/requisitions/tabs/{tab}.html`. Return 404 for unknown tab names.

For the `parts` tab: query requirements with sighting counts.
For `offers` tab: query `Offer` model filtered by `requisition_id`.
For `quotes` tab: query `Quote` model filtered by `requisition_id`.
For `buy_plans` tab: query `BuyPlan` model filtered by `requisition_id`.
For `tasks` tab: query `RequisitionTask` filtered by `requisition_id`, with status filter param.
For `activity` tab: query activity events (if activity tracking model exists) or return placeholder.

- [x] **4.3** Create `app/templates/partials/requisitions/tabs/parts.html`:

```html
{# parts.html — Requirements table tab for requisition detail.
   Receives: req (Requisition), requirements (list of Requirement with sighting_count).
   Called by: requisition detail (inline on first load) and tab route.
   Depends on: brand palette.
#}
<div>
  {# Inline add requirement form #}
  <div class="mb-4 p-4 bg-brand-50 rounded-lg border border-brand-200">
    <form hx-post="/v2/partials/requisitions/{{ req.id }}/requirements"
          hx-target="#parts-tbody"
          hx-swap="afterbegin"
          @htmx:after-request.camel="if(event.detail.successful) { $el.reset(); Alpine.store('toast').message='Requirement added'; Alpine.store('toast').type='success'; Alpine.store('toast').show=true; }"
          class="flex flex-wrap items-end gap-3">
      <div>
        <label class="block text-xs font-medium text-gray-500 mb-1">MPN</label>
        <input type="text" name="primary_mpn" required placeholder="Part number"
               class="px-3 py-1.5 border border-gray-200 rounded-lg text-sm font-mono focus:ring-2 focus:ring-brand-500 focus:border-brand-500 w-48">
      </div>
      <div>
        <label class="block text-xs font-medium text-gray-500 mb-1">Qty</label>
        <input type="number" name="target_qty" min="1" value="1" placeholder="Qty"
               class="px-3 py-1.5 border border-gray-200 rounded-lg text-sm focus:ring-2 focus:ring-brand-500 focus:border-brand-500 w-24">
      </div>
      <div>
        <label class="block text-xs font-medium text-gray-500 mb-1">Manufacturer</label>
        <input type="text" name="brand" placeholder="Brand"
               class="px-3 py-1.5 border border-gray-200 rounded-lg text-sm focus:ring-2 focus:ring-brand-500 focus:border-brand-500 w-40">
      </div>
      <button type="submit" class="px-4 py-1.5 text-sm font-medium text-white bg-brand-500 rounded-lg hover:bg-brand-600">Add</button>
    </form>
  </div>

  {# Requirements table #}
  {% if requirements %}
  <div class="overflow-x-auto">
    <table class="min-w-full divide-y divide-gray-200">
      <thead class="bg-gray-50">
        <tr>
          <th class="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">MPN</th>
          <th class="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Brand</th>
          <th class="px-4 py-2 text-right text-xs font-medium text-gray-500 uppercase">Qty</th>
          <th class="px-4 py-2 text-right text-xs font-medium text-gray-500 uppercase">Target Price</th>
          <th class="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Status</th>
          <th class="px-4 py-2 text-right text-xs font-medium text-gray-500 uppercase">Sightings</th>
          <th class="px-4 py-2 text-center text-xs font-medium text-gray-500 uppercase">Actions</th>
        </tr>
      </thead>
      <tbody id="parts-tbody" class="divide-y divide-gray-200">
        {% for r in requirements %}
        <tr id="req-row-{{ r.id }}" class="hover:bg-brand-50 group"
            hx-trigger="dblclick"
            hx-get="/v2/partials/requisitions/{{ req.id }}/requirements/{{ r.id }}/edit"
            hx-target="#req-row-{{ r.id }}"
            hx-swap="outerHTML">
          <td class="px-4 py-2 text-sm font-mono font-medium text-gray-900">{{ r.primary_mpn or "\u2014" }}</td>
          <td class="px-4 py-2 text-sm text-gray-500">{{ r.brand or "\u2014" }}</td>
          <td class="px-4 py-2 text-sm text-gray-500 text-right">{{ "{:,}".format(r.target_qty) if r.target_qty else "\u2014" }}</td>
          <td class="px-4 py-2 text-sm text-gray-500 text-right">{{ "${:,.4f}".format(r.target_price) if r.target_price else "\u2014" }}</td>
          <td class="px-4 py-2">
            {% set st_colors = {
              "open": "bg-brand-100 text-brand-600",
              "sourcing": "bg-brand-100 text-brand-600",
              "offered": "bg-emerald-50 text-emerald-700",
              "quoted": "bg-amber-50 text-amber-700"
            } %}
            <span class="inline-flex px-2 py-0.5 text-xs font-medium rounded-full {{ st_colors.get(r.sourcing_status, 'bg-gray-100 text-gray-600') }}">
              {{ r.sourcing_status|capitalize }}
            </span>
          </td>
          <td class="px-4 py-2 text-sm text-gray-500 text-right">{{ r.sighting_count }}</td>
          <td class="px-4 py-2 text-center">
            <div class="flex items-center justify-center gap-2">
              <button hx-post="/v2/partials/search/run?requirement_id={{ r.id }}&mpn={{ r.primary_mpn or '' }}"
                      hx-target="#search-results-{{ r.id }}"
                      hx-indicator="#spinner-{{ r.id }}"
                      class="text-xs text-brand-500 hover:text-brand-600 font-medium"
                      @click.stop>
                Search
                <span id="spinner-{{ r.id }}" class="htmx-indicator ml-1">
                  <svg class="inline h-3 w-3 animate-spin" viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="10" stroke="currentColor" stroke-width="3" stroke-dasharray="31.4" stroke-dashoffset="10"/></svg>
                </span>
              </button>
              <button hx-delete="/v2/partials/requisitions/{{ req.id }}/requirements/{{ r.id }}"
                      hx-target="#req-row-{{ r.id }}"
                      hx-swap="delete"
                      hx-confirm="Delete this requirement?"
                      class="text-xs text-rose-500 hover:text-rose-700 font-medium opacity-0 group-hover:opacity-100"
                      @click.stop>
                Delete
              </button>
            </div>
          </td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
  {% else %}
  {% include "partials/shared/empty_state.html" with context %}
  {% endif %}
</div>
```

- [x] **4.4** Create `app/templates/partials/requisitions/tabs/offers.html`:

```html
{# offers.html — Offers tab for requisition detail.
   Receives: offers (list of Offer objects).
   Called by: requisition tab route.
   Depends on: brand palette.
#}
<div>
  {% if offers %}
  <div class="overflow-x-auto">
    <table class="min-w-full divide-y divide-gray-200">
      <thead class="bg-gray-50">
        <tr>
          <th class="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Vendor</th>
          <th class="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">MPN</th>
          <th class="px-4 py-2 text-right text-xs font-medium text-gray-500 uppercase">Qty</th>
          <th class="px-4 py-2 text-right text-xs font-medium text-gray-500 uppercase">Unit Price</th>
          <th class="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Lead Time</th>
          <th class="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Date</th>
          <th class="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Status</th>
        </tr>
      </thead>
      <tbody class="divide-y divide-gray-200">
        {% for o in offers %}
        <tr class="hover:bg-brand-50">
          <td class="px-4 py-2 text-sm text-gray-900">{{ o.vendor_name or "\u2014" }}</td>
          <td class="px-4 py-2 text-sm font-mono text-gray-900">{{ o.mpn or "\u2014" }}</td>
          <td class="px-4 py-2 text-sm text-gray-500 text-right">{{ "{:,}".format(o.qty_available) if o.qty_available else "\u2014" }}</td>
          <td class="px-4 py-2 text-sm text-right {{ 'text-emerald-700 font-medium' if o.unit_price else 'text-gray-500' }}">{{ "${:,.4f}".format(o.unit_price) if o.unit_price else "RFQ" }}</td>
          <td class="px-4 py-2 text-sm text-gray-500">{{ o.lead_time or "\u2014" }}</td>
          <td class="px-4 py-2 text-sm text-gray-500">{{ o.created_at.strftime('%b %d, %Y') if o.created_at else "\u2014" }}</td>
          <td class="px-4 py-2">
            {% set offer_colors = {
              "active": "bg-emerald-50 text-emerald-700",
              "pending_review": "bg-amber-50 text-amber-700",
              "approved": "bg-emerald-50 text-emerald-700",
              "rejected": "bg-rose-50 text-rose-700",
              "draft": "bg-brand-100 text-brand-600"
            } %}
            <span class="inline-flex px-2 py-0.5 text-xs font-medium rounded-full {{ offer_colors.get(o.status, 'bg-gray-100 text-gray-600') }}">
              {{ (o.status or 'unknown')|replace('_', ' ')|capitalize }}
            </span>
          </td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
  {% else %}
  <div class="p-8 text-center">
    <p class="text-sm text-gray-500">No offers received yet.</p>
  </div>
  {% endif %}
</div>
```

- [x] **4.5** Create `app/templates/partials/requisitions/tabs/quotes.html`:

```html
{# quotes.html — Quotes tab for requisition detail.
   Receives: quotes (list of Quote objects).
   Called by: requisition tab route.
#}
<div>
  {% if quotes %}
  <div class="overflow-x-auto">
    <table class="min-w-full divide-y divide-gray-200">
      <thead class="bg-gray-50">
        <tr>
          <th class="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Quote #</th>
          <th class="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Customer</th>
          <th class="px-4 py-2 text-right text-xs font-medium text-gray-500 uppercase">Total</th>
          <th class="px-4 py-2 text-right text-xs font-medium text-gray-500 uppercase">Margin %</th>
          <th class="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Status</th>
          <th class="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Created</th>
        </tr>
      </thead>
      <tbody class="divide-y divide-gray-200">
        {% for q in quotes %}
        <tr class="hover:bg-brand-50 cursor-pointer"
            hx-get="/v2/partials/quotes/{{ q.id }}"
            hx-target="#main-content"
            hx-push-url="/v2/quotes/{{ q.id }}">
          <td class="px-4 py-2 text-sm font-medium text-brand-500">{{ q.quote_number or "Q-" ~ q.id }}</td>
          <td class="px-4 py-2 text-sm text-gray-500">{{ q.customer_name or "\u2014" }}</td>
          <td class="px-4 py-2 text-sm text-gray-500 text-right">{{ "${:,.2f}".format(q.total_amount) if q.total_amount else "\u2014" }}</td>
          <td class="px-4 py-2 text-sm text-right">
            {% if q.margin_pct is not none %}
              {% if q.margin_pct >= 30 %}
                <span class="text-emerald-700 font-medium">{{ "%.1f%%"|format(q.margin_pct) }}</span>
              {% elif q.margin_pct >= 15 %}
                <span class="text-amber-700 font-medium">{{ "%.1f%%"|format(q.margin_pct) }}</span>
              {% else %}
                <span class="text-rose-700 font-medium">{{ "%.1f%%"|format(q.margin_pct) }}</span>
              {% endif %}
            {% else %}
              <span class="text-gray-500">\u2014</span>
            {% endif %}
          </td>
          <td class="px-4 py-2">
            {% set quote_colors = {"draft": "bg-brand-100 text-brand-600", "sent": "bg-amber-50 text-amber-700", "won": "bg-emerald-50 text-emerald-700", "lost": "bg-rose-50 text-rose-700"} %}
            <span class="inline-flex px-2 py-0.5 text-xs font-medium rounded-full {{ quote_colors.get(q.status, 'bg-gray-100 text-gray-600') }}">
              {{ q.status|capitalize }}
            </span>
          </td>
          <td class="px-4 py-2 text-sm text-gray-500">{{ q.created_at.strftime('%b %d, %Y') if q.created_at else "\u2014" }}</td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
  {% else %}
  <div class="p-8 text-center">
    <p class="text-sm text-gray-500">No quotes generated.</p>
  </div>
  {% endif %}
</div>
```

- [x] **4.6** Create `app/templates/partials/requisitions/tabs/buy_plans.html`:

```html
{# buy_plans.html — Buy Plans tab for requisition detail.
   Receives: buy_plans (list of BuyPlan objects).
   Called by: requisition tab route.
#}
<div>
  {% if buy_plans %}
  <div class="overflow-x-auto">
    <table class="min-w-full divide-y divide-gray-200">
      <thead class="bg-gray-50">
        <tr>
          <th class="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Buy Plan #</th>
          <th class="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">SO#</th>
          <th class="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Status</th>
          <th class="px-4 py-2 text-right text-xs font-medium text-gray-500 uppercase">Lines</th>
          <th class="px-4 py-2 text-right text-xs font-medium text-gray-500 uppercase">Total Cost</th>
          <th class="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Created</th>
        </tr>
      </thead>
      <tbody class="divide-y divide-gray-200">
        {% for bp in buy_plans %}
        <tr class="hover:bg-brand-50 cursor-pointer"
            hx-get="/v2/partials/buy-plans/{{ bp.id }}"
            hx-target="#main-content"
            hx-push-url="/v2/buy-plans/{{ bp.id }}">
          <td class="px-4 py-2 text-sm font-medium text-brand-500">#{{ bp.id }}</td>
          <td class="px-4 py-2 text-sm text-gray-500">{{ bp.sales_order_number or "\u2014" }}</td>
          <td class="px-4 py-2">
            {% set bp_colors = {"draft": "bg-brand-100 text-brand-600", "pending_approval": "bg-amber-50 text-amber-700", "approved": "bg-emerald-50 text-emerald-700", "active": "bg-emerald-50 text-emerald-700", "completed": "bg-emerald-50 text-emerald-700", "cancelled": "bg-gray-100 text-gray-600"} %}
            <span class="inline-flex px-2 py-0.5 text-xs font-medium rounded-full {{ bp_colors.get(bp.status, 'bg-gray-100 text-gray-600') }}">
              {{ (bp.status or 'unknown')|replace('_', ' ')|capitalize }}
            </span>
          </td>
          <td class="px-4 py-2 text-sm text-gray-500 text-right">{{ bp.lines|length if bp.lines else 0 }}</td>
          <td class="px-4 py-2 text-sm text-gray-500 text-right">{{ "${:,.2f}".format(bp.total_cost) if bp.total_cost else "\u2014" }}</td>
          <td class="px-4 py-2 text-sm text-gray-500">{{ bp.created_at.strftime('%b %d, %Y') if bp.created_at else "\u2014" }}</td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
  {% else %}
  <div class="p-8 text-center">
    <p class="text-sm text-gray-500">No buy plans linked.</p>
  </div>
  {% endif %}
</div>
```

- [x] **4.7** Create `app/templates/partials/requisitions/tabs/tasks.html`:

```html
{# tasks.html — Task board tab for requisition detail.
   Receives: tasks (list of RequisitionTask), req (Requisition), users (list for assignee dropdown).
   Called by: requisition tab route.
   Depends on: RequisitionTask model (app/models/task.py), task.py router.
#}
<div x-data="{ taskFilter: 'all' }">
  {# Filter buttons #}
  <div class="flex gap-2 mb-4">
    {% for f in ['all', 'todo', 'in_progress', 'done'] %}
    <button @click="taskFilter = '{{ f }}'"
            :class="taskFilter === '{{ f }}' ? 'bg-brand-500 text-white' : 'bg-white text-gray-900 border border-gray-200 hover:bg-brand-50'"
            class="px-3 py-1 text-xs font-medium rounded-full transition-colors">
      {{ f|replace('_', ' ')|capitalize }}
    </button>
    {% endfor %}
  </div>

  {# Add task form #}
  <div class="mb-4 p-4 bg-brand-50 rounded-lg border border-brand-200">
    <form hx-post="/api/requisitions/{{ req.id }}/tasks"
          hx-target="#task-list"
          hx-swap="afterbegin"
          @htmx:after-request.camel="if(event.detail.successful) { $el.reset(); }"
          class="flex flex-wrap items-end gap-3">
      <div class="flex-1 min-w-[150px]">
        <input type="text" name="title" required placeholder="Task title"
               class="w-full px-3 py-1.5 border border-gray-200 rounded-lg text-sm focus:ring-2 focus:ring-brand-500 focus:border-brand-500">
      </div>
      <select name="task_type" class="px-3 py-1.5 border border-gray-200 rounded-lg text-sm focus:ring-2 focus:ring-brand-500 focus:border-brand-500">
        <option value="general">General</option>
        <option value="sourcing">Sourcing</option>
        <option value="sales">Sales</option>
      </select>
      <select name="priority" class="px-3 py-1.5 border border-gray-200 rounded-lg text-sm focus:ring-2 focus:ring-brand-500 focus:border-brand-500">
        <option value="2">Medium</option>
        <option value="1">Low</option>
        <option value="3">High</option>
      </select>
      <select name="assigned_to_id" class="px-3 py-1.5 border border-gray-200 rounded-lg text-sm focus:ring-2 focus:ring-brand-500 focus:border-brand-500">
        <option value="">Unassigned</option>
        {% for u in users %}
        <option value="{{ u.id }}">{{ u.name }}</option>
        {% endfor %}
      </select>
      <input type="date" name="due_at" class="px-3 py-1.5 border border-gray-200 rounded-lg text-sm focus:ring-2 focus:ring-brand-500 focus:border-brand-500">
      <button type="submit" class="px-4 py-1.5 text-sm font-medium text-white bg-brand-500 rounded-lg hover:bg-brand-600">Add</button>
    </form>
  </div>

  {# Task list #}
  <div id="task-list" class="space-y-2">
    {% if tasks %}
      {% for t in tasks %}
      <div class="flex items-center gap-3 p-3 bg-white rounded-lg border border-gray-200 hover:border-brand-200"
           x-show="taskFilter === 'all' || taskFilter === '{{ t.status }}'">
        {# Complete checkbox #}
        <input type="checkbox" {{ 'checked' if t.status == 'done' }}
               hx-post="/api/requisitions/{{ req.id }}/tasks/{{ t.id }}/complete"
               hx-target="closest div"
               hx-swap="outerHTML"
               class="h-4 w-4 rounded border-gray-200 text-brand-500 focus:ring-brand-500">
        {# Type badge #}
        {% set type_colors = {"sourcing": "bg-brand-100 text-brand-600", "sales": "bg-amber-50 text-amber-700", "general": "bg-gray-100 text-gray-600"} %}
        <span class="inline-flex px-2 py-0.5 text-xs font-medium rounded-full {{ type_colors.get(t.task_type, 'bg-gray-100 text-gray-600') }}">
          {{ t.task_type|capitalize }}
        </span>
        {# Priority indicator #}
        {% if t.priority == 3 %}
          <span class="text-rose-500" title="High priority">
            <svg class="h-4 w-4" fill="currentColor" viewBox="0 0 20 20"><path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm1-11a1 1 0 10-2 0v4a1 1 0 102 0V7zm-1 8a1 1 0 100-2 1 1 0 000 2z" clip-rule="evenodd"/></svg>
          </span>
        {% endif %}
        {# Title #}
        <span class="flex-1 text-sm {{ 'line-through text-gray-500' if t.status == 'done' else 'text-gray-900' }}">{{ t.title }}</span>
        {# AI risk flag #}
        {% if t.ai_risk_flag %}
          <span class="text-xs text-amber-700 bg-amber-50 px-2 py-0.5 rounded-full">{{ t.ai_risk_flag }}</span>
        {% endif %}
        {# Assignee #}
        {% if t.assignee %}
          <span class="text-xs text-gray-500">{{ t.assignee.name }}</span>
        {% endif %}
        {# Due date #}
        {% if t.due_at %}
          <span class="text-xs text-gray-500">{{ t.due_at.strftime('%b %d') }}</span>
        {% endif %}
        {# AI score #}
        {% if t.ai_priority_score and t.ai_priority_score > 0 %}
          <span class="text-xs text-brand-400" title="AI Priority Score">{{ "%.0f"|format(t.ai_priority_score * 100) }}</span>
        {% endif %}
        {# Delete #}
        <button hx-delete="/api/requisitions/{{ req.id }}/tasks/{{ t.id }}"
                hx-target="closest div"
                hx-swap="delete"
                hx-confirm="Delete this task?"
                class="text-gray-500 hover:text-rose-500">
          <svg class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/></svg>
        </button>
      </div>
      {% endfor %}
    {% else %}
      <div class="p-8 text-center">
        <p class="text-sm text-gray-500">No tasks yet. Add a task to track work on this requisition.</p>
      </div>
    {% endif %}
  </div>
</div>
```

- [x] **4.8** Create `app/templates/partials/requisitions/tabs/activity.html`:

```html
{# activity.html — Activity timeline tab for requisition detail.
   Receives: activities (list of activity dicts or empty).
   Called by: requisition tab route.
#}
<div>
  {% if activities %}
  <div class="space-y-3">
    {% for a in activities %}
    <div class="flex gap-3 p-3 border-l-2 border-brand-200">
      <div class="flex-shrink-0">
        <div class="h-8 w-8 rounded-full bg-brand-100 flex items-center justify-center text-xs font-medium text-brand-600">
          {{ a.user_name[0]|upper if a.user_name else "S" }}
        </div>
      </div>
      <div class="flex-1">
        <p class="text-sm text-gray-900">{{ a.description }}</p>
        <p class="text-xs text-gray-500 mt-0.5">{{ a.timestamp }}</p>
      </div>
    </div>
    {% endfor %}
  </div>
  {% else %}
  <div class="p-8 text-center">
    <p class="text-sm text-gray-500">No activity recorded yet.</p>
  </div>
  {% endif %}
</div>
```

- [x] **4.9** Update `requisition_detail_partial` in `app/routers/htmx_views.py` to also load offers count and pass `users` list for the tasks tab assignee dropdown. Add `req.offer_count = len(req.offers) if req.offers else 0`.

- [x] **4.10** Run tests:
```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_htmx_views.py -v -k "requisition" --no-header -q
```

- [x] **4.11** Commit:
```bash
git add app/templates/htmx/partials/requisitions/detail.html app/templates/partials/requisitions/tabs/ app/routers/htmx_views.py
git commit -m "Add tabbed requisition detail with parts, offers, quotes, buy plans, tasks, activity tabs"
```

---

## Task 5: Requisition Delete Requirement Route

**Files to modify:**
- `app/routers/htmx_views.py` — add `DELETE /v2/partials/requisitions/{req_id}/requirements/{req_item_id}` route

**Steps:**

- [x] **5.1** Add delete requirement route to `app/routers/htmx_views.py`:

```python
@router.delete("/v2/partials/requisitions/{req_id}/requirements/{item_id}", response_class=HTMLResponse)
async def delete_requirement(
    request: Request,
    req_id: int,
    item_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Delete a requirement from a requisition. Returns empty response for hx-swap='delete'."""
    req = db.query(Requisition).filter(Requisition.id == req_id).first()
    if not req:
        raise HTTPException(404, "Requisition not found")
    item = db.query(Requirement).filter(
        Requirement.id == item_id, Requirement.requisition_id == req_id
    ).first()
    if not item:
        raise HTTPException(404, "Requirement not found")
    db.delete(item)
    db.commit()
    return HTMLResponse("")
```

- [x] **5.2** Write test:
```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_htmx_views.py -v -k "delete_requirement" --no-header -q
```

- [x] **5.3** Commit:
```bash
git add app/routers/htmx_views.py
git commit -m "Add DELETE requirement endpoint for inline removal"
```

---

## Task 6: Companies List Rebuild (Brand Colors, Avatar, OOB Breadcrumb)

**Files to modify:**
- `app/templates/htmx/partials/companies/list.html` — full rewrite with brand colors

**Steps:**

- [x] **6.1** Rewrite `app/templates/htmx/partials/companies/list.html`:

The template should include:
1. OOB breadcrumb: `<div id="breadcrumb" hx-swap-oob="true">Companies</div>`
2. Page header with count
3. Search input with `hx-trigger="keyup changed delay:300ms"`, `hx-get="/v2/partials/companies"`, `hx-target="#main-content"`, `hx-push-url="true"`
4. Table with columns: Company (name + domain + initial avatar circle with `bg-brand-100 text-brand-600`), Account Type badge, Industry, Owner, Sites count, Open Reqs count
5. Account type badges using spec colors:
   - Customer: `bg-emerald-50 text-emerald-700`
   - Prospect: `bg-brand-100 text-brand-600`
   - Partner: `bg-brand-100 text-brand-300` (use `brand-300` text per spec)
   - Competitor: `bg-rose-50 text-rose-700`
6. Clickable rows with `hx-push-url`
7. Pagination (inline prev/next or shared partial)
8. Empty state
9. All `blue-600` replaced with `brand-500`, `blue-500` hover with `brand-600`, etc.

- [x] **6.2** Run tests:
```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_htmx_views.py -v -k "compan" --no-header -q
```

- [x] **6.3** Commit:
```bash
git add app/templates/htmx/partials/companies/list.html
git commit -m "Rebuild companies list with brand colors and avatar circles"
```

---

## Task 7: Companies Detail Rebuild (Tabs, Enrich, Click-to-Call)

**Files to modify:**
- `app/templates/htmx/partials/companies/detail.html` — full rewrite with tabs
- `app/routers/htmx_views.py` — update company detail route to load contacts; add tab route

**Steps:**

- [x] **7.1** Update `company_detail_partial` in `app/routers/htmx_views.py`:

Add loading of company contacts. Query contacts from `Contact` model (or the company's sites' contacts, depending on model structure). Count open requisitions for the company. Pass `contacts`, `open_req_count`, `user` to template context.

Add tab route:
```python
@router.get("/v2/partials/companies/{company_id}/tab/{tab}", response_class=HTMLResponse)
async def company_tab(
    request: Request,
    company_id: int,
    tab: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
```

Valid tabs: `sites`, `contacts`, `requisitions`, `activity`. Each loads relevant data and renders inline HTML (no separate tab template files needed for companies since they are simpler — render directly in the route or use a single template with conditionals).

- [x] **7.2** Rewrite `app/templates/htmx/partials/companies/detail.html`:

The template should include:
1. OOB breadcrumb: `<div id="breadcrumb" hx-swap-oob="true"><a ... class="text-brand-500 hover:text-brand-600 cursor-pointer">Companies</a> <span class="text-gray-500">></span> <span class="text-gray-900">{{ company.name }}</span></div>`
2. Header card with company name, domain, industry, city, account type badge (brand semantic colors)
3. Quick info grid (4 cols): Account Owner, Credit Terms, Phone, Employees
4. Stats row (3 cols): Sites count, Open Requisitions count, Created date — white cards with brand-50 hover
5. Enrich button: `{% include "partials/shared/enrich_button.html" %}` with `entity_type="company"` and `entity_id=company.id`
6. Tab bar using Alpine `x-data="{ activeTab: 'sites' }"`:
   - Tabs: Sites (default), Contacts, Requisitions, Activity
   - Active: `border-brand-500 text-brand-500`
   - Inactive: `border-transparent text-gray-500 hover:text-gray-900 hover:border-gray-200`
   - Each loads via `hx-get="/v2/partials/companies/{{ company.id }}/tab/{tab}"` into `#company-tab-content`
7. Tab content div `id="company-tab-content"` — sites tab rendered inline on first load
8. Sites table: Site Name, Type, City, Country
9. Contacts tab (loaded via HTMX): Name, Title, Email, Phone with click-to-call:
   - Phone numbers as `<a href="tel:{{ contact.phone }}" class="text-brand-500 hover:text-brand-600">{{ contact.phone }}</a>`
   - If user has `eight_by_eight_enabled`, add phone icon button: `hx-post="/api/activity"` with `origin=click_to_call` body data, then `onclick="window.location.href='tel:...'"` after logging
10. Notes section (if present)
11. All brand colors, no blue-600

- [x] **7.3** Run tests:
```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_htmx_views.py -v -k "compan" --no-header -q
```

- [x] **7.4** Commit:
```bash
git add app/templates/htmx/partials/companies/detail.html app/routers/htmx_views.py
git commit -m "Rebuild company detail with tabs, enrich button, and click-to-call contacts"
```

---

## Task 8: Vendors List Rebuild (Table Layout, Blacklisted Toggle, Sortable)

**Files to modify:**
- `app/templates/htmx/partials/vendors/list.html` — full rewrite from cards to table
- `app/routers/htmx_views.py` — update `vendors_list_partial` to accept `hide_blacklisted`, `sort`, `dir` params

**Steps:**

- [x] **8.1** Update `vendors_list_partial` in `app/routers/htmx_views.py`:

```python
@router.get("/v2/partials/vendors", response_class=HTMLResponse)
async def vendors_list_partial(
    request: Request,
    q: str = "",
    hide_blacklisted: bool = True,
    sort: str = "sighting_count",
    dir: str = "desc",
    limit: int = Query(30, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
```

Change blacklisted filtering: if `hide_blacklisted` is True, filter out blacklisted; otherwise include all. Add sort logic mapping column name + direction (valid columns: `display_name`, `sighting_count`, `overall_win_rate`, `hq_country`, `industry`). Pass `hide_blacklisted`, `sort`, `dir` to template context.

- [x] **8.2** Rewrite `app/templates/htmx/partials/vendors/list.html` from card grid to table:

The template should include:
1. OOB breadcrumb: `<div id="breadcrumb" hx-swap-oob="true">Vendors</div>`
2. Page header with count
3. Filter bar:
   - Search input with `hx-trigger="keyup changed delay:300ms"`, `hx-include="#vendor-filters"`
   - "Hide blacklisted" toggle: Alpine `x-data="{ hideBlacklisted: {{ 'true' if hide_blacklisted else 'false' }} }"` with checkbox that toggles hidden input and triggers request
   - All wrapped in `<form id="vendor-filters">`
4. Table (dense, scannable):
   - Sortable column headers: each `<a>` with `hx-get="/v2/partials/vendors"` passing `sort` and `dir`, with `hx-include="#vendor-filters"`, `hx-target="#main-content"`, `hx-push-url="true"`. Active sort shows arrow.
   - Columns: Vendor Name + Domain, Score/Blacklisted badge, Sightings count, Win Rate %, Location, Industry
   - Blacklisted rows: `bg-rose-50` background, prominent `bg-rose-50 text-rose-700` "Blacklisted" badge
   - Non-blacklisted with score: `bg-emerald-50 text-emerald-700` score badge
   - Clickable rows with `hx-push-url`
5. Pagination
6. Empty state

Template row for each vendor:
```html
<tr class="{{ 'bg-rose-50' if v.is_blacklisted else '' }} hover:bg-brand-50 cursor-pointer"
    hx-get="/v2/partials/vendors/{{ v.id }}"
    hx-target="#main-content"
    hx-push-url="/v2/vendors/{{ v.id }}">
  <td class="px-4 py-3">
    <div>
      <p class="text-sm font-medium text-brand-500">{{ v.display_name }}</p>
      {% if v.domain %}<p class="text-xs text-gray-500">{{ v.domain }}</p>{% endif %}
    </div>
  </td>
  <td class="px-4 py-3">
    {% if v.is_blacklisted %}
      <span class="inline-flex px-2 py-0.5 text-xs font-medium rounded-full bg-rose-50 text-rose-700">Blacklisted</span>
    {% elif v.vendor_score %}
      <span class="inline-flex px-2 py-0.5 text-xs font-medium rounded-full bg-emerald-50 text-emerald-700">{{ "%.0f"|format(v.vendor_score) }}</span>
    {% else %}
      <span class="text-xs text-gray-500">\u2014</span>
    {% endif %}
  </td>
  <td class="px-4 py-3 text-sm text-gray-500">{{ v.sighting_count or 0 }}</td>
  <td class="px-4 py-3 text-sm text-gray-500">{{ "%.0f%%"|format(v.overall_win_rate * 100) if v.overall_win_rate else "\u2014" }}</td>
  <td class="px-4 py-3 text-sm text-gray-500">{{ v.hq_country or "\u2014" }}</td>
  <td class="px-4 py-3 text-sm text-gray-500">{{ v.industry or "\u2014" }}</td>
</tr>
```

- [x] **8.3** Run tests:
```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_htmx_views.py -v -k "vendor" --no-header -q
```

- [x] **8.4** Commit:
```bash
git add app/templates/htmx/partials/vendors/list.html app/routers/htmx_views.py
git commit -m "Rebuild vendors list as sortable table with blacklisted toggle"
```

---

## Task 9: Safety Review Shared Partial

**Files to create:**
- `app/templates/partials/shared/safety_review.html`

**Steps:**

- [x] **9.1** Create `app/templates/partials/shared/safety_review.html`:

```html
{# safety_review.html — Vendor safety review block.
   Receives: safety_band (str: low/medium/high or None),
             safety_summary (str or None),
             safety_flags (list of dicts with 'type' and 'text' keys, or None).
   Called by: vendor detail, sourcing lead detail.
   Depends on: brand palette.
#}
{% if safety_band %}
<div class="bg-white rounded-lg border border-gray-200 p-4">
  <h3 class="text-sm font-semibold text-gray-900 mb-3">Safety Review</h3>

  {# Safety band indicator #}
  {% set band_colors = {
    "low": {"bg": "bg-emerald-50", "text": "text-emerald-700", "border": "border-emerald-200", "bar": "bg-emerald-500", "label": "Low Risk"},
    "medium": {"bg": "bg-amber-50", "text": "text-amber-700", "border": "border-amber-200", "bar": "bg-amber-500", "label": "Medium Risk"},
    "high": {"bg": "bg-rose-50", "text": "text-rose-700", "border": "border-rose-200", "bar": "bg-rose-500", "label": "High Risk"}
  } %}
  {% set bc = band_colors.get(safety_band, band_colors.low) %}

  <div class="flex items-center gap-3 mb-3 p-3 {{ bc.bg }} rounded-lg border {{ bc.border }}">
    <div class="h-3 w-3 rounded-full {{ bc.bar }}"></div>
    <span class="text-sm font-medium {{ bc.text }}">{{ bc.label }}</span>
  </div>

  {# Summary #}
  {% if safety_summary %}
  <p class="text-sm text-gray-900 mb-3">{{ safety_summary }}</p>
  {% endif %}

  {# Positive signals #}
  {% if safety_flags %}
    {% set positives = safety_flags|selectattr('type', 'equalto', 'positive')|list %}
    {% set cautions = safety_flags|selectattr('type', 'equalto', 'caution')|list %}

    {% if positives %}
    <div class="mb-2">
      <p class="text-xs font-medium text-gray-500 mb-1">Positive Signals</p>
      <ul class="space-y-1">
        {% for s in positives %}
        <li class="flex items-start gap-2 text-sm text-emerald-700">
          <svg class="h-4 w-4 mt-0.5 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"/></svg>
          {{ s.text }}
        </li>
        {% endfor %}
      </ul>
    </div>
    {% endif %}

    {% if cautions %}
    <div class="mb-2">
      <p class="text-xs font-medium text-gray-500 mb-1">Caution Signals</p>
      <ul class="space-y-1">
        {% for s in cautions %}
        <li class="flex items-start gap-2 text-sm text-amber-700">
          <svg class="h-4 w-4 mt-0.5 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"/></svg>
          {{ s.text }}
        </li>
        {% endfor %}
      </ul>
    </div>
    {% endif %}
  {% endif %}
</div>
{% else %}
<div class="p-4 text-center text-sm text-gray-500">
  No safety data available — safety is assessed when sourcing leads are created.
</div>
{% endif %}
```

- [x] **9.2** Commit:
```bash
git add app/templates/partials/shared/safety_review.html
git commit -m "Add reusable safety review partial for vendor and lead detail"
```

---

## Task 10: Vendor Detail Rebuild (Tabs, Enrich, Click-to-Call, Safety, Analytics)

**Files to modify:**
- `app/templates/htmx/partials/vendors/detail.html` — full rewrite with tabs
- `app/routers/htmx_views.py` — update vendor detail route; add vendor tab route

**Steps:**

- [x] **10.1** Update `vendor_detail_partial` in `app/routers/htmx_views.py`:

Add loading of safety data from recent `SourcingLead` records for this vendor (aggregate from most recent leads). Query: `db.query(SourcingLead).filter(SourcingLead.vendor_name_normalized == vendor.normalized_name).order_by(SourcingLead.created_at.desc()).first()`. Extract `vendor_safety_band`, `vendor_safety_summary`, `vendor_safety_flags` from the lead. Pass `safety_band`, `safety_summary`, `safety_flags` to template context.

Add tab route:
```python
@router.get("/v2/partials/vendors/{vendor_id}/tab/{tab}", response_class=HTMLResponse)
async def vendor_tab(
    request: Request,
    vendor_id: int,
    tab: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
```

Valid tabs: `overview`, `contacts`, `analytics`, `offers`.

For `overview`: safety review + contact info + recent sightings (already loaded in detail route, but tab route should re-query).
For `contacts`: query `VendorContact` records, render contacts table with click-to-call.
For `analytics`: call vendor_analytics endpoints internally or query directly. Render stats grid + offer history table + parts summary table.
For `offers`: query `Offer` model filtered by `vendor_card_id`.

- [x] **10.2** Rewrite `app/templates/htmx/partials/vendors/detail.html`:

The template should include:
1. OOB breadcrumb: `<div id="breadcrumb" hx-swap-oob="true"><a ... class="text-brand-500 hover:text-brand-600 cursor-pointer">Vendors</a> <span class="text-gray-500">></span> <span class="text-gray-900">{{ vendor.display_name }}</span></div>`
2. Header card:
   - Vendor name, domain, city/country, industry
   - Score (large number, emerald) or Blacklisted badge (rose)
   - All brand colors
3. 4-stat row: Sightings, Win Rate, Total POs, Avg Response Time — white cards
4. Enrich button: `{% include "partials/shared/enrich_button.html" %}` with `entity_type="vendor"` and `entity_id=vendor.id`
5. Tab bar using Alpine `x-data="{ activeTab: 'overview' }"`:
   - Tabs: Overview (default), Contacts, Analytics, Offers
   - Active: `border-brand-500 text-brand-500`
   - Each loads via `hx-get="/v2/partials/vendors/{{ vendor.id }}/tab/{tab}"` into `#vendor-tab-content`
6. Tab content div `id="vendor-tab-content"` — overview tab rendered inline on first load

**Overview tab content (inline):**
- Safety review block: `{% include "partials/shared/safety_review.html" %}`
- Contact info card (website, emails, phones)
- Recent sightings table: MPN, Qty, Price, Source badge (with distinct hue per source), Date

**Source badges in sightings** (reusable pattern):
```html
{% set source_colors = {
  "brokerbin": "bg-sky-50 text-sky-700",
  "nexar": "bg-violet-50 text-violet-700",
  "digikey": "bg-orange-50 text-orange-700",
  "mouser": "bg-teal-50 text-teal-700",
  "oemsecrets": "bg-fuchsia-50 text-fuchsia-700",
  "element14": "bg-lime-50 text-lime-700",
  "ebay": "bg-yellow-50 text-yellow-700"
} %}
<span class="inline-flex px-2 py-0.5 text-xs font-medium rounded-full {{ source_colors.get((s.source_type or '')|lower, 'bg-gray-100 text-gray-600') }}">
  {{ s.source_type or "\u2014" }}
</span>
```

- [x] **10.3** Implement the vendor tab route handler in `app/routers/htmx_views.py`. For each tab, render HTML directly using `HTMLResponse` or create minimal tab templates.

**Contacts tab HTML** should include click-to-call:
```html
{# For each contact phone #}
<td class="px-4 py-2 text-sm">
  {% if c.phone %}
    <a href="tel:{{ c.phone }}" class="text-brand-500 hover:text-brand-600">{{ c.phone }}</a>
    {% if user.eight_by_eight_enabled|default(false) %}
    <button hx-post="/api/activity"
            hx-vals='{"origin": "click_to_call", "entity_type": "vendor_contact", "entity_id": "{{ c.id }}", "vendor_card_id": "{{ vendor_id }}"}'
            hx-swap="none"
            onclick="window.location.href='tel:{{ c.phone }}'"
            class="ml-1 text-brand-400 hover:text-brand-500" title="Call & log">
      <svg class="h-4 w-4 inline" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 5a2 2 0 012-2h3.28a1 1 0 01.948.684l1.498 4.493a1 1 0 01-.502 1.21l-2.257 1.13a11.042 11.042 0 005.516 5.516l1.13-2.257a1 1 0 011.21-.502l4.493 1.498a1 1 0 01.684.949V19a2 2 0 01-2 2h-1C9.716 21 3 14.284 3 6V5z"/></svg>
    </button>
    {% endif %}
  {% else %}
    <span class="text-gray-500">\u2014</span>
  {% endif %}
</td>
```

**Analytics tab** should render:
- Stats grid: Win Rate, Response Rate, Quote Quality Rate, Avg Response Hours, Engagement Score, Vendor Score — 6 stat cards in a 3x2 grid
- Offer history table fetched from vendor data (or loaded via separate HTMX call to `/api/vendors/{card_id}/offer-history`)
- Parts summary table (or loaded via HTMX call to `/api/vendors/{card_id}/parts-summary`)
- Empty state: "No analytics data yet — data builds as you interact with this vendor."

- [x] **10.4** Run tests:
```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_htmx_views.py -v -k "vendor" --no-header -q
```

- [x] **10.5** Commit:
```bash
git add app/templates/htmx/partials/vendors/detail.html app/templates/partials/shared/safety_review.html app/routers/htmx_views.py
git commit -m "Rebuild vendor detail with tabs, safety review, analytics, enrich, click-to-call"
```

---

## Task 11: Add Tests for All New Routes

**Files to create/modify:**
- `tests/test_htmx_core_pages.py` — new test file for Plan 2 routes

**Steps:**

- [x] **11.1** Create `tests/test_htmx_core_pages.py` with tests covering:

```python
"""Tests for Plan 2 core page HTMX routes.

Tests requisition list filters, bulk ops, tabs, create modal,
company tabs/enrich, vendor tabs/sort/blacklisted toggle.

Called by: pytest
Depends on: conftest.py fixtures, app/routers/htmx_views.py
"""
```

Test cases:
1. `test_requisitions_list_with_filters` — GET `/v2/partials/requisitions?status=active&urgency=hot&sort=name&dir=asc` returns 200
2. `test_requisitions_list_owner_filter` — GET with `owner=1` returns 200
3. `test_requisitions_list_date_filter` — GET with `date_from=2026-01-01&date_to=2026-12-31` returns 200
4. `test_requisitions_create_form` — GET `/v2/partials/requisitions/create-form` returns 200 with form HTML
5. `test_requisitions_create_returns_row` — POST `/v2/partials/requisitions/create` returns single `<tr>` (not full list)
6. `test_requisitions_bulk_archive` — POST `/v2/partials/requisitions/bulk/archive` with `ids=1,2` returns 200
7. `test_requisitions_tab_parts` — GET `/v2/partials/requisitions/1/tab/parts` returns 200
8. `test_requisitions_tab_offers` — GET `/v2/partials/requisitions/1/tab/offers` returns 200
9. `test_requisitions_tab_tasks` — GET `/v2/partials/requisitions/1/tab/tasks` returns 200
10. `test_requisitions_tab_invalid` — GET `/v2/partials/requisitions/1/tab/invalid` returns 404
11. `test_delete_requirement` — DELETE `/v2/partials/requisitions/1/requirements/1` returns 200 empty
12. `test_companies_tab_sites` — GET `/v2/partials/companies/1/tab/sites` returns 200
13. `test_companies_tab_contacts` — GET `/v2/partials/companies/1/tab/contacts` returns 200
14. `test_vendors_list_hide_blacklisted` — GET `/v2/partials/vendors?hide_blacklisted=false` includes blacklisted vendors
15. `test_vendors_list_sort` — GET `/v2/partials/vendors?sort=display_name&dir=asc` returns 200
16. `test_vendors_tab_overview` — GET `/v2/partials/vendors/1/tab/overview` returns 200
17. `test_vendors_tab_analytics` — GET `/v2/partials/vendors/1/tab/analytics` returns 200
18. `test_vendors_tab_contacts_click_to_call` — GET `/v2/partials/vendors/1/tab/contacts` returns HTML with `tel:` links

All tests should use the `client` fixture from conftest.py, create test data (requisition, company, vendor) in fixtures, and assert response status codes and key HTML content.

- [x] **11.2** Run full test suite to verify no regressions:
```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_htmx_core_pages.py -v --no-header -q
```

- [x] **11.3** Run coverage check:
```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ --cov=app --cov-report=term-missing --tb=no -q
```

- [x] **11.4** Commit:
```bash
git add tests/test_htmx_core_pages.py
git commit -m "Add tests for Plan 2 HTMX core page routes"
```

---

## Task 12: Final Color Audit and Cleanup

**Files to modify:**
- All templates touched in Tasks 1-10

**Steps:**

- [x] **12.1** Search all modified templates for any remaining `blue-600`, `blue-500`, `blue-700`, `green-100`, `red-100`, `yellow-100` references. Replace with brand equivalents:
  - `blue-600` -> `brand-500`
  - `blue-700` -> `brand-600`
  - `blue-500` (hover) -> `brand-600`
  - `blue-100` -> `brand-100`
  - `green-100 text-green-800` -> `emerald-50 text-emerald-700`
  - `red-100 text-red-800` -> `rose-50 text-rose-700`
  - `yellow-100 text-yellow-800` -> `amber-50 text-amber-700`
  - `gray-900` (for sidebar bg) -> `brand-700`

Search command:
```bash
grep -rn "blue-600\|blue-500\|blue-700\|green-100\|red-100\|yellow-100" app/templates/htmx/partials/requisitions/ app/templates/htmx/partials/companies/ app/templates/htmx/partials/vendors/ app/templates/partials/requisitions/ app/templates/partials/shared/safety_review.html
```

- [x] **12.2** Verify all OOB breadcrumb divs are present in every list and detail partial:
  - `requisitions/list.html` — "Requisitions"
  - `requisitions/detail.html` — "Requisitions > {name}"
  - `companies/list.html` — "Companies"
  - `companies/detail.html` — "Companies > {name}"
  - `vendors/list.html` — "Vendors"
  - `vendors/detail.html` — "Vendors > {name}"

- [x] **12.3** Run full test suite:
```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v --no-header -q
```

- [x] **12.4** Commit:
```bash
git add -A
git commit -m "Color audit: replace all blue/green/red with brand/emerald/rose palette"
```

---

## Summary

| Task | Description | Files | Est. Time |
|------|-------------|-------|-----------|
| 1 | Requisition row partial | 1 new template | 5 min |
| 2 | Create modal + routes | 1 new template, 1 router update | 10 min |
| 3 | Requisitions list rebuild | 1 template rewrite, 1 router update | 20 min |
| 4 | Requisition detail tabs | 1 template rewrite, 6 new tab templates, 1 router update | 30 min |
| 5 | Delete requirement route | 1 router update | 5 min |
| 6 | Companies list rebuild | 1 template rewrite | 10 min |
| 7 | Companies detail tabs | 1 template rewrite, 1 router update | 15 min |
| 8 | Vendors list rebuild | 1 template rewrite, 1 router update | 15 min |
| 9 | Safety review partial | 1 new template | 5 min |
| 10 | Vendor detail tabs | 1 template rewrite, 1 router update | 25 min |
| 11 | Tests | 1 new test file | 20 min |
| 12 | Color audit | All templates | 10 min |
| **Total** | | **~20 files** | **~170 min** |

## Template Directory After Completion

```
app/templates/
├── htmx/partials/
│   ├── requisitions/
│   │   ├── list.html          (REWRITTEN)
│   │   └── detail.html        (REWRITTEN)
│   ├── companies/
│   │   ├── list.html          (REWRITTEN)
│   │   └── detail.html        (REWRITTEN)
│   └── vendors/
│       ├── list.html          (REWRITTEN)
│       └── detail.html        (REWRITTEN)
├── partials/
│   ├── requisitions/
│   │   ├── req_row.html       (NEW)
│   │   ├── create_modal.html  (NEW)
│   │   └── tabs/
│   │       ├── parts.html     (NEW)
│   │       ├── offers.html    (NEW)
│   │       ├── quotes.html    (NEW)
│   │       ├── buy_plans.html (NEW)
│   │       ├── tasks.html     (NEW)
│   │       └── activity.html  (NEW)
│   └── shared/
│       └── safety_review.html (NEW)
```
