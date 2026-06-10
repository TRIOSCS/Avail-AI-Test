# Unavailability Temporal Policy — "Two Windows, Real Proof"

**Date:** 2026-06-10
**Status:** Adopted (user-approved). This document is the authoritative temporal /
release policy for the vendor-part-unavailability feature.
**Companion:** `docs/superpowers/specs/2026-06-10-vendor-part-unavailability-design.md`
(the main feature spec — data model v1, service surface, routes, UI). Where the main
spec describes *what* the feature does, this document defines *when a record
suppresses, when it stops, and what proof releases it early*.

One-line mental model: **time heals lot facts; only proof heals listing facts; an
unchanged echo is never proof.**

## 1. Core rule (buyer-facing UI copy, 2 sentences)

> "Marking a vendor unavailable hides their listings of this part for **30 days** —
> **6 months** if you said it's not really in stock, and **until you clear it** for a
> wrong part number. They come back early only on real proof — an authorized
> distributor showing stock, the vendor emailing you a stock list, an offer coming in,
> or a listing showing at least **double** the stock you flagged — and they always come
> back labeled with what you learned, plus a one-click re-verify."

## 2. Reason classes and windows

| class | reasons | active window | rationale (cost-asymmetry memo) |
|---|---|---|---|
| LOT | `bought_by_us`, `sold_elsewhere`, `broken`, `other` | **30d** (knob) | lot facts decay; proven past sources are the best restock candidates — bias against silent missed opportunity |
| LISTING | `not_really_there` | **180d** (knob) | vendor-behavior verdict; re-appearance of the same listing is the expected failure mode |
| IDENTITY | `different_part` | **never expires** (hard-coded constant, not a knob) | identity knowledge; a "restock" of a mislabeled listing is still the wrong part — zero opportunity upside to releasing |

**Active predicate** (pure read-time, Python-computed cutoffs, no cron, no lazy
writes — `sighting_stale_days` precedent):

```
released_at IS NULL AND (reason == different_part OR created_at >= now − window(reason))
```

## 3. Source trust classes

Module-level `Final` constants, computed from `source_type` / `is_authorized` — never
the stored `evidence_tier`, which is NULL on 4 of 6 persistence paths:

- **LIVE** = `source_type ∈ {digikey, mouser, element14}` OR `is_authorized is True`
  (covers authorized octopart/oemsecrets/sourcengine/NC rows).
- **HUMAN_DIRECT** = `{email_attachment}` only — a buyer *deliberately routed* a vendor
  email just now (`app/routers/sources.py`, user-initiated).
- **LISTING-CLASS (default for everything else)** = brokerbin, non-authorized
  aggregator rows, ebay, icsource, netcomponents, ai_live_web, **and** `excess_list`,
  `email_auto_import`, `stock_list`, `historical`, `vendor_affinity`, `""`/unknown,
  picker-inherited values. The explicit default fixes the unclassified-source hole:
  unknown types can only be stamped, never trigger a release. Demoting auto-mined
  documents (`excess_list` T7 / `email_auto_import` T5 per `app/evidence_tiers.py`) to
  listing-class kills the #1 judge-found failure — the weekly stale stock-list
  re-upload that resurrected marks on every inbox scan.

## 4. Suppression matrix

Per fresh row matching a record, in `apply_to_fresh_sightings`.

If the record is **not active** (window lapsed or `released_at` set): never stamp; row
renders **advisory** (history hint + verify affordance). If **active**, **dispatch on
the row's source class — NOT priority order**: the three overrides apply to mutually
exclusive classes (LIVE → O1 only, HUMAN_DIRECT → O3 only, listing-class → O2 only),
so the stronger evidence class always wins. In particular, a HUMAN_DIRECT row whose
qty also clears the O2 jump still RELEASES the record (S7 — the vendor sent a stock
list; under priority order O2 would shadow O3 and leave the mark active). O1 already
subsumes any O2-shaped signal on LIVE rows, since any qty difference triggers it.

| # | override | rule (NULL-safe, Python only, no raw_data, no SQL-over-JSON) | applies to |
|---|---|---|---|
| O1 | **Live truth** | class LIVE AND `qty_available > 0` AND `qty_available != qty_at_mark` (NULL snapshot ⇒ passes) → leave row unstamped, renders advisory+nudge. The `!=`-guard means a stale distributor-API echo showing the exact flagged qty stays stamped (marks on DigiKey/Mouser rows are no longer total no-ops) | ALL reasons incl. `different_part` (an authorized catalog match is identity evidence) |
| O2 | **Restock** | class LISTING (the default) AND `qty_at_mark` non-NULL AND fresh `qty_available` non-NULL AND fresh `> qty_at_mark` AND fresh `>= qty_at_mark × factor` (2.0); snapshot 0 ⇒ any fresh > 0. NULL either side = **no signal** (never "no change"). Row left unstamped → advisory+nudge. **No record mutation** — a one-off broker qty jitter surfaces one row this scrape and is re-stamped next scrape if the listing reverts: stateless, self-healing, no flap, no re-mark chore | LOT + LISTING reasons; disabled for `different_part` (more of the wrong part is still the wrong part) |
| O3 | **Vendor document** | class HUMAN_DIRECT AND `qty_available > 0` → **record-level release**: `released_at=now`, `release_trigger='vendor_email'`, one ActivityLog line, stamp nothing. Safe to write here: this path is a user-initiated router, not a background worker — no worker race | LOT + LISTING; disabled for `different_part` (a qty claim doesn't fix identity) |
| — | **else** | a row whose class's override doesn't fire → stamp `is_unavailable=True` (current behavior) | |

**Offer hook** (the only other `released_at` writer): **`released_at` is written only
by user-initiated proof** — a person entering, saving, or approving an offer. All five
sites go through ONE gate, `maybe_release_on_offer(db, requirement_id, vendor_name,
user)` (thin wrapper over `release_on_offer`; `'offer_received'`, all reasons **except
`different_part`** — same principle as O3: *availability evidence never releases
identity knowledge*): (1) canonical `create_offer` (`app/routers/crm/offers.py`),
fires only for ACTIVE offers with a requirement_id and also covers the sightings
convert/enter-offer route, which delegates to it; (2) manual `add_offer`
(`app/routers/htmx_views.py`); (3) the user-edited `save_parsed_offers` route
(`htmx_views.py`, persists ACTIVE); (4) `save_freeform_offers`
(`app/services/ai_offer_service.py`, ACTIVE after user review); (5) the
pending-review → approve transition (`approve_offer`, `crm/offers.py`).
**Excluded — never release:** auto-created offers (background inbox monitor
`_auto_create_offers_from_parse`; excess auto-matching `match_excess_demand` /
`create_proactive_matches_for_excess`) are auto-mined evidence — same class as the
demoted stock-list re-uploads — and the three clone paths (`crm/clone.py`,
`requisition_service.py`, `proactive_service.py`) copy old offers: clones are never
proof. `ai_offer_service.save_parsed_offers` persists PENDING_REVIEW and therefore
does not release until a user approves.

**Deliberately excluded triggers** (judge-backed rationale): price deltas (0% column
fill on brokerbin/NC — exactly where marks live — plus repricer-bot noise); date-code
deltas (0% fill everywhere on staging); raw_data freshness extras
(`age_in_days`/`uploaded_date` reset on unchanged re-uploads); same-class source_type
changes (syndication echo).

## 5. Reader-side coherence rule (authority rule)

**The record predicate is the only authority; `Sighting.is_unavailable` is a render
cache.** One helper `is_active(record, now)` used identically by all read surfaces:

1. **Row render:** stamped row + active record → suppressed (rose, reason); stamped
   row + non-active/no-longer-matching record → **advisory style** (hint + verify)
   *even though the flag is stale*; unstamped row + record (active or not) →
   advisory+nudge; no record → normal.
2. **Batch 4 pill** (`compute_vendor_statuses`): vendor is `unavailable` iff
   `(active record matches AND vendor has NO unstamped row)` OR `(no record at all AND
   all rows flagged — true legacy)`. Rows-win: one override-surfaced row flips the
   pill off "unavailable"; an expired record's stale stamped rows no longer pin the
   pill (no more RFQ-resumed-while-tab-says-unavailable incoherence).
3. **RFQ** (`excluded_vendor_norms` + send/preview re-checks): active records only —
   fetch the few matching records and filter with `is_active` in Python.
   Released/expired → RFQ resumes, and now the tab agrees.

Consequence: a worker stamping a row milliseconds after an offer-release, or
batch-ordering races across the 6 persistence paths, produce at most a stale flag that
every reader reinterprets correctly on next render. No reconciliation pass, no
read-path writes, self-healing by construction.

## 6. Schema delta

One Alembic migration (id ≤32 chars, single head, downgrade drops the columns), no
backfill. Policy columns on `vendor_part_unavailability`
(`app/models/vendor_part_unavailability.py`):

| column | type | semantics |
|---|---|---|
| `qty_at_mark` | Integer, nullable | **per-key** snapshot at mark/re-mark: max non-NULL `qty_available` over the vendor's sightings whose `normalize_mpn_key(mpn_matched)` equals THIS record's key (rows with empty `mpn_matched` count toward the primary-key record, mirroring `apply_to_fresh_sightings`' key fallback). Never max-across-all-keys, never a cross-key fallback. |
| `released_at` | UTCDateTime, nullable | written ONLY by O3 and the offer hook (both user-initiated paths) |
| `release_trigger` | String(32), nullable | `'vendor_email'` \| `'offer_received'` — renders the hint copy |

Integration note: the same migration also carries the `requirement_id` provenance
column required by the main spec's silent-failure hardening (IMPORTANT-3 there) — one
migration total, four columns. That column is provenance, not policy; its semantics
live in the main spec.

Legacy/pre-migration records have NULL `qty_at_mark` ⇒ O2 never fires for them ⇒ they
ride the windows — fail-closed, bounded.

## 7. Re-arm, clear, expiry

- **Re-arm is manual-only:** the existing upsert refreshes `created_at=now`,
  reason/note/created_by, NULLs `released_at`/`release_trigger`, and **re-snapshots
  `qty_at_mark` per key — keeping the old value when the new computation is NULL**
  (no cross-requirement clobber). The just-seen echo becomes the new baseline: one
  click buys a full quiet window.
- **Manual "Mark available" keeps DELETE semantics** (`clear_unavailability`): an
  explicit human "forget it"; history survives in ActivityLog. Auto-expiry and O1/O2
  never delete — surviving records power the advisory hint. (The delete *predicate*
  widens with `requirement_id` provenance per the main spec's silent-failure hardening;
  the delete semantics themselves are unchanged.)
- **Verify affordance** (advisory state) carries BOTH actions: "Still unavailable" →
  re-arm; "It's back" → clear (which also unblocks RFQ) — no dead-end nudge. During an
  active window, O1/O2-surfaced rows render the nudge but **RFQ stays excluded until
  expiry/release/clear** — one consistent answer: "you can email them when the window
  ends, an offer/email proves stock, or you clear the mark."

## 8. Config knobs

`app/config.py`, Pydantic Settings, `vendor_protection_*` precedent — validated, no
0-sentinel anywhere:

```python
# Vendor-part unavailability: read-time suppression windows (changing them re-evaluates
# existing marks at next render). different_part never expires by design (identity, not stock).
unavailability_suppress_days: int = Field(30, ge=1)          # lot reasons: bought_by_us/sold_elsewhere/broken/other
unavailability_listing_suppress_days: int = Field(180, ge=1) # not_really_there
unavailability_qty_jump_factor: float = Field(2.0, ge=1.0)   # fresh qty must be >= factor x qty_at_mark AND strictly greater
```

`ge=` bounds remove knob footguns; permanence-as-constant removes the "0 = forever"
inversion; **strict-greater is structurally required even at factor 1.0**, so an
identical echo can never release regardless of misconfiguration.
`LOT_REASONS`/`LIVE_SOURCES`/`HUMAN_DIRECT_SOURCES` are `Final` constants in the
service (the `MPN_COOLDOWN_HOURS` pattern).

## 9. Scenario walkthrough

| # | scenario | verdict | mechanism |
|---|---|---|---|
| S1 | phantom 6mo echo | **PASS to day 180, then ACCEPTED conversion** | identical echoes match no override (strict-greater + 2.0×; auto-doc demoted) → stamped 180d. At ~day 180 the next echo renders one *labeled* advisory; one click re-arms 180d. Memo rationale: a 6-month-old single human assertion shouldn't silently outvote the market forever; the advisory costs ~zero by construction while silent suppression errors never self-correct. Operator can raise the knob. |
| S2 | restock 3wk, 4× qty | **PASS** | 4× ≥ 2.0× → O2 surfaces with nudge + "sold elsewhere 3wk ago". NULL-qty mark: waits ≤9 remaining days of the 30d window — accepted bounded miss. |
| S3 | bought lot | **PASS** | day-1 identical cache echo: qty == snapshot → stamped. Two months later: 30d window long expired → advisory + hint (O2 would also fire). |
| S4 | mislabeled forever | **PASS** | `different_part` never expires; O2/O3/offer disabled; only LIVE catalog evidence (identity proof), a vendor-side MPN fix (key no longer matches), or manual clear ends it. |
| S5 | DigiKey contradiction | **PASS** | DigiKey is its own vendor_norm — never suppressed (trivial). If the *marked* vendor surfaces authorized with a different qty → O1 instant. |
| S6 | broken + new lot at 60d | **PASS** | 30d lot window expired → advisory + "Broken — 2mo ago" hint; no date-code machinery needed. |
| S7 | vendor emails "500 pcs" | **PASS** | buyer routes it through the email import → `email_attachment` = HUMAN_DIRECT, qty>0 → O3 record release, RFQ resumes immediately, advisory + nudge. The *auto-mined* weekly copy of the same list does NOT release (listing-class) — both correct simultaneously. |
| S8 | quiet expiry | **PASS** | stateless read-time predicate; no cron, no lazy writes, nothing rendered until a row exists; stale stamped rows re-render as advisory via the reader rule, and RFQ + pill + row now agree. |

## 10. Accepted limitations (with bounds)

1. **Condition/variant key collapse** — `normalize_mpn_key` ignores condition; NEW +
   REFURB share one record. Record granularity is the already-built model; escape =
   LIVE evidence or one manual clear. The marking-modal copy MUST say "applies to all
   of this vendor's listings of this MPN". Candidate v2 = condition-aware snapshot.
2. **Knob retroactivity** — window changes re-evaluate existing marks at read time.
   Inherent to stateless predicates, matches the `sighting_stale_days` precedent,
   single-user staging risk profile, documented in the knob comment.
3. **Cross-source qty disagreement** can fire O2 spuriously — bounded by 2.0× and by
   landing in the advisory state, which the cost-asymmetry memo prices at ~zero.
4. **NULL-snapshot live mark** (e.g. Mouser, 11% qty fill) makes O1 fire on the next
   authorized echo — marking authorized distributors is rare and their data lags
   hours, not weeks.
5. **S1 day-180 conversion** (see scenario table) — the labeled advisory at window end
   is the deliberate trade against silent forever-suppression.
