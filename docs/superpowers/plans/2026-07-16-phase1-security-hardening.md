# Phase 1 — Security & Launch Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task (fresh subagent per task + two-stage review). Steps use checkbox (`- [ ]`) syntax. Verbatim current-code, edit blocks, and test patterns for every task live in the companion **`2026-07-16-phase1-security-grounding.md`** (same dir) — read the matching section before starting a task.

**Goal:** Close the verified "reads-as-done-but-isn't" security cluster before multi-user go-live — no unauth API-doc surface, a live global rate limit, no silent admin re-promotion, webhook edge allowlist, password-login fail-boot guard, and the three ops secrets set.

**Architecture:** Small, independently-revertable changes. App-code items (Tasks 1–3, 5) ship as focused PRs with pytest coverage + post-deploy live-verification. Edge-config items (Task 4, and the Caddy half of Task 1) are Caddyfile changes verified live (no unit test). Ops-secret items (Task 6) are a coordinated `.env` + container-recreate window, gated on two image/tooling fixes.

**Tech Stack:** FastAPI + slowapi + Caddy v2 + Alembic + PostgreSQL + pytest (in-memory SQLite; `TESTING=1`).

## Global Constraints
- Every migration claims the next free number in `MIGRATION_NUMBERS_IN_FLIGHT.txt` **in the same commit**; current single head after #739 is `189_category_residue_backfill` — **re-verify with `alembic heads` at write time**. Next free number = **190**.
- Settings are frozen at import (`settings = Settings()`, `config.py:437`); anything that must react to a per-process env toggle reads `os.getenv` at runtime, **not** `settings.*`.
- Live-verify security behavior on staging after deploy (401/403/404/429), not just unit tests — evidence before "done".
- `pre-commit run mypy` runs in a pinned-stub hook env that can diverge from CI; keep touched files clean in both.
- No band-aids; match surrounding idiom; UI-affecting changes need approval (none in this phase).

---

### Task 1: Lock down `/docs`, `/redoc`, `/openapi.json` (edge + app)

**Files:**
- Modify: `Caddyfile:36` (extend `@blocked` matcher)
- Modify: `app/config.py:~55` (add `expose_api_docs: bool = False`)
- Modify: `app/main.py:238-244` (gate `docs_url`/`redoc_url`/`openapi_url` on the setting)
- Modify: `tests/test_contract.py:24-33,52-60` (use `app.openapi()` instead of HTTP-fetching `/openapi.json`)
- Test: `tests/test_security_headers.py` (new: all three paths → 404 by default)

**Interfaces:** Produces `settings.expose_api_docs` (default `False`). Consumed only by the `main.py` constructor.

- [ ] **Step 1 — failing test:** in `tests/test_security_headers.py`, add the parametrized `test_api_docs_disabled_by_default(client, path)` (paths `/docs`, `/redoc`, `/openapi.json`) asserting `resp.status_code == 404` (grounding §1 test_file block, verbatim).
- [ ] **Step 2 — run, expect FAIL:** `cd /root/availai && TESTING=1 .venv/bin/python -m pytest tests/test_security_headers.py -k api_docs_disabled -q` → fails (routes currently 200/registered).
- [ ] **Step 3 — implement:** add `expose_api_docs: bool = False` to `config.py` (grounding EDIT 2); add the three gated kwargs to `FastAPI(...)` in `main.py` (grounding EDIT 3).
- [ ] **Step 4 — run, expect PASS:** same pytest command → passes (openapi_url=None ⇒ 404; verified on fastapi 0.139.0).
- [ ] **Step 5 — fix contract tests:** switch `tests/test_contract.py` to `schema = app.openapi()` / `schemathesis.from_dict(app.openapi())` (grounding EDIT 4a/4b). Run `pytest tests/test_contract.py -q` (skips locally — schemathesis is CI-only — but must be import-clean).
- [ ] **Step 6 — Caddy edge block:** `@blocked path /metrics /docs /redoc /openapi.json` (grounding EDIT 1). No unit test; live-verified in Step 8.
- [ ] **Step 7 — commit** (`config.py`, `main.py`, `Caddyfile`, `tests/test_contract.py`, `tests/test_security_headers.py`).
- [ ] **Step 8 — post-deploy live-verify:** through the public edge, `curl -s -o /dev/null -w '%{http_code}'` on all three paths → `403` (Caddy) — and in-container without proxy → `404` (app). Both layers confirmed.

### Task 2: Register the global rate limiter (`SlowAPIMiddleware`)

**Files:**
- Modify: `app/main.py:246-254` (add `SlowAPIMiddleware`); `app/main.py` SSE routes → `@limiter.exempt`
- Modify: `app/config.py:56` (raise `rate_limit_default` for htmx-heavy sessions)
- Test: `tests/test_main.py` (new `TestRateLimitMiddleware` after `TestRateLimitHandler`)

**Grounding correction (do NOT re-solve a solved problem):** the deployed stack runs `uvicorn --proxy-headers --forwarded-allow-ips 172.28.0.0/24`, so `get_remote_address` **already** returns the real client IP — **no custom key_func**. `@limiter.limit`-decorated routes and static mounts are **auto-exempt**. The real work: add the middleware, raise the default to a value that won't throttle a legit htmx-heavy admin session, and **exempt the SSE endpoints** (functional — a rate-limited stream is killed).

- [ ] **Step 1 — failing test:** `TestRateLimitMiddleware` — with a temporarily low limit, N+1 rapid requests to a plain route → the last is `429`; an exempt SSE path is never `429` (mirror `TestRateLimitHandler`, grounding §2 test_file).
- [ ] **Step 2 — run, expect FAIL** (no middleware registered → never 429).
- [ ] **Step 3 — implement:** in the `if settings.rate_limit_enabled:` block, `from slowapi.middleware import SlowAPIMiddleware; app.add_middleware(SlowAPIMiddleware)`; add `@limiter.exempt` to `/api/events/stream` (`events.py:23`) and `/v2/partials/search/stream` (`search_views.py`), and `/health`/`/metrics` per grounding; raise `rate_limit_default` in `config.py`.
- [ ] **Step 4 — run, expect PASS.**
- [ ] **Step 5 — commit.**
- [ ] **Step 6 — post-deploy live-verify:** a tight loop past the limit from one IP → `429`; confirm normal htmx browsing + an SSE stream are unaffected.

### Task 3: Stop silent admin re-promotion (migration + opt-out flag)

**Files:**
- Create: `alembic/versions/190_admin_bootstrap_optout.py` (**190**, not 189 — 189 is now `category_residue_backfill`; `down_revision` = current head, re-verify `alembic heads`)
- Modify: `app/models/auth.py` (User: `admin_bootstrap_opted_out` Boolean, default false); `app/routers/auth.py:154-157` (gate re-promote); `app/routers/admin/users.py:253-288` (set the flag on demotion)
- Modify: `MIGRATION_NUMBERS_IN_FLIGHT.txt` (claim 190, same commit)
- Test: `tests/test_routers_auth.py` (re-login regression) + migration test + drift-gate test

- [ ] **Step 1 — failing test:** seed an `ADMIN_EMAILS` user, demote via `change_user_role`, simulate re-login, assert role stays non-admin (grounding §3 test pattern).
- [ ] **Step 2 — run, expect FAIL** (current code re-promotes).
- [ ] **Step 3 — model + migration:** add the column (grounding migration_notes: `Boolean, NOT NULL, server_default text('false')` — PG-safe, no rewrite lock); create `190_...` chained onto the current head; claim 190 in the coordination file.
- [ ] **Step 4 — gate + demotion:** `auth.py:155` skips re-promote when `admin_bootstrap_opted_out`; `users.py` sets it true on admin→non-admin demotion.
- [ ] **Step 5 — run tests + `alembic heads` (single head) + round-trip on a THROWAWAY pg16** (never staging).
- [ ] **Step 6 — commit; Step 7 — post-deploy: demote a test admin, re-login, confirm they stay demoted.**

### Task 4: Graph-webhook edge IP allowlist (HIGH-SEC-4)

**Files:** Modify `Caddyfile` (+ mirror `Caddyfile.example`) — new named-matcher blocks for `/api/webhooks/graph|teams` gated by a Microsoft ServiceTag IP snapshot. **No pytest** (edge config).

- [ ] **Step 1 — placement:** insert the webhook `handle` blocks **before** the `@api` handle (grounding §4: sibling `handle` blocks are mutually exclusive, source-ordered; negation `not remote_ip <allowed>` = block-all-except).
- [ ] **Step 2 — scope:** cover graph + teams; **exclude acs** (already fails closed on `?secret=`, sender is Azure Event Grid).
- [ ] **Step 3 — commit both Caddyfiles.**
- [ ] **Step 4 — post-deploy live-verify:** request each webhook path from a non-Microsoft IP → `403`; from an allowed range (or a documented test) → falls through to the app.

### Task 5: Password-login fail-boot guard (launch blocker #1)

**Files:** Modify `app/startup.py:119-126` (before the TESTING short-circuit at 128) + a helper after `app/routers/auth.py:244`; new setting. Test: `tests/test_auth_password_guard.py` (new `TestStartupPasswordFailBoot`).

- [ ] **Step 1 — failing test:** boot (non-TESTING) with `ENABLE_PASSWORD_LOGIN=true` and no `ALLOW_PASSWORD_LOGIN_RISK` → `RuntimeError`; with the ack env → boots. Read `os.getenv` at runtime (grounding §5 import-time gotcha); restore `os.environ['TESTING']='1'` in a `finally`.
- [ ] **Step 2 — run, expect FAIL.**
- [ ] **Step 3 — implement** the guard before the TESTING short-circuit; keep the auth import function-local.
- [ ] **Step 4 — run, expect PASS; Step 5 — add a `deploy.sh` preflight assertion; Step 6 — commit.**
- [ ] **Step 7 — post-deploy:** staging sets the ack env, so it must still boot — confirm health 200.

### Task 6: Three ops secrets — coordinated window (blockers first)

**Not code** (mostly). Grounding §6 found two **blockers** that must land first:
- [ ] **6a — gpg missing from db-backup image:** `BACKUP_GPG_PASSPHRASE` is inert until `gpg` is added to the db-backup image (verified absent). Add it (Dockerfile/compose) + a `tests/test_backup.py` guard, then set the passphrase; verify a backup encrypts and a restore round-trips.
- [ ] **6b — salt-rotation canary gap:** `rotate_encryption_salt.py` rotates only the `users` table, not the `system_config` canary → rotating `ENCRYPTION_SALT` on populated data can strand boot. Extend the rotation (or document the exact safe procedure) before setting it.
- [ ] **6c — REDIS_PASSWORD:** compose interpolation (`${REDIS_PASSWORD:+...}`) resolved at container-create; `deploy.sh` recreates only app+worker, **not redis** — must `docker compose up -d --force-recreate redis` in the window.
- [ ] **6d — window:** set all three in `.env`, recreate the affected containers (app, worker, redis, db-backup), verify each took effect (read-only checks), health 200.

---

## Sequencing & gating
- **Independent PRs, this order:** Task 1 (docs) → Task 2 (rate limit) → Task 5 (password guard) → Task 3 (admin optout, migration). Task 4 (webhook Caddy) can land with Task 1's Caddy change or separately. Task 6 is an ops window after 6a/6b land.
- **One deploy per PR-batch;** live-verify each item's behavior before calling it done.
- Tasks 1 & 4 both edit `Caddyfile` — if built in parallel, merge serially and re-verify.

## Self-review notes
- Spec coverage: all 6 Phase-1 roadmap items mapped to Tasks 1–6. ✅
- Migration number corrected 189→**190** (189 taken by #739). ✅
- Rate-limiter proxy-IP concern retired by grounding (uvicorn `--proxy-headers` already set); scope narrowed to middleware + SSE exemptions + default bump. ✅
- Docs `test_contract` breakage handled via `app.openapi()`. ✅
- Ops-secret blockers (gpg image, rotation canary, redis recreate) surfaced as explicit sub-tasks. ✅
