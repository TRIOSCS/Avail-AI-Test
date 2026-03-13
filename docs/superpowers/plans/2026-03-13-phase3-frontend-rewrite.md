# Phase 3: Frontend Rewrite (HTMX + Alpine.js) Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace 26K lines of vanilla JS (app.js + crm.js) with server-rendered HTMX + Alpine.js. Rock solid, well-partitioned, stable.

**Architecture:** HTMX handles server communication (partial page updates, form submissions, search). Alpine.js handles client-side interactivity (dropdowns, modals, tabs, form validation). Jinja2 partials render HTML fragments. No client-side routing — server controls navigation.

**Tech Stack:** HTMX 2.x, Alpine.js 3.x, Jinja2, Vite (bundler), Python/FastAPI

**Spec:** `docs/superpowers/specs/2026-03-13-mvp-strip-down-design.md` (Phase 3 section)

---

## Pre-Implementation Notes

**Current state of the frontend:**
- `app/static/app.js` — 16,932 lines (requisitions, vendors, contacts, materials, dashboard, modals, mobile, sourcing drill-down)
- `app/static/crm.js` — 8,764 lines (companies, offers, quotes, buy plans, enrichment, settings, prospecting)
- `app/static/tickets.js` — self-heal tickets UI (may already be deleted by Phase 1)
- `app/static/touch.js` — touch/swipe handlers for mobile
- `app/templates/index.html` — 1,578 lines, single monolithic Jinja2 template with all views
- Hash-based routing via `showView()` and `window.location.hash`
- Window globals via `Object.assign(window, {...})` — ~120 exports in app.js, ~100 in crm.js
- Vite 6.0 build pipeline with rollup, Vitest for frontend tests
- No HTMX or Alpine currently in codebase

**Migration strategy:**
- Feature flag `USE_HTMX` in `app/config.py` gates which frontend loads
- Build new HTMX views alongside existing JS during migration
- Migrate one domain at a time — each task is independently deployable
- After all domains migrated (Task 12), delete old JS entirely

**Testing strategy:**
- Each task includes backend tests for new HTML-returning endpoints
- Frontend behavior tested via Playwright (browser snapshot assertions)
- Existing API JSON endpoints remain unchanged — only new `Accept: text/html` responses added

---

## Task 1: Foundation — Install HTMX + Alpine, Create Base Template

**Goal:** Set up the HTMX + Alpine.js infrastructure so subsequent tasks can build on it.

**Files to create:**
- `app/templates/base.html`
- `app/templates/partials/shared/modal.html`
- `app/templates/partials/shared/toast.html`
- `app/templates/partials/shared/pagination.html`
- `app/templates/partials/shared/empty_state.html`

**Files to modify:**
- `package.json` — add htmx.org, alpinejs dependencies
- `vite.config.js` — add HTMX + Alpine entry points
- `app/config.py` — add `USE_HTMX` feature flag
- `app/main.py` — add HTMX response detection middleware

- [ ] **Step 1: Install npm dependencies**

```bash
cd /root/availai && npm install htmx.org@^2 alpinejs@^3
```

- [ ] **Step 2: Update `vite.config.js` — add HTMX + Alpine entry point**

Add a new entry point `htmx_app` to `rollupOptions.input`:

```js
// In rollupOptions.input, add:
htmx_app: resolve(__dirname, "app/static/htmx_app.js"),
```

- [ ] **Step 3: Create `app/static/htmx_app.js` — HTMX + Alpine bootstrap**

```js
import htmx from 'htmx.org';
import Alpine from 'alpinejs';

window.htmx = htmx;
window.Alpine = Alpine;

// Global Alpine stores
Alpine.store('sidebar', { open: true, active: '' });
Alpine.store('toast', { message: '', type: 'info', show: false });

// HTMX config
htmx.config.defaultSwapStyle = 'innerHTML';
htmx.config.historyCacheSize = 0;
htmx.config.selfRequestsOnly = true;

// HTMX error handler — show toast on failed requests
htmx.on('htmx:responseError', (evt) => {
    Alpine.store('toast').message = 'Request failed. Please try again.';
    Alpine.store('toast').type = 'error';
    Alpine.store('toast').show = true;
});

// HTMX afterSettle — re-init Alpine on swapped content
htmx.on('htmx:afterSettle', () => {
    // Alpine auto-discovers new x-data elements
});

Alpine.start();
```

- [ ] **Step 4: Add `USE_HTMX` feature flag to `app/config.py`**

Add to the `Settings` class:

```python
# --- Feature Flags ---
use_htmx: bool = False
```

- [ ] **Step 5: Add HTMX detection utility to `app/dependencies.py`**

```python
def wants_html(request: Request) -> bool:
    """Return True if the client wants an HTML partial (HTMX request)."""
    return request.headers.get("HX-Request") == "true"

def is_htmx_boosted(request: Request) -> bool:
    """Return True if this is an hx-boost navigation (needs full page shell)."""
    return request.headers.get("HX-Boosted") == "true"
```

- [ ] **Step 6: Create `app/templates/base.html`**

Server-rendered shell template with nav, sidebar, content area, and script tags. Uses Jinja2 blocks for content injection. Includes HTMX + Alpine when `USE_HTMX` is enabled.

Key blocks:
- `{% block title %}` — page title
- `{% block content %}` — main content area (HTMX swap target, `id="main-content"`)
- `{% block scripts %}` — page-specific scripts
- Sidebar navigation with `hx-boost="true"` on all nav links
- Toast notification component (Alpine `x-data`)
- Modal container (Alpine `x-data`)

- [ ] **Step 7: Create `app/templates/partials/shared/modal.html`**

Reusable modal shell — Alpine controls open/close, HTMX loads body content:

```html
<div x-data="{ open: false }" x-show="open" @open-modal.window="open = true" @close-modal.window="open = false" @keydown.escape.window="open = false">
    <div class="modal-overlay" @click.self="open = false">
        <div class="modal-box" x-trap.noscroll="open">
            <div id="modal-content" hx-target="this">
                {% block modal_body %}{% endblock %}
            </div>
        </div>
    </div>
</div>
```

- [ ] **Step 8: Create `app/templates/partials/shared/toast.html`**

Toast notification component driven by Alpine store:

```html
<div x-data x-show="$store.toast.show" x-transition
     @click="$store.toast.show = false"
     x-init="$watch('$store.toast.show', v => v && setTimeout(() => $store.toast.show = false, 4000))"
     :class="{ 'toast-success': $store.toast.type === 'success', 'toast-error': $store.toast.type === 'error' }">
    <span x-text="$store.toast.message"></span>
</div>
```

- [ ] **Step 9: Create `app/templates/partials/shared/pagination.html`**

HTMX-powered pagination partial (receives `page`, `total_pages`, `target_id`, `url` as template vars):

```html
<nav class="pagination" aria-label="Pagination">
    {% if page > 1 %}
    <button hx-get="{{ url }}?page={{ page - 1 }}" hx-target="#{{ target_id }}" hx-swap="innerHTML">Prev</button>
    {% endif %}
    <span>Page {{ page }} of {{ total_pages }}</span>
    {% if page < total_pages %}
    <button hx-get="{{ url }}?page={{ page + 1 }}" hx-target="#{{ target_id }}" hx-swap="innerHTML">Next</button>
    {% endif %}
</nav>
```

- [ ] **Step 10: Create `app/templates/partials/shared/empty_state.html`**

```html
<div class="empty-state">
    <p>{{ message | default("No items found.") }}</p>
    {% if action_url %}
    <button hx-get="{{ action_url }}" hx-target="#main-content">{{ action_label | default("Create one") }}</button>
    {% endif %}
</div>
```

- [ ] **Step 11: Create directory structure**

```bash
mkdir -p app/templates/partials/{shared,requisitions,companies,vendors,quotes,buy_plans,prospecting,sourcing}
```

- [ ] **Step 12: Add HTMX view router — `app/routers/views.py`**

New router that serves full-page HTML views (when `USE_HTMX` is enabled). This is the server-side "router" that replaces hash-based navigation:

```python
from fastapi import APIRouter, Request, Depends
from app.dependencies import require_user
from app.config import get_settings

router = APIRouter(tags=["views"])

@router.get("/requisitions")
async def requisitions_page(request: Request, user=Depends(require_user)):
    """Serve the requisitions list page (full page or HTMX partial)."""
    template = "partials/requisitions/list.html"
    if not request.headers.get("HX-Request"):
        template = "base.html"  # wrap in shell
    return templates.TemplateResponse(template, {"request": request, "user": user})
```

- [ ] **Step 13: Register views router in `app/main.py`**

```python
if settings.use_htmx:
    from app.routers.views import router as views_router
    app.include_router(views_router)
```

- [ ] **Step 14: Run Vite build to verify HTMX + Alpine bundle**

```bash
cd /root/availai && npm run build
```

- [ ] **Step 15: Write tests**

File: `tests/test_htmx_foundation.py`

Tests:
- `test_base_template_renders` — GET `/` with `USE_HTMX=true` returns HTML with HTMX script tag
- `test_htmx_scripts_present` — base template includes `htmx.org` and `alpinejs` script references
- `test_wants_html_detection` — `wants_html()` returns True when `HX-Request: true` header present
- `test_feature_flag_off` — `USE_HTMX=false` serves old `index.html` template

- [ ] **Step 16: Commit**

```bash
git add package.json package-lock.json vite.config.js app/static/htmx_app.js \
    app/config.py app/dependencies.py app/main.py \
    app/templates/base.html app/templates/partials/ \
    app/routers/views.py tests/test_htmx_foundation.py
git commit -m "phase3-task1: foundation — install HTMX + Alpine, create base template and shared partials"
```

---

## Task 2: Shared Components — Sidebar Nav, Top Bar, Mobile Nav

**Goal:** Build the app shell components that wrap all views — sidebar navigation, top bar with search, and mobile bottom nav.

**Files to create:**
- `app/templates/partials/shared/sidebar.html`
- `app/templates/partials/shared/topbar.html`
- `app/templates/partials/shared/mobile_nav.html`

**Files to modify:**
- `app/templates/base.html` — include sidebar, topbar, mobile nav partials
- `app/routers/views.py` — add search endpoint

- [ ] **Step 1: Create `app/templates/partials/shared/sidebar.html`**

Alpine `x-data` for sidebar state (collapsed/expanded, active item). Navigation items:
- Requisitions (`/requisitions`)
- Companies (`/companies`)
- Vendors (`/vendors`)
- Quotes (`/quotes`)
- Buy Plans (`/buy-plans`)
- Prospecting (`/prospecting`)
- Settings (`/settings`)

All links use `hx-boost="true"` for SPA-like transitions. Active item highlighted via Alpine `$store.sidebar.active`.

```html
<nav x-data="{ collapsed: false }" class="sidebar" :class="{ 'sidebar-collapsed': collapsed }">
    <button @click="collapsed = !collapsed" class="sidebar-toggle">...</button>
    <ul>
        {% set nav_items = [
            ('requisitions', 'Requisitions', '/requisitions'),
            ('companies', 'Companies', '/companies'),
            ('vendors', 'Vendors', '/vendors'),
            ('quotes', 'Quotes', '/quotes'),
            ('buy-plans', 'Buy Plans', '/buy-plans'),
            ('prospecting', 'Prospecting', '/prospecting'),
        ] %}
        {% for id, label, href in nav_items %}
        <li>
            <a href="{{ href }}" hx-boost="true" hx-target="#main-content" hx-push-url="true"
               :class="{ 'active': $store.sidebar.active === '{{ id }}' }"
               @click="$store.sidebar.active = '{{ id }}'">
                {{ label }}
            </a>
        </li>
        {% endfor %}
    </ul>
</nav>
```

- [ ] **Step 2: Create `app/templates/partials/shared/topbar.html`**

Top bar with:
- Breadcrumb (dynamic based on current view)
- Global search input with `hx-get="/search" hx-trigger="keyup changed delay:300ms" hx-target="#search-results"`
- User menu dropdown (Alpine `x-data="{ open: false }"`)
- Notification bell

- [ ] **Step 3: Create `app/templates/partials/shared/mobile_nav.html`**

Bottom navigation bar for mobile viewports. Alpine `x-data` for active tab:

```html
<nav x-data class="mobile-nav" :class="{ 'hidden': window.innerWidth > 768 }">
    <a href="/requisitions" hx-boost="true" hx-target="#main-content"
       :class="{ 'active': $store.sidebar.active === 'requisitions' }">Reqs</a>
    <a href="/companies" hx-boost="true" hx-target="#main-content"
       :class="{ 'active': $store.sidebar.active === 'companies' }">Companies</a>
    <a href="/vendors" hx-boost="true" hx-target="#main-content"
       :class="{ 'active': $store.sidebar.active === 'vendors' }">Vendors</a>
    <a href="/quotes" hx-boost="true" hx-target="#main-content"
       :class="{ 'active': $store.sidebar.active === 'quotes' }">Quotes</a>
</nav>
```

- [ ] **Step 4: Update `app/templates/base.html` — include sidebar, topbar, mobile nav**

Wire the three partials into the base template layout:

```html
<body x-data>
    {% include "partials/shared/topbar.html" %}
    <div class="app-layout">
        {% include "partials/shared/sidebar.html" %}
        <main id="main-content" class="main-content">
            {% block content %}{% endblock %}
        </main>
    </div>
    {% include "partials/shared/mobile_nav.html" %}
    {% include "partials/shared/toast.html" %}
    {% include "partials/shared/modal.html" %}
</body>
```

- [ ] **Step 5: Add global search endpoint to `app/routers/views.py`**

```python
@router.get("/search")
async def global_search(request: Request, q: str = "", user=Depends(require_user)):
    """Return search results partial for top bar search."""
    results = []  # TODO: aggregate search across requisitions, companies, vendors
    return templates.TemplateResponse("partials/shared/search_results.html",
        {"request": request, "results": results, "query": q})
```

- [ ] **Step 6: Create `app/templates/partials/shared/search_results.html`**

Dropdown partial showing search results grouped by type (requisitions, companies, vendors).

- [ ] **Step 7: Write tests**

File: `tests/test_htmx_shared_components.py`

Tests:
- `test_sidebar_renders` — sidebar partial contains all nav items
- `test_topbar_renders` — topbar partial contains search input with hx-get
- `test_mobile_nav_renders` — mobile nav partial renders for small viewports
- `test_global_search_endpoint` — GET `/search?q=test` returns HTML partial

- [ ] **Step 8: Commit**

```bash
git add app/templates/partials/shared/ app/templates/base.html \
    app/routers/views.py tests/test_htmx_shared_components.py
git commit -m "phase3-task2: shared components — sidebar nav, topbar, mobile nav with HTMX boost"
```

---

## Task 3: Requisitions List + Create Modal

**Goal:** Migrate the requisitions list view and create-requisition modal from vanilla JS to HTMX + Alpine.

**Files to create:**
- `app/templates/partials/requisitions/list.html`
- `app/templates/partials/requisitions/create_modal.html`
- `app/templates/partials/requisitions/req_row.html`

**Files to modify:**
- `app/routers/views.py` — add requisitions list + create endpoints returning HTML

**Current JS functions being replaced:**
- `loadRequisitions()`, `renderReqList()`, `_renderReqRow()`, `sortReqList()`, `setToolbarQuickFilter()`
- Create modal inline in `index.html`

- [ ] **Step 1: Create `app/templates/partials/requisitions/list.html`**

Requisition list with:
- Search input: `hx-get="/views/requisitions/rows" hx-trigger="keyup changed delay:300ms" hx-target="#req-table-body"`
- Quick filters (All, Open, Awarded, Archived): `hx-get="/views/requisitions/rows?status=open" hx-target="#req-table-body"`
- Sort headers: `hx-get="/views/requisitions/rows?sort=created_at&dir=desc" hx-target="#req-table-body"`
- Table body with `id="req-table-body"` as HTMX swap target
- "New Requisition" button: `@click="$dispatch('open-modal')" hx-get="/views/requisitions/create-form" hx-target="#modal-content"`
- Pagination via `{% include "partials/shared/pagination.html" %}`

- [ ] **Step 2: Create `app/templates/partials/requisitions/req_row.html`**

Single table row for a requisition. Receives `req` template variable:

```html
<tr id="req-{{ req.id }}" hx-get="/views/requisitions/{{ req.id }}" hx-push-url="true" hx-target="#main-content" class="clickable-row">
    <td>{{ req.name }}</td>
    <td>{{ req.customer_name }}</td>
    <td>{{ req.status }}</td>
    <td>{{ req.requirement_count }} parts</td>
    <td>{{ req.created_at.strftime('%b %d') }}</td>
</tr>
```

- [ ] **Step 3: Create `app/templates/partials/requisitions/create_modal.html`**

Alpine-powered form inside modal:

```html
<form hx-post="/api/requisitions" hx-target="#req-table-body" hx-swap="afterbegin"
      @htmx:after-request="$dispatch('close-modal'); $store.toast.message = 'Requisition created'; $store.toast.type = 'success'; $store.toast.show = true"
      x-data="{ name: '', customer: '' }">
    <h3>New Requisition</h3>
    <input type="text" name="name" x-model="name" placeholder="Requisition name" required>
    <input type="text" name="customer_name" x-model="customer" placeholder="Customer">
    <button type="submit" :disabled="!name">Create</button>
    <button type="button" @click="$dispatch('close-modal')">Cancel</button>
</form>
```

- [ ] **Step 4: Add HTML endpoints to `app/routers/views.py`**

```python
@router.get("/views/requisitions")
async def requisitions_page(request: Request, user=Depends(require_user)):
    """Full requisitions list page."""

@router.get("/views/requisitions/rows")
async def requisitions_rows(request: Request, q: str = "", status: str = "",
                             sort: str = "created_at", dir: str = "desc",
                             page: int = 1, user=Depends(require_user)):
    """Return requisition table rows partial (HTMX swap target)."""

@router.get("/views/requisitions/create-form")
async def requisitions_create_form(request: Request, user=Depends(require_user)):
    """Return the create-requisition modal form partial."""
```

- [ ] **Step 5: Add HTML response to existing `POST /api/requisitions` endpoint**

In `app/routers/requisitions.py` (or in the views router), detect `HX-Request` header and return the new row partial instead of JSON:

```python
if wants_html(request):
    return templates.TemplateResponse("partials/requisitions/req_row.html", {"request": request, "req": new_req})
```

- [ ] **Step 6: Write tests**

File: `tests/test_htmx_requisitions_list.py`

Tests:
- `test_requisitions_list_page` — GET `/views/requisitions` returns full page HTML
- `test_requisitions_rows_partial` — GET `/views/requisitions/rows` with `HX-Request: true` returns table rows only
- `test_requisitions_search` — GET `/views/requisitions/rows?q=test` filters results
- `test_requisitions_pagination` — page param returns correct page
- `test_requisitions_sort` — sort param orders results correctly
- `test_create_form_partial` — GET `/views/requisitions/create-form` returns modal form
- `test_create_returns_row_html` — POST with `HX-Request: true` returns new row partial

- [ ] **Step 7: Commit**

```bash
git add app/templates/partials/requisitions/ app/routers/views.py \
    tests/test_htmx_requisitions_list.py
git commit -m "phase3-task3: requisitions list + create modal — HTMX paginated table with search and Alpine form"
```

---

## Task 4: Requisition Detail + Drill-Down

**Goal:** Migrate the requisition detail view (tabbed layout with parts, offers, quotes, buy plans, activity, tasks) from vanilla JS to HTMX + Alpine.

**Files to create:**
- `app/templates/partials/requisitions/detail.html`
- `app/templates/partials/requisitions/tabs/parts.html`
- `app/templates/partials/requisitions/tabs/offers.html`
- `app/templates/partials/requisitions/tabs/quotes.html`
- `app/templates/partials/requisitions/tabs/buy_plans.html`
- `app/templates/partials/requisitions/tabs/activity.html`
- `app/templates/partials/requisitions/tabs/tasks.html`
- `app/templates/partials/requisitions/requirement_row.html`

**Files to modify:**
- `app/routers/views.py` — add detail + tab endpoints

**Current JS functions being replaced:**
- `toggleDrillDown()`, `_renderSourcingDrillDown()`, `_renderDrillDownTable()`
- `_switchDdTab()`, `_renderDdTab()`, `_renderDdDetails()`, `_renderDdOffers()`, `_renderDdQuotes()`
- `_renderDdActivity()`, `_renderDdTasks()`, `_renderDdQA()`
- `addDrillRow()`, `deleteDrillRow()`, `editDrillCell()`

- [ ] **Step 1: Create `app/templates/partials/requisitions/detail.html`**

Requisition detail layout:
- Header with name (inline editable via `hx-put`), customer, status badge, deadline
- Tab bar (Alpine `x-data="{ tab: 'parts' }"`):
  - Each tab uses `hx-get="/views/requisitions/{{ req.id }}/tab/parts" hx-target="#tab-content"` on click
  - Active tab styling via `:class="{ 'active': tab === 'parts' }"`
- Tab content area: `<div id="tab-content">` — HTMX swap target
- Action buttons: Archive, Clone, Send RFQ

- [ ] **Step 2: Create `app/templates/partials/requisitions/tabs/parts.html`**

Parts (requirements) tab:
- Table of requirement rows (each row is its own swap target)
- "Add Part" row: `hx-post="/api/requisitions/{{ req_id }}/requirements" hx-target="#parts-table tbody" hx-swap="beforeend"`
- Inline editing: `hx-put="/api/requirements/{{ row.id }}" hx-target="#req-row-{{ row.id }}" hx-swap="outerHTML"`
- Delete: `hx-delete="/api/requirements/{{ row.id }}" hx-target="#req-row-{{ row.id }}" hx-swap="outerHTML" hx-confirm="Delete this part?"`
- Source button per row: `hx-get="/views/sourcing/{{ row.id }}/results" hx-target="#sourcing-panel"`

- [ ] **Step 3: Create `app/templates/partials/requisitions/requirement_row.html`**

Single requirement row partial — returned after inline edit or create:

```html
<tr id="req-row-{{ row.id }}">
    <td>{{ row.line_num }}</td>
    <td hx-get="/views/requisitions/{{ req_id }}/requirements/{{ row.id }}/edit" hx-trigger="dblclick" hx-target="this" hx-swap="innerHTML">{{ row.mpn }}</td>
    <td>{{ row.qty }}</td>
    <td>{{ row.target_price or '-' }}</td>
    <td><button hx-get="/views/sourcing/{{ row.id }}/results" hx-target="#sourcing-panel">Source</button></td>
    <td><button hx-delete="/api/requirements/{{ row.id }}" hx-target="#req-row-{{ row.id }}" hx-swap="delete" hx-confirm="Delete?">X</button></td>
</tr>
```

- [ ] **Step 4: Create remaining tab partials (offers, quotes, buy_plans, activity, tasks)**

Each tab partial:
- `tabs/offers.html` — list of offers for this requisition, with accept/reject actions via `hx-post`
- `tabs/quotes.html` — list of quotes, click to open quote detail
- `tabs/buy_plans.html` — list of buy plans, status badges
- `tabs/activity.html` — activity timeline loaded via `hx-get`, with "Load more" pagination
- `tabs/tasks.html` — task list with inline add/complete/delete

- [ ] **Step 5: Add detail + tab endpoints to `app/routers/views.py`**

```python
@router.get("/views/requisitions/{req_id}")
async def requisition_detail(req_id: int, request: Request, user=Depends(require_user)):
    """Full requisition detail page or partial."""

@router.get("/views/requisitions/{req_id}/tab/{tab_name}")
async def requisition_tab(req_id: int, tab_name: str, request: Request, user=Depends(require_user)):
    """Return a specific tab content partial."""
    valid_tabs = {"parts", "offers", "quotes", "buy_plans", "activity", "tasks"}
    if tab_name not in valid_tabs:
        raise HTTPException(404)
    return templates.TemplateResponse(f"partials/requisitions/tabs/{tab_name}.html", {...})
```

- [ ] **Step 6: Write tests**

File: `tests/test_htmx_requisition_detail.py`

Tests:
- `test_detail_page_renders` — GET `/views/requisitions/1` returns detail HTML
- `test_tab_parts` — GET `/views/requisitions/1/tab/parts` returns parts table
- `test_tab_offers` — returns offers content
- `test_tab_activity` — returns activity timeline
- `test_invalid_tab_404` — GET `/views/requisitions/1/tab/bogus` returns 404
- `test_requirement_row_partial` — inline edit returns updated row HTML
- `test_add_requirement` — POST returns new row partial

- [ ] **Step 7: Commit**

```bash
git add app/templates/partials/requisitions/ app/routers/views.py \
    tests/test_htmx_requisition_detail.py
git commit -m "phase3-task4: requisition detail — tabbed view with lazy-loaded tabs and inline requirement editing"
```

---

## Task 5: Sourcing Results

**Goal:** Migrate the sourcing results display (unified results list, live search progress, material cards) from vanilla JS to HTMX with SSE streaming.

**Files to create:**
- `app/templates/partials/sourcing/results.html`
- `app/templates/partials/sourcing/result_row.html`
- `app/templates/partials/sourcing/material_card.html`
- `app/templates/partials/sourcing/sighting_row.html`
- `app/templates/partials/sourcing/search_progress.html`

**Files to modify:**
- `app/routers/views.py` — add sourcing result endpoints
- `app/routers/sources.py` — add SSE endpoint for search progress

**Current JS functions being replaced:**
- `inlineSourceAll()`, `_renderSourcingDrillDown()`, `_renderDrillDownTable()`
- `ddResearchPart()`, `ddResearchAll()`, `toggleSighting()`, `ddToggleSighting()`
- `ddToggleHistory()`, `ddToggleHistorySightings()`, `openMaterialPopup()`

- [ ] **Step 1: Create `app/templates/partials/sourcing/results.html`**

Unified sourcing results panel:
- Source filter pills (Alpine `x-data="{ filter: 'all' }"`) — All, Live Stock, Historical, Vendor Affinity
- Sort dropdown
- Results container `id="sourcing-results"` — HTMX swap target
- SSE connection for live search progress: `hx-ext="sse" sse-connect="/views/sourcing/{{ req_row_id }}/stream" sse-swap="results"`
- Each result rendered via `result_row.html` partial

- [ ] **Step 2: Create `app/templates/partials/sourcing/result_row.html`**

Single sourcing result with:
- Source badge (BrokerBin, Nexar, DigiKey, etc.)
- Confidence score ring
- Vendor name, MPN, qty, price, date age
- Action buttons: Add to Quote (`hx-post`), Log Offer (`hx-get` opens modal), RFQ (`hx-post`)

- [ ] **Step 3: Create `app/templates/partials/sourcing/material_card.html`**

Material card popup/detail:
- MPN, manufacturer, description, tags
- Sightings history (paginated via `hx-get`)
- Loaded via: `hx-get="/views/materials/{{ material_id }}" hx-target="#modal-content"`

- [ ] **Step 4: Create `app/templates/partials/sourcing/sighting_row.html`**

Single sighting row with vendor, price, qty, date, source.

- [ ] **Step 5: Create `app/templates/partials/sourcing/search_progress.html`**

Progress indicator showing which sources have completed:

```html
<div id="search-progress">
    {% for source in sources %}
    <span class="source-pill {{ 'done' if source.done else 'pending' }}">
        {{ source.name }} {% if source.done %}({{ source.count }}){% else %}...{% endif %}
    </span>
    {% endfor %}
</div>
```

- [ ] **Step 6: Add SSE streaming endpoint for search progress**

In `app/routers/views.py` or `app/routers/sources.py`:

```python
@router.get("/views/sourcing/{req_row_id}/stream")
async def sourcing_stream(req_row_id: int, request: Request, user=Depends(require_user)):
    """SSE stream for live sourcing progress. Sends HTML partials as sources complete."""
    async def event_generator():
        # Fire search, yield partial HTML as each source completes
        yield {"event": "progress", "data": render_template("partials/sourcing/search_progress.html", ...)}
        yield {"event": "results", "data": render_template("partials/sourcing/results.html", ...)}
    return EventSourceResponse(event_generator())
```

- [ ] **Step 7: Add sourcing result endpoints to `app/routers/views.py`**

```python
@router.get("/views/sourcing/{req_row_id}/results")
async def sourcing_results(req_row_id: int, request: Request, filter: str = "all", user=Depends(require_user)):
    """Return sourcing results partial for a requirement row."""

@router.get("/views/materials/{material_id}")
async def material_card_detail(material_id: int, request: Request, user=Depends(require_user)):
    """Return material card detail partial."""
```

- [ ] **Step 8: Write tests**

File: `tests/test_htmx_sourcing.py`

Tests:
- `test_sourcing_results_partial` — GET returns results HTML
- `test_sourcing_filter` — filter param narrows results
- `test_material_card_partial` — material detail renders
- `test_sighting_row_render` — sighting row partial renders correctly
- `test_sse_stream_returns_events` — SSE endpoint yields progress events

- [ ] **Step 9: Commit**

```bash
git add app/templates/partials/sourcing/ app/routers/views.py \
    tests/test_htmx_sourcing.py
git commit -m "phase3-task5: sourcing results — unified results list with SSE streaming progress"
```

---

## Task 6: Companies List + Detail Drawer

**Goal:** Migrate the companies list and detail drawer from crm.js to HTMX + Alpine.

**Files to create:**
- `app/templates/partials/companies/list.html`
- `app/templates/partials/companies/detail.html`
- `app/templates/partials/companies/company_row.html`
- `app/templates/partials/companies/tabs/overview.html`
- `app/templates/partials/companies/tabs/sites.html`
- `app/templates/partials/companies/tabs/activity.html`
- `app/templates/partials/companies/tabs/contacts.html`
- `app/templates/partials/companies/tabs/pipeline.html`
- `app/templates/partials/companies/site_row.html`

**Files to modify:**
- `app/routers/views.py` — add company view endpoints

**Current JS functions being replaced (from crm.js):**
- `loadCustomers()`, `sortCustList()`, `selectCustomer()`, `renderCustomerDetail()`
- `toggleSiteDetail()`, `openAddSiteModal()`, `openEditSiteModal()`, `openEditCompany()`
- `quickCreateCompany()`, `unifiedEnrichCompany()`

- [ ] **Step 1: Create `app/templates/partials/companies/list.html`**

Company list with:
- Search: `hx-get="/views/companies/rows" hx-trigger="keyup changed delay:300ms" hx-target="#company-table-body"`
- Owner filter dropdown (Alpine): `x-data="{ owner: '' }"` with `hx-get="/views/companies/rows?owner=..." hx-target="#company-table-body"`
- Paginated table (server-side pagination via `{items, total, limit, offset}` response format)
- "New Company" button opens modal
- Bulk select via Alpine `x-data="{ selected: [] }"`
- Each row clickable — opens detail drawer

- [ ] **Step 2: Create `app/templates/partials/companies/company_row.html`**

```html
<tr id="company-{{ company.id }}" class="clickable-row"
    hx-get="/views/companies/{{ company.id }}" hx-target="#detail-drawer" hx-swap="innerHTML">
    <td><input type="checkbox" x-model="selected" :value="{{ company.id }}"></td>
    <td>{{ company.name }}</td>
    <td>{{ company.owner or '-' }}</td>
    <td>{{ company.site_count }}</td>
    <td>{{ company.open_req_count }}</td>
</tr>
```

- [ ] **Step 3: Create `app/templates/partials/companies/detail.html`**

Company detail drawer (slides in from right, Alpine transition):
- Header: company name (inline editable), owner, tags
- Enrich button: `hx-post="/api/enrich/company/{{ company.id }}" hx-target="#enrich-results" hx-indicator="#enrich-spinner"`
- Tab bar (Alpine `x-data="{ tab: 'overview' }"`):
  - Overview, Sites, Activity, Contacts, Pipeline
  - Each tab: `hx-get="/views/companies/{{ company.id }}/tab/{{ tab_name }}" hx-target="#company-tab-content"`
- Close button: `@click="$dispatch('close-drawer')"`

- [ ] **Step 4: Create company tab partials**

- `tabs/overview.html` — company details, notes (editable via `hx-put`), enrichment results
- `tabs/sites.html` — list of customer sites, each expandable, add/edit site modals
- `tabs/activity.html` — activity timeline, paginated
- `tabs/contacts.html` — site contacts list with inline status editing
- `tabs/pipeline.html` — open requisitions and quotes for this company

- [ ] **Step 5: Create `app/templates/partials/companies/site_row.html`**

Customer site row with expand/collapse for site details and contacts.

- [ ] **Step 6: Add company view endpoints to `app/routers/views.py`**

```python
@router.get("/views/companies")
async def companies_page(request: Request, user=Depends(require_user)):
    """Companies list page."""

@router.get("/views/companies/rows")
async def companies_rows(request: Request, q: str = "", owner: str = "",
                          page: int = 1, limit: int = 100, user=Depends(require_user)):
    """Company table rows partial with server-side filtering and pagination."""

@router.get("/views/companies/{company_id}")
async def company_detail(company_id: int, request: Request, user=Depends(require_user)):
    """Company detail drawer partial."""

@router.get("/views/companies/{company_id}/tab/{tab_name}")
async def company_tab(company_id: int, tab_name: str, request: Request, user=Depends(require_user)):
    """Company tab content partial."""
```

- [ ] **Step 7: Write tests**

File: `tests/test_htmx_companies.py`

Tests:
- `test_companies_list_page` — full page renders
- `test_companies_rows_partial` — rows returned for HTMX request
- `test_companies_search_filter` — search narrows results
- `test_companies_owner_filter` — owner filter works
- `test_companies_pagination` — page/limit params work
- `test_company_detail_drawer` — detail partial renders with tabs
- `test_company_tab_overview` — overview tab content renders
- `test_company_tab_sites` — sites tab renders site list
- `test_company_tab_pipeline` — pipeline tab shows open reqs

- [ ] **Step 8: Commit**

```bash
git add app/templates/partials/companies/ app/routers/views.py \
    tests/test_htmx_companies.py
git commit -m "phase3-task6: companies list + detail drawer — HTMX paginated list with owner filter and tabbed drawer"
```

---

## Task 7: Vendor Cards + Contacts

**Goal:** Migrate vendor list, vendor detail with tabs (overview, contacts, analytics, offers), and contact management from vanilla JS to HTMX + Alpine.

**Files to create:**
- `app/templates/partials/vendors/list.html`
- `app/templates/partials/vendors/vendor_row.html`
- `app/templates/partials/vendors/detail.html`
- `app/templates/partials/vendors/tabs/overview.html`
- `app/templates/partials/vendors/tabs/contacts.html`
- `app/templates/partials/vendors/tabs/analytics.html`
- `app/templates/partials/vendors/tabs/offers.html`
- `app/templates/partials/vendors/contact_row.html`

**Files to modify:**
- `app/routers/views.py` — add vendor view endpoints

**Current JS functions being replaced (from app.js):**
- `sortVendorList()`, `openVendorDrawer()`, `closeVendorDrawer()`, `switchVendorDrawerTab()`
- `openAddVendorContact()`, `openEditVendorContact()`, `deleteVendorContact()`
- `openContactDrawer()`, `closeContactDrawer()`, `sortContactList()`
- `placeVendorCall()`, `logCall()`, `unifiedEnrichVendor()`
- `vpSetRating()`, `vpSubmitReview()`, `vpToggleBlacklist()`

- [ ] **Step 1: Create `app/templates/partials/vendors/list.html`**

Vendor list with:
- Search: `hx-get="/views/vendors/rows" hx-trigger="keyup changed delay:300ms" hx-target="#vendor-table-body"`
- Sort headers with `hx-get` + sort params
- Bulk select (Alpine `x-data="{ selected: [] }"`)
- Each row opens vendor detail drawer
- "New Vendor" button opens modal

- [ ] **Step 2: Create `app/templates/partials/vendors/vendor_row.html`**

Single vendor row — clickable to open drawer:

```html
<tr id="vendor-{{ vendor.id }}" class="clickable-row"
    hx-get="/views/vendors/{{ vendor.id }}" hx-target="#detail-drawer" hx-swap="innerHTML">
    <td>{{ vendor.name }}</td>
    <td>{{ vendor.health_score or '-' }}</td>
    <td>{{ vendor.contact_count }}</td>
    <td>{{ vendor.last_activity }}</td>
</tr>
```

- [ ] **Step 3: Create `app/templates/partials/vendors/detail.html`**

Vendor detail drawer:
- Header: vendor name, health score ring, blacklist toggle
- Enrich button: `hx-post="/api/enrich/vendor/{{ vendor.id }}" hx-target="#enrich-results" hx-indicator="#enrich-spinner"`
- Click-to-call button (if phone available)
- Tabs: Overview, Contacts, Analytics, Offers (lazy-loaded via `hx-get`)

- [ ] **Step 4: Create vendor tab partials**

- `tabs/overview.html` — vendor details, notes, rating (Alpine star widget), review form
- `tabs/contacts.html` — contact list with inline add/edit/delete, status badges, click-to-call
- `tabs/analytics.html` — vendor scorecard, response rate, price competitiveness
- `tabs/offers.html` — historical offers from this vendor, filterable

- [ ] **Step 5: Create `app/templates/partials/vendors/contact_row.html`**

Contact row with inline editing:

```html
<tr id="contact-{{ contact.id }}">
    <td>{{ contact.name }}</td>
    <td>{{ contact.email }}</td>
    <td>{{ contact.phone }}</td>
    <td>
        <select hx-put="/api/vendor-contacts/{{ contact.id }}" hx-target="#contact-{{ contact.id }}" hx-swap="outerHTML" name="status">
            <option value="active" {{ 'selected' if contact.status == 'active' }}>Active</option>
            <option value="inactive" {{ 'selected' if contact.status == 'inactive' }}>Inactive</option>
        </select>
    </td>
    <td>
        <button hx-get="/views/vendors/contacts/{{ contact.id }}/edit" hx-target="#modal-content" @click="$dispatch('open-modal')">Edit</button>
        <button hx-delete="/api/vendor-contacts/{{ contact.id }}" hx-target="#contact-{{ contact.id }}" hx-swap="delete" hx-confirm="Delete contact?">X</button>
    </td>
</tr>
```

- [ ] **Step 6: Add vendor view endpoints to `app/routers/views.py`**

```python
@router.get("/views/vendors")
async def vendors_page(request: Request, user=Depends(require_user)):
    """Vendors list page."""

@router.get("/views/vendors/rows")
async def vendors_rows(request: Request, q: str = "", sort: str = "name",
                        dir: str = "asc", page: int = 1, user=Depends(require_user)):
    """Vendor table rows partial."""

@router.get("/views/vendors/{vendor_id}")
async def vendor_detail(vendor_id: int, request: Request, user=Depends(require_user)):
    """Vendor detail drawer partial."""

@router.get("/views/vendors/{vendor_id}/tab/{tab_name}")
async def vendor_tab(vendor_id: int, tab_name: str, request: Request, user=Depends(require_user)):
    """Vendor tab content partial."""
```

- [ ] **Step 7: Write tests**

File: `tests/test_htmx_vendors.py`

Tests:
- `test_vendors_list_page` — full page renders
- `test_vendors_rows_partial` — rows partial returns vendor table
- `test_vendors_search` — search filter works
- `test_vendor_detail_drawer` — detail partial renders
- `test_vendor_tab_contacts` — contacts tab renders contact list
- `test_vendor_tab_analytics` — analytics tab renders scorecard
- `test_contact_row_inline_edit` — status change returns updated row
- `test_contact_delete` — delete removes row

- [ ] **Step 8: Commit**

```bash
git add app/templates/partials/vendors/ app/routers/views.py \
    tests/test_htmx_vendors.py
git commit -m "phase3-task7: vendor cards + contacts — HTMX list, drawer with tabs, inline contact editing"
```

---

## Task 8: Quotes + Offers

**Goal:** Migrate quotes list, quote detail (line items editor), and offer cards from crm.js to HTMX + Alpine.

**Files to create:**
- `app/templates/partials/quotes/list.html`
- `app/templates/partials/quotes/quote_row.html`
- `app/templates/partials/quotes/detail.html`
- `app/templates/partials/quotes/line_item_row.html`
- `app/templates/partials/quotes/offer_card.html`

**Files to modify:**
- `app/routers/views.py` — add quote/offer view endpoints

**Current JS functions being replaced (from crm.js):**
- `loadQuote()`, `loadSpecificQuote()`, `saveQuoteDraft()`, `sendQuoteEmail()`
- `updateQuoteLine()`, `updateQuoteLineField()`, `applyMarkup()`, `copyQuoteTable()`
- `markQuoteResult()`, `reopenQuote()`, `reviseQuote()`, `confirmSendQuote()`
- `openOfferGallery()`, `openEditOffer()`, `deleteOffer()`, `updateOffer()`
- `setOfferFilter()`, `setOfferSort()`, `toggleOfferSelect()`

- [ ] **Step 1: Create `app/templates/partials/quotes/list.html`**

Quote list with:
- Filter tabs: All, Draft, Sent, Won, Lost (Alpine `x-data="{ filter: 'all' }"`)
- Search input with HTMX debounced search
- Each quote row clickable to open detail
- Sort by date, customer, total

- [ ] **Step 2: Create `app/templates/partials/quotes/quote_row.html`**

```html
<tr id="quote-{{ quote.id }}" class="clickable-row"
    hx-get="/views/quotes/{{ quote.id }}" hx-target="#main-content" hx-push-url="true">
    <td>{{ quote.ref_number }}</td>
    <td>{{ quote.customer_name }}</td>
    <td>{{ quote.line_count }} lines</td>
    <td>${{ '%.2f' | format(quote.total or 0) }}</td>
    <td><span class="badge badge-{{ quote.status }}">{{ quote.status }}</span></td>
    <td>{{ quote.created_at.strftime('%b %d') }}</td>
</tr>
```

- [ ] **Step 3: Create `app/templates/partials/quotes/detail.html`**

Quote detail with:
- Header: ref number, customer, status, total, margin
- Action buttons: Send Quote, Mark Result, Revise, Copy Table
- Line items table (each row is an HTMX swap target)
- Global markup input: Alpine `x-data="{ markup: '' }"` with `hx-post` to apply to all lines
- Auto-save on field change: `hx-post="/api/quotes/{{ quote.id }}/lines/{{ line.id }}" hx-trigger="change" hx-swap="none"`

- [ ] **Step 4: Create `app/templates/partials/quotes/line_item_row.html`**

Single quote line item — inline editable:

```html
<tr id="line-{{ line.id }}">
    <td>{{ line.mpn }}</td>
    <td><input type="number" name="qty" value="{{ line.qty }}" hx-put="/api/quotes/{{ quote_id }}/lines/{{ line.id }}" hx-trigger="change" hx-target="#line-{{ line.id }}" hx-swap="outerHTML"></td>
    <td><input type="number" name="unit_price" value="{{ line.unit_price }}" step="0.01" hx-put="/api/quotes/{{ quote_id }}/lines/{{ line.id }}" hx-trigger="change" hx-target="#line-{{ line.id }}" hx-swap="outerHTML"></td>
    <td>{{ '%.2f' | format((line.qty or 0) * (line.unit_price or 0)) }}</td>
    <td><button hx-delete="/api/quotes/{{ quote_id }}/lines/{{ line.id }}" hx-target="#line-{{ line.id }}" hx-swap="delete">X</button></td>
</tr>
```

- [ ] **Step 5: Create `app/templates/partials/quotes/offer_card.html`**

Offer card showing vendor, price, qty, date, attachments. Alpine lightbox for attachments:

```html
<div class="offer-card" id="offer-{{ offer.id }}" x-data="{ expanded: false }">
    <div class="offer-header" @click="expanded = !expanded">
        <span>{{ offer.vendor_name }}</span>
        <span>${{ '%.4f' | format(offer.unit_price) }} x {{ offer.qty }}</span>
    </div>
    <div x-show="expanded" x-transition class="offer-body">
        <!-- Offer details, accept/reject buttons -->
        <button hx-post="/api/offers/{{ offer.id }}/accept" hx-target="#offer-{{ offer.id }}" hx-swap="outerHTML">Accept</button>
    </div>
</div>
```

- [ ] **Step 6: Add quote/offer view endpoints to `app/routers/views.py`**

```python
@router.get("/views/quotes")
async def quotes_page(request: Request, user=Depends(require_user)):
    """Quotes list page."""

@router.get("/views/quotes/rows")
async def quotes_rows(request: Request, q: str = "", status: str = "",
                       sort: str = "created_at", page: int = 1, user=Depends(require_user)):
    """Quote table rows partial."""

@router.get("/views/quotes/{quote_id}")
async def quote_detail(quote_id: int, request: Request, user=Depends(require_user)):
    """Quote detail page."""
```

- [ ] **Step 7: Write tests**

File: `tests/test_htmx_quotes.py`

Tests:
- `test_quotes_list_page` — full page renders
- `test_quotes_rows_partial` — rows partial with filter
- `test_quote_detail` — detail page renders with line items
- `test_line_item_inline_edit` — PUT returns updated row
- `test_offer_card_render` — offer card partial renders
- `test_quote_status_filter` — status filter narrows results

- [ ] **Step 8: Commit**

```bash
git add app/templates/partials/quotes/ app/routers/views.py \
    tests/test_htmx_quotes.py
git commit -m "phase3-task8: quotes + offers — HTMX quote editor with inline line items and offer cards"
```

---

## Task 9: Buy Plans (Unified v1 + v3)

**Goal:** Migrate buy plans list and detail workflow from crm.js to HTMX + Alpine. By this point, Phase 2 should have merged v1 and v3 into a single buy plan system.

**Files to create:**
- `app/templates/partials/buy_plans/list.html`
- `app/templates/partials/buy_plans/buy_plan_row.html`
- `app/templates/partials/buy_plans/detail.html`
- `app/templates/partials/buy_plans/line_row.html`
- `app/templates/partials/buy_plans/approval_modal.html`

**Files to modify:**
- `app/routers/views.py` — add buy plan view endpoints

**Current JS functions being replaced (from crm.js):**
- `loadBuyPlans()`, `loadBuyPlanV3()`, `renderBuyPlansList()`, `sortBpList()`
- `submitBuyPlan()`, `submitBuyPlanV3()`, `approveBuyPlan()`, `approveBuyPlanV3()`
- `rejectBuyPlan()`, `rejectBuyPlanV3()`, `cancelBuyPlan()`, `completeBuyPlan()`
- `saveBuyPlanPOs()`, `verifyBuyPlanPOs()`, `confirmPOV3()`, `verifyPOV3()`, `verifySOV3()`

- [ ] **Step 1: Create `app/templates/partials/buy_plans/list.html`**

Buy plans list with:
- Status filter tabs: All, Pending, Approved, Rejected, Completed (Alpine)
- "My Only" toggle: `hx-get="/views/buy-plans/rows?mine=true" hx-target="#bp-table-body"`
- Sort by date, customer, total
- Each row clickable to open detail

- [ ] **Step 2: Create `app/templates/partials/buy_plans/buy_plan_row.html`**

```html
<tr id="bp-{{ bp.id }}" class="clickable-row"
    hx-get="/views/buy-plans/{{ bp.id }}" hx-target="#main-content" hx-push-url="true">
    <td>{{ bp.name }}</td>
    <td>{{ bp.customer_name }}</td>
    <td>{{ bp.line_count }} lines</td>
    <td>${{ '%.2f' | format(bp.total or 0) }}</td>
    <td><span class="badge badge-{{ bp.status }}">{{ bp.status }}</span></td>
    <td>{{ bp.created_at.strftime('%b %d') }}</td>
</tr>
```

- [ ] **Step 3: Create `app/templates/partials/buy_plans/detail.html`**

Buy plan detail with workflow:
- Header: name, customer, status badge, total
- Workflow action bar (context-sensitive based on status):
  - Pending: Approve / Reject buttons
  - Approved: Confirm PO / Halt SO
  - Confirmed: Verify PO / Verify SO
- Line items table with per-line PO confirmation
- Approval/rejection confirmation modals (Alpine)
- Action buttons use `hx-post` with `hx-target="#bp-{{ bp.id }}"` to refresh the detail

- [ ] **Step 4: Create `app/templates/partials/buy_plans/line_row.html`**

Buy plan line row with PO number field and confirmation checkbox:

```html
<tr id="bp-line-{{ line.id }}">
    <td>{{ line.mpn }}</td>
    <td>{{ line.vendor_name }}</td>
    <td>{{ line.qty }}</td>
    <td>${{ '%.4f' | format(line.unit_price) }}</td>
    <td><input type="text" name="po_number" value="{{ line.po_number or '' }}"
               hx-put="/api/buy-plans/{{ bp_id }}/lines/{{ line.id }}" hx-trigger="change"
               hx-target="#bp-line-{{ line.id }}" hx-swap="outerHTML"></td>
    <td><input type="checkbox" name="po_confirmed" {{ 'checked' if line.po_confirmed }}
               hx-put="/api/buy-plans/{{ bp_id }}/lines/{{ line.id }}" hx-trigger="change"
               hx-target="#bp-line-{{ line.id }}" hx-swap="outerHTML"></td>
</tr>
```

- [ ] **Step 5: Create `app/templates/partials/buy_plans/approval_modal.html`**

Alpine confirmation modal for approve/reject/halt actions:

```html
<div x-data="{ reason: '' }">
    <h3>{{ action_label }} Buy Plan</h3>
    <textarea x-model="reason" placeholder="Reason (optional)"></textarea>
    <button hx-post="/api/buy-plans/{{ bp_id }}/{{ action }}" hx-target="#main-content"
            hx-vals='js:{"reason": reason}'
            @htmx:after-request="$dispatch('close-modal')">
        {{ action_label }}
    </button>
    <button @click="$dispatch('close-modal')">Cancel</button>
</div>
```

- [ ] **Step 6: Add buy plan view endpoints to `app/routers/views.py`**

```python
@router.get("/views/buy-plans")
async def buy_plans_page(request: Request, user=Depends(require_user)):
    """Buy plans list page."""

@router.get("/views/buy-plans/rows")
async def buy_plans_rows(request: Request, q: str = "", status: str = "",
                          mine: bool = False, page: int = 1, user=Depends(require_user)):
    """Buy plan table rows partial."""

@router.get("/views/buy-plans/{bp_id}")
async def buy_plan_detail(bp_id: int, request: Request, user=Depends(require_user)):
    """Buy plan detail page with workflow actions."""
```

- [ ] **Step 7: Write tests**

File: `tests/test_htmx_buy_plans.py`

Tests:
- `test_buy_plans_list_page` — full page renders
- `test_buy_plans_rows_partial` — rows with status filter
- `test_buy_plans_my_only` — mine filter works
- `test_buy_plan_detail` — detail renders with action buttons
- `test_buy_plan_approve_action` — approve returns updated detail
- `test_buy_plan_reject_action` — reject returns updated detail
- `test_line_po_confirm` — PO confirmation updates line

- [ ] **Step 8: Commit**

```bash
git add app/templates/partials/buy_plans/ app/routers/views.py \
    tests/test_htmx_buy_plans.py
git commit -m "phase3-task9: buy plans — HTMX workflow view with approval actions and per-line PO confirmation"
```

---

## Task 10: Prospecting + Enrichment

**Goal:** Migrate the prospect pool (filterable list), prospect detail, and enrichment button from crm.js to HTMX + Alpine.

**Files to create:**
- `app/templates/partials/prospecting/pool.html`
- `app/templates/partials/prospecting/prospect_row.html`
- `app/templates/partials/prospecting/detail.html`
- `app/templates/partials/shared/enrich_button.html`

**Files to modify:**
- `app/routers/views.py` — add prospecting view endpoints

**Current JS functions being replaced (from crm.js):**
- `switchProactiveTab()`, `convertProactiveOffer()`, `dismissProactiveGroup()`
- `openSuggestedContacts()`, `searchSuggestedContacts()`, `addSelectedSuggestedContacts()`
- `unifiedEnrichCompany()`, `unifiedEnrichVendor()`

- [ ] **Step 1: Create `app/templates/partials/prospecting/pool.html`**

Prospect pool with:
- Filter dropdowns (Alpine `x-data`): Industry, Revenue Range, Region, Source
- Search input: `hx-get="/views/prospecting/rows" hx-trigger="keyup changed delay:300ms" hx-target="#prospect-table-body"`
- Prospect cards or table rows
- Claim/Unclaim buttons per prospect
- "Enrich" button per prospect using shared enrich partial

- [ ] **Step 2: Create `app/templates/partials/prospecting/prospect_row.html`**

Prospect row/card:

```html
<div id="prospect-{{ prospect.id }}" class="prospect-card">
    <h4>{{ prospect.company_name }}</h4>
    <p>{{ prospect.industry }} | {{ prospect.region }}</p>
    <p>Score: {{ prospect.score }}/100</p>
    <div class="prospect-actions">
        <button hx-post="/api/prospects/{{ prospect.id }}/claim" hx-target="#prospect-{{ prospect.id }}" hx-swap="outerHTML">Claim</button>
        {% include "partials/shared/enrich_button.html" with context %}
    </div>
</div>
```

- [ ] **Step 3: Create `app/templates/partials/prospecting/detail.html`**

Prospect detail:
- Company info, signals, score breakdown
- Contact suggestions (HTMX-loaded list)
- Enrichment results panel
- Activity log
- Schedule outreach button

- [ ] **Step 4: Create `app/templates/partials/shared/enrich_button.html`**

Reusable enrichment button used across companies, vendors, and prospects:

```html
<div x-data="{ enriching: false, results: null }">
    <button hx-post="/api/enrich/{{ entity_type }}/{{ entity_id }}"
            hx-target="#enrich-results-{{ entity_id }}" hx-swap="innerHTML"
            hx-indicator="#enrich-spinner-{{ entity_id }}"
            @htmx:before-request="enriching = true"
            @htmx:after-request="enriching = false"
            :disabled="enriching">
        <span x-show="!enriching">Enrich</span>
        <span x-show="enriching" id="enrich-spinner-{{ entity_id }}">Enriching...</span>
    </button>
    <div id="enrich-results-{{ entity_id }}"></div>
</div>
```

- [ ] **Step 5: Add prospecting view endpoints to `app/routers/views.py`**

```python
@router.get("/views/prospecting")
async def prospecting_page(request: Request, user=Depends(require_user)):
    """Prospect pool page."""

@router.get("/views/prospecting/rows")
async def prospecting_rows(request: Request, q: str = "", industry: str = "",
                            revenue: str = "", region: str = "", source: str = "",
                            page: int = 1, user=Depends(require_user)):
    """Prospect rows partial with filters."""

@router.get("/views/prospecting/{prospect_id}")
async def prospect_detail(prospect_id: int, request: Request, user=Depends(require_user)):
    """Prospect detail partial."""
```

- [ ] **Step 6: Write tests**

File: `tests/test_htmx_prospecting.py`

Tests:
- `test_prospecting_pool_page` — full page renders
- `test_prospecting_rows_partial` — rows with filter params
- `test_prospecting_industry_filter` — industry filter narrows results
- `test_prospect_detail` — detail partial renders
- `test_enrich_button_renders` — enrich button partial includes correct hx-post URL
- `test_claim_prospect` — claim action returns updated prospect card

- [ ] **Step 7: Commit**

```bash
git add app/templates/partials/prospecting/ app/templates/partials/shared/enrich_button.html \
    app/routers/views.py tests/test_htmx_prospecting.py
git commit -m "phase3-task10: prospecting + enrichment — HTMX filterable pool with reusable enrich button"
```

---

## Task 11: Mobile Optimization

**Goal:** Ensure all HTMX views are fully responsive and touch-friendly on mobile viewports.

**Files to create:**
- `app/static/htmx_mobile.css` — mobile-specific styles for HTMX views

**Files to modify:**
- `app/templates/base.html` — add mobile meta tags, viewport handling
- `app/templates/partials/shared/mobile_nav.html` — finalize mobile bottom nav
- All list partials — add responsive table behavior (card layout on small screens)

- [ ] **Step 1: Create `app/static/htmx_mobile.css`**

Mobile-specific styles:
- Card-based layout for tables below 768px (each row becomes a card)
- Touch-friendly tap targets (min 44px height)
- Bottom nav fixed positioning
- Drawer slides from bottom on mobile (not right)
- Modal full-screen on mobile
- Swipe gestures disabled (HTMX handles navigation)

- [ ] **Step 2: Update all list partials with responsive classes**

Add `class="responsive-table"` to all tables. CSS handles transformation to card layout on small screens:

```css
@media (max-width: 768px) {
    .responsive-table thead { display: none; }
    .responsive-table tr { display: block; margin-bottom: 8px; border: 1px solid var(--border); border-radius: 8px; padding: 12px; }
    .responsive-table td { display: flex; justify-content: space-between; padding: 4px 0; }
    .responsive-table td::before { content: attr(data-label); font-weight: 600; }
}
```

- [ ] **Step 3: Update detail drawer for mobile**

On viewports < 768px, detail drawer should:
- Slide up from bottom (not right)
- Take full width
- Have a "swipe down to close" gesture (Alpine touch handler)
- Back button in header

- [ ] **Step 4: Update `app/templates/partials/shared/mobile_nav.html`**

Finalize mobile bottom nav with:
- 5 items max (Reqs, Companies, Vendors, Quotes, More)
- Active state tracking via Alpine store
- Badge counts for items needing attention

- [ ] **Step 5: Add mobile viewport entry to Vite config**

In `vite.config.js`, add `htmx_mobile` CSS entry:

```js
htmx_mobile: resolve(__dirname, "app/static/htmx_mobile.css"),
```

- [ ] **Step 6: Write tests**

File: `tests/test_htmx_mobile.py`

Tests:
- `test_mobile_nav_renders` — mobile nav partial contains correct links
- `test_responsive_table_classes` — all list partials include responsive-table class
- `test_mobile_meta_tags` — base template includes viewport meta tag
- `test_detail_drawer_mobile_class` — drawer has mobile-friendly class

- [ ] **Step 7: Commit**

```bash
git add app/static/htmx_mobile.css app/templates/ vite.config.js \
    tests/test_htmx_mobile.py
git commit -m "phase3-task11: mobile optimization — responsive tables, mobile nav, touch-friendly drawers"
```

---

## Task 12: Delete Old JS + Final Cleanup

**Goal:** Remove all legacy vanilla JS (app.js, crm.js, tickets.js, touch.js), delete the `USE_HTMX` feature flag, clean up Vite config, and run a full smoke test.

**Files to delete:**
- `app/static/app.js` (16,932 lines)
- `app/static/crm.js` (8,764 lines)
- `app/static/tickets.js`
- `app/static/touch.js`

**Files to modify:**
- `app/config.py` — remove `USE_HTMX` flag (HTMX is now the only frontend)
- `app/main.py` — remove conditional HTMX router inclusion (always include)
- `vite.config.js` — remove old JS entry points (app, crm, tickets, touch), remove `checkExportsPlugin`
- `app/templates/index.html` — either delete entirely or redirect to `/requisitions`
- `package.json` — remove unused devDependencies if any

- [ ] **Step 1: Verify all views work with HTMX — full smoke test checklist**

Before deleting anything, confirm every view works:

```
[ ] Requisitions list loads, search works, pagination works
[ ] Requisition create modal opens and submits
[ ] Requisition detail loads, all tabs work
[ ] Sourcing fires, results stream in, material cards open
[ ] Companies list loads, owner filter works, search works
[ ] Company detail drawer opens, all tabs work
[ ] Vendors list loads, search works
[ ] Vendor detail drawer opens, contacts tab works
[ ] Quotes list loads, filter tabs work
[ ] Quote detail loads, line items editable
[ ] Buy plans list loads, workflow actions work
[ ] Prospecting pool loads, filters work
[ ] Enrich button works on companies, vendors, prospects
[ ] Mobile nav works, responsive tables work
[ ] All modals open and close correctly
[ ] Toast notifications appear and auto-dismiss
```

- [ ] **Step 2: Delete old JS files**

```bash
rm -f app/static/app.js
rm -f app/static/crm.js
rm -f app/static/tickets.js
rm -f app/static/touch.js
```

- [ ] **Step 3: Remove `USE_HTMX` feature flag from `app/config.py`**

Delete the `use_htmx: bool = False` line from the Settings class.

- [ ] **Step 4: Update `app/main.py` — always include views router**

Remove the `if settings.use_htmx:` conditional. The views router is now always registered.

- [ ] **Step 5: Update `vite.config.js` — remove old entry points and checkExportsPlugin**

Remove from `rollupOptions.input`:
- `app` entry
- `crm` entry
- `tickets` entry
- `touch` entry

Remove the entire `checkExportsPlugin()` function (it validated app.js/crm.js exports — no longer needed).

Remove the `resolve.alias` for `app` (was `app: resolve(__dirname, "app/static/app.js")`).

Updated `rollupOptions.input` should be:

```js
input: {
    htmx_app: resolve(__dirname, "app/static/htmx_app.js"),
    styles: resolve(__dirname, "app/static/styles.css"),
    htmx_mobile: resolve(__dirname, "app/static/htmx_mobile.css"),
},
```

- [ ] **Step 6: Handle `app/templates/index.html`**

Replace the 1,578-line monolithic template with a simple redirect:

```html
<!DOCTYPE html>
<html><head><meta http-equiv="refresh" content="0;url=/views/requisitions"></head></html>
```

Or delete `index.html` entirely and update the root route in `app/main.py` to redirect:

```python
@app.get("/")
async def root():
    return RedirectResponse("/views/requisitions")
```

- [ ] **Step 7: Delete old frontend test files**

```bash
rm -f tests/frontend/*.test.js tests/frontend/*.test.mjs
```

(These tested app.js/crm.js window globals — no longer relevant.)

- [ ] **Step 8: Run Vite build — verify clean build with no errors**

```bash
cd /root/availai && npm run build
```

- [ ] **Step 9: Run full backend test suite**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v
```

- [ ] **Step 10: Run coverage check**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ --cov=app --cov-report=term-missing --tb=no -q
```

Ensure coverage remains at or above current level.

- [ ] **Step 11: Commit**

```bash
git add -u
git add app/templates/ vite.config.js app/config.py app/main.py
git commit -m "phase3-task12: delete 26K lines of vanilla JS — HTMX + Alpine is now the only frontend"
```

- [ ] **Step 12: Deploy and verify**

```bash
cd /root/availai && git push origin main && docker compose up -d --build
```

Check logs for clean startup:

```bash
docker compose logs -f app | head -50
```

---

## Summary

| Task | Domain | Lines of JS Replaced | Key HTMX Patterns |
|------|--------|---------------------|-------------------|
| 1 | Foundation | 0 (infrastructure) | Script loading, feature flag, shared partials |
| 2 | Shell | ~200 | hx-boost nav, Alpine sidebar state |
| 3 | Requisitions list | ~1,500 | hx-get search, hx-post create, pagination |
| 4 | Requisition detail | ~4,000 | Lazy tabs, inline editing, hx-put/delete |
| 5 | Sourcing | ~3,500 | SSE streaming, result cards, material popups |
| 6 | Companies | ~2,500 | Paginated list, owner filter, tabbed drawer |
| 7 | Vendors + contacts | ~3,000 | Drawer, inline contact editing, click-to-call |
| 8 | Quotes + offers | ~3,000 | Line item editor, auto-save, offer cards |
| 9 | Buy plans | ~2,500 | Workflow actions, approval modals, PO confirm |
| 10 | Prospecting | ~2,000 | Filter dropdowns, enrich button, claim/unclaim |
| 11 | Mobile | ~1,500 | Responsive tables, mobile nav, touch drawers |
| 12 | Cleanup | -26,000 | Delete app.js + crm.js, remove feature flag |

**Total new files:** ~50 Jinja2 partials, 1 JS bootstrap, 1 CSS file, 1 views router
**Total deleted:** ~26,000 lines of vanilla JS + 1,578-line monolithic index.html
**No database migrations required** — all endpoints use existing API/service layer
