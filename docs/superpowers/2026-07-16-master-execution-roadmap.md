# AvailAI — Master Execution Roadmap (2026-07-16)

Sequences **all** outstanding work into phases: the 3 in-flight PRs, the 17 verified archaeology gaps
(`2026-07-15-missing-incomplete-projects-report.md`), the carried launch/tech-debt items, and the queued
module-rework programs (Resell has its own plan: `2026-07-15-resell-module-action-plan.md`).

**Sequencing logic:** land what's in flight → retire the highest-stakes/lowest-effort security surface →
correctness & data quick wins → the concurrency decision → requested feature builds → the Resell rework →
long-tail debt & module programs. Small-and-critical beats large-and-optional. Every phase ends deployable;
each migration claims a number in `MIGRATION_NUMBERS_IN_FLIGHT.txt`; every UI change is gated on your approval
(the "never add/remove/rearrange UI without approval" rule).

Sizes: **S** ≤ 0.5 day · **M** 0.5–1.5 d · **L** 2–3 d · **XL** multi-day program.

---

## Decisions I need from you (each has a recommendation — override any)

These are pre-resolved so the plan has no TBDs; confirm or redirect and I execute. I'll also raise each one
**at the start of its phase**, one at a time, so nothing is decided in a vacuum.

| # | Decision | Recommendation | Phase |
|---|---|---|---|
| D1 | **Concurrency fix approach** — single uvicorn worker + sync ORM in async handlers | Immediate: raise workers to 2–4 + size the DB pool to Postgres `max_connections`. Defer full async-SQLAlchemy migration (XL). | 3 |
| D2 | **CRM reporting page** — build the destination, or drop Phase-5 | **Build** it (you asked for "honest real-time reporting"); relocate the stray daily-hub chip out regardless. | 4 |
| D3 | **Calendar near-real-time** — delta poll vs push webhooks | **Delta first** (proposal ready, no public-callback infra); revisit webhooks (commit `ba7ef84d`, on origin tags) if push latency matters. | 4 |
| D4 | **Buy Plans hub nav** — dedicated bottom-nav item / in-page link / under "More" | **Dedicated bottom-nav item** (most discoverable for a daily-use hub). UI-approval gate. | 2 |
| D5 | **`enrichment_credit_usage` empty table** — drop or keep | **Keep** + record the decision (drift-gate protects it; memory flags it never-drop). No migration. | 2 |
| D6 | **QP vendor share link — redacted field whitelist** | Recover the whitelist from commit `1b0c13a0`; you confirm the vendor-safe fields before build. | 4 |
| D7 | **SQLAlchemy 1.x→2.0 mass migration** (~1,559 `db.query`) | Land the **lint guard now** (blocks NEW `db.query`); the mass migration stays **gated on your explicit go-ahead**. | 6 |
| D8 | **`deploy.sh` rollback** — port from `deploy.yml`, or revive `deploy.yml` as canonical | **Port rollback into `deploy.sh`** (the path actually used). | 2 |

---

## Phase 0 — Land in-flight work + unblock (0.5 d)
**Goal:** clear the board so later phases build on current main. All authored this session, review-clean.

| Item | State | Action |
|---|---|---|
| PR **#737** api-health revive + Settings wiring | CI green | Merge (your call) → deploy |
| PR **#738** delta_query bound + token-loss fix | CI green | Merge → deploy |
| PR **#739** migration 189 category backfill | fixed the one CI failure (stale rejected-category test); re-running | Merge after green → deploy (applies migration 189) |
| **Resell live-500 hotfix** (Resell plan Phase 0) | staging list #10 bid-assembly 500s (dangling `best_offer_id` from the sample-data seeder) | Repair rows via `recompute_line_rollup`, fix the seeder type-confusion, harden `build_bid_back` (validate pointer + `_safe_commit`). Staging-data only; safe. |

**Exit:** main carries #737–#739, staging redeployed & health-verified (migration head 189), Resell Build-Bid works on the live list.
**Note:** #737–#739 each also cleared a pre-existing hook-env mypy straggler (`faceted_search_service`) that had `pre-commit run mypy` red on main; merging any one fixes it on main.

---

## Phase 1 — Security & launch hardening (2–3 d)
**Goal:** close the "reads-as-done-but-isn't" cluster before multi-user go-live. Mostly small, highest stakes. Live-verify each (401/403/429/boot behavior) on staging.

| Item | Why it matters | Approach | Size | Migration |
|---|---|---|---|---|
| **Lock down `/docs` `/redoc` `/openapi.json`** | Full internal API surface (741 paths) served unauth through the public edge | Env-gate the FastAPI constructor to `…=None` in prod (or admin-gated Swagger) **and** add the 3 paths to Caddy `@blocked`; test 401/404 | S | — |
| **Register the global rate limiter** | `SlowAPIMiddleware` never added → the 120/min default is dead; only ~41 routes throttle | `add_middleware(SlowAPIASGIMiddleware)` guarded by `rate_limit_enabled`; confirm Caddy forwards real client IP; live-verify a 429 | S | — |
| **Stop silent admin re-promotion** | A demoted admin in `ADMIN_EMAILS` is re-promoted on next login | Persistent per-user `admin_opt_out` flag gated at `auth.py:155` (or true first-boot-only bootstrap) + re-login regression test | M | **yes** (opt-out flag) |
| **Graph-webhook edge IP allowlist** (HIGH-SEC-4) | App-layer token check shipped; the prescribed edge allowlist never did → routes edge-reachable | Caddy named matcher for `/api/webhooks/graph\|teams\|acs` gated by a Microsoft ServiceTag IP snapshot (acs already fails closed on secret) | M | — |
| **Password-login fail-boot guard** (blocker #1) | Only the log severity was raised; the boot guard was dropped | Boot `RuntimeError` unless `ALLOW_PASSWORD_LOGIN_RISK=true` + `deploy.sh` preflight assertion; gated so staging (accepted risk) still boots | S | — |
| **Set `ENCRYPTION_SALT` on staging** | Unset → legacy static salt fallback; untracked sibling of REDIS_PASSWORD/BACKUP_GPG | Ops: `openssl rand` → `.env` → `python -m app.management.rotate_encryption_salt` → recreate app+worker. Coordinated window. | S (ops) | — |
| **Set `REDIS_PASSWORD`** (carried) | Redis unauthenticated on staging | Coordinated `.env` + host-worker restart window | S (ops) | — |
| **Encrypt on-disk backups** (carried, `BACKUP_GPG_PASSPHRASE`) | Backups stored unencrypted (Spaces upload OK) | Set passphrase; verify the backup job encrypts + a restore round-trips | S (ops) | — |

**Exit:** every item live-verified on staging; the three ops secrets coordinated in one restart window; regression tests added where code changed. Ships as small, individually-revertable PRs.

**Build notes (found during 2026-07-16 grounding — do NOT skip these):**
- **Rate limiter is not a one-liner.** The `limiter` (`app/rate_limit.py`) already carries `default_limits=[120/minute]`; the only missing piece is `app.add_middleware(SlowAPIMiddleware)` in the `rate_limit_enabled` block of `app/main.py`. BUT: (1) `key_func=get_remote_address` returns the **Caddy proxy IP** behind the reverse proxy → a global limit would throttle **all users collectively** at 120/min. Must switch to a key func that reads `X-Forwarded-For`/`X-Real-IP` (and confirm Caddy sets it). (2) High-frequency internal traffic (`/health` every few sec, htmx pollers, approval-outbox) will trip 120/min from one IP → **exempt** `/health`, `/metrics`, static, and the poll endpoints (`@limiter.exempt` or path allowlist), and pick a default that doesn't lock out an htmx-heavy admin session. Needs a small load test on staging.
- **Docs lockdown has a test coupling.** Disabling `openapi_url` at the FastAPI constructor (app-layer defense-in-depth) breaks `tests/test_contract.py` (it fetches `/openapi.json` via the ASGI app + schemathesis). Do the **Caddy `@blocked` edge block first** (`path /metrics /docs /redoc /openapi.json` → 403; secures external access, no app/test impact), then the app-layer `docs_url/redoc_url/openapi_url=None` gated on a new `expose_api_docs` setting **together with** refactoring `test_contract.py` to use `app.openapi()` (the method, always available) instead of the HTTP route.
**Goal:** knock out the small, mostly no-decision gaps and the doc rot.

| Item | Why | Approach | Size | Gate |
|---|---|---|---|---|
| **`deploy.sh` rollback** (D8) | A failed health check leaves the broken container live | Port `deploy.yml`'s prev-image-restore into `deploy.sh` failure branches | M | — |
| **`companies.account_type` index** | Actively filtered (CRM), zero DB index | btree index via migration + register in `Company.__table_args__` (drift gate) | S | migration |
| **Buy Plans hub nav slot** (D4) | Hub built + routed but reachable only via Approvals | Add the chosen destination to `mobile_nav.html`, remove the `:52` marker, fix stale `APP_MAP_ARCHITECTURE.md:425` | S | **UI approval** |
| **Relocate stray CRM pipeline chip** | `pipeline_summary` sits in the daily hub, violating the Phase-5b plan | Move it off the daily hub (pairs with D2); interim even if reporting page deferred | S | UI approval |
| **`enrichment_credit_usage` decision** (D5) | Decision never routed to you | Record "keep" in the drift-gate comment + backlog; **no drop** | S | — |
| **Purge stale CLAUDE.md config keys** | `AZURE_REDIRECT_URI`/`MICROSOFT_GRAPH_ENDPOINT`/`SMTP_FROM` don't exist → config footgun | Doc-only: remove the three keys from `CLAUDE.md` + `.claude/agents/devops-engineer.md:109`; note callback derives from `APP_URL` | S | — |
| **`DEPENDABOT_LOCKFILE_TOKEN`** (carried) | Absent → Dependabot pip PRs stay stale-red until manual re-run | Needs a fine-grained PAT (contents:write). **Your input** to mint it; then `gh secret set` | S | — |

**Exit:** each shipped as a focused PR; UI items only after your nod on D4.

---

## Phase 3 — Concurrency & performance triage (D1) (M–L)
**Goal:** the app currently serializes under load (1 worker + blocking sync ORM in 767 async handlers). Its batch-siblings (#696–#700) shipped; this was skipped and never triaged.

- **3a (recommended, immediate):** raise uvicorn `--workers` to 2–4, size the SQLAlchemy pool against Postgres `max_connections`, load-test a concurrent burst on staging. Size **M**.
- **3b (follow-up):** convert the hottest DB-bound async handlers to `def` (FastAPI runs them in the threadpool, freeing the event loop). Targeted, low-risk. Size **M**.
- **Deferred (XL, gated):** full async-SQLAlchemy migration — only if 3a/3b prove insufficient.

**Exit:** a measured concurrency improvement on staging + a recorded fix/defer decision for the async migration.

---

## Phase 4 — Requested feature builds (decision-gated) (L each)
**Goal:** the features you asked for that fell through the cracks. Each starts with its decision.

| Feature | Decision | Build | Migration / UI |
|---|---|---|---|
| **CRM honest-reporting page** | D2 (build) | Re-add the 3 rollups (`pipeline_by_account/_by_owner/conversion_funnel`, recover from `d2b0e0cd`) + a Reporting route/template/nav (NOT the daily hub) | UI approval |
| **Vendor-facing QP share link** | D6 (confirm redacted fields) | Recover redaction whitelist (`1b0c13a0`); revocable `share_token` migration + public no-login `/qp/share/<token>` route (pattern: `/p/confirm/{token}`) + redacted template | migration + UI approval |
| **Near-real-time calendar** | D3 (delta first) | Execute the calendar-delta redesign proposal (reconciling full scan; also fixes the latent `delta_query` max_items truncation — partly landed in #738) + its 3 product calls; drop 8x8 poll 30→5 min | — |

**Exit:** one feature per sub-phase, each with tests + APP_MAP updates + your UI sign-off.

---

## Phase 5 — Resell module rework (XL — its own plan)
**Goal:** execute `2026-07-15-resell-module-action-plan.md` (33 verified findings, ~19–21 eng-days across its own 6 phases). Phase-0 hotfix already pulled into **Phase 0** above.

- **Blocked on 6 product decisions (D-Resell-1…6):** posting time-box, offer-count privacy, accepted-bid revision, posted-list corrections, lifecycle vocabulary, offer expiry. I'll walk these one at a time when we start.
- Highest-risk items (lifecycle-guard corruption, outreach truthfulness) retire in that plan's first ~7 days.

**Exit:** per the Resell plan's own phase exits; each phase deployable, each migration claimed.

---

## Phase 6 — Long-tail debt & module-rework programs (XL, sequenced last)
**Goal:** the large, lower-urgency, mostly-gated work.

| Program | Approach | Gate |
|---|---|---|
| **SQLAlchemy 1.x→2.0** (D7) | Land the lint guard now (fail on NEW `db.query`, allowlist existing); migrate in waves (start: requisitions router) | your go-ahead for the mass waves |
| **mypy `no_strict_optional` burn-down** (36 errors, worst: search_service 20) | Clear per-module, drop the override, in small PRs | — |
| **assertion-theater baseline burn-down** (941 left) | Continue the tranche pattern (strengthen densest files) alongside other test work | — |
| **Queued module programs** | Prospecting → Tasks → CRM aesthetics → trouble-ticket form: each gets its read-only deep-review → prioritized plan → build (Resell pattern) | plan-first, per-program |
| **Workspace grouping/collapse theme** | Design pass → build | scope with you |
| **Tailwind v4 upgrade** (backlog row Q) | Gated upgrade; verify built CSS post-deploy + safelist | — |

---

## Cross-cutting (not phase-bound)
- **Claude GitHub App completion** — blocked on your `claude setup-token` output (`sk-ant-oat01-…`); then I set the repo secret + add `claude.yml`. Do anytime.
- **Migration coordination** — Phases 1/2/4 each add migrations; next free number is **190** (189 is #739). Claim in `MIGRATION_NUMBERS_IN_FLIGHT.txt` per migration.
- **Per-item shipping** — everything lands as focused, individually-revertable PRs behind CI; UI-gated items wait on your nod; ops-secret items batch into coordinated restart windows.

---

## Suggested cadence
1. **Now:** merge #737–#739, deploy, Resell live-500 hotfix (Phase 0).
2. **This week:** Phase 1 security sprint (highest ROI) + the Phase 2 quick wins that need no decision.
3. **Then:** Phase 3 concurrency (D1), Phase 4 features as you green-light D2/D3/D6.
4. **Rolling:** Phase 5 Resell (once its 6 decisions are made), Phase 6 debt in the background between feature work.
