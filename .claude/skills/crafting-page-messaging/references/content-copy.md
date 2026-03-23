# Content Copy Reference

## Contents
- Voice and Tone
- Microcopy Patterns
- Feature Naming Canon
- Status Badge Copy
- Anti-Patterns

AvailAI's copy lives entirely in Jinja2 templates and Python constants. There is no CMS. Copy changes are code changes — they go through `ruff`, `mypy`, and the full deploy pipeline.

## Voice and Tone

**Audience:** Procurement buyers under deadline pressure. Technical enough to read datasheets. Not software engineers.

| Use | Avoid |
|-----|-------|
| Direct, action-oriented | Fluffy marketing-speak |
| Specific numbers ("10 suppliers", "30 seconds") | Vague claims ("many suppliers", "fast") |
| Domain language: RFQ, BOM, MPN, lead time | Consumer tech: "sync", "dashboard", "unlock" |
| Present-tense outcomes | Future-tense promises ("will help you") |
| Second person ("your parts", "your vendors") | Third person ("the user's parts") |

## Microcopy Patterns

### Button Labels — Verb + Object

```html
{# GOOD #}
<button>Send RFQ</button>           {# verb: send, object: RFQ #}
<button>Run Search</button>         {# verb: run, object: search #}
<button>Create Requisition</button> {# verb: create, object: requisition #}
<button>Parse Email</button>        {# verb: parse, object: email #}

{# BAD — missing the object #}
<button>Submit</button>
<button>Go</button>
<button>OK</button>
```

### Status Badge Copy

Status badge text MUST match the StrEnum values in `app/constants.py`. Divergence creates confusion when the same state appears with different labels in different views.

```html
{# app/templates/htmx/partials/requisitions/response_status_badge.html #}
{% set badge_map = {
  "pending":  ("Pending",   "bg-yellow-100 text-yellow-700"),
  "replied":  ("Reply In",  "bg-green-100 text-green-700"),
  "no_reply": ("No Reply",  "bg-gray-100 text-gray-500"),
  "bounced":  ("Bounced",   "bg-red-100 text-red-600"),
} %}
{% set label, cls = badge_map.get(status, ("Unknown", "bg-gray-100 text-gray-400")) %}
<span class="px-2 py-0.5 rounded text-xs font-medium {{ cls }}">{{ label }}</span>
```

### Confidence Score Copy

The AI parser in `app/services/response_parser.py` returns a 0–1 score. Surface the thresholds consistently — they match the auto-apply logic in `app/email_service.py`.

```html
{% if confidence >= 0.8 %}
  <span class="text-xs text-green-600 font-medium">Auto-applied</span>
{% elif confidence >= 0.5 %}
  <span class="text-xs text-yellow-600 font-medium">Needs review</span>
{% else %}
  <span class="text-xs text-red-500">Low confidence — verify manually</span>
{% endif %}
```

### Toast / Success Messages

```html
{# Specific: name the outcome and what happens next #}
<p class="text-sm text-green-700 font-medium">
  RFQ sent to {{ vendor_count }} vendor{{ 's' if vendor_count != 1 else '' }}.
  Replies will be parsed automatically.
</p>

{# NOT: "Success!" or "Done" — these tell the user nothing #}
```

### Pluralization

```html
{# Always handle singular/plural — Jinja2 ternary #}
{{ result_count }} result{{ 's' if result_count != 1 else '' }}
{{ vendor_count }} vendor{{ 's' if vendor_count != 1 else '' }}
{{ part_count }} part{{ 's' if part_count != 1 else '' }}
```

## Feature Naming Canon

Use these names consistently across all copy. Never improvise synonyms.

```python
# app/constants.py — feature name reference
FEATURE_NAMES = {
    "search":    "Sourcing Engine",     # NOT "search", "API search", "find parts"
    "rfq":       "RFQ Workflow",        # NOT "email", "send RFQ", "outreach"
    "proactive": "Proactive Matching",  # NOT "matching", "offers", "alerts"
    "materials": "Material Cards",      # NOT "parts database", "inventory"
    "excess":    "Excess Inventory",    # NOT "surplus", "overstock"
}
```

## Feature Description Template

Use this structure for intro copy on any new feature banner or empty state:

```
[Feature name] [verb] [object] so that [buyer outcome].

Example:
"Proactive Matching surfaces vendor offers against your purchase history
so that you catch relevant stock before it's gone."
```

```
When [trigger], AvailAI [action] — no [manual step] required.

Example:
"When a vendor replies to your RFQ, AvailAI parses the email and creates
an offer automatically — no copy-paste required."
```

## WARNING: Inconsistent Status Labels

**The Problem:**

```html
{# View A — uses StrEnum value #}
<span>pending</span>

{# View B — improvised synonym #}
<span>Awaiting Reply</span>
```

**Why This Breaks:** The same requisition shows different status text depending on which view the buyer is in. This erodes trust in the data. Buyers will wonder if these are actually different states.

**The Fix:** Always use `badge_map` from the shared partial or match `app/constants.py` exactly.

## Related Skills

- See the **jinja2** skill for template macro and filter patterns
- See the **clarifying-market-fit** skill for ICP positioning context
- See the **designing-onboarding-paths** skill for first-run copy flows
