# Composer Coverage-Discovery — Surface Cardless Vendors — Design

**Date:** 2026-06-15
**Status:** Approved (user chose "Ship coverage-discovery" after a 4-agent investigation
of the 86%-NULL-`vendor_card_id` finding).
**Builds on:** the bulk RFQ composer (PR #280) and its coverage-ranked suggestions.

## Problem

`_coverage_ranked_vendor_rows` (`app/routers/sightings.py:1351`) INNER-joins
`VendorSightingSummary` (VSS) → `VendorCard`, so a vendor with sightings but no card is
silently absent from the composer's suggestions. On live data that's **112 of 120**
real distributors (avnet, cyclops, comsit — clean, not noise) invisible: the composer
surfaces only the ~8 carded vendors. Root cause (investigation-confirmed, by-design):
the VSS materializer only *links* sightings to pre-existing cards, never creates one.

Separately, **100% of sightings carry no `vendor_email`** — contact emails live only on
cards. So a cardless vendor is not RFQ-able until someone gives it a contact. The fix
must therefore surface these vendors as **discovery** ("who has my parts?") while being
honest that most aren't reachable yet, and give a one-click path to make them reachable.

## Decision

Make the composer a coverage-**discovery** surface: every vendor with sightings on the
selected parts appears, ranked by coverage. A vendor that can't be RFQ'd today
(cardless, OR carded but no resolvable contact email) renders a neutral **"no contact on
file"** badge with a **disabled checkbox** (you can't select what the send path would
skip) and an **"Add contact"** action that pre-fills the existing inline vendor-create
form with that vendor's name — the buyer types a known email and the vendor becomes
selectable. **No bulk CRM writes; no new endpoint; no schema change.**

## Part 1 — Query: include cardless vendors (`_coverage_ranked_vendor_rows`)

Replace the INNER join + SQL `GROUP BY VendorCard.id` with an **outer join + Python
grouping** (avoids the GROUP-BY-entity SQLite/PG portability seam the fleet already
flagged; VSS is a few hundred rows — trivial in Python):

1. Query: `db.query(VendorSightingSummary, VendorCard).outerjoin(VendorCard,
   _vss_vendor_card_join()).filter(VendorSightingSummary.requirement_id.in_(req_id_list))`.
   A VSS row with no matching card yields `card=None` (cardless).
2. Drop blacklisted **only when carded**: a row is excluded iff
   `card is not None and card.is_blacklisted`. (Cardless vendors have no blacklist flag.)
3. Group in Python by a **group key**: `card.id` when carded, else
   `normalize_vendor_name(vendor_name)` (the canonical normalizer, matching the exclusion
   set — NOT raw `lower(trim)`; two name variants of the same cardless vendor merge). Per
   group accumulate: distinct `requirement_id` count (= `covered_count`), the non-null
   `score` values (→ `avg_score`), a representative `card` (any non-null; None if all
   cardless), and a display name (`card.display_name` if carded, else the
   lexicographically-min raw `vendor_name` in the group — deterministic).
4. Exclusion (unavailability): drop any group whose normalized name ∈ `excluded`
   (cardless: its group key; carded: `normalize_vendor_name(card.display_name or
   card.normalized_name)` — keep the existing belt-and-braces re-check).
5. `has_contact`: True iff the vendor is RFQ-able **by the same resolution the send path
   uses** — read `sightings_send_inquiry` / `send_batch_rfq` contact resolution and match
   it exactly (card present AND a resolvable email: `card.emails` non-empty OR a
   `VendorContact` with an email). The badge MUST be truthful: `has_contact` true ⇔ the
   send path would NOT skip this vendor. Resolve contact info in one batched query for the
   carded groups (no N+1).
6. Rank: `covered_count` desc, then `has_contact` desc (contactable above equal-coverage
   non-contactable), then `engagement_score` desc nullslast, then a stable tiebreak
   (group key). Cap 20.

`RankedVendor` (NamedTuple) becomes: `card: VendorCard | None`, `vendor_name: str`,
`covered_count: int`, `avg_score: float | None`, `has_contact: bool`. Update its
docstring. Every consumer (modal context build, MPN-title lookup, affinity dedup at
`sightings.py:1524`) updates to the new shape — the affinity "already-suggested" set keys
on each row's normalized name (`normalize_vendor_name(r.vendor_name)`), which now covers
cardless rows too.

## Part 2 — Template (`vendor_modal.html` suggested-vendor rows)

- **Contactable row** (`has_contact`): unchanged from today — enabled checkbox wired to
  `toggleVendor`, engagement/response-rate badges, coverage chip.
- **Non-contactable row** (`not has_contact`): coverage chip unchanged; **disabled
  checkbox** (reuse the existing excluded-vendor disabled-checkbox pattern); a neutral
  **`bg-gray-100 text-gray-500`** "no contact on file" badge (gray = unknown/neutral;
  does not collide with rose=unavailable, amber=OOO/attention, emerald=positive,
  indigo=affinity); and an **"Add contact"** link (`text-brand-600` small link, the
  existing inline-action vocabulary). No engagement badges (no card / no data).
- All Tailwind classes full literals; single-quoted Alpine attrs around any Jinja.

## Part 3 — "Add contact" action (reuse Track B inline-create, no new endpoint)

The "Add contact" link is an Alpine `@click` that **pre-fills and reveals the existing
inline "Add new vendor" form** (Track B): set `newVendorName = '<vendor display name>'`,
`showNewVendorForm = true` (or the actual state name in `rfqVendorModal`), and focus the
email input. The buyer enters the known email → the existing `composer-vendor` POST
creates the card + `VendorContact` and fires `_background_enrich_vendor` (unchanged) →
the returned contactable row joins the selection via the existing append/dedup path.
Read the current `rfqVendorModal` factory + `vendor_modal.html` inline-form markup and
use its real state/method names; do not invent new ones. The `@click` must carry no
literal `"` inside a double-quoted Alpine attr (CLAUDE.md landmine) — single-quote it.

Enrichment is async, so "Add contact" without a typed email does not make the vendor
instantly contactable; the typed-email path is the instant one. That is acceptable and
honest — the form copy already lets the buyer supply the email.

## Out of scope (deliberate)

- No card auto-creation during VSS aggregation, no bulk backfill (the rejected Option A).
- No vendor-email capture at scrape time (the real long-tail lever — separate, flagged
  as the pre-rollout follow-up; tracked, not built here).
- The cardless suggested row may visually persist after its "Add contact" creates a
  carded row in the added-vendors section (minor duplication; selection dedup already
  prevents double-RFQ). Hiding it on success is a nice-to-have, not required.
- No change to affinity/send/preview semantics beyond consuming the new RankedVendor shape.

## Testing

- **Query** (`tests/test_sightings_router.py`): cardless vendor (VSS row, no card) on a
  selected requirement → appears in suggestions with `has_contact=False`; carded vendor
  with an email → `has_contact=True`; carded-but-no-email vendor → `has_contact=False`;
  a cardless vendor covering 2/2 parts ranks above a carded vendor covering 1/2;
  contactable ranks above non-contactable at equal coverage; two name-variant cardless
  rows merge into one group; excluded (unavailability) cardless vendor absent; blacklisted
  carded vendor absent; cap 20 honored.
- **Template**: contactable row → enabled checkbox + engagement badge; non-contactable →
  disabled checkbox + "no contact on file" badge + "Add contact" link; covered-MPN title
  still rendered.
- **Affinity**: a now-suggested cardless vendor is dropped from affinity results (dedup
  by normalized name still works through the new shape).
- **Regression**: an all-carded-contactable selection renders byte-equivalent to today
  (no badge, enabled checkboxes); single + multi-requisition coverage ranking unchanged
  for carded vendors.

## Risks

- The `has_contact` resolver MUST mirror the send-path skip logic, or the badge lies —
  this is the one correctness-critical coupling; pin it with the carded-no-email test.
- Python grouping must stay O(rows); batch the contact-info lookup (no N+1 over groups).
- Live data: confirm post-deploy that cardless vendors actually appear (they don't today).
