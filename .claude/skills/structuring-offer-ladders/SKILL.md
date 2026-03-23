---
name: structuring-offer-ladders
description: |
  Frames plan tiers, value ladders, and upgrade logic for the AvailAI sourcing platform.
  Use when: designing offer evidence tiers (T1–T7), modeling the offer-to-PO pipeline
  progression, building proactive match scoring, configuring multiplier score breakdowns,
  or writing UI copy that communicates tier value to buyers and sales reps.
allowed-tools: Read, Edit, Write, Glob, Grep, Bash
---

# Structuring Offer Ladders

AvailAI has two interlocking offer ladders: the **evidence tier** (T1–T7) that ranks data provenance trust, and the **pipeline progression** (offer → quoted → buy plan → PO) that tracks commercial value. A third ladder, the **proactive match score** (0–100), determines which vendor offers surface to customers. Each ladder has distinct display logic, upgrade triggers, and scoring weights.

## Quick Start

### Render evidence tier badge (Jinja2)

```jinja2
{# shared/offer_card.html — tier badge color follows T1–T3 = green, T4 = amber, T5+ = muted #}
{% set tier_num = offer.evidence_tier|replace('T', '')|int if offer.evidence_tier else 5 %}
<span class="inline-flex px-1.5 py-0.5 text-xs font-medium rounded-full
             {% if tier_num <= 3 %}bg-emerald-50 text-emerald-700
             {% elif tier_num == 4 %}bg-amber-50 text-amber-700
             {% else %}bg-brand-100 text-brand-600{% endif %}">
  {{ offer.evidence_tier }}
</span>
```

### Pipeline stage points (SQLAlchemy model)

```python
# app/models/performance.py — non-stacking: offer earns only its highest tier
offers_base_pts     = Column(Float, default=0)   # offer logged
offers_quoted_pts   = Column(Float, default=0)   # advanced to quote
offers_bp_pts       = Column(Float, default=0)   # advanced to buy plan
offers_po_pts       = Column(Float, default=0)   # closed as PO
```

### Proactive match score display

```jinja2
{# proactive/_match_row.html — score drives row prominence #}
<span class="inline-flex items-center justify-center w-8 h-8 rounded-full text-xs font-bold
             {% if match.score >= 80 %}bg-emerald-100 text-emerald-700
             {% elif match.score >= 60 %}bg-amber-100 text-amber-700
             {% else %}bg-gray-100 text-gray-500{% endif %}">
  {{ match.score }}
</span>
```

## Key Concepts

| Ladder | Range | Display Location |
|--------|-------|-----------------|
| Evidence tier | T1–T7 | `shared/offer_card.html` badge |
| Pipeline stage | base → quoted → BP → PO | `performance.py` `MultiplierScoreSnapshot` |
| Proactive score | 0–100 | `proactive/_match_row.html` |
| Parse confidence | 0.0–1.0 | `shared/offer_card.html` progress bar |

## Common Patterns

### Confidence threshold gate

Confidence ≥ 0.8 auto-approves; 0.5–0.8 flags for manual review. Match this in any UI that surfaces parsed data:

```python
# app/services/response_parser.py pattern
if offer.parse_confidence >= 0.8:
    offer.status = OfferStatus.ACTIVE
elif offer.parse_confidence >= 0.5:
    offer.status = OfferStatus.PENDING_REVIEW
else:
    offer.status = OfferStatus.REJECTED
```

### Non-stacking pipeline points

```python
# services/multiplier_score_service.py pattern
# Each offer earns points only for its HIGHEST achieved stage.
# Never sum across stages for the same offer.
stage_pts = max(base_pts, quoted_pts, bp_pts, po_pts)
```

### Tier promotion audit

```python
# Offers promoted from T4 to trusted tier require human review.
offer.promoted_by_id = current_user.id
offer.promoted_at = datetime.now(timezone.utc)
offer.evidence_tier = "T3"
```

## See Also

- [conversion-optimization](references/conversion-optimization.md)
- [content-copy](references/content-copy.md)
- [distribution](references/distribution.md)
- [measurement-testing](references/measurement-testing.md)
- [growth-engineering](references/growth-engineering.md)
- [strategy-monetization](references/strategy-monetization.md)

## Related Skills

- See the **frontend-design** skill for badge and tier UI components
- See the **htmx** skill for partial swaps on tier promotion events
- See the **jinja2** skill for tier-based conditional rendering
- See the **orchestrating-feature-adoption** skill for surfacing tier upgrades as nudges
- See the **mapping-user-journeys** skill for tracing how offers advance through pipeline stages
- See the **designing-onboarding-paths** skill for first-run empty states on each tier level
