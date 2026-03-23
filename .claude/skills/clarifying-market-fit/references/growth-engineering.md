# Growth Engineering Reference

## Contents
- Activation Loop Architecture
- Vendor Intelligence Flywheel
- Proactive Matching as a Retention Hook
- Feature Adoption Nudges
- Anti-Patterns

AvailAI's growth model is a **data flywheel**: more searches → richer material cards → better proactive matches → more RFQs → stronger vendor scores → better future searches. Engineering growth means keeping users in this loop.

## Activation Loop Architecture

```
User creates Requisition
       ↓
Runs Search (10 connectors in parallel)
       ↓
Sightings created → Material Cards enriched
       ↓
Vendor scores updated (reply speed, accuracy)
       ↓
Proactive Matching surfaces relevant offers
       ↓
User sends RFQ → AI parses reply → Offer created
       ↓
Buy Plan created → Customer quote sent
       ↓  (loop: more data = better scores)
```

Every step in this loop is a **retention touchpoint**. Engineering growth means reducing friction at each handoff.

## Vendor Intelligence Flywheel

Vendor scores compound with use. Surface this to users — it's a switching cost.

```html
{# app/templates/htmx/partials/vendors/vendor_card.html — score with context #}
<div class="flex items-center gap-2">
  <span class="text-xs font-semibold text-gray-900">
    Score: {{ vendor.reliability_score | round(1) }}
  </span>
  <span class="text-xs text-gray-400">
    based on {{ vendor.reply_count }} RFQ{{ "s" if vendor.reply_count != 1 }}
  </span>
</div>
```

**Why:** Showing "based on N RFQs" signals that the score is earned data, not a static rating. Users trust it more and are incentivized to keep using the platform to improve it.

## Proactive Matching as a Retention Hook

`app/routers/proactive.py` powers the Proactive Matching tab. This is the highest-value retention surface — it delivers value without user action.

```python
# Trigger proactive match notification after new offers are ingested
# app/jobs/proactive_refresh.py
from loguru import logger

async def run_proactive_refresh(db: Session) -> None:
    matches = await proactive_service.find_matches(db)
    if matches:
        logger.info(
            "proactive_matches_found",
            extra={"match_count": len(matches), "customer_count": len({m.customer_id for m in matches})}
        )
```

**Copy for the proactive tab empty state:**

```html
{# app/templates/htmx/partials/proactive/list.html — empty state #}
{% if not matches %}
<div class="text-center py-10">
  <p class="text-sm font-medium text-gray-700">No matches yet</p>
  <p class="text-xs text-gray-400 mt-1">
    As vendors offer parts you've sourced before, they'll appear here automatically.
  </p>
</div>
{% endif %}
```

## Feature Adoption Nudges

Add nudges at natural transition points — not as interstitials that block workflow.

```html
{# After first successful search — nudge toward RFQ #}
{% if req.sightings_count > 0 and req.rfqs_sent == 0 %}
<div class="mt-3 p-3 bg-brand-50 border border-brand-200 rounded text-xs text-brand-700"
     x-data="{ show: true }" x-show="show">
  <div class="flex justify-between items-start">
    <p>
      <strong>Send an RFQ</strong> — select vendors from your results and
      get price confirmations in your inbox.
    </p>
    <button @click="show = false" class="text-brand-400 ml-2">✕</button>
  </div>
</div>
{% endif %}
```

## WARNING: Friction at the Requisition→Search Handoff

### The Problem

```html
{# BAD — two-step form before user sees any value #}
<form>
  <input name="customer" placeholder="Customer name" required>
  <input name="due_date" type="date" required>
  <input name="parts" placeholder="Part numbers">
  <button type="submit">Create Requisition</button>
</form>
```

**Why This Breaks:** Requiring customer name and due date before showing search results adds 30 seconds of overhead on a user's first session. They leave before seeing the sourcing engine work. Required fields should be minimized at creation — fill details later.

**The Fix:** Make only the parts field required at creation. Prompt for metadata after the first search run.

## Related Skills

- See the **orchestrating-feature-adoption** skill for nudge placement patterns
- See the **designing-onboarding-paths** skill for first-run activation flow
- See the **htmx** skill for dismissible nudge wiring with Alpine.js
