# Prospecting Enrichment + Screening Program — HANDOFF (2026-06-19)

State saved for unifying multiple Claude instances into one for final assembly. This is the
single source of truth for where the 4-sub-project prospecting program stands.

## Program shape

Inflow (SP4) → enrich (SP1/SP2) → screen + rank (SP3) → claim. Specs in
`docs/superpowers/specs/2026-06-18-prospect-*`; plans in `docs/superpowers/plans/2026-06-18-*`.

| SP | What | Status |
|----|------|--------|
| **SP1** | Lusha in the enrichment chain (real contacts + firmographics) | **MERGED #395 + DEPLOYED + LIVE** (Lusha **enabled**) |
| **SP3** | AI account screening + match/opportunity scoring | **MERGED #398 + DEPLOYED + LIVE but DARK** (`AI_SCREEN_ENABLED` unset → off) |
| **SP4** | Account reclamation + park inflows + 90-day hardline sweep | **BACKEND built + reviewed, pushed (branch `feat/sp4-account-reclamation`), NOT merged, NO PR yet. UI pending approval.** |
| **SP2** | Clay async-primary enrichment | **DESIGNED ONLY** — blocked on Clay HTTP-API intake (prompt below) |

## What's live on the server (app.availai.net)
- Build `8872a22f-...` ; live DB alembic head was `122_prospect_ai_scores` after SP3 deploy.
- **SP1 Lusha is ENABLED** (`LUSHA_ENRICHMENT_ENABLED=true`, key in `.env`): clicking Enrich
  on a prospect — and CRM/vendor enrichment — now spends Lusha credits for real contacts +
  firmographics. Watch the Lusha dashboard; the only credit guard is a 15-min circuit-breaker
  on 402/429 (no hard monthly cap, per decision).
- **SP3 screening is OFF** (dark). To turn on: set `AI_SCREEN_ENABLED=true` (+ optionally
  `AI_SCREEN_MIN_MATCH`, `AI_SCREEN_DAILY_CAP=200`). When enabling, do a live headless-browser
  check of the screen UI (curl can't exercise htmx/Alpine) — cards get AI Match/Opportunity
  bars, detail gets a verdict card, the list gets a "Screened out / low fit" bucket, and the
  default sort becomes AI-match.

## SP4 — what's done vs pending
- **Built (backend, branch `feat/sp4-account-reclamation`, up to date with origin/main, 55 tests
  green, single head `123_sp4_park_provenance`):** migration 123 (provenance cols
  `swept_from_owner_id`/`swept_at`/`parked_by_id`); config (`account_sweep_enabled=False`,
  `account_sweep_inactivity_days=90`, `account_sweep_manager_email`, `account_reactivation_sweep_enabled`);
  `activity_service.get_last_activity_at()`; APScheduler sweep + auto-surface jobs; the
  **90-day HARDLINE sweep** (`prospect_reclamation.py`, Graph loss-email to rep + CC manager,
  idempotent via `swept_at`, no-op while disabled); auto-surface of unassigned past customers;
  `reclaim_prospect_account()` service + `POST /v2/partials/prospects/{id}/reclaim` (authz:
  former owner / admin / sweep manager). Plan: `docs/superpowers/plans/2026-06-18-sp4-account-reclamation.md`.
- **NOT built — Tasks 8 & 9 (UI), PENDING USER APPROVAL:**
  - **"Park in prospecting"** button on the CRM Company detail page (with a confirm dialog) —
    backend `send_company_to_prospecting` + park wrapper already exist.
  - **"Reclaim"** button on swept/parked prospect cards (visible to former owner / manager /
    admin) — proposed with a **justification note** logged to the timeline ("make an argument").
- **Next for SP4:** get UI approval → build Tasks 8 & 9 (TDD) → open PR → CI → merge → deploy
  (detached-HEAD recipe). Ships dark; enable later with `ACCOUNT_SWEEP_ENABLED=true` (set
  `ACCOUNT_SWEEP_MANAGER_EMAIL` first — defaults to admin).

## Worktrees (mine)
- `.claude/worktrees/sp4-reclamation` — ACTIVE (SP4). Clean + pushed.
- `.claude/worktrees/sp3-screening` — SP3 merged; safe to `git worktree remove`.
- `sp1-lusha` — already auto-removed (SP1 merged).
- (Other worktrees — buy-plan-deal-hub, comm-ledger-alerts, p2d-2e-inline-dnc, parked-items-batch —
  belong to OTHER sessions; do not touch.)

## Operational gotchas learned this run
- **Deploy around the concurrent-WIP host (detached-HEAD recipe):** `git -C /root/availai stash
  push -u` → `git checkout --detach origin/main` (leaves `main` ref put) → `./deploy.sh --no-commit`
  → `git checkout main` → `git stash pop`. Restores onto the unchanged base, zero conflict.
- **deploy.sh build-tag "MISMATCH" is a FALSE ALARM** (~9s ts drift, same commit). Verify a real
  deploy by inspecting the running container (`docker compose exec app` for the file + `psql` for
  the alembic head), not the tag.
- **Migration-number treadmill:** the repo has NO GitHub auto-merge and a hot concurrent-merge
  window. Each migration-bearing branch must re-number + re-chain onto the latest head every time
  someone lands first (SP3 went 120→121→122). Coordinate via `MIGRATION_NUMBERS_IN_FLIGHT.txt`.
  Merge-then-immediately is the only way to win the race; `tests/test_migration_chain.py` guards it.
- **NEVER run `alembic upgrade` against the shared `DATABASE_URL`** from a worktree (mutates prod);
  use a disposable sqlite or just `alembic heads`. Real migrations run via the deploy entrypoint.

## SP2 (Clay) — blocked; the API-intake prompt to hand a coworker
SP2 needs Clay's HTTP-API/webhook details before it can be built. Use this prompt (also covers
verifying Lusha/Apollo/Explorium + Anthropic):

> ROLE: Gather authoritative, CURRENT API integration details for AvailAI's enrichment system.
> Pull from the actual vendor dashboards + official docs; mark UNCONFIRMED if unsure.
> For EACH provider: (1) plan/tier + owner; (2) credit allocation + current balance + reset;
> (3) cost per company/contact/verified-email lookup; (4) auth scheme (exact header) + key
> location; (5) endpoints we use (base URL, version, params, FULL response JSON incl. the
> email-verified field name + firmographic field names); (6) rate/concurrency limits; (7) docs URL.
> LUSHA: confirm our v2 company-by-domain + contact-search endpoints, whether our plan includes
> the Prospecting/contact-search API, and the verified-email field. APOLLO/EXPLORIUM: plan +
> endpoints + limits. ANTHROPIC: models our key can use + RPM/TPM + spend.
> CLAY (most important): confirm HTTP API/inbound webhooks are on our plan, then document the
> round-trip — (A) INBOUND: the webhook URL we POST a domain to + its auth + body + limits;
> (B) TABLE: the enrichment columns (firmographics + contacts filtered to procurement/supply-chain
> titles) + an HTTP-action column that POSTs results to OUR callback; (C) CALLBACK: the EXACT
> payload schema Clay POSTs back (a redacted real example), how our correlation token is echoed,
> and how Clay signs/authenticates the callback (shared secret/header); (D) credit model (confirm
> "waterfall, billed only on success"), allocation + balance, and typical enrichment latency.
> DELIVERABLE: one doc, a section per provider, all fields filled or UNCONFIRMED. SECURITY: never
> paste full secret keys; state only where each lives; deliver new keys via the secure channel.

The single highest-value item for SP2 is **Clay callback payload schema (C)** — SP2's
`normalize_callback` is blocked on it.

## Recommended order for the unified session
1. SP4: approve + build the two UI buttons → PR → merge → deploy (dark).
2. Decide when to flip `AI_SCREEN_ENABLED=true` (+ the live UI check) and `ACCOUNT_SWEEP_ENABLED=true`.
3. SP2: once the Clay intake (above) is filled, build per `2026-06-18-prospect-enrichment-sp2-clay-async-design.md`.
4. Housekeeping: `git worktree remove` the merged sp3-screening; the stranded `cb1d6932` plan-doc
   commit on local `/root/availai` main is harmless (its content is on origin via the #395 squash).
