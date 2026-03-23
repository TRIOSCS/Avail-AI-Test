# Strategy & Monetization Reference

## Contents
- Bonus qualification via multiplier score
- Proactive revenue tracking
- Vendor reliability score as tier gate
- Pipeline value vs. closed value distinction
- Anti-pattern: conflating offer count with pipeline value

---

## Bonus Qualification via Multiplier Score

The `MultiplierScoreSnapshot` model (`app/models/performance.py`) drives the bonus system. Buyers qualify at `$500` or `$250` based on `qualified` and `bonus_amount` columns:

```python
# MultiplierScoreSnapshot columns that determine bonus outcome
qualified     = Column(Boolean, default=False)  # met threshold?
bonus_amount  = Column(Float, default=0)        # $500, $250, or 0
avail_score   = Column(Float, default=0)        # cached AvailScore for threshold check
rank          = Column(Integer)                 # peer rank this month
```

The threshold logic lives in `services/multiplier_score_service.py`. When adjusting point values for pipeline stages, always recalculate whether the thresholds still make qualification achievable at realistic activity levels — if the bar is too high, buyers disengage.

---

## Proactive Revenue Tracking

Revenue from proactive matching is tracked via `ProactiveOffer.total_sell` once an offer is converted. Surface it in the scorecard:

```python
# Aggregated in htmx_views.py for proactive/scorecard.html
total_revenue = db.scalar(
    select(func.sum(ProactiveOffer.total_sell))
    .where(ProactiveOffer.status == "converted")
) or Decimal("0")
```

```jinja2
{# proactive/scorecard.html — revenue tile #}
<div class="bg-gray-50 rounded-lg p-3 text-center border border-gray-200">
  <p class="text-xs text-gray-500 font-medium mb-1">Revenue</p>
  <p class="text-2xl font-bold text-gray-900">${{ "{:,.0f}".format(stats.get('total_revenue', 0)) }}</p>
</div>
```

This is **sent-and-converted** revenue only. Offers that are sent but not yet responded to do not count. This is intentional — it reflects real closed value, not pipeline optimism.

---

## Vendor Reliability Score as Tier Gate

The 6-factor vendor scoring in `app/scoring.py` produces a `reliability_score` on the `VendorCard` model. High reliability scores gate automatic T3 promotion (see growth-engineering.md). This creates a monetization-aligned feedback loop:

- Buyers who send more RFQs to reliable vendors → more T3 auto-promotions → cleaner pipeline → higher pipeline scores → closer to bonus threshold

Model the incentive explicitly when setting reliability thresholds:

```python
# app/scoring.py — reliability threshold for auto-promotion
# Set this high enough that only genuinely consistent vendors qualify.
# Too low: T4 → T3 promotions become meaningless, trust signals degrade.
# Too high: buyers get no benefit from cultivating vendor relationships.
AUTO_PROMOTE_THRESHOLD = 0.92  # tune based on vendor distribution
```

---

## Pipeline Value vs. Closed Value Distinction

NEVER conflate these two in reporting or UI copy:

| Term | What it means | Where it lives |
|------|--------------|----------------|
| Pipeline value | Offers logged × unit_price × qty (estimated) | `Offer` table, `status = active` |
| Quoted value | Offers added to quotes | `Quote` + `Offer` join |
| Closed value | Offers converted to POs | `BuyPlan` fulfilled records |
| Proactive revenue | Proactive offers converted | `ProactiveOffer.total_sell` |

Display them separately in any performance dashboard. Mixing them creates inflated numbers that erode trust in the reporting.

---

## WARNING: Conflating Offer Count with Pipeline Value

**The Problem:**

```python
# BAD — treating offer count as a proxy for pipeline health
pipeline_health = snapshot.offers_total  # just a count
```

**Why This Breaks:**
1. A buyer can log 200 tiny offers worth $0.01 each and outscore a buyer with 10 offers worth $50,000 each.
2. The multiplier score is already designed to prevent this via non-stacking stages — but raw count metrics elsewhere undermine the design.
3. Management sees inflated activity numbers and sets unrealistic quotas.

**The Fix:**

```python
# GOOD — use stage-weighted points, not raw counts
pipeline_pts = snapshot.total_points  # weighted for stage reached

# Or compute estimated pipeline value
pipeline_value = db.scalar(
    select(func.sum(Offer.unit_price * Offer.qty_available))
    .where(Offer.status == "active", Offer.entered_by_id == user_id)
)
```

See the **clarifying-market-fit** skill for how to communicate pipeline vs. closed value to end users.
