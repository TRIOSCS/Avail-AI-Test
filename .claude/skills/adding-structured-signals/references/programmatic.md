# Programmatic Reference — Structured Signals

## Contents
- Service-layer schema builders
- Shared macro for all page types
- Loop-based schema for list pages
- WARNING: Schema in HTMX partials
- Testing programmatic output

---

## Service-Layer Schema Builders

Keep schema construction out of templates and routers. One builder function per entity type.

```python
# app/services/schema_builders.py
"""
Schema.org JSON-LD builder functions for AvailAI entity pages.

Called by: app/routers/htmx_views.py
Depends on: app/models/vendor.py, app/models/company.py, app/models/materials.py
"""
from __future__ import annotations
from app.models.vendor import Vendor
from app.models.company import Company


def vendor_schema(vendor: Vendor, base_url: str) -> dict:
    data: dict = {
        "@context": "https://schema.org",
        "@type": "Organization",
        "name": vendor.name,
        "url": f"{base_url}/vendors/{vendor.id}",
    }
    if vendor.description:
        data["description"] = vendor.description
    if vendor.website:
        data["sameAs"] = vendor.website
    return data


def company_schema(company: Company, base_url: str) -> dict:
    return {
        "@context": "https://schema.org",
        "@type": "Organization",
        "name": company.name,
        "url": f"{base_url}/companies/{company.id}",
    }
```

---

## Shared Jinja2 Macro

One macro eliminates repetitive `<script>` tags across 30+ templates.

```html
{# app/templates/macros/schema.html #}
{% macro json_ld(data) %}
{% if data %}
<script type="application/ld+json">{{ data | tojson }}</script>
{% endif %}
{% endmacro %}
```

Usage in any template:

```html
{# app/templates/htmx/partials/vendors/detail.html #}
{% from "macros/schema.html" import json_ld %}
{% block structured_data %}
  {{ json_ld(schema_data) }}
{% endblock %}
```

See the **jinja2** skill for macro imports and block inheritance.

---

## WARNING: Schema in HTMX Partials

**The Problem:**

```html
<!-- BAD - JSON-LD in an HTMX partial loaded via hx-get -->
<!-- app/templates/htmx/partials/vendors/_row.html -->
<tr hx-get="/vendors/{{ vendor.id }}" ...>
  <script type="application/ld+json">{{ schema | tojson }}</script>
</tr>
```

**Why This Breaks:**
1. HTMX partial responses are HTML fragments swapped into `#main-content` — Googlebot does not index them as standalone documents.
2. `<script>` tags injected into the DOM via HTMX swaps may not be executed by the browser (HTMX strips scripts from swap targets by default).
3. Duplicate JSON-LD blocks accumulate if the partial is swapped multiple times.

**The Fix:**

Only add structured data to full-page responses — routes that return a complete HTML document, not partials. Check `htmx_views.py`: full-page routes use `base_page.html` as their base; partial routes use fragment templates directly.

---

## Testing Programmatic Output

```python
# tests/test_schema_builders.py
from app.services.schema_builders import vendor_schema
from tests.conftest import make_vendor  # fixture factory

def test_vendor_schema_omits_null_fields():
    vendor = make_vendor(name="ACME", description=None, website=None)
    result = vendor_schema(vendor, "https://example.com")
    assert result["name"] == "ACME"
    assert "description" not in result
    assert "sameAs" not in result

def test_vendor_schema_includes_description_when_present():
    vendor = make_vendor(name="ACME", description="Great supplier")
    result = vendor_schema(vendor, "https://example.com")
    assert result["description"] == "Great supplier"
```

See the **pytest** skill for fixture patterns with SQLAlchemy models.
