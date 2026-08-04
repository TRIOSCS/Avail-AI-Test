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

- [ ] W2.1 Nav 10→5 + Settings gear outside the bar (§4; Search/Sightings nav-only folds, W4 does the merges)
- [ ] W2.2 Proactive parks whole — workspace + engine + badge, one unit, existing flag (§4/§5.4/§8)
- [ ] W2.3 Trader lane + buyer intelligence parked behind existing flags (§5.3; if no flag exists: smallest mechanism = registration/nav removal, deviation-logged, NO new framework)
- [ ] W2.4 Prospecting → CRM lens (§5.4)
- [ ] W2.5 Materials → contextual lookup from Deals/CRM (§4)
- [ ] W2.6 Orphan /api delete batches B0–B13 per docs/evidence/w2-delete-manifest.md (107 routes, with the manifest's two corrections: error-report alias decorators only; e2e/api.spec.ts re-pointed not deleted) (§8)
- [ ] W2.7 Coverage farm deleted + diff-coverage gate lands same PR (§9 Decision K)
- [ ] W2.8 Surface deletes: Sourcing Leads, Dashboard/Knowledge, Email-Intelligence dashboard, contact-intelligence, standalone cross-company contact pages (§5.4/§8)
- [ ] W2.9 Backfill graveyard + startup.py backfills → alembic; write-only Notification table + writers deleted (§8/§5.5)
- [ ] W2.10 Legacy JSON create endpoint (POST /api/requisitions) deleted (§5.1, spec-named)
- [ ] W2.11 QP serial/FRU relink from workspace QP pane (§5.2 Decision E)
- [ ] W2.12 Resell→Sighting mirror dual-write stopped (§5.3; drafted W2, flip to W3 noted)
- [ ] W2.13 Glossary old→new table + screen-diet cut list FINALIZED for Packet 2 (application SIGN-OFF-GATED)
- [ ] W2.A Acceptance: exactly 5 tabs + gear; deleted surfaces 404; fresh-DB drift-gate boot green; kernel E2E green → Packet 2

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
| Nav tab count | 10 + Settings (template: app/templates/htmx/partials/shared/mobile_nav.html) | 10 |

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

### Packet 1 decision queue (owner, one sitting)
- 4 disposition flip-ables (bid_due_alerts / auto_attribute / auto_dedup deletes; inbox_scan mining sub-ops now flag-gated off)
- Seeded-admin `can_approve_purchase_orders` toggle: ON lets the nightly kernel walk exercise the PO-verify + prepayment gates end-to-end (currently 2 honest skips)
- Spec v1.1 §12 final-read checkboxes (9 items)

## Backlog (Rule 2.1 stops — net-new items, never built during simplification)

- (none yet)
