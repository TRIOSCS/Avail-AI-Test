# Feedback & Insights Reference

## Contents
- Lead feedback event model
- Buyer feedback on sourcing leads
- Command center as feedback surface
- Unmatched activity attribution queue
- Vendor response analytics as feedback loop
- Anti-patterns

---

## Lead Feedback Events

`LeadFeedbackEvent` (`app/models/sourcing_lead.py`) is the structured feedback model for buyer decisions on sourcing leads. Use it to record why a lead was accepted, rejected, or escalated.

```python
from app.models.sourcing_lead import LeadFeedbackEvent, SourcingLead
from app.constants import LeadStatus  # use enum, never raw string

event = LeadFeedbackEvent(
    lead_id=lead.id,
    status=LeadStatus.REJECTED,
    reason_code="price_too_high",
    contact_method="email",
    contact_attempt_count=2,
    notes="Vendor quoted 3x market price",
    created_by_user_id=user.id,
)
db.add(event)
db.commit()
```

Query feedback to compute rejection reason distribution:

```python
from sqlalchemy import select, func

stmt = (
    select(LeadFeedbackEvent.reason_code, func.count().label("n"))
    .group_by(LeadFeedbackEvent.reason_code)
    .order_by(func.count().desc())
    .limit(10)
)
rows = db.execute(stmt).all()
```

---

## Buyer Feedback Summary on SourcingLead

`SourcingLead.buyer_feedback_summary` stores freetext context from the buyer's last action. Update it alongside the status change:

```python
from app.models.sourcing_lead import SourcingLead

lead = db.get(SourcingLead, lead_id)
lead.buyer_status = LeadStatus.QUOTED
lead.buyer_feedback_summary = "Accepted at $2.40/unit, net 30"
lead.last_buyer_action_at = func.now()
db.commit()
```

---

## Unmatched Activity Attribution Queue

`get_unmatched_activities()` returns activity records that couldn't be auto-matched to a company/vendor. These represent data quality issues — surface them in a triage view so users can manually attribute them.

```python
from app.services.activity_service import (
    get_unmatched_activities,
    attribute_activity,
    dismiss_activity,
)

# Get queue
unmatched = await get_unmatched_activities(db=db, limit=50)

# Attribute to a company
await attribute_activity(
    db=db,
    activity_id=unmatched[0].id,
    company_id=42,
)

# Dismiss (spam, irrelevant)
await dismiss_activity(db=db, activity_id=unmatched[1].id)
```

Surface this queue at `/v2/activity/unmatched` — not in the main nav. It's a power-user triage tool.

---

## Command Center as Feedback Triage

`offers_needing_review` in the command center response represents Claude-parsed RFQ replies with confidence 0.5–0.8. These require human verification before becoming confirmed offers.

```python
# Buyer reviews and confirms:
offer.status = OfferStatus.CONFIRMED
offer.reviewed_by_user_id = user.id
offer.reviewed_at = func.now()
db.commit()

# Log the review action
await log_rfq_activity(
    db=db,
    requisition_id=offer.requisition_id,
    activity_type="offer_review_confirmed",
    details={"offer_id": offer.id, "confidence": offer.parse_confidence},
)
```

---

## Vendor Response Analytics as Feedback Loop

`get_email_intelligence_dashboard()` from `response_analytics.py` returns 7-day aggregate feedback on the email mining pipeline:

```python
from app.services.response_analytics import get_email_intelligence_dashboard

dashboard = await get_email_intelligence_dashboard(db=db)
# dashboard.emails_scanned: int
# dashboard.offers_detected: int
# dashboard.stock_lists_found: int
# dashboard.ooo_vendors: list
# dashboard.top_vendors: list
# dashboard.recent_offers: list
# dashboard.pending_review: int
```

Use this to detect pipeline degradation: if `emails_scanned > 0` but `offers_detected == 0` for 3+ days, something is broken upstream.

---

## Anti-Patterns

**NEVER discard unmatched activities silently.** Every unmatched record is lost CRM signal. Route them to the attribution queue.

**NEVER store freetext feedback in a JSON column without a structured `reason_code`** alongside it. Freetext is unsearchable; reason codes enable aggregate analysis.

**NEVER treat "no feedback" as a signal.** Absence of a `LeadFeedbackEvent` means the lead was never acted on, not that it was satisfactory. Filter on `last_buyer_action_at IS NOT NULL` for engagement metrics.

---

## Related Skills

- See the **sqlalchemy** skill for aggregate feedback queries
- See the **mapping-user-journeys** skill to trace where feedback collection drops off
- See the **pytest** skill for testing feedback event creation and status transitions
