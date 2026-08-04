# STATE.md — AVAIL Simplification progress

Single source of truth (Brief §3.1). Every session starts by reading
this file and ends by updating it. Decisions recorded here are not
re-litigated. Companion docs: SIMPLIFICATION_BRIEF.md (execution
discipline, wins on conflict) and SIMPLIFICATION_SPEC.md (work order).

## Current wave: Session 1 setup (pre-Wave 1)

- [x] S1.1 Create simplification branch (worktree /root/availai-worktrees/simplification, base bcfb9a54)
- [x] S1.2 Commit brief v2 + spec + this file to docs/
- [ ] S1.3 Baseline metrics recorded (table below)
- [ ] S1.4 Parallel instance up: prod-DB copy restored, boots clean, reachable
- [ ] S1.5 docs/CUTOVER.md v0 written
- [ ] S1.6 Nightly E2E pointed at the parallel instance
- [ ] S1.7 Wave 1 decomposed into checklist below; Wave 1 begun

## Baseline metrics (Rule 3.6 — recorded before any Wave 1 work)

| Metric | Baseline (2026-08-04 @ bcfb9a54) | Current |
|---|---|---|
| Route count | TO RECORD (S1.3) | — |
| Scheduled job count | TO RECORD (S1.3) | — |
| LOC app/routers/sightings.py | TO RECORD (S1.3) | — |
| LOC htmx_app.js | TO RECORD (S1.3) | — |
| LOC app/services/search_service.py | TO RECORD (S1.3) | — |
| Status count per entity | TO RECORD (S1.3) | — |
| Test file count | TO RECORD (S1.3) | — |
| Nav tab count | TO RECORD (S1.3) | — |

## Deviation log (Rule 3.4 — one line each, surfaced in the wave packet)

- 2026-08-04 S1: Brief v2 references "Spec v1.1"; the only spec that exists anywhere on disk is v1.0 (header still says AWAITING OWNER REVIEW). Operative work order = spec v1.0 + Brief §4 amendments (brief wins on conflict). If a v1.1 document exists in the owner's chat, drop it into docs/ and it will be reconciled next session.
- 2026-08-04 S1: Host-level cron jobs (backup_postgres.sh 6-hourly, daily_coverage_report.sh, check_fru_matrix_refresh.sh, weekly_cleanup.sh, nightly_tests.sh) operate on PRODUCTION; spec §6 removals of host-side duplicates are deferred to cutover and recorded in CUTOVER.md instead of being executed during Wave 1. In-app scheduler shrink proceeds normally on the branch.

## Backlog (Rule 2.1 stops — net-new items, never built during simplification)

- (none yet)
