# STATE.md — AVAIL Simplification progress

Single source of truth (Brief §3.1). Every session starts by reading
this file and ends by updating it. Decisions recorded here are not
re-litigated. Companion docs: SIMPLIFICATION_BRIEF.md (execution
discipline, wins on conflict) and SIMPLIFICATION_SPEC.md (work order).

## Session 1 setup — COMPLETE

- [x] S1.1 Create simplification branch (worktree /root/availai-worktrees/simplification, base bcfb9a54)
- [x] S1.2 Commit brief v2 + spec + this file to docs/
- [x] S1.3 Baseline metrics recorded (table below)
- [x] S1.4 Parallel instance up: prod-DB copy restored, boots clean, reachable at https://app.availai.net:8443 (project availai-simp; db localhost:5433; restore ritual = scripts/simp-refresh-db.sh)
- [x] S1.5 docs/CUTOVER.md v0 written
- [x] S1.6 Nightly E2E at parallel instance (cron 04:00 UTC → scripts/simp-nightly.sh: deployed-instance smoke + branch Playwright suite; upgraded to the kernel walk in W1.11)
- [x] S1.7 Wave 1 decomposed into checklist below

## Current wave: Wave 1 — quiet + honest (spec §10)

Acceptance: scheduler runs ONLY kernel jobs (§3); zero recurring
warnings in a 48h simp log window; kernel E2E green. Then Packet 1.

- [x] W1.0 Job disposition table: 59 jobs → keep 7 / park 12 / delete 40, code+DB evidence, 10/10 spot-checks confirmed → docs/W1_JOB_DISPOSITION.md (§3/§6, reconciled to v1.1); 4 flip-ables queued for Packet 1
- [x] W1.1 Delete Explorium discovery machine jobs (6 monthly) (§6)
- [x] W1.2 Park 8x8 CDR poll job — off the scheduler, existing flag, Data Capture Initiative comeback (§6 v1.1)
- [x] W1.3 Tagging suite: delete 2 zero-yield jobs; prefix/spec jobs → on-demand management commands; AI tagging on-demand only (§6)
- [x] W1.4 Park remaining non-kernel jobs per disposition table; scheduler == kernel list exactly (§3, §11)
- [x] W1.5 Keys-off honesty: BOM "Parse with AI" honest state, no 500 (§7)
- [x] W1.6 Keys-off honesty: search "external sources off" notice + AI-intent plain-search fallback (§7)
- [x] W1.7 Keys-off honesty: CRM Enrich free SAM.gov path (fix 503 guard) + paid providers labeled off (§7)
- [x] W1.8 Badge consolidation: 6 pollers → one badge endpoint (§5.5)
- [x] W1.9 Dead statuses removed — 10 members, three-way evidence each; kept-with-reasons list for Wave 3 (§5.3/§9)
- [x] W1.10 Deleted BUY_PLAN_TRANSITIONS + REQUISITION_TRANSITIONS; live tables untouched (§9)
- [x] W1.11 Kernel-walk E2E script against the deployed instance (18 passed / 2 honest skips live); simp-nightly.sh runs it (§3/§6)
- [x] W1.11b Nightly failure pages ONE admin — RED verdict invokes the existing notify_nightly_status seam with a SIMPLIFICATION marker (§6)
- [x] W1.17 Resell outreach manual-channel log no longer requires an M365 token (in-branch acquisition for email only) (§7 family)
- [x] W1.18 Quote Won/Lost buttons render for draft quotes (draft→won/lost verified legal in QUOTE_TRANSITIONS) (§7 family)
- [x] W1.12 Backup verify-timer unit files ready in repo; installation deferred to cutover (§6; CUTOVER.md §5)
- [x] W1.13 Task statuses → open/done; migration 205 round-tripped on throwaway PG, applied to simp copy at deploy (§5.4, v1.1 §0.6)
- [x] W1.14 Trouble-ticket AI calls gate behind AI flag, regex fallback per brief §4.5 (§5.5, v1.1 §0.6)
- [x] W1.15 token_refresh skips-with-notice when Azure creds unset (48h-gate killer found by pre-flight: ~1,150 warnings/48h keys-off; docs/evidence/w1-quiet-preflight.md) (§7 spirit, §11)
- [x] W1.16 worker_liveness_check skips workers whose enabling creds are unset (48h-gate killer; DB-refresh re-imports is_running=true each wave, so the guard must be code-level) (§7 spirit, §11)
- [ ] W1.A Acceptance checks pass → assemble + deliver Packet 1 (brief §6; include spec v1.1 §12 final-read checkboxes)

## Wave 2 — the delete/park sweep (spec §10; ADOPTED under owner's continuous-autonomy directive 2026-08-04)

Owner directive: "work thru all waves autonomously" — packets delivered as
reversible reports, no waiting; ONLY glossary application, screen-diet
application, and cutover stay on the owner's word (brief §4.1/§4.2/§5).
Full 38-item decomposition: docs/evidence/w2-checklist-draft.md. Build on
branch now; first W2 deploy AFTER the W1 quiet window closes (~Tue 23:00 UTC)
with the deferred wave-start DB refresh + migration rerun at that moment.

- [x] W2.1 Nav 10→5 + Settings gear outside the bar (§4; Search/Sightings nav-only folds, W4 does the merges)
- [x] W2.2 Proactive parks whole — workspace + engine + badge, one unit, existing flag (§4/§5.4/§8)
- [x] W2.3 Trader lane + buyer intelligence parked behind existing flags (§5.3; if no flag exists: smallest mechanism = registration/nav removal, deviation-logged, NO new framework)
- [x] W2.4 Prospecting → CRM lens (§5.4)
- [x] W2.5 Materials → contextual lookup from Deals/CRM (§4)
- [x] W2.6 Orphan /api delete batches B0–B13 per docs/evidence/w2-delete-manifest.md (107 routes, with the manifest's two corrections: error-report alias decorators only; e2e/api.spec.ts re-pointed not deleted) (§8)
- [x] W2.7 Coverage farm deleted + diff-coverage gate lands same PR (§9 Decision K)
- [x] W2.8 Surface deletes: Sourcing Leads, Dashboard/Knowledge, Email-Intelligence dashboard, contact-intelligence, standalone cross-company contact pages (§5.4/§8)
- [x] W2.9 Backfill graveyard + startup.py backfills → alembic; write-only Notification table + writers deleted (§8/§5.5)
- [x] W2.10 Legacy JSON create endpoint (POST /api/requisitions) deleted (§5.1, spec-named)
- [x] W2.11 QP serial/FRU relink from workspace QP pane (§5.2 Decision E)
- [x] W2.12 Resell→Sighting mirror dual-write stopped (§5.3; drafted W2, flip to W3 noted)
- [x] W2.13 Glossary old→new table + screen-diet cut list FINALIZED for Packet 2 (application SIGN-OFF-GATED)
- [x] W2.A Acceptance: exactly 5 tabs + gear; deleted surfaces 404; fresh-DB drift-gate boot green; kernel E2E green → Packet 2

## Wave 3 — one implementation per behavior (spec §10; build started 2026-08-05)

Prior session built W3.1/2/4/5/9/10 but ended without commit or close-out;
this session ran a 12-agent audit of the uncommitted tree, fixed the found
gaps, and committed. Acceptance: behavior parity on the kernel walk; the
named canonical semantics (reconfirm TTL, UI dup detection, every-deal
approval) verified by test.

- [x] W3.1 offer_service — ONE offer lifecycle (create/update/reconfirm-TTL/
  approve/reject/mark-sold/delete + system email auto-create); all five
  drifted doors delegate or died. Two clone-only constructors remain by
  design (parked Proactive convert-to-win; requisition revision reference-
  copy) — neither is an interactive door.
- [x] W3.2 Requirement pipeline — ONE creation pipeline
  (requirement_service) + migration 206 (condition case-fold,
  normalized_mpn key form, packaging vocab clamp); PUT edit path aligned
  NULL-on-unmapped; description_service key-form lookup fix; seed drift
  fixed; dedicated unit tests incl. dup detection.
- [ ] W3.3 Derived requisition status (replaces the stored 9-state ladder)
- [x] W3.4 Quote builder — one implementation (combined.html; modal +
  macros deleted)
- [x] W3.5 RFQ composer deleted (offers/rfq.py + compose/results templates;
  vendor-modal composer is the survivor; deletion pinned by
  test_inventory_cleanup)
- [x] W3.6 Offer doors 5→2: both AI-paste modals + the standalone
  review-queue page deleted (8 URLs pinned gone); the spec-named
  replacements reuse existing paths (paste box → existing parse/save,
  flagged-AI filter → existing review route). Kept deliberately:
  parsed_offer_results.html (the paste box's preview), crm JSON
  approve/reject (other surfaces reference them), /api/ai/parse-email
  (JSON API, not a modal — ai_email_parser now single-caller, orphan-sweep
  candidate for W4).
- [x] W3.7 QP single lock matrix: QP_SECTION_LOCK_MATRIX in
  qp_workspace.py, consulted by BOTH the workspace panes and the standalone
  page; Mark-Reviewed ceremony + can_review_qp_* checks deleted
  (5 ceremonies → 3); reviewed_* stamps + user grant columns stay
  vestigial (auto_approved precedent, no DDL).
- [x] W3.8 Notification single-path: every approval-lifecycle event now
  delivers exactly ONCE via the existing approval outbox email; Teams
  channel/DM + in-app duplicates + direct-email paths deleted; prepayment
  emails rerouted through the outbox (payload to/subject/html — seam
  extension, no new system, no migration). OWNER FLAGS (Packet 3):
  (a) submitted-event recipients = the request's eligible approvers, not
  role-managers; (b) notify_buyplan_email_enabled=False now suppresses the
  event entirely (email is the only path); (c) prepayment_teams_webhook
  setting registered but no longer read; (d) notify_rejected kept as no-op
  seam until the legacy-PENDING fallback dies.
- [x] W3.9 Single transition() (buyplan_state.py) + auto-approve branch
  deleted + migration 208 (INBOUND retired; BuyPlanStatus = 6). OPEN
  SLICE: legacy pre-engine PENDING fallback in htmx/buy_plans.py stays
  until the owner picks backfill-vs-resubmit (4 PENDING plans on the prod
  copy) — Packet 3 decision queue.
- [x] W3.10 Resell status collapse 34→5 + outcome + migration 207 + bid
  paths (2 UI doors + solicited inbound). Outcome forward writer built
  same session: close_awarded_list (AWARDED→CLOSED, owner picks
  sold/scrapped/withdrawn) + close_list_without_bid stamps no_bids +
  detail-pane form + POST /api/resell/{id}/close-awarded + kernel-walk
  outcome step (tests: test_resell_close_outcome.py, 10 green).
- [ ] W3.A Acceptance: semantics tests green (TTL ✅ / dup ✅ / every-deal
  ✅ already), kernel walk parity on the deployed W3 build → Packet 3.

## Baseline metrics (Rule 3.6 — recorded before any Wave 1 work)

All numbers independently re-derived by a second pass before recording.
Route count = runtime `app.routes` (flattened), not decorator grep.

| Metric | Baseline (2026-08-04 @ bcfb9a54) | Current |
|---|---|---|
| Route count | 809 (268 /api, 516 /v2, 25 other; 753 unique paths) | ≈804 (6 badge endpoints → 1; runtime re-count at Packet 1) |
| Scheduled job count (in-app) | 59 (all in app/jobs/*.py, 17 modules) | **7 kernel** (6 live keys-off — token_refresh registration-gated; 11 modules) |
| Host cron jobs (prod droplet) | 5 (nightly tests, weekly cleanup, legacy pg backup, FRU check, coverage report) | 5 |
| LOC app/routers/sightings.py | 3,812 | 3,812 |
| LOC app/static/htmx_app.js | 3,654 | 3,654 |
| LOC app/search_service.py | 3,604 | 3,604 |
| Status values (all entities) | 114 across 21 enums — resell subset = 34 (ExcessList 9, LineItem 4, Offer 5, OfferLineMatch 3, CustomerBid 4, Outreach 9); BuyPlan 7; Requisition 9; Task 3 | 103 (−10 dead, Task 3→2) |
| Test file count (pytest) | 1,113 | 1,107 |
| E2E spec files (Playwright) | 12 | 12 |
| Nav tab count | 10 + Settings (template: app/templates/htmx/partials/shared/mobile_nav.html) | **5 + gear** |

## Deviation log (Rule 3.4 — one line each, surfaced in the wave packet)

- 2026-08-04 S1 (RESOLVED same day): Session started on spec v1.0 (only version on disk); owner supplied v1.1 mid-session. docs/SIMPLIFICATION_SPEC.md replaced with v1.1 verbatim; deltas folded in: 8x8 poll delete→park (Data Capture Initiative), Proactive parks whole, task statuses + trouble-ticket gating added to Wave 1 (W1.13/W1.14), Settings gear outside a literal 5-tab bar, resell 34→5 named statuses with Wave-3 remap. Disposition table reconciled (keep 7 / park 12 / delete 40).
- 2026-08-04 S1: Login visual-regression baseline was stale on main (snapshot from the original frontend-testing commit; login.html restyled twice since — password form + light theme are deliberate). Re-baselined on this branch after image inspection; note: PRODUCTION's own nightly is red on this same test and stays red until main gets the same re-baseline (owner's call — outside this branch's scope).
- 2026-08-04 S1: Nightly E2E v0 = deployed-instance smoke + branch Playwright suite (with Vite build step — missing dist/ renders every page unstyled). The deployed-app kernel-walk script replaces the smoke in W1.11 per spec §10.
- 2026-08-04 S1: Host-level cron jobs (backup_postgres.sh 6-hourly, daily_coverage_report.sh, check_fru_matrix_refresh.sh, weekly_cleanup.sh, nightly_tests.sh) operate on PRODUCTION; spec §6 removals of host-side duplicates are deferred to cutover and recorded in CUTOVER.md instead of being executed during Wave 1. In-app scheduler shrink proceeds normally on the branch.

- 2026-08-04 W2-prep: measured orphan inventory re-baselines two spec §8 estimates — orphaned /api routes = 107 firm (+35 keep-ambiguous; spec said "111+"), pinned test files = 80 (spec said "~280"). Full route-by-route list in docs/evidence/w2-api-orphans.md; POST /api/requisitions (the legacy JSON create) measured keep-ambiguous but stays on the W2 delete list because the spec names it explicitly.
- 2026-08-04 W1: simp-DB worker singletons (ics/nc/tbf_worker_status.is_running) flipped false on the copy so the watchdog stops firing pre-W1.16; the durable fix is W1.16 since every wave-start refresh re-imports prod's true.
- 2026-08-04 W1: rule 3.5 relaxed for the shrink batch — W1.1–W1.16 landed as 14 module-scoped commits (not one per checklist item): parallel agent execution interleaved items inside shared files; each commit message enumerates its items, so packet assembly and surgical rollback hold at module granularity.
- 2026-08-04 W1: suite gate — 23,740 passed / 19 failed on first full run; all 19 root-caused and fixed same-session (14 = W1.15 guard needed Azure-configured test fixtures; 3 = tests pinned the pre-fix CRM-enrich 503; 2 = tests pinned the six-poller nav markup). Zero failures on the affected files after fix; confirmation full-suite run recorded in the packet.

- 2026-08-04 W1: 48h quiet-log window restarted at 22:55 UTC with the FINAL Wave-1 build (fa00466f deployed; migration 205 applied to the copy = cutover rehearsal #2). All W1 build items complete; only W1.A (Packet 1, after the window ~Wed 22:55 UTC) remains. Owner may shorten the window to 24h ("24h window") — pre-flight evidence supports it; 48h recommended.

- 2026-08-04 W2: wave-start DB refresh deferred to W2's first deploy (post-window, ~Tue) — refreshing mid-window would disturb the W1 quiet evidence and the owner's live test data; build proceeds on branch (rule 3.7 deviation).
- 2026-08-04: OWNER DIRECTIVE — continuous autonomous execution through Wave 4; packets delivered as reversible reports without waiting; glossary/screen-diet application + cutover remain owner-gated.

- 2026-08-05 W2 SHIPPED+DEPLOYED @31457826 (~01:00 UTC): refresh rehearsal #3 (fresh prod copy → head 205), 5 tabs + gear live-verified, deleted surfaces 404, kept pages routable, scheduler 6, kernel walk 18/2 (one E2E race hardened — Edit-before-Alpine-mount, same class as openModal; app itself fine). Gate: 19,093 passed / 2 root-caused stale toolbar pins ("All contacts" parked §5.4).

- 2026-08-05 OWNER DIRECTIVE (mid-W3): "avoid drift — stick to simplifying
  what I built, not building anything new." Bar: every change traces to a
  named spec line; additions only where the spec itself names the replacement
  (outcome-on-close §5.3, Add-offer paste box + flagged-AI filter §5.1);
  net-new ideas go to the Backlog section, never built.
- 2026-08-05 W3 session 2: prior session ended mid-W3 with the whole build
  uncommitted and no STATE section — recovered via 12-agent tree audit; gaps
  fixed same-session; committed module-scoped per the W1 rule-3.5 relaxation.
- 2026-08-05: Aug-5 simp nightly RED root-caused 3-way: (a) branch-suite fail
  = W2 leftover (api.spec.ts merge probe missed by the B0 re-point; now
  asserts the deletion), (b) kernel-walk fail = W3 spec vs W2-deployed
  instance skew (expected mid-wave; clears at the W3 deploy), (c) W1.11b
  pager path NEVER worked from the host (compose hostname "db" unresolvable)
  — simp-nightly.sh now execs notify_nightly_status inside the PROD app
  container exactly like nightly_tests.sh; the missed RED alert was
  hand-delivered 16:37Z (3 admins, in-app + Teams).
- 2026-08-05: availai-simp-enrichment-worker crash-loop (918 restarts since
  the W2 deploy): its image predated migration 205 while the DB was at head —
  the W2 deploy rebuilt only the app image (known stale-container class).
  Fixed by retagging the app image for the worker + recreate (clean boot,
  alembic no-op). RULE for every simp deploy: rebuild BOTH images.
- 2026-08-05 W3: migration 206 EXTENDED with a packaging pass + the pipeline
  clamps packaging to the 5-value chk_req_packaging vocabulary —
  normalize_packaging's wide map also emits sightings/offers-only values
  ('bag','box','each'), which would 500 on any fresh-built DB (real bug, not
  test drift); live copy carries 'tape & reel'→'reel' and 'yes'→NULL.
  Re-round-tripped 205↔head on a throwaway PG 16; frozen map verified
  case-by-case against the app-side clamp.
- 2026-08-05 W3: ORM gotcha — Column(default=1) fires even for an
  EXPLICITLY-set None, so the search-picker "no quantity → NULL" design
  silently stored 1; the pipeline re-asserts explicit NULLs post-flush
  (defaults are INSERT-only), pinned by test.
- 2026-08-05 W3: 5 stale test pins fixed (display-form normalized_mpn ×2,
  display-form packaging ×2, deleted create-route 200 ×1) +
  seed_test_data.py display-form/`condition="New"` drift; suite green after.

### Packet 1 decision queue (owner, one sitting)
- 4 disposition flip-ables (bid_due_alerts / auto_attribute / auto_dedup deletes; inbox_scan mining sub-ops now flag-gated off)
- Seeded-admin `can_approve_purchase_orders` toggle: ON lets the nightly kernel walk exercise the PO-verify + prepayment gates end-to-end (currently 2 honest skips)
- Spec v1.1 §12 final-read checkboxes (9 items)

## Backlog (Rule 2.1 stops — net-new items, never built during simplification)

- (none yet)
