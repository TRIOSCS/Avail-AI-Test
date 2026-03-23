---
name: jinja2
description: |
  Renders server-side templates with Jinja2 syntax and inheritance for the AvailAI FastAPI stack.
  Use when: writing new templates, adding template macros, registering custom filters, building
  HTMX partials, tracing template context, or debugging Jinja2 rendering in FastAPI routes.
allowed-tools: Read, Edit, Write, Glob, Grep, Bash, mcp__plugin_context7_context7__resolve-library-id, mcp__plugin_context7_context7__query-docs
---

# Jinja2

AvailAI uses Jinja2 for all server-side rendering via a singleton `Jinja2Templates` instance in `app/template_env.py`. Every route returns `templates.TemplateResponse(...)` — no JSON for page loads. The page lifecycle is always: full-page shell → spinner → HTMX lazy-fetches the real partial.

## Quick Start

### Render a partial from a route

```python
from ..template_env import templates

@router.get("/v2/partials/quotes", response_class=HTMLResponse)
async def quotes_list(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    ctx = _base_ctx(request, user, "quotes")
    ctx.update({"quotes": results, "total": total, "limit": 50, "offset": 0})
    return templates.TemplateResponse("htmx/partials/quotes/list.html", ctx)
```

### Use a macro

```html
{% from "htmx/partials/shared/_macros.html" import status_badge, urgency_badge %}
{{ status_badge(quote.status) }}
{{ urgency_badge(req.urgency) }}
```

### Register a custom filter

```python
# app/template_env.py
templates.env.filters["timesince"] = _timesince_filter
templates.env.filters["sanitize_html"] = _sanitize_html_filter
```

### Apply custom filters

```html
{{ created_at | timesince }}          {# "5 min ago" #}
{{ created_at | timeago }}            {# "5m" #}
{{ created_at | fmtdate("%b %d") }}   {# "Mar 23" #}
{{ body | sanitize_html }}            {# XSS-safe HTML #}
```

## Key Concepts

| Concept | Location | Usage |
|---------|----------|-------|
| Template singleton | `app/template_env.py` | `from ..template_env import templates` |
| Base shell | `app/templates/base.html` | Topbar, nav, modal, toast, scripts |
| Lazy loader | `app/templates/htmx/base_page.html` | Spinner → `hx-get` to actual partial |
| Macro library | `app/templates/htmx/partials/shared/_macros.html` | `status_badge`, `urgency_badge`, `filter_pill` |
| Base context | `_base_ctx()` in `htmx_views.py` | Always pass as first ctx block |

## Template Hierarchy

```
base.html                    ← app shell (blocks: title, head, content, scripts)
  └── htmx/base_page.html   ← extends base.html, triggers hx-get on load
        └── partials/*/     ← returned by hx-get, swapped into #main-content
```

## See Also

- [patterns](references/patterns.md)
- [workflows](references/workflows.md)

## Related Skills

- See the **htmx** skill for HTMX attributes used inside templates
- See the **fastapi** skill for route and dependency injection patterns
- See the **frontend-design** skill for Tailwind CSS and Alpine.js component patterns
- See the **alpine-js** skill for `x-data`, `x-show`, `@click` bindings in templates

## Documentation Resources

> Fetch latest Jinja2 documentation with Context7.

1. Use `mcp__plugin_context7_context7__resolve-library-id` → search `"jinja2"`
2. Prefer `/websites/` IDs over source repos
3. Query with `mcp__plugin_context7_context7__query-docs`

**Recommended queries:** `"jinja2 template inheritance"`, `"jinja2 macros"`, `"jinja2 custom filters"`
