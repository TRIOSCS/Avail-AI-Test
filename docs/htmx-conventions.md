# HTMX Conventions

## Rules for imperative htmx.ajax() calls

### DO: pass indicator explicitly
`htmx.ajax()` does not read `hx-indicator` from the target element.
Always pass it in the options object:

    htmx.ajax('POST', url, {
        target: '#foo',
        swap: 'innerHTML',
        indicator: '#foo-skeleton'
    });

See: `app/templates/htmx/partials/sightings/list.html:64` (selectReq → /refresh)
and `app/templates/htmx/partials/sightings/list.html:100` (SSE handler → /refresh).

### DO: add X-Rendered-Req-Id to responses that update context-sensitive panels
When a response targets a panel whose content depends on a selected item
(e.g. a detail pane), the server must echo a correlation header
(`X-Rendered-Req-Id`) so a `htmx:beforeSwap` guard can discard stale
responses that arrive out of order.

See: `app/routers/sightings.py:620` (sightings_detail sets the header on every
response — sightings_refresh inherits via `await sightings_detail(...)`)
and `app/static/htmx_app.js:178` (beforeSwap guard correlates by header).

### DO: clear the in-flight click flag on every htmx:afterRequest
When using a click-flight flag to suppress SSE refreshes during user-initiated
POSTs, the flag must be cleared on `htmx:afterRequest` — not just on swap
success. Otherwise an error, timeout, abort, or stale-reject leaves the flag
stuck-true and silently drops every subsequent SSE refresh.

See: `app/static/htmx_app.js:200` (afterRequest listener clearing
clickInFlight for the sightings-detail target regardless of outcome).

### Sightings click pattern (read-only row click + explicit refresh)

Sourcing on `/v2/sightings` is strictly user-initiated. There are three
distinct interactions:

- **Row click** → `GET /v2/partials/sightings/{id}/detail` only. Read-only.
  No connector calls. Paints the cached `VendorSightingSummary` panel in
  ~100ms. `selectReq` increments `$store.sightingSelection.clickPending`
  by **1**; the `htmx:afterRequest` listener decrements once on the
  `#sightings-detail` response so SSE suppression stays active until the
  GET completes.
- **Per-row search icon** (always visible) → `POST /v2/partials/sightings/{id}/refresh?source=user`.
- **Detail-panel "Search" button** (`m.search_button` macro) → same POST.

Both POSTs run `search_requirement`, which gates each MPN by a 48h
cooldown via `MaterialCard.last_searched_at`. MPNs inside the window
are skipped; their prior sightings are surfaced via the `material_card_id`
linkage on Sighting rows so cross-requirement visibility is preserved.
The response carries an `HX-Trigger` per-MPN toast summarizing
`{searched, cached}` counts (suppressed when `?source=sse`).

SSE-driven background refreshes also POST `/refresh?source=sse`; the
gate skips `broker.publish` and `HX-Trigger` toasts so the loop breaks
and background work stays silent.

All `/refresh` and `/detail` responses target `#sightings-detail` and
echo `X-Rendered-Req-Id`. The `htmx:beforeSwap` correlation guard in
`app/static/htmx_app.js` drops any swap whose header doesn't match the
currently-selected row, so clicking a different row mid-flight cannot
clobber the new panel with a stale response.

Historical note: prior to 2026-05-14, the row click also fired a
parallel POST `/refresh` (LEG B) so every click ran the full connector
pipeline. Sourcing is now strictly explicit — see
`docs/superpowers/specs/2026-05-14-search-button-only-sourcing-design.md`.

### DO NOT: publish SSE events from a handler that was itself triggered by an SSE event
If an SSE event fires a POST that publishes a new SSE event, and the client
re-listens for that event, you have a loop. Guard with `?source=sse` and
skip the publish on that code path. Type the param as `Literal["user","sse"]`
so FastAPI rejects unknown values with HTTP 422 — a plain `str` silently
falls back to the user-path branch on typos like `?source=SSE`.

See: `app/routers/sightings.py:671` (`if not is_sse:` gate around
`broker.publish`).

### DO NOT: surface background-triggered toasts to the user
Rate-guard toasts ("Already searched within X minutes") and refresh-failure
toasts ("Search refresh failed") are only appropriate when the user
explicitly clicked. SSE-triggered refreshes must suppress `HX-Trigger`
toast headers. Use `?source=sse` on the POST URL and check it server-side
before setting `HX-Trigger`.

See: `app/routers/sightings.py:655` (rate-guard toast gated on `not is_sse`)
and `app/routers/sightings.py:679` (refresh-failure toast gated on
`refresh_failed and not is_sse`).

### DO: apply the source gate to every mutation endpoint that calls broker.publish

The `?source=sse` gate is not specific to the refresh endpoint — it belongs on
every endpoint that calls `broker.publish("sighting-updated", ...)`. If any
endpoint omits the gate, an SSE-triggered call to that endpoint will re-publish,
which triggers another SSE, which calls the endpoint again.

Gated endpoints (as of this writing):
- `sightings_refresh` — `app/routers/sightings.py`
- `sightings_batch_refresh` — `app/routers/sightings.py`
- `sightings_mark_unavailable` — `app/routers/sightings.py`
- `sightings_assign_buyer` — `app/routers/sightings.py`
- `sightings_advance_status` — `app/routers/sightings.py`
- `sightings_log_activity` — `app/routers/sightings.py`
- `sightings_send_inquiry` — `app/routers/sightings.py`

The static-analysis test `tests/test_static_analysis.py::test_broker_publish_calls_have_source_gate`
enforces this list automatically.
