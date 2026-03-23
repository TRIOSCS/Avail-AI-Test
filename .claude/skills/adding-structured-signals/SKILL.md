---
name: adding-structured-signals
description: |
  Adds structured data markup (JSON-LD) for rich results in search engines.
  Use when: adding Schema.org types to Jinja2 templates, instrumenting entity pages
  (companies, vendors, products) with machine-readable signals, auditing missing
  structured data across page types, or validating rich result eligibility for
  the AvailAI sourcing platform.
allowed-tools: Read, Edit, Write, Glob, Grep, Bash, mcp__playwright__browser_navigate, mcp__playwright__browser_snapshot, mcp__playwright__browser_take_screenshot, mcp__playwright__browser_evaluate, mcp__playwright__browser_wait_for
---

# Adding Structured Signals

AvailAI renders HTML server-side via Jinja2. Structured data lives as inline `<script type="application/ld+json">` blocks injected into templates — no build-time static generation, no meta-framework abstractions. Every entity page (vendor, company, requisition) is a candidate for structured markup.

## Quick Start

### Inject JSON-LD into a Jinja2 template

```html
{# app/templates/htmx/partials/vendors/detail.html #}
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "Organization",
  "name": "{{ vendor.name | e }}",
  "url": "{{ request.url_for('vendor_detail', vendor_id=vendor.id) }}",
  "description": "{{ vendor.description | e }}"
}
</script>
```

### Pass structured data from FastAPI route

```python
# app/routers/htmx_views.py
@router.get("/vendors/{vendor_id}")
async def vendor_detail(vendor_id: int, db: Session = Depends(get_db)):
    vendor = db.get(Vendor, vendor_id)
    return templates.TemplateResponse("htmx/partials/vendors/detail.html", {
        "request": request,
        "vendor": vendor,
        "schema_type": "Organization",
    })
```

## Key Concepts

| Concept | Usage | Example |
|---------|-------|---------|
| JSON-LD block | Inline script in `<head>` or body | `<script type="application/ld+json">` |
| `@type` | Schema.org entity class | `Organization`, `Product`, `BreadcrumbList` |
| Jinja2 escaping | Prevent XSS in JSON-LD values | `{{ value \| e }}` or `{{ value \| tojson }}` |
| `tojson` filter | Safe serialization of Python dicts | `{{ schema_data \| tojson }}` |
| Canonical URL | `url` property in schema | `request.url_for(...)` |

## Common Patterns

### BreadcrumbList for navigation

**When:** Any page deeper than root — vendor detail, requisition detail, company profile.

```html
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "BreadcrumbList",
  "itemListElement": [
    {"@type": "ListItem", "position": 1, "name": "Vendors",
     "item": "{{ request.url_for('vendor_list') }}"},
    {"@type": "ListItem", "position": 2, "name": "{{ vendor.name | e }}",
     "item": "{{ request.url_for('vendor_detail', vendor_id=vendor.id) }}"}
  ]
}
</script>
```

### Reusable macro in base template

```html
{# app/templates/macros/schema.html #}
{% macro json_ld(data) %}
<script type="application/ld+json">{{ data | tojson }}</script>
{% endmacro %}
```

## See Also

- [technical](references/technical.md)
- [on-page](references/on-page.md)
- [content](references/content.md)
- [programmatic](references/programmatic.md)
- [schema](references/schema.md)
- [competitive](references/competitive.md)

## Related Skills

- See the **jinja2** skill for template macros, filters, and inheritance patterns
- See the **fastapi** skill for passing context to `TemplateResponse`
- See the **frontend-design** skill for where JSON-LD blocks fit in the template hierarchy
- See the **playwright** skill for automated rich-result validation
