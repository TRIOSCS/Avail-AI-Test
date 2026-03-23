# Engagement & Adoption Reference

## Contents
- Toast Nudge System
- Contextual Feature Discovery
- Suppressing Repeat Nudges
- DO / DON'T Pairs
- Anti-Patterns

---

## Toast Nudge System

The Alpine `$store.toast` store is the only in-app notification channel. It auto-dismisses after
4 seconds. Use it for one-line contextual tips after key actions.

```javascript
// app/static/htmx_app.js — store definition
Alpine.store('toast', {
  message: '',
  type: 'info',   // 'success' | 'error' | 'info'
  show: false,
})
```

**Trigger from a Jinja2 template after an action response:**

```html
<!-- After creating first requisition — inject with HTMX response -->
<script>
  Alpine.store('toast').message = 'Requisition created! Search will run across all 10 sources.';
  Alpine.store('toast').type = 'success';
  Alpine.store('toast').show = true;
</script>
```

**Trigger from a FastAPI route (in HTMX partial response):**

```python
# app/routers/htmx_views.py
html = templates.TemplateResponse("htmx/partials/requisitions/created.html", context)
# Append nudge script to the response body
nudge = "<script>Alpine.store('toast').message='Tip: Pin key vendors to surface them faster.';" \
        "Alpine.store('toast').type='info';Alpine.store('toast').show=true;</script>"
return HTMLResponse(html.body.decode() + nudge)
```

## Contextual Feature Discovery

Feature discovery works best at the moment of relevance — show the nudge immediately after
the action that unlocks the feature.

```html
<!-- In requisitions/detail.html — show RFQ discovery after first sighting -->
{% if sightings|length > 0 and not user_has_sent_rfq %}
<div class="bg-blue-50 border border-blue-200 rounded-lg p-4 mb-4 flex items-start gap-3">
  <svg class="w-5 h-5 text-blue-500 mt-0.5 shrink-0"><!-- info icon --></svg>
  <div>
    <p class="text-sm font-medium text-blue-800">Ready to send RFQs?</p>
    <p class="text-sm text-blue-600">Select vendors from the results below and click "Send RFQ" to request quotes.</p>
  </div>
  <button
    class="ml-auto text-blue-400 hover:text-blue-600"
    @click="$el.closest('.bg-blue-50').remove()"
    aria-label="Dismiss"
  >✕</button>
</div>
{% endif %}
```

## Suppressing Repeat Nudges

**Problem:** Showing the same nudge on every visit is noise. Use `SystemConfig` to track
dismissals server-side (persistent) or Alpine `$persist` for session-level suppression.

**Session-level suppression (Alpine persist):**

```html
<div
  x-data="{ dismissed: $persist(false).as('rfq_nudge_dismissed') }"
  x-show="!dismissed"
>
  <p class="text-sm text-blue-600">Ready to send RFQs?</p>
  <button @click="dismissed = true">Dismiss</button>
</div>
```

**Persistent suppression via SystemConfig (for high-value nudges):**

```python
# app/routers/htmx_views.py
from app.models.config import SystemConfig

def has_seen_nudge(db: Session, user_id: int, nudge_key: str) -> bool:
    key = f"nudge_seen:{user_id}:{nudge_key}"
    row = db.query(SystemConfig).filter_by(key=key).first()
    return row is not None

def mark_nudge_seen(db: Session, user_id: int, nudge_key: str) -> None:
    key = f"nudge_seen:{user_id}:{nudge_key}"
    if not db.query(SystemConfig).filter_by(key=key).first():
        db.add(SystemConfig(key=key, value="1", description=f"Nudge seen: {nudge_key}"))
        db.commit()
```

## DO / DON'T Pairs

**DO: Time nudges to the moment of relevance**
```python
# After first search returns results
if len(results) > 0 and req.rfq_count == 0:
    context["show_rfq_discovery"] = True
```

**DON'T: Show nudges on every page load unconditionally**
```html
<!-- BAD — this fires on every dashboard load -->
<script>
  Alpine.store('toast').message = 'Did you know you can send RFQs?';
  Alpine.store('toast').show = true;
</script>
```
Why: Permanent noise trains users to ignore all toasts, including critical errors.

**DO: Give users an explicit dismiss action**
```html
<button @click="$el.closest('[x-data]').remove()" aria-label="Dismiss tip">✕</button>
```

**DON'T: Auto-dismiss feature discovery banners**
Toasts auto-dismiss (4s) — that's fine for confirmations. Feature discovery banners must
be user-dismissed because users are mid-task and may not have time to read them immediately.

## Anti-Patterns

### WARNING: Using Toast for Multi-Step Guidance

**The Problem:** A toast shows one line for 4 seconds. Using it for step-by-step instructions
("First do X, then Y, then Z") means users see truncated text or miss the message entirely.

**The Fix:** Use an inline banner (dismissible `<div>`) for multi-step guidance. Reserve toasts
for single-line confirmations and tips.

### WARNING: Nudges Without a Clear Action

A nudge that says "Try Proactive Matching!" with no link or CTA is dead weight. Every nudge
must answer: "what do I do next?" Link to the feature or provide an action button.

See the **htmx** skill for HTMX partial swap patterns used in feature discovery flows.
See the **redis** skill for caching nudge-seen state to avoid repeated DB queries.
