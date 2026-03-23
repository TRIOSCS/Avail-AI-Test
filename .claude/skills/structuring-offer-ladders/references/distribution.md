# Distribution Reference

## Contents
- How tiers distribute through the offer pipeline
- RFQ → inbox → parse distribution flow
- Proactive matching distribution model
- Anti-pattern: bypassing the tier assignment

---

## How Tiers Distribute Through the Offer Pipeline

Offers enter the system from multiple sources, each with a fixed starting tier. The tier is set at creation and only upgraded by explicit human action or a high-confidence AI parse.

| Source | Starting tier | Entry point |
|--------|--------------|-------------|
| Manual buyer entry | T2 | `POST /api/offers/` |
| RFQ email reply (AI parsed, confidence ≥ 0.8) | T4, auto-active | `app/services/response_parser.py` |
| RFQ email reply (confidence 0.5–0.8) | T4, pending_review | `app/services/response_parser.py` |
| Vendor stock list upload | T2 | excess/stock list import |
| AI web search (Sourcengine, eBay, etc.) | T5–T6 | `app/search_service.py` |
| Proactive match sourced from offer | inherits source tier | `app/services/proactive_service.py` |

This means the tier distribution of active offers reflects the team's RFQ throughput. If most offers are T4, the inbox monitor is doing most of the work. If T2 dominates, buyers are entering offers manually.

---

## RFQ → Inbox → Parse Distribution Flow

```
Graph API inbox poll (every 30min, app/jobs/inbox_monitor.py)
  → response_parser.py (Claude extracts: price, qty, lead time, condition, date code)
    → confidence ≥ 0.8 → Offer(evidence_tier="T4", status="active")
    → confidence 0.5–0.8 → Offer(evidence_tier="T4", status="pending_review")
    → confidence < 0.5 → Offer(evidence_tier="T4", status="rejected")
```

The parse confidence bar in `shared/offer_card.html` makes this distribution visible to buyers:

```jinja2
{% if offer.parse_confidence %}
<div class="w-16 h-1.5 bg-gray-200 rounded-full overflow-hidden">
  <div class="h-full bg-brand-500 rounded-full"
       style="width: {{ (offer.parse_confidence * 100)|int }}%"></div>
</div>
<span class="text-xs text-gray-400">{{ '{:.0f}'.format(offer.parse_confidence * 100) }}%</span>
{% endif %}
```

---

## Proactive Matching Distribution Model

The proactive match score (0–100) is a SQL scorecard in `proactive_service.py`. It factors:

- Part match (MPN similarity against customer purchase history)
- Quantity fit (vendor qty vs. customer's typical buy qty)
- Price vs. historical (offer price vs. what customer paid before)
- Vendor reliability (from vendor scoring in `app/scoring.py`)

Offers scoring ≥ 80 are the highest-value distribution targets. Wire the score threshold into sort order and visual prominence — don't surface low-scoring matches at the same visual weight as high-scoring ones:

```python
# app/services/proactive_service.py — sort by score descending
matches = sorted(raw_matches, key=lambda m: m["score"], reverse=True)
```

---

## WARNING: Bypassing Tier Assignment

**The Problem:**

```python
# BAD — creating an offer without setting evidence_tier
offer = Offer(
    vendor_name="Acme",
    mpn="NE555",
    unit_price=0.12,
    qty_available=5000,
)
db.add(offer)
```

**Why This Breaks:**
1. `evidence_tier` defaults to `NULL` in the DB schema — the badge in `shared/offer_card.html` falls back to T5 visual styling silently.
2. Buyers see a muted badge and lose trust in the data, even if the offer is from a reliable source.
3. Pipeline scoring queries that filter by tier will exclude NULL rows, understating the buyer's contribution.

**The Fix:**

```python
# GOOD — always set tier at creation
offer = Offer(
    vendor_name="Acme",
    mpn="NE555",
    unit_price=0.12,
    qty_available=5000,
    evidence_tier="T2",  # manual entry from stock list
    source="manual",
)
```

---

## Distribution Checklist for New Offer Sources

When adding a new connector or intake path, validate tier assignment:

- [ ] Confirm which tier the new source starts at (see table above)
- [ ] Set `evidence_tier` at offer creation in the connector/service
- [ ] Add the source string to `offer.source` (e.g., `"digikey"`, `"brokerbin"`)
- [ ] Verify the badge renders correctly in `shared/offer_card.html`
- [ ] Write a pytest test asserting the tier is set correctly on import
