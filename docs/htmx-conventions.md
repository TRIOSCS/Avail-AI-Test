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

### DO: add X-Rendered-Req-Id to responses that update context-sensitive panels
When a response targets a panel whose content depends on a selected item
(e.g. a detail pane), the server must echo a correlation header
(`X-Rendered-Req-Id`) so a `htmx:beforeSwap` guard can discard stale
responses that arrive out of order.

### DO NOT: fire a parallel GET for stale data alongside a POST for fresh data
The split-GET-then-POST pattern (load stale immediately, replace with fresh)
creates two simultaneous race conditions: response ordering and target
collisions. Use a single POST and show a skeleton while waiting.

### DO NOT: publish SSE events from a handler that was itself triggered by an SSE event
If an SSE event fires a POST that publishes a new SSE event, and the client
re-listens for that event, you have a loop. Guard with `?source=sse` and
skip the publish on that code path.

### DO NOT: surface background-triggered toasts to the user
Rate-guard toasts ("Already searched within X minutes") are only
appropriate when the user explicitly clicked. SSE-triggered refreshes
must suppress `HX-Trigger` toast headers. Use `?source=sse` on the POST
URL and check it server-side before setting `HX-Trigger`.
