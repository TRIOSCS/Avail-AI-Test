# Product Analytics Reference

## Contents
- Activity Log as Analytics Backbone
- Key Activation Events to Track
- Querying Adoption Metrics
- API Source Usage Tracking
- DO / DON'T Pairs
- Missing Analytics Infrastructure

---

## Activity Log as Analytics Backbone

AvailAI has no external analytics SDK (no Mixpanel, Amplitude, PostHog). All behavioral data
lives in `ActivityLog` (`app/models/intelligence.py:257`) and `ApiSource`
(`app/models/config.py:12`). This is the source of truth for adoption metrics.

```python
# app/models/intelligence.py — ActivityLog fields relevant to adoption
class ActivityLog(Base):
    activity_type: str    # "call", "email", "note", "meeting"
    event_type: str       # "email", "call", "note", "meeting"
    direction: str        # "inbound", "outbound"
    requisition_id: int   # nullable — links to req workflow
    company_id: int       # nullable — links to CRM
    occurred_at: datetime
    created_at: datetime
```

## Key Activation Events to Track

Define "activated" as: user has completed at least one core workflow action.

| Event | Table/Field | Activation Signal |
|-------|-------------|-------------------|
| First requisition created | `Requisition.created_at` | User reached search phase |
| First search run | `Sighting.created_at` | Search pipeline fired |
| First RFQ sent | `ActivityLog.activity_type = 'email'` + `direction = 'outbound'` | RFQ workflow started |
| First offer parsed | `Offer.created_at` | AI parsing activated |
| First proactive match | `ProactiveLead.created_at` | Proactive workflow engaged |

## Querying Adoption Metrics

```python
# app/services/analytics_service.py (to be created)
from sqlalchemy import func
from app.models.requisition import Requisition
from app.models.intelligence import ActivityLog

def get_activation_stats(db: Session) -> dict:
    """Returns counts for the core activation funnel."""
    return {
        "total_reqs": db.query(func.count(Requisition.id)).scalar() or 0,
        "reqs_with_sightings": db.query(func.count(Requisition.id))
            .filter(Requisition.sighting_count > 0)
            .scalar() or 0,
        "rfqs_sent": db.query(func.count(ActivityLog.id))
            .filter(
                ActivityLog.activity_type == "email",
                ActivityLog.direction == "outbound",
            )
            .scalar() or 0,
    }
```

## API Source Usage Tracking

`ApiSource` (`app/models/config.py`) tracks per-connector usage. Use it to identify which
connectors users rely on and which are underused.

```python
# Check connector health and usage
from app.models.config import ApiSource

def get_connector_adoption(db: Session) -> list[dict]:
    sources = db.query(ApiSource).filter(ApiSource.is_active == True).all()
    return [
        {
            "name": s.name,
            "total_searches": s.total_searches,
            "last_success": s.last_success,
            "error_rate": s.error_count_24h,
        }
        for s in sources
    ]
```

## DO / DON'T Pairs

**DO: Use ActivityLog for adoption event tracking**
```python
# Log a feature-use event when user sends first RFQ
activity = ActivityLog(
    activity_type="email",
    direction="outbound",
    requisition_id=req.id,
    event_type="email",
    summary=f"RFQ sent to {len(vendor_ids)} vendors",
    occurred_at=datetime.utcnow(),
)
db.add(activity)
db.commit()
```

**DON'T: Add a third-party analytics SDK for in-app events**
The existing `ActivityLog` table covers AvailAI's core workflows. Adding Mixpanel or Amplitude
before this data is fully leveraged is premature complexity — and a privacy/compliance surface.

**DO: Surface adoption metrics in the dashboard**
```python
# Add activation funnel to dashboard context
context["activation"] = get_activation_stats(db)
```

**DON'T: Query adoption stats on every page load without caching**
```python
# BAD — runs on every dashboard hit
stats = db.query(func.count(Requisition.id)).scalar()  # in a hot path
```
Use the **redis** skill's `@cached_endpoint` decorator with `ttl_hours=1`.

## Missing Analytics Infrastructure

AvailAI has no structured product event pipeline beyond `ActivityLog`. For funnel analysis
(e.g., "how many users who searched also sent an RFQ"), you need cross-table joins that become
expensive at scale. Consider adding:

1. A lightweight `ProductEvent` table (`user_id`, `event_name`, `properties JSON`, `created_at`)
2. Seeding it from the existing ActivityLog via a backfill migration

This is additive — do NOT replace ActivityLog; it serves CRM purposes independently.

See the **sqlalchemy** skill for aggregation query patterns.
See the **redis** skill for caching dashboard stats.
