# Structured Data (Schema.org) Reference

## Contents
- When structured data matters for AvailAI
- SoftwareApplication schema for the login page
- Product schema for catalog pages
- Organization schema
- Validation

---

## When Structured Data Applies

For a behind-auth B2B tool, structured data has limited ROI. Prioritise:

1. **`SoftwareApplication`** on the login/landing page — enables rich results for app searches
2. **`Organization`** in `base.html` sitewide — trust signal, eligibility for Knowledge Panel
3. **`Product`** on public component catalog pages (if they exist)

---

## SoftwareApplication Schema

Add to the login page as a JSON-LD block:

```html
<!-- app/templates/login.html — inside {% block extra_head %} -->
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "SoftwareApplication",
  "name": "AvailAI",
  "applicationCategory": "BusinessApplication",
  "operatingSystem": "Web",
  "description": "Electronic component sourcing platform with automated RFQ workflows and 10-source parallel search.",
  "offers": {
    "@type": "Offer",
    "price": "0",
    "priceCurrency": "USD"
  },
  "publisher": {
    "@type": "Organization",
    "name": "Trio Supply Chain Solutions"
  }
}
</script>
```

**DO:** Keep the `description` under 300 chars and factual.
**DON'T:** Add `aggregateRating` unless you have real review data — fake ratings get manual penalties.

---

## Organization Schema

Sitewide, in `base.html`:

```html
<!-- app/templates/base.html — bottom of <body> -->
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "Organization",
  "name": "Trio Supply Chain Solutions",
  "url": "https://yourdomain.com",
  "logo": "https://yourdomain.com/static/img/logo.png"
}
</script>
```

Inject `BASE_URL` from config to avoid hardcoding:

```python
# app/main.py — add to Jinja2 globals
from app.config import settings
templates.env.globals["BASE_URL"] = settings.base_url
```

```html
<!-- base.html — use the global -->
"url": "{{ BASE_URL }}"
```

See the **fastapi** skill for Jinja2 global configuration patterns.

---

## Product Schema for Catalog Pages

If public component pages exist:

```html
<!-- app/templates/catalog/part_detail.html -->
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "Product",
  "name": "{{ card.mpn }}",
  "description": "{{ card.description | truncate(300) }}",
  "brand": {
    "@type": "Brand",
    "name": "{{ card.manufacturer or 'Unknown' }}"
  },
  "sku": "{{ card.mpn }}"
}
</script>
```

**WARNING:** Only add `offers` to `Product` schema if you have real, current pricing. Stale pricing in schema markup triggers rich result eligibility loss.

---

## Validation

Validate schema with Google's Rich Results Test after any changes:

```bash
# Capture the rendered HTML for offline inspection
curl -s https://yourdomain.com/ | grep -A 20 'application/ld+json'
```

Or use Playwright to render and extract:

```python
# Validate schema is rendered correctly
page.goto("https://yourdomain.com/")
schema = page.evaluate("""
  () => JSON.parse(document.querySelector('script[type="application/ld+json"]').textContent)
""")
assert schema["@type"] == "SoftwareApplication"
```

See the **playwright** skill for browser automation patterns.
