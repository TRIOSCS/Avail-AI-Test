# On-Page Reference — Structured Signals

## Contents
- Page type to schema type mapping
- Title and description alignment
- Canonical URL in structured data
- Open Graph alongside JSON-LD
- WARNING: Mismatched on-page vs schema content

---

## Page Type → Schema Type Mapping

| AvailAI Page | Schema.org Type | Key Properties |
|---|---|---|
| Vendor detail | `Organization` | name, url, description |
| Company (customer) detail | `Organization` | name, url, contactPoint |
| Requisition detail | `ItemList` or custom | name, description |
| Search results | `SearchResultsPage` | — |
| Login / auth | none | skip — no indexable content |
| HTMX partials | none | partials are not full pages |

**Only apply structured data to full-page responses**, not HTMX partials loaded via `hx-get`. Partial responses lack `<head>` and won't be indexed independently.

---

## Canonical URL in Structured Data

The `url` property must match the canonical URL you want indexed. Use `request.url_for()` to generate stable URLs rather than hardcoding.

```python
# app/services/company_schema.py
def build_company_schema(company, request) -> dict:
    canonical = str(request.url_for("company_detail", company_id=company.id))
    return {
        "@context": "https://schema.org",
        "@type": "Organization",
        "name": company.name,
        "url": canonical,
    }
```

---

## Open Graph Alongside JSON-LD

JSON-LD handles search engine rich results. Open Graph handles social sharing previews. Both belong in `<head>`. Add an `og_data` block alongside `structured_data`.

```html
{# app/templates/base.html #}
{% block og_tags %}{% endblock %}
{% block structured_data %}{% endblock %}
```

```html
{# app/templates/htmx/partials/vendors/detail.html #}
{% block og_tags %}
<meta property="og:title" content="{{ vendor.name | e }}">
<meta property="og:type" content="website">
<meta property="og:url" content="{{ canonical_url }}">
{% endblock %}

{% block structured_data %}
<script type="application/ld+json">{{ schema_data | tojson }}</script>
{% endblock %}
```

---

## WARNING: Mismatched On-Page vs Schema Content

**The Problem:**

```python
# BAD - schema shows data not visible on the page
schema_data = {
    "@type": "Organization",
    "name": vendor.legal_name,  # internal field, not shown in UI
    "description": "Premium supplier",  # marketing copy not on page
}
```

**Why This Breaks:**
1. Google's quality guidelines require schema content to match visible page content. Mismatches trigger manual penalties.
2. If `legal_name` differs from the displayed `name`, Google may suppress the rich result.

**The Fix:**

```python
# GOOD - schema mirrors exactly what the template renders
schema_data = {
    "@type": "Organization",
    "name": vendor.name,          # same field used in <h1>
    "description": vendor.description,  # same field shown in UI
}
```

---

## Breadcrumb Alignment

BreadcrumbList `name` values must match the visible breadcrumb text rendered in the template. If the UI shows "All Vendors" but the schema says "Vendors", Google may ignore the breadcrumb markup.

```html
{# Keep schema name in sync with visible label #}
{% set breadcrumbs = [
  {"name": "Vendors", "url": request.url_for("vendor_list")},
  {"name": vendor.name, "url": request.url_for("vendor_detail", vendor_id=vendor.id)},
] %}

<nav aria-label="breadcrumb">
  {% for crumb in breadcrumbs %}
    <span>{{ crumb.name }}</span>
  {% endfor %}
</nav>

<script type="application/ld+json">
{{ {"@context": "https://schema.org", "@type": "BreadcrumbList",
    "itemListElement": [
      {"@type": "ListItem", "position": loop.index, "name": crumb.name, "item": crumb.url}
      for loop.index, crumb in enumerate(breadcrumbs, 1)
    ]} | tojson }}
</script>
```

See the **jinja2** skill for macro and loop patterns.
