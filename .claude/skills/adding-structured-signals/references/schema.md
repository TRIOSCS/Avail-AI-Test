# Schema Reference — Structured Signals

## Contents
- Schema types relevant to AvailAI
- Required vs recommended properties
- Nested types
- WARNING: Using wrong @type for entity
- Validation commands

---

## Schema Types Relevant to AvailAI

| Entity | `@type` | Required | Recommended |
|---|---|---|---|
| Vendor / Supplier | `Organization` | `name` | `url`, `description`, `telephone`, `sameAs` |
| Customer company | `Organization` | `name` | `url`, `contactPoint` |
| Component (material card) | `Product` | `name` | `mpn`, `brand`, `description`, `url` |
| Page breadcrumb | `BreadcrumbList` | `itemListElement` | — |
| Search results | `SearchResultsPage` | — | — |
| Contact person | `Person` | `name` | `email`, `telephone`, `worksFor` |

---

## Organization Schema — Full Example

```python
{
    "@context": "https://schema.org",
    "@type": "Organization",
    "name": "ACME Electronics",
    "url": "https://availai.example.com/vendors/42",
    "description": "Distributor of passive and active electronic components.",
    "telephone": "+1-555-0100",
    "sameAs": "https://acme-electronics.com",
    "address": {
        "@type": "PostalAddress",
        "addressCountry": "US",
        "addressLocality": "San Jose",
        "addressRegion": "CA"
    }
}
```

---

## Product Schema — Material Card

```python
{
    "@context": "https://schema.org",
    "@type": "Product",
    "name": "LM741CN",
    "mpn": "LM741CN",
    "url": "https://availai.example.com/materials/1234",
    "description": "General-purpose operational amplifier, DIP-8 package.",
    "brand": {
        "@type": "Brand",
        "name": "Texas Instruments"
    }
}
```

---

## BreadcrumbList — Navigation Path

```python
{
    "@context": "https://schema.org",
    "@type": "BreadcrumbList",
    "itemListElement": [
        {
            "@type": "ListItem",
            "position": 1,
            "name": "Vendors",
            "item": "https://availai.example.com/vendors"
        },
        {
            "@type": "ListItem",
            "position": 2,
            "name": "ACME Electronics",
            "item": "https://availai.example.com/vendors/42"
        }
    ]
}
```

---

## WARNING: Using Wrong @type for Entity

**The Problem:**

```python
# BAD - using LocalBusiness for a global distributor
{
    "@type": "LocalBusiness",
    "name": vendor.name,
}
```

**Why This Breaks:**
1. `LocalBusiness` triggers address and hours validation. Missing `address` will cause Google to demote the rich result.
2. Electronic component distributors are not local businesses — the wrong type misleads the knowledge graph and may cause type mismatch penalties.

**The Fix:** Use `Organization` for vendors/companies unless they are explicitly physical retail locations. `Organization` has no required address field.

---

## Validation Commands

```bash
# Extract and print JSON-LD from a running page
docker compose exec app python -c "
import httpx, json
r = httpx.get('http://localhost:8000/vendors/1', follow_redirects=True)
from bs4 import BeautifulSoup
soup = BeautifulSoup(r.text, 'html.parser')
for tag in soup.find_all('script', type='application/ld+json'):
    print(json.dumps(json.loads(tag.string), indent=2))
"
```

Iterate until output is valid JSON with correct `@type` and no null values.
