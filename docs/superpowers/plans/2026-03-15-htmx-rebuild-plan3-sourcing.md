# Plan 3: Sourcing Engine — Search, Results, Leads

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build Part Search page and full sourcing engine UI with SSE progress streaming, filterable lead cards, lead detail with evidence/safety/buyer actions.

**Architecture:** Part Search uses simple HTMX post for search. Sourcing results use SSE (Server-Sent Events) via `hx-ext="sse"` for real-time per-source progress, with lead cards rendered as HTMX partials. Lead detail shows evidence list, safety review (reusable shared component), and lightweight buyer status.

**Tech Stack:** HTMX 2.x (with SSE extension), Alpine.js 3.x, Jinja2, FastAPI EventSourceResponse, Tailwind CSS (brand palette)

**Spec:** `docs/superpowers/specs/2026-03-15-htmx-frontend-rebuild-design.md` (Sections 7, 9)

**Depends on:** Plan 1 (Foundation) must be complete first.

---

## Task 1: Part Search — Form and Results Templates

**Files:**
- Rewrite: `app/templates/htmx/partials/search/form.html`
- Rewrite: `app/templates/htmx/partials/search/results.html`
- Modify: `app/routers/htmx_views.py` (update `search_form_partial`, `search_run` to use new templates)

### Context

The Part Search page is a standalone search tool (accessed via sidebar "Part Search"). It posts to `POST /v2/partials/search/run`, receives results HTML, and swaps it into the results area. The search form and results templates already exist but need to be rebuilt with brand styling and source badges.

### Steps

- [x] **Step 1: Rewrite `form.html` — search form with large input**

File: `app/templates/partials/search/form.html`

Template structure:
```
<div id="breadcrumb" hx-swap-oob="true">Part Search</div>

<div class="space-y-6">
  <!-- Header -->
  <h1 class="text-2xl font-bold text-gray-900">Part Search</h1>

  <!-- Search form -->
  <form hx-post="/v2/partials/search/run"
        hx-target="#search-results"
        hx-indicator="#search-spinner">
    <div class="flex gap-3">
      <input type="text" name="mpn"
             placeholder="Enter part number (e.g. LM317T, STM32F407VG)"
             class="flex-1 px-4 py-3 text-lg border border-brand-200 rounded-lg
                    focus:ring-2 focus:ring-brand-500 focus:border-brand-500"
             required
             x-data x-ref="mpn"
             @input="$refs.searchBtn.disabled = !$refs.mpn.value.trim()">
      <button type="submit" x-ref="searchBtn" disabled
              class="px-6 py-3 bg-brand-500 text-white font-semibold rounded-lg
                     hover:bg-brand-600 disabled:opacity-50 disabled:cursor-not-allowed
                     flex items-center gap-2">
        <span id="search-spinner" class="htmx-indicator">
          <!-- spinner SVG -->
        </span>
        Search All Sources
      </button>
    </div>
  </form>

  <!-- Results container -->
  <div id="search-results">
    <!-- Empty state (shown initially) -->
    <div class="flex flex-col items-center justify-center py-16 text-gray-400">
      <!-- search icon SVG (magnifying glass, 48px) -->
      <p class="mt-4 text-lg">Enter a part number to search all sources</p>
    </div>
  </div>
</div>
```

Key requirements:
- Button disabled until text entered (Alpine `x-data` inline)
- `hx-indicator` shows spinner SVG inside button during search
- Empty state with magnifying glass icon and "Enter a part number to search all sources"
- Use brand colors: input border `brand-200`, focus ring `brand-500`, button `brand-500`/`brand-600`

- [x] **Step 2: Rewrite `results.html` — results table with source badges**

File: `app/templates/partials/search/results.html`

Template receives context vars: `results` (list of dicts), `mpn` (str), `elapsed_seconds` (float), `error` (str|None).

Template structure:
```
{% if error %}
<div class="p-4 bg-rose-50 text-rose-700 rounded-lg border border-rose-200">
  <p class="font-medium">Search failed</p>
  <p class="text-sm mt-1">{{ error }}</p>
  <button hx-post="/v2/partials/search/run"
          hx-target="#search-results"
          hx-vals='{"mpn": "{{ mpn }}"}'
          class="mt-3 text-sm text-brand-500 hover:text-brand-600 font-medium">
    Retry
  </button>
</div>
{% elif results %}
<div class="space-y-3">
  <!-- Results header -->
  <div class="flex items-center justify-between">
    <p class="text-sm text-gray-500">
      <span class="font-semibold text-gray-900">{{ results|length }}</span> results for
      <span class="font-mono font-semibold text-gray-900">{{ mpn }}</span>
      in {{ "%.1f"|format(elapsed_seconds) }}s
    </p>
  </div>

  <!-- Results table -->
  <div class="overflow-x-auto bg-white rounded-lg border border-brand-200">
    <table class="min-w-full divide-y divide-brand-200">
      <thead class="bg-brand-50">
        <tr>
          <th class="px-4 py-3 text-left text-xs font-semibold text-brand-600 uppercase tracking-wider">Vendor</th>
          <th class="...">MPN</th>
          <th class="...">Manufacturer</th>
          <th class="... text-right">Qty Available</th>
          <th class="... text-right">Unit Price</th>
          <th class="...">Source</th>
          <th class="...">Lead Time</th>
        </tr>
      </thead>
      <tbody class="divide-y divide-gray-200">
        {% for r in results %}
        <tr class="hover:bg-brand-50">
          <td class="px-4 py-3 text-sm font-medium text-gray-900">
            {{ r.vendor_name or 'Unknown' }}
          </td>
          <td class="px-4 py-3 text-sm font-mono text-gray-900">
            {{ r.mpn_matched or r.mpn or 'n/a' }}
          </td>
          <td class="px-4 py-3 text-sm text-gray-600">
            {{ r.manufacturer or 'n/a' }}
          </td>
          <td class="px-4 py-3 text-sm text-right text-gray-900">
            {{ '{:,}'.format(r.qty_available) if r.qty_available else 'n/a' }}
          </td>
          <td class="px-4 py-3 text-sm text-right text-gray-900 font-medium">
            {% if r.unit_price %}${{ '%.4f'|format(r.unit_price) }}{% else %}RFQ{% endif %}
          </td>
          <td class="px-4 py-3">
            {% include "partials/shared/source_badge.html" %}
          </td>
          <td class="px-4 py-3 text-sm text-gray-600">
            {{ r.lead_time or (r.lead_time_days ~ ' days' if r.lead_time_days else 'n/a') }}
          </td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
</div>
{% else %}
<!-- Empty results -->
<div class="flex flex-col items-center justify-center py-16 text-gray-400">
  <!-- empty inbox icon SVG -->
  <p class="mt-4 text-lg">No results found for <span class="font-mono font-semibold">{{ mpn }}</span></p>
  <p class="text-sm mt-1">Try a different part number or check back later</p>
</div>
{% endif %}
```

- [x] **Step 3: Create `partials/shared/source_badge.html`**

File: `app/templates/partials/shared/source_badge.html`

Reusable source badge partial. Expects `r.source_type` (or standalone var `source_type`) to be available in context.

Source badge color mapping (from spec):
- `brokerbin` -> `bg-sky-100 text-sky-700` label "BrokerBin"
- `nexar` / `octopart` -> `bg-violet-100 text-violet-700` label "Nexar"
- `digikey` -> `bg-orange-100 text-orange-700` label "DigiKey"
- `mouser` -> `bg-teal-100 text-teal-700` label "Mouser"
- `oemsecrets` -> `bg-fuchsia-100 text-fuchsia-700` label "OEMSecrets"
- `element14` / `farnell` -> `bg-lime-100 text-lime-700` label "Element14"
- `ebay` -> `bg-yellow-100 text-yellow-700` label "eBay"
- Default -> `bg-gray-100 text-gray-600` label (source_type|title)

Template implementation:
```
{% set st = (r.source_type if r is defined and r.source_type is defined else source_type|default(''))|lower %}
{% if st == 'brokerbin' %}
  <span class="inline-flex px-2 py-0.5 text-xs font-medium rounded-full bg-sky-100 text-sky-700">BrokerBin</span>
{% elif st in ('nexar', 'octopart') %}
  <span class="inline-flex px-2 py-0.5 text-xs font-medium rounded-full bg-violet-100 text-violet-700">Nexar</span>
{% elif st == 'digikey' %}
  <span class="inline-flex px-2 py-0.5 text-xs font-medium rounded-full bg-orange-100 text-orange-700">DigiKey</span>
{% elif st == 'mouser' %}
  <span class="inline-flex px-2 py-0.5 text-xs font-medium rounded-full bg-teal-100 text-teal-700">Mouser</span>
{% elif st == 'oemsecrets' %}
  <span class="inline-flex px-2 py-0.5 text-xs font-medium rounded-full bg-fuchsia-100 text-fuchsia-700">OEMSecrets</span>
{% elif st in ('element14', 'farnell') %}
  <span class="inline-flex px-2 py-0.5 text-xs font-medium rounded-full bg-lime-100 text-lime-700">Element14</span>
{% elif st == 'ebay' %}
  <span class="inline-flex px-2 py-0.5 text-xs font-medium rounded-full bg-yellow-100 text-yellow-700">eBay</span>
{% else %}
  <span class="inline-flex px-2 py-0.5 text-xs font-medium rounded-full bg-gray-100 text-gray-600">{{ st|title or 'Unknown' }}</span>
{% endif %}
```

- [x] **Step 4: Move templates to spec directory structure**

Per the spec (Section 14), search templates should be at `app/templates/partials/search/` (not `htmx/partials/search/`). Move:
- `app/templates/htmx/partials/search/form.html` -> `app/templates/partials/search/form.html`
- `app/templates/htmx/partials/search/results.html` -> `app/templates/partials/search/results.html`

Update the router template paths in `htmx_views.py`:
- `search_form_partial`: change template to `partials/search/form.html`
- `search_run`: change template to `partials/search/results.html`

- [x] **Step 5: Verify Part Search**

Test manually:
1. Navigate to `/v2/search` -- form renders with large input, button disabled
2. Type a part number -- button enables
3. Click "Search All Sources" -- spinner shows, results table renders
4. Verify source badges show correct colors per connector
5. Verify price formatting ("$X.XXXX" or "RFQ")
6. Verify qty formatted with commas
7. Search for nonsense MPN -- empty state shows
8. Force an error (disable connectors) -- error state with retry button shows

---

## Task 2: Safety Review Shared Component

**Files:**
- Create: `app/templates/partials/shared/safety_review.html`

### Context

The safety review block is a reusable component used in both vendor detail (Task in Plan 2) and lead detail (Task 5 below). It renders safety band color, summary, positive signals, caution signals, and recommended action. Building it here so both lead detail and vendor detail can include it.

### Steps

- [x] **Step 1: Create `partials/shared/safety_review.html`**

File: `app/templates/partials/shared/safety_review.html`

Expects context vars:
- `safety_band` (str): `"low_risk"`, `"medium_risk"`, `"high_risk"`, or `"unknown"`
- `safety_score` (float|None): 0-100
- `safety_summary` (str): Human-readable summary
- `safety_flags` (list[str]): List of flag strings. Positive signals prefixed with `"positive:"`. All others are caution signals.
- `safety_available` (bool, optional): If false, show "No safety data available" message

Template structure:
```
{% if safety_available is defined and not safety_available %}
<div class="bg-gray-50 rounded-lg p-4 border border-gray-200">
  <p class="text-sm text-gray-500">No safety data available -- safety is assessed when sourcing leads are created.</p>
</div>
{% else %}

{% set band_colors = {
  'low_risk': 'bg-emerald-500',
  'medium_risk': 'bg-amber-500',
  'high_risk': 'bg-rose-500',
  'unknown': 'bg-gray-400'
} %}
{% set badge_colors = {
  'low_risk': 'bg-emerald-50 text-emerald-700 border-emerald-200',
  'medium_risk': 'bg-amber-50 text-amber-700 border-amber-200',
  'high_risk': 'bg-rose-50 text-rose-700 border-rose-200',
  'unknown': 'bg-gray-100 text-gray-600 border-gray-200'
} %}
{% set band_labels = {
  'low_risk': 'Low Risk',
  'medium_risk': 'Medium Risk',
  'high_risk': 'High Risk',
  'unknown': 'Unknown'
} %}

<div class="bg-white rounded-lg border border-brand-200 overflow-hidden">
  <!-- Color band at top -->
  <div class="h-1.5 {{ band_colors.get(safety_band, 'bg-gray-400') }}"></div>

  <div class="p-4 space-y-4">
    <!-- Header row: badge + score -->
    <div class="flex items-center justify-between">
      <h3 class="text-sm font-semibold text-gray-900">Safety Review</h3>
      <div class="flex items-center gap-3">
        {% if safety_score is not none %}
        <span class="text-sm font-medium text-gray-600">{{ '%.0f'|format(safety_score) }}%</span>
        {% endif %}
        <span class="inline-flex px-2.5 py-0.5 text-xs font-medium rounded-full border
                     {{ badge_colors.get(safety_band, badge_colors.unknown) }}">
          {{ band_labels.get(safety_band, 'Unknown') }}
        </span>
      </div>
    </div>

    <!-- Summary -->
    <p class="text-sm text-gray-600">{{ safety_summary }}</p>

    <!-- Positive signals -->
    {% set positive_flags_list = [] %}
    {% for flag in safety_flags %}
      {% if flag.startswith('positive:') %}
        {% if positive_flags_list.append(flag) %}{% endif %}
      {% endif %}
    {% endfor %}
    {% if positive_flags_list %}
    <div>
      <h4 class="text-xs font-semibold text-emerald-700 uppercase tracking-wider mb-2">Positive Signals</h4>
      <ul class="space-y-1">
        {% for flag in positive_flags_list %}
        <li class="flex items-start gap-2 text-sm text-emerald-700">
          <svg class="w-4 h-4 mt-0.5 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"/>
          </svg>
          <span>{{ flag.replace('positive:', '').replace('_', ' ')|title }}</span>
        </li>
        {% endfor %}
      </ul>
    </div>
    {% endif %}

    <!-- Caution signals -->
    {% set caution_flags_list = [] %}
    {% for flag in safety_flags %}
      {% if not flag.startswith('positive:') %}
        {% if caution_flags_list.append(flag) %}{% endif %}
      {% endif %}
    {% endfor %}
    {% if caution_flags_list %}
    <div>
      <h4 class="text-xs font-semibold text-amber-700 uppercase tracking-wider mb-2">Caution Signals</h4>
      <ul class="space-y-1">
        {% for flag in caution_flags_list %}
        <li class="flex items-start gap-2 text-sm text-amber-700">
          <svg class="w-4 h-4 mt-0.5 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2"
                  d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-2.5L13.732 4.5c-.77-.833-2.694-.833-3.464 0L3.34 16.5c-.77.833.192 2.5 1.732 2.5z"/>
          </svg>
          <span>{{ flag.replace('_', ' ')|title }}</span>
        </li>
        {% endfor %}
      </ul>
    </div>
    {% endif %}

    <!-- Recommended action -->
    {% if safety_band == 'high_risk' %}
    <div class="bg-rose-50 rounded p-3 text-sm text-rose-700">
      <span class="font-medium">Recommended:</span> Verify vendor identity and contact details before any outreach.
    </div>
    {% elif safety_band == 'medium_risk' %}
    <div class="bg-amber-50 rounded p-3 text-sm text-amber-700">
      <span class="font-medium">Recommended:</span> Confirm business footprint and contact path before relying on inventory claims.
    </div>
    {% elif safety_band == 'low_risk' %}
    <div class="bg-emerald-50 rounded p-3 text-sm text-emerald-700">
      <span class="font-medium">Recommended:</span> Lower risk -- proceed with standard verification of stock and terms.
    </div>
    {% endif %}
  </div>
</div>
{% endif %}
```

Usage from vendor detail:
```jinja2
{% with safety_band=vendor_safety_band, safety_score=vendor_safety_score,
        safety_summary=vendor_safety_summary, safety_flags=vendor_safety_flags,
        safety_available=safety_available %}
  {% include "partials/shared/safety_review.html" %}
{% endwith %}
```

Usage from lead detail:
```jinja2
{% with safety_band=lead.vendor_safety_band, safety_score=lead.vendor_safety_score,
        safety_summary=lead.vendor_safety_summary, safety_flags=lead.vendor_safety_flags %}
  {% include "partials/shared/safety_review.html" %}
{% endwith %}
```

---

## Task 3: SSE Streaming Progress for Sourcing Search

**Files:**
- Create: `app/templates/partials/sourcing/search_progress.html`
- Modify: `app/routers/htmx_views.py` (add SSE stream endpoint)
- Modify: `app/services/sse_broker.py` (no changes needed -- existing broker supports this)

### Context

When a buyer clicks "Search" on a requirement row in requisition detail, the UI connects to an SSE endpoint. The server fires all connectors via `asyncio.gather()` and publishes per-source completion events through `sse_broker.py`. The progress partial shows per-source status rows and an overall progress bar. When all sources complete, the SSE connection closes and the progress panel collapses to reveal the full lead results.

The SSE channel name convention: `sourcing:{requirement_id}`.

### Steps

- [x] **Step 1: Add SSE stream endpoint to router**

File: `app/routers/htmx_views.py`

Add new route:
```python
from sse_starlette.sse import EventSourceResponse

@router.get("/v2/partials/sourcing/{requirement_id}/stream")
async def sourcing_stream(
    request: Request,
    requirement_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """SSE endpoint for sourcing search progress.

    Streams per-source completion events as connectors finish searching.
    Client connects via hx-ext="sse" sse-connect attribute.
    Channel: sourcing:{requirement_id}
    """
    from ..services.sse_broker import broker

    req = db.query(Requirement).filter(Requirement.id == requirement_id).first()
    if not req:
        raise HTTPException(404, "Requirement not found")

    async def event_generator():
        async for msg in broker.listen(f"sourcing:{requirement_id}"):
            if await request.is_disconnected():
                break
            yield {
                "event": msg["event"],
                "data": msg["data"],
            }

    return EventSourceResponse(event_generator())
```

- [x] **Step 2: Add sourcing search trigger endpoint**

File: `app/routers/htmx_views.py`

Add route that kicks off the multi-source search and publishes SSE events:
```python
@router.post("/v2/partials/sourcing/{requirement_id}/search")
async def sourcing_search_trigger(
    request: Request,
    requirement_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Trigger multi-source search for a requirement.

    Runs connectors in parallel, publishes SSE events per source completion,
    syncs leads on completion, returns final results partial.
    """
    import asyncio
    import json
    import time
    from ..services.sse_broker import broker
    from ..search_service import search_single_source
    from ..services.sourcing_leads import sync_leads_for_sightings

    req = db.query(Requirement).filter(Requirement.id == requirement_id).first()
    if not req:
        raise HTTPException(404, "Requirement not found")

    mpn = req.primary_mpn or ""
    sources = ["brokerbin", "nexar", "digikey", "mouser", "oemsecrets", "element14"]
    channel = f"sourcing:{requirement_id}"
    all_sightings = []

    async def search_source(source_name):
        start = time.time()
        try:
            results = await search_single_source(mpn, source_name, db)
            elapsed = int((time.time() - start) * 1000)
            count = len(results) if results else 0
            await broker.publish(channel, "source-complete", json.dumps({
                "source": source_name, "count": count,
                "elapsed_ms": elapsed, "status": "done"
            }))
            return results or []
        except Exception as exc:
            elapsed = int((time.time() - start) * 1000)
            await broker.publish(channel, "source-complete", json.dumps({
                "source": source_name, "count": 0,
                "elapsed_ms": elapsed, "status": "failed",
                "error": str(exc)
            }))
            return []

    results_by_source = await asyncio.gather(
        *[search_source(s) for s in sources],
        return_exceptions=True
    )

    for source_results in results_by_source:
        if isinstance(source_results, list):
            all_sightings.extend(source_results)

    # Sync leads from sightings
    # Note: implementer must convert raw result dicts to Sighting objects
    # following the existing pattern in search_service.py
    if all_sightings:
        sync_leads_for_sightings(db, req, all_sightings)

    await broker.publish(channel, "search-complete", json.dumps({
        "total": len(all_sightings),
        "requirement_id": requirement_id
    }))

    # Return redirect header to sourcing results
    return HTMLResponse(
        status_code=200,
        headers={"HX-Redirect": f"/v2/sourcing/{requirement_id}"}
    )
```

Note: The actual implementation must adapt `search_single_source` from the existing `search_service.py` pattern. The above is a structural guide -- the implementer must verify the exact search service API and sighting creation flow.

- [x] **Step 3: Create `search_progress.html` partial**

File: `app/templates/partials/sourcing/search_progress.html`

This partial renders inline in the requisition detail view when "Search" is clicked. It connects to the SSE stream and updates per-source status rows.

Template structure:
```
<div id="sourcing-progress-{{ requirement_id }}"
     hx-ext="sse"
     sse-connect="/v2/partials/sourcing/{{ requirement_id }}/stream"
     class="bg-white rounded-lg border border-brand-200 p-4 space-y-4">

  <div class="flex items-center justify-between">
    <h3 class="text-sm font-semibold text-gray-900">Searching sources...</h3>
    <span class="text-xs text-gray-500" id="progress-count-{{ requirement_id }}">0 / 6 complete</span>
  </div>

  <!-- Overall progress bar -->
  <div class="w-full bg-gray-200 rounded-full h-2">
    <div id="progress-bar-{{ requirement_id }}"
         class="bg-brand-500 h-2 rounded-full transition-all duration-300"
         style="width: 0%"></div>
  </div>

  <!-- Per-source status rows -->
  <div class="divide-y divide-gray-100" id="source-rows-{{ requirement_id }}">
    {% for source in ['BrokerBin', 'Nexar', 'DigiKey', 'Mouser', 'OEMSecrets', 'Element14'] %}
    <div class="flex items-center justify-between py-2" id="source-row-{{ source|lower }}-{{ requirement_id }}">
      <div class="flex items-center gap-2">
        {% with source_type=source|lower %}
          {% include "partials/shared/source_badge.html" %}
        {% endwith %}
      </div>
      <div class="flex items-center gap-3">
        <span class="text-xs text-gray-400" id="source-count-{{ source|lower }}-{{ requirement_id }}">--</span>
        <span class="text-xs text-gray-400" id="source-time-{{ source|lower }}-{{ requirement_id }}">--</span>
        <!-- Status: searching spinner (default) -->
        <span id="source-status-{{ source|lower }}-{{ requirement_id }}">
          <svg class="w-4 h-4 animate-spin text-brand-400" fill="none" viewBox="0 0 24 24">
            <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"/>
            <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"/>
          </svg>
        </span>
      </div>
    </div>
    {% endfor %}
  </div>
</div>

<!-- Fallback: if SSE fails, degrade to simple spinner -->
<noscript>
<div class="flex items-center gap-3 py-8 justify-center text-gray-500">
  <svg class="w-5 h-5 animate-spin" fill="none" viewBox="0 0 24 24">
    <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"/>
    <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"/>
  </svg>
  <span>Searching 6 sources...</span>
</div>
</noscript>
```

- [x] **Step 4: Add Alpine.js `sourcingProgress` component**

File: `app/static/htmx_app.js` (append to existing)

Add an Alpine data component that listens for SSE events from the HTMX SSE extension and updates the progress UI. The component:
- Tracks `completed` count
- On `source-complete` events: updates the count, elapsed time, and status icon for that source; updates the overall progress bar percentage
- On `search-complete` events: waits 500ms then uses `htmx.ajax()` to load the full sourcing results into `#main-content`
- Status icons: done = emerald checkmark SVG, failed = rose X SVG (set via `textContent` on the status span, using pre-rendered SVG template elements rather than string HTML)

```js
Alpine.data('sourcingProgress', (requirementId, totalSources) => ({
    completed: 0,
    init() {
        document.body.addEventListener('htmx:sseMessage', (evt) => {
            if (evt.detail.type === 'source-complete') {
                this.handleSourceComplete(JSON.parse(evt.detail.data));
            }
            if (evt.detail.type === 'search-complete') {
                this.handleSearchComplete(JSON.parse(evt.detail.data));
            }
        });
    },
    handleSourceComplete(data) {
        this.completed++;
        const source = data.source.toLowerCase();
        // Update count text
        const countEl = document.getElementById('source-count-' + source + '-' + requirementId);
        if (countEl) countEl.textContent = data.count + ' results';
        // Update elapsed time text
        const timeEl = document.getElementById('source-time-' + source + '-' + requirementId);
        if (timeEl) timeEl.textContent = (data.elapsed_ms / 1000).toFixed(1) + 's';
        // Update status icon -- clone from template SVGs
        const statusEl = document.getElementById('source-status-' + source + '-' + requirementId);
        if (statusEl) {
            const tplId = data.status === 'done' ? 'tpl-icon-check' : 'tpl-icon-fail';
            const tpl = document.getElementById(tplId);
            if (tpl) {
                statusEl.replaceChildren(tpl.content.cloneNode(true));
            }
        }
        // Update progress bar width
        const pct = Math.round((this.completed / totalSources) * 100);
        const bar = document.getElementById('progress-bar-' + requirementId);
        if (bar) bar.style.width = pct + '%';
        const counter = document.getElementById('progress-count-' + requirementId);
        if (counter) counter.textContent = this.completed + ' / ' + totalSources + ' complete';
    },
    handleSearchComplete(data) {
        setTimeout(function() {
            htmx.ajax('GET', '/v2/partials/sourcing/' + requirementId, '#main-content');
        }, 500);
    }
}));
```

Also add hidden `<template>` elements in `base.html` for the icon SVGs:
```html
<template id="tpl-icon-check">
  <svg class="w-4 h-4 text-emerald-500" fill="none" viewBox="0 0 24 24" stroke="currentColor">
    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"/>
  </svg>
</template>
<template id="tpl-icon-fail">
  <svg class="w-4 h-4 text-rose-500" fill="none" viewBox="0 0 24 24" stroke="currentColor">
    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/>
  </svg>
</template>
```

- [x] **Step 5: Install `sse-starlette` dependency**

Add to `requirements.txt`:
```
sse-starlette>=1.6.0
```

Verify the SSE extension for HTMX is loaded. In `htmx_app.js`:
```js
import 'htmx.org/dist/ext/sse.js';
```

---

## Task 4: Sourcing Results View — Leads with Filters

**Files:**
- Create: `app/templates/partials/sourcing/results.html`
- Create: `app/templates/partials/sourcing/lead_card.html`
- Modify: `app/routers/htmx_views.py` (add sourcing results routes)

### Context

The sourcing results page shows lead cards for a specific requirement, with a rich filter bar. Entry point: requisition detail "Search" on a requirement row. After SSE search completes, the user lands on `/v2/sourcing/{requirement_id}`. Leads are `SourcingLead` records filtered/sorted by query params. Sighting data (qty, price) is joined via `requirement_id` + `vendor_name_normalized`.

### Steps

- [x] **Step 1: Add full page + partial routes for sourcing results**

File: `app/routers/htmx_views.py`

```python
# Full page entry point
@router.get("/v2/sourcing/{requirement_id}", response_class=HTMLResponse)
async def v2_sourcing_page(request: Request, requirement_id: int, db: Session = Depends(get_db)):
    """Full page load for sourcing results."""
    user = get_user(request, db)
    if not user:
        return templates.TemplateResponse("htmx/login.html", {"request": request})
    ctx = _base_ctx(request, user, "requisitions")
    ctx["partial_url"] = f"/v2/partials/sourcing/{requirement_id}"
    return templates.TemplateResponse("htmx/base_page.html", ctx)

# Partial endpoint with full filter support
@router.get("/v2/partials/sourcing/{requirement_id}", response_class=HTMLResponse)
async def sourcing_results_partial(
    request: Request,
    requirement_id: int,
    confidence: str = "",       # "high", "medium", "low" (comma-separated)
    safety: str = "",           # "low_risk", "medium_risk", "high_risk"
    freshness: str = "",        # "24h", "7d", "30d", "all"
    source: str = "",           # comma-separated source types
    status: str = "",           # "new", "contacted", "has_stock", "bad_lead"
    contactability: str = "",   # "has_email", "has_phone", "any"
    corroborated: str = "",     # "yes", "no", "all"
    sort: str = "best",         # "best", "freshest", "safest", "contact", "proven"
    page: int = Query(1, ge=1),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return sourcing results with lead cards for a requirement.

    Supports filtering by confidence band, safety band, freshness window,
    source type, buyer status, contactability, and corroboration. Sorts by
    best overall (default), freshest, safest, easiest to contact, or most proven.
    """
    from ..models.sourcing_lead import SourcingLead

    req = db.query(Requirement).filter(Requirement.id == requirement_id).first()
    if not req:
        raise HTTPException(404, "Requirement not found")

    # Base query
    query = db.query(SourcingLead).filter(SourcingLead.requirement_id == requirement_id)

    # Apply filters
    if confidence:
        bands = [b.strip() for b in confidence.split(",")]
        query = query.filter(SourcingLead.confidence_band.in_(bands))
    if safety:
        bands = [b.strip() for b in safety.split(",")]
        query = query.filter(SourcingLead.vendor_safety_band.in_(bands))
    if freshness and freshness != "all":
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        cutoffs = {"24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30)}
        if freshness in cutoffs:
            query = query.filter(SourcingLead.source_last_seen_at >= now - cutoffs[freshness])
    if source:
        sources = [s.strip() for s in source.split(",")]
        query = query.filter(SourcingLead.primary_source_type.in_(sources))
    if status and status != "all":
        statuses = [s.strip() for s in status.split(",")]
        query = query.filter(SourcingLead.buyer_status.in_(statuses))
    if contactability == "has_email":
        query = query.filter(SourcingLead.contact_email.isnot(None))
    elif contactability == "has_phone":
        query = query.filter(SourcingLead.contact_phone.isnot(None))
    if corroborated == "yes":
        query = query.filter(SourcingLead.corroborated.is_(True))
    elif corroborated == "no":
        query = query.filter(SourcingLead.corroborated.is_(False))

    # Apply sort
    sort_map = {
        "best": [SourcingLead.confidence_score.desc()],
        "freshest": [SourcingLead.source_last_seen_at.desc().nullslast()],
        "safest": [SourcingLead.vendor_safety_score.desc().nullslast()],
        "contact": [SourcingLead.contactability_score.desc().nullslast()],
        "proven": [SourcingLead.historical_success_score.desc().nullslast()],
    }
    for col in sort_map.get(sort, sort_map["best"]):
        query = query.order_by(col)

    total = query.count()
    per_page = 24
    leads = query.offset((page - 1) * per_page).limit(per_page).all()

    # Fetch best sighting data per lead (qty + price from most recent Sighting)
    lead_sighting_data = {}
    if leads:
        for lead in leads:
            best_sighting = (
                db.query(Sighting)
                .filter(
                    Sighting.requirement_id == requirement_id,
                    Sighting.vendor_name_normalized == lead.vendor_name_normalized,
                )
                .order_by(Sighting.created_at.desc().nullslast())
                .first()
            )
            if best_sighting:
                lead_sighting_data[lead.id] = {
                    "qty_available": best_sighting.qty_available,
                    "unit_price": best_sighting.unit_price,
                }

    ctx = _base_ctx(request, user, "requisitions")
    ctx.update({
        "requirement": req,
        "leads": leads,
        "lead_sighting_data": lead_sighting_data,
        "total": total,
        "page": page,
        "total_pages": max(1, (total + per_page - 1) // per_page),
        "per_page": per_page,
        # Pass filter state back for maintaining active filter UI
        "f_confidence": confidence,
        "f_safety": safety,
        "f_freshness": freshness,
        "f_source": source,
        "f_status": status,
        "f_contactability": contactability,
        "f_corroborated": corroborated,
        "f_sort": sort,
    })
    return templates.TemplateResponse("partials/sourcing/results.html", ctx)
```

- [x] **Step 2: Create `results.html` template**

File: `app/templates/partials/sourcing/results.html`

Template structure:
```
<!-- OOB breadcrumb update -->
<div id="breadcrumb" hx-swap-oob="true">
  <a href="/v2/requisitions" hx-get="/v2/partials/requisitions" hx-target="#main-content" hx-push-url="true"
     class="text-brand-500 hover:text-brand-600">Requisitions</a>
  <span class="text-gray-400 mx-1">/</span>
  <a href="/v2/requisitions/{{ requirement.requisition_id }}"
     hx-get="/v2/partials/requisitions/{{ requirement.requisition_id }}"
     hx-target="#main-content" hx-push-url="true"
     class="text-brand-500 hover:text-brand-600">{{ requirement.requisition.name }}</a>
  <span class="text-gray-400 mx-1">/</span>
  <span class="text-gray-900">Sourcing: {{ requirement.primary_mpn }}</span>
</div>

<div class="space-y-6">
  <!-- Header -->
  <div class="flex items-center justify-between">
    <div>
      <h1 class="text-2xl font-bold text-gray-900">
        <span class="font-mono">{{ requirement.primary_mpn }}</span>
      </h1>
      <p class="text-sm text-gray-500 mt-1">
        {{ requirement.brand or '' }}
        {% if requirement.brand %}&middot;{% endif %}
        {{ total }} lead{{ 's' if total != 1 }} found
      </p>
    </div>
    <button hx-post="/v2/partials/sourcing/{{ requirement.id }}/search"
            hx-target="#main-content"
            class="px-4 py-2 bg-brand-500 text-white text-sm font-medium rounded-lg
                   hover:bg-brand-600 flex items-center gap-2">
      Re-search
    </button>
  </div>

  <!-- Filter bar -->
  <div id="sourcing-filters" class="bg-white rounded-lg border border-brand-200 p-4 space-y-3"
       x-data="{ showFilters: true }">
    <div class="flex items-center justify-between">
      <h3 class="text-xs font-semibold text-gray-500 uppercase tracking-wider">Filters</h3>
      <button @click="showFilters = !showFilters" class="text-xs text-brand-500">
        <span x-text="showFilters ? 'Hide' : 'Show'"></span>
      </button>
    </div>

    <div x-show="showFilters" class="space-y-3">
      <!-- Row 1: Confidence + Safety + Freshness -->
      <div class="flex flex-wrap gap-4">
        <div class="flex items-center gap-2">
          <span class="text-xs text-gray-500 font-medium">Confidence:</span>
          {% for band, label in [('high', 'High'), ('medium', 'Medium'), ('low', 'Low')] %}
          <a hx-get="/v2/partials/sourcing/{{ requirement.id }}?confidence={{ band }}&safety={{ f_safety }}&freshness={{ f_freshness }}&source={{ f_source }}&status={{ f_status }}&contactability={{ f_contactability }}&corroborated={{ f_corroborated }}&sort={{ f_sort }}"
             hx-target="#main-content" hx-push-url="true"
             class="px-2.5 py-1 text-xs rounded-full border cursor-pointer
               {{ 'bg-brand-500 text-white border-brand-500' if band == f_confidence else 'bg-white text-gray-600 border-gray-300 hover:border-brand-300' }}">
            {{ label }}
          </a>
          {% endfor %}
        </div>

        <div class="flex items-center gap-2">
          <span class="text-xs text-gray-500 font-medium">Safety:</span>
          {% for band, label in [('low_risk', 'Low Risk'), ('medium_risk', 'Medium'), ('high_risk', 'High Risk')] %}
          <a hx-get="/v2/partials/sourcing/{{ requirement.id }}?confidence={{ f_confidence }}&safety={{ band }}&freshness={{ f_freshness }}&source={{ f_source }}&status={{ f_status }}&contactability={{ f_contactability }}&corroborated={{ f_corroborated }}&sort={{ f_sort }}"
             hx-target="#main-content" hx-push-url="true"
             class="px-2.5 py-1 text-xs rounded-full border cursor-pointer
               {{ 'bg-brand-500 text-white border-brand-500' if band == f_safety else 'bg-white text-gray-600 border-gray-300 hover:border-brand-300' }}">
            {{ label }}
          </a>
          {% endfor %}
        </div>

        <div class="flex items-center gap-2">
          <span class="text-xs text-gray-500 font-medium">Freshness:</span>
          {% for val, label in [('24h', '24h'), ('7d', '7 days'), ('30d', '30 days'), ('all', 'All')] %}
          <a hx-get="/v2/partials/sourcing/{{ requirement.id }}?confidence={{ f_confidence }}&safety={{ f_safety }}&freshness={{ val }}&source={{ f_source }}&status={{ f_status }}&contactability={{ f_contactability }}&corroborated={{ f_corroborated }}&sort={{ f_sort }}"
             hx-target="#main-content" hx-push-url="true"
             class="px-2.5 py-1 text-xs rounded-full border cursor-pointer
               {{ 'bg-brand-500 text-white border-brand-500' if val == f_freshness else 'bg-white text-gray-600 border-gray-300 hover:border-brand-300' }}">
            {{ label }}
          </a>
          {% endfor %}
        </div>
      </div>

      <!-- Row 2: Source checkboxes -->
      <div class="flex flex-wrap gap-4">
        <div class="flex items-center gap-2">
          <span class="text-xs text-gray-500 font-medium">Source:</span>
          {% for src in ['brokerbin', 'nexar', 'digikey', 'mouser', 'oemsecrets', 'element14'] %}
          <label class="flex items-center gap-1 text-xs cursor-pointer">
            <input type="checkbox" name="source" value="{{ src }}"
                   {{ 'checked' if src in f_source }}
                   hx-get="/v2/partials/sourcing/{{ requirement.id }}"
                   hx-target="#main-content" hx-push-url="true"
                   hx-include="#sourcing-filters"
                   class="rounded border-gray-300 text-brand-500 focus:ring-brand-500">
            {% with source_type=src %}{% include "partials/shared/source_badge.html" %}{% endwith %}
          </label>
          {% endfor %}
        </div>
      </div>

      <!-- Row 3: Status + Contactability + Corroborated -->
      <div class="flex flex-wrap gap-4">
        <div class="flex items-center gap-2">
          <span class="text-xs text-gray-500 font-medium">Status:</span>
          {% for val, label in [('new', 'New'), ('contacted', 'Contacted'), ('has_stock', 'Has Stock'), ('bad_lead', 'Bad Lead'), ('', 'All')] %}
          <a hx-get="/v2/partials/sourcing/{{ requirement.id }}?confidence={{ f_confidence }}&safety={{ f_safety }}&freshness={{ f_freshness }}&source={{ f_source }}&status={{ val }}&contactability={{ f_contactability }}&corroborated={{ f_corroborated }}&sort={{ f_sort }}"
             hx-target="#main-content" hx-push-url="true"
             class="px-2.5 py-1 text-xs rounded-full border cursor-pointer
               {{ 'bg-brand-500 text-white border-brand-500' if val == f_status else 'bg-white text-gray-600 border-gray-300 hover:border-brand-300' }}">
            {{ label }}
          </a>
          {% endfor %}
        </div>

        <div class="flex items-center gap-2">
          <span class="text-xs text-gray-500 font-medium">Contact:</span>
          {% for val, label in [('has_email', 'Has Email'), ('has_phone', 'Has Phone'), ('', 'Any')] %}
          <a hx-get="/v2/partials/sourcing/{{ requirement.id }}?confidence={{ f_confidence }}&safety={{ f_safety }}&freshness={{ f_freshness }}&source={{ f_source }}&status={{ f_status }}&contactability={{ val }}&corroborated={{ f_corroborated }}&sort={{ f_sort }}"
             hx-target="#main-content" hx-push-url="true"
             class="px-2.5 py-1 text-xs rounded-full border cursor-pointer
               {{ 'bg-brand-500 text-white border-brand-500' if val == f_contactability else 'bg-white text-gray-600 border-gray-300 hover:border-brand-300' }}">
            {{ label }}
          </a>
          {% endfor %}
        </div>

        <div class="flex items-center gap-2">
          <span class="text-xs text-gray-500 font-medium">Corroborated:</span>
          {% for val, label in [('yes', 'Yes'), ('no', 'No'), ('', 'All')] %}
          <a hx-get="/v2/partials/sourcing/{{ requirement.id }}?confidence={{ f_confidence }}&safety={{ f_safety }}&freshness={{ f_freshness }}&source={{ f_source }}&status={{ f_status }}&contactability={{ f_contactability }}&corroborated={{ val }}&sort={{ f_sort }}"
             hx-target="#main-content" hx-push-url="true"
             class="px-2.5 py-1 text-xs rounded-full border cursor-pointer
               {{ 'bg-brand-500 text-white border-brand-500' if val == f_corroborated else 'bg-white text-gray-600 border-gray-300 hover:border-brand-300' }}">
            {{ label }}
          </a>
          {% endfor %}
        </div>
      </div>

      <!-- Sort dropdown -->
      <div class="flex items-center gap-2">
        <span class="text-xs text-gray-500 font-medium">Sort:</span>
        <select hx-get="/v2/partials/sourcing/{{ requirement.id }}"
                hx-target="#main-content" hx-push-url="true"
                hx-include="#sourcing-filters"
                name="sort"
                class="text-sm border border-gray-300 rounded px-2 py-1 focus:ring-brand-500 focus:border-brand-500">
          {% for val, label in [('best', 'Best Overall'), ('freshest', 'Freshest'), ('safest', 'Safest'), ('contact', 'Easiest to Contact'), ('proven', 'Most Proven')] %}
          <option value="{{ val }}" {{ 'selected' if f_sort == val }}>{{ label }}</option>
          {% endfor %}
        </select>
      </div>
    </div>
  </div>

  <!-- Lead cards grid -->
  {% if leads %}
  <div class="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4" id="lead-cards">
    {% for lead in leads %}
      {% include "partials/sourcing/lead_card.html" %}
    {% endfor %}
  </div>

  <!-- Pagination -->
  {% if total_pages > 1 %}
    {% with target_id="main-content", url="/v2/partials/sourcing/" ~ requirement.id %}
      {% include "partials/shared/pagination.html" %}
    {% endwith %}
  {% endif %}

  {% else %}
  <!-- Empty state -->
  <div class="flex flex-col items-center justify-center py-16 text-gray-400">
    <svg class="w-16 h-16" fill="none" viewBox="0 0 24 24" stroke="currentColor">
      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5"
            d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"/>
    </svg>
    <p class="mt-4 text-lg">No leads found for this part</p>
    <p class="text-sm mt-1">Try broadening your search or check back after the next sourcing run</p>
  </div>
  {% endif %}
</div>
```

- [x] **Step 3: Create `lead_card.html` template**

File: `app/templates/partials/sourcing/lead_card.html`

Each lead card displays rich info. Expects `lead` (SourcingLead) and `lead_sighting_data` (dict keyed by lead.id) in context.

Template structure:
```
{% set sighting = lead_sighting_data.get(lead.id, {}) %}
<div id="lead-card-{{ lead.id }}"
     class="bg-white rounded-lg border border-brand-200 hover:border-brand-400
            hover:shadow-md transition-all cursor-pointer"
     hx-get="/v2/partials/sourcing/leads/{{ lead.id }}"
     hx-target="#main-content" hx-push-url="/v2/sourcing/leads/{{ lead.id }}">

  <div class="p-4 space-y-3">
    <!-- Row 1: Vendor name + safety badge -->
    <div class="flex items-start justify-between">
      <div>
        <h3 class="font-semibold text-gray-900">{{ lead.vendor_name }}</h3>
        {% if lead.contact_url %}
        <p class="text-xs text-gray-400 truncate max-w-[200px]">{{ lead.contact_url }}</p>
        {% endif %}
      </div>
      {% set safety_badge = {
        'low_risk': 'bg-emerald-50 text-emerald-700',
        'medium_risk': 'bg-amber-50 text-amber-700',
        'high_risk': 'bg-rose-50 text-rose-700',
        'unknown': 'bg-gray-100 text-gray-600'
      } %}
      <span class="inline-flex px-2 py-0.5 text-xs font-medium rounded-full
                   {{ safety_badge.get(lead.vendor_safety_band, 'bg-gray-100 text-gray-600') }}">
        {{ lead.vendor_safety_band|replace('_', ' ')|title if lead.vendor_safety_band else 'Unknown' }}
      </span>
    </div>

    <!-- Row 2: Confidence score + progress bar -->
    <div>
      <div class="flex items-center justify-between mb-1">
        <span class="text-xs text-gray-500">Confidence</span>
        <span class="text-sm font-semibold
          {% if lead.confidence_score >= 70 %}text-emerald-700
          {% elif lead.confidence_score >= 40 %}text-amber-700
          {% else %}text-rose-700{% endif %}">
          {{ '%.0f'|format(lead.confidence_score) }}%
        </span>
      </div>
      <div class="w-full bg-gray-200 rounded-full h-1.5">
        <div class="h-1.5 rounded-full transition-all
          {% if lead.confidence_score >= 70 %}bg-emerald-500
          {% elif lead.confidence_score >= 40 %}bg-amber-500
          {% else %}bg-rose-500{% endif %}"
             style="width: {{ [lead.confidence_score, 100]|min }}%"></div>
      </div>
    </div>

    <!-- Row 3: Source badges -->
    <div class="flex flex-wrap gap-1">
      {% with source_type=lead.primary_source_type %}
        {% include "partials/shared/source_badge.html" %}
      {% endwith %}
    </div>

    <!-- Row 4: Qty + Price + Freshness -->
    <div class="flex items-center justify-between text-sm">
      <div class="flex gap-4">
        <span class="text-gray-600">
          {% if sighting.qty_available %}
            {{ '{:,}'.format(sighting.qty_available) }} avail
          {% else %}--{% endif %}
        </span>
        <span class="font-medium text-gray-900">
          {% if sighting.unit_price %}
            ${{ '%.4f'|format(sighting.unit_price) }}
          {% else %}RFQ{% endif %}
        </span>
      </div>
      <span class="text-xs text-gray-400">
        {% if lead.source_last_seen_at %}
          {{ lead.source_last_seen_at | timesince }}
        {% else %}--{% endif %}
      </span>
    </div>

    <!-- Row 5: Contact preview -->
    {% if lead.contact_email or lead.contact_phone %}
    <div class="text-xs text-gray-500 truncate">
      {{ lead.contact_email or lead.contact_phone }}
    </div>
    {% endif %}

    <!-- Row 6: Corroboration badge -->
    {% if lead.corroborated %}
    <div class="inline-flex items-center gap-1 px-2 py-0.5 text-xs font-medium
                rounded-full bg-emerald-50 text-emerald-700">
      <svg class="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor">
        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"/>
      </svg>
      Corroborated ({{ lead.evidence_count }} signals)
    </div>
    {% endif %}

    <!-- Row 7: Suggested next action -->
    {% if lead.suggested_next_action %}
    <p class="text-xs text-brand-500 font-medium">{{ lead.suggested_next_action }}</p>
    {% endif %}

    <!-- Row 8: Quick actions (stop propagation to prevent card click navigation) -->
    <div class="flex gap-2 pt-2 border-t border-gray-100" @click.stop>
      <button hx-post="/v2/partials/sourcing/leads/{{ lead.id }}/status"
              hx-vals='{"status": "contacted"}'
              hx-target="#lead-card-{{ lead.id }}"
              hx-swap="outerHTML"
              class="flex-1 px-3 py-1.5 text-xs font-medium text-brand-600
                     bg-brand-50 rounded hover:bg-brand-100">
        Claim
      </button>
      <button hx-post="/v2/partials/sourcing/leads/{{ lead.id }}/status"
              hx-vals='{"status": "bad_lead"}'
              hx-target="#lead-card-{{ lead.id }}"
              hx-swap="outerHTML"
              class="flex-1 px-3 py-1.5 text-xs font-medium text-gray-600
                     bg-gray-100 rounded hover:bg-gray-200">
        Dismiss
      </button>
      {% if lead.contact_email %}
      <button hx-get="/v2/partials/sourcing/leads/{{ lead.id }}?action=rfq"
              hx-target="#main-content"
              class="flex-1 px-3 py-1.5 text-xs font-medium text-emerald-600
                     bg-emerald-50 rounded hover:bg-emerald-100">
        Send RFQ
      </button>
      {% endif %}
    </div>
  </div>
</div>
```

- [x] **Step 4: Register `timesince` Jinja2 filter**

File: `app/routers/htmx_views.py` -- add after `templates = Jinja2Templates(...)`:

```python
def _timesince_filter(dt):
    """Convert datetime to human-readable relative time string."""
    if not dt:
        return ""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    diff = now - dt
    seconds = diff.total_seconds()
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        mins = int(seconds // 60)
        return f"{mins} min ago"
    if seconds < 86400:
        hours = int(seconds // 3600)
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = int(seconds // 86400)
    if days == 1:
        return "1 day ago"
    return f"{days} days ago"

templates.env.filters["timesince"] = _timesince_filter
```

---

## Task 5: Lead Detail View

**Files:**
- Create: `app/templates/partials/sourcing/lead_detail.html`
- Modify: `app/routers/htmx_views.py` (add lead detail routes + status/feedback endpoints)

### Context

Lead detail shows the full information for a single sourcing lead: summary card, evidence list, source attribution, contact info, safety review (reusable shared component from Task 2), and buyer actions panel. Lead status is lightweight buyer notes (not a workflow tracker).

### Steps

- [x] **Step 1: Add lead detail routes**

File: `app/routers/htmx_views.py`

```python
# Full page entry
@router.get("/v2/sourcing/leads/{lead_id}", response_class=HTMLResponse)
async def v2_lead_detail_page(request: Request, lead_id: int, db: Session = Depends(get_db)):
    user = get_user(request, db)
    if not user:
        return templates.TemplateResponse("htmx/login.html", {"request": request})
    ctx = _base_ctx(request, user, "requisitions")
    ctx["partial_url"] = f"/v2/partials/sourcing/leads/{lead_id}"
    return templates.TemplateResponse("htmx/base_page.html", ctx)

# Partial endpoint
@router.get("/v2/partials/sourcing/leads/{lead_id}", response_class=HTMLResponse)
async def lead_detail_partial(
    request: Request,
    lead_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return lead detail as HTML partial.

    Loads the SourcingLead, its evidence (sorted by confidence_impact desc),
    groups evidence by source category, fetches vendor card and best sighting.
    """
    from ..models.sourcing_lead import SourcingLead, LeadEvidence
    from ..services.sourcing_leads import _source_category

    lead = db.query(SourcingLead).filter(SourcingLead.id == lead_id).first()
    if not lead:
        raise HTTPException(404, "Lead not found")

    # Load evidence sorted by confidence_impact desc
    evidence = (
        db.query(LeadEvidence)
        .filter(LeadEvidence.lead_id == lead.id)
        .order_by(LeadEvidence.confidence_impact.desc().nullslast())
        .all()
    )

    # Group evidence by source category for attribution table
    evidence_by_category = {}
    for ev in evidence:
        cat = _source_category(ev.source_type)
        evidence_by_category.setdefault(cat, []).append(ev)

    category_labels = {
        "api": "API",
        "marketplace": "Marketplace",
        "salesforce_history": "Salesforce History",
        "avail_history": "Avail History",
        "web_ai": "Web / AI",
        "safety_review": "Safety Review",
        "buyer_feedback": "Buyer Feedback",
    }

    requirement = db.query(Requirement).filter(
        Requirement.id == lead.requirement_id
    ).first()

    vendor_card = lead.vendor_card

    best_sighting = (
        db.query(Sighting)
        .filter(
            Sighting.requirement_id == lead.requirement_id,
            Sighting.vendor_name_normalized == lead.vendor_name_normalized,
        )
        .order_by(Sighting.created_at.desc().nullslast())
        .first()
    )

    ctx = _base_ctx(request, user, "requisitions")
    ctx.update({
        "lead": lead,
        "evidence": evidence,
        "evidence_by_category": evidence_by_category,
        "category_labels": category_labels,
        "requirement": requirement,
        "vendor_card": vendor_card,
        "best_sighting": best_sighting,
    })
    return templates.TemplateResponse("partials/sourcing/lead_detail.html", ctx)
```

- [x] **Step 2: Add lead status update endpoint**

File: `app/routers/htmx_views.py`

```python
@router.post("/v2/partials/sourcing/leads/{lead_id}/status", response_class=HTMLResponse)
async def lead_status_update(
    request: Request,
    lead_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Update lead buyer status.

    Returns updated lead card when called from results view (for OOB swap),
    or updated lead detail when called from lead detail page.
    """
    from ..services.sourcing_leads import update_lead_status
    from ..models.sourcing_lead import SourcingLead

    form = await request.form()
    status = form.get("status", "").strip()
    note = form.get("note", "").strip() or None

    try:
        lead = update_lead_status(
            db, lead_id, status,
            note=note,
            actor_user_id=user.id,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))

    if not lead:
        raise HTTPException(404, "Lead not found")

    # Detect call context: from lead detail page or from results card
    referer = request.headers.get("HX-Current-URL", "")
    if "/leads/" in referer:
        return await lead_detail_partial(request, lead_id, user, db)

    # Return updated lead card for results view swap
    best_sighting = (
        db.query(Sighting)
        .filter(
            Sighting.requirement_id == lead.requirement_id,
            Sighting.vendor_name_normalized == lead.vendor_name_normalized,
        )
        .order_by(Sighting.created_at.desc().nullslast())
        .first()
    )
    lead_sighting_data = {}
    if best_sighting:
        lead_sighting_data[lead.id] = {
            "qty_available": best_sighting.qty_available,
            "unit_price": best_sighting.unit_price,
        }

    ctx = _base_ctx(request, user, "requisitions")
    ctx.update({"lead": lead, "lead_sighting_data": lead_sighting_data})
    return templates.TemplateResponse("partials/sourcing/lead_card.html", ctx)
```

- [x] **Step 3: Add lead feedback endpoint**

File: `app/routers/htmx_views.py`

```python
@router.post("/v2/partials/sourcing/leads/{lead_id}/feedback", response_class=HTMLResponse)
async def lead_feedback(
    request: Request,
    lead_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Add feedback event to a lead without changing status. Returns updated lead detail."""
    from ..services.sourcing_leads import append_lead_feedback

    form = await request.form()
    note = form.get("note", "").strip() or None
    reason_code = form.get("reason_code", "").strip() or None
    contact_method = form.get("contact_method", "").strip() or None

    lead = append_lead_feedback(
        db, lead_id,
        note=note,
        reason_code=reason_code,
        contact_method=contact_method,
        actor_user_id=user.id,
    )
    if not lead:
        raise HTTPException(404, "Lead not found")

    return await lead_detail_partial(request, lead_id, user, db)
```

- [x] **Step 4: Create `lead_detail.html` template**

File: `app/templates/partials/sourcing/lead_detail.html`

Template structure:
```
<!-- OOB breadcrumb -->
<div id="breadcrumb" hx-swap-oob="true">
  <a href="/v2/requisitions" hx-get="/v2/partials/requisitions" hx-target="#main-content"
     hx-push-url="true" class="text-brand-500 hover:text-brand-600">Requisitions</a>
  <span class="text-gray-400 mx-1">/</span>
  <a href="/v2/requisitions/{{ lead.requisition_id }}"
     hx-get="/v2/partials/requisitions/{{ lead.requisition_id }}"
     hx-target="#main-content" hx-push-url="true"
     class="text-brand-500 hover:text-brand-600">
    {{ requirement.requisition.name if requirement and requirement.requisition else '' }}
  </a>
  <span class="text-gray-400 mx-1">/</span>
  <a href="/v2/sourcing/{{ lead.requirement_id }}"
     hx-get="/v2/partials/sourcing/{{ lead.requirement_id }}"
     hx-target="#main-content" hx-push-url="true"
     class="text-brand-500 hover:text-brand-600">
    {{ requirement.primary_mpn if requirement else '' }}
  </a>
  <span class="text-gray-400 mx-1">/</span>
  <span class="text-gray-900">{{ lead.vendor_name }}</span>
</div>

<div class="space-y-6 max-w-4xl">

  <!-- ===== Lead Summary Card ===== -->
  <div class="bg-white rounded-lg border border-brand-200 p-6">
    <div class="flex items-start justify-between">
      <div>
        <h1 class="text-xl font-bold text-gray-900">{{ lead.vendor_name }}</h1>
        <p class="text-sm text-gray-500 mt-1">
          <span class="font-mono">{{ lead.part_number_matched }}</span>
          {% if lead.match_type != 'exact' %}
          <span class="text-xs text-amber-600 ml-1">({{ lead.match_type }} match)</span>
          {% endif %}
        </p>
      </div>
      <div class="flex items-center gap-3">
        <!-- Confidence score -->
        <div class="text-right">
          <span class="text-2xl font-bold
            {% if lead.confidence_score >= 70 %}text-emerald-600
            {% elif lead.confidence_score >= 40 %}text-amber-600
            {% else %}text-rose-600{% endif %}">
            {{ '%.0f'|format(lead.confidence_score) }}%
          </span>
          <p class="text-xs text-gray-400">confidence</p>
        </div>
        <!-- Safety badge -->
        {% set safety_badge = {
          'low_risk': 'bg-emerald-50 text-emerald-700',
          'medium_risk': 'bg-amber-50 text-amber-700',
          'high_risk': 'bg-rose-50 text-rose-700',
          'unknown': 'bg-gray-100 text-gray-600'
        } %}
        <span class="inline-flex px-2.5 py-0.5 text-xs font-medium rounded-full
                     {{ safety_badge.get(lead.vendor_safety_band, 'bg-gray-100 text-gray-600') }}">
          {{ lead.vendor_safety_band|replace('_', ' ')|title if lead.vendor_safety_band else 'Unknown' }}
        </span>
        <!-- Buyer status badge -->
        {% set status_colors = {
          'new': 'bg-brand-100 text-brand-600',
          'contacted': 'bg-amber-50 text-amber-700',
          'has_stock': 'bg-emerald-50 text-emerald-700',
          'no_stock': 'bg-gray-100 text-gray-600',
          'bad_lead': 'bg-rose-50 text-rose-700',
          'do_not_contact': 'bg-rose-50 text-rose-700'
        } %}
        <span class="inline-flex px-2.5 py-0.5 text-xs font-medium rounded-full
                     {{ status_colors.get(lead.buyer_status, 'bg-gray-100 text-gray-600') }}">
          {{ lead.buyer_status|replace('_', ' ')|title }}
        </span>
      </div>
    </div>

    <!-- Sighting data: qty + price + lead time -->
    {% if best_sighting %}
    <div class="flex gap-6 mt-4 pt-4 border-t border-gray-100">
      <div>
        <span class="text-xs text-gray-400">Qty Available</span>
        <p class="text-lg font-semibold text-gray-900">
          {{ '{:,}'.format(best_sighting.qty_available) if best_sighting.qty_available else '--' }}
        </p>
      </div>
      <div>
        <span class="text-xs text-gray-400">Unit Price</span>
        <p class="text-lg font-semibold text-gray-900">
          {% if best_sighting.unit_price %}${{ '%.4f'|format(best_sighting.unit_price) }}{% else %}RFQ{% endif %}
        </p>
      </div>
      <div>
        <span class="text-xs text-gray-400">Lead Time</span>
        <p class="text-lg font-semibold text-gray-900">
          {{ best_sighting.lead_time or (best_sighting.lead_time_days|string ~ ' days' if best_sighting.lead_time_days else '--') }}
        </p>
      </div>
    </div>
    {% endif %}
  </div>

  <!-- ===== Evidence List ===== -->
  <div class="bg-white rounded-lg border border-brand-200">
    <div class="px-4 py-3 border-b border-brand-200">
      <h2 class="text-sm font-semibold text-gray-900">Evidence ({{ evidence|length }})</h2>
    </div>
    {% if evidence %}
    <div class="divide-y divide-gray-100">
      {% for ev in evidence %}
      <div class="px-4 py-3 flex items-start gap-4">
        <!-- Signal type icon -->
        <div class="flex-shrink-0 w-8 h-8 rounded-full bg-brand-50 flex items-center justify-center">
          {% if ev.signal_type == 'stock_listing' %}
          <svg class="w-4 h-4 text-brand-500" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2"
                  d="M20 7l-8-4-8 4m16 0l-8 4m8-4v10l-8 4m0-10L4 7m8 4v10M4 7v10l8 4"/>
          </svg>
          {% elif ev.signal_type == 'vendor_history' %}
          <svg class="w-4 h-4 text-brand-500" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2"
                  d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z"/>
          </svg>
          {% elif ev.signal_type == 'web_discovery' %}
          <svg class="w-4 h-4 text-brand-500" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2"
                  d="M21 12a9 9 0 01-9 9m9-9a9 9 0 00-9-9m9 9H3m9 9a9 9 0 01-9-9m9 9c1.657 0 3-4.03 3-9s-1.343-9-3-9m0 18c-1.657 0-3-4.03-3-9s1.343-9 3-9m-9 9a9 9 0 019-9"/>
          </svg>
          {% elif ev.signal_type == 'email_signal' %}
          <svg class="w-4 h-4 text-brand-500" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2"
                  d="M3 8l7.89 5.26a2 2 0 002.22 0L21 8M5 19h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z"/>
          </svg>
          {% else %}
          <svg class="w-4 h-4 text-brand-500" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2"
                  d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"/>
          </svg>
          {% endif %}
        </div>
        <div class="flex-1 min-w-0">
          <div class="flex items-center gap-2">
            {% with source_type=ev.source_type %}
              {% include "partials/shared/source_badge.html" %}
            {% endwith %}
            <span class="text-xs text-gray-400">{{ ev.signal_type|replace('_', ' ')|title }}</span>
          </div>
          <p class="text-sm text-gray-700 mt-1">{{ ev.explanation }}</p>
          <div class="flex items-center gap-4 mt-1">
            <span class="text-xs text-gray-400">
              {% if ev.observed_at %}{{ ev.observed_at | timesince }}{% endif %}
            </span>
            {% if ev.confidence_impact %}
            <span class="text-xs font-medium
              {% if ev.confidence_impact > 0 %}text-emerald-600{% else %}text-rose-600{% endif %}">
              {{ '+' if ev.confidence_impact > 0 }}{{ '%.1f'|format(ev.confidence_impact) }}%
            </span>
            {% endif %}
            {% set verify_colors = {
              'raw': 'bg-gray-100 text-gray-600',
              'inferred': 'bg-brand-100 text-brand-600',
              'buyer_confirmed': 'bg-emerald-50 text-emerald-700',
              'rejected': 'bg-rose-50 text-rose-700'
            } %}
            <span class="inline-flex px-1.5 py-0.5 text-[10px] font-medium rounded
                         {{ verify_colors.get(ev.verification_state, 'bg-gray-100 text-gray-600') }}">
              {{ ev.verification_state|replace('_', ' ')|title }}
            </span>
          </div>
        </div>
      </div>
      {% endfor %}
    </div>
    {% else %}
    <div class="px-4 py-8 text-center text-sm text-gray-400">No evidence records.</div>
    {% endif %}
  </div>

  <!-- ===== Source Attribution Table ===== -->
  <div class="bg-white rounded-lg border border-brand-200">
    <div class="px-4 py-3 border-b border-brand-200">
      <h2 class="text-sm font-semibold text-gray-900">Source Attribution</h2>
    </div>
    <div class="divide-y divide-gray-100">
      {% for category, items in evidence_by_category.items() %}
      <div class="px-4 py-3">
        <h3 class="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-2">
          {{ category_labels.get(category, category|title) }}
        </h3>
        <div class="flex flex-wrap gap-2">
          {% for ev in items %}
          <div class="inline-flex items-center gap-1.5 px-2 py-1 bg-gray-50 rounded text-xs">
            {% with source_type=ev.source_type %}
              {% include "partials/shared/source_badge.html" %}
            {% endwith %}
            <span class="text-gray-500">{{ ev.signal_type|replace('_', ' ') }}</span>
          </div>
          {% endfor %}
        </div>
      </div>
      {% endfor %}
    </div>
  </div>

  <!-- ===== Contact Information ===== -->
  <div class="bg-white rounded-lg border border-brand-200 p-4">
    <h2 class="text-sm font-semibold text-gray-900 mb-3">Contact Information</h2>
    <div class="grid grid-cols-1 sm:grid-cols-3 gap-4">
      <div>
        <span class="text-xs text-gray-400">Email</span>
        {% if lead.contact_email %}
        <p class="text-sm text-gray-900">{{ lead.contact_email }}</p>
        {% elif vendor_card and vendor_card.emails %}
        {% for email in vendor_card.emails[:3] %}
        <p class="text-sm text-gray-900">{{ email }}</p>
        {% endfor %}
        {% else %}
        <p class="text-sm text-gray-400">Not available</p>
        {% endif %}
      </div>
      <div>
        <span class="text-xs text-gray-400">Phone</span>
        {% if lead.contact_phone %}
        <p class="text-sm text-gray-900">{{ lead.contact_phone }}</p>
        {% elif vendor_card and vendor_card.phones %}
        {% for phone in vendor_card.phones[:3] %}
        <p class="text-sm text-gray-900">{{ phone }}</p>
        {% endfor %}
        {% else %}
        <p class="text-sm text-gray-400">Not available</p>
        {% endif %}
      </div>
      <div>
        <span class="text-xs text-gray-400">Website</span>
        {% if lead.contact_url %}
        <a href="{{ lead.contact_url }}" target="_blank" rel="noopener"
           class="text-sm text-brand-500 hover:text-brand-600 break-all">{{ lead.contact_url }}</a>
        {% elif vendor_card and vendor_card.website %}
        <a href="{{ vendor_card.website }}" target="_blank" rel="noopener"
           class="text-sm text-brand-500 hover:text-brand-600 break-all">{{ vendor_card.website }}</a>
        {% else %}
        <p class="text-sm text-gray-400">Not available</p>
        {% endif %}
      </div>
    </div>
  </div>

  <!-- ===== Safety Review Block (shared component) ===== -->
  {% with safety_band=lead.vendor_safety_band,
          safety_score=lead.vendor_safety_score,
          safety_summary=lead.vendor_safety_summary,
          safety_flags=lead.vendor_safety_flags %}
    {% include "partials/shared/safety_review.html" %}
  {% endwith %}

  <!-- ===== Buyer Actions Panel ===== -->
  <div class="bg-white rounded-lg border border-brand-200 p-4 space-y-4">
    <h2 class="text-sm font-semibold text-gray-900">Buyer Actions</h2>

    <!-- Status update form -->
    <form hx-post="/v2/partials/sourcing/leads/{{ lead.id }}/status"
          hx-target="#main-content"
          class="space-y-3">
      <div class="flex gap-3">
        <div class="flex-1">
          <label class="text-xs text-gray-500">Status</label>
          <select name="status"
                  class="w-full mt-1 text-sm border border-gray-300 rounded-lg px-3 py-2
                         focus:ring-brand-500 focus:border-brand-500">
            {% for val, label in [
              ('new', 'New'),
              ('contacted', 'Contacted'),
              ('has_stock', 'Has Stock'),
              ('no_stock', 'No Stock'),
              ('bad_lead', 'Bad Lead'),
              ('do_not_contact', 'Do Not Contact')
            ] %}
            <option value="{{ val }}" {{ 'selected' if lead.buyer_status == val }}>{{ label }}</option>
            {% endfor %}
          </select>
        </div>
        <div class="flex-1">
          <label class="text-xs text-gray-500">Note (optional)</label>
          <input type="text" name="note" placeholder="Add a note..."
                 class="w-full mt-1 text-sm border border-gray-300 rounded-lg px-3 py-2
                        focus:ring-brand-500 focus:border-brand-500">
        </div>
      </div>
      <div class="flex gap-2">
        <button type="submit"
                class="px-4 py-2 bg-brand-500 text-white text-sm font-medium rounded-lg
                       hover:bg-brand-600">
          Update Status
        </button>
        {% if lead.contact_email %}
        <button type="button"
                class="px-4 py-2 bg-emerald-500 text-white text-sm font-medium rounded-lg
                       hover:bg-emerald-600">
          Send RFQ
        </button>
        {% endif %}
        <button type="button"
                hx-post="/v2/partials/sourcing/leads/{{ lead.id }}/status"
                hx-vals='{"status": "contacted"}'
                hx-target="#main-content"
                class="px-4 py-2 bg-brand-50 text-brand-600 text-sm font-medium rounded-lg
                       hover:bg-brand-100 border border-brand-200">
          Claim
        </button>
        <button type="button"
                hx-post="/v2/partials/sourcing/leads/{{ lead.id }}/status"
                hx-vals='{"status": "bad_lead"}'
                hx-target="#main-content"
                class="px-4 py-2 bg-gray-100 text-gray-600 text-sm font-medium rounded-lg
                       hover:bg-gray-200 border border-gray-200">
          Dismiss
        </button>
      </div>
    </form>

    <!-- Feedback history -->
    {% if lead.feedback_events %}
    <div class="mt-4 pt-4 border-t border-gray-100">
      <h3 class="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-2">History</h3>
      <div class="space-y-2">
        {% for event in lead.feedback_events|sort(attribute='created_at', reverse=True) %}
        <div class="flex items-center gap-3 text-xs text-gray-500">
          <span>{{ event.created_at | timesince }}</span>
          <span class="font-medium text-gray-700">{{ event.status|replace('_', ' ')|title }}</span>
          {% if event.note %}
          <span class="text-gray-400">-- {{ event.note }}</span>
          {% endif %}
        </div>
        {% endfor %}
      </div>
    </div>
    {% endif %}
  </div>

</div>
```

- [x] **Step 5: Verify lead detail**

Test manually:
1. Navigate to sourcing results for a requirement with leads
2. Click a lead card -- lead detail loads with breadcrumb trail
3. Verify evidence list sorted by confidence_impact descending
4. Verify source badges on each evidence item
5. Verify verification state badges (raw=gray, inferred=brand, buyer_confirmed=emerald, rejected=rose)
6. Verify source attribution table groups by category
7. Verify contact info shows lead contact or falls back to vendor card
8. Verify safety review block renders with correct band color, signals, and recommendation
9. Change status via dropdown -- detail refreshes with updated status badge
10. Click "Claim" -- status changes to "contacted"
11. Click "Dismiss" -- status changes to "bad_lead"
12. Verify feedback history shows after status changes

---

## Task 6: Wire Up Requisition Detail "Search" Button

**Files:**
- Modify: `app/templates/partials/requisitions/detail.html` (or equivalent parts tab template)

### Context

The "Search" button on each requirement row in the requisition detail Parts tab needs to trigger the sourcing flow. Currently it posts to `/v2/partials/search/run`. It should instead navigate to the sourcing results page for that requirement.

### Steps

- [x] **Step 1: Update "Search" button on requirement rows**

In the requisition detail Parts tab, change each requirement row's "Search" button.

From:
```html
<button hx-post="/v2/partials/search/run?requirement_id={r.id}&mpn={r.primary_mpn}"
        hx-target="#sightings-{r.id}" ...>Search</button>
```

To:
```html
<a hx-get="/v2/partials/sourcing/{{ r.id }}"
   hx-target="#main-content"
   hx-push-url="/v2/sourcing/{{ r.id }}"
   class="text-xs text-brand-500 hover:text-brand-600 font-medium cursor-pointer">
  Search
</a>
```

This navigates to the full sourcing results view for that requirement, which shows existing leads. The "Re-search" button on the sourcing results page triggers a new multi-source search with SSE progress.

- [x] **Step 2: Verify wiring**

1. Open a requisition detail with requirements
2. Click "Search" on a requirement row
3. URL changes to `/v2/sourcing/{requirement_id}`
4. Sourcing results page loads (existing leads or empty state)
5. Click "Re-search" on sourcing results to trigger new multi-source search
6. Back button returns to requisition detail

---

## Task 7: Tests

**Files:**
- Create: `tests/test_htmx_sourcing.py`

### Steps

- [x] **Step 1: Create test fixtures**

```python
import pytest
from app.models.sourcing import Requisition, Requirement, Sighting
from app.models.sourcing_lead import SourcingLead, LeadEvidence


@pytest.fixture
def sample_requisition_with_leads(db, sample_user):
    """Create a requisition with a requirement and sourcing leads for testing."""
    req = Requisition(name="Test Req", status="active", created_by=sample_user.id)
    db.add(req)
    db.flush()

    requirement = Requirement(
        requisition_id=req.id,
        primary_mpn="LM317T",
        target_qty=100,
        sourcing_status="open",
    )
    db.add(requirement)
    db.flush()

    # Create a sighting for qty/price data
    sighting = Sighting(
        requirement_id=requirement.id,
        vendor_name="Test Vendor",
        vendor_name_normalized="test_vendor",
        mpn_matched="LM317T",
        qty_available=5000,
        unit_price=0.5500,
        source_type="brokerbin",
    )
    db.add(sighting)
    db.flush()

    lead = SourcingLead(
        lead_id="ld_test_001",
        requirement_id=requirement.id,
        requisition_id=req.id,
        part_number_requested="LM317T",
        part_number_matched="LM317T",
        vendor_name="Test Vendor",
        vendor_name_normalized="test_vendor",
        primary_source_type="brokerbin",
        primary_source_name="Brokerbin",
        confidence_score=72.5,
        confidence_band="medium",
        vendor_safety_score=68.0,
        vendor_safety_band="medium_risk",
        vendor_safety_summary="Moderate caution.",
        vendor_safety_flags=["limited_business_footprint", "positive:contact_channels_present"],
        contact_email="sales@testvendor.com",
        buyer_status="new",
        evidence_count=1,
        corroborated=False,
        reason_summary="Test lead",
    )
    db.add(lead)
    db.flush()

    evidence = LeadEvidence(
        evidence_id="ev_test_001",
        lead_id=lead.id,
        signal_type="stock_listing",
        source_type="brokerbin",
        source_name="Brokerbin",
        explanation="BrokerBin stock listing for Test Vendor",
        confidence_impact=14.4,
        verification_state="raw",
    )
    db.add(evidence)
    db.commit()

    return requirement


@pytest.fixture
def sample_lead(db, sample_requisition_with_leads):
    """Return the first lead for the sample requirement."""
    return db.query(SourcingLead).filter(
        SourcingLead.requirement_id == sample_requisition_with_leads.id
    ).first()
```

- [x] **Step 2: Test Part Search form partial**

```python
def test_search_form_partial(client, auth_headers):
    """GET /v2/partials/search returns search form HTML."""
    resp = client.get("/v2/partials/search", headers=auth_headers)
    assert resp.status_code == 200
    assert "Search All Sources" in resp.text
    assert 'name="mpn"' in resp.text
```

- [x] **Step 3: Test Part Search results partial**

```python
def test_search_run_returns_results(client, auth_headers, db, mocker):
    """POST /v2/partials/search/run returns results table."""
    mocker.patch("app.search_service.quick_search_mpn", return_value=[
        {"vendor_name": "Acme", "mpn_matched": "LM317T", "manufacturer": "TI",
         "qty_available": 1000, "unit_price": 0.55, "source_type": "brokerbin",
         "lead_time": "Stock"}
    ])
    resp = client.post(
        "/v2/partials/search/run",
        data={"mpn": "LM317T"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert "Acme" in resp.text
    assert "LM317T" in resp.text
    assert "BrokerBin" in resp.text
    assert "$0.5500" in resp.text
```

- [x] **Step 4: Test sourcing results partial**

```python
def test_sourcing_results_partial(client, auth_headers, db, sample_requisition_with_leads):
    """GET /v2/partials/sourcing/{req_id} returns lead cards."""
    req_id = sample_requisition_with_leads.id
    resp = client.get(f"/v2/partials/sourcing/{req_id}", headers=auth_headers)
    assert resp.status_code == 200
    assert "lead-card-" in resp.text
    assert "Test Vendor" in resp.text

def test_sourcing_results_not_found(client, auth_headers):
    """GET /v2/partials/sourcing/99999 returns 404."""
    resp = client.get("/v2/partials/sourcing/99999", headers=auth_headers)
    assert resp.status_code == 404
```

- [x] **Step 5: Test sourcing results filtering**

```python
def test_sourcing_filter_confidence(client, auth_headers, db, sample_requisition_with_leads):
    """Confidence filter restricts leads by band."""
    req_id = sample_requisition_with_leads.id
    resp = client.get(f"/v2/partials/sourcing/{req_id}?confidence=high", headers=auth_headers)
    assert resp.status_code == 200
    # Lead has confidence_band="medium", so high filter should exclude it
    assert "lead-card-" not in resp.text

    resp = client.get(f"/v2/partials/sourcing/{req_id}?confidence=medium", headers=auth_headers)
    assert resp.status_code == 200
    assert "lead-card-" in resp.text

def test_sourcing_filter_safety(client, auth_headers, db, sample_requisition_with_leads):
    """Safety filter restricts leads by band."""
    req_id = sample_requisition_with_leads.id
    resp = client.get(f"/v2/partials/sourcing/{req_id}?safety=low_risk", headers=auth_headers)
    assert resp.status_code == 200
    assert "lead-card-" not in resp.text  # Lead is medium_risk

def test_sourcing_sort_options(client, auth_headers, db, sample_requisition_with_leads):
    """Sort options work without errors."""
    req_id = sample_requisition_with_leads.id
    for sort_val in ["best", "freshest", "safest", "contact", "proven"]:
        resp = client.get(f"/v2/partials/sourcing/{req_id}?sort={sort_val}", headers=auth_headers)
        assert resp.status_code == 200
```

- [x] **Step 6: Test lead detail partial**

```python
def test_lead_detail_partial(client, auth_headers, db, sample_lead):
    """GET /v2/partials/sourcing/leads/{id} returns lead detail."""
    resp = client.get(f"/v2/partials/sourcing/leads/{sample_lead.id}", headers=auth_headers)
    assert resp.status_code == 200
    assert sample_lead.vendor_name in resp.text
    assert "Evidence" in resp.text
    assert "Safety Review" in resp.text
    assert "Buyer Actions" in resp.text

def test_lead_detail_not_found(client, auth_headers):
    """GET /v2/partials/sourcing/leads/99999 returns 404."""
    resp = client.get("/v2/partials/sourcing/leads/99999", headers=auth_headers)
    assert resp.status_code == 404
```

- [x] **Step 7: Test lead status update**

```python
def test_lead_status_update(client, auth_headers, db, sample_lead):
    """POST status update changes buyer_status and creates feedback event."""
    resp = client.post(
        f"/v2/partials/sourcing/leads/{sample_lead.id}/status",
        data={"status": "contacted", "note": "Called vendor"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    db.refresh(sample_lead)
    assert sample_lead.buyer_status == "contacted"
    assert sample_lead.buyer_feedback_summary == "Called vendor"

def test_lead_status_invalid(client, auth_headers, db, sample_lead):
    """Invalid status returns 400."""
    resp = client.post(
        f"/v2/partials/sourcing/leads/{sample_lead.id}/status",
        data={"status": "invalid_status"},
        headers=auth_headers,
    )
    assert resp.status_code == 400

def test_lead_status_not_found(client, auth_headers):
    """Status update on nonexistent lead returns 404."""
    resp = client.post(
        "/v2/partials/sourcing/leads/99999/status",
        data={"status": "contacted"},
        headers=auth_headers,
    )
    assert resp.status_code == 404
```

- [x] **Step 8: Test lead feedback**

```python
def test_lead_feedback(client, auth_headers, db, sample_lead):
    """POST feedback adds event without changing status."""
    resp = client.post(
        f"/v2/partials/sourcing/leads/{sample_lead.id}/feedback",
        data={"note": "Vendor confirmed stock", "contact_method": "email"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    db.refresh(sample_lead)
    assert sample_lead.buyer_feedback_summary == "Vendor confirmed stock"

def test_lead_feedback_not_found(client, auth_headers):
    """Feedback on nonexistent lead returns 404."""
    resp = client.post(
        "/v2/partials/sourcing/leads/99999/feedback",
        data={"note": "test"},
        headers=auth_headers,
    )
    assert resp.status_code == 404
```

---

## Summary of All Files

### New Files (9)
| File | Description |
|------|-------------|
| `app/templates/partials/search/form.html` | Part Search form (large input + button) |
| `app/templates/partials/search/results.html` | Part Search results table with source badges |
| `app/templates/partials/shared/source_badge.html` | Reusable source connector badge (7 color mappings) |
| `app/templates/partials/shared/safety_review.html` | Reusable safety review block (band color + signals + recommendation) |
| `app/templates/partials/sourcing/results.html` | Sourcing results with filter bar + lead card grid |
| `app/templates/partials/sourcing/lead_card.html` | Individual lead card (confidence, safety, qty, price, actions) |
| `app/templates/partials/sourcing/lead_detail.html` | Lead detail (evidence list, attribution, contact, safety, buyer actions) |
| `app/templates/partials/sourcing/search_progress.html` | SSE streaming progress (per-source status rows + progress bar) |
| `tests/test_htmx_sourcing.py` | Tests for all new endpoints |

### Modified Files (2)
| File | Changes |
|------|---------|
| `app/routers/htmx_views.py` | Add 8 new routes: sourcing results (full + partial), lead detail (full + partial), SSE stream, search trigger, lead status update, lead feedback. Add `timesince` Jinja2 filter. |
| `app/static/htmx_app.js` | Add `sourcingProgress` Alpine component for SSE event handling. Add SSE extension import. |

### Dependencies
| Package | Purpose |
|---------|---------|
| `sse-starlette>=1.6.0` | EventSourceResponse for SSE streaming |
| `htmx.org/ext/sse.js` | HTMX SSE extension (client-side) |

### New Routes (8)
| Method | Path | Description |
|--------|------|-------------|
| GET | `/v2/sourcing/{requirement_id}` | Full page -- sourcing results |
| GET | `/v2/sourcing/leads/{lead_id}` | Full page -- lead detail |
| GET | `/v2/partials/sourcing/{requirement_id}` | Partial -- sourcing results with filters + sort |
| GET | `/v2/partials/sourcing/{requirement_id}/stream` | SSE -- search progress stream |
| POST | `/v2/partials/sourcing/{requirement_id}/search` | Trigger multi-source search |
| GET | `/v2/partials/sourcing/leads/{lead_id}` | Partial -- lead detail |
| POST | `/v2/partials/sourcing/leads/{lead_id}/status` | Update lead buyer status |
| POST | `/v2/partials/sourcing/leads/{lead_id}/feedback` | Add feedback event |
