# Content Copy Reference

## Contents
- Conversion Copy Surfaces
- Microcopy Standards
- Funnel-Stage Copy Patterns
- WARNING: Anti-Patterns
- Template Locations

## Conversion Copy Surfaces

AvailAI's copy lives in Jinja2 templates. There is no CMS. All user-facing text is in `app/templates/`.

| Surface | File | Conversion Goal |
|---------|------|-----------------|
| Login page | `app/templates/htmx/partials/auth/login.html` | Drive Microsoft SSO click |
| Empty state (no requirements) | `app/templates/htmx/partials/requisitions/empty.html` | Prompt first requirement creation |
| RFQ send button | `app/templates/htmx/partials/rfq/*.html` | Reduce friction before send |
| Offer review banner | `app/templates/htmx/partials/offers/*.html` | Drive offer acceptance |
| Proactive match notification | `app/templates/htmx/partials/proactive/*.html` | Prompt match review |

## Microcopy Standards

Every action button, toast, and empty state should answer: "What will happen next, in plain language?"

```html
<!-- GOOD — specific outcome, present tense -->
<button type="submit">Send RFQ to 5 vendors</button>

<!-- BAD — vague, user doesn't know what "Submit" does here -->
<button type="submit">Submit</button>
```

```html
<!-- GOOD — toast confirms the exact action taken -->
<div class="toast">RFQ sent to Avnet, Arrow, and 3 others</div>

<!-- BAD — generic, doesn't reinforce the action -->
<div class="toast">Success</div>
```

## Funnel-Stage Copy Patterns

### Search stage — empty state

When a requisition has no requirements yet, the empty state is the first conversion point.

```html
<!-- app/templates/htmx/partials/requisitions/empty.html -->
<div class="empty-state">
  <h3>Add your first part number</h3>
  <p>Enter an MPN and quantity to search 10 supplier databases in seconds.</p>
  <button hx-get="/v2/requirements/new" hx-target="#modal-container">
    Add Part Number
  </button>
</div>
```

### RFQ stage — vendor selection prompt

```html
<p class="hint-text">
  Select vendors below, then click Send RFQ — replies arrive in the inbox within 24 hours.
</p>
```

### Offer stage — review nudge

```html
<!-- Only show when confidence < 0.8 (flagged for manual review) -->
{% if offer.confidence < 0.8 %}
<div class="review-banner">
  Review this offer — Claude parsed it with {{ (offer.confidence * 100)|int }}% confidence.
</div>
{% endif %}
```

## WARNING: Anti-Patterns

### WARNING: Copy that assumes the user knows the workflow

**The Problem:**
```html
<!-- BAD — "proactive match" is internal jargon -->
<h3>New proactive match</h3>
<p>Review your proactive matches.</p>
```

**Why This Breaks:**
New buyers don't know what "proactive matching" means. They ignore the notification.

**The Fix:**
```html
<!-- GOOD — explains value in buyer language -->
<h3>A vendor has stock for a part you've bought before</h3>
<p>{{ vendor.name }} is offering {{ part.mpn }} × {{ offer.quantity }} at ${{ offer.unit_price }}.</p>
```

## Template Locations

Always trace `router → view function → template_response()` before editing copy. The requisitions parts tab renders from `app/templates/htmx/partials/parts/list.html`, not `requisitions/list.html`.

See the **jinja2** skill for template inheritance and the **htmx** skill for partial swap patterns.
See the **crafting-page-messaging** skill for full CTA and hero copy workflows.
