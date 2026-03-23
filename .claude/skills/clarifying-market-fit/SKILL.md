---
name: clarifying-market-fit
description: |
  Aligns ICP, positioning, and value narrative for on-page messaging in the AvailAI sourcing platform.
  Use when: updating login page copy, writing feature descriptions for the sourcing engine, refining
  value propositions for electronic component buyers, repositioning RFQ/search/proactive matching
  features, or auditing in-app microcopy for clarity against the ICP.
allowed-tools: Read, Edit, Write, Glob, Grep, Bash, mcp__playwright__browser_navigate, mcp__playwright__browser_snapshot, mcp__playwright__browser_take_screenshot, mcp__playwright__browser_click, mcp__playwright__browser_evaluate, mcp__playwright__browser_wait_for
---

# Clarifying Market Fit

AvailAI targets **electronics procurement teams at independent distributors and contract manufacturers** — buyers who manually chase vendor quotes across email, spreadsheets, and fragmented supplier portals. The value narrative centers on three outcomes: fewer hours searching, faster RFQ turnaround, and vendor intelligence that compounds over time.

All marketing surfaces live inside the app (login page, empty states, feature intro banners). There is no separate marketing site in this repo.

## ICP Summary

| Dimension | AvailAI Buyer Persona |
|-----------|----------------------|
| **Role** | Procurement buyer / sourcing manager at a distributor or CM |
| **Pain** | Manual multi-supplier search; RFQ replies buried in email |
| **Trigger** | BOM shortage, customer demand spike, supply disruption |
| **Success metric** | Quote turnaround time, fill rate, cost savings |
| **Decision context** | Daily tool; evaluated on speed and data quality |

## Key Marketing Surfaces

| File | Surface | Purpose |
|------|---------|---------|
| `app/templates/htmx/login.html` | Login page | First-impression value statement |
| `app/templates/htmx/partials/*/list.html` | Empty states | Feature intro for new users |
| `app/templates/htmx/base_page.html` | Page shell | Navigation labels, section headers |

## Quick Start

### Updating the Login Value Proposition

```html
{# app/templates/htmx/login.html — hero copy #}
<h1 class="text-2xl font-bold text-gray-900">
  Source faster. Quote smarter.
</h1>
<p class="text-gray-500 mt-2">
  Search 10 supplier networks in parallel. Auto-parse RFQ replies with AI.
  Turn vendor emails into offers in seconds.
</p>
```

### Writing an Empty-State Value Hook

```html
{# When a user has no requisitions yet — app/templates/htmx/partials/requisitions/list.html #}
<div class="text-center py-12">
  <p class="text-sm font-medium text-gray-900">No requisitions yet</p>
  <p class="text-xs text-gray-500 mt-1">
    Paste a BOM or part number to search 10 supplier networks at once.
  </p>
  <a hx-get="/v2/partials/requisitions/new"
     hx-target="#main-content"
     class="mt-4 inline-block px-4 py-2 text-xs font-medium text-white bg-brand-500 rounded">
    Create Requisition
  </a>
</div>
```

### Consistent Feature Naming

```python
# Use these names in all copy, labels, and microcopy — never improvise synonyms
FEATURE_NAMES = {
    "search":     "Sourcing Engine",    # NOT "search" or "API search"
    "rfq":        "RFQ Workflow",       # NOT "email" or "send RFQ"
    "proactive":  "Proactive Matching", # NOT "matching" or "offers"
    "materials":  "Material Cards",     # NOT "parts database"
}
```

## Positioning Rules

1. **Lead with outcomes, not features.** "Find 10 suppliers in 30 seconds" > "Multi-source connector"
2. **Name the pain explicitly.** "Chasing quotes over email" resonates — use that language.
3. **Vendor intelligence compounds.** Every search improves future scores — use this for retention messaging.
4. **Confidence thresholds are a trust signal.** AI auto-parses at ≥0.8 confidence — surface this near the review queue.

## See Also

- [conversion-optimization](references/conversion-optimization.md)
- [content-copy](references/content-copy.md)
- [distribution](references/distribution.md)
- [measurement-testing](references/measurement-testing.md)
- [growth-engineering](references/growth-engineering.md)
- [strategy-monetization](references/strategy-monetization.md)

## Related Skills

- See the **designing-onboarding-paths** skill for empty-state UI patterns
- See the **orchestrating-feature-adoption** skill for feature discovery banners
- See the **frontend-design** skill for Tailwind/HTMX component styling
- See the **jinja2** skill for template rendering and macro patterns
- See the **htmx** skill for HTMX partial and CTA wiring
