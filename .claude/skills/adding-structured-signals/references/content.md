# Content Reference — Structured Signals

## Contents
- Which AvailAI entities warrant structured data
- Description field quality requirements
- Populating sparse fields safely
- WARNING: Empty or null field injection
- Content checklist

---

## Which Entities Warrant Structured Data

Only pages with stable, indexable URLs and meaningful user-facing content qualify.

| Entity | Worth Marking Up? | Reason |
|---|---|---|
| Vendor profile | Yes | Stable URL, company info, search intent match |
| Company (customer) | Conditional | Only if publicly accessible; most are internal |
| Material card | Yes | Product-like entity with MPN, description, suppliers |
| Requisition | No | Internal workflow, not publicly indexed |
| Search results page | Minimal | `SearchResultsPage` type only, no item markup |
| RFQ/offer detail | No | Transactional, not indexable |

---

## Description Field Quality

Schema.org `description` should be 1–2 sentences, plain text, no HTML. AvailAI vendor descriptions may be null or one-word. Add a fallback.

```python
# app/services/vendor_schema.py
def _safe_description(vendor) -> str:
    if vendor.description and len(vendor.description) > 20:
        return vendor.description
    return f"Electronic component supplier specializing in {vendor.primary_category or 'sourcing'}."
```

---

## WARNING: Empty or Null Field Injection

**The Problem:**

```python
# BAD - null fields produce invalid JSON-LD
schema_data = {
    "@type": "Organization",
    "name": vendor.name,
    "telephone": vendor.phone,    # may be None → "telephone": null
    "address": vendor.address,    # may be None → "address": null
}
```

**Why This Breaks:**
1. `null` values in JSON-LD are technically valid JSON but signal missing data to parsers. Google's Rich Results Test flags them as errors for required/recommended fields.
2. Empty string `""` is equally useless and can trigger validation warnings.

**The Fix:**

```python
# GOOD - only include fields that have real values
def build_vendor_schema(vendor, base_url: str) -> dict:
    data: dict = {
        "@context": "https://schema.org",
        "@type": "Organization",
        "name": vendor.name,
        "url": f"{base_url}/vendors/{vendor.id}",
    }
    if vendor.description:
        data["description"] = vendor.description
    if vendor.phone:
        data["telephone"] = vendor.phone
    if vendor.website:
        data["sameAs"] = vendor.website
    return data
```

---

## Material Card as Product Schema

MaterialCards are the closest AvailAI entity to a `Product` schema. MPN is the key identifier.

```python
# app/services/material_schema.py
def build_material_schema(card, base_url: str) -> dict:
    data: dict = {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": card.mpn,
        "url": f"{base_url}/materials/{card.id}",
        "mpn": card.mpn,
    }
    if card.manufacturer:
        data["brand"] = {"@type": "Brand", "name": card.manufacturer}
    if card.description:
        data["description"] = card.description
    return data
```

---

## Content Checklist

Copy and track progress per page type:

- [ ] Identify the primary `@type` for this entity
- [ ] Map schema properties to ORM model fields
- [ ] Add null/empty guards for all optional properties
- [ ] Write description fallback for sparse records
- [ ] Confirm content matches what the template renders
- [ ] Run Playwright JSON-LD extraction test
- [ ] Validate with Rich Results Test
