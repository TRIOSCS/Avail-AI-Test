# Backlog Completion — Phased Plan (2026-07-03)

From a deep search for stashed / uncompleted / unstarted work. Each item has a
recommendation; decisions captured are marked ✅.

## Phase 1 — Git hygiene ✅ (approved)
- Archive the ~45 merged-lingering branches to tags via `scripts/branch-cleanup.sh`
  (verified merged — e.g. `fix699` content is in main as squash `#699`).
- Drop the superseded stash `stash@{0}` (its files — `test_proactive_prepare.py`,
  `_contact_picker.html` — are already in main; it also stale-deletes a test main still has).
- Clear the stray uncommitted file in the `pr697-verify` worktree.
- Decide PR #708 (auto nightly-coverage): merge (like #628) or close. *Rec: merge if green.*

## Phase 2 — Finish prepay closure (IN PROGRESS)
Task 1 (migration 179 lifecycle columns) done + committed (`4c2a4f94`). Remaining Tasks 2–9
per `docs/superpowers/plans/2026-07-03-prepay-closure.md`: approve/reject transitions,
`mark_prepayment_paid`, paid/voided notifications, public tokenized confirm-paid route +
email link, in-app mark-paid + undo, void-on-teardown of an approved prepayment, Paid/Void
badges, docs+deploy. *Rec: resume immediately — it's live-code half-done.*

## Phase 3 — Activate dormant features ✅ (approved set)
Flip on + live-verify (no new key needed):
- **Lusha enrichment** — key present in `.env`; set `lusha_enrichment_enabled=true`.
- **Email-mining** — `email_mining_enabled=true` (Graph mailbox access).
- **Ownership + account sweeps** — `ownership_sweep_enabled=true`, `account_sweep_enabled=true`.
- **Spec-resolver** — `spec_resolver_enabled=true`.
Each: flip in `.env`, deploy, verify the feature runs, watch for errors.
- **Already ON** (no action): Clay enrichment, AI-screen.
- **Await your key** (OPEN): **Explorium** (no key), **eBay** (empty client id) — supply
  keys to activate, or defer.

## Phase 4 — Loose ends *(rec: accept unless you object)*
- **API Keys tab** — dead/orphaned: the route `GET /v2/partials/settings/api-keys`
  (`settings.py:433`) is superseded by the Connectors tab ("replaces sources + api-keys
  tabs"), and its nav button was never wired. *Rec: REMOVE the dead route + its template +
  fix the stale `clay_oauth.py` "Settings → API Keys" doc comment. Not a rebuild.*
- **Code TODOs**: email calendar-delta (`email_jobs.py:82`, incremental-sync optimization)
  and startup category-alias backfill (`startup.py:1128`). *Rec: defer — minor optimizations,
  not incomplete features.*

## Phase 5 — Future programs *(rec: schedule as their own projects, NOT this sprint)*
Written specs/plans that are multi-week roadmap efforts, not quick completions:
- Vendor-API parametric enrichment (the ~6-month filters program).
- CRM phase-5b pipeline forecast.
- CRM-redesign remaining roadmap.
- Supervise-lens redesign (may be partly superseded by the approvals rework — audit first).
- Deferred-high-tier roadmap.
*Rec: revisit each as a standalone brainstorm→spec→plan→build after Avail is fully live.*

## Phase 6 — Your side (data/config, not code) *(yours whenever)*
- SFDC data import (no near-term date).
- March enrichment recovery (SFDC Weekly Export, mkhoury OneDrive).
- Launch-readiness config flips (disable password login after Microsoft sign-in, DO Spaces
  backup creds, the 3 prepayment notification config keys, the datasheet SharePoint site).

## Not found / good news
- No genuinely LOST work: the one stash is superseded; the ~45 branches are merged-lingering;
  the code TODO markers are almost all `TaskStatus.TODO` enum values / format examples;
  every skipped test is an environmental guard (gpg/Redis/schemathesis absent).

## Sequencing recommendation
Phase 1 (fast, parallel-safe) → Phase 2 (finish closure) → Phase 3 (activate flags) → Phase 4
(remove dead code) → then Phase 5 as scheduled programs. Phase 6 is yours.
