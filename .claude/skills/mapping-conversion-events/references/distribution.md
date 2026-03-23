# Distribution Reference

## Contents
- Distribution Channels
- Email RFQ as Distribution
- Proactive Matching as Outbound
- In-App Notification Surfaces
- WARNING: Anti-Patterns

## Distribution Channels

AvailAI distributes sourcing requests through four channels. Each maps to a code path.

| Channel | Code Path | Trigger |
|---------|-----------|---------|
| RFQ email (outbound) | `app/email_service.py → send_batch_rfq()` | User selects vendors and clicks Send |
| Inbox monitor (inbound) | `app/jobs/inbox_monitor.py` | APScheduler every 30 min |
| Proactive match (push) | `app/services/proactive_service.py` | New offer matched to purchase history |
| In-app notification | HTMX partial swap into `#notification-area` | Any backend event with a UI hook |

## Email RFQ as Distribution

The RFQ is the primary outbound distribution action. Every sent RFQ is tagged `[AVAIL-{id}]` so replies are automatically threaded.

```python
# email_service.py — send_batch_rfq() entry point
await send_batch_rfq(
    db=db,
    requisition_id=req_id,
    vendor_ids=[42, 77, 103],
    subject="RFQ: {mpn} × {qty}",
    body=rendered_body,
)
# Tags each message [AVAIL-{rfq_id}] in subject line
# Writes activity_type="rfq_sent" to activity_log after send
```

**Distribution signal to track:** Number of unique vendors reached per requisition. Low vendor count = low reply probability.

```python
from sqlalchemy import select, func
from app.models import ActivityLog

vendor_count = db.execute(
    select(func.count()).where(
        ActivityLog.rfq_id == rfq_id,
        ActivityLog.activity_type == "rfq_sent",
    )
).scalar()
```

## Proactive Matching as Outbound

When a new vendor offer is created, `proactive_service.py` scores it against customer purchase history (SQL scorecard 0-100). Matches above threshold are surfaced in the UI as actionable cards.

```python
# This is the outbound push — no user action needed to trigger it
from app.services.proactive_service import find_proactive_matches

matches = await find_proactive_matches(db=db, offer_id=new_offer.id)
# Returns list of {customer_id, score, part_match, qty_fit, price_vs_historical}
```

**Distribution signal:** `proactive_matches_surfaced` / `proactive_matches_sent`. Low ratio means the threshold is too high or the scoring is wrong.

## In-App Notification Surfaces

HTMX SSE (`htmx-ext-sse`) can push real-time notifications when background jobs complete. The extension is already loaded in `app/static/htmx_app.js`.

```html
<!-- Connect to SSE stream for live updates -->
<div hx-ext="sse" sse-connect="/events/stream"
     sse-swap="rfq_reply"
     hx-target="#notification-area"
     hx-swap="afterbegin">
</div>
```

## WARNING: Anti-Patterns

### WARNING: Sending RFQs without tracking vendor count

**The Problem:**
```python
# BAD — fires the send but logs nothing about distribution breadth
await send_batch_rfq(db=db, requisition_id=req_id, vendor_ids=vendor_ids)
# No count logged → can't identify under-distributed requisitions
```

**The Fix:**
```python
# GOOD — log the count after send
await send_batch_rfq(db=db, requisition_id=req_id, vendor_ids=vendor_ids)
if settings.activity_tracking_enabled:
    await log_rfq_activity(
        db=db, rfq_id=rfq_id,
        activity_type="rfq_sent",
        details={"vendor_count": len(vendor_ids)},
    )
```

### WARNING: Polling inbox manually instead of using the scheduler

The `inbox_monitor` job already runs every 30 minutes. Don't add ad-hoc Graph API calls in routers — they bypass rate limiting and create duplicate reply processing.

See the **instrumenting-product-metrics** skill for reply rate and email health scoring.
See the **orchestrating-feature-adoption** skill for surfacing proactive match notifications.
