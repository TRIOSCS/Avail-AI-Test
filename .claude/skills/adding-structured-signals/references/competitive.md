# Competitive Reference — Structured Signals

## Contents
- Competitive positioning signals via schema
- sameAs for entity disambiguation
- Comparison page markup
- WARNING: Misleading competitor signals
- Internal linking for schema graph authority

---

## Competitive Positioning via sameAs

`sameAs` links your entity to authoritative external identifiers, strengthening the knowledge graph signal. For AvailAI vendor profiles, link to the vendor's canonical web presence.

```python
def vendor_schema(vendor, base_url: str) -> dict:
    data = {
        "@context": "https://schema.org",
        "@type": "Organization",
        "name": vendor.name,
        "url": f"{base_url}/vendors/{vendor.id}",
    }
    # sameAs strengthens entity disambiguation over competitor listings
    same_as = []
    if vendor.website:
        same_as.append(vendor.website)
    if vendor.linkedin_url:
        same_as.append(vendor.linkedin_url)
    if same_as:
        data["sameAs"] = same_as if len(same_as) > 1 else same_as[0]
    return data
```

---

## Comparison and Alternative Pages

If AvailAI adds comparison pages (e.g., "AvailAI vs. manual RFQ"), use `WebPage` with `about` and `mentions` to signal competitive relevance.

```python
{
    "@context": "https://schema.org",
    "@type": "WebPage",
    "name": "AvailAI vs Manual RFQ: Electronic Component Sourcing",
    "description": "How automated sourcing reduces lead time vs. manual RFQ processes.",
    "about": {
        "@type": "SoftwareApplication",
        "name": "AvailAI",
        "applicationCategory": "BusinessApplication"
    }
}
```

---

## SoftwareApplication Schema for the Platform Itself

The login/marketing page (if public) is the right place for platform-level schema.

```python
{
    "@context": "https://schema.org",
    "@type": "SoftwareApplication",
    "name": "AvailAI",
    "applicationCategory": "BusinessApplication",
    "operatingSystem": "Web",
    "description": "Electronic component sourcing engine with automated RFQ workflows and vendor intelligence.",
    "offers": {
        "@type": "Offer",
        "price": "0",
        "priceCurrency": "USD"
    }
}
```

See the **crafting-page-messaging** skill for aligning schema descriptions with on-page messaging.

---

## WARNING: Misleading Competitor Signals

**The Problem:**

```python
# BAD - mentioning competitors in schema to capture their branded searches
{
    "@type": "WebPage",
    "description": "Better than DigiKey and Mouser for sourcing.",
    "keywords": "DigiKey alternative, Mouser alternative"
}
```

**Why This Breaks:**
1. Google ignores the `keywords` property entirely — it has been deprecated since 2009.
2. `description` is used for snippet generation, not ranking. Stuffing competitor names into schema description reads as spam and degrades click-through quality.
3. Comparison pages work through on-page content and backlink signals, not schema manipulation.

**The Fix:** Use on-page content for competitive comparisons. Reserve schema for factual entity data only.

---

## Internal Linking for Schema Graph Authority

Schema signals are stronger when the entity is well-linked internally. Pair structured data work with internal link audits.

```html
{# Every vendor mention in partials should link to the canonical vendor page #}
<a href="{{ request.url_for('vendor_detail', vendor_id=vendor.id) }}">
  {{ vendor.name }}
</a>
```

Each `<a>` to a vendor page reinforces the URL used in `Organization.url` — consistency between internal links and schema `url` values builds entity authority.

See the **mapping-user-journeys** skill for auditing link coverage across HTMX partials.
