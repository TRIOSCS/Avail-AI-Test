# Search Experience Redesign — Design Spec

**Sub-project:** 1 of 3 (Search Experience Redesign)
**Date:** 2026-03-18
**Status:** Draft

## Problem

The search tab is functional but raw. It waits for all 8 connectors before showing anything, displays flat lead cards with too many inline actions, and has no way to act on results (add to requisition, create RFQ). It needs to feel like a precision sourcing tool.

## Goals

1. **Streaming results** — show results as each connector completes, not after all finish
2. **Aggressive deduplication** — one card per vendor (best offer), expand for all offers
3. **Shortlist + batch actions** — select results, then add to requisition or create RFQ
4. **Polished visual design** — industrial precision aesthetic, not generic AI slop
5. **Clean hybrid UX** — minimal entry state, rich results state

## Non-Goals

- Search history / saved searches (Sub-project #2)
- Bulk MPN search (Sub-project #2)
- Export / comparison (Sub-project #2)
- Vendor shortlisting across searches (dropped)

## Design Direction

**Aesthetic: Industrial Precision.** Professional buyers searching part numbers dozens of times a day. Every pixel earns its place.

- **Typography:** DM Sans for labels/body, JetBrains Mono for part numbers, prices, and quantities
- **Color:** Existing brand palette as accent. Dark card surfaces (gray-900/gray-800) with high-contrast data. Source chips use existing color map (violet=Nexar, orange=DigiKey, teal=Mouser, etc.)
- **Motion:** Source chips animate from "searching..." to "found N" with pulse. Cards slide in with staggered animation-delay. Transition from empty→results feels alive.
- **Key differentiator:** The source progress chips — 8 chips representing the sourcing network, lighting up in real-time as connectors complete.

## Architecture

### 1. Entry State

The existing `search/form.html` with tightened design:
- Large monospace input for MPN
- "Search All Sources" button
- Clean empty state below

No changes to the form's HTMX behavior except: instead of `hx-post` returning full results HTML, the form triggers an SSE connection.

**How the SSE connection starts:**
- Form submits via `hx-post="/v2/partials/search/run"` (keep existing route)
- Response returns the **results shell** — the progress bar with 8 source chips + empty results container + SSE connection element
- The shell contains `<div hx-ext="sse" sse-connect="/v2/partials/search/stream?mpn={mpn}">` which opens the SSE stream
- The results container inside has `sse-swap="results"` with `hx-swap="beforeend"` to **append** new cards as they arrive
- Stream events populate the shell progressively

### 2. SSE Streaming — Reusing the Existing SSE Broker

The codebase already has `app/services/sse_broker.py` with a `publish()`/`subscribe()`/`listen()` pattern used by the sourcing search. We reuse this pattern rather than creating a divergent inline generator.

**Channel naming:** `search:{session_search_id}` where `session_search_id` is a UUID generated per search request.

**Two-route pattern (matching existing sourcing stream):**

1. `POST /v2/partials/search/run` — triggers the search, returns the results shell HTML, launches the search as a background task that publishes to the broker
2. `GET /v2/partials/search/stream` — SSE endpoint that subscribes to the broker channel and yields events

**Route 1 — search_run (modified existing route):**
```python
@router.post("/v2/partials/search/run", response_class=HTMLResponse)
async def search_run(request, mpn: str = Form(""), ...):
    search_id = str(uuid4())
    # Launch search in background task
    asyncio.create_task(_run_streaming_search(search_id, mpn, db))
    # Return the shell HTML with SSE connection to stream endpoint
    ctx = {"search_id": search_id, "mpn": mpn, "enabled_sources": get_enabled_sources(db)}
    return templates.TemplateResponse("htmx/partials/search/results_shell.html", ctx)
```

**Route 2 — search_stream (new):**
```python
@router.get("/v2/partials/search/stream")
async def search_stream(search_id: str, user = Depends(require_user)):
    async def event_generator():
        async for msg in sse_broker.listen(f"search:{search_id}"):
            yield msg
    return EventSourceResponse(event_generator())
```

**Background task — _run_streaming_search:**

This function reuses the shared connector infrastructure from `_fetch_fresh()` but yields results incrementally:

```python
async def _run_streaming_search(search_id: str, mpn: str, db: Session):
    channel = f"search:{search_id}"
    clean_mpn = normalize_mpn(mpn) or mpn.strip().upper()

    # Reuse _build_connectors() for credential loading, disabled source checks
    connectors, source_stats_map = _build_connectors(db)

    # Wrap each connector to return (name, results)
    async def _run_one(name: str, connector):
        start = time.time()
        try:
            results = await connector.search(clean_mpn)
            return name, results, None, time.time() - start
        except Exception as exc:
            return name, [], str(exc), time.time() - start

    # Create tasks and track mapping
    pending = {}
    for conn in connectors:
        task = asyncio.create_task(_run_one(conn.source_name, conn))
        pending[task] = conn.source_name

    all_results = []
    card_index = 0

    while pending:
        done, _ = await asyncio.wait(pending.keys(), return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            name = pending.pop(task)
            source_name, results, error, elapsed_ms = task.result()

            # Publish source status chip update (OOB swap)
            chip_html = render_source_chip(source_name, len(results), error)
            await sse_broker.publish(channel, "source-status", chip_html)

            if results and not error:
                # Score results
                scored = _score_raw_results(results, db)
                # Deduplicate against already-sent results
                new_cards, updated_cards = _incremental_dedup(scored, all_results)
                all_results.extend(scored)

                # Publish new vendor cards (appended via sse-swap="results" + hx-swap="beforeend")
                for card_data in new_cards:
                    card_html = render_vendor_card(card_data, card_index)
                    await sse_broker.publish(channel, "results", card_html)
                    card_index += 1

                # Publish updated vendor cards (OOB swap replaces existing cards)
                for card_data in updated_cards:
                    card_html = render_vendor_card_oob(card_data)
                    await sse_broker.publish(channel, "source-status", card_html)

    # Done — publish final stats
    stats_html = render_final_stats(all_results, elapsed_total)
    await sse_broker.publish(channel, "done", stats_html)
```

**Key detail:** `_build_connectors(db)` is a new helper extracted from `_fetch_fresh()` lines 574-637. It handles disabled source checks, credential loading, and connector instantiation. Both `_fetch_fresh()` (for existing batch search) and `_run_streaming_search()` (for new streaming search) call it. No duplication.

### 3. Source Progress Chips

A horizontal flex row of chips, one per enabled connector. Rendered in the results shell.

**States per chip:**
- `waiting` — gray, shows source name
- `searching` — pulsing animation, shows source name (initial state in shell)
- `found N` — colored (source-specific color), shows count
- `empty` — muted, shows "0"
- `error` — red tint, shows error icon

**Implementation:** Each chip has an `id` like `source-chip-nexar`. SSE `source-status` events include `hx-swap-oob="outerHTML:#source-chip-nexar"` to replace the chip HTML with its new state. No Alpine.js state tracking needed — pure HTMX OOB swaps.

**HTMX timeout note:** The existing `htmx.config.timeout = 15000` (15s) applies to AJAX requests, not SSE connections. The HTMX SSE extension manages its own connection lifecycle. Verified: SSE connections are exempt from the global timeout.

### 4. Aggressive Deduplication

**New function `_deduplicate_sightings_aggressive()`** — separate from the existing `_deduplicate_sightings()` to avoid breaking requisition search.

The existing function groups by `(vendor_name, mpn, price)` and is used by `search_requirement()` for requisition-based searches. It remains unchanged.

**New grouping key:** `(normalized_vendor_name, normalized_mpn)` — one entry per vendor+MPN combo.

**Merge rules:**
- Primary offer = highest scored offer in the group
- `sub_offers` list = all other offers in the group, sorted by price ascending
- `offer_count` = total number of offers from this vendor
- Sum quantities across all offers (existing behavior)
- Keep best confidence (existing behavior)
- Keep lowest MOQ (existing behavior)
- Collect all source types into `sources_found` set

**Incremental dedup during streaming (`_incremental_dedup`):**

When new results arrive from a connector, check each against already-sent vendor cards:
- **New vendor** → create a new card, return in `new_cards` list (appended to DOM)
- **Existing vendor** → merge offers into existing card data, return in `updated_cards` list (OOB swap replaces existing card by `id="vendor-card-{normalized_vendor_name}"`)

This means a vendor card may update mid-stream as more sources report in. The user sees the card appear with the first source, then the offer count badge updates as more sources find the same vendor.

**The vendor card shows:**
- Primary: best offer (price, qty, confidence, source)
- Badge: "3 offers" if sub_offers exist
- Expandable section: all offers in a compact table (price, qty, source, condition)

### 5. Vendor Cards (Results Display)

Each vendor gets one card. Card anatomy:

```
┌─────────────────────────────────────────────────┐
│ ☐  ARROW ELECTRONICS              High  Nexar   │
│     LM317T · Texas Instruments                   │
│                                                   │
│     $0.4500  │  12,450 avail  │  MOQ 1          │
│     Authorized · 3 offers from 2 sources         │
│                                                   │
│  [View Details]            [▼ See all offers]    │
├─────────────────────────────────────────────────┤
│  (expanded: sub-offers table)                    │
│  $0.4500  12,450  Nexar     New    Authorized    │
│  $0.4800   8,200  DigiKey   New    Authorized    │
│  $0.5100   3,000  Mouser    New    Authorized    │
└─────────────────────────────────────────────────┘
```

**Card elements:**
- Checkbox (left) — for shortlist selection
- Vendor name (bold, large)
- MPN + manufacturer (subtitle)
- Best price, total qty available, MOQ
- Confidence badge (High/Medium/Low with color)
- Source badge(s) for sources that found this vendor
- Authorization status
- Offer count + source count
- "View Details" → opens existing drawer
- "See all offers" → Alpine.js toggle expands sub-offers table

**Card ID scheme:** `id="vendor-card-{normalized_vendor_name}"` — enables OOB swap updates when later connectors find the same vendor.

**Animation:** Cards use CSS `@keyframes slideUp` with the `animation-delay` set server-side via `style="--i: {card_index}"` in the rendered HTML. The `card_index` is tracked in `_run_streaming_search` and incremented per new card.

### 6. Shortlist + Batch Actions

**Alpine.js store:**
```javascript
Alpine.store('shortlist', {
    items: [],    // array of {vendor_name, mpn, price, qty, source_type, ...}
    toggle(item) { ... },
    has(vendorMpnKey) { ... },
    clear() { ... },
    get count() { return this.items.length; }
})
```

**Sticky action bar:** When `$store.shortlist.count > 0`, a bar slides up from the bottom of the viewport:

```
┌─────────────────────────────────────────────────┐
│  3 vendors selected                              │
│              [Add to Requisition]  [Create RFQ]  │
│              [Clear]                              │
└─────────────────────────────────────────────────┘
```

**"Add to Requisition" flow:**
1. Click → modal with requisition picker (search existing or create new)
2. Modal lists recent requisitions with a search input (HTMX-powered)
3. User selects a requisition
4. Backend creates a Requirement row (if one for this MPN doesn't already exist on the selected requisition) and persists each selected result as a Sighting row attached to that Requirement
5. HTMX POST to `POST /v2/partials/search/add-to-requisition`

**Data model for "Add to Requisition":**
- **Requirement**: `primary_mpn` = searched MPN, `target_qty` = null (user can fill in later), `requisition_id` = selected requisition
- **Sighting rows**: One per selected vendor. Maps from search result dict → Sighting model:
  - `vendor_name` → `vendor_name`
  - `unit_price` → `unit_price`
  - `qty_available` → `qty_available`
  - `source_type` → `source_type`
  - `is_authorized` → `is_authorized`
  - `confidence` → `confidence`
  - `score` → `score`
  - `evidence_tier` → `evidence_tier`
  - `mpn_matched` → `mpn_matched`
  - `manufacturer` → `manufacturer`
  - All other fields: `currency`, `moq`, `lead_time`, `condition`, `date_code`, `packaging`, `vendor_email`, `vendor_phone`, `vendor_url`, `click_url`, `octopart_url`, `vendor_sku`
- If a Requirement for this MPN already exists on the requisition, sightings are added to the existing Requirement (no duplicate Requirement created)

**"Create RFQ" flow:**
1. Click → modal with RFQ form (pre-filled with MPN and selected vendor emails)
2. Uses existing `POST /v2/partials/requisitions/{req_id}/rfq-send` route (htmx_views.py:1718)
3. Requires a requisition context — if the user hasn't added to a requisition yet, prompt them to do so first, or auto-create a draft requisition

### 7. Filters

Keep the existing filter pattern but simplify:

- **Confidence:** All / High / Medium / Low (pill buttons)
- **Source:** All / per-source toggles (click a completed source chip to filter to that source)
- **Sort:** Best Overall / Cheapest / Most Stock (dropdown)

**Filter implementation:** Filters trigger an HTMX GET to a filter endpoint that reads from Redis cache (the search results are cached during `_run_streaming_search`), applies filters server-side, and returns re-rendered card HTML. This avoids re-running connectors and avoids the fragile Jinja2-in-Alpine `x-show` pattern.

**Cache key:** `search:{search_id}:results` — stores the full scored/deduped result set in Redis with 15-min TTL (matching existing search cache TTL).

### 8. Detail Drawer

Keep the existing `lead_detail.html` drawer. Changes:
- Tighten padding and typography to match new card design
- Add "Add to Shortlist" button in drawer
- Show all offers from this vendor in the drawer (not just the one clicked)

## Files Touched

### Modified:
1. `app/search_service.py` — extract `_build_connectors()` from `_fetch_fresh()`, add `_deduplicate_sightings_aggressive()`, add `_incremental_dedup()`, add `_run_streaming_search()`, add `_score_raw_results()` helper
2. `app/routers/htmx_views.py` — modify `search_run` to return shell + launch background task, add `search_stream` SSE route, add `add-to-requisition` route, add `search-filter` route
3. `app/templates/htmx/partials/search/form.html` — tighten design
4. `app/templates/htmx/partials/search/results.html` — full rewrite: new vendor cards, progress chips, shortlist checkboxes
5. `app/templates/htmx/partials/search/lead_detail.html` — tighten CSS, add shortlist button, show all vendor offers
6. `app/static/htmx_app.js` — add Alpine.store('shortlist') definition

### New:
7. `app/templates/htmx/partials/search/results_shell.html` — streaming container (progress chips + SSE connect + empty results div with `sse-swap="results"` and `hx-swap="beforeend"`)
8. `app/templates/htmx/partials/search/vendor_card.html` — single vendor card partial (rendered server-side for SSE events)
9. `app/templates/htmx/partials/search/shortlist_bar.html` — sticky action bar partial
10. `app/templates/htmx/partials/search/requisition_picker_modal.html` — modal for selecting a requisition

### Tests:
11. `tests/test_search_streaming.py` — SSE streaming endpoint tests, aggressive dedup tests, incremental dedup tests, add-to-requisition route tests, filter route tests

## Dependencies

- `sse-starlette` — check if already installed (it is, used by existing sourcing stream). If not, use raw `StreamingResponse` with manual SSE formatting: `f"event: {event}\ndata: {data}\n\n"`.

## Risk & Mitigations

| Risk | Mitigation |
|------|-----------|
| SSE connection drops mid-search | Include `retry: 3000` in SSE stream header. Results cached in Redis — reconnect reads from cache and resumes from last-sent index. |
| `_build_connectors` extraction breaks existing `_fetch_fresh` | Extract is mechanical — move lines 574-637 into helper, call from both places. Test with existing test suite. |
| Incremental dedup ordering produces flickering cards | OOB swap is atomic — card replaces in one frame. User sees card update, not flicker. Add CSS transition on card content for smooth updates. |
| `sse-starlette` not installed | Fallback: raw Starlette `StreamingResponse` with manual `text/event-stream` formatting (trivial, ~10 lines). |
| "Add to Requisition" creates orphan Requirements | Validate requisition exists and user has access. If creating a new Requirement, set status to "pending" so it's visible but not yet searched. |

## Success Criteria

1. Searching a part number shows source chips animating as connectors complete
2. Results stream in as vendor cards, grouped by vendor with best offer primary
3. Existing vendor cards update (offer count, sub-offers) as later connectors find the same vendor
4. User can expand a card to see all offers from that vendor
5. User can select multiple vendors and "Add to Requisition" or "Create RFQ"
6. Filters work against cached results without re-running connectors
7. The whole experience feels fast, precise, and professional
8. All existing search functionality preserved (requisition-based search uses original `_deduplicate_sightings` unchanged)
