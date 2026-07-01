# AvailAI Production-Ready Total Polish — Program Plan

## Context

The June 109-finding audit remediation (#598–#617) and the workflow deep-review (#619–#623) are fully merged and deployed; main is at `5e6b8794` with CI green and staging healthy. The user wants a finishing pass to make AvailAI production-ready in every dimension: a deep fresh code review (weighted toward what the June audit did NOT cover), plus cleanup of GitHub (branches, worktrees, drafts, issue #464), all server files on `/root`, repo organization, aesthetics, docs — ending in a staging deploy.

**Approved by user:** full fresh weighted review (heavy, ~12 dimensions, multi-workflow, adversarially verified) · archive-then-remove server files · full GitHub scope incl. #464 · hygiene-first phased structure · **fully autonomous execution to the end including CI-gated merges and the final deploy** — stop only for genuine design forks or truly destructive/ambiguous steps.

**Standing guardrails (from project memory):** root-cause fixes only, TDD for behavior changes, `pre-commit run --all-files` before pushing, full-suite ripple runs for behavior changes, adversarial review of every agent-produced fix, worktree agents use relative paths only (absolute-path leak), verify CI via `statusCheckRollup`/run conclusion not `--watch`, Alembic ids ≤32 chars, migration round-trips on throwaway Postgres (never staging DB), update APP_MAP docs after code changes, `gh pr edit` broken → `gh api -X PATCH`, hooks block force-push/branch -D (use branch-cleanup.sh), deploy via `./deploy.sh` then live-verify on real PG with seed admin + CSRF-aware requests.

## Phase 0 — Hygiene (mechanical, reversible)

**0a. Commit program spec.** Write this plan as a durable spec to `docs/superpowers/specs/2026-07-01-production-polish-design.md` on a branch, PR, merge (or fold into the first Phase-0 PR).

**0b. GitHub sweep.**
- `scripts/branch-cleanup.sh` (dry-run first, review output, then `--apply`, then `--apply --remote`). It never touches open-PR branches; unmerged branches are archived to pushed `archive/<name>` tags. ~200 local / 35 remote branches.
- Prune ~46 leftover workflow worktrees under `/root/availai/.claude/worktrees/` (`git worktree remove` + `git worktree prune`) — verify no uncommitted work in each first.
- Draft PRs: rebase #593 (1 test file) and #603 (14 util-test files) onto main; **verify their tests target live modules** (#614's phone-normalizer consolidation may have orphaned some of #603's targets: `test_utils_phone.py`, `test_utils_phone_utils.py`, `test_utils_normalization_helpers_ext.py`); run them; merge if green + meaningful, else close with an explanatory note. Where a file is partly stale, fix imports/drop stale tests rather than discarding the whole file.

**0c. Server file cleanup.** Build the full inventory with per-item verdict; archive to `/mnt/volume_sfo2_1782582546660/backups/root-archive-2026-07/` (preserving names), verify the copies, then remove from `/root`.
- **KEEP (live/untouchable):** `availai/`, `availai-credentials-backup/`, `availai-worktrees/` (verify empty→remove if so), `backups` symlink, `ics_browser_profile/`, `nc_browser_profile/`, `tbf_browser_profile/`, `source_ingest/`, `/root/scripts/` (crontab-referenced), **`Material Items/` (live FRU cron drop-folder — `check_fru_matrix_refresh.sh` reads it)**, `snap/`, dotfiles/caches, `.ssh`, `.docker`.
- **ARCHIVE→REMOVE:** `avail.zip`, `avail-v109/110/111.zip`, `fix.zip`, `update.zip` (Feb), `Desktop/` (Feb JS/HTML dumps), `ibm_rfq/` (Apr one-off analysis), `martina_tewes_accounts.csv`, `create_inventory_xlsx.py`, `send_brokersite_outreach_FINAL.py`, `send_output.log`, `enrichment.log`, `automation-setup.{sh,log}`, `quarantine/`, `repro/`, `docs/sourcing-engine-handoff/` (Mar; also exists in repo docs/ — verify duplication first), `overnight_enrichment/` (after cron removal below), `tbf_capture/` (verify not referenced by tbf worker first).
- **SPECIAL:** `env-backup` (contains secrets) → move into `availai-credentials-backup/`, never the general archive. `.bash_history` etc. untouched.
- **Crontab:** remove the TEMP overnight-enrichment tracking entry (marked "remove after planning" — planning long done). All other entries stay.

## Phase 1 — Review sweep (heavy, multi-workflow, on frozen main)

Multiple Workflow runs, findings adversarially verified (3-lens skeptic panels), consolidated into a ranked report **committed to `docs/audit/`** (June's report was lost to an ephemeral scratchpad). Dimensions & weights:

- **DEEP (never audited):** **end-to-end workflow functional verification (user directive 2026-07-01): trace every core workflow top-to-bottom — requisition intake → search/sourcing → offers/qualification → quote build/send/win → buy plan → approvals (A–F, both gates) → PO → receiving → resell, plus CRM/prospecting/connectors/settings; verify every button, field, and CTA is wired (handler exists, HTMX target/trigger/params correct), every step-to-step transition works, no dead ends; and assess each step for simplicity/clarity — easy to see, use, and understand** · UX/aesthetic conformance (design-system consistency, page-width policy `.page-fluid`/`.page-readable`, buttons/empty-states/modals, spacing/typography) · dead code & duplication (incl. known `mpn_chips_aggregated` → `_build_row_mpn_chips` chain; `fix_queue/` dir; stale root files `CODE_REVIEW_NOTES.md` [May review, likely superseded — verify then relocate/retire], `STABLE.md` [live registry — keep but verify current], `MIGRATION_NUMBERS_IN_FLIGHT.txt` [live coordination log — keep]) · docs freshness (APP_MAP_ARCHITECTURE/DATABASE/INTERACTIONS vs code; numbered legacy docs 00–12 relevance) · test-suite health (root-cause the ~9 known xdist flakes: nc_worker circuit-breaker, user_mgmt/vendor/activities; suite runtime) · repo organization · CI/workflow efficiency (Actions, nightly_tests.sh, pre-commit) · config/env-flag hygiene (dormant flags: Explorium/Clay blending, QP routing, etc. — inventory, don't remove approved-dormant features) · server/ops (compose, Caddy, crons, backup rotation, log rotation, disk)
- **MEDIUM:** performance (N+1, indexes, page weight) · dependency hygiene (`requirements*.in`, npm audit, unused deps) · frontend assets (Vite build, unused static assets)
- **LIGHT (just remediated):** correctness/security — regression-focused adversarial pass over the ~25 PRs merged since `b21e62d8` (esp. #618–#623 which postdate the last regression sweep)

Each finding tagged: fix-now (Phase 2) / aesthetics (Phase 4) / docs (Phase 5) / waive-with-reason.

## Phase 2 — Remediation waves

Themed CI-gated PRs per dimension as findings verify. Proven pattern: parallel worktree Workflows for disjoint fixes (relative paths only) → verify each returned SHA's `git show` diff is non-empty and in expected files → cherry-pick onto clean branch → `pre-commit run --all-files` (cherry-pick bypasses hooks) → full-suite ripple → adversarial review → merge on green, verify main CI green before stacking. Includes dead-code chain removal and xdist flake root-causes. Classify test failures per-file against the diff (never sample-and-assume).

## Phase 3 — Schema-drift #464 (models now final)

Work the issue's own checklist in `scripts/check_schema_matches_models.py` (`_GRANDFATHERED_*` sets / `_ALLOWLIST`):
- Safe reconciliations via real Alembic migrations: drop dead `activity_log.source_url`, `vendor_responses.teams_alert_sent_at`, stale FK `fk_activity_log_quote`; reconcile trigram/GIN indexes (declare on models or document as intentional raw-DDL); UniqueConstraint reflections; TIMESTAMP→UTCDateTime custom-type reflections.
- **DANGER — never drop:** `buy_plans`, `enrichment_credit_usage`, `notifications`, `_sp1_desc_backup` (migration 091 downgrade path). These stay grandfathered, documented.
- Batched migrations, each up/down round-tripped on throwaway PG; claim numbers in `MIGRATION_NUMBERS_IN_FLIGHT.txt`; `migration-full-cycle` CI green; remove reconciled entries from the grandfather sets; close #464 when only DANGER entries remain (per issue exit criteria).

## Phase 4 — Aesthetics/UX polish

Execute Phase-1 UX-conformance findings: conformance fixes across surfaces, Tailwind classes verified present in built CSS, headless HTMX render verification for changed partials (hx-trigger=load needs hx-target; hx-push-url inheritance), static ratchet tests respected. Frontend-design skill guidance where new visual decisions arise; no redesign — conformance and finish only.

## Phase 5 — Docs & organization

APP_MAP docs refreshed to final code state (feedback_update_app_map); README/LOCAL_SETUP accuracy pass; CLAUDE.md audit via claude-md-improver; stray root files relocated/retired per verified Phase-1 verdicts; final review report in `docs/audit/`; legacy numbered docs marked historical if stale (verify, don't assume).

## Phase 6 — Final gate → deploy → live-verify

1. Fresh adversarial regression review of the **entire program diff** (`5e6b8794..main`) — the #617 lesson: remediations introduce their own regressions; fix anything found.
2. Full suite + `pre-commit run --all-files` green; main CI green (via run conclusions).
3. `./deploy.sh` from `/root/availai` (authorized by user's "fully autonomous" selection); watch for stale-DNS crash-loop (compose down/up if "host name db").
4. Live-verify: seed-admin login on real PG, **drive full workflows end-to-end (not just page loads): create requisition → search → qualify offer → build/send quote → win → buy plan → approval gates → PO confirm → receive, plus CRM/prospecting/resell surfaces**, CSRF-aware POSTs, workers active, Tailwind classes in built CSS, no concurrent-deploy staleness (verify container build SHA).
5. Close the loop: update memory + report (PRs merged, findings fixed/waived, archive manifest, before/after metrics).

## Autonomy contract

Fully autonomous through all phases including merges and the final deploy. Stop ONLY for: genuine design forks (product-behavior decisions), destructive actions outside the approved dispositions above, or evidence contradicting a planned disposition (e.g. an "obsolete" file turns out referenced). Report at each phase boundary; user can interrupt anytime.

## Verification summary

- Every PR: CI green via statusCheckRollup/run conclusion before merge; main CI green before next wave.
- Behavior changes: TDD + full-suite ripple; failures classified per-file against the diff.
- Migrations: throwaway-PG up/down round-trip + migration-full-cycle job.
- Server cleanup: archive copies verified (size/hash) before removal; crontab re-listed after edit; live crons untouched.
- End: staging deployed, live-driven, workers healthy; #464 closed; branch list reduced to active-only; report committed to docs/audit/.
