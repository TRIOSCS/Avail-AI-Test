# PACKET 3 — Wave 3 (one implementation per behavior) review

One message, everything Wave 3 changed, and every call you can reverse.
Wave 3's job (spec §10): every behavior that had drifted into multiple
implementations now has exactly ONE — one offer lifecycle, one
requirement pipeline, one quote builder, one RFQ composer, one lock
matrix, one notification path, one buy-plan transition function, one
requisition status story. Every decision below was executed as
recommended; one word from you flips it.

## 1. The numbers — baseline → now

| Metric | Baseline (2026-08-04) | Current |
|---|---|---|
| Status values, all entities | 114 | **92** stored (+ the new 4-word outcome vocab, §5.3) |
| — Resell family | 34 | 27 stored + outcome (sold/scrapped/withdrawn/no_bids) |
| — Buy plan | 7 | 6 (INBOUND retired; erratum: spec said 7→5 but EXPIRED was an approval-request status W1.9 already removed; HALTED stays — live halt/resume) |
| — Requisition | 9 stored | **6 stored** — rfqs_sent/offers/quoted are now derived from data, not stored |
| Offer-entry doors | 5 | 2 (Responses tab + Add-offer modal w/ paste box) |
| QP approval ceremonies | 5 | 3 (the two self-stamped Mark-Reviewed gates dropped) |
| Notification deliveries per approval event | up to 4 paths | exactly 1 (approval outbox email) |
| Scheduler jobs / nav tabs / routes | unchanged from Packet 2 | unchanged (Wave 4 owns the merges) |

Migrations this wave: 206 (requirement normalization), 207 (resell
ladder + outcome), 208 (buy-plan INBOUND), 209 (requisition collapse).
All four round-tripped on throwaway Postgres; the deployed instance
sits at head 209 on a fresh prod copy (cutover rehearsal #4 + upgrade).

## 2. Resell status remap — 34 → 5 + outcome (spec §5.3)

The LIST ladder is now DRAFT → POSTED → BIDDING → AWARDED → CLOSED,
with a required outcome recorded at close (sold / scrapped / withdrawn /
no_bids). Migration 207 remapped the live rows (15 lists):
draft→draft, open→posted, collecting→bidding, bid_out→bidding (the
bid-back's sent/accepted state lives on the customer bid itself),
expired→closed. Line items lost the writer-less "bidding"; outreach
lost the writer-less "no_response". Closed rows got their outcome
backfilled from data (a won offer/accepted bid → sold; bids but no
award → withdrawn; zero offers → no_bids); "scrapped" is manual-only.
The award → outcome tail is in the nightly kernel walk and pinned by
tests (test_resell_close_outcome.py, 10 green).

## 3. Buy plan — ONE transition() + no auto-approve (spec §5.2/§9)

All buy-plan status movement now goes through one transition function
(buyplan_state.py) — the scattered raw status writes are gone, and the
**auto-approve branch is deleted: every deal routes through the manager
approval step**, confirmed by test (test_buyplan_state.py) and by the
kernel walk's approval leg. INBOUND retired (migration 208, 0 rows).
The old pre-engine PENDING fallback is still in place for 4 legacy
plans — decision D2 below.

## 4. Requisition status is now DERIVED (spec §5.1) — landed today

The 9-state stored ladder is gone. What's stored is only what you
decide: draft / open / hotlist / won / lost (+ legacy cancelled).
The pipeline stages you SEE are computed from what actually happened:

- **Quoted** — the deal has a quote (combined quotes count).
- **Offers** — at least one offer row exists.
- **RFQs Sent** — at least one RFQ email actually went out (failed
  sends don't count).
- Won/Lost still require the close reason; Hot List still feeds the
  (parked) Proactive matcher; nothing about approval gates changed.

Why this is better than what it replaced: the stored ladder lied.
Nothing ever auto-set "RFQs Sent" (only the manual dropdown could),
quote-send force-advanced every attached deal, and deleting a deal's
only offer left it claiming "Offers" forever. Now the badge always
matches the data, everywhere it renders (Sales Hub, detail header, CSV
export, JSON API, company/vendor tabs, forecast chip, PDF summary).
Migration 209 remapped 11 rows (4 offers, 7 quoted → open) and the
database CHECK now enforces the 6 stored values. The status dropdown
offers draft/open/hotlist; Won/Lost stay on the row actions that ask
for a reason.

Side effects worth knowing (all flip-able, D3 below): the parts list
and company open-deal counters now INCLUDE mid-pipeline deals they
used to hide once a deal advanced — arguably fixes, but they change
numbers you may be used to; the quote-send API's "status_changed"
field is now always false (sending can't change deal status anymore).

## 5. Canonical semantics — confirmed by test, per spec §10

- **Reconfirm TTL** (offer freshness window) — one implementation in
  offer_service, pinned green (test_offer_service.py).
- **UI dup detection** (requirement create) — one pipeline with
  dedicated dup-detection units, pinned green (test_requirement_service.py).
- **Every-deal approval** (no auto-approve) — pinned green
  (test_buyplan_state.py) and walked on the deployed instance.

## 6. Acceptance — the wave-close ritual (brief §7)

- Full suite: **19,102 passed / 0 failed** (27 keys-off skips).
- pre-commit all-files green (converged on the second run).
- Deployed to the parallel instance (BOTH images rebuilt), head 209,
  health 200.
- Kernel walk on the DEPLOYED build: **18 passed / 2 honest keys-off
  skips** — the same two PO-verify/prepayment skips as Packet 1 noted
  (they light up when the seeded admin gets the PO-approver toggle —
  that's a Packet 1 decision item, not a Wave 3 gap).

## 7. Decisions — flip-able, with recommendations

**D1 — Notification single-path flags (W3.8).** Every approval event
now delivers exactly once, via the approval outbox email. Consequences
you own:
  (a) "Submitted" notices go to the request's eligible approvers, not
      everyone with a manager role. *Recommended: keep.*
  (b) Turning OFF a user's buy-plan email preference now suppresses
      the event entirely for them (email is the only path left).
      *Recommended: keep — that's what the toggle says it does.*
  (c) The prepayment Teams-webhook setting is registered but no longer
      read. *Recommended: delete the setting in Wave 4.*
  (d) notify_rejected stays a no-op seam until D2 resolves.

**D2 — The 4 legacy PENDING buy plans.** The last pre-engine fallback
code path exists only for them. Options: **backfill** (one data
migration stamps them into the engine's PENDING and the fallback dies
now) or **resubmit** (you re-submit those 4 from the UI at your leisure
and the fallback dies at cutover). *Recommended: backfill — it's four
rows, and the fallback is the last duplicate implementation standing.*

**D3 — Requisition-collapse side effects (§4 above).** Parts list +
company counters now count mid-pipeline deals; quote-send API
status_changed always false; the two parked/stock jobs match on all
open deals instead of (open, offers). *Recommended: keep all — each is
the honest reading of "open pipeline".*

**D4 — Status filter pills.** The Sales Hub filter pills stay
All / Open / Hot List / Won ("Open" now means the whole working
pipeline). If you want stage-level filters (RFQs Sent / Offers /
Quoted as filterable buckets), that's a small Wave 4 add — say the
word. *Recommended: leave as is; the badges already show the stage.*

— End of Packet 3. Reply with flip words (any, in one message) or
"all stands." Wave 4 (merges + splits) is next: sightings split, Deals
merge, Approvals tab merge + QP absorption, htmx_app.js split,
search_service decomposition.
