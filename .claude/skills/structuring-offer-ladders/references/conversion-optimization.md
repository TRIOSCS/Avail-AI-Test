# Conversion Optimization Reference

## Contents
- Offer pipeline conversion triggers
- Evidence tier upgrade paths
- Proactive match → sent conversion
- Anti-patterns: blocking tier progression

---

## Offer Pipeline Conversion Triggers

The pipeline has four stages. Each stage has a specific FastAPI endpoint that drives the transition:

| Stage | Trigger | Endpoint |
|-------|---------|----------|
| Base offer | Offer logged manually or via AI parse | `POST /api/offers/` |
| Quoted | Offer added to a quote | `POST /v2/partials/quotes/{id}/add-offer/{offer_id}` |
| Buy plan | Quote → buy plan conversion | `POST /v2/buy-plans/` |
| PO closed | Buy plan marked fulfilled | `PUT /api/buy-plans/{id}/status` |

Track these in `MultiplierScoreSnapshot` (`app/models/performance.py`). Points are **non-stacking**: an offer that reaches PO earns only PO-tier points, not the sum of all stages.

```python
# GOOD — take max across stages
stage_pts = max(
    snapshot.offers_base_pts,
    snapshot.offers_quoted_pts,
    snapshot.offers_bp_pts,
    snapshot.offers_po_pts,
)

# BAD — summing stages double-counts every advanced offer
stage_pts = (
    snapshot.offers_base_pts
    + snapshot.offers_quoted_pts
    + snapshot.offers_bp_pts
    + snapshot.offers_po_pts
)
```

---

## Evidence Tier Upgrade Paths

Tiers T1–T3 are trusted (green badge). T4 is amber — AI-parsed, unreviewed. T5–T7 are untrusted (muted).

Upgrade path for T4 offers:
1. Parse confidence ≥ 0.8 → auto-approve → stays T4 but `status = active`
2. Human reviews pending_review offer → sets `promoted_by_id`, `promoted_at`, bumps tier

```python
# app/routers/offers.py — tier promotion
@router.put("/api/offers/{offer_id}/approve")
async def approve_offer(offer_id: int, db: Session = Depends(get_db), user=Depends(require_buyer)):
    offer = db.get(Offer, offer_id)
    offer.status = OfferStatus.ACTIVE
    offer.promoted_by_id = user.id
    offer.promoted_at = datetime.now(timezone.utc)
    db.commit()
```

---

## Proactive Match → Sent Conversion

Conversion funnel: Match surfaces → buyer prepares → offer sent → customer converts.

```jinja2
{# proactive/list.html — conversion rate shown in scorecard #}
<p class="text-2xl font-bold text-brand-700">
  {{ "%.0f%%"|format(stats.get('conversion_rate', 0) * 100) }}
</p>
```

The `Convert` button in `proactive/list.html` fires `POST /v2/partials/proactive/{offer_id}/convert`. This is the critical conversion event — wire activity tracking here.

---

## WARNING: Blocking Tier Progression with Stale Flags

**The Problem:**

```python
# BAD — stale offers silently excluded from pipeline scoring
offers = db.query(Offer).filter(Offer.is_stale == False).all()
```

**Why This Breaks:**
1. `is_stale` is a **display-only** flag set after 14 days — it does not represent commercial validity.
2. Filtering it out removes offers that were already quoted or in a buy plan, causing the pipeline score to under-count advanced stages.
3. Buyers lose credit for offers they legitimately progressed before the stale threshold.

**The Fix:**

```python
# GOOD — filter stale only for display lists, never for pipeline scoring
offers_for_display = db.query(Offer).filter(Offer.is_stale == False).all()
offers_for_scoring = db.query(Offer).all()  # or filter by status only
```

---

## Conversion Workflow Checklist

Copy this checklist when adding a new pipeline stage:

- [ ] Define the status transition in `app/constants.py` StrEnum
- [ ] Add the FastAPI endpoint that drives the transition
- [ ] Update `MultiplierScoreSnapshot` columns if a new point category is introduced
- [ ] Add HTMX partial swap to reflect the stage change in the UI
- [ ] Wire an activity tracking call at the transition point
- [ ] Add a pytest test asserting the transition updates the correct status and score field
