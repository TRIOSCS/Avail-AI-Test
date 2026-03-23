# Growth Engineering Reference

## Contents
- Automating tier upgrade triggers
- Inbox monitor as growth lever
- Score-driven match surfacing
- Pending review queue as activation event
- Anti-pattern: manual-only tier progression

---

## Automating Tier Upgrade Triggers

The fastest way to grow the T1–T3 offer pool is to automate the T4 → T3 upgrade path. The inbox monitor already does the first step (AI parse). Add an auto-upgrade rule for consistently reliable vendors:

```python
# app/services/response_parser.py — auto-promote trusted vendors
TRUSTED_VENDOR_THRESHOLD = 0.95  # reliability score

async def parse_and_store_offer(vendor_response, db):
    offer = build_offer_from_parse(vendor_response)
    vendor = db.get(VendorCard, offer.vendor_card_id)

    if vendor and vendor.reliability_score >= TRUSTED_VENDOR_THRESHOLD:
        offer.evidence_tier = "T3"   # upgrade from T4
        offer.promoted_at = datetime.now(timezone.utc)
    else:
        offer.evidence_tier = "T4"

    db.add(offer)
    db.commit()
```

This keeps the pending_review queue focused on genuinely ambiguous replies rather than noise.

---

## Inbox Monitor as Growth Lever

The APScheduler inbox monitor (`app/jobs/inbox_monitor.py`) fires every 30 minutes. More RFQs sent → more replies → more T4 offers → more pipeline. The growth loop is:

```
Send RFQ (email_service.send_batch_rfq)
  → Vendor replies
    → inbox_monitor.py parses reply (every 30min)
      → parse_confidence ≥ 0.8 → Offer(T4, active) → pipeline
      → parse_confidence 0.5–0.8 → Offer(T4, pending_review) → review queue
```

Increasing RFQ send volume is the primary growth lever for T4 offer count. The scorecard on `proactive/scorecard.html` tracks the downstream conversion result.

---

## Score-Driven Match Surfacing

High-score proactive matches (≥ 80) should be surfaced more prominently than low-score ones. Use Alpine.js to highlight or pin them:

```jinja2
{# proactive/_match_row.html — pin high-score rows #}
<tr class="hover:bg-gray-50 transition-colors
           {% if match.score >= 80 %}ring-1 ring-inset ring-emerald-200 bg-emerald-50/30{% endif %}">
  ...
</tr>
```

And expose a filter by score threshold so buyers can focus on the highest-value opportunities:

```jinja2
{# proactive/list.html — score filter #}
<select name="min_score" hx-get="/v2/partials/proactive" hx-target="#main-content"
        hx-include="this" class="text-sm border-gray-200 rounded-lg">
  <option value="0">All scores</option>
  <option value="60">Score ≥ 60</option>
  <option value="80">Score ≥ 80</option>
</select>
```

---

## Pending Review Queue as Activation Event

The `pending_review` queue is where growth stalls if buyers don't check it. Treat clearing the queue as an activation event:

```python
# app/routers/htmx_views.py — surface pending review count in topbar
pending_count = db.scalar(
    select(func.count(Offer.id)).where(Offer.status == OfferStatus.PENDING_REVIEW)
)
# Pass to base.html context so the nav badge shows the count
```

```jinja2
{# base.html — pending review badge on nav #}
{% if pending_count > 0 %}
<span class="ml-1 px-1.5 py-0.5 text-[10px] font-bold bg-amber-500 text-white rounded-full">
  {{ pending_count }}
</span>
{% endif %}
```

See the **orchestrating-feature-adoption** skill for wiring this to feature discovery nudges.

---

## WARNING: Manual-Only Tier Progression

**The Problem:**

Relying entirely on buyers to manually promote T4 offers creates a bottleneck. If the team grows or RFQ volume spikes, the pending_review queue overflows and buyers start ignoring it.

**Why This Breaks:**
1. Buyers miss high-confidence T4 offers because they can't process the volume.
2. Good offers age out (marked `is_stale` after 14 days) before they're reviewed.
3. The pipeline score understates actual sourcing activity.

**The Fix:**

Apply the vendor reliability auto-upgrade rule (see above) and set confidence thresholds appropriately. Periodic batch review of T4 offers older than 7 days via a background job is a reasonable middle ground:

```python
# app/jobs/offer_review_reminder.py
async def flag_aged_pending_offers(db):
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    aged = db.execute(
        select(Offer)
        .where(Offer.status == "pending_review", Offer.created_at < cutoff)
    ).scalars().all()
    logger.info(f"{len(aged)} T4 offers pending review older than 7 days")
    # Emit activity event, notify buyer via in-app nudge
```
