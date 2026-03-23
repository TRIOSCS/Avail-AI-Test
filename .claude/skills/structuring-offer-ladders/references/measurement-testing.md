# Measurement & Testing Reference

## Contents
- Multiplier score snapshot queries
- Proactive conversion rate tracking
- Evidence tier distribution queries
- Testing tier logic with pytest
- Anti-pattern: measuring only final stage

---

## Multiplier Score Snapshot Queries

The `MultiplierScoreSnapshot` model (`app/models/performance.py`) is the primary measurement table for pipeline value. Query it for per-user, per-month breakdowns:

```python
# app/routers/performance.py — fetch buyer's monthly pipeline breakdown
from app.models.performance import MultiplierScoreSnapshot

snapshot = db.execute(
    select(MultiplierScoreSnapshot)
    .where(
        MultiplierScoreSnapshot.user_id == user_id,
        MultiplierScoreSnapshot.month == target_month,
        MultiplierScoreSnapshot.role_type == "buyer",
    )
).scalar_one_or_none()

if snapshot:
    pipeline = {
        "offers_logged": snapshot.offers_base_count,
        "offers_quoted": snapshot.offers_quoted_count,
        "offers_in_bp": snapshot.offers_bp_count,
        "offers_po": snapshot.offers_po_count,
        "total_pts": snapshot.total_points,
    }
```

---

## Proactive Conversion Rate Tracking

The scorecard at `/v2/partials/proactive/scorecard` surfaces four metrics. Query them in `htmx_views.py`:

```python
# htmx_views.py — proactive scorecard stats
total_sent = db.scalar(select(func.count(ProactiveOffer.id)))
total_converted = db.scalar(
    select(func.count(ProactiveOffer.id))
    .where(ProactiveOffer.status == "converted")
)
conversion_rate = total_converted / total_sent if total_sent else 0
total_revenue = db.scalar(
    select(func.sum(ProactiveOffer.total_sell))
    .where(ProactiveOffer.status == "converted")
) or 0

stats = {
    "total_sent": total_sent,
    "total_converted": total_converted,
    "conversion_rate": conversion_rate,
    "total_revenue": total_revenue,
}
```

---

## Evidence Tier Distribution Query

To audit how offers are distributed across tiers (useful for identifying if buyers are over-relying on unverified sources):

```python
from sqlalchemy import func
from app.models.offers import Offer

tier_counts = db.execute(
    select(Offer.evidence_tier, func.count(Offer.id).label("count"))
    .where(Offer.status == "active")
    .group_by(Offer.evidence_tier)
    .order_by(Offer.evidence_tier)
).all()
# Returns: [("T1", 12), ("T2", 45), ("T4", 201), (None, 3)]
```

A healthy distribution has T1–T3 > T4–T7. If T4 dominates, buyers need to review their pending_review queue.

---

## Testing Tier Logic with pytest

```python
# tests/test_services.py — verify tier assignment and confidence gating
def test_offer_tier_set_on_creation(db_session):
    offer = Offer(
        vendor_name="Acme",
        mpn="NE555",
        unit_price=0.12,
        qty_available=1000,
        evidence_tier="T2",
        source="manual",
    )
    db_session.add(offer)
    db_session.commit()
    assert offer.evidence_tier == "T2"


def test_high_confidence_parse_sets_active(db_session):
    offer = Offer(mpn="LM741", vendor_name="Test", parse_confidence=0.92, evidence_tier="T4")
    # simulate parser logic
    offer.status = "active" if offer.parse_confidence >= 0.8 else "pending_review"
    assert offer.status == "active"


def test_low_confidence_parse_requires_review(db_session):
    offer = Offer(mpn="LM741", vendor_name="Test", parse_confidence=0.63, evidence_tier="T4")
    offer.status = "active" if offer.parse_confidence >= 0.8 else "pending_review"
    assert offer.status == "pending_review"
```

See the **pytest** skill for fixture setup and `db_session` configuration.

---

## WARNING: Measuring Only the Final Pipeline Stage

**The Problem:**

```python
# BAD — only counts POs, misses offers that stalled at quote/buy plan
conversion_rate = po_count / total_offers
```

**Why This Breaks:**
1. An offer that reaches a buy plan but not a PO represents real commercial progress — ignoring it understates buyer performance.
2. The multiplier score system explicitly rewards each stage to prevent this — your measurement should match.
3. You lose visibility into where the pipeline is stalling (offer → quote vs. quote → BP vs. BP → PO).

**The Fix:**

```python
# GOOD — measure each stage transition rate separately
offer_to_quote_rate = quoted_count / base_count if base_count else 0
quote_to_bp_rate    = bp_count / quoted_count if quoted_count else 0
bp_to_po_rate       = po_count / bp_count if bp_count else 0
```

See the **instrumenting-product-metrics** skill for wiring these rates to activity tracking events.
