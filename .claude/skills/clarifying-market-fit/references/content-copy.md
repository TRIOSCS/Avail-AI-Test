# Content Copy Reference

## Contents
- Voice and Tone Guidelines
- Microcopy Patterns
- Feature Description Templates
- Anti-Patterns

AvailAI's copy lives in Jinja2 templates and Python constants. There is no CMS. Copy changes are code changes — they go through the same review/deploy process.

## Voice and Tone Guidelines

**Audience:** Procurement buyers under deadline pressure. They are technical enough to read part datasheets but not software engineers.

| Tone to use | Tone to avoid |
|-------------|---------------|
| Direct, action-oriented | Fluffy, marketing-speak |
| Specific numbers ("10 suppliers") | Vague claims ("many suppliers") |
| Workflow-native language ("RFQ", "BOM", "MPN") | Consumer tech language ("sync", "dashboard") |
| Present-tense outcomes | Future-tense promises |

## Microcopy Patterns

### Button Labels

```html
{# GOOD — action + object #}
<button>Send RFQ</button>
<button>Run Search</button>
<button>Create Requisition</button>

{# BAD — vague verbs #}
<button>Submit</button>
<button>Go</button>
<button>Process</button>
```

### Status Badges

AvailAI uses StrEnum constants from `app/constants.py`. Copy in status badges must match these exactly.

```html
{# app/templates/htmx/partials/requisitions/response_status_badge.html #}
{% set badge_map = {
  "pending":  ("Pending",     "bg-yellow-100 text-yellow-700"),
  "replied":  ("Reply In",    "bg-green-100  text-green-700"),
  "no_reply": ("No Reply",    "bg-gray-100   text-gray-500"),
} %}
{% set label, cls = badge_map.get(status, ("Unknown", "bg-gray-100 text-gray-400")) %}
<span class="px-2 py-0.5 rounded text-xs font-medium {{ cls }}">{{ label }}</span>
```

**DON'T** write status copy that diverges from the enum values — it creates user confusion when the same state shows different labels in different views.

### Confidence Score Copy

The AI parser (`app/services/response_parser.py`) returns a 0–1 confidence score. Surface it clearly:

```html
{# Confidence threshold copy — mirrors logic in email_service.py #}
{% if confidence >= 0.8 %}
  <span class="text-green-600 text-xs">Auto-applied</span>
{% elif confidence >= 0.5 %}
  <span class="text-yellow-600 text-xs">Needs review</span>
{% else %}
  <span class="text-red-500 text-xs">Low confidence — verify manually</span>
{% endif %}
```

## Feature Description Templates

Use these when writing intro copy for new features:

```
[Feature name] [verb] [object] so that [buyer outcome].

Example:
"Proactive Matching surfaces vendor offers against your purchase history
so that you catch relevant stock before it's gone."
```

```
When [trigger condition], AvailAI [action].

Example:
"When a vendor replies to your RFQ, AvailAI parses the email with AI
and creates an offer automatically — no copy-paste required."
```

## WARNING: Present-Tense vs. Conditional Copy

### The Problem

```html
{# BAD — conditional tense weakens the value claim #}
<p>AvailAI can help you find parts faster.</p>
```

**Why This Breaks:** "Can help" is hedged. Buyers evaluate tools against tight deadlines — uncertainty in copy creates doubt.

**The Fix:**

```html
{# GOOD — present tense, specific claim #}
<p>AvailAI searches 10 supplier networks in parallel, in under 30 seconds.</p>
```

## Related Skills

- See the **jinja2** skill for template macro and filter patterns
- See the **designing-onboarding-paths** skill for first-run copy flows
