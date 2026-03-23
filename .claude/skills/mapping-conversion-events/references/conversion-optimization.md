# Conversion Optimization Reference

## Contents
- Funnel Stage Definitions
- Status Transitions as Conversion Signals
- Drop-Off Queries
- WARNING: Anti-Patterns
- Checklist: Auditing a New Funnel Stage

## Funnel Stage Definitions

AvailAI has two conversion sub-funnels — search-to-sighting and RFQ-to-offer. Each has a distinct set of status transitions.

**Search sub-funnel** (per `Requirement` row):

| Stage | Status | Signal |
|-------|--------|--------|
| Submitted | `SEARCHING` | Requirement created |
| Found | `FOUND` | ≥1 sighting returned |
| Dead end | `NOT_FOUND` | All connectors returned empty |

**RFQ sub-funnel** (per `Requisition` + `ActivityLog`):

| Stage | Signal | Source |
|-------|--------|--------|
| RFQ sent | `activity_type="rfq_sent"` | `activity_log` |
| Reply received | `activity_type="rfq_reply"` | `activity_log` |
| Offer accepted | `OfferStatus.ACCEPTED` | `offers` table |
| Quote created | `QuoteStatus.DRAFT` | `quotes` table |
| Buy plan created | `BuyPlanStatus.ACTIVE` | `buy_plans` table |

## Status Transitions as Conversion Signals

Use status columns for coarse funnel metrics. They're always in sync with the DB — no risk of missed `activity_log` writes.

```python
# Conversion rate: requirements that found at least one sighting
from sqlalchemy import select, func, case
from app.models import Requirement
from app.constants import RequirementStatus

stmt = select(
    func.count().label("total"),
    func.sum(
        case((Requirement.status == RequirementStatus.FOUND, 1), else_=0)
    ).label("found"),
)
row = db.execute(stmt).one()
rate = row.found / row.total if row.total else 0.0
```

## Drop-Off Queries

Identify which funnel stage loses the most volume.

```python
# RFQs sent that never got a reply
from sqlalchemy import select, func, not_, exists
from app.models import ActivityLog

sent_ids = select(ActivityLog.rfq_id).where(
    ActivityLog.activity_type == "rfq_sent"
).scalar_subquery()

replied_ids = select(ActivityLog.rfq_id).where(
    ActivityLog.activity_type == "rfq_reply"
).scalar_subquery()

no_reply = db.execute(
    select(func.count()).where(
        ActivityLog.rfq_id.in_(sent_ids),
        ActivityLog.rfq_id.not_in(replied_ids),
    )
).scalar()
```

## WARNING: Anti-Patterns

### WARNING: Deriving conversion from router logic

**The Problem:**
```python
# BAD — router checks presence of response JSON to infer "converted"
@router.get("/rfq/{rfq_id}/status")
async def rfq_status(rfq_id: int, db: Session = Depends(get_db)):
    responses = db.query(Response).filter_by(rfq_id=rfq_id).all()
    return {"converted": len(responses) > 0}  # Wrong proxy
```

**Why This Breaks:**
1. `Response` rows are created when Claude parses an email — not when an Offer is accepted
2. Low-confidence replies (0.5–0.8) create flagged responses but not Offers
3. The router now owns business logic that belongs in `app/services/`

**The Fix:**
```python
# GOOD — check OfferStatus directly
from app.constants import OfferStatus
from app.models import Offer

converted = db.execute(
    select(func.count(Offer.id)).where(
        Offer.rfq_id == rfq_id,
        Offer.status == OfferStatus.ACCEPTED,
    )
).scalar() > 0
```

### WARNING: Missing feature flag guard

NEVER write to `activity_log` without checking `activity_tracking_enabled`. In test environments this flag is `false`, and unguarded writes cause integrity errors against the SQLite test DB.

## Checklist: Auditing a New Funnel Stage

Copy this checklist and track progress:
- [ ] Identify the status column or `activity_type` string that marks this stage
- [ ] Confirm the write happens in a service (not a router)
- [ ] Add `if settings.activity_tracking_enabled:` guard
- [ ] Write a drop-off query that counts entries at this stage vs. the prior stage
- [ ] Add a pytest fixture that creates rows in both states
- [ ] Verify the query returns correct counts against the fixture
