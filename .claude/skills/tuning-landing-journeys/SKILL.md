---
name: tuning-landing-journeys
description: |
  Improves landing page flow, hierarchy, and conversion paths for the AvailAI sourcing platform.
  Use when: optimizing the login page first impression, improving post-login dashboard activation,
  refining empty-state CTAs to drive first-use behavior, auditing in-app navigation hierarchy,
  or improving the journey from login to first completed requisition or RFQ send.
allowed-tools: Read, Edit, Write, Glob, Grep, Bash, mcp__playwright__browser_navigate, mcp__playwright__browser_snapshot, mcp__playwright__browser_take_screenshot, mcp__playwright__browser_click, mcp__playwright__browser_evaluate, mcp__playwright__browser_wait_for, mcp__playwright__browser_console_messages, mcp__playwright__browser_network_requests
---

# Tuning Landing Journeys

AvailAI has one public surface (the login page) and one post-auth landing surface (the dashboard). "Landing journey" means: the path from first impression to first value action — login → dashboard → first requisition created → first RFQ sent. This skill covers improving hierarchy, copy, and conversion paths across those surfaces in Jinja2 templates served via HTMX.

## Key Surfaces

| Surface | File | Route |
|---------|------|-------|
| Login page | `app/templates/htmx/login.html` | `GET /auth/login` |
| Dashboard | `app/templates/htmx/partials/dashboard.html` | `GET /v2/` |
| Empty states | `app/templates/htmx/partials/shared/empty_state.html` | (list pages) |
| Pipeline insights | lazy-loaded via `hx-trigger="load"` | `/v2/partials/dashboard/pipeline-insights` |

## Quick Start

### Strengthen the Login Page Value Proposition

The current login page has zero value copy — just "Sign in to continue." Add a one-line proof point:

```html
{# app/templates/htmx/login.html — inside the card, above the button #}
<h2 class="text-lg font-semibold text-white text-center mb-2">Sign in to continue</h2>
<p class="text-sm text-brand-300 text-center mb-6">
  Search 10 supplier APIs. Send RFQs. Close faster.
</p>
```

### Improve Dashboard CTA Hierarchy

Primary action (Create Requisition) should visually dominate. Current layout mixes primary/secondary equally:

```html
{# app/templates/htmx/partials/dashboard.html — quick actions #}
{# PRIMARY: solid brand — largest, leftmost #}
<button class="inline-flex items-center gap-2 px-6 py-3 bg-brand-600 text-white text-sm font-semibold
               rounded-lg hover:bg-brand-700 transition-colors shadow-sm"
        @click="$dispatch('open-create-requisition')">
  New Requisition
</button>

{# SECONDARY: ghost — clearly subordinate #}
<button class="inline-flex items-center gap-2 px-4 py-2.5 border border-gray-200 text-gray-600
               text-sm rounded-lg hover:bg-gray-50 transition-colors"
        hx-get="/v2/partials/search" hx-target="#main-content" hx-push-url="/v2/search">
  Search Parts
</button>
```

### Context-Aware Empty State CTA

Generic "No items found" loses the activation moment. Match CTA to the specific empty list:

```html
{# app/templates/htmx/partials/requisitions/list.html #}
{% if not requisitions %}
<div class="text-center py-16">
  <p class="text-gray-500 mb-4">No requisitions yet</p>
  <button class="px-4 py-2 bg-brand-600 text-white text-sm rounded-lg hover:bg-brand-700"
          @click="$dispatch('open-create-requisition')">
    Create your first requisition
  </button>
</div>
{% endif %}
```

## Common Patterns

### Lazy-Load Below-the-Fold Content

```html
<div id="my-section"
     hx-get="/v2/partials/dashboard/my-section"
     hx-trigger="load"
     hx-swap="innerHTML"
     class="min-h-[4rem]">
  <div class="flex items-center justify-center py-6 text-sm text-gray-400">
    <svg class="animate-spin h-4 w-4 mr-2 text-brand-400" ...></svg>
    Loading...
  </div>
</div>
```

### Route to Template Tracing

ALWAYS trace the full route chain before editing any template:

```
GET /v2/  ->  htmx_views.py: v2_page()  ->  base_page.html (lazy loader)
  ->  hx-get="/v2/partials/dashboard"  ->  dashboard_partial()
  ->  app/templates/htmx/partials/dashboard.html
```

## See Also

- [conversion-optimization](references/conversion-optimization.md)
- [content-copy](references/content-copy.md)
- [distribution](references/distribution.md)
- [measurement-testing](references/measurement-testing.md)
- [growth-engineering](references/growth-engineering.md)
- [strategy-monetization](references/strategy-monetization.md)

## Related Skills

- See the **designing-onboarding-paths** skill for first-run flows and onboarding checklists
- See the **mapping-user-journeys** skill for tracing HTMX routes and identifying dead-ends
- See the **orchestrating-feature-adoption** skill for feature discovery nudges and adoption banners
- See the **clarifying-market-fit** skill for ICP-aligned copy on login page and value props
- See the **frontend-design** skill for Tailwind component patterns and visual hierarchy
- See the **htmx** skill for partial loading, HTMX attributes, and swap targets
- See the **jinja2** skill for template inheritance and Jinja2 rendering in FastAPI
