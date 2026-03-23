# Product Analytics Reference

## Contents
- Metrics stack overview
- Prometheus infrastructure metrics
- Activity log as product event log
- Vendor email health scoring
- Performance snapshot queries
- Funnel query pattern
- Anti-patterns

---

## Metrics Stack

| Layer | Technology | What It Measures |
|-------|-----------|-----------------|
| Infrastructure | Prometheus (`/metrics`) | HTTP latency, error rates, request counts |
| CRM events | `activity_log` table | Email sent/received, calls, notes, meetings |
| Vendor KPIs | `vendor_metrics_snapshot` | Response rate, quote accuracy, lead time (daily) |
| Buyer KPIs | `buyer_leaderboard_snapshot` | Points per action type (monthly) |
| Composite score | `avail_score_snapshot` | 0-100 across 10 metrics, role-aware (monthly) |
| Email health | `VendorCard.email_health_score` | 5-component score (computed by `response_analytics.py`) |

---

## Prometheus: Infrastructure Metrics

The FastAPI Instrumentator runs automatically. The `/metrics` endpoint requires `X-Metrics-Token` (constant-time HMAC comparison). Do not scrape it from within the application — it is for external Prometheus only.

```python
# app/main.py — already configured, do not duplicate
# Excluded paths: /metrics, /health, /static/*
# All other routes are auto-instrumented
```

To add a custom Prometheus counter for a specific business event:

```python
from prometheus_client import Counter

rfq_sent_total = Counter(
    "availai_rfq_sent_total",
    "Total RFQs sent",
    ["vendor_type"],
)

# In service:
rfq_sent_total.labels(vendor_type="broker").inc()
```

Register counters at module level, not inside functions.

---

## Activity Log as Product Event Log

`activity_log` is the source of truth for CRM-level product events. Query it for funnel analysis.

```python
from app.models.intelligence import ActivityLog
from sqlalchemy import select, func

# Count RFQ-related events by type in the last 30 days
stmt = (
    select(ActivityLog.activity_type, func.count().label("n"))
    .where(ActivityLog.created_at >= func.now() - text("interval '30 days'"))
    .where(ActivityLog.activity_type.in_(["rfq_sent", "rfq_response", "offer_logged"]))
    .group_by(ActivityLog.activity_type)
)
rows = db.execute(stmt).all()
```

---

## Vendor Email Health Score

Computed by `response_analytics.py`. Five weighted components:

| Component | Weight | Range |
|-----------|--------|-------|
| Response rate | 30% | outreach:reply ratio |
| Response time | 25% | ≤4h=100, ≥168h=0 |
| Quote quality | 20% | replies with pricing / total replies |
| OOO frequency | 10% | OOO contacts / total |
| Thread resolution | 15% | resolved threads / total |

```python
from app.services.response_analytics import (
    compute_email_health_score,
    batch_update_email_health,
)

# Single vendor
score = await compute_email_health_score(db=db, vendor_card_id=42)
print(score.total)           # 0-100
print(score.components)      # dict of component scores

# Batch update (called by scheduler)
await batch_update_email_health(db=db)  # processes 500-vendor batches
```

---

## Funnel Query: Search → RFQ → Offer

```python
from app.models.requirements import Requirement
from app.models.sightings import Sighting
from app.models.responses import VendorResponse
from app.constants import RequirementStatus
from sqlalchemy import select, func

def get_funnel_counts(db, requisition_id: int) -> dict:
    reqs = db.execute(
        select(func.count()).select_from(Requirement)
        .where(Requirement.requisition_id == requisition_id)
    ).scalar_one()

    with_sightings = db.execute(
        select(func.count()).select_from(Requirement)
        .where(Requirement.requisition_id == requisition_id)
        .where(Requirement.status == RequirementStatus.FOUND)
    ).scalar_one()

    with_responses = db.execute(
        select(func.count(VendorResponse.id.distinct()))
        .join(Requirement)
        .where(Requirement.requisition_id == requisition_id)
    ).scalar_one()

    return {
        "requirements": reqs,
        "searched": with_sightings,
        "responses": with_responses,
    }
```

---

## Anti-Patterns

**NEVER run aggregation queries inline in a router.** Move them to `app/services/`. Cache results with `@cached_endpoint` when appropriate (see the **redis** skill).

**NEVER store metric state in Alpine.js stores.** Alpine state is ephemeral and client-side. Metric data lives in the database.

**NEVER invent new event types** — check `app/constants.py` for existing `ActivityType` / `ChannelType` enums first. Adding raw strings bypasses type safety and breaks analytics queries.

---

## Related Skills

- See the **sqlalchemy** skill for aggregate query patterns
- See the **redis** skill for caching expensive metric aggregations
- See the **pytest** skill for asserting on metric service outputs
