# Strategy and Monetization Reference

## Contents
- Business Model Context
- MVP Mode and Feature Gating
- Expansion Messaging Patterns
- Pricing Copy Guidelines
- Anti-Patterns

AvailAI is a single-tenant B2B SaaS deployed per customer. Monetization is negotiated sales (not self-serve checkout). The in-app strategy levers are: demonstrating ROI to the buyer's manager, gating premium features via `MVP_MODE`, and making expansion value visible.

## Business Model Context

- **Deployment:** Single-tenant Docker Compose on DigitalOcean — one instance per customer
- **Pricing:** Negotiated; no in-app payment flow
- **Expansion signal:** Number of active users, requisitions processed, RFQs sent, AI-parsed offers
- **Churn risk:** Low switching cost if the tool isn't embedded in daily workflow

Monetization messaging means: making the ROI visible to the economic buyer (the manager, not the daily user).

## MVP Mode and Feature Gating

`MVP_MODE=true` in `.env` disables Dashboard, Enrichment, Teams, and Task Manager. Gated features show a soft prompt — not a paywall, since pricing is handled outside the app.

```html
{# Pattern for any MVP-gated feature #}
{% if mvp_mode %}
<div class="text-center py-16 px-4">
  <p class="text-sm font-semibold text-gray-700">
    {{ feature_name }} is not enabled on your current plan.
  </p>
  <p class="text-xs text-gray-500 mt-1">
    Contact your Trio Supply Chain administrator to enable it.
  </p>
</div>
{% else %}
  {# Feature content #}
{% endif %}
```

**DO:** Name the specific feature that's gated. "This feature" is ambiguous.

**DON'T:** Show pricing in the gate copy — pricing is handled by the sales team. The app should not contradict what's been negotiated.

## Expansion Messaging Patterns

### ROI Signals on Key Views

Surface metrics that a manager would care about. These make the business case visible without a dedicated analytics dashboard.

```html
{# app/templates/htmx/partials/requisitions/list.html — stats row #}
<div class="grid grid-cols-3 gap-4 mb-4">
  <div class="bg-white rounded-lg border border-gray-200 px-4 py-3 text-center">
    <p class="text-2xl font-bold text-gray-900">{{ stats.requisitions_this_month }}</p>
    <p class="text-xs text-gray-500 mt-0.5">Requisitions this month</p>
  </div>
  <div class="bg-white rounded-lg border border-gray-200 px-4 py-3 text-center">
    <p class="text-2xl font-bold text-gray-900">{{ stats.rfqs_sent }}</p>
    <p class="text-xs text-gray-500 mt-0.5">RFQs sent</p>
  </div>
  <div class="bg-white rounded-lg border border-gray-200 px-4 py-3 text-center">
    <p class="text-2xl font-bold text-gray-900">{{ stats.offers_auto_parsed }}</p>
    <p class="text-xs text-gray-500 mt-0.5">Offers auto-parsed by AI</p>
  </div>
</div>
```

### Value Milestone Messaging

```html
{# Surface a milestone when a team crosses a threshold #}
{% if stats.offers_auto_parsed >= 100 %}
<div class="rounded-md bg-brand-50 border border-brand-100 px-4 py-3 text-sm text-brand-700 mb-4">
  <span class="font-medium">100+ offers auto-parsed.</span>
  Your team has eliminated {{ stats.offers_auto_parsed }} manual copy-paste sessions.
</div>
{% endif %}
```

## Pricing Copy Guidelines

There is no pricing page in this repo. If a pricing surface is ever added (e.g., a feature comparison modal), follow these rules:

1. **Outcome-based tiers** — name tiers by what the buyer can do, not by seat count. "Sourcing + RFQ" vs. "Full Platform" beats "Starter / Pro / Enterprise".
2. **Avoid feature laundry lists** — buyers care about outcomes, not checkbox comparisons.
3. **Never show prices in templates** — prices change, templates don't. Pass price values from config or a database record.

```html
{# GOOD — outcome-based tier description #}
<p class="text-sm text-gray-700">
  <span class="font-medium">Full Platform</span> —
  Sourcing Engine, RFQ Workflow, Proactive Matching, and AI inbox mining.
</p>

{# BAD — feature checkbox list #}
<ul class="text-xs text-gray-600 space-y-1">
  <li>✓ BrokerBin connector</li>
  <li>✓ Nexar connector</li>
  <li>✓ DigiKey connector</li>
  ...
</ul>
```

## WARNING: Showing Version Numbers in Marketing Copy

**The Problem:**

```html
{# BAD — version number in user-facing copy #}
<p class="text-xs text-gray-400">AvailAI v{{ app_version }} — now with AI parsing</p>
```

**Why This Breaks:** Version numbers anchor the user's perception. "v1.2" implies early/incomplete; "v3.1" means nothing to a buyer. It also creates maintenance overhead — every release requires copy review.

**The Fix:** Surface the capability, not the version.

```html
{# GOOD #}
<p class="text-xs text-gray-400">Powered by Claude AI — parses RFQ replies automatically</p>
```

The `APP_VERSION` constant (`app/config.py`) is for internal logging and API headers, not user-facing copy.

## Related Skills

- See the **clarifying-market-fit** skill for ICP and value narrative positioning
- See the **orchestrating-feature-adoption** skill for feature flag gating patterns
- See the **designing-onboarding-paths** skill for first-run expansion hooks
```

---

These are the 7 files to write into `.claude/skills/crafting-page-messaging/`. The skill is distinct from `clarifying-market-fit` — it's execution-focused (how to write the copy) rather than strategy-focused (what to say and to whom). Key design decisions:

- **Login page** is treated as the primary conversion surface since there's no external marketing site
- **Empty states** are the highest-leverage copy surface for activation
- **StrEnum alignment** is called out as a hard constraint — copy must match `app/constants.py` values
- **Confidence thresholds** from `app/email_service.py` are documented for microcopy consistency
- **No analytics SDK** is noted in measurement — measurement is via Loguru + DB queries
