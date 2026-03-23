---
name: instrumenting-product-metrics
description: |
  Defines product events, funnels, and activation metrics for AvailAI's sourcing platform.
  Use when: adding new activity tracking calls, defining funnel stages for search→RFQ→offer workflows,
  setting up activation criteria for new users, querying vendor/buyer performance snapshots,
  wiring feature flags to metric collection, or debugging gaps in the activity timeline.
allowed-tools: Read, Edit, Write, Glob, Grep, Bash
---

# Instrumenting Product Metrics

AvailAI has a layered metrics stack: Prometheus (infrastructure), `activity_log` (CRM timeline events), `vendor_metrics_snapshot` / `buyer_leaderboard_snapshot` / `avail_score_snapshot` (daily/monthly rollups), and `response_analytics` (email health scoring). All product event decisions flow through `app/config.py` feature flags and land in services — never in routers.

## Quick Start

### Log a system activity event

```python
from app.services.activity_service import log_rfq_activity

await log_rfq_activity(
    db=db,
    requisition_id=req.id,
    activity_type="rfq_sent",
    details={"vendor_count": 5, "part_count": 3},
)
```

### Gate metric collection behind a feature flag

```python
from app.config import get_settings

settings = get_settings()
if settings.activity_tracking_enabled:
    await log_email_activity(db=db, ...)
```

### Read the activation funnel for a requirement

```python
# Funnel: requirement created → search run → sighting found → RFQ sent → offer received
from app.constants import RequirementStatus, RequisitionStatus
from sqlalchemy import select

stmt = (
    select(Requirement.status, func.count())
    .group_by(Requirement.status)
)
rows = db.execute(stmt).all()
```

## Key Concepts

| Concept | Location | Purpose |
|---------|----------|---------|
| `activity_log` table | `app/models/intelligence.py:257` | CRM-level event timeline per entity |
| `activity_service.py` | `app/services/activity_service.py` | Zero-manual-logging engine |
| `vendor_metrics_snapshot` | `app/models/performance.py:21` | Daily vendor KPI rollups (90-day window) |
| `buyer_leaderboard_snapshot` | `app/models/performance.py:58` | Monthly buyer points/rank |
| `avail_score_snapshot` | `app/models/performance.py:120` | 0-100 composite score (10 metrics × 2 roles) |
| `response_analytics.py` | `app/services/response_analytics.py` | Email health score (5-component, 0-100) |
| `system_config` | `app/models/config.py:45` | Key-value runtime config, persists across restarts |
| `activity_tracking_enabled` | `app/config.py:140` | Master kill-switch for all activity logging |
| `/metrics` endpoint | `app/main.py` | Prometheus scrape, token-protected |

## Common Patterns

### Tracking a new funnel stage

Add to the relevant service (NOT the router). Check the flag, write to `activity_log`, return the record ID.

```python
# app/services/my_service.py
from loguru import logger
from app.config import get_settings
from app.services.activity_service import log_rfq_activity

async def send_offer(db, offer_id: int, user_id: int) -> None:
    settings = get_settings()
    # ... business logic ...
    if settings.activity_tracking_enabled:
        await log_rfq_activity(
            db=db,
            requisition_id=offer.requisition_id,
            activity_type="offer_sent",
            details={"offer_id": offer_id, "user_id": user_id},
        )
    logger.info("Offer sent", extra={"offer_id": offer_id})
```

### Querying vendor email health

```python
from app.services.response_analytics import compute_email_health_score

score = await compute_email_health_score(db=db, vendor_card_id=42)
# score.total: 0-100
# score.components: {response_rate, response_time, quote_quality, ooo_frequency, thread_resolution}
```

### Reading activation state from system_config

```python
from app.models.config import SystemConfig
from sqlalchemy import select

row = db.execute(
    select(SystemConfig).where(SystemConfig.key == "onboarding_completed")
).scalar_one_or_none()

activated = row is not None and row.value == "true"
```

## See Also

- [activation-onboarding](references/activation-onboarding.md)
- [engagement-adoption](references/engagement-adoption.md)
- [in-app-guidance](references/in-app-guidance.md)
- [product-analytics](references/product-analytics.md)
- [roadmap-experiments](references/roadmap-experiments.md)
- [feedback-insights](references/feedback-insights.md)

## Related Skills

- See the **fastapi** skill for route structure and dependency injection patterns
- See the **sqlalchemy** skill for querying `activity_log` and snapshot tables
- See the **pytest** skill for mocking `activity_tracking_enabled` in tests
- See the **redis** skill for caching aggregated metric queries
- See the **mapping-user-journeys** skill to trace HTMX partials back to metric gaps
- See the **designing-onboarding-paths** skill for first-run activation UI
- See the **orchestrating-feature-adoption** skill for feature rollout and nudge wiring
