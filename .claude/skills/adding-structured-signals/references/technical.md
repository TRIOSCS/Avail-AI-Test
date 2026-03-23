# Technical Reference — Structured Signals

## Contents
- JSON-LD injection patterns
- Jinja2 escaping rules
- Route context setup
- WARNING: XSS via unescaped JSON-LD
- Validation workflow

---

## JSON-LD Injection in Jinja2

Structured data belongs in `<head>` when possible. In AvailAI's template hierarchy, `base.html` controls `<head>` — use a named block.

```html
{# app/templates/base.html #}
<head>
  {% block structured_data %}{% endblock %}
</head>
```

```html
{# app/templates/htmx/partials/vendors/detail.html #}
{% block structured_data %}
<script type="application/ld+json">{{ schema_data | tojson }}</script>
{% endblock %}
```

---

## FastAPI Route — Building the Schema Dict

Build the schema dict in the service layer, not the router. Routers are thin HTTP handlers.

```python
# app/services/vendor_schema.py
from app.models.vendor import Vendor

def build_vendor_schema(vendor: Vendor, base_url: str) -> dict:
    return {
        "@context": "https://schema.org",
        "@type": "Organization",
        "name": vendor.name,
        "url": f"{base_url}/vendors/{vendor.id}",
        "description": vendor.description or "",
    }
```

```python
# app/routers/htmx_views.py
from app.services.vendor_schema import build_vendor_schema

@router.get("/vendors/{vendor_id}")
async def vendor_detail(request: Request, vendor_id: int, db: Session = Depends(get_db)):
    vendor = db.get(Vendor, vendor_id)
    schema_data = build_vendor_schema(vendor, str(request.base_url).rstrip("/"))
    return templates.TemplateResponse("htmx/partials/vendors/detail.html", {
        "request": request,
        "vendor": vendor,
        "schema_data": schema_data,
    })
```

---

## WARNING: XSS via Unescaped JSON-LD

**The Problem:**

```html
<!-- BAD - never interpolate variables directly into JSON-LD string literals -->
<script type="application/ld+json">
{
  "name": "{{ vendor.name }}"
}
</script>
```

**Why This Breaks:**
1. If `vendor.name` contains `</script>`, it terminates the script block and allows arbitrary HTML injection.
2. Jinja2's autoescaping converts `<` to `&lt;` inside HTML context — but `<script>` blocks are **not** HTML context, so autoescaping does NOT apply.
3. Attackers can inject `{"name": "x</script><script>alert(1)</script>"}` via vendor name fields.

**The Fix:**

```html
<!-- GOOD - use tojson filter, which produces valid JSON with proper escaping -->
<script type="application/ld+json">{{ schema_data | tojson }}</script>
```

`tojson` escapes `<`, `>`, and `&` as `\u003c`, `\u003e`, `\u0026` — safe inside `<script>` blocks.

---

## Validation Workflow

Iterate until Google's Rich Results Test passes:

1. Add JSON-LD block to template
2. Start server: `docker compose up -d`
3. Navigate to page with Playwright, extract structured data:

```python
# Verify JSON-LD is present and parses correctly
import json

script_content = page.evaluate("""
  () => document.querySelector('script[type="application/ld+json"]').textContent
""")
data = json.loads(script_content)
assert data["@type"] == "Organization"
```

4. If parse fails, fix escaping and repeat step 3.
5. Validate `@type` and required properties are present before marking complete.

See the **playwright** skill for full browser automation setup.
