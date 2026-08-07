# PACKET 4 — Wave 4 (merges + splits) review — THE FINAL PACKET

One message, everything Wave 4 changed, the final numbers, and the
launch-deal ask. All four waves are now COMPLETE. Every decision below
was executed as recommended; one word flips any of them.

## 1. The final numbers — baseline → launch candidate

| Metric | Baseline (2026-08-04) | Now |
|---|---|---|
| Routes (runtime, flattened) | 809 (753 unique) | **643** (167 /api, 451 /v2; 596 unique) — −166 |
| Scheduled jobs (in-app) | 59 | **7 kernel** |
| Status values, all entities | 114 | **92** stored (+4-word outcome vocab) |
| Nav tabs | 10 + Settings | **5 + gear** |
| Approvals workspace tabs | 4 | **3** (Deals / Purchase Orders / Prepayments) |
| Largest router file | sightings.py 3,812 | **796** (guard: none may exceed 800, no allowlist) |
| htmx_app.js | 3,654 lines, one file | **38-line entry + 13 modules** (largest 485) |
| search_service.py | 3,604 lines, one file | **10-module package** (largest 689) |
| Scoring implementations | 4 generations disagreeing on screen | **1** (persisted v2; screen == stored, pinned by test) |
| Offer-entry doors / QP ceremonies / notification paths | 5 / 5 / up to 4 | **2 / 3 / exactly 1** (Wave 3) |
| Kernel walk (nightly, deployed instance) | — | **18 passed / 2 honest keys-off skips, 20 steps** |

## 2. What Wave 4 did (spec §5.1/§5.2/§9/§10)

- **The Deals merge.** The requisition detail page is the ONE deal
  editor, with a Sales/Sourcing lens toggle (persisted; deep links
  auto-flip). Each deal line now carries the sourcing drawer (the
  sightings pane inline: vendor coverage, RFQ compose, offer intake,
  refresh), the part-dossier door (Search's market view for that MPN —
  the "one door"), and its sales notes. The split-panel parts
  workspace became read-only triage (status, best price, Open deal) —
  its 7 duplicate tab endpoints and 17 write endpoints died. The
  sourcing board's door lives in the Deals view toggle. Sightings and
  Search are fully folded — 5 tabs, nothing orphaned.
- **The Approvals merge.** Sales Orders + Buy Plans were already one
  code path wearing two tabs — now it's one Deals tab (old URLs
  redirect). The standalone QP page keeps serial/FRU (its only home,
  per Decision E) but sales/purchasing sections are edited ONLY in the
  workspace panes under the one lock matrix. The parallel JSON
  approval API and per-tab CSV export are gone.
- **The splits.** sightings.py, htmx_app.js, search_service.py — the
  three named regression factories — plus the ten other routers over
  the bar (resell 2,830 → 8 files, contacts, buy_plans, requisitions,
  materials, vendors, companies/core, approvals_hub, settings,
  archive). All pure structural moves: URLs identical, route counts
  runtime-verified, every former name re-exported. The 800-line guard
  is a test with no allowlist.
- **Scoring is one truth.** v1/v3/v4 deleted; every screen reads the
  persisted v2 score; the streaming path computes v2 so what you see
  during a search equals what gets stored (pinned by test). Bonus: the
  streamed vendor chips were rendering 0%/red under v1 — fixed.
- **hx-disinherit.** One attribute on the page container kills the
  recurring page-wipe class (652 elements audited, zero relied on
  inheritance) and repaired the live add-manufacturer page-wipe.

## 3. W4.7 — Postgres test engine: SHIPPED, and it caught a real one

The shared test engine now runs on Postgres when PG_TEST_DSN is set
(per-xdist-worker throwaway databases, same per-test isolation
semantics); SQLite stays the explicit local fallback, byte-for-byte
unchanged. First full PG run: 10 failures across 6 files — all
mechanical, all fixed same-session. **PG-backed suite: 18,933 passed /
0 failed. SQLite default: 18,914 / 0.**

The migration surfaced exactly the masked class it exists for, and one
finding is a real product observation (P4-D4 below): production's
fuzzy vendor-dedup runs on pg_trgm, and at the default threshold 85 it
genuinely misses near-identical pairs like "arrow electronics" /
"arrow electronic" (trgm scores 84.2 where the SQLite test path's
rapidfuzz scores 97). The SQLite suite had been asserting behavior
production doesn't have.

## 4. Decisions — flip-able, with recommendations

**P4-D1 — Approval reassign/delegate machinery.** The JSON API cut
removed the only exposure of approval reassignment. It had zero UI
callers, and under any-of routing (every eligible approver can act,
first responder wins) reassignment is redundant. The delegate columns
stay as vestigial data-compat. *Recommended: leave as-is; delete the
columns at a future cleanup.*

**P4-D2 — Redundant page-wipe guards.** ~6 anti-main hx-target guards
and 24 landmine comments are now redundant under hx-disinherit.
Harmless. *Recommended: leave; drain opportunistically.*

**P4-D4 — Vendor fuzzy-dedup threshold on Postgres.** The pg_trgm
scale runs lower than rapidfuzz's: at the shared cutoff 85, production
misses near-identical vendor-name pairs the tests used to assert.
Options: lower the pg_trgm threshold (~0.80), or leave it strict.
*Recommended: lower to 0.80 for the trgm path only — a one-line,
spec-consistent honesty fix — but it touches matching behavior, so it
waits for your word.*

**P4-D3 — Packets 1 and 3 flip words.** Still open from their
deliveries: Packet 1 (4 job flip-ables + the PO-approver toggle that
turns the walk's 2 honest skips into real passes + spec §12 final-read
checkboxes) and Packet 3 (D1 notification flags, D2 the 4 legacy
PENDING plans — recommended backfill, D3 collapse side effects, D4
stage filter pills). One sitting covers everything.

## 5. Launch-deal scheduling (brief §5)

Wave 4 has landed green: the §3 kernel walk passes on the deployed
parallel instance nightly and after every deploy this wave (18/2 across
five consecutive deploys). Per the brief, **the go signal is yours**:
one salesperson, one buyer, and you as approver run ONE real deal end
to end on https://app.availai.net:8443 — the walk with real money.
Cutover (CUTOVER.md, rehearsed 5× via the wave-start DB refreshes)
executes only on your explicit word after that deal completes clean.

Suggested sitting: (1) reply flip words or "all stands" for Packets
1/3/4; (2) flip the PO-approver toggle so tonight's walk runs 20/20;
(3) pick the launch-deal day.

— End of Packet 4, and of the four packets. The instance is ready when
you are.
