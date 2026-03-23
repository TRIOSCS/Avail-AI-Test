# Distribution Reference

## Contents
- In-App Distribution Channels
- Navigation and Discoverability
- Feature Surfacing Patterns
- Anti-Patterns

AvailAI has no external marketing channels in this repo — no email campaigns, no blog, no social. Distribution means: getting existing users to discover and use features they haven't tried yet. The primary levers are navigation labels, empty-state copy, and feature intro banners.

## In-App Distribution Channels

| Channel | Location | Lever |
|---------|----------|-------|
| Navigation labels | `app/templates/htmx/base.html` sidebar | Clear, outcome-oriented labels |
| Empty states | `app/templates/htmx/partials/*/list.html` | CTA to activate the feature |
| Feature banners | HTMX partials with `x-show` or `{% if %}` guards | Intro copy for new workflows |
| Toast messages | Success/confirm partials | Next-step nudges after an action |
| Tab labels | Detail view tab bars | Surface adjacent features |

## Navigation and Discoverability

Navigation labels are the highest-reach copy in the app — every user sees them on every visit.

```html
{# app/templates/htmx/base.html — sidebar nav items #}
{# GOOD — outcome-oriented, domain-language #}
<a hx-get="/v2/requisitions">Requisitions</a>
<a hx-get="/v2/proactive">Proactive Matching</a>
<a hx-get="/v2/materials">Material Cards</a>

{# BAD — generic or technical #}
<a hx-get="/v2/req">Requests</a>
<a hx-get="/v2/proactive">Matching Engine</a>
<a hx-get="/v2/materials">Parts DB</a>
```

**Rule:** Nav labels must match the Feature Naming Canon (see `references/content-copy.md`). A buyer who sees "Proactive Matching" in a banner and "Matching Engine" in the nav will not connect them.

## Feature Surfacing Patterns

### Progressive Disclosure via HTMX Tabs

Adjacent features are distributed through tab bars on detail views. Tab labels drive adoption.

```html
{# app/templates/htmx/partials/parts/header.html — tabs surface related workflows #}
<div class="flex gap-1 border-b border-gray-200">
  <button hx-get="/v2/partials/parts/{{ part_id }}/offers"
          hx-target="#part-tabs"
          class="px-3 py-2 text-xs font-medium text-gray-600 hover:text-brand-600
                 border-b-2 border-transparent hover:border-brand-500">
    Offers
  </button>
  <button hx-get="/v2/partials/parts/{{ part_id }}/activity"
          hx-target="#part-tabs"
          class="px-3 py-2 text-xs font-medium text-gray-600 hover:text-brand-600
                 border-b-2 border-transparent hover:border-brand-500">
    Activity
  </button>
</div>
```

### Post-Action Next-Step Nudge

After a key action completes, surface the next workflow step.

```html
{# app/templates/htmx/partials/requisitions/tabs/parse_save_success.html #}
<div class="rounded-lg bg-green-50 border border-green-200 p-4 text-sm text-green-800">
  <p class="font-medium">Offer saved from email reply.</p>
  <p class="mt-1 text-xs text-green-700">
    Ready to add to a buy plan?
    <a hx-get="/v2/partials/buy-plans/create?req_id={{ req_id }}"
       hx-target="#modal-content"
       @click="$dispatch('open-modal')"
       class="underline font-medium">Create Buy Plan</a>
  </p>
</div>
```

### Feature Gate Copy (MVP Mode)

When `MVP_MODE=true`, gated features show an upgrade prompt. Keep it honest — don't oversell.

```html
{# When a feature is gated by MVP_MODE #}
{% if mvp_mode %}
<div class="text-center py-12 text-gray-400">
  <p class="text-sm font-medium">Dashboard is not enabled in this plan.</p>
  <p class="text-xs mt-1">Contact your administrator to enable it.</p>
</div>
{% endif %}
```

## Anti-Patterns

### WARNING: Orphaned Features

**The Problem:** A feature exists (e.g., Excess Inventory at `/v2/excess`) but has no nav link, no empty-state CTA pointing to it, and no post-action nudge from adjacent workflows.

**Why This Breaks:** Users who weren't onboarded to the feature before it shipped will never find it. Search analytics will show zero traffic to the route despite it being built and deployed.

**The Fix:** For every new feature, confirm:
1. Nav label added to sidebar
2. Empty state in the list partial has a CTA
3. At least one adjacent workflow surfaces it via a next-step nudge

### WARNING: Copy That Doesn't Match the Route

```html
{# BAD — link says "Proactive Matching" but goes to materials #}
<a hx-get="/v2/materials">Proactive Matching</a>
```

**Why This Breaks:** HTMX navigation swaps `#main-content` silently. There's no URL change visible to the user (unless `hx-push-url="true"`). If label and destination mismatch, the user lands on unexpected content with no back button context.

**The Fix:** Always trace `hx-get` value to the actual router route before shipping copy changes.

## Related Skills

- See the **orchestrating-feature-adoption** skill for adoption nudges and flag-gating
- See the **mapping-user-journeys** skill for tracing dead-ends in HTMX partials
- See the **htmx** skill for push-url and swap patterns
