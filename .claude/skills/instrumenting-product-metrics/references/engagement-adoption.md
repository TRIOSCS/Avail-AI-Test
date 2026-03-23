# Engagement & Adoption Reference

## Contents
- Engagement signals in AvailAI
- Activity timeline as engagement record
- Inactivity thresholds from config
- Reactivation signal model
- Buyer leaderboard as engagement driver
- Anti-patterns

---

## Engagement Signals

AvailAI's engagement signals are CRM-level, not session-level. The platform does not use page-view analytics. Engagement is measured by:

| Signal | Source | Threshold |
|--------|--------|-----------|
| Last outbound activity | `activity_log.direction='outbound'` | `customer_inactivity_days=30` |
| Strategic account inactivity | same | `strategic_inactivity_days=90` |
| Warning window | computed | `customer_warning_days=23` |
| Vendor not contacted | `activity_log` | `vendor_protection_warn_days=60` / `drop_days=90` |

All thresholds live in `app/config.py` — never hardcode them.

```python
from app.config import get_settings
from app.services.activity_service import days_since_last_activity

settings = get_settings()

days_inactive = await days_since_last_activity(db=db, company_id=company_id)
is_at_risk = days_inactive >= settings.customer_warning_days
is_churned = days_inactive >= settings.customer_inactivity_days
```

---

## Activity Timeline as Engagement Record

`get_account_timeline()` returns paginated `ActivityLogRead` objects. Use it to render CRM timeline partials and to compute engagement scores.

```python
from app.services.activity_service import get_account_timeline

timeline = await get_account_timeline(
    db=db,
    company_id=42,
    channel="email",          # optional filter
    direction="outbound",     # optional filter
    limit=20,
    offset=0,
)
# timeline.items: List[ActivityLogRead]
# timeline.total: int
```

---

## Reactivation Signals

The `reactivation_signals` table (`app/models/intelligence.py`) stores computed risk flags. Query it to drive in-app alerts.

```python
from app.models.intelligence import ReactivationSignal
from sqlalchemy import select

at_risk = db.execute(
    select(ReactivationSignal)
    .where(ReactivationSignal.company_id == company_id)
    .where(ReactivationSignal.signal_type == "churn_risk")
    .order_by(ReactivationSignal.created_at.desc())
).scalars().all()
```

Surface these in the command center partial, not as pop-ups. Users trust the timeline; interruptions reduce trust.

---

## Buyer Leaderboard as Engagement Driver

The `buyer_leaderboard_snapshot` table drives competitive engagement. Points map directly to actions:

```python
# app/models/performance.py
# points_offers: offers logged
# points_quoted: offers moved to quoted
# points_buyplan: offers added to buy plan
# points_po: POs confirmed
# points_stock: stock lists uploaded
# total_points: sum
```

Surface rankings in the dashboard partial. Only show rank delta (vs. prior month) — absolute rank without context is demotivating.

---

## Anti-Patterns

**NEVER compute "engagement score" inline in a router.** The scoring logic belongs in `app/services/`. Routers return the result; they don't compute it.

**NEVER use session duration as an engagement proxy.** AvailAI is a task tool; short focused sessions are high-value. Use action completion (RFQ sent, offer received) not time-on-page.

**NEVER show reactivation nudges on every page load.** Gate them: show once per day at most, only on the company/vendor detail page where action is possible.

---

## Related Skills

- See the **orchestrating-feature-adoption** skill for nudge placement and dismissal patterns
- See the **redis** skill for caching leaderboard queries (expensive aggregations)
- See the **mapping-user-journeys** skill to trace where engagement drops off
