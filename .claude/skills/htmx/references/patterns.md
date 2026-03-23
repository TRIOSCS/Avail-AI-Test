# HTMX Patterns Reference

## Contents
- Swap Strategies
- Loading States (htmx-ext-loading-states)
- Error Handling
- Filter + Pagination
- JSON POST with Extensions
- Anti-Patterns

---

## Swap Strategies

Choose `hx-swap` based on what you're replacing:

| Scenario | Swap | Why |
|----------|------|-----|
| Replace a section's content | `innerHTML` | Keeps the container, replaces children |
| Replace a row or cell | `outerHTML` | Replaces the element itself (inline editing) |
| Prepend new item to list | `afterbegin` | New items appear at top |
| Append to list | `beforeend` | New items appear at bottom |
| Remove element after action | `delete` | Clean removal without JS |

```html
{# Prepend new excess list row after creation #}
<form hx-post="/api/excess-lists"
      hx-target="#excess-table-body"
      hx-swap="afterbegin">
```

---

## Loading States (htmx-ext-loading-states)

This project uses `htmx-ext-loading-states` for button state during requests. NEVER use plain `disabled` — the extension handles it automatically via data attributes.

```html
{# Button: spinner shows during request, icon hides; button auto-disabled #}
<button hx-post="/v2/partials/search/run"
        hx-target="#search-results"
        data-loading-disable>
  <svg data-loading class="h-4 w-4 spinner" .../>        {# shows during request #}
  <svg data-loading-remove class="h-4 w-4" .../>         {# hides during request #}
  Search
</button>
```

For a separate spinner indicator:
```html
{# .htmx-indicator is hidden by default, shown during any request #}
<span id="req-loading" class="htmx-indicator flex items-center gap-1 text-sm text-gray-500">
  <svg class="h-4 w-4 animate-spin text-brand-500" .../>
</span>

<input hx-get="/v2/partials/requisitions"
       hx-indicator="#req-loading"
       ...>
```

---

## Error Handling

Use `hx-target-error` (from `htmx-ext-response-targets`) to route non-2xx responses to a dedicated error element. NEVER silently discard errors.

```html
<form hx-get="/v2/partials/requisitions"
      hx-target="#main-content"
      hx-target-error="#req-error">
  ...
</form>

{# Error container — hidden until a 4xx/5xx response arrives #}
<div id="req-error" class="hidden mt-2 p-2 text-sm text-rose-600 bg-rose-50 rounded-lg"></div>
```

**FastAPI side** — always return an appropriate HTTP status for errors. HTMX routes error responses to `hx-target-error` only when the status code is 4xx or 5xx:

```python
@router.patch("/v2/partials/requisitions/{req_id}/inline")
async def inline_edit(req_id: int, ...):
    req = db.get(Requisition, req_id)
    if not req:
        raise HTTPException(status_code=404, detail="Not found")
    ...
    return templates.TemplateResponse("htmx/partials/requisitions/req_row.html", ctx)
```

Retry button pattern for transient failures:
```html
{% if error %}
<button hx-post="/v2/partials/search/run"
        hx-target="#search-results"
        hx-vals='{"mpn": "{{ mpn }}"}'>
  Retry search
</button>
{% endif %}
```

---

## Filter + Pagination Pattern

Use a named form as the single source of filter state. Every filter input includes that form via `hx-include`. Pagination links include the same form to preserve active filters.

```html
<form id="req-filters" onsubmit="return false">
  <input type="text" name="q" value="{{ q }}"
         hx-get="/v2/partials/requisitions"
         hx-target="#main-content"
         hx-push-url="true"
         hx-include="#req-filters"
         hx-trigger="input delay:300ms">

  {# Hidden inputs preserve non-input filter state #}
  <input type="hidden" name="sort" value="{{ sort }}">
  <input type="hidden" name="dir" value="{{ dir }}">
</form>

{# Pagination — preserves all filters, preloads on hover #}
<a hx-get="/v2/partials/requisitions?offset={{ offset + limit }}"
   hx-target="#main-content"
   hx-include="#req-filters"
   preload="mouseover">Next</a>
```

**WHY `hx-push-url="true"`**: Without it, browser back/forward breaks. Filter state lives in the URL. Use `hx-push-url="/explicit/path"` when the partial URL differs from the canonical page URL.

---

## JSON POST with Extensions

For endpoints that expect JSON bodies (not form data), use `hx-ext="json-enc"`:

```html
<form hx-post="/api/excess-lists"
      hx-target="#excess-table-body"
      hx-swap="afterbegin"
      hx-ext="json-enc"
      @htmx:after-request.camel="if(event.detail.successful) {
        $dispatch('close-modal');
        Alpine.store('toast').show = true;
      }">
  <input type="text" name="title" required>
  <select name="company_id" required>...</select>
</form>
```

Use `@htmx:after-request.camel` (Alpine camelCase event listener) for post-submit side effects like closing modals or showing toasts. Never use `hx-on:htmx:after-request` — Alpine's camelCase binding is more readable.

---

## WARNING: Anti-Patterns

### WARNING: Missing `hx-push-url` on Navigation

**The Problem:**
```html
{# BAD — navigable content without URL update #}
<a hx-get="/v2/partials/requisitions/{{ req.id }}"
   hx-target="#main-content">
  {{ req.name }}
</a>
```

**Why This Breaks:**
1. Browser back button returns to previous URL but HTMX won't know to reload the correct content
2. Sharing the URL gives the wrong page
3. Page refresh loses the current view

**The Fix:**
```html
{# GOOD — push the canonical URL #}
<a hx-get="/v2/partials/requisitions/{{ req.id }}"
   hx-target="#main-content"
   hx-push-url="/v2/requisitions/{{ req.id }}">
  {{ req.name }}
</a>
```

---

### WARNING: Editing Templates Without Tracing the Route

**The Problem:** Modifying `app/templates/htmx/partials/parts/list.html` when the requisitions parts tab actually loads that template — NOT `requisitions/list.html`.

**Why This Breaks:** The router, not the template path, determines what renders. Multiple routes can render the same template; one template can be included by several routes.

**The Fix:** Always trace `router → view function → template_response()` before editing. Search `app/routers/` for the partial URL, find the `templates.TemplateResponse(...)` call, confirm the template path.

---

### WARNING: `htmx.ajax()` Without Error Handling

**The Problem:**
```javascript
// BAD — fire and forget from Alpine
htmx.ajax('GET', '/v2/partials/some/thing', {target: '#content'})
```

**Why This Breaks:** Network errors or server errors silently fail with no user feedback.

**The Fix:**
```javascript
// GOOD — handle the promise
htmx.ajax('GET', '/v2/partials/some/thing', {target: '#content', swap: 'innerHTML'})
  .then(() => {/* success side effects */})
  .catch(() => Alpine.store('toast').showError('Failed to load'))
```
