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
- [ ] W1.1 Delete Explorium discovery machine jobs (6 monthly) (§6)
- [ ] W1.2 Park 8x8 CDR poll job — off the scheduler, existing flag, Data Capture Initiative comeback (§6 v1.1)
- [ ] W1.3 Tagging suite: delete 2 zero-yield jobs; prefix/spec jobs → on-demand management commands; AI tagging on-demand only (§6)
- [ ] W1.4 Park remaining non-kernel jobs per disposition table; scheduler == kernel list exactly (§3, §11)
- [ ] W1.5 Keys-off honesty: BOM "Parse with AI" honest state, no 500 (§7)
- [ ] W1.6 Keys-off honesty: search "external sources off" notice + AI-intent plain-search fallback (§7)
- [ ] W1.7 Keys-off honesty: CRM Enrich free SAM.gov path (fix 503 guard) + paid providers labeled off (§7)
- [ ] W1.8 Badge consolidation: 6 pollers → one badge endpoint (§5.5)
- [ ] W1.9 Dead statuses removed (prod-copy zero-use evidence per status) (§5.3/§9)
- [ ] W1.10 Delete the 2 dead transition tables (§9)
- [ ] W1.11 Kernel-walk E2E script against the deployed instance, failure pages ONE admin; simp-nightly.sh runs it (§3/§6)
- [ ] W1.12 Backup verify-timer unit files ready in repo; installation deferred to cutover (§6; CUTOVER.md §5)
- [ ] W1.13 Task statuses → open/done (§5.4, v1.1 §0.6)
- [ ] W1.14 Trouble-ticket AI calls gate behind AI flag, regex fallback per brief §4.5 (§5.5, v1.1 §0.6)
- [ ] W1.A Acceptance checks pass → assemble + deliver Packet 1 (brief §6)

## Baseline metrics (Rule 3.6 — recorded before any Wave 1 work)

All numbers independently re-derived by a second pass before recording.
Route count = runtime `app.routes` (flattened), not decorator grep.

| Metric | Baseline (2026-08-04 @ bcfb9a54) | Current |
|---|---|---|
| Route count | 809 (268 /api, 516 /v2, 25 other; 753 unique paths) | 809 |
| Scheduled job count (in-app) | 59 (all in app/jobs/*.py, 17 modules) | 59 |
| Host cron jobs (prod droplet) | 5 (nightly tests, weekly cleanup, legacy pg backup, FRU check, coverage report) | 5 |
| LOC app/routers/sightings.py | 3,812 | 3,812 |
| LOC app/static/htmx_app.js | 3,654 | 3,654 |
| LOC app/search_service.py | 3,604 | 3,604 |
| Status values (all entities) | 114 across 21 enums — resell subset = 34 (ExcessList 9, LineItem 4, Offer 5, OfferLineMatch 3, CustomerBid 4, Outreach 9); BuyPlan 7; Requisition 9; Task 3 | 114 |
| Test file count (pytest) | 1,113 | 1,113 |
| E2E spec files (Playwright) | 12 | 12 |
| Nav tab count | 10 + Settings (template: app/templates/htmx/partials/shared/mobile_nav.html) | 10 |

## Deviation log (Rule 3.4 — one line each, surfaced in the wave packet)

- 2026-08-04 S1 (RESOLVED same day): Session started on spec v1.0 (only version on disk); owner supplied v1.1 mid-session. docs/SIMPLIFICATION_SPEC.md replaced with v1.1 verbatim; deltas folded in: 8x8 poll delete→park (Data Capture Initiative), Proactive parks whole, task statuses + trouble-ticket gating added to Wave 1 (W1.13/W1.14), Settings gear outside a literal 5-tab bar, resell 34→5 named statuses with Wave-3 remap. Disposition table reconciled (keep 7 / park 12 / delete 40).
- 2026-08-04 S1: Login visual-regression baseline was stale on main (snapshot from the original frontend-testing commit; login.html restyled twice since — password form + light theme are deliberate). Re-baselined on this branch after image inspection; note: PRODUCTION's own nightly is red on this same test and stays red until main gets the same re-baseline (owner's call — outside this branch's scope).
- 2026-08-04 S1: Nightly E2E v0 = deployed-instance smoke + branch Playwright suite (with Vite build step — missing dist/ renders every page unstyled). The deployed-app kernel-walk script replaces the smoke in W1.11 per spec §10.
- 2026-08-04 S1: Host-level cron jobs (backup_postgres.sh 6-hourly, daily_coverage_report.sh, check_fru_matrix_refresh.sh, weekly_cleanup.sh, nightly_tests.sh) operate on PRODUCTION; spec §6 removals of host-side duplicates are deferred to cutover and recorded in CUTOVER.md instead of being executed during Wave 1. In-app scheduler shrink proceeds normally on the branch.

## Backlog (Rule 2.1 stops — net-new items, never built during simplification)

- (none yet)
