# Measurement and Testing Reference

## Contents
- What to Measure
- Querying the Activity Log
- Snapshot Tables for Aggregated Metrics
- Testing Funnel Events
- WARNING: Anti-Patterns

## What to Measure

Track three numbers per funnel stage: volume in, volume out, and time-to-next-stage.

| Stage | Volume metric | Time metric |
|-------|--------------|-------------|
| Search | requirements created vs. FOUND | `updated_at - created_at` on Requirement |
| RFQ | rfq_sent count vs. rfq_reply count | `reply.created_at - sent.created_at` |
| Offer | offers created vs. ACCEPTED | `offer.accepted_at - offer.created_at` |
| Quote | quotes created vs. SIGNED | `quote.signed_at - quote.created_at` |

## Querying the Activity Log

```python
# Time between RFQ sent and first reply, per requisition
from sqlalchemy import select, func
from app.models import ActivityLog

sent = (
    select(ActivityLog.rfq_id, ActivityLog.created_at.label("sent_at"))
    .where(ActivityLog.activity_type == "rfq_sent")
    .subquery()
)
replied = (
    select(ActivityLog.rfq_id, func.min(ActivityLog.created_at).label("replied_at"))
    .where(ActivityLog.activity_type == "rfq_reply")
    .group_by(ActivityLog.rfq_id)
    .subquery()
)
rows = db.execute(
    select(
        sent.c.rfq_id,
        (replied.c.replied_at - sent.c.sent_at).label("time_to_reply"),
    ).join(replied, sent.c.rfq_id == replied.c.rfq_id)
).all()
```

## Snapshot Tables for Aggregated Metrics

`vendor_metrics_snapshot` and `avail_score_snapshot` in `app/models/performance.py` pre-compute expensive aggregates daily. Use them instead of re-computing from raw rows.

```python
from app.models.performance import VendorMetricsSnapshot

snapshots = db.execute(
    select(VendorMetricsSnapshot)
    .where(VendorMetricsSnapshot.vendor_card_id == vendor_id)
    .order_by(VendorMetricsSnapshot.snapshot_date.desc())
    .limit(90)
).scalars().all()
# Fields: reply_rate, avg_response_hours, quote_accuracy, total_rfqs_sent
```

## Testing Funnel Events

### Mock the feature flag in pytest

```python
# tests/test_conversion_events.py
from unittest.mock import patch

def test_rfq_sent_logs_activity(db_session):
    with patch("app.config.Settings.activity_tracking_enabled", new=True):
        # call service under test
        result = send_rfq(db=db_session, rfq_id=1, vendor_ids=[42])
        log = db_session.query(ActivityLog).filter_by(rfq_id=1).first()
        assert log is not None
        assert log.activity_type == "rfq_sent"
        assert log.details["vendor_count"] == 1
```

### Assert drop-off is measurable

```python
def test_not_found_requirement_is_queryable(db_session):
    req = Requirement(status=RequirementStatus.NOT_FOUND, ...)
    db_session.add(req)
    db_session.commit()

    rows = db_session.execute(
        select(Requirement.status, func.count()).group_by(Requirement.status)
    ).all()
    status_map = {r.status: r.count for r in rows}
    assert RequirementStatus.NOT_FOUND in status_map
```

## WARNING: Anti-Patterns

### WARNING: Testing with `activity_tracking_enabled=False` and asserting logs exist

**The Problem:**
```python
# BAD — default test settings disable tracking, so this always fails
def test_activity_logged(db_session):
    send_rfq(db=db_session, rfq_id=1, vendor_ids=[1])
    log = db_session.query(ActivityLog).first()
    assert log is not None  # Always None — flag is off
```

**The Fix:** Explicitly patch the flag to `True` for tests that assert log writes. See the **pytest** skill for fixture patterns.

### WARNING: Computing funnel metrics in a router on every request

Aggregating `activity_log` on every page load will time out under load. Use `vendor_metrics_snapshot` for display, and schedule a background job for heavy aggregations.

See the **instrumenting-product-metrics** skill for the full snapshot refresh pattern.
See the **redis** skill for caching metric queries with short TTLs.
