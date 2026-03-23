# In-App Guidance Reference

## Contents
- Command center as primary guidance surface
- SSE for real-time action nudges
- HTMX partial swap patterns for guidance
- system_config for dismissed state
- Anti-patterns

---

## Command Center: Primary Guidance Surface

`/api/command-center/actions` aggregates the four highest-priority action items. This is the correct place to surface guidance — not modals, not banners on unrelated pages.

```python
# app/routers/command_center.py — existing endpoint
# Returns:
# {
#   "stale_rfqs": [...],          # sent >48h, no response
#   "pending_quotes": [...],      # sent >5 days, no result
#   "offers_needing_review": [...],  # status="needs_review"
#   "todays_responses": [...]     # created since midnight UTC
# }
```

Render each category as a collapsed HTMX section. Use `hx-get` to lazy-load the count badge — don't block page load on it.

```html
<!-- app/templates/htmx/partials/command_center/actions.html -->
<div hx-get="/api/command-center/actions"
     hx-trigger="load"
     hx-target="#action-list"
     hx-swap="innerHTML">
  <div class="animate-pulse h-4 w-32 bg-slate-200 rounded"></div>
</div>
<div id="action-list"></div>
```

---

## SSE for Real-Time Nudges

The `/api/events/stream` SSE endpoint pushes to a per-user channel. Use it for "new response received" nudges that don't require page reload.

```javascript
// app/static/htmx_app.js — wire SSE on login
// HTMX extension: hx-ext="sse" sse-connect="/api/events/stream"
// Listen for specific event types:
document.body.addEventListener('htmx:sseMessage', (e) => {
  if (e.detail.type === 'rfq_response') {
    // Update badge count without full reload
    htmx.trigger('#response-badge', 'refresh');
  }
});
```

---

## Dismissal State via system_config

Store dismissal of one-time guidance in `system_config`. Use a namespaced key.

```python
# app/services/guidance_service.py
from app.models.config import SystemConfig
from sqlalchemy.orm import Session

GUIDANCE_KEY = "guidance_dismissed_{name}_user_{user_id}"

def dismiss_guidance(db: Session, name: str, user_id: int) -> None:
    key = GUIDANCE_KEY.format(name=name, user_id=user_id)
    if not db.get(SystemConfig, key):
        db.add(SystemConfig(key=key, value="true"))
        db.commit()

def is_guidance_dismissed(db: Session, name: str, user_id: int) -> bool:
    key = GUIDANCE_KEY.format(name=name, user_id=user_id)
    return db.get(SystemConfig, key) is not None
```

Pass `guidance_dismissed` as a boolean to the template context. Never compute this in the template.

---

## HTMX Partial Swap for Guidance Banners

Guidance banners should self-remove on dismiss without a page reload. Use `hx-delete` + `hx-swap="outerHTML"` returning an empty 200.

```html
<!-- Guidance banner partial -->
{% if not guidance_dismissed %}
<div id="guidance-rfq-tip" class="rounded-md bg-blue-50 p-3 text-sm text-blue-800">
  <p>Tip: Send RFQs to multiple vendors to get competitive pricing.</p>
  <button
    hx-post="/api/guidance/dismiss/rfq_tip"
    hx-target="#guidance-rfq-tip"
    hx-swap="outerHTML"
    class="mt-1 text-xs underline">
    Got it
  </button>
</div>
{% endif %}
```

```python
# Router returns empty string to wipe the element
@router.post("/api/guidance/dismiss/{name}")
async def dismiss_guidance_endpoint(name: str, user=Depends(require_user), db=Depends(get_db)):
    dismiss_guidance(db=db, name=name, user_id=user.id)
    return HTMLResponse("")
```

---

## Anti-Patterns

**NEVER use JavaScript alerts or `window.confirm()` for guidance.** They block the thread and users dismiss them without reading.

**NEVER show the same guidance banner on every page load.** Check `system_config` dismissal state server-side. Repeated guidance destroys trust.

**NEVER use modals for passive information.** Modals are for decisions that require a response. Tips and nudges belong inline.

---

## Related Skills

- See the **htmx** skill for `hx-swap="outerHTML"` and `hx-trigger` patterns
- See the **jinja2** skill for passing boolean context flags to templates
- See the **designing-onboarding-paths** skill for empty-state first-run patterns
