# Feedback & Insights Reference

## Contents
- Notification Model as Feedback Surface
- Trouble Ticket System
- In-App Feedback Patterns
- Using ActivityLog for Signal Mining
- DO / DON'T Pairs

---

## Notification Model as Feedback Surface

`app/models/notification.py` stores admin notifications from system events (diagnosed,
prompt_ready, escalated, fixed, failed). This is currently wired to the trouble ticket system
only. It's the right model to extend for user feedback signals.

```python
# app/models/notification.py — existing structure
class Notification(Base):
    user_id: int
    ticket_id: int | None
    event_type: str   # "diagnosed" | "prompt_ready" | "escalated" | "fixed" | "failed"
    title: str
    body: str
    is_read: bool = False
    created_at: datetime
```

**Extend for feature feedback:**

```python
# To capture in-app thumbs-up/down on AI parsing accuracy
class Notification(Base):
    ...
    event_type: str  # add: "feedback_positive" | "feedback_negative"
    reference_type: str | None   # "offer", "requisition", "sighting"
    reference_id: int | None
```

## Trouble Ticket System

The settings page (`app/templates/htmx/partials/settings/index.html`) has a Tickets tab.
This is the existing in-app feedback mechanism for system issues. Route: likely
`/v2/settings?tab=tickets`.

Use it as the model for adding lightweight feature feedback forms — same tab-based pattern,
same HTMX lazy-load on tab click.

```html
<!-- Settings tab pattern — reuse for a "Feedback" tab -->
<button
  @click="activeTab = 'feedback'"
  :class="activeTab === 'feedback' ? 'border-brand-500 text-brand-600' : 'border-transparent'"
  class="px-4 py-2 border-b-2 text-sm font-medium"
>
  Feedback
</button>

<div x-show="activeTab === 'feedback'"
     hx-get="/v2/partials/settings/feedback"
     hx-trigger="intersect once">
</div>
```

## In-App Feedback Patterns

For lightweight signal collection (was this result useful?), use a minimal thumbs pattern
injected into the relevant partial.

```html
<!-- Inline feedback on AI-parsed offer accuracy -->
<div x-data="{ voted: false }" class="flex items-center gap-2 mt-2">
  <span class="text-xs text-gray-500">Was this parsed correctly?</span>
  <button
    x-show="!voted"
    @click="
      voted = true;
      htmx.ajax('POST', '/api/feedback/offer/{{ offer.id }}/positive', {target: 'this', swap: 'none'})
    "
    class="text-xs text-green-600 hover:text-green-800"
    aria-label="Yes, correct"
  >👍</button>
  <button
    x-show="!voted"
    @click="
      voted = true;
      htmx.ajax('POST', '/api/feedback/offer/{{ offer.id }}/negative', {target: 'this', swap: 'none'})
    "
    class="text-xs text-red-500 hover:text-red-700"
    aria-label="No, incorrect"
  >👎</button>
  <span x-show="voted" class="text-xs text-gray-400">Thanks for the feedback</span>
</div>
```

```python
# app/routers/feedback.py (to be created)
@router.post("/api/feedback/offer/{offer_id}/{sentiment}")
async def record_offer_feedback(
    offer_id: int,
    sentiment: str,  # "positive" | "negative"
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    db.add(Notification(
        user_id=user.id,
        event_type=f"feedback_{sentiment}",
        title=f"Offer {offer_id} feedback",
        body=f"User rated offer parsing as {sentiment}",
        reference_type="offer",
        reference_id=offer_id,
    ))
    db.commit()
    return Response(status_code=204)
```

## Using ActivityLog for Signal Mining

`ActivityLog` is already a rich signal source for understanding how users interact with features.
Query it to find adoption blockers before adding explicit feedback forms.

```python
# Find requisitions that were searched but never sent an RFQ (possible friction point)
from sqlalchemy import select, not_, exists

stalled = db.execute(
    select(Requisition)
    .where(Requisition.sighting_count > 0)
    .where(
        not_(
            exists(
                select(ActivityLog.id)
                .where(ActivityLog.requisition_id == Requisition.id)
                .where(ActivityLog.activity_type == "email")
                .where(ActivityLog.direction == "outbound")
            )
        )
    )
    .limit(20)
).scalars().all()
```

## DO / DON'T Pairs

**DO: Start with signal mining from existing data before adding forms**
Unstructured feedback forms have low response rates and high noise. ActivityLog + Notification
data gives you behavioral signal without asking users anything.

**DON'T: Interrupt the workflow with a satisfaction survey**
```html
<!-- BAD — modal on every search result page -->
<div x-init="setTimeout(() => showSurvey = true, 3000)">
  How satisfied are you with these results? (1-5)
</div>
```
Why: Interruptions during active sourcing tasks cause drop-off. Collect feedback passively or
immediately after task completion.

**DO: Record negative feedback as a Notification for later review**
The Notification model already exists and is admin-visible in the Tickets tab. Use it
rather than building a separate feedback store.

**DON'T: Send feedback data to a third-party service without review**
AvailAI handles commercially sensitive component sourcing data. Any external feedback tool
(Intercom, Hotjar) would receive part numbers, vendor names, and pricing signals. Treat this
as sensitive — keep feedback in-database.

See the **fastapi** skill for building the feedback API route.
See the **sqlalchemy** skill for ActivityLog query patterns.
