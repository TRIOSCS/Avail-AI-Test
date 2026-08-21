# Part / Offer Qualification — spec (v6 clean scope, building)

Status: **LOCKED SCOPE — "AI-assisted qualification & risk screening that
strengthens the existing quality process." Clean and effective, NOT a beast.
Building now. Owner directive 2026-08-20.**
Source of truth: David Tuckman's Part/Offer Qualification quick reference.

## SCOPE LOCK (v6) — what's in, what's deliberately out

**IN — the clean version (all from data we already have, plus ONE AI piece):**
1. **Fresh AI flags at buy-plan review** — compute-on-render (fixes the stale
   snapshot). ✅ shipped (tranche 1).
2. **Vendor-risk flag** from one shared vendor-safety computation. ✅ shipped.
3. **Below-market ("too cheap") flag** — computed from the offers already loaded on
   the same requirement; no market sweep, no new data. ✅ built.
4. **Offer Pre-check** — reuse the vendor-safety computation + `safety_review`
   signal labels in the offer drawer: vendor risk band + caution signals; read-only,
   shares the plan's computation so they agree. ✅ built.
5. **Language / contradiction screen (deterministic, not LLM — more reliable for
   these signals)** — flags evasive vendor wording ("New & Original") and the
   claims-in-stock-but-quotes-lead-time contradiction; advisory, cited; surfaced on
   the offer Pre-check and as a buy-plan line flag. LLM tone-pass is a later,
   optional upgrade. ✅ built.

**OUT — deferred to keep it lean (revisit only if asked):**
- Image / label vision analysis.
- A separate counterfeit-pattern AI model.
- MSL / PCN datasheet extraction + new part fields (the derivability gaps).
- Typed cert attachments / authorized-on-offer new capture fields.
- Any new DB column (v6 adds none — flags compute on read; AI findings ride the
  existing qualification blob).

Every AI signal stays advisory + cited + human-confirmed; nothing gates approval.
The rest of this doc (below) is the verified detail that scope draws from.

---

## (v5 detail — the verified foundation the v6 scope is drawn from)

## 0. What the verification found (why v5 corrects v4)

| v4 claim | Verdict | Reality |
|---|---|---|
| `BuyPlan.ai_flags` renders nowhere | ✅ confirmed | true — surfacing it is real value |
| "Qualification" name / gap-checklist duplication | ✅ confirmed | true (images/cert live in the ask-vendor request chips, not the gap checklist) |
| "Just render the stored `ai_flags` at review" | ❌ **bug** | it's a **build-time snapshot, never regenerated** on offer swap / qty edit / add-remove-line — the Deal Sheet is a live editor, so at approval it's stale, with orphaned line_ids and missing new lines. Must **compute fresh on render**. |
| "`generate_ai_flags` already produces the red flags" | ❌ overstated | only 1 of 6 emitted types (stale) is vendor/offer risk; the rest are economics. **6 of the owner's 7 red flags are net-new detectors.** |
| "All derivable, no new fields" | ❌ **fails** | **MSL and PCN/LTB dates don't exist in AVAIL at all** (no column, no data, nothing emits them); attachments are **untyped** so "no cert doc" can't be derived; **authorized-distributor never reaches a picked offer**; price-vs-market data is a 15-min cache with no durable store. |
| Reuse `safety_review` on offers | ⚠️ partial | feasible, but the vendor page *reads a stored lead band*; an offer with no lead needs a small refactor + one decision (show the stored band vs recompute) so the same vendor can't show two bands. |

## 1. Two logic bugs to fix regardless of scope

1. **Freshness.** `ai_flags` is written only at plan build (`buyplan_builder.py:313`)
   and never refreshed by `edit_buy_plan_line` / `bulk_edit_buy_plan_lines` /
   header edits / submit. **Compute it fresh on the review render** (call
   `generate_ai_flags(plan, db, region)` — it's read-only and re-queries live
   offers) rather than reading the stale column. This is a pre-existing latent bug,
   not just a new-feature concern.
2. **One shared vendor computation.** The offer Pre-check and the plan flags must
   consume the **same** `_compute_vendor_safety` output, not each re-derive vendor
   risk — otherwise the same vendor shows different risk on the offer vs the plan.

## 2. The honest existing-vs-new split

**Reusable (real):** the `ai_flags` shape + per-line loop + severity plumbing; the
`safety_review.html` band/caution template; `compute_market_baseline`; `qual_badge`
(untouched — it's completeness, not risk); the ask-vendor request chips (images /
cert / pkg gaps).

**Net-new detection (the real work):** below-market, no-certs, residential,
unauthorized, cancellation-history, counterfeit red-flag detectors — none in the
generator today. Two (cancellation, counterfeit) have data elsewhere to wire in;
four need data that isn't captured.

**Net-new data (doesn't exist):** MSL, PCN/LTB dates, a typed cert attachment,
authorized-on-offer, a durable authorized-price baseline.

## 3. Phase 1 — buildable now, from data that exists

1. **Render `ai_flags` at review, computed fresh** (fixes bug #1) — a plan-level
   strip in the Deal Sheet header + a per-line count badge (reuse the Issue/Edited
   chip idiom). This alone surfaces the stale/low-margin/better-offer/coverage/
   no-buyer/geo flags that are **computed today but invisible** — immediate value,
   zero new detection. (Resolve `quantity_gap`'s `line_id: None` for per-line
   surfacing.)
2. **Add the cancellation-history risk flag** to `generate_ai_flags`, sourced from
   the `_compute_vendor_safety` flags output (satisfies bug #2) — data already
   exists (`vendor.cancellation_rate` / `po_cancellations`).
3. **Offer Pre-check** — reuse `safety_review.html` for an offer-scoped band + the
   caution signals that already compute, via the small `_contactability_from_vendor_card`
   refactor; **decision: show the vendor's stored lead band when one exists** (so it
   can't disagree with the vendor page), else compute. Links to dossier/vendor for
   depth. Not a new panel, not the "Qualification" name, no second rose chip.
4. **Price-vs-market** — the one net-new metric worth it now: compare the offer's
   `unit_price` to the franchise median. Needs a **durable baseline** (persist the
   authorized baseline at sourcing / add `is_authorized` to a price snapshot) so it
   doesn't depend on a 15-min cache or a live sweep at build.

Phase 1 is real value with no fabricated data: the invisible flags become visible
and fresh, vendor risk shows once (consistently), and below-market pricing flags.

## 4. Phase 2 — needs new data capture or AI (the AI-for-quality work)

These are genuinely absent today; each needs a source, and AI is the natural one:

- **MSL level, PCN/LTB/LTS dates** → extract from the datasheet/PCN docs enrichment
  already captures (extend the enrichment schema). New field + AI emitter.
- **"Vague communication," "won't send images when told in stock"** → AI over the
  vendor email thread (extends the existing `qualify-ai` assist) — turns today's
  "manual" flags into derived ones.
- **Counterfeit-pattern signal** → AI synthesis over part+source+price+lifecycle,
  advisory + cited.
- **Typed cert attachment** → add a doc-type discriminator to `OfferAttachment` so
  "no cert on file" is real.
- **Authorized-distributor on the offer** → capture `is_authorized` at pick, or a
  `(material_card, vendor) → history` lookup at build (a market rollup, not proof
  this offer is franchise).
- **Part-label image analysis** (re-marking, mismatched date codes) → AI vision;
  the highest-leverage counterfeit check, its own effort.

Every Phase-2 AI signal stays advisory, cited, and human-confirmed — never gates a
buy (a false "clean" is worse than a false flag).

## 5. Naming, storage, guardrails

- **Naming:** offer-level = "Pre-check"; plan-level = the existing "flags"; leave
  "Qualification" to the completeness meter.
- **Storage:** Phase 1 adds **no new column** (flags computed on read; price-delta
  either computed live at offer entry or stored in the existing `Offer.qualification`
  blob; the durable price baseline reuses/extends a price-snapshot table). Phase 2's
  MSL/PCN/cert-type/authorized are the only genuinely new fields, deferred with their
  AI sources.
- **No new roles, no new gates.** Flags are informational at review.

## 6. Build order

**Phase 1 (one PR):** (1) fresh-compute + render `ai_flags` at review incl. the
per-line count and the `quantity_gap` line_id fix; (2) cancellation-history flag via
the shared `_compute_vendor_safety`; (3) offer Pre-check reusing `safety_review`
(the contactability refactor + stored-band decision); (4) durable price baseline +
the price-vs-market flag. Full suite + live-verify.

**Phase 2 (scoped separately, AI-led):** MSL/PCN enrichment extraction; the
email-thread language flags; typed cert attachment + authorized capture; then image
analysis as its own investment.

## 7. Net

The consolidation *direction* is right — AVAIL's plumbing is genuinely reusable —
but the honest picture is: **the detection is mostly new, and the most valuable
checks need new data that only AI can cheaply supply.** Phase 1 ships real,
correct, no-fabrication value (surface the invisible flags, freshly; one consistent
vendor risk; below-market pricing). Phase 2 is where the AI-for-quality investment
turns the rest of David's checklist from "not in AVAIL" into derived signals.
