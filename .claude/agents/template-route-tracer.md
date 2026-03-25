---
name: template-route-tracer
description: Traces the full route → view → template chain for any URL or template file before editing
---

# Template Route Tracer

Before editing any template, trace the full request chain to ensure you're editing the right file.

## Instructions

Given a URL path or template filename, trace the complete chain:

1. **Find the route**: Search `app/routers/htmx_views.py` and `app/routers/` for the URL pattern
2. **Find the view function**: Read the route handler to find which template it renders via `template_response()` or `TemplateResponse()`
3. **Find the template**: Verify the template path exists and read its header comment
4. **Trace includes**: Check for `{% include %}` and `{% extends %}` to map all partials involved
5. **Map the context**: List all template variables the view passes via the context dict

## Output Format

```
URL:      /v2/partials/parts
Route:    htmx_views.py:8661 → parts_list_partial()
Template: htmx/partials/parts/list.html
Extends:  (none — partial)
Includes: (none)
Context:  requirements, offer_stats, q, status, sort, dir, ...
Parent:   htmx/partials/parts/workspace.html (loads via hx-get)
```

## Key Gotchas

- The requisitions page loads `parts/list.html`, NOT `requisitions/list.html`
- `workspace.html` templates load their content via `hx-get` on page load, not via includes
- Detail views often have a different route than the list partial
- Always check `hx-target` to understand what gets swapped
