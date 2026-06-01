# 2026-05-27 — Phased Roadmap: Deferred High-Tier Cleanup

**Author:** Claude (Opus 4.7, 1M-context session 2026-05-27)
**Driver of this plan:** TRIOSCS / mkhoury — original brief said "Hold these till I say go." That go-ahead was given on 2026-05-27 23:30 UTC, after the cleanup-queue cascade landed (PRs #149, #150, #151, #161, #168, #169, plus 5 dependabot bumps).

**Scope of this document:** sequence + execution plan for the four deferred items the user explicitly named:
- **HIGH-DB-2** — `Column(DateTime)` → `UTCDateTime` migration
- **HIGH-SEC-4** — Microsoft Graph `validationToken` echo is unauthenticated
- **HIGH-BE-11** — ~1,166 SQLAlchemy 1.x `db.query(...)` callsites → 2.0 style
- **HIGH-BE-1/2** — Split `htmx_views.py` (9,918 LOC, 249 routes) and other god files

Other deferred items (HIGH-TEST-2/3/5, HIGH-DEVOPS-1/7, HIGH-BE-10) are listed under "Future Phases" at the end — they are not blocked by this roadmap but are not in scope for the current execution sequence.

---

## Sequencing rationale

```
Phase 1: HIGH-DB-2 (UTCDateTime)        ┐
                                         ├── parallel-able
Phase 2: HIGH-SEC-4 (Graph webhook)     ┘
                                         ↓
Phase 3: HIGH-BE-11 (SQLAlchemy 2.0 migration)
                                         ↓
Phase 4: HIGH-BE-1/2 (split god files)
```

**Why this order:**

- **DB-2 before BE-11.** SQLAlchemy 2.0 migration touches every model query. If column types are migrated *during* the query refactor, every query reads/writes timezone-aware datetimes inconsistently with the surrounding code that still uses `datetime.now()` against `Column(DateTime)`. Doing DB-2 first creates one consistent baseline; BE-11 then refactors query syntax against a stable column type.

- **SEC-4 in parallel with DB-2.** Different files, different teams of concern. SEC-4 touches `Caddyfile` + `app/routers/v13_features/activity.py` + alerting config. DB-2 touches `app/models/*.py`. Zero overlap.

- **BE-11 before BE-1/2.** Splitting `htmx_views.py` creates ~8-12 new sub-routers. Doing the split first means every new sub-router carries forward the legacy `db.query(...)` patterns; subsequent BE-11 work then has to migrate twice as many file locations. Doing BE-11 first means each sub-router is born with modern API usage and the split is a pure decomposition without behavior change.

- **BE-1/2 last.** Highest blast radius. Building on a clean DB layer + clean query API reduces compounding risk.

---

## Phase 1 — HIGH-DB-2: UTCDateTime migration — DONE (shipped via #183)

**Status: DONE.** Shipped as PR #183. The actual fix:

- A **symmetric `UTCDateTime`** type in `app/database.py` — `load_dialect_impl` forces
  PostgreSQL `TIMESTAMPTZ`, and `process_bind_param` normalizes writes to tz-aware UTC
  (symmetric bind + result). Reads and writes are timezone-aware on both sides.
- Migration **`085_utcdatetime_timestamptz`** — a guarded conversion of the columns that
  were still tz-naive, preserving values. Columns that were already deliberately
  `timezone=True` are left as-is (their per-column `timezone=True` is preserved).

The earlier blanket plan in this doc (PR-1.1 → PR-1.4: "convert all ~197 `Column(DateTime)`
→ `UTCDateTime` and flip everything to `TIMESTAMPTZ` via one big PR-1.3") was **dropped as
inaccurate** — it mis-scoped the 38 columns that were intentionally `timezone=True` and
treated them as needing conversion. The shipped approach (symmetric type + guarded
per-column migration) is the correct, narrower fix.

---

## Phase 2 — HIGH-SEC-4: Graph webhook validation hardening

**Goal:** The Microsoft Graph webhook subscription handshake (`validationToken` echo) is currently unauthenticated — Graph spec compliant but anyone can POST a validationToken and get it echoed. Mitigate at the edge.

**Current state:**
- `app/routers/v13_features/activity.py:49–51, 88–90` echoes `validationToken` unauthenticated (required by Graph protocol).
- `Caddyfile` does not currently IP-restrict the Graph webhook routes.

**Scope:**
1. Identify Microsoft Graph subscription notification URL paths in AVAIL (`/api/webhooks/graph`, `/api/webhooks/teams`, etc.).
2. Add an IP-allowlist directive in `Caddyfile` matching Microsoft's published ranges for those paths.
3. Add a Sentry alert (or loguru `logger.warning`) when a validation handshake fires unexpectedly (outside of an active subscription renewal window).
4. Document the new edge contract in `docs/APP_MAP_ARCHITECTURE.md` under "External Service Integration".

**PR shape:**
- **PR-2.1 (Caddy IP allowlist):** Single Caddyfile change + matching `Caddyfile.example`. Includes a test against the deployed config to confirm Graph IPs still reach the route.

**Dependencies:** none — fully independent of Phase 1, can run in parallel.

**Risks:**
- *Microsoft IP ranges shift over time.* Mitigation: pull from Microsoft's published `ServiceTags.json` either at deploy time or via a scheduled job that regenerates the Caddy snippet. Decision: **start with a hardcoded snapshot, add the regeneration job as a follow-up.**
- *Webhook delivery breaks on the IP-allowlist deployment.* Mitigation: deploy during low-traffic window; have the Caddy snippet ready to revert.

**Checkpoints:**
- Before deploy: validate the Caddyfile syntax (`caddy validate`).
- After deploy: trigger one Graph subscription renewal in a controlled window and confirm webhook still delivers.

**Rollback:** Revert the Caddyfile change. Re-deploy.

**Estimated total:** 1 PR, ~30 LOC, half a session.

---

## Phase 3 — HIGH-BE-11: SQLAlchemy 2.0 migration

**Goal:** Migrate ~1,166 `db.query(Model)` callsites to the SQLAlchemy 2.0 idioms (`db.execute(select(...))` / `db.get(Model, id)` / `db.scalars(...)`).

**Current state:**
- SQLAlchemy 2.0.49 is already the installed version.
- The 2.0-compatible API works alongside the legacy 1.x API in `LegacyAPIWarning` mode — no immediate breakage if we don't migrate, but every callsite emits a warning and prevents the eventual `LegacyAPIWarning → error` cutover.
- `db.query(Model).get(id)` already explicitly banned in CLAUDE.md as an anti-pattern (use `db.get(Model, id)`).
- CODE_REVIEW_NOTES.md count: 1,166 sites (slightly up from 1,163 at original review — suggests new sites slip in faster than we migrate them; **establish a linter rule alongside the migration**).

**Scope:**

1. **Lint rule first.** Add a custom ruff rule or pre-commit script that flags new `db.query(` introductions. Prevents regression while the migration is in flight.

2. **Migrate in domain waves**, not file-by-file — file-by-file PRs would be ~80 separate PRs and a logistical nightmare. Domain waves cluster related callsites so each PR has a coherent test surface.

3. **Wave order** (smallest-blast-radius first):
   - **Wave A** — `app/routers/requisitions/` (already partially modernized in PR #168/#177; bulk-update path is done). Finish the rest of the router. ~120 callsites.
   - **Wave B** — `app/routers/crm/` + `app/services/requisition_service.py`. CRM router family is medium-sized and isolated. ~150 callsites.
   - **Wave C** — `app/services/*.py` (non-search). Service layer is heavily called but the call shape is uniform — high pattern density, mechanical migration. ~300 callsites.
   - **Wave D** — `app/search_service.py` + `app/email_service.py` + `app/services/knowledge_service.py`. Big files with complex query patterns. ~250 callsites.
   - **Wave E** — `app/routers/htmx_views.py`. The big one. ~350 callsites. **Held until after Phase 4 begins** (split first, migrate the sub-routers individually — that's the natural fold-in).

4. **Test coverage on every wave.** Existing tests catch most regressions, but each wave should:
   - Run the full pytest suite locally before push.
   - Spot-check 2-3 endpoints manually against the deployed app to confirm response payload shape didn't change.

**PR shape:**
- **PR-3.0 (lint rule):** Ruff custom rule or pre-commit hook. Prevents new `db.query` introductions. ~30 LOC config + a small Python check script.
- **PR-3.A, 3.B, 3.C, 3.D:** One PR per wave above. Each ~100-300 LOC of mechanical edits + targeted test verification.
- **(Wave E folds into Phase 4.)**

**Dependencies:**
- Phase 1 complete (UTCDateTime baseline so the migrated queries return TZ-aware datetimes consistently).

**Risks:**
- *Subtle behavior change between `query.first()` and `execute(...).scalar_one_or_none()`.* Both return None for empty result, but `.scalar_one()` raises — migration should preserve the existing none-vs-raise contract per callsite.
- *Identity-map / session-state differences.* The 2.0 API doesn't auto-attach instances to the session in the same way `query(Model)` does. Mitigation: any callsite that mutates the returned object expects it attached — verify in tests.
- *Eager-loading idioms.* `query.options(joinedload(...))` → `select(...).options(joinedload(...))` is mechanical but easy to drop accidentally during migration. Each wave's PR should grep for `joinedload` / `selectinload` and verify they survived.

**Checkpoints:**
- After PR-3.0 (lint rule) — confirm the rule fires on a new `db.query` introduction in a test commit.
- After each wave — full pytest suite must be green; manual smoke on 2-3 endpoints in the wave's domain.

**Rollback:** Revert the wave's PR via `git revert`. Each wave is independent.

**Estimated total:** 5 PRs (3.0 + 3.A-D), ~1,000 LOC across the whole phase, 3-5 sessions.

---

## Phase 4 — HIGH-BE-1/2: Split `htmx_views.py` and other god files

**Goal:** Decompose `htmx_views.py` (9,918 LOC, 249 routes, 244 functions, 377 direct DB ops) into ~8-12 domain-specific routers. Same treatment for `search_service.py` (2,348 LOC) and `email_service.py` (1,277 LOC) — convert to packages.

**Current state:**
- `htmx_views.py` is the biggest file in the codebase. Per `grep '@router\.' app/routers/htmx_views.py`, routes span the full feature surface: requisitions, vendors, customers, parts, materials, quotes, sightings, search, prospecting, proactive, emails, tickets, settings, buy_plans.
- Routes are loosely organized by feature within the file but lack clean boundaries.
- Existing router files exist for many of these features (`app/routers/sightings.py`, `requisitions/`, `crm/`, `excess.py`, etc.) — `htmx_views.py` is the legacy catch-all that other routers were carved *out* of incompletely.

**Scope:**

1. **Inventory** — produce a map of every `@router.get/post/put` in `htmx_views.py` grouped by feature prefix:
   - `/v2/partials/requisitions/*`
   - `/v2/partials/vendors/*`
   - `/v2/partials/customers/*`
   - `/v2/partials/parts/*`
   - `/v2/partials/materials/*`
   - `/v2/partials/quotes/*`
   - `/v2/partials/sourcing/*`
   - `/v2/partials/search/*`
   - `/v2/partials/buy_plans/*`
   - `/v2/partials/proactive/*`
   - `/v2/partials/prospecting/*`
   - `/v2/partials/settings/*`
   - `/v2/partials/follow_ups/*`
   - top-level pages (`/`, `/login`, `/dashboard`)

2. **Migrate by domain prefix** — one PR per domain, smallest first.

3. **Per-domain PR shape:**
   - Create or extend the existing router file for the domain (e.g., `app/routers/vendors.py`).
   - Move the routes physically (cut from `htmx_views.py`, paste into the new file).
   - Migrate `db.query(...)` callsites to 2.0 style at the same time (fold-in for Wave E of Phase 3).
   - Update `app/main.py` to mount the new router.
   - Run the full pytest suite — existing tests cover most routes via path, so the move is invisible to tests.

4. **Sequence (small → large):**
   - **PR-4.A:** Settings routes (`/v2/partials/settings/*`) — small, isolated. Warm-up.
   - **PR-4.B:** Follow-ups + prospecting + proactive — small, related.
   - **PR-4.C:** Buy plans + quotes + materials — medium.
   - **PR-4.D:** Search + sourcing — medium-large, more complex queries.
   - **PR-4.E:** Vendors + customers — large, lots of HTMX partials.
   - **PR-4.F:** Parts + requisitions remainder — largest, most complex.
   - **PR-4.G:** Final cleanup — `htmx_views.py` reduced to its true catch-all (login/dashboard/base pages) or eliminated entirely.

5. **`search_service.py` and `email_service.py` → packages.** After (or in parallel with) the router split:
   - Create `app/search/__init__.py`, `app/search/orchestrator.py`, `app/search/connectors.py`, `app/search/normalize.py`, etc.
   - Move pieces of `search_service.py` into the package preserving public API at `app/search/__init__.py`.
   - Same for `email_service.py` → `app/email/`.

**PR shape:** 7-8 PRs as listed in step 4 + 2 PRs for the package conversions.

**Dependencies:**
- Phase 3 (SQLAlchemy 2.0 migration) — the sub-routers should be born on the modern API. The htmx_views.py split is Wave E of Phase 3 effectively.

**Risks:**
- *Imports of `templates`, helpers, constants in `htmx_views.py` — every moved route needs the imports moved too.* Mitigation: each PR uses a `grep` checklist to verify every import the moved code uses is present in the new file.
- *Behavioral change from route ordering or middleware coupling.* FastAPI route registration is order-sensitive in narrow cases (e.g., `/v2/partials/foo/{id}` vs `/v2/partials/foo/bar`). Each PR should verify the moved routes don't change the registration order in a way that changes resolution.
- *`templates` global state.* AVAIL uses a module-level `templates = Jinja2Templates(...)` singleton. New routers import it via `from app.template_env import templates, template_response`. Mechanical, low risk.
- *Test discovery.* Pytest finds tests by path, not by source. Moves are invisible. But any test that imports a function from `app.routers.htmx_views` by name will break — grep before moving.

**Checkpoints:**
- After PR-4.A — confirm the small migration pattern works before committing to larger waves.
- After each subsequent wave — full pytest suite green; manual smoke on 2-3 routes from the wave.
- After PR-4.G — confirm `htmx_views.py` is either gone or contains only the small set of routes that genuinely don't fit a domain.

**Rollback:** Each PR is a pure file-move + import update. Revert via `git revert` is safe.

**Estimated total:** 7-9 PRs, ~9,918 LOC moved across multiple sessions over 1-2 weeks of calendar time.

---

## Cross-phase rules (apply throughout)

- **No band-aids.** Each phase ships its full intent — if a wave reveals a deeper problem (e.g., a model lacks `UTCDateTime` because of a custom `DateTime(timezone=False)` cast), fix the underlying issue, not paper over it.
- **APP_MAP docs updated in the same PR as the code change**, per CLAUDE.md.
- **CODE_REVIEW_NOTES.md updated as findings close**, per the user's standing brief (PR #169 sets the pattern).
- **pr-review-toolkit fan-out runs on every PR**, per CLAUDE.md ("Run ALL pr-review-toolkit agents on every PR"). Apply no-brainers in a follow-up commit; defer larger findings.
- **Pre-commit `--all-files` before pushing**, per `feedback_pre_commit_all_files`.
- **No surprise scope changes.** If a phase is taking >2× its estimated PR count, stop and re-plan — likely the architecture needs a rethink (per systematic-debugging Phase 4.5).

---

## Future Phases (out of scope for *this* roadmap, but tracked)

These are deferred items the user named in the original brief but didn't ask me to plan now. Pulled here for visibility:

- **HIGH-TEST-2** — E2E `workflows.spec.ts` / `dead-ends.spec.ts` run unauthenticated. Fix: inject signed Starlette session cookies via `storageState` in `playwright.config.ts`. Independent of all phases.
- **HIGH-TEST-3** — 2,392 single-status-code-only assertions. Long-tail quality work; pairs with each phase's domain testing.
- **HIGH-TEST-5** — Skipped tests for removed routes (`test_tt105_user_validation.py:14`, `test_req_offer_fields.py:64,118`). Delete or restore. 30-min task.
- **HIGH-BE-10** — Duplicate cache machinery in `routers/sightings.py`. Should fold into Phase 4's sightings router pass.
- **HIGH-DEVOPS-1** — Backups not encrypted at rest. Add `--sse AES256` + `gpg --symmetric`. Independent.
- **HIGH-DEVOPS-7** — `alembic downgrade base` in PR CI slow + fragile. Switch to `downgrade -1 && upgrade head` for PRs.

These can be slotted in between the four primary phases as appetite allows; they don't gate or block each other.

---

## What this roadmap is NOT

- **Not a green-light to execute autonomously.** Each phase requires user go-ahead. The user's "phased plan to do all items" pick was authorization to *plan*, not to *execute*.
- **Not a commitment to dates.** Estimates are rough effort, not calendar. AVAIL is single-user staging (per project memory `project_app_stage_single_user`), so calendar urgency is lower than on multi-tenant prod.
- **Not exhaustive.** Other High-tier items not named by the user are out of scope. CRIT-tier items (CRIT-SEC-1, CRIT-DEVOPS-1/3/4, CRIT-FE-1/2, CRIT-TEST-2) remain higher priority than any of these phases if the user shifts focus.

---

## Next step

Phase 1 (UTCDateTime) is **done** (shipped via #183). Pick from the remaining phases: Phase 2 (Graph webhook) is the cheapest single PR if you want a quick win first.
