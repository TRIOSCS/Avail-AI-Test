# Strategy and Monetization Reference

## Contents
- AvailAI's Business Model
- Value Framing in the UI
- Positioning the Tool Internally
- Feature Prioritization by Value
- Upgrade Path Considerations

---

## AvailAI's Business Model

AvailAI is an **internal productivity tool** for Trio Supply Chain Solutions, not a SaaS product with tiered pricing. There is no monetization surface in the traditional sense.

"Strategy" here means: ensuring the tool's UI communicates the value it delivers to justify continued investment and expansion.

---

## Value Framing in the UI

Every time the UI surfaces a metric or outcome, it should frame it in terms of business value, not raw counts.

### DO: Frame stats as business outcomes

```html
{# dashboard.html — stat cards with value framing #}
<p class="text-3xl font-bold text-brand-600">{{ open_reqs_count }}</p>
<p class="text-sm text-gray-500 mt-1">Open Requisitions</p>
<p class="text-xs text-gray-400 mt-0.5">{{ urgent_count }} need attention</p>
```

The sub-label "{{ urgent_count }} need attention" converts a count into an action signal.

### DON'T: Show counts with no context

```html
<!-- BAD — "23" means nothing without context -->
<p class="text-3xl font-bold text-brand-600">23</p>
<p class="text-sm text-gray-500 mt-1">Open Requisitions</p>
```

---

## Positioning the Tool Internally

When new users arrive at the login page, they should immediately understand what AvailAI does. The current tagline ("Trio Supply Chain Solutions") is a company name, not a product description.

**Positioning hierarchy** for the login page:

```
Line 1 (logo): Visual identity
Line 2 (tagline): What the product does — one sentence
Line 3 (card heading): Action invitation
Line 4 (button): The action
```

```html
{# app/templates/htmx/login.html — recommended copy #}
<img src="/static/avail_logo.png" alt="AVAIL" class="h-14 w-auto mx-auto">
<p class="mt-2 text-sm text-brand-300 tracking-wide">
  Electronic component sourcing, automated.
</p>
...
<h2 class="text-lg font-semibold text-white text-center mb-6">Sign in to continue</h2>
```

See the **clarifying-market-fit** skill for ICP-aligned messaging and value proposition refinement.

---

## Feature Prioritization by Value

Not all features deliver equal value. Prioritize UI real estate by value impact:

| Feature | Value | UI Priority |
|---------|-------|-------------|
| Requisition search (10 APIs) | Core — eliminates manual supplier lookup | Highest — primary CTA on dashboard |
| RFQ send via Graph API | Core — eliminates email composition | High — visible in search results |
| Proactive matching | High — surfaces passive opportunities | Medium — sidebar + pipeline insights |
| Email mining (Claude parsing) | High — automates reply processing | Medium — inbox badge/nudge |
| Buy plans | Medium — tracking workflow | Standard — sidebar nav |
| Excess inventory | Situational | Standard — sidebar nav |

**Implication for UI:** The dashboard Quick Actions should only show the two highest-value entry points: Create Requisition and Search Parts. Proactive and Buy Plans belong in the pipeline insights widget, not as primary CTAs.

---

## MVP Mode Considerations

`MVP_MODE=true` in `.env` gates Dashboard, Enrichment, Teams, and Task Manager.

When MVP mode is active, the sidebar shows a reduced feature set. The landing journey becomes simpler — fewer features to discover, clearer path to value.

**Copy consideration:** In MVP mode, the dashboard welcome copy should acknowledge the focused scope:

```html
{# dashboard.html — MVP mode framing #}
{% if config.mvp_mode %}
<p class="text-center text-sm text-gray-500 mt-1">
  Sourcing engine active — search parts, send RFQs, track vendors.
</p>
{% endif %}
```

---

## Upgrade Path Considerations

If AvailAI ever evolves toward a multi-tenant SaaS model (separate Trio teams or external customers), the current authentication architecture (Azure AD OAuth, single-tenant) would need to change.

The login page glassmorphism card (`bg-white/10 backdrop-blur-sm`) and brand gradient are well-suited for a product marketing context. No visual redesign would be needed — only copy and auth mechanism changes.

**Do not** add pricing or tier UI to the current login page. It is a single-organization tool and adding tier messaging would confuse existing users.
```

---

These 7 files constitute the complete `tuning-landing-journeys` skill. Key decisions made based on the actual codebase:

1. **Grounded in real files** — all examples reference `login.html:31-44`, `dashboard.html:73-91`, actual Tailwind classes and HTMX patterns from the codebase
2. **Honest about the context** — AvailAI is an internal enterprise tool, not a public marketing site; the skill reflects that throughout
3. **No analytics gap ignored** — the WARNING in `measurement-testing.md` and `conversion-optimization.md` flags the complete absence of conversion tracking
4. **Actionable, not generic** — every recommendation maps to a specific file path and code pattern
5. **Cross-references** — links to `clarifying-market-fit`, `designing-onboarding-paths`, `orchestrating-feature-adoption`, `instrumenting-product-metrics`, `playwright`, `htmx`, and `jinja2` skills where relevant
