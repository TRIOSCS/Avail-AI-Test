# Growth Engineering Reference

## Contents
- Growth Levers in AvailAI
- Virality via RFQ Volume
- Proactive Matching as Retention Loop
- Feature Flag-Gated Rollouts
- Checklist: Launching a Growth Experiment

## Growth Levers in AvailAI

AvailAI's growth is driven by increasing the number of completed buy plans per buyer per month. The three levers are:

1. **More searches** — lower friction on requirement creation
2. **More RFQs sent** — reduce vendor selection friction
3. **Faster offer acceptance** — surface best offer first (scoring algorithm)

All three are measurable via existing status columns and `activity_log`.

## Virality via RFQ Volume

Each sent RFQ creates an email thread in the vendor's inbox. Vendors that receive repeated RFQs become "trained" on AvailAI's format and reply faster — a compounding quality flywheel, not just distribution.

```python
# Track vendor reply rate improvement over time
from app.models.performance import VendorMetricsSnapshot
from sqlalchemy import select

snapshots = db.execute(
    select(VendorMetricsSnapshot.snapshot_date, VendorMetricsSnapshot.reply_rate)
    .where(VendorMetricsSnapshot.vendor_card_id == vendor_id)
    .order_by(VendorMetricsSnapshot.snapshot_date)
).all()
# Increasing reply_rate over time = vendor engagement loop working
```

## Proactive Matching as Retention Loop

Proactive matching converts passive buyers into active ones by surfacing relevant offers without requiring a new search. It is the primary retention mechanic.

```python
# Retention signal: buyers who reviewed ≥1 proactive match in last 30 days
from datetime import timedelta, timezone, datetime
from app.models import ActivityLog

cutoff = datetime.now(timezone.utc) - timedelta(days=30)
active_buyers = db.execute(
    select(ActivityLog.user_id, func.count().label("matches_reviewed"))
    .where(
        ActivityLog.activity_type == "proactive_match_reviewed",
        ActivityLog.created_at >= cutoff,
    )
    .group_by(ActivityLog.user_id)
).all()
```

## Feature Flag-Gated Rollouts

New growth features should be gated behind `system_config` or env-var flags before full release.

```python
# app/config.py pattern — add a new flag
class Settings(BaseSettings):
    enhanced_vendor_scoring: bool = Field(default=False, env="ENHANCED_VENDOR_SCORING")

# Service usage
settings = get_settings()
if settings.enhanced_vendor_scoring:
    score = compute_enhanced_score(sighting)
else:
    score = compute_legacy_score(sighting)
```

Set `ENHANCED_VENDOR_SCORING=true` in `.env` for staging, then promote to production after validating conversion improvement.

## WARNING: Anti-Patterns

### WARNING: A/B testing by branching in templates

**The Problem:**
```html
<!-- BAD — test variant embedded directly in Jinja2 template -->
{% if user.id % 2 == 0 %}
  <button>Send RFQ Now</button>
{% else %}
  <button>Request Quotes</button>
{% endif %}
```

**Why This Breaks:**
1. No way to measure which variant converted better — there's no event attached
2. The branch logic is coupled to the template, not to a measured flag
3. Removing the test requires a deploy

**The Fix:** Gate the variant behind a `system_config` flag, emit a `activity_type="cta_variant_shown"` event, and compare conversion rates by variant in a query.

## Checklist: Launching a Growth Experiment

Copy this checklist and track progress:
- [ ] Define the hypothesis: "If we change X, metric Y will increase by Z%"
- [ ] Identify the existing funnel stage this affects (search / RFQ / offer / quote)
- [ ] Add a feature flag in `app/config.py`
- [ ] Add an `activity_log` event for the variant exposure (`activity_type="experiment_shown"`)
- [ ] Add a conversion event for the outcome (`activity_type="experiment_converted"`)
- [ ] Write a drop-off query comparing variant vs. control
- [ ] Gate rollout behind the flag in staging, measure for ≥5 business days
- [ ] Promote to production or roll back

See the **orchestrating-feature-adoption** skill for feature flag wiring.
See the **instrumenting-product-metrics** skill for snapshot-based experiment analysis.
