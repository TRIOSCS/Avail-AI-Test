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

### DO NOT: fire a parallel GET for stale data alongside a POST for fresh data
The split-GET-then-POST pattern (load stale immediately, replace with fresh)
creates two simultaneous race conditions: response ordering and target
collisions. Use a single POST and show a skeleton while waiting.

See: this anti-pattern was deleted in commit `7b0cb7b0` ("fix(htmx):
structural fix for click-to-refresh — single POST, stale-response guard,
source-aware toast"). The current implementation issues a single POST in
`selectReq` (`app/templates/htmx/partials/sightings/list.html:64`).

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
