# HTMX + Alpine.js Toolkit for AvailAI

> Complete reference for every plugin, extension, and component library installed.
> Each section shows what it does, how to use it, and specific AvailAI examples.

---

## Table of Contents

1. [Alpine.js Plugins (9 official)](#alpine-plugins)
2. [HTMX Extensions (15 installed)](#htmx-extensions)
3. [FastAPI Python Libraries (2)](#fastapi-libraries)
4. [Component Libraries (Penguin UI + Pines UI)](#component-libraries)
5. [Developer Tools](#developer-tools)
6. [AvailAI Feature-to-Plugin Map](#feature-map)

---

## Alpine Plugins

All registered in `app/static/htmx_app.js`. Available globally on every page.

### 1. Focus (`@alpinejs/focus`) — replaces deprecated `@alpinejs/trap`

**What it does:** Traps keyboard focus inside an element (modals, drawers, slide-overs).
Prevents tabbing out of a dialog into the page behind it. ADA/accessibility requirement.

**Directives:** `x-trap`, `x-trap.noscroll`, `x-trap.inert`

**AvailAI uses:**
```html
<!-- RFQ modal — trap focus so Tab key stays inside the form -->
<div x-data="{ open: false }">
  <button @click="open = true">New RFQ</button>
  <div x-show="open" x-trap.noscroll="open" class="fixed inset-0 z-50">
    <form hx-post="/v2/rfq/send" hx-target="#main-content">
      <input name="vendor" placeholder="Vendor email" autofocus>
      <input name="mpn" placeholder="Part number">
      <button type="submit">Send RFQ</button>
      <button type="button" @click="open = false">Cancel</button>
    </form>
  </div>
</div>

<!-- Quote detail slide-over — inert makes background non-interactive -->
<div x-show="slideOpen" x-trap.inert="slideOpen"
     class="fixed right-0 inset-y-0 w-96 bg-white shadow-xl z-50">
  <!-- quote details here -->
</div>
```

**Already used in:** `base.html` global modal (`x-trap.noscroll="open"`)

---

### 2. Persist (`@alpinejs/persist`)

**What it does:** Saves Alpine state to localStorage so it survives page reloads.
Uses `$persist()` magic to wrap any value.

**AvailAI uses:**
```html
<!-- Search filters — remembered across sessions -->
<div x-data="{ filters: $persist({ source: 'all', minQty: 0, sortBy: 'price' }).as('avail_search_filters') }">
  <select x-model="filters.source">
    <option value="all">All Sources</option>
    <option value="nexar">Nexar</option>
    <option value="brokerbin">BrokerBin</option>
  </select>
</div>

<!-- Table density preference — compact vs comfortable -->
<div x-data="{ compact: $persist(false).as('avail_compact_tables') }">
  <button @click="compact = !compact">Toggle Density</button>
  <table :class="compact ? 'text-xs' : 'text-sm'">
    <!-- rows -->
  </table>
</div>

<!-- Sidebar collapsed state — already wired in htmx_app.js store -->
<!-- Alpine.store('sidebar').collapsed uses $persist automatically -->
```

**Already wired in:** `htmx_app.js` — sidebar collapsed state and user preferences store.

---

### 3. Intersect (`@alpinejs/intersect`)

**What it does:** Fires code when an element enters/exits the viewport.
Wrapper around Intersection Observer API. Great for infinite scroll and lazy loading.

**Directives:** `x-intersect`, `x-intersect:enter`, `x-intersect:leave`, `x-intersect.once`, `x-intersect.half`, `x-intersect.full`

**AvailAI uses:**
```html
<!-- Infinite scroll on vendor list -->
<div id="vendor-list">
  {% for vendor in vendors %}
  <div class="vendor-card">{{ vendor.name }}</div>
  {% endfor %}

  <!-- Sentinel: when this scrolls into view, load next page -->
  <div x-data x-intersect.once="htmx.ajax('GET', '/v2/partials/vendors?page={{ next_page }}', '#vendor-list-append')"
       class="h-4">
  </div>
  <div id="vendor-list-append"></div>
</div>

<!-- Lazy-load vendor logo images -->
<img x-data x-intersect.once="$el.src = $el.dataset.src"
     data-src="/api/vendor/{{ vendor.id }}/logo"
     src="/static/placeholder.svg"
     alt="{{ vendor.name }}" class="h-10 w-10">

<!-- Animate search result cards on scroll -->
<div x-data="{ visible: false }" x-intersect:enter="visible = true"
     :class="visible ? 'opacity-100 translate-y-0' : 'opacity-0 translate-y-4'"
     class="transition duration-300">
  <!-- result card content -->
</div>
```

---

### 4. Collapse (`@alpinejs/collapse`)

**What it does:** Smooth expand/collapse animations with proper height transitions.
No need to manually calculate heights or use max-height hacks.

**Directives:** `x-collapse`, `x-collapse.duration.500ms`

**AvailAI uses:**
```html
<!-- Collapsible search result details -->
<div x-data="{ expanded: false }">
  <button @click="expanded = !expanded" class="flex items-center gap-2">
    <span>LM358N — Texas Instruments</span>
    <svg :class="expanded && 'rotate-180'" class="w-4 h-4 transition">...</svg>
  </button>
  <div x-show="expanded" x-collapse class="pl-4 mt-2">
    <p>Lead time: 12 weeks</p>
    <p>MOQ: 1000 pcs</p>
    <p>Datasheet: <a href="#">View</a></p>
  </div>
</div>

<!-- Accordion for vendor intelligence sections -->
<div x-data="{ active: null }">
  {% for section in ['Overview', 'Pricing History', 'RFQ History', 'Notes'] %}
  <div>
    <button @click="active = active === '{{ section }}' ? null : '{{ section }}'">
      {{ section }}
    </button>
    <div x-show="active === '{{ section }}'" x-collapse>
      <div hx-get="/v2/partials/vendors/{{ vendor.id }}/{{ section|lower }}"
           hx-trigger="intersect once" hx-target="this">
        Loading...
      </div>
    </div>
  </div>
  {% endfor %}
</div>
```

---

### 5. Morph (`@alpinejs/morph`)

**What it does:** Morphs an existing DOM element into new HTML while preserving
Alpine state, focus position, scroll position, and form input values.
Critical for HTMX integration via the `alpine-morph` HTMX extension.

**AvailAI uses:**
```html
<!-- Use alpine-morph swap strategy to preserve Alpine state during updates -->
<!-- Just add hx-swap="morph" to any HTMX element -->
<div hx-get="/v2/partials/requisitions" hx-trigger="every 30s"
     hx-swap="morph" hx-target="this">
  <!-- Requisition list auto-refreshes without losing open dropdowns or form inputs -->
</div>

<!-- Or use morph:innerHTML for inner content only -->
<div hx-get="/v2/partials/search/results" hx-swap="morph:innerHTML">
  <!-- Search results update without losing filter selections -->
</div>
```

**How it works with HTMX:** The `alpine-morph` HTMX extension (enabled on `<body>`)
intercepts `hx-swap="morph"` and uses Alpine's morph algorithm instead of innerHTML.

---

### 6. Mask (`@alpinejs/mask`)

**What it does:** Auto-formats text input as the user types using pattern masks.

**Directives:** `x-mask`, `x-mask:dynamic`

**AvailAI uses:**
```html
<!-- Phone number input for CRM contacts -->
<input x-data x-mask="(999) 999-9999" placeholder="(555) 123-4567"
       name="phone" class="input">

<!-- Currency input for quote pricing -->
<input x-data x-mask:dynamic="$money($input, '.', ',')"
       placeholder="$0.00" name="unit_price" class="input">

<!-- Quantity with thousand separators -->
<input x-data x-mask:dynamic="$money($input, '', ',')"
       placeholder="10,000" name="quantity" class="input">

<!-- Part number format (optional — if your part numbers have consistent patterns) -->
<!-- Example: Mouser-style formatting -->
<input x-data x-mask="999-***-9999" placeholder="595-LM358N-3" name="mpn">
```

---

### 7. Sort (`@alpinejs/sort`)

**What it does:** Drag-and-drop reordering of list items.
Emits events when order changes so you can save to server.

**Directives:** `x-sort`, `x-sort:item`, `x-sort:handle`, `x-sort:group`

**AvailAI uses:**
```html
<!-- Reorder RFQ line items by priority -->
<div x-data="{ items: {{ rfq_items|tojson }} }"
     x-sort="(item, position) => {
       htmx.ajax('PATCH', '/v2/api/rfq/reorder', {
         values: { item_id: item, position: position }
       })
     }">
  <template x-for="item in items" :key="item.id">
    <div x-sort:item="item.id"
         class="flex items-center gap-3 p-3 bg-white border rounded-lg mb-2 cursor-move">
      <div x-sort:handle class="text-gray-400 cursor-grab">⠿</div>
      <span x-text="item.mpn"></span>
      <span x-text="'Qty: ' + item.quantity"></span>
    </div>
  </template>
</div>

<!-- Reorder buy plan priorities -->
<div x-sort="(item, pos) => htmx.ajax('PATCH', '/v2/api/buy-plans/reorder', {values: {id: item, pos: pos}})">
  {% for plan in buy_plans %}
  <div x-sort:item="{{ plan.id }}" class="p-2 border rounded mb-1">
    {{ plan.mpn }} — {{ plan.vendor_name }}
  </div>
  {% endfor %}
</div>
```

---

### 8. Anchor (`@alpinejs/anchor`)

**What it does:** Positions a floating element relative to a reference element.
Uses Floating UI under the hood. Perfect for dropdowns, tooltips, popovers.

**Directives:** `x-anchor`, `x-anchor.bottom-start`, `x-anchor.offset.5`

**AvailAI uses:**
```html
<!-- Vendor action dropdown -->
<div x-data="{ open: false }" class="relative">
  <button x-ref="button" @click="open = !open" class="btn btn-sm">Actions</button>
  <div x-show="open" x-anchor.bottom-start.offset.4="$refs.button"
       @click.outside="open = false" x-cloak
       class="bg-white rounded-lg shadow-lg border p-1 w-48 z-50">
    <a hx-get="/v2/partials/rfq/new?vendor={{ vendor.id }}" hx-target="#modal-content"
       @click="$dispatch('open-modal'); open = false" class="block px-3 py-2 hover:bg-gray-100 rounded">
      Send RFQ
    </a>
    <a href="/v2/vendors/{{ vendor.id }}" class="block px-3 py-2 hover:bg-gray-100 rounded">
      View Profile
    </a>
  </div>
</div>

<!-- Tooltip on search result source badge -->
<div x-data="{ show: false }" class="relative inline-block">
  <span x-ref="badge" @mouseenter="show = true" @mouseleave="show = false"
        class="px-2 py-0.5 text-xs rounded-full bg-blue-100 text-blue-700">Nexar</span>
  <div x-show="show" x-anchor.top.offset.6="$refs.badge" x-cloak
       class="bg-gray-900 text-white text-xs px-2 py-1 rounded shadow z-50">
    Last updated 2 hours ago
  </div>
</div>
```

---

### 9. Resize (`@alpinejs/resize`)

**What it does:** Fires code when an element is resized. Uses ResizeObserver.
Good for responsive layouts that need JS logic, not just CSS media queries.

**Directives:** `x-resize`, `x-resize.document`

**AvailAI uses:**
```html
<!-- Switch table to card layout on small containers (not just screen width) -->
<div x-data="{ cardMode: false }"
     x-resize="cardMode = $width < 600">
  <table x-show="!cardMode"><!-- full table --></table>
  <div x-show="cardMode"><!-- card layout --></div>
</div>

<!-- Adjust chart/visualization size in vendor analytics -->
<div x-data="{ chartWidth: 800, chartHeight: 400 }"
     x-resize="chartWidth = $width; chartHeight = Math.max(300, $width * 0.5)">
  <canvas :width="chartWidth" :height="chartHeight"></canvas>
</div>
```

---

## HTMX Extensions

All enabled globally on `<body>` in `base.html` via `hx-ext="..."`.
Debug is intentionally NOT global — enable per-element during development.

### 1. Alpine-Morph (`htmx-ext-alpine-morph`)

**What it does:** Uses Alpine's morph algorithm as HTMX's swap strategy.
Preserves all Alpine state (x-data, stores, watchers) during content updates.

**How to use:** Set `hx-swap="morph"` or `hx-swap="morph:innerHTML"` on any element.

```html
<!-- Search results that preserve filter state on refresh -->
<div id="search-results"
     hx-get="/v2/partials/search/results?q={{ query }}"
     hx-trigger="load"
     hx-swap="morph:innerHTML">
</div>

<!-- Requisition list auto-refreshing without losing dropdown states -->
<div hx-get="/v2/partials/requisitions" hx-trigger="every 60s"
     hx-swap="morph">
  <!-- content -->
</div>
```

---

### 2. Preload (`htmx-ext-preload`)

**What it does:** Prefetches HTMX content on mouseover so clicks feel instant.
Content is cached and swapped immediately when clicked.

**How to use:** Add `preload` attribute to any element with `hx-get`.

```html
<!-- Sidebar nav — preload pages on hover for instant navigation -->
<a href="/v2/requisitions" hx-get="/v2/partials/requisitions"
   hx-target="#main-content" hx-push-url="/v2/requisitions"
   preload="mouseover">
  Requisitions
</a>

<!-- Vendor cards — preload detail view on hover -->
<a hx-get="/v2/partials/vendors/{{ vendor.id }}" hx-target="#main-content"
   preload="mouseover" class="block p-4 border rounded hover:shadow">
  {{ vendor.name }}
</a>

<!-- Preload on page load (for high-priority next pages) -->
<a hx-get="/v2/partials/search" preload="init" class="hidden">preload search</a>
```

---

### 3. Response-Targets (`htmx-ext-response-targets`)

**What it does:** Routes different HTTP status codes to different target elements.
Instead of one target for everything, show errors in an error container.

**How to use:** `hx-target-4*="#error-div"` or `hx-target-404="#not-found"`.

```html
<!-- RFQ form with separate error display -->
<form hx-post="/v2/rfq/send"
      hx-target="#rfq-results"
      hx-target-422="#rfq-errors"
      hx-target-5*="#rfq-errors">
  <div id="rfq-errors" class="text-rose-600 text-sm mb-4"></div>
  <input name="vendor_email" required>
  <input name="mpn" required>
  <button type="submit">Send RFQ</button>
</form>
<div id="rfq-results"></div>

<!-- Search with 404 handling -->
<form hx-get="/v2/partials/search/results"
      hx-target="#search-results"
      hx-target-404="#no-results-msg">
  <input name="q" placeholder="Search parts...">
</form>
<div id="no-results-msg" class="hidden text-gray-500 text-center py-8">
  No results found.
</div>
```

---

### 4. Loading-States (`htmx-ext-loading-states`)

**What it does:** Automatically shows/hides elements and adds/removes CSS classes
during HTMX requests. No Alpine needed for basic loading indicators.

**How to use:** `data-loading`, `data-loading-class`, `data-loading-disable`,
`data-loading-path` (only trigger for specific URL paths).

```html
<!-- Search button with spinner -->
<button hx-get="/v2/partials/search/results" hx-target="#results"
        data-loading-disable>
  <span data-loading-class="hidden">Search</span>
  <span data-loading class="hidden">
    <svg class="spinner w-4 h-4 inline mr-1">...</svg> Searching...
  </span>
</button>

<!-- Table skeleton while loading -->
<div id="vendor-table">
  <div data-loading data-loading-path="/v2/partials/vendors"
       class="animate-pulse space-y-2 hidden">
    <div class="h-8 bg-gray-200 rounded"></div>
    <div class="h-8 bg-gray-200 rounded"></div>
    <div class="h-8 bg-gray-200 rounded"></div>
  </div>
  <!-- actual table content -->
</div>

<!-- Disable entire form during submission -->
<form hx-post="/v2/rfq/send" data-loading-disable>
  <!-- all inputs auto-disabled while request is in flight -->
</form>
```

---

### 5. Class-Tools (`htmx-ext-class-tools`)

**What it does:** Add/remove/toggle CSS classes on a timer. Good for flash effects,
temporary highlights, and auto-dismissing notifications.

**How to use:** `classes="add classname:timing"`, `remove`, `toggle`.

```html
<!-- Flash highlight on newly added search result -->
<div classes="add bg-yellow-100:0s, remove bg-yellow-100:2s"
     class="p-3 border rounded transition-colors duration-500">
  New result: LM358N — $0.42 from DigiKey
</div>

<!-- Auto-dismiss success banner -->
<div classes="add opacity-0:3s, add hidden:3.5s"
     class="bg-emerald-50 text-emerald-700 p-3 rounded transition-opacity">
  RFQ sent successfully!
</div>

<!-- Pulse animation on updated vendor card -->
<div classes="add ring-2 ring-brand-500:0s, remove ring-2 ring-brand-500:1.5s"
     class="transition-all duration-300">
  <!-- vendor card content -->
</div>
```

---

### 6. Head-Support (`htmx-ext-head-support`)

**What it does:** Merges `<head>` content (title, meta tags, CSS links) when HTMX
navigates between pages. Without this, page titles don't update on HTMX navigation.

**How to use:** Enabled globally. Return `<head>` content in HTMX responses:

```html
<!-- In your partial templates, include a title block -->
<!-- partials/vendors/list.html -->
<head>
  <title>Vendors — AvailAI</title>
</head>
<div>
  <!-- vendor list content -->
</div>

<!-- partials/requisitions/detail.html -->
<head>
  <title>REQ-{{ req.id }} — AvailAI</title>
</head>
<div>
  <!-- requisition detail content -->
</div>
```

The extension automatically picks up `<head>` from HTMX responses and merges it.

---

### 7. Multi-Swap (`htmx-ext-multi-swap`)

**What it does:** Swap multiple target elements from a single HTMX response.
The server returns HTML with multiple `id`-tagged elements and they all get swapped.

**How to use:** `hx-swap="multi:#target1:innerHTML,#target2:outerHTML"`

```html
<!-- Update both search results AND result count in one request -->
<form hx-get="/v2/partials/search/results"
      hx-swap="multi:#search-results:innerHTML,#result-count:innerHTML,#breadcrumb:innerHTML">
  <input name="q" placeholder="Search...">
</form>

<!-- Server response returns: -->
<!-- <div id="search-results">...results...</div> -->
<!-- <span id="result-count">42 results</span> -->
<!-- <span id="breadcrumb">Search > LM358N</span> -->
```

---

### 8. SSE (`htmx-ext-sse`)

**What it does:** Server-Sent Events for real-time server-to-client streaming.

**Already used for:** Sourcing search progress (see `sourcingProgress` component).

```html
<!-- Real-time email mining status -->
<div hx-ext="sse" sse-connect="/v2/stream/mining-status"
     sse-swap="status-update" hx-target="#mining-status">
  <div id="mining-status">Waiting for updates...</div>
</div>

<!-- Real-time RFQ response notifications -->
<div hx-ext="sse" sse-connect="/v2/stream/rfq-responses">
  <div sse-swap="new-response" hx-target="#rfq-notifications" hx-swap="afterbegin">
  </div>
</div>
```

---

### 9. WS — WebSocket (`htmx-ext-ws`)

**What it does:** Full-duplex WebSocket communication with auto-reconnect
using exponential backoff with full jitter.

```html
<!-- Live notification feed -->
<div hx-ext="ws" ws-connect="/v2/ws/notifications">
  <div id="notification-feed">
    <!-- New notifications appear here automatically -->
  </div>
  <!-- Send a message (form submit goes through WebSocket) -->
  <form ws-send>
    <input name="message" placeholder="Quick note...">
    <button type="submit">Send</button>
  </form>
</div>
```

---

### 10. JSON-Enc (`htmx-ext-json-enc`)

**What it does:** Encodes request body as JSON instead of form-urlencoded.
Useful when your FastAPI endpoints expect JSON.

```html
<!-- Send JSON body to API endpoint -->
<form hx-post="/v2/api/vendors" hx-ext="json-enc"
      hx-target="#vendor-list" hx-swap="afterbegin">
  <input name="name" placeholder="Vendor name">
  <input name="email" placeholder="Email">
  <button type="submit">Add Vendor</button>
</form>
```

---

### 11. Path-Params (`htmx-ext-path-params`)

**What it does:** Substitute path parameters in URLs from element data.

```html
<!-- Dynamic vendor URL from data attribute -->
<button hx-get="/v2/partials/vendors/{vendor_id}"
        hx-vals='{"vendor_id": "{{ vendor.id }}"}'
        hx-target="#main-content">
  View {{ vendor.name }}
</button>
```

---

### 12. Remove-Me (`htmx-ext-remove-me`)

**What it does:** Auto-removes an element from the DOM after a timeout.
Perfect for flash messages and temporary alerts.

```html
<!-- Success toast that auto-removes after 4 seconds -->
<div remove-me="4s"
     class="fixed top-4 right-4 bg-emerald-50 text-emerald-700 px-4 py-3 rounded-lg shadow-lg">
  RFQ sent to {{ vendor_name }} successfully!
</div>

<!-- Temporary "just saved" indicator -->
<span remove-me="2s" class="text-emerald-600 text-sm ml-2">Saved!</span>
```

---

### 13. Restored (`htmx-ext-restored`)

**What it does:** Fires an `htmx:restored` event when the browser back-button
restores a page from cache. Use to re-initialize things after back navigation.

```html
<!-- Re-fetch fresh data when user navigates back -->
<div hx-get="/v2/partials/requisitions" hx-trigger="htmx:restored from:body"
     hx-target="#main-content">
</div>
```

---

### 14. Debug (`htmx-ext-debug`)

**What it does:** Logs ALL HTMX events for the element to the browser console.
**Use only during development.** Not enabled globally.

```html
<!-- Debug a specific element's HTMX lifecycle -->
<div hx-ext="debug" hx-get="/v2/partials/search/results" hx-target="#results">
  <!-- all htmx events for this element logged to console -->
</div>
```

---

### 15. Idiomorph

**What it does:** Alternative DOM morphing algorithm by the HTMX team.
Uses element IDs to match old and new DOM trees, producing minimal changes.

```html
<!-- Use idiomorph for swap -->
<div hx-get="/v2/partials/vendors" hx-swap="morph:idiomorph"
     hx-target="this" hx-trigger="every 30s">
  <!-- auto-refresh with minimal DOM changes -->
</div>
```

**alpine-morph vs idiomorph:** Use `alpine-morph` (hx-swap="morph") when you have
Alpine state to preserve. Use `idiomorph` when you just want efficient DOM updates
without Alpine components in that section.

---

## FastAPI Libraries

### 1. FastHX (`fasthx`)

**What it does:** Decorator-based rendering that auto-detects HTMX vs full-page requests.
Same route serves both JSON (for API) and HTML (for HTMX).

```python
# app/routers/vendors.py
from fasthx import hx

@router.get("/v2/partials/vendors")
@hx("htmx/partials/vendors/list.html")  # renders template for HTMX requests
async def vendor_list(request: Request, db: Session = Depends(get_db)):
    vendors = db.query(VendorCard).all()
    return {"vendors": vendors}  # returned as JSON for API, rendered as HTML for HTMX
```

### 2. fastapi-htmx (`fastapi-htmx`)

**What it does:** Provides `HtmxRequest` dependency and template routing helpers.
Auto-selects partial vs full-page template based on `HX-Request` header.

```python
# app/routers/vendors.py
from fastapi_htmx import htmx

@router.get("/v2/vendors")
@htmx("htmx/partials/vendors/list.html", "htmx/base_page.html")
async def vendors_page(request: Request, db: Session = Depends(get_db)):
    """First template for HTMX partial requests, second for full page loads."""
    vendors = db.query(VendorCard).all()
    return {"vendors": vendors}
```

**Manual approach (no library needed):**
```python
# This is what you're already doing — both libraries just reduce this boilerplate
@router.get("/v2/vendors")
async def vendors_page(request: Request, db: Session = Depends(get_db)):
    vendors = db.query(VendorCard).all()
    template = "htmx/partials/vendors/list.html"
    if "HX-Request" not in request.headers:
        template = "htmx/base_page.html"  # full page wrapper
    return templates.TemplateResponse(template, {"request": request, "vendors": vendors})
```

---

## Component Libraries

### Penguin UI (penguinui.com) — FREE, copy-paste

**Best components for AvailAI:**

| Component | Use in AvailAI | Page |
|-----------|---------------|------|
| **Table** | Requisition list, search results, vendor list, quotes | All list pages |
| **Modal** | RFQ form, quote details, confirm dialogs | Global |
| **Dropdown** | Vendor actions, bulk actions, filter menus | Vendor list, search |
| **Tabs** | Vendor detail sections, settings page | vendor detail, settings |
| **Toast** | Success/error notifications | Global |
| **Badge** | Source type labels, status indicators | Search results |
| **Accordion** | Collapsible vendor intel sections | Vendor detail |
| **Combobox** | Part number search with autocomplete | Search form |
| **Pagination** | All list views | Everywhere |
| **Sidebar** | Main navigation (reference implementation) | base.html |
| **Card** | Proactive match cards, vendor cards | Proactive, vendors |
| **Alert** | System notices, mining status | Dashboard |
| **Steps** | RFQ workflow progress | RFQ detail |
| **Skeleton** | Loading placeholders | All pages |
| **Toggle** | Feature flags in settings, enable/disable sources | Settings |

**How to use:** Visit https://www.penguinui.com, find the component, copy the HTML.
All components use Alpine.js `x-data` + Tailwind classes — drop directly into Jinja2 templates.

### Pines UI (devdojo.com/pines) — FREE, copy-paste

**Best components for AvailAI:**

| Component | Use in AvailAI | Why Pines over Penguin |
|-----------|---------------|----------------------|
| **Command Palette** | Global quick-search (Cmd+K) for parts, vendors, requisitions | Not in Penguin |
| **Date Picker** | Quote validity dates, RFQ deadlines | Not in Penguin |
| **Slide-over** | Quick-view panels for vendor/quote details | More polished |
| **Context Menu** | Right-click actions on table rows | Not in Penguin |
| **Hover Card** | Quick vendor preview on hover | Not in Penguin |
| **Copy to Clipboard** | Copy part numbers, vendor emails | Not in Penguin |
| **Popover** | Rich tooltips with data | Better than Penguin tooltip |
| **Image Gallery** | Vendor logos, product images | Not in Penguin |
| **Toast Notification** | More animation options | More variants |
| **Full Screen Modal** | Large data views, comparison tables | Not in Penguin |
| **Text Animation** | Dashboard welcome, status updates | Polish |

**How to use:** Visit https://devdojo.com/pines, browse elements, copy the HTML.
Uses Alpine.js + Tailwind. Drop into Jinja2 templates.

### DaisyUI (daisyui.com) — FREE, class-based

**Alternative approach:** Instead of copying Alpine components, DaisyUI gives you
Tailwind CSS classes that style components without JavaScript.

```bash
npm install daisyui  # optional — only if you want class-based components
```

```html
<!-- DaisyUI table classes (no Alpine needed for styling) -->
<table class="table table-zebra">
  <thead><tr><th>MPN</th><th>Vendor</th><th>Price</th></tr></thead>
  <tbody><!-- rows --></tbody>
</table>
```

---

## Developer Tools

### Browser Extensions (install these today)

1. **htmx-debugger** (Chrome/Firefox)
   - DevTools panel that captures all HTMX events in real-time
   - Groups related events, shows timing, XHR details
   - Install: [Chrome Web Store](https://chromewebstore.google.com/detail/htmx-debugger/fkpjmdhppdadklmcjbifffmjplgoboic)

2. **Alpine.js DevTools** (Chrome/Firefox)
   - Inspect Alpine component state, data, stores
   - Edit state live in DevTools
   - Install: [Chrome Web Store](https://chromewebstore.google.com/detail/alpinejs-devtools/fopaemeedckajflibkpifppcankfmbhk)

3. **Alpine.js DevTools Pro** (Chrome/Firefox) — free to use
   - Advanced component inspector, event monitor, store management
   - Supports HTMX + Alpine stacks explicitly
   - Install: [Chrome Web Store](https://chromewebstore.google.com/detail/alpinejs-devtools-pro/lljjpbaakboipfnhngbmmfmlbiiklmnl)

### In-Code Debugging

```html
<!-- Add hx-ext="debug" to any element to log its HTMX events -->
<div hx-ext="debug" hx-get="/v2/partials/search/results">
  <!-- open browser console to see all events -->
</div>

<!-- Alpine: dump component state visually -->
<div x-data="{ items: [], loading: false }">
  <pre x-text="JSON.stringify($data, null, 2)" class="text-xs bg-gray-100 p-2 rounded"></pre>
</div>
```

---

## Feature Map

Quick reference: which plugin to use for each AvailAI feature.

| AvailAI Feature | Alpine Plugin | HTMX Extension | Component Source |
|----------------|---------------|-----------------|------------------|
| **Sidebar navigation** | persist, focus | preload | Penguin UI Sidebar |
| **Part search form** | mask (qty/price formatting) | loading-states, response-targets | Penguin UI Combobox |
| **Search results** | intersect (infinite scroll), collapse (details) | alpine-morph, multi-swap | Penguin UI Table + Cards |
| **Sourcing progress** | — | sse (already using) | Custom (already built) |
| **Requisition list** | persist (filters), sort (reorder) | preload, alpine-morph | Penguin UI Table |
| **Requisition detail** | collapse (sections), focus (modals) | head-support (title) | Pines UI Tabs |
| **RFQ workflow** | mask (formatting), focus (modal) | loading-states, response-targets, json-enc | Pines UI Steps + Modal |
| **Vendor list** | intersect (lazy load), persist (sort prefs) | preload, alpine-morph | Penguin UI Table |
| **Vendor detail** | collapse (accordion), anchor (tooltips) | head-support, multi-swap | Pines UI Accordion + Hover Card |
| **Company CRM** | persist (view prefs), sort (contacts) | alpine-morph | Penguin UI Table |
| **Quotes** | mask (currency), sort (line items) | response-targets, loading-states | Penguin UI Table + Modal |
| **Buy Plans** | sort (priority order), collapse | alpine-morph, loading-states | Penguin UI Card |
| **Proactive matching** | intersect (lazy cards) | sse (new matches), remove-me (alerts) | Penguin UI Card |
| **Email mining status** | — | sse (live status), class-tools (highlights) | Pines UI Toast |
| **Settings page** | persist (all settings) | — | Penguin UI Tabs + Toggle |
| **Global search** | anchor (dropdown), resize | response-targets | Pines UI Command Palette |
| **Notifications** | — | ws (real-time), remove-me (auto-dismiss) | Pines UI Toast |
| **Error handling** | — | response-targets (4xx/5xx) | Penguin UI Alert |
| **Page transitions** | — | head-support, restored, preload | N/A |
| **Debug (dev only)** | — | debug (per-element) | N/A |

---

## Quick Start Checklist

After this installation, here's what to do when building new features:

1. **Check the Feature Map above** to see which plugins apply
2. **Browse Penguin UI / Pines UI** for a matching component
3. **Copy the component HTML** into your Jinja2 partial
4. **Add HTMX attributes** (`hx-get`, `hx-target`, `hx-swap`) for server interaction
5. **Use `hx-swap="morph"`** instead of `innerHTML` when Alpine state needs preserving
6. **Add `preload="mouseover"`** to navigation links for instant feel
7. **Use `loading-states`** attributes for loading indicators (no JS needed)
8. **Use `response-targets`** to route errors to separate elements
9. **Test** with the htmx-debugger and Alpine DevTools extensions

## HTMX + Alpine Gotcha Reference

1. **HTMX swaps destroy Alpine state** — Use `hx-swap="morph"` (alpine-morph) to prevent this
2. **Alpine `x-if` + HTMX attributes** — Call `htmx.process(el)` after `x-if` renders new HTMX elements
3. **Alpine `$persist` needs unique keys** — Always use `.as('avail_something')` to namespace
4. **SSE events** — Listen on `document.body` not on the SSE element itself
5. **`preload` + auth pages** — Preloaded content may get 401; use `response-targets` to handle
6. **`loading-states` scope** — The `data-loading` element must be inside the element making the request
