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

## Phase 3 — Activate dormant features ✅ DONE
All four now live on staging (container recreated, health 200): Lusha, email-mining,
account-sweep were already `=true` in `.env`; added `OWNERSHIP_SWEEP_ENABLED=true` +
`SPEC_RESOLVER_ENABLED=true`. Clay + AI-screen already on. **Explorium + eBay DEFERRED**
(await keys, user's call). ✅

## Phase 2.5 — ⭐ API-SEARCH CORE DEEP-DIVE (user-raised, HIGH priority)
The parallel supplier-API search is the product's core — user mandate: highly functional,
optimized, stable. Fable deep-dive audit running (4 reviewers: connectors / orchestration /
infra-pooling-ratelimit-cache / Settings API-management tool → prioritized synthesis). On
completion: bring the fix-now/optimize/UX action plan to the user, then execute the
critical/high items. Findings drive a follow-up build.

## Phase 4 — Loose ends ✅ (mostly no-op — corrected after read-only prep)
- **API Keys tab** — NOT dead: `GET /v2/partials/settings/api-keys` (`settings.py:433`) is a
  live **302 redirect to the unified Connectors tab** — a backward-compat alias that old
  links AND several tests rely on. The "tab button never wired" note is RESOLVED by design
  (API-key management folded into Connectors; no separate tab intended). *KEEP the redirect.*
  Only real remnant: a stale doc comment in `clay_oauth.py` ("Settings → API Keys" →
  "Connectors") — ✅ FIXED.
- **Code TODOs**: email calendar-delta (`email_jobs.py:82`, incremental-sync optimization)
  and startup category-alias backfill (`startup.py:1128`). *Rec: defer — minor optimizations,
  not incomplete features.*

## ⭐ USER-ADDED review/rework PROGRAMS (2026-07-03, queue after the API-core sprint)
Same treatment as the API-search core: deep review → prioritized findings → build. Each is
its own brainstorm→spec→plan→build (or an audit-workflow → action plan → build).
- **RESELL module — workflow / function / process review + optimization + improvement +
  rework.** The resell module (/v2/resell, models 127-133). Audit the end-to-end resell flow
  (offer→buyers→award→...) like the prepayment/API-search reviews: correctness, gaps,
  efficiency, UX; then rework. → Fable multi-lens audit workflow first.
- **CRM aesthetics / readability pass** — make the CRM (Customers/Contacts/Companies +
  detail panels) **easier to read, more pleasing to the eye, with the important info standing
  out WITHOUT being noise — clean + effective.** A taste/readability pass (like the app-wide
  taste-layer we shipped, focused on CRM): visual hierarchy, scannability, signal-vs-noise,
  the key facts (deal value, status, next action) prominent; muted chrome recedes. → invoke
  frontend-design; likely a targeted taste sweep, not a rebuild.

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
