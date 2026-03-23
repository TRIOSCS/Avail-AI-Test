---
name: htmx
description: |
  Implements HTMX attributes for server-driven UI updates in the AvailAI FastAPI + Jinja2 stack.
  Use when: adding HTMX attributes to templates, building partials, implementing inline editing,
  lazy-loading sections, filter/search forms, tab navigation, modals, or handling HTMX responses
  in FastAPI routes.
allowed-tools: Read, Edit, Write, Glob, Grep, Bash, mcp__plugin_context7_context7__resolve-library-id, mcp__plugin_context7_context7__query-docs
---

# HTMX Skill

AvailAI uses HTMX 2.x as the sole navigation mechanism — no SPA, no client-side routing. Every page transition is an `hx-get` swap into `#main-content`. Alpine.js handles local component state; HTMX handles all HTTP. They are complementary, not competing. See the **alpine-js** and **fastapi** skills for the other two sides of this pattern.

## Core Pattern: Page → Partial Route

Every "page" is actually two routes in FastAPI:

```python
@router.get("/v2/requisitions", response_class=HTMLResponse)
async def requisitions_page(request: Request, ...):
    if request.headers.get("HX-Request") == "true":
        return templates.TemplateResponse("htmx/partials/requisitions/list.html", ctx)
    ctx["partial_url"] = "/v2/partials/requisitions"
    return templates.TemplateResponse("htmx/base_page.html", ctx)
```

```html
{# base_page.html — spinner until partial loads via hx-trigger="load" #}
<div hx-get="{{ partial_url }}"
     hx-target="#main-content"
     hx-trigger="load"
     hx-swap="innerHTML">
  <svg class="h-8 w-8 text-gray-400 spinner" .../>
</div>
```

## Common Patterns

### Live Search with Debounce + URL Push

```html
<input type="text" name="q" value="{{ q }}"
       hx-get="/v2/partials/requisitions"
       hx-target="#main-content"
       hx-swap="innerHTML"
       hx-push-url="true"
       hx-include="#req-filters"
       hx-trigger="input delay:300ms">
```

### Tab Navigation (HTMX + Alpine)

```html
<div x-data="{ activeTab: 'parts' }">
  <button @click="activeTab = 'parts'"
          hx-get="/v2/partials/requisitions/{{ req.id }}/tab/parts"
          hx-target="#tab-content"
          hx-swap="innerHTML"
          :class="activeTab === 'parts' ? 'border-brand-500 text-brand-500' : 'border-transparent'">
    Parts
  </button>
  <div id="tab-content">{% include "htmx/partials/requisitions/tabs/parts.html" %}</div>
</div>
```

### Inline Editing (click-to-edit)

```html
{# Display cell — click loads edit form via outerHTML swap #}
<td id="cell-{{ req.id }}-name"
    hx-get="/v2/partials/requisitions/{{ req.id }}/edit/name"
    hx-target="#cell-{{ req.id }}-name"
    hx-swap="outerHTML"
    class="cursor-pointer hover:bg-brand-50">{{ req.name }}</td>

{# Edit form — blur/Enter saves, Escape cancels #}
<form hx-patch="/v2/partials/requisitions/{{ req.id }}/inline"
      hx-target="#cell-{{ req.id }}-name"
      hx-swap="outerHTML">
  <input type="text" name="value" value="{{ req.name }}"
         x-init="$el.focus(); $el.select()"
         @blur="$el.form.requestSubmit()"
         @keydown.enter.prevent="$el.form.requestSubmit()"
         @keydown.escape.prevent="htmx.ajax('GET', '/v2/.../cell/display/name', {target: '#cell-{{ req.id }}-name', swap: 'outerHTML'})">
</form>
```

### Modal Load Pattern

```html
<button @click="$dispatch('open-modal')"
        hx-get="/v2/partials/requisitions/create-form"
        hx-target="#modal-content">
  New Requisition
</button>
```

### Out-of-Band Updates

```html
{# Update page title from any partial #}
<title hx-swap-oob="true">{{ req.name }} — AvailAI</title>
```

## Key Concepts

| Concept | Attribute | Notes |
|---------|-----------|-------|
| Target swap | `hx-target`, `hx-swap` | Default: `this` / `innerHTML` |
| Form state | `hx-include="#form-id"` | Includes all inputs from another form |
| Extra values | `hx-vals='{"key": "val"}'` | JSON merged into request params |
| Error target | `hx-target-error="#div"` | Requires `htmx-ext-response-targets` |
| Loading indicator | `hx-indicator="#spinner"` | Shows `.htmx-indicator` elements |
| Preload | `preload="mouseover"` | Prefetch on hover via htmx-ext-preload |
| OOB swap | `hx-swap-oob="true"` | Update elements outside `hx-target` |
| Programmatic | `htmx.ajax('GET', url, {target, swap})` | Call from Alpine.js handlers |

## See Also

- [patterns](references/patterns.md) — anti-patterns, loading states, error handling
- [workflows](references/workflows.md) — route structure, inline editing flow, modal workflow

## Related Skills

- **fastapi** — route handlers returning `TemplateResponse`
- **alpine-js** — component state paired with HTMX requests
- **jinja2** — templates that render the HTML HTMX receives

## Documentation Resources

**How to use Context7:**
1. `mcp__plugin_context7_context7__resolve-library-id` → search "htmx"
2. Prefer `/websites/` IDs over source repos
3. `mcp__plugin_context7_context7__query-docs` with resolved ID

**Recommended Queries:** "htmx hx-swap hx-target", "htmx extensions loading states", "htmx events after-request"
