# Resell Outreach & Buyer-Tracking — Design (rough draft)

> Companion to the Resell module (`2026-06-23-resell-module-design.md`, shipped). Adds the
> **outbound** half: help a trader decide *who to offer excess to*, track *who they offered to* +
> the response, build the company's record of *good bidders + what they bid on*, and stop the team
> *overlapping or forgetting important companies*. North star: **simple / clean / easy to understand,
> flexible to a diverse workflow.** Grounded in the outreach audit (2026-06-23).

## Concept — one action, one tracker

The module collects inbound broker offers today. This adds the **outbound** side:

- From a posted list (or selected lines, or a single part): one button **"Offer to buyers."**
- Opens a **buyer panel**: suggestions ranked **bought-this-part → buys-this-commodity →
  engaged-recently**, each with a one-glance chip (last bid · win-rate · last-contacted · soft
  *"Dana offered this to Acme 6d ago"* overlap flag). Add anyone manually (CRM/vendor search). Scope
  pre-filled (whole list / these lines), editable.
- **Send** (email — reuses the RFQ engine; replies auto-advance status) **or Log** (one tap for
  phone / Teams / BrokerBin, with a channel tag).
- Everything lands in **one "Outreach" view** on the list: *who · when · by whom · channel · status*
  (sent → opened → responded → bid / declined), reading as **"offered to 8 · 5 responded · 3 bid."**
  The bids that come back are the `ExcessOffer`s already collected — outreach and offers are one story.

**Channel decision (user):** tracker-centric, **email is the most common** channel → email is the
first-class automated spoke; other channels are one-tap logs into the same tracker.

**Assertiveness (user = HYBRID):** **overlap = advisory** (warn, never block; log the override);
**don't-forget = active** (auto follow-up Tasks in "My Day" + a "buyers you usually offer this
commodity to, but haven't this round" strip).

## Simple-but-flexible

One entry point; suggestions up front; advanced bits (manual add, re-offer, per-line tweaks,
non-email log) behind progressive disclosure. The tracker is identical across channels, so an
email-heavy desk that also works phone/IM/marketplace all flows to one place. Built from the live
design system (shared macros, `splitPanel`, existing `status_badge` keys — no new vocabulary), to
match the just-shipped Resell workspace.

## Data model

**New:**
- **`ExcessOutreach`** (the tracking spine — a parallel to the sales `Contact`/RFQ record, NOT bolted
  onto it): `excess_list_id` (FK), `excess_line_item_id` (nullable — per-line tracking),
  `target_vendor_card_id` (canonical "who" — see below), `submitted_by` (FK User — for team-overlap),
  `channel` (email / phone / teams / marketplace / other), `status` (sent → opened → responded →
  bid → declined / no_response), `graph_message_id` / `graph_conversation_id` (nullable — email only),
  `parts_included` (JSON), `sent_at`, `created_at`. One row per buyer×line (compose is per-list).
- **`BuyerScore`** rollup (invert the vendor scorecard): per `vendor_card_id` — `offers_received`,
  `wins`, `avg_bid_pct_of_ask`, `response_rate`, `median_response_hours`, `last_offered_at`,
  per-commodity affinity (JSON) — fed from `ExcessOffer` + `ExcessOutreach`. Recompute on offer-win +
  nightly backstop.

**Extend:**
- **`ActivityLog`** — add nullable `excess_list_id` scope (CRM Phase 3 generalizes the activity layer;
  align to it) so outreach events write to the same immutable timeline + cadence clocks.
- **`ExcessOffer`** — resolve the dual counterparty (`offerer_company_id` vs `offerer_vendor_card_id`)
  to **`vendor_card_id` as canonical** (it carries the engagement/score columns); backfill a card for
  company-only offerers. One "who" to track / score / dedup against.

## Reuse (≈70% — per the audit; do NOT reinvent)

| Need | Reuse |
|---|---|
| Batch email send (parallel Graph, DNC-at-send, save-to-sent, retry) | `send_batch_rfq` (already entity-agnostic via `parts_map`) — thin adapter to buyer contacts |
| Reply ingestion + 4-tier vendor-scoped matching + per-message dedup | `poll_inbox`, `_scope_thread_contacts_to_sender` |
| AI parse → offer extraction | `_auto_create_offers_from_parse`, `response_parser` |
| Coverage-ranked, unavailability-filtered, cardless suggestion shape | `_coverage_ranked_vendor_rows` (adapt: rank *buyers who buy this* vs *vendors who stock it*) |
| Counterparty intelligence + clocks | `VendorCard` engagement cols, `VendorContact.relationship_score`/trend, `cadence_service` |
| Score computation pattern | `vendor_score.compute_all_vendor_scores`, `engagement_scorer` (invert → buyer side) |
| "Don't offer again" durable marking + 3-state UI | `vendor_unavailability` / `VendorPartUnavailability` (flip to buyer-side) |
| Clean-export provenance discipline | `bid_back_service.bid_back_export_context` whitelist |
| Workspace shell / lens / stat-strip | the Resell workspace (`resell.py` + `partials/resell/*`) |

## Ride on the CRM-cleanup (`healthy-crm-foundations.md`) — do NOT duplicate

- **Phase 2 (Tasks + "My Day"):** outreach follow-ups ("offer Q1 surplus to Acme") are **generalized
  account/contact Tasks** surfaced in **My Day** — not a resell-specific task system. The "don't-forget"
  active nudges create these Tasks.
- **Phase 3 (Activity completeness):** outreach events + the per-activity follow-up flag use the
  generalized `ActivityLog` (+ the new `excess_list_id` scope).
- **Phase 1 (contact_owner_id / tags):** the buyer directory + "important companies" leans on CRM
  Company/Contact fields + tags.

**Sequencing:** build outreach **after CRM Phase 2 lands** (Tasks + My Day) so the nudge/follow-up
half has its substrate. The core (send + tracker + buyer scorecard + suggestions + advisory overlap)
can land first; wire the active My-Day nudges when Phase 2 is in.

## Suggestions, scorecard, overlap, nudge (behaviors)

- **Who-to-offer ranking:** tiered — exact `material_card_id` bought-before → `commodity_tags` →
  engagement tiebreak (mirrors the coverage-then-engagement rank tuple). Buyer-keyed, fed from
  `ExcessOffer`/won `Offer` history. Built on a fresh **buyer-side** summary (NOT the `customer_excess`
  Sighting mirror — that's *supply* with the customer hidden; conflating risks leaking the seller).
- **Buyer scorecard:** passive rollup; surfaces as the suggestion chips + a buyer profile panel.
- **Overlap (advisory):** at compose, soft inline flag "teammate offered N of these to this buyer Nd
  ago — still send?"; logs override. Never blocks (re-offers/follow-ups are legitimate).
- **Don't-forget (active):** a per-list "usually offered this commodity to X/Y, not yet this round"
  strip + auto follow-up Tasks → My Day.

## Out of scope (v1)
- External buyer portal/auth (offers still arrive via email/manual, as today).
- Buyer-side multi-channel opt-out beyond a simple "no excess offers" flag + per-part "don't offer
  again" (mirrors `VendorPartUnavailability`).
- Auto-send / sequences / drip (manual-trigger only).

## Open decisions (resolved defaults — flag to change)
1. Tracking record → **new `ExcessOutreach`** (not relaxing `Contact.requisition_id`). ✓ engineering call.
2. Suggestion ranking → **MPN → commodity → engagement**. ✓
3. Overlap → **advisory, never block** (per HYBRID). ✓
4. Granularity → **per-list compose, per-(buyer×line) tracking rows**. ✓
5. Affinity source → **fresh buyer-side summary** (not the supply Sighting mirror). ✓
6. Scorecard compute → **on offer-win + nightly backstop**. ✓
7. Counterparty canonicalization → **`vendor_card_id` primary**, backfill cards. ✓
8. Don't-forget → **active (My-Day Tasks), after CRM Phase 2**. ✓

## Also: Part-A refinements (quick wins, independent of this feature)
From the audit, fix on the existing Sales/Sightings outreach (all "should", small): render
`lead_time_days` on all selectable vendor rows; move `[ref:{req_id}]` token into the compose subject;
per-`[REQ-N]` grouping in the cross-req preview body; `vendor_score`-vs-`engagement_score` tooltip;
and the high-leverage **commodity-segmented engagement read** (which also seeds this feature's
buyer-affinity ranking). No must-fix; the core outreach path is safe and sound.

---

## Grounding update (2026-06-23, against main 9bfa7d57)

**CRM Phase 2 (generalized Tasks + My-Day) is NOT yet on main** (`app/models/task.py` still only
`RequisitionTask`; no My-Day surface). Reuse anchors confirmed present: `email_service.send_batch_rfq`/
`poll_inbox`, `ExcessOffer.offerer_company_id`/`offerer_vendor_card_id`, `ActivityLog` (intelligence.py).

**Build decision (user said build now):** ship the full CORE now —
`ExcessOutreach` record, send (reuse `send_batch_rfq` via adapter) + manual log, the unified Outreach
tracker, who-to-offer suggestions (MPN→commodity→engagement), `BuyerScore` rollup, advisory overlap,
`ActivityLog.excess_list_id` scope, `ExcessOffer` counterparty→`vendor_card` canonicalization. The
**don't-forget** active half ships via the EXISTING cadence/nudge system + a self-contained
"usually-offered-this-commodity, not yet this round" strip on the list. **DEFER only** the
"auto-create follow-up Tasks in My-Day" sub-piece behind a clean seam (a single hook) until CRM
Phase 2 lands — do NOT build a parallel Task/My-Day system.
