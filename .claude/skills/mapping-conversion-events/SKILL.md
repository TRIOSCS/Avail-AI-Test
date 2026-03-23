---
name: mapping-conversion-events
description: |
  Defines funnel events, tracking, and success signals for AvailAI's sourcing platform.
  Use when: defining conversion events for the search→RFQ→offer→quote funnel, adding
  tracking calls to sourcing workflows, mapping where users drop off, instrumenting
  new feature adoption signals, or auditing which funnel stages lack event coverage.
allowed-tools: Read, Edit, Write, Glob, Grep, Bash
---

# Mapping Conversion Events

AvailAI's conversion funnel is B2B and transactional: the goal is a completed buy plan, not a signup click. Events live in `activity_log` (CRM timeline) and status columns on ORM models — there is no separate analytics database. Conversion signals are derived from status transitions, `activity_log` rows, and snapshot tables in `app/models/performance.py`.

## Core Funnel

```
Requisition created
  → Requirement searched (RequirementStatus.SEARCHING → FOUND/NOT_FOUND)
  → Sighting created (vendor quote auto-upserted by search_service)
  → RFQ sent (activity_type="rfq_sent" on activity_log)
  → Reply received (activity_type="rfq_reply")
  → Offer created (OfferStatus.PENDING → ACCEPTED)
  → Quote created → Buy plan created
```

## Quick Start

### Record a funnel stage transition

```python
# app/services/rfq_service.py
from app.services.activity_service import log_rfq_activity
from app.config import get_settings

async def mark_rfq_sent(db, rfq_id: int, vendor_count: int) -> None:
    settings = get_settings()
    if settings.activity_tracking_enabled:
        await log_rfq_activity(
            db=db,
            rfq_id=rfq_id,
            activity_type="rfq_sent",
            details={"vendor_count": vendor_count},
        )
```

### Query drop-off at any stage

```python
from sqlalchemy import select, func
from app.models import Requirement
from app.constants import RequirementStatus

stmt = (
    select(Requirement.status, func.count().label("n"))
    .group_by(Requirement.status)
)
rows = db.execute(stmt).all()
# {SEARCHING: 12, FOUND: 87, NOT_FOUND: 31} → 26% drop at search stage
```

### Check if a requisition crossed the RFQ threshold

```python
from sqlalchemy import select
from app.models import ActivityLog

sent = db.execute(
    select(ActivityLog)
    .where(
        ActivityLog.rfq_id == rfq_id,
        ActivityLog.activity_type == "rfq_sent",
    )
).scalars().first()
converted = sent is not None
```

## Key Concepts

| Concept | Location | Role in Funnel |
|---------|----------|----------------|
| `RequirementStatus` | `app/constants.py` | Tracks search stage (SEARCHING→FOUND→NOT_FOUND) |
| `RequisitionStatus` | `app/constants.py` | Top-level workflow state (OPEN→CLOSED) |
| `activity_log` | `app/models/intelligence.py:257` | Timestamped event per entity (RFQ, company, vendor) |
| `log_rfq_activity()` | `app/services/activity_service.py` | Writes RFQ-scoped funnel events |
| `vendor_metrics_snapshot` | `app/models/performance.py:21` | Daily vendor KPI rollups (reply rate, response time) |
| `avail_score_snapshot` | `app/models/performance.py:120` | 0-100 composite per vendor/buyer |
| `response_analytics.py` | `app/services/response_analytics.py` | Email health score (5 components, 0-100) |
| `activity_tracking_enabled` | `app/config.py` | Master kill-switch — always gate calls behind this |

## Common Patterns

### Gate every tracking call behind the feature flag

```python
from app.config import get_settings

settings = get_settings()
if settings.activity_tracking_enabled:
    await log_rfq_activity(db=db, rfq_id=rfq_id, activity_type="offer_created")
```

NEVER write to `activity_log` without this guard — it breaks tests and staging environments.

### Use consistent `activity_type` string values

```python
# GOOD — matches existing patterns in activity_service.py
activity_type="rfq_sent"
activity_type="rfq_reply"
activity_type="offer_created"

# BAD — invented strings that fragment queries
activity_type="RFQ Sent"      # breaks all filters
activity_type="sent_rfq"      # creates undiscoverable event type
```

## See Also

- [conversion-optimization](references/conversion-optimization.md)
- [content-copy](references/content-copy.md)
- [distribution](references/distribution.md)
- [measurement-testing](references/measurement-testing.md)
- [growth-engineering](references/growth-engineering.md)
- [strategy-monetization](references/strategy-monetization.md)

## Related Skills

- See the **instrumenting-product-metrics** skill for snapshot tables, activation criteria, and Prometheus metrics
- See the **mapping-user-journeys** skill to trace HTMX partials back to funnel gaps
- See the **orchestrating-feature-adoption** skill to wire feature flags to conversion nudges
- See the **designing-onboarding-paths** skill for first-run activation flows
- See the **sqlalchemy** skill for querying `activity_log` and status columns
- See the **fastapi** skill for route structure when adding tracking endpoints
- See the **pytest** skill for mocking `activity_tracking_enabled` in tests
