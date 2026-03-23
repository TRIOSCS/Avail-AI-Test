# Product Analytics Reference

## Contents
- Event Tracking via Activity Log
- Activation Metrics from Existing Models
- Router-Level Tracking Hooks
- Building a Funnel from Existing Data
- Anti-Patterns

## Event Tracking via Activity Log

AvailAI has `activity_tracking_enabled` in `app/config.py` and a CRM activity model. Use the existing activity log as the event store for product analytics rather than adding a third-party SDK:

```python
# app/services/activity_service.py — log a product event
from app.models.activity import Activity
from app.constants import ActivityType

def track_event(db: Session, user_id: int, event: str, entity_id: int, metadata: dict) -> None:
    if not get_settings().activity_tracking_enabled:
        return
    activity = Activity(
        user_id=user_id,
        activity_type=event,
        entity_id=entity_id,
        metadata=metadata,
    )
    db.add(activity)
    db.commit()
    logger.info("Product event tracked", extra={"event": event, "user_id": user_id})
```

Key activation events to track:
- `requisition_created` — first action in core workflow
- `search_completed` — sourcing engine used
- `rfq_sent` — core value delivered
- `rfq_reply_received` — loop closed
- `offer_created_from_reply` — AI parsing succeeded

## Activation Metrics from Existing Models

The data for activation funnels already exists in the database. Query it directly:

```python
# Activation funnel query — no extra instrumentation needed
from sqlalchemy import func

def get_activation_funnel(db: Session) -> dict:
    total_users = db.query(func.count(User.id)).scalar() or 0
    created_req = db.query(func.count(func.distinct(Requisition.created_by))).scalar() or 0
    ran_search = db.query(func.count(func.distinct(Sighting.created_by))).scalar() or 0
    sent_rfq = db.query(func.count(func.distinct(RFQLog.sent_by))).scalar() or 0

    return {
        "total_users": total_users,
        "created_requisition": created_req,
        "ran_search": ran_search,
        "sent_rfq": sent_rfq,
    }
```

## Router-Level Tracking Hooks

Add tracking in service layer (NOT routers) to keep routes thin:

```python
# app/services/requisition_service.py
def create_requisition(db: Session, user_id: int, data: RequisitionCreate) -> Requisition:
    req = Requisition(**data.model_dump(), created_by=user_id)
    db.add(req)
    db.commit()
    db.refresh(req)
    track_event(db, user_id, "requisition_created", req.id, {"part_count": len(data.parts)})
    return req
```

NEVER put tracking calls in routers — they violate the thin-router rule and make tracking hard to test.

## Building a Funnel from Existing Data

```python
# Add to app/routers/htmx_views.py or an admin analytics partial
@router.get("/v2/partials/admin/activation-funnel")
async def activation_funnel(request: Request, db: Session = Depends(get_db), _=Depends(require_admin)):
    funnel = get_activation_funnel(db)
    return template_response("htmx/partials/admin/activation_funnel.html", request, funnel)
```

```jinja2
{# app/templates/htmx/partials/admin/activation_funnel.html #}
{% set steps = [
  ("Registered", total_users),
  ("Created Requisition", created_requisition),
  ("Ran Search", ran_search),
  ("Sent RFQ", sent_rfq),
] %}
{% for label, count in steps %}
  <div class="funnel-step">
    <span class="font-medium">{{ label }}</span>
    <span class="text-gray-500">{{ count }}</span>
    {% if not loop.first %}
      <span class="text-xs text-red-400">
        {{ "%.0f"|format((count / steps[loop.index0 - 1][1] * 100) if steps[loop.index0 - 1][1] else 0) }}% conversion
      </span>
    {% endif %}
  </div>
{% endfor %}
```

## Anti-Patterns

### WARNING: Third-Party Analytics SDK for Internal Metrics

AVOID adding Segment, Mixpanel, or PostHog for internal activation tracking. AvailAI is a B2B tool with a small user base — the existing activity log and database queries are sufficient.

**Why:** Third-party SDKs add frontend JS payload, require cookie consent, and create a data compliance surface for enterprise customers.

**The Fix:** Query activation metrics directly from PostgreSQL. Add a lightweight admin dashboard partial.

### WARNING: Tracking in Templates

```jinja2
{# BAD — never fire analytics from templates #}
<div hx-post="/api/track/page-view" hx-trigger="load">
```

**The Fix:** Track events in the service layer on data mutations. Page views are not meaningful for B2B activation.

See the **fastapi** skill for service layer patterns and the **sqlalchemy** skill for aggregate query patterns.
