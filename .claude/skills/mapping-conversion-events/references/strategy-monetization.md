# Strategy and Monetization Reference

## Contents
- AvailAI's Value Exchange
- Conversion Events That Signal Value Delivery
- Buy Plan as the Primary Monetization Signal
- Vendor Reliability as a Paid Differentiator
- WARNING: Measuring the Wrong Thing

## AvailAI's Value Exchange

AvailAI is a B2B tool. Value is delivered when a buyer closes a buy plan faster than they could without the platform. The monetization case rests on time-to-PO reduction and vendor reply rates — not on page views or logins.

**The three value-delivery moments:**
1. Sighting found in < 60 seconds (search beats manual emailing)
2. RFQ reply received from a reliable vendor
3. Buy plan created (demand fulfilled)

Each maps to a measurable event in `activity_log` or a status column.

## Conversion Events That Signal Value Delivery

```python
# Did the platform deliver a sighting faster than human alternatives?
from app.models import Requirement
from sqlalchemy import select, func

stmt = select(
    func.avg(
        func.extract("epoch", Requirement.updated_at - Requirement.created_at)
    ).label("avg_seconds_to_found")
).where(Requirement.status == RequirementStatus.FOUND)

avg_seconds = db.execute(stmt).scalar()
# < 60 seconds = platform is delivering on its core promise
```

```python
# Buy plans created in the last 30 days — primary monetization signal
from app.models import BuyPlan
from datetime import datetime, timedelta, timezone
from app.constants import BuyPlanStatus

cutoff = datetime.now(timezone.utc) - timedelta(days=30)
completed = db.execute(
    select(func.count(BuyPlan.id)).where(
        BuyPlan.created_at >= cutoff,
        BuyPlan.status == BuyPlanStatus.ACTIVE,
    )
).scalar()
```

## Buy Plan as the Primary Monetization Signal

A buy plan represents fulfilled demand. It is the equivalent of a completed transaction in a marketplace. Track it at the requisition level and at the buyer level.

```python
# Buyers with ≥1 completed buy plan in last 90 days = "active retained buyers"
from sqlalchemy import select, func, distinct
from app.models import BuyPlan

retained = db.execute(
    select(func.count(distinct(BuyPlan.created_by_user_id))).where(
        BuyPlan.created_at >= cutoff,
        BuyPlan.status == BuyPlanStatus.ACTIVE,
    )
).scalar()
```

## Vendor Reliability as a Paid Differentiator

The `avail_score_snapshot` table (0-100 composite) is the data asset that justifies premium positioning. Buyers with access to reliable-vendor routing close faster. Tracking score improvement over cohorts proves ROI.

```python
from app.models.performance import AvailScoreSnapshot

# Average score for vendors contacted via the platform vs. manually added
high_score_vendors = db.execute(
    select(AvailScoreSnapshot.vendor_card_id)
    .where(AvailScoreSnapshot.score >= 70)
    .order_by(AvailScoreSnapshot.snapshot_date.desc())
    .distinct()
).scalars().all()
```

## WARNING: Measuring the Wrong Thing

### WARNING: Using login count as a success metric

**The Problem:**
Daily active users is a vanity metric for AvailAI. A buyer who logs in daily but never creates a requisition is not getting value.

**The Fix:** Track `requirements_created`, `rfq_sent`, and `buy_plans_created` per buyer per month. These are the metrics that correlate with retained revenue.

### WARNING: Optimizing search result count instead of sighting quality

More sightings per search looks good on a dashboard but degrades buyer experience if sightings are low-quality (stale, mismatched quantity, unreliable vendor). The `avail_score_snapshot` already weights vendor reliability — use it in sighting ranking, not just in analytics.

See the **instrumenting-product-metrics** skill for the full scoring and snapshot architecture.
See the **structuring-offer-ladders** skill for offer-to-PO pipeline progression and tier modeling.
See the **clarifying-market-fit** skill for messaging that communicates these value signals to buyers.
