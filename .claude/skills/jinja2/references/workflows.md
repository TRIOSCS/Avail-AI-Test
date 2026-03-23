# Jinja2 Workflows Reference

## Contents
- Adding a new page with lazy-loaded partial
- Adding a new macro
- Registering a custom filter
- Tracing a template route
- Inline editing with HTMX swap

---

## Adding a New Page with Lazy-Loaded Partial

This is the standard pattern for every new page in AvailAI.

**Checklist:**

```
- [ ] Add full-page route (returns base_page.html + partial_url)
- [ ] Add partial route (returns actual HTML fragment)
- [ ] Create partial template in app/templates/htmx/partials/<domain>/
- [ ] Add header comment to template with received context vars
- [ ] Add hx-swap-oob title tag
- [ ] Add nav link in topbar pointing to full-page URL
```

**Step 1: Full-page route**

```python
@router.get("/v2/buy-plans/{bp_id}", response_class=HTMLResponse)
async def buy_plan_page(request: Request, bp_id: int, user: User = Depends(require_user)):
    ctx = _base_ctx(request, user, "buy_plans")
    ctx["partial_url"] = f"/v2/partials/buy-plans/{bp_id}"
    return templates.TemplateResponse("htmx/base_page.html", ctx)
```

**Step 2: Partial route**

```python
@router.get("/v2/partials/buy-plans/{bp_id}", response_class=HTMLResponse)
async def buy_plan_detail(
    request: Request, bp_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    bp = db.get(BuyPlan, bp_id)
    if not bp:
        raise HTTPException(404, "Buy plan not found")
    ctx = _base_ctx(request, user, "buy_plans")
    ctx["bp"] = bp
    return templates.TemplateResponse("htmx/partials/buy_plans/detail.html", ctx)
```

**Step 3: Template**

```html
{# buy_plans/detail.html — Buy plan detail view.
   Receives: bp (BuyPlan ORM object).
   Called by: htmx_views.py buy_plan_detail endpoint.
#}
{% from "htmx/partials/shared/_macros.html" import status_badge %}

<title hx-swap-oob="true">{{ bp.reference }} — AvailAI</title>

<div class="max-w-7xl mx-auto">
  <h1 class="text-xl font-semibold text-gray-900">{{ bp.reference }}</h1>
  {{ status_badge(bp.status) }}
</div>
```

---

## Adding a New Macro

All macros go in `app/templates/htmx/partials/shared/_macros.html`.

**Step 1: Define the macro**

```html
{% macro priority_dot(level) %}
{%- set colors = {'high': 'bg-rose-500', 'medium': 'bg-amber-400', 'low': 'bg-gray-300'} -%}
<span class="inline-block w-2 h-2 rounded-full {{ colors.get(level, 'bg-gray-300') }}" title="{{ level }}"></span>
{%- endmacro %}
```

**Step 2: Import and use**

```html
{% from "htmx/partials/shared/_macros.html" import priority_dot %}
{{ priority_dot(item.priority) }}
```

**Validate — search for existing macro before adding:**

```bash
grep -n "macro " app/templates/htmx/partials/shared/_macros.html
```

---

## Registering a Custom Filter

All filters are registered in `app/template_env.py`.

**Step 1: Write the filter function**

```python
def _currency_filter(value: float | None, symbol: str = "$") -> str:
    if value is None:
        return "-"
    return f"{symbol}{value:,.2f}"
```

**Step 2: Register it**

```python
templates.env.filters["currency"] = _currency_filter
```

**Step 3: Use in templates**

```html
{{ quote.subtotal | currency }}          {# "$1,234.56" #}
{{ offer.price | currency(symbol="€") }} {# "€1,234.56" #}
```

**Validate — check the filter is registered:**

```bash
grep "env.filters" app/template_env.py
```

---

## Tracing a Template Route

When you can't find which template a URL renders, follow this chain:

1. Find the route:
```bash
grep -rn '"/v2/partials/quotes"' app/routers/
```

2. Read the view function — find `templates.TemplateResponse("path/to/template.html", ctx)`

3. Open that template and check for `{% extends %}` or `{% include %}` chains

4. Check what context keys are used: `{{ variable }}` — verify all are populated in `ctx`

**Common gotcha:** The parts tab inside a requisition renders `htmx/partials/parts/list.html`, NOT `htmx/partials/requisitions/parts.html`. Always trace the router, never guess the path.

---

## Inline Editing with HTMX OOB Swap

For inline edits that update one field without reloading the section, use `hx-swap-oob`:

**Template (display mode):**

```html
<span id="req-name-{{ req.id }}">{{ req.name }}</span>
<button hx-get="/v2/partials/requisitions/{{ req.id }}/edit-name"
        hx-target="#req-name-{{ req.id }}"
        hx-swap="outerHTML">Edit</button>
```

**Template (edit mode partial):**

```html
{# Returned by the edit-name endpoint #}
<form id="req-name-{{ req.id }}"
      hx-post="/v2/partials/requisitions/{{ req.id }}/save-name"
      hx-swap="outerHTML">
  <input name="name" value="{{ req.name }}" autofocus>
  <button type="submit">Save</button>
</form>
```

**Route (save + return updated display):**

```python
@router.post("/v2/partials/requisitions/{req_id}/save-name", response_class=HTMLResponse)
async def save_req_name(req_id: int, name: str = Form(...), db: Session = Depends(get_db)):
    req = db.get(Requisition, req_id)
    req.name = name
    db.commit()
    # Return display-mode HTML — HTMX swaps outerHTML
    return HTMLResponse(f'<span id="req-name-{req_id}">{name}</span>')
```

See the **htmx** skill for full HTMX swap modes and OOB patterns. See the **fastapi** skill for `Form(...)` and `HTMLResponse` in routes.
