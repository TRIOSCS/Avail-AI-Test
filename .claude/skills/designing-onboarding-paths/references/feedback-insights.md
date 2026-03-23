# Feedback & Insights Reference

## Contents
- Activity Log as Feedback Signal
- RFQ Reply Parsing as Implicit Feedback
- Admin Visibility Patterns
- Support Signal Collection
- Anti-Patterns

## Activity Log as Feedback Signal

The existing `activity_tracking_enabled` flag and CRM activity model capture user actions. These are your primary implicit feedback signals — no external tool needed.

```python
# Query for friction signals: actions started but not completed
def get_abandoned_searches(db: Session, days: int = 7) -> list[dict]:
    cutoff = datetime.utcnow() - timedelta(days=days)
    # Requisitions with searches triggered but no sightings created
    return (
        db.query(Requisition)
        .filter(
            Requisition.created_at >= cutoff,
            Requisition.sighting_count == 0,
            Requisition.status != RequisitionStatus.DRAFT,
        )
        .all()
    )
```

Key friction signals to monitor:
- `sighting_count == 0` after search → connectors failing or no API keys configured
- `rfq_count == 0` with `sighting_count > 0` → users finding vendors but not progressing
- `response_count == 0` after RFQ sent → email deliverability or vendor engagement issue

## RFQ Reply Parsing as Implicit Feedback

`app/services/response_parser.py` uses Claude to parse RFQ replies. Confidence scores are stored and surfaced in the UI. Low confidence flags (`0.5-0.8`) are explicit signals that the parsing model needs attention:

```python
# app/services/response_parser.py — confidence-gated outcomes
if confidence >= 0.8:
    create_offer(db, parsed_data)
    logger.info("Auto-created offer from reply", extra={"confidence": confidence})
elif confidence >= 0.5:
    flag_for_manual_review(db, reply_id)
    logger.warning("Low confidence reply flagged", extra={"confidence": confidence})
else:
    logger.error("Reply below confidence threshold, skipped", extra={"confidence": confidence})
```

Track the ratio of auto-created vs. flagged offers as a model quality metric. A rising flag rate signals prompt or parser degradation.

## Admin Visibility Patterns

Build admin-only partials to surface insights without cluttering the main UI:

```python
# app/routers/htmx_views.py
@router.get("/v2/partials/admin/insights")
async def admin_insights(request: Request, db: Session = Depends(get_db), _=Depends(require_admin)):
    ctx = {
        "avg_confidence": db.query(func.avg(RFQResponse.ai_confidence)).scalar() or 0,
        "flagged_replies": db.query(func.count(RFQResponse.id))
            .filter(RFQResponse.status == "flagged").scalar() or 0,
        "abandoned_searches": len(get_abandoned_searches(db)),
    }
    return template_response("htmx/partials/admin/insights.html", request, ctx)
```

```jinja2
{# app/templates/htmx/partials/admin/insights.html #}
<div class="grid grid-cols-3 gap-4">
  <div class="metric-card">
    <span class="metric-value">{{ "%.0f"|format(avg_confidence * 100) }}%</span>
    <span class="metric-label">Avg AI Confidence</span>
  </div>
  <div class="metric-card {% if flagged_replies > 10 %}metric-card--warning{% endif %}">
    <span class="metric-value">{{ flagged_replies }}</span>
    <span class="metric-label">Flagged Replies</span>
  </div>
  <div class="metric-card">
    <span class="metric-value">{{ abandoned_searches }}</span>
    <span class="metric-label">Searches w/ 0 Results (7d)</span>
  </div>
</div>
```

## Support Signal Collection

For explicit user feedback, use a lightweight inline form that posts to a simple feedback endpoint — no third-party chat widget:

```jinja2
{# Feedback widget in base.html or relevant partials #}
<div x-data="{ open: false, sent: false }">
  <button @click="open = true" class="text-xs text-gray-400 hover:text-gray-600">
    Give feedback
  </button>
  <div x-show="open" x-transition class="feedback-popover">
    <form hx-post="/api/feedback"
          hx-on::after-request="$data.sent = true; $data.open = false">
      <textarea name="message" placeholder="What's working? What's broken?" rows="3"
                class="w-full border rounded p-2 text-sm"></textarea>
      <button type="submit" class="btn-sm btn-primary mt-2">Send</button>
    </form>
  </div>
  <span x-show="sent" class="text-xs text-green-600">Thanks for the feedback!</span>
</div>
```

## Anti-Patterns

### WARNING: External Chat Widgets for B2B Feedback

AVOID embedding Intercom, Crisp, or Drift for a small B2B user base. These add 100-300KB of JS, create privacy concerns for enterprise clients, and collect more noise than signal.

**The Fix:** A simple `POST /api/feedback` endpoint storing to a `feedback` table gives you structured data without the overhead. Admin views in AvailAI are already gated — add a feedback review partial.

### WARNING: Ignoring the AI Confidence Signal

The `ai_confidence` field on RFQ responses is the most direct feedback signal in the system. Not monitoring it means parser degradation is invisible until users complain.

**The Fix:** Add an alert to the admin insights partial when `avg_confidence < 0.7` over the last 50 replies. Log it in `startup.py`'s health check output.

See the **fastapi** skill for thin router patterns and the **jinja2** skill for admin partial structure.
