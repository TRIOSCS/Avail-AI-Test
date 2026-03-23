# Content Copy Reference

## Contents
- Tier label copy conventions
- Pipeline stage microcopy
- Proactive match copy patterns
- Empty state copy for each tier
- Anti-patterns: vague status labels

---

## Tier Label Copy Conventions

Evidence tiers communicate data trustworthiness to buyers. Use specific, provenance-grounded labels — never generic words like "good" or "verified":

| Tier | Display label | Plain-language meaning |
|------|--------------|------------------------|
| T1 | Direct PO | Customer confirmed purchase |
| T2 | Stock List | Vendor-uploaded inventory file |
| T3 | RFQ Response | Vendor replied to an RFQ |
| T4 | AI Parsed | Claude extracted from email — needs review |
| T5 | Web Search | Sourced via AI web search |
| T6 | Market Data | Third-party aggregator |
| T7 | Unverified | Source unknown |

The login page (`app/templates/htmx/login.html`) and proactive list (`app/templates/htmx/partials/proactive/list.html`) show these labels to end users. Always use the canonical labels above, not freeform descriptions.

---

## Pipeline Stage Microcopy

Match button labels and status badges to the stage the offer is in:

```jinja2
{# shared/offer_card.html — status badge copy #}
{% set status_labels = {
  'active':         'Active',
  'pending_review': 'Needs Review',
  'rejected':       'Rejected',
  'sold':           'Sold'
} %}
<span class="...">{{ status_labels.get(offer.status, offer.status|capitalize) }}</span>
```

CTA copy for the pipeline advance buttons:

| Stage transition | Button label | Confirm text |
|-----------------|-------------|--------------|
| Offer → Quote | "Select for Quote" | none |
| Quote → Buy Plan | "Create Buy Plan" | none |
| Proactive → Sent | "Prepare (N)" | none |
| Sent → Converted | "Convert" | none |
| Pending → Approved | "Approve" | none |
| Pending → Rejected | "Reject" | none |

---

## Proactive Match Copy Patterns

The proactive list (`app/templates/htmx/partials/proactive/list.html`) uses sparse, factual copy. Match this register:

```jinja2
{# Empty state — no matches #}
<p class="mt-3 text-sm font-medium text-gray-600">No new proactive matches</p>
<p class="mt-1 text-xs text-gray-400">Matches appear when vendor offers align with customer purchase history</p>

{# Empty state — no sent offers #}
<p class="mt-3 text-sm font-medium text-gray-600">No offers sent yet</p>
<p class="mt-1 text-xs text-gray-400">Select matches and prepare offers from the Matches tab</p>
```

The pattern: one factual `font-medium` line + one contextual `text-gray-400` explanation line. Never use exclamation points or marketing language in empty states — buyers are operational, not aspirational.

---

## Empty State Copy for Each Tier

When an offer list is empty, the copy should tell the buyer what action produces that tier's data:

| Tier | Empty state headline | Sub-line |
|------|---------------------|----------|
| T1–T3 | "No confirmed offers yet" | "Offers appear after RFQ replies are reviewed" |
| T4 | "No offers pending review" | "AI-parsed offers appear here after inbox check" |
| T5–T7 | "No market data available" | "Run a search to pull live market pricing" |

---

## WARNING: Vague Status Labels

**The Problem:**

```jinja2
{# BAD — "processing" tells the buyer nothing about what action is needed #}
<span class="badge">Processing</span>
```

**Why This Breaks:**
1. Buyers don't know if they need to act or wait.
2. "Processing" could mean AI parsing, human review, or a background job — all require different user responses.
3. Support requests increase when status copy doesn't map to a clear next step.

**The Fix:**

```jinja2
{# GOOD — status maps to a specific action or waiting state #}
{% if offer.status == 'pending_review' %}
  <span class="badge amber">Needs Review</span>
{% elif offer.parse_confidence and offer.parse_confidence < 0.5 %}
  <span class="badge red">Low Confidence — Review Required</span>
{% endif %}
```

See the **clarifying-market-fit** skill for broader in-app messaging conventions.
