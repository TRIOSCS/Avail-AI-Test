---
name: crafting-page-messaging
description: |
  Writes conversion-focused messaging for pages and key CTAs in the AvailAI sourcing platform.
  Use when: writing or revising copy for the login page, empty states, feature banners, action buttons,
  toast confirmations, modal headings, or any user-facing text where clarity and conversion matter.
  Covers hero copy, CTA labels, onboarding hooks, status microcopy, and confirmation messaging.
allowed-tools: Read, Edit, Write, Glob, Grep, Bash, mcp__playwright__browser_navigate, mcp__playwright__browser_snapshot, mcp__playwright__browser_take_screenshot, mcp__playwright__browser_click, mcp__playwright__browser_evaluate, mcp__playwright__browser_wait_for
---

# Crafting Page Messaging

AvailAI has no external marketing site — all persuasive surfaces live inside the app: the login page, empty states, feature intro banners, and action CTAs. Copy is code: it lives in Jinja2 templates and Python constants, ships through the same deploy pipeline, and must stay in sync with StrEnum values in `app/constants.py`.

The ICP is **procurement buyers at distributors and CMs** — deadline-driven, fluent in domain language (BOM, MPN, RFQ), allergic to vague promises. Every line of copy must earn its space with a specific outcome claim.

## Key Messaging Surfaces

| File | Surface | Conversion Goal |
|------|---------|----------------|
| `app/templates/htmx/login.html` | Login page hero + CTA | First-impression sign-in |
| `app/templates/htmx/partials/*/list.html` | Empty states | Feature activation for new users |
| `app/templates/htmx/partials/proactive/list.html` | Proactive matching intro | Workflow adoption |
| `app/templates/htmx/base_page.html` | Shell / nav labels | Navigation clarity |
| `app/templates/htmx/partials/parts/header.html` | Parts header CTAs | Search to RFQ conversion |

## Quick Start

### Login Page Hero Copy

```html
{# app/templates/htmx/login.html — hero section #}
<div class="text-center">
  <img src="/static/avail_logo.png" alt="AVAIL" class="h-14 w-auto mx-auto">
  <h1 class="mt-4 text-2xl font-bold text-white">
    Source parts. Close quotes. Skip the spreadsheets.
  </h1>
  <p class="mt-2 text-sm text-brand-300">
    Search 10 supplier networks in parallel. Auto-parse RFQ replies with AI.
  </p>
</div>
```

### Empty-State CTA Hook

```html
{# app/templates/htmx/partials/requisitions/list.html — zero-state #}
{% if not requisitions %}
<div class="text-center py-16">
  <p class="text-sm font-semibold text-gray-900">No requisitions yet</p>
  <p class="text-xs text-gray-500 mt-1">
    Paste a BOM or part number to search 10 supplier networks at once.
  </p>
  <button hx-get="/v2/partials/requisitions/create-form"
          hx-target="#modal-content"
          @click="$dispatch('open-modal')"
          class="mt-4 px-4 py-2 text-xs font-medium text-white bg-brand-500 rounded-lg">
    Create Requisition
  </button>
</div>
{% endif %}
```

### Action Button Labels

```html
{# GOOD — verb + domain object #}
<button>Send RFQ</button>
<button>Run Search</button>
<button>Create Requisition</button>
<button>Parse Email</button>
<button>Add to Buy Plan</button>

{# BAD — vague or generic #}
<button>Submit</button>
<button>Process</button>
<button>Continue</button>
```

### Toast / Confirmation Copy

```html
{# app/templates/htmx/partials/follow_ups/sent_success.html #}
<div class="text-sm text-green-700 font-medium">
  RFQ sent to {{ vendor_count }} vendor{{ 's' if vendor_count != 1 else '' }}.
  Replies will be parsed automatically.
</div>
```

### Confidence-Threshold Microcopy

```html
{# Mirror thresholds from app/email_service.py: 0.8 auto-apply, 0.5 needs review #}
{% if confidence >= 0.8 %}
  <span class="text-xs text-green-600 font-medium">Auto-applied</span>
{% elif confidence >= 0.5 %}
  <span class="text-xs text-yellow-600 font-medium">Needs review</span>
{% else %}
  <span class="text-xs text-red-500">Low confidence — verify manually</span>
{% endif %}
```

## Messaging Rules

1. **Lead with outcome, not mechanism.** "Find 10 suppliers in 30 seconds" > "Multi-connector search"
2. **Use domain language.** BOM, MPN, RFQ, lead time — buyers recognize these; consumer language creates friction.
3. **Present tense only.** "AvailAI parses replies automatically" > "AvailAI can help parse replies"
4. **Quantify where possible.** "10 supplier networks", "0.8 confidence", "every 30 minutes" beat vague claims.
5. **Match copy to StrEnum labels.** Status badge text must align with constants in `app/constants.py`.

## Copy Update Workflow

```
- [ ] Read the target template: trace router to view to template_response()
- [ ] Check which StrEnum values are displayed (app/constants.py)
- [ ] Draft copy using outcome-first, domain-language pattern
- [ ] Apply present tense; remove "can", "may", "help you"
- [ ] Test in browser: npm run dev, navigate to the surface
- [ ] Verify no placeholder text remains
- [ ] Run: ruff check app/ if Python constants changed
```

## See Also

- [conversion-optimization](references/conversion-optimization.md)
- [content-copy](references/content-copy.md)
- [distribution](references/distribution.md)
- [measurement-testing](references/measurement-testing.md)
- [growth-engineering](references/growth-engineering.md)
- [strategy-monetization](references/strategy-monetization.md)

## Related Skills

- See the **clarifying-market-fit** skill for ICP positioning and value narrative foundations
- See the **designing-onboarding-paths** skill for first-run empty-state UI patterns
- See the **orchestrating-feature-adoption** skill for feature discovery banners and nudges
- See the **frontend-design** skill for Tailwind component styling
- See the **jinja2** skill for template macros, filters, and inheritance
- See the **htmx** skill for wiring CTAs to HTMX partials
