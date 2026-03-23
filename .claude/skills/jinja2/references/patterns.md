# Jinja2 Patterns Reference

## Contents
- Template environment and custom filters
- Base context pattern
- Template inheritance
- Macros
- HTMX partial structure
- Anti-patterns

---

## Template Environment

All templates go through the singleton in `app/template_env.py`. Never instantiate `Jinja2Templates` elsewhere.

```python
# app/template_env.py
from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory="app/templates")

templates.env.filters["timesince"] = _timesince_filter   # "5 min ago"
templates.env.filters["timeago"] = _timeago_filter        # "5m ago"
templates.env.filters["fmtdate"] = _fmtdate_filter        # strftime wrapper
templates.env.filters["sanitize_html"] = _sanitize_html_filter  # nh3-based XSS filter
```

**Import in every router that renders templates:**

```python
from ..template_env import templates
```

---

## Base Context Pattern

Every route that renders a template must call `_base_ctx()` first. This injects user info, Vite asset paths, and the active nav item.

```python
def _base_ctx(request: Request, user: User, current_view: str = "") -> dict:
    assets = _vite_assets()
    return {
        "request": request,        # Required — Jinja2Templates needs this
        "user_name": user.name,
        "user_email": user.email,
        "is_admin": user.role == UserRole.ADMIN,
        "current_view": current_view,
        "vite_js": assets["js_file"],
        "vite_css": assets["css_files"],
    }

# Then extend with domain data:
ctx = _base_ctx(request, user, "quotes")
ctx.update({"quotes": results, "total": total, "limit": 50, "offset": 0})
return templates.TemplateResponse("htmx/partials/quotes/list.html", ctx)
```

**NEVER** skip `_base_ctx()`. Templates that extend `base.html` depend on `vite_js`, `vite_css`, and `current_view`.

---

## Template Inheritance

```
base.html → htmx/base.html → htmx/base_page.html → partial rendered via hx-get
```

Full-page routes render `base_page.html` (which just shows a spinner and fires `hx-get`). The actual content is a separate partial endpoint.

```python
# Full page — renders shell + spinner
@router.get("/v2/quotes/{quote_id}", response_class=HTMLResponse)
async def quote_page(request: Request, quote_id: int, user: User = Depends(require_user)):
    ctx = _base_ctx(request, user, "quotes")
    ctx["partial_url"] = f"/v2/partials/quotes/{quote_id}"
    return templates.TemplateResponse("htmx/base_page.html", ctx)

# Partial — returns the real HTML, swapped into #main-content
@router.get("/v2/partials/quotes/{quote_id}", response_class=HTMLResponse)
async def quote_detail(request: Request, quote_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    quote = db.get(Quote, quote_id)
    ctx = _base_ctx(request, user, "quotes")
    ctx["quote"] = quote
    return templates.TemplateResponse("htmx/partials/quotes/detail.html", ctx)
```

**In `base_page.html`:**

```html
{% extends "htmx/base.html" %}
{% block content %}
<div hx-get="{{ partial_url }}"
     hx-target="#main-content"
     hx-trigger="load"
     hx-swap="innerHTML">
  {# spinner SVG #}
</div>
{% endblock %}
```

---

## Macros

All reusable components live in `app/templates/htmx/partials/shared/_macros.html`.

```html
{% macro status_badge(value, status_map=None) %}
{%- set default_map = {
  'active': 'bg-emerald-50 text-emerald-700',
  'draft': 'bg-brand-100 text-brand-600',
  'won': 'bg-emerald-200 text-emerald-800',
  'lost': 'bg-rose-50 text-rose-700',
} -%}
{%- set colors = status_map if status_map else default_map -%}
<span class="inline-flex px-2 py-0.5 text-xs font-medium rounded-full {{ colors.get(value, 'bg-gray-100 text-gray-600') }}">
  {{ value | replace('_', ' ') | capitalize }}
</span>
{%- endmacro %}
```

**Import and call:**

```html
{% from "htmx/partials/shared/_macros.html" import status_badge, urgency_badge, filter_pill %}

{{ status_badge(quote.status) }}
{{ status_badge(req.status, status_map={'open': 'bg-blue-50 text-blue-700'}) }}
{{ urgency_badge(req.urgency) }}
```

**Add new macros to `_macros.html`, never inline repeat HTML across partials.**

---

## HTMX Partial Structure

Every partial file should have a header comment and use `hx-swap-oob` for the page title:

```html
{# quotes/list.html — Quote list with status filters, search, pagination.
   Receives: quotes, q (str), status (str), total, limit, offset.
   Called by: htmx_views.py quotes_list endpoint.
#}
{% from "htmx/partials/shared/_macros.html" import status_badge %}

<title hx-swap-oob="true">Quotes — AvailAI</title>

<div class="max-w-7xl mx-auto">
  {# content #}
</div>
```

**Pagination math in templates:**

```html
{% set current_page = (offset // limit) + 1 %}
{% set total_pages = ((total - 1) // limit) + 1 %}
<span>Page {{ current_page }} of {{ total_pages }}</span>
<span>Showing {{ offset + 1 }}–{{ [offset + limit, total] | min }} of {{ total }}</span>
```

---

## Anti-Patterns

### WARNING: Duplicate template singletons

**The Problem:**

```python
# BAD — creates a second, filter-less Jinja2Templates instance
from fastapi.templating import Jinja2Templates
templates = Jinja2Templates(directory="app/templates")
```

**Why This Breaks:** Custom filters (`timesince`, `sanitize_html`) are registered only on the singleton in `app/template_env.py`. A second instance renders raw datetimes and unsanitized HTML.

**The Fix:** Always import the singleton:

```python
from ..template_env import templates
```

---

### WARNING: Skipping `_base_ctx()`

**The Problem:**

```python
# BAD — missing vite_js, current_view, user fields
return templates.TemplateResponse("htmx/partials/foo.html", {"request": request, "items": items})
```

**Why This Breaks:** `base.html` references `{{ vite_js }}` and `{{ current_view }}`. Missing keys cause `UndefinedError` at render time, even for partials that extend base indirectly.

**The Fix:**

```python
ctx = _base_ctx(request, user, "foo")
ctx["items"] = items
return templates.TemplateResponse("htmx/partials/foo.html", ctx)
```

---

### WARNING: Logic-heavy templates

Keep computed values in Python, not Jinja2. Templates that compute margin percentages, run string parsing, or build data structures are hard to test and slow.

```html
{# BAD — business logic in template #}
{% set margin = (price - cost) / price * 100 if price else 0 %}

{# GOOD — compute in route/service, pass as context #}
{{ quote.margin_pct | round(1) }}%
