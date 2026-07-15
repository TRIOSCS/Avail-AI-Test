# Code Audit & Hardening Plan — July 2026

**Source:** Full-codebase review by a 7-agent fleet (architecture, security, database,
frontend, tests, conventions/typing, infra/ops) across ~161k lines of Python,
333 templates, 233 migrations, and 1,055 test files. The five highest-stakes findings
were independently re-verified against the code before inclusion.

**How to use this doc:** work top-down. Each phase is a set of independent PR-sized
work items. Check items off in this doc in the same PR that fixes them.

---

## Phase 0 — Verified live bugs (fix immediately, ~1 PR each)

These are confirmed defects shipping today, not style issues.

- [x] **P0.1 — Unawaited async coroutines: all 4 "Refresh AI insights" buttons are no-ops.**
  `app/routers/htmx_views.py:1644, 1676, 1708, 1738` call `generate_insights(db, id)` /
  `generate_vendor_insights(...)` etc. without `await` — the coroutine is created and
  discarded; the surrounding `try/except` never fires; the endpoint re-renders stale
  cache. Fix: `await` all four. Add regression tests asserting the service is awaited
  (e.g. `AsyncMock` + `assert_awaited_once`).

- [x] **P0.2 — Duplicate-column migration diverges from sibling guard convention.**
  `alembic/versions/5c6736d6381f_add_screenshot_path_ai_summary_root_.py:34-42`
  re-adds `offers.excess_line_item_id` + FK already created by its ancestor
  `d1a2b3c4e5f6_add_excess_phase4_columns.py` (which carries its own `_column_exists`
  guard). **Honesty correction:** the originally claimed fresh-DB `DuplicateColumn`
  crash could NOT actually fire — `alembic/env.py:26-52` wraps `add_column` /
  `create_foreign_key` (and 8 other ops) in global idempotent no-op-when-present
  wrappers, so a chain replay would have skipped the duplicate ops with a WARN log.
  What the fix actually does: adds the same in-migration `_column_exists` guard the
  sibling uses, so the migration is self-contained and correct on its own terms
  instead of silently relying on the env.py safety net. Verified with
  upgrade → downgrade → upgrade on a throwaway Postgres.

- [x] **P0.3 — Whole-page wipe on quote detail scroll.**
  `app/templates/htmx/partials/quotes/detail.html:370` — pricing-history lazy-load
  `div` has `hx-trigger="revealed"` but no `hx-target`, so it inherits
  `hx-target="this"` from `#main-content` (`htmx/base.html:50`) and replaces the whole
  page. Every sibling lazy-load carries a "LANDMINE" guard comment; this one was missed.
  Fix: add `hx-target="this"`. Add a dead-ends E2E case.

- [x] **P0.4 — Dangling `asyncio.create_task`: SSE events can be GC'd mid-flight.**
  `app/email_service.py:1605`, `app/services/prepayment_notifications.py:122` —
  task results unreferenced; `sighting-updated` events silently lost. Fix: hold refs
  (`_bg_tasks.add(task); task.add_done_callback(_bg_tasks.discard)`).

- [x] **P0.5 — The mypy CI gate is hollow.**
  Two configs coexist: `mypy.ini` (bare run → 566 errors in 158 files) vs
  `pyproject.toml [tool.mypy]` with `ignore_errors = true` for `app.routers.*`,
  `app.services.*`, `app.connectors.*`, `app.jobs.*`, `app.schemas.*` — which is what
  pre-commit/CI uses (`--config-file=pyproject.toml`) → "0 errors". This is what let
  P0.1 ship (mypy's `unused-coroutine` catches it). Fix: unify into `pyproject.toml`,
  delete `mypy.ini`, remove `ignore_errors` at least for `app.routers.*`, enable
  `check_untyped_defs = true`, burn down errors module-group by module-group
  (273 `no-any-return`, 81 `attr-defined`, 73 `union-attr`, 46 `arg-type`, …).
  **Fixed:** `mypy.ini` deleted; `pyproject.toml [tool.mypy]` is the single config.
  `ignore_errors` is gone everywhere; every module is now checked with
  `check_untyped_defs = true`, and `unused-coroutine` + `unused-awaitable` are enforced
  across the whole tree (verified: deleting an `await` in `app/routers/htmx_views.py` or
  discarding a `safe_background_task` Task fails the gate). Enabling the gate immediately
  caught two real bug families: (1) `app/services/attachment_parser.py` called
  `claude_structured(tool_schema=...)` — a stale kwarg + tool-definition wrapper that
  raised `TypeError` on every call and silently disabled AI column detection (fixed,
  regression test added); (2) 17 fire-and-forget `await safe_background_task(...)` sites
  discarded their Task while the helper held no strong ref — the P0.4 GC bug at scale
  (fixed once inside `app/utils/async_helpers.py`; call sites now `_ = await ...`).
  Fully re-enabled, zero suppressions: `app.schemas.*`, `app.connectors.*`, `app.cache.*`,
  `app.enrichment_service`, `app.scoring`, `app.utils.llm_router`, `app.utils.claude_client`.
  **Honest remaining debt** (per-module `disable_error_code` lists in `pyproject.toml`,
  all rooted in untyped `Column(...)` declarative models — migrate to `Mapped[]` to burn
  down): `app.routers.*` ~1,380 (arg-type 638, assignment 377, union-attr 232, …);
  `app.services.*` ~1,370 (assignment 540, arg-type 482, attr-defined 101, …);
  `app.search_service` ~100; `app.email_service` ~50; `app.jobs.*` ~24; `app.startup` ~10;
  `app.utils.vendor_helpers` ~7. `app.models.*` and `app.management.*` are FULLY checked
  with no package override (matching the pre-pyproject CI gate): models were fixed at
  the root (`UTCDateTime` is now `TypeDecorator[datetime]`; no-op `(timezone=True)` args
  that defeated `Column[...]` inference were dropped) plus targeted line-level ignores
  on instrumented-attribute writes; management keeps only two per-module overrides
  (`seed_sample_data` ~42, `reconcile_decoded_facets` ~27). Modules under the router/
  service globs that were verified clean (`tags`, `avatars`, `error_reports`,
  `category_normalizer`, `buyplan_naming`, `enrichment_credit_guard`, `spec_tiers`) are
  carved back out to full checking via trailing empty-disable overrides.
  `warn_return_any` is enabled globally (2026-07-15): the ~98 `no-any-return` errors it
  surfaced (plus 9 more only visible in the leaner pre-commit hook env) were cleared —
  precise return/param annotations, generics for the parameterized helpers
  (`QueueManager`, seed `get_or_create`, contact promotion), typed locals at legacy
  Column/relationship reads, and documented `cast()`s at genuine third-party JSON
  boundaries. `no_strict_optional` remains the last global debt flag (36 errors
  measured 2026-07-15; see the debt comment in `pyproject.toml`).
  Dangerous codes stay live everywhere: `unused-coroutine`,
  `unused-awaitable`, `call-arg`, `no-redef`, `name-defined`, `unused-ignore`, `return`.
  Note: since P2.9 the pre-commit hook env runs the same mypy 2.1.0 with the key typed
  deps pinned in `additional_dependencies`, so the two envs agree almost everywhere;
  the rare suppression that is still cross-env-sensitive (a dep NOT in the hook env,
  e.g. slowapi in `app/main.py`) uses `# type: ignore[code, unused-ignore]` so both
  stay green.

- [x] **P0.6 — `"complete"` vs `"completed"` status-string landmine.**
  `app/services/prospect_scheduler.py:260` sets `batch.status = "complete"` while
  `PendingBatchStatus.COMPLETED` is `"completed"`. Audit consumers of
  `DiscoveryBatch.status`, pick one value, add a `DiscoveryBatchStatus` StrEnum (see P2.5).
  Fixed: added `DiscoveryBatchStatus` (`running`/`completed`/`failed`) to
  `app/constants.py`; the only reader (`get_next_discovery_slice`'s "already ran this
  month" filter, same file) and the only writers now agree on `COMPLETED`. Caveat: any
  pre-existing production rows persisted with the old `"complete"` literal will no
  longer match the filter — no data migration was written for this (out of scope here);
  ops should check `SELECT count(*) FROM discovery_batches WHERE status = 'complete'`
  before relying on rotation continuity.

---

## Phase 1 — Security & data-safety hardening (~1 week)

- [x] **P1.1 — Authenticate the ACS webhook.**
  `app/routers/v13_features/activity.py:139-185` accepts unauthenticated
  `CallCompleted`/`CallDisconnected` events (Graph/Teams webhooks validate HMAC
  `clientState`; ACS doesn't). Forged events pollute CRM call-activity timelines.
  Fix: shared-secret query param minted at Event Grid subscription time, compared with
  `hmac.compare_digest`, mirroring `webhook_service.validate_notifications`.
  **Fixed:** added `settings.acs_webhook_secret` (`app/config.py`, `.env.example`);
  `POST /api/webhooks/acs` now requires `?secret=` to match it via
  `hmac.compare_digest` before doing anything else — including the Event Grid
  `SubscriptionValidationEvent` handshake, so a forged handshake can't slip through
  either. Unset/empty secret fails closed (403 on every event, even if
  `ACS_CONNECTION_STRING` is set) with a startup warning in `app/main.py` lifespan
  if that misconfiguration is detected. The auto-built default callback URL used by
  `POST /api/calls/initiate` (`activity.py:224-234`) now also carries `?secret=` so
  ACS's mid-call event delivery isn't broken by the new check. Tests:
  `tests/test_activity_router_coverage2.py` (`TestAcsWebhook`,
  `TestAcsWebhookDirect`, `TestInitiateCall`), `tests/test_main.py`
  (`TestLifespanAcsWebhookSecretWarning`).

- [x] **P1.2 — Scrub request bodies in Sentry `before_send`.**
  `app/main.py:72-106` scrubs headers/query-string/locals but never
  `event["request"]["data"]`. An unhandled exception in
  `PUT /api/sources/{name}/credentials` (`sources.py:753`) or `POST /auth/login`
  (plaintext `password` form field) ships raw secrets to Sentry SaaS. Fix: recursively
  mask keys matching `_SENSITIVE_VARS` in `request.data`, or set
  `max_request_body_size="never"`.
  **Fixed:** added `_scrub_nested_body()` (`app/main.py`), called from
  `_sentry_before_send` — recursively redacts any dict key matching the existing
  `_SENSITIVE_VARS` substring/case-insensitive match through nested dicts/lists;
  a raw (unparsed) string body is wholesale-filtered only when the request URL
  matches a known-sensitive path (`/auth/login`, `/credentials`), otherwise left
  as-is for debugging value (did not set `max_request_body_size="never"` — no case
  found that scrubbing couldn't cover). Tests extended in `tests/test_main.py`
  (`TestLifespanSentry`): nested dict, list-of-dicts, string body on/off a
  sensitive path, and no-`data`-key passthrough.

- [x] **P1.3 — Rollback safety in deploy workflow.**
  `.github/workflows/deploy.yml:124-132` — on failed health check it unconditionally
  restores the pre-deploy dump, silently discarding all writes made during the deploy
  window, and recreates *all* services. Fix: take a pre-rollback safety dump first
  (pattern already exists in `scripts/restore.sh:174-214`); scope
  `docker compose up -d` to `app enrichment-worker`.
  **Fixed:** added a `pg_dump`-based pre-rollback safety dump
  (`avail_prerollback_<ts>.sql`, mirroring `restore.sh`'s dump-before-destructive-op
  pattern) before the restore runs, scoped the rollback's `docker compose up -d` to
  `app enrichment-worker` (was unscoped — risked recreating db/redis/caddy if the
  bad commit's `git reset --hard` touched their compose config), and echo a loud
  NOTICE naming the safety-dump path after restore. The restore itself stays
  unconditional (whether migrations ran during the failed deploy isn't observable
  from this script) — the safety dump is what makes that acceptable.

- [x] **P1.4 — Container hardening.**
  - Add `USER appuser` as final `Dockerfile` directive (currently root-by-default for
    any `exec`/entrypoint override; `docker-entrypoint.sh` `runuser` stays for the
    migration step).
  - Move `gh` CLI (unpinned!) + Chromium/Playwright stack (`Dockerfile:33-49`) out of
    the internet-facing prod image into a separate tooling image.
  - Redis auth: `--requirepass ${REDIS_PASSWORD}` in `docker-compose.yml:49-70` +
    update `REDIS_URL`.
  - `stop_grace_period: 60s` for `enrichment-worker` (`docker-compose.yml:112-142`) —
    its graceful-shutdown handler (`worker.py:46`) is currently killed at 10s mid-batch.
  **Fixed:** did NOT add a blanket `USER appuser` — verified against
  `docker-entrypoint.sh` first: Docker's `USER` covers the whole ENTRYPOINT+CMD
  process tree, and the entrypoint needs root for the TT-0002 uploads chown and
  for `runuser -u appuser` (which escalates DOWN from root and cannot run from a
  non-root PID 1) — adding it would break the migration step outright, which is
  explicitly out of bounds. Documented the reconciliation at length in the
  Dockerfile; the app process itself already runs as `appuser` via the existing
  `exec runuser`, the one remaining gap (`docker compose exec` defaulting to root)
  is flagged as a real follow-up (root-capable init container) rather than
  band-aided. Extracted `gh` + Chromium into `Dockerfile.tooling` (verified via
  grep: no runtime consumer in `app/` — the patchright workers run as host
  systemd units against system Chrome, not in either container); `gh` is now
  pinned to an exact release via its GitHub Releases `.deb` asset. Added
  `${REDIS_PASSWORD:+--requirepass ...}` to the redis command + a
  password-aware healthcheck, and composed `REDIS_URL` in `docker-compose.yml`
  for `app`/`enrichment-worker` so it can never drift from the redis service's
  own auth config; empty `REDIS_PASSWORD` = no auth (local dev unaffected).
  Added `stop_grace_period: 60s` to `enrichment-worker`.

- [x] **P1.5 — Backup verification on a schedule.**
  Nothing ever runs `scripts/restore.sh --verify`; a corrupt backup could sit unnoticed
  for the full 30-day retention. Add a weekly systemd timer (or Actions cron over SSH)
  that verifies the newest dump and alerts on failure.
  **Fixed:** added `scripts/verify-backup.sh` (resolves the newest backup via the
  `LATEST` marker inside `db-backup`, runs `restore.sh --verify`, exits nonzero
  with a clear message on failure) plus `scripts/systemd/avail-backup-verify.{service,timer}`
  (weekly, Sun 04:00); install one-liner is in the script's header and in
  `docs/APP_MAP_ARCHITECTURE.md`'s Scripts table.

- [x] **P1.6 — Rename `scripts/deploy.sh` → `scripts/bootstrap-server.sh`.**
  Two files named `deploy.sh` with wildly different behavior (one runs
  `docker compose down`); an operator mixup takes prod down.
  **Fixed:** copied to `scripts/bootstrap-server.sh` (filesystem copy, not
  `git mv`), deleted `scripts/deploy.sh`, updated its self-referential header
  (curl URL, usage line) and added an explicit "this is NOT the deploy script"
  banner pointing at the real `./deploy.sh`. No other docs/README/scripts
  referenced the old path except `docs/audit/2026-07-02-production-polish-review.md`
  (a dated historical audit record of the old script's own bugs — left
  untouched so the historical evidence quotes stay accurate to what was
  reviewed at the time).

---

## Phase 2 — Guardrails so classes of bugs can't recur (~1 week)

- [x] **P2.1 — Extend ruff config** (`ruff.toml`, currently only E/F/W/I), in order:
  `ASYNC` (48 real hits, 0 false positives), `RUF006` (dangling tasks → P0.4),
  `T20` (with `"app/management/*" = ["T201"]` per-file-ignore),
  `B904` (121 missing `raise … from` — mechanical sweep first),
  `RUF100` (113 dead `# noqa` — `--fix` sweep first),
  `BLE001` (with per-file-ignores for the supplier fan-out orchestrators where broad
  catch is deliberate), then `UP` after a one-time `--fix` (733 `UP017`, 38 `UP032`).
  **Fixed (2026-07-09):** `select` is now
  `["E","F","W","I","UP","ASYNC","B904","BLE001","T20","RUF006","RUF100"]`. Per family:
  - `RUF100`: 276 unused-noqa removed. **Caveat for reruns:** RUF100 must be evaluated
    under the FULL configured select — a bare `ruff check --select RUF100 --fix` treats
    every noqa for an unselected rule (F401 re-exports, E712, E402…) as "unused" and
    deletes it; recovery was `ruff check --add-noqa` under the real select, which also
    upgraded the surviving directives to exactly-coded form (304 re-added).
  - `UP`: 3,746 auto-fixed in app/tests/scripts (3,629 `UP017`, 40 `UP032`, 37 `UP041`,
    13 `UP037`, 11 `UP045`, 8 `UP035`, 6 `UP012`, 1 `UP034`, 1 `UP043`) plus 390 more via
    the all-files pre-commit pass (alembic/ migrations + e2e — semantically neutral
    `timezone.utc → UTC` etc.). Follow-on cleanup the sweep exposed: 802 `F401` (dead
    `timezone`/`asyncio` imports), 741 `I001` re-sorts, 32 `F811`. The 2 `UP047` sites
    (`app/services/desc_extractor/_common.py`, `tests/test_buyplan_workflow.py`) keep
    `TypeVar` under `# noqa: UP047` — PEP 695 syntax needs py3.12+ and dev/test envs
    still run 3.11 (repo has zero PEP 695 usage today; drop the noqas when dev moves).
  - `B904`: 121 fixed — all `raise … from e` (never `from None`; every site benefits
    from the causal chain), adding `as e` bindings where the handler had none.
  - `ASYNC`: 48 hits = 33 `ASYNC109` + 15 real blocking-I/O. `ASYNC109` is globally
    ignored with a comment (ruff calls it "highly opinionated"; every hit forwards
    `timeout=` to httpx/Graph — nobody hand-rolls timeout logic). The 8 app/ blockers
    were fixed P2.6-style (`asyncio.to_thread`): `routers/avatars.py` +
    `routers/error_reports.py` (new `_resolve_avatar_file`/`_resolve_screenshot_file`
    sync helpers, one thread hop per request), `services/tagging_ai_batch.py`
    (`_latest_backfill_meta_path` glob/stat helper + offloaded `open()` of the
    streamed-download temp file), `services/tagging_ai_triage.py` (same `open()`
    offload). tests/* and scripts/* get `ASYNC230` per-file-ignores (blocking I/O off
    the server event loop is harmless).
  - `RUF006`: 0 hits (clean post-P0.4), enabled.
  - `T20`: 85 `T201`, all in `app/management/*` + `scripts/*` (both per-file-ignored —
    stdout is a CLI's UX); 0 hits elsewhere.
  - `BLE001`: 529 hits measured. Globs for the deliberate broad-catch layers
    (`app/jobs/*`, `scripts/*`, `tests/*`) plus a **frozen per-file legacy list** of 158
    app files in `ruff.toml` — per-file (not per-directory) so every NEW file gets full
    enforcement; delete a file's line when its catches are narrowed. A worst-pattern
    scan (`except Exception:` + bare `pass`) found exactly one site
    (`app/cache/redis_probe.py` metrics guard) — deliberate and commented; no
    genuinely-wrong catches to fix.
  - `B` wholesale: **deferred** — with `lint.flake8-bugbear.extend-immutable-calls`
    set to the FastAPI param factories, 149 violations remain (90 `B008` — mostly
    `routers/resell.py` + `htmx/materials.py`; 23 `B905`, 21 `B007`, 8 `B017`, 7 misc),
    over the ≤30-edit budget; `B904` is selected individually instead.
  - **CI landing note:** the UP sweep content-touched ~530 test files, so the
    changed-files assertion-theater gate (`ci.yml`, P2.4) will surface the
    pre-existing P6.1 offenders in those files on this PR (violations are 100%
    pre-existing; the hand-edited test files lint clean). Landing this needs either a
    one-time gate accommodation or the P6.1 backfill — do NOT "fix" it by
    allowlisting 1,000 tests.

- [x] **P2.2 — Build the Docker image in CI.**
  `.github/workflows/ci.yml` never runs `docker build`; the first-ever build of a
  release image is the production deploy (`deploy.yml:96`). Add
  `docker compose build app` as a required PR gate.
  **Fixed:** added a `docker-build` job to `.github/workflows/ci.yml` (same
  workflow, so a failed build fails the run) that builds both prod
  Dockerfiles with `docker/build-push-action@v6` over `docker/setup-buildx-action@v3`:
  the main `Dockerfile` (app/enrichment-worker) with `--build-arg
  BUILD_COMMIT=<short-sha>-<unix-ts>` mirroring `deploy.sh`'s own cache-bust
  arg, and `Dockerfile.tooling` (P1.4's split-out gh+Chromium image, which
  has no docker-compose consumer and would otherwise silently rot). Both use
  GHA-hosted layer caching (`cache-from`/`cache-to: type=gha`, scoped
  per-Dockerfile so one build's cache can't evict the other's) to keep the
  job fast on repeat runs; neither build pushes/tags anywhere.

- [x] **P2.3 — Shard the CI `test` job.**
  The 22.7k-test suite needs ~24 min on a 2-vCPU runner; the timeout has been
  re-tuned three times as the suite grew (15 → 25 → 40, `ci.yml:32`) and will be
  outgrown again. Split into a 2-3 way matrix (`pytest-split` or directory
  buckets) so wall-clock drops to ~10 min and the timeout stops being a moving
  target. Pairs with P6.1 — retiring assertion-theater tests shrinks the
  runtime this sharding has to carry.
  **Fixed:** converted `test` into a `strategy.matrix.shard: [0, 1]` job
  (`timeout-minutes` cut 40 → 25 per shard). Added `scripts/ci_shard.py`
  (stdlib-only, no new dependency): lists `tests/test_*.py` sorted, excludes
  the same paths `pytest.ini`'s `addopts` ignores (`e2e/`,
  `test_browser_e2e.py`, `.claude`), and emits `sorted(files)[shard::total]`
  — a fully deterministic round-robin, verified balanced to 520/520 files
  (diff 0) with `union(shard0, shard1) == full set` and empty intersection.
  `pytest.ini`'s `-n auto` xdist still applies within each shard's file
  subset. One-time gates (pre-commit, ruff, assertion-theater lint, raw-DDL
  grep, all three Alembic steps, frontend build/tests, the Redis integration
  test) run only on `matrix.shard == 0` so they aren't paid for twice.
  Coverage: each shard writes a shard-scoped data file
  (`COVERAGE_FILE=.coverage.${{ matrix.shard }}`, `--cov-report=` — no
  report, since a partial shard's line coverage is meaningless in isolation)
  and uploads it as `coverage-data-${{ matrix.shard }}`; junit XML is
  likewise `test-results-${{ matrix.shard }}.xml` under an
  `test-results-${{ matrix.shard }}` artifact — none of these clobber. A new
  `coverage-report` job (`needs: test`) downloads both data files, `coverage
  combine`s them, and enforces the original `--cov-fail-under=85` gate once
  against the merged, whole-suite result. `postgres-paths` and
  `migration-full-cycle` are unchanged. Validated: `python -c "yaml.safe_load"`
  on `ci.yml`, `bash -n` on every `run:` block, and a standalone assertion
  script confirming the shard split (see above).

- [x] **P2.4 — CI lint against assertion-theater tests.**
  Simple AST check flagging any test whose only assertion is a bare
  `status_code == 200` / `is not None`. (See P5.1 for the backfill.)
  **Fixed:** added `scripts/lint_assertion_theater.py` (stdlib `ast` only —
  no new dependency). Flags any `test_*` function (module-level or a class
  method, sync or async) whose ONLY `assert` is `<expr>.status_code == 200`
  (either operand order) or `<expr> is not None`; supports a
  `# assertion-theater: allow` escape hatch as a trailing comment on the
  `def` line or anywhere in the docstring. Wired into `.github/workflows/ci.yml`
  as an `if: matrix.shard == 0` step scoped to test files CHANGED vs the PR
  base — the same `git merge-base`/`$GITHUB_BASE_REF` pattern the pre-commit
  step already uses — so the ~542 pre-existing offenders never block an
  unrelated PR; only a newly touched test file must pass. Verified: run
  against `tests/test_coverage_nightly_2026_06_30.py` → flags 18 violations,
  exit 1; run against `tests/test_spec_tiers.py` → 0 violations, exit 0.
  `ruff check` passes on the new script.

- [ ] **P2.5 — StrEnum enforcement (partial).**
  - Add `SearchQueueStatus` (`queued/searching/completed/gated_out/pending` written raw
    in `search_worker_base/ai_gate.py:195-255`, `queue_manager.py:219-346`) and
    `DiscoveryBatchStatus` to `app/constants.py`; migrate call sites.
  - Enforce the existing-but-unused `OfferCondition` enum — 10+ sites hardcode `"new"`
    (`htmx/offers.py:221,282`, `htmx/requisitions.py:673`, `sightings.py:1394,2980,3027,3057`,
    `ai_offer_service.py:320`, `offer_qualification.py:133,196,235`, `schemas/crm.py:201`).
    Add a validator so raw strings can't slip past.
  - Pre-commit grep hook rejecting `\.status\s*=\s*"` outside `app/constants.py`.
  **Partially fixed (2026-07-09):** Added `SearchQueueStatus` StrEnum
  (`app/constants.py`; `PENDING/QUEUED/SEARCHING/COMPLETED/GATED_OUT/FAILED`, values
  unchanged from the raw literals — audited every write/compare site first via grep
  across `search_worker_base/{ai_gate,queue_manager}.py` and the `nc_worker`/
  `ics_worker`/`tbf_worker` `worker.py` consumers, including the `"failed"` writes in
  the three `worker.py` main loops that the original audit line-range missed). Migrated
  all of those call sites; `DiscoveryBatchStatus` already existed from P0.6. Enforced
  `OfferCondition` at every listed site EXCEPT `htmx/requisitions.py:673` and
  `sightings.py` (owned by the parallel P3 agent this wave): `htmx/offers.py:221,282`,
  `ai_offer_service.py:320`, `offer_qualification.py:133,196,235` (comparisons — logic
  unchanged, StrEnum compares equal to the raw string), `schemas/crm.py:201` (default
  value only, field stays typed `str` — verified `model_dump()`/`model_dump_json()`
  still serialize `"new"`, not an enum repr). Deliberately did NOT touch
  `freeform_parser_service.py:166`'s `"new"` literal — that's the separate broad
  new/refurb/used vocab (`normalize_condition`), not `OfferCondition`; conflating the
  two is exactly what `MaterialCondition`'s docstring warns against.
  **Leftovers closed (2026-07-09, follow-up pass):** `Offer._validate_condition`
  (`app/models/offers.py`) now normalizes via the existing
  `app.services.offer_qualification.normalize_offer_condition()` — case-insensitive,
  maps documented legacy spellings (`used`→`pulls`, `pull`/`pulled`→`pulls`,
  `refurbished`/`recertified`→`refurb`, `new no pkg`→`new_no_pkg`, etc. — the same
  `_LEGACY_CONDITION` table `normalize_offer_condition` already used elsewhere) onto
  the live `OfferCondition` members, so raw strings can't silently diverge from the
  enum again. Data-safety preserved: a value matching no known/legacy spelling is
  passed through UNCHANGED with a logged warning rather than raised — verified against
  every `Offer(condition=...)` test fixture in the suite (244 call sites audited via
  grep; the handful of title-case/off-vocab literals found all belonged to
  `ExcessLineItem`/`MaterialCard`/`Sighting`/`VendorPartUnavailability`, not `Offer`).
  Pre-commit hook added: `.pre-commit-config.yaml`'s new local `no-raw-status-assignment`
  (`language: pygrep`) rejects `\.status\s*=\s*"` under `app/` excluding
  `app/constants.py`. Ran the regex over `app/` first (per the assignment): exactly one
  hit, `app/services/requisition_state.py:4`'s module docstring documenting the very
  anti-pattern the hook replaces (`` `req.status = "..."` ``) — not a live assignment,
  so that file is excluded too (comment in the YAML explains why + how it was
  verified) rather than migrated. Verified the hook both passes clean on current `app/`
  and fails on a probe file with a real `req.status = "open"` line. `sightings.py`/
  `htmx/requisitions.py:673` OfferCondition sites remain the parallel P3 agent's
  responsibility (unrelated to this pass). Tests:
  `tests/test_search_queue_status_enum.py` (new — full queue lifecycle + AI-gate
  transitions assert against the enum), `tests/test_offer_qualification.py`
  (`TestOfferConditionEnumSites`), `tests/test_schemas_crm.py`,
  `tests/test_ai_offer_service.py`, `tests/test_save_parsed_offers_normalize_qual.py`,
  `tests/test_offer_condition_validator.py` (new — canonical/case-variant/legacy
  normalization + unknown-value pass-through-with-warning).

- [x] **P2.6 — Event-loop protection.**
  - Move the 14 blocking file-I/O sites in `async def` to `anyio` (worst:
    `tagging_ai_batch.py:128-458`, `tagging_ai_triage.py:233-246` — large JSONL files;
    also fixes the 2 unclosed file handles at `tagging_ai_batch.py:437`,
    `tagging_ai_triage.py:228`).
  - `htmx/requisitions.py:554` — run `openpyxl.load_workbook` in
    `anyio.to_thread.run_sync`; add an upload size cap.

  **Fixed (2026-07-09):** Used `asyncio.to_thread` rather than `anyio.to_thread.run_sync`
  — `anyio` is importable (FastAPI/Starlette transitive dep) but every existing
  event-loop-offload site in this codebase (`app/main.py`, `app/search_service.py`,
  `app/jobs/tagging_jobs.py`, `app/routers/sightings.py`, etc.) already uses
  `asyncio.to_thread`; introducing `anyio` in just these files would mix two patterns
  for the same job (CLAUDE.md: "follow existing codebase patterns").
  - `app/services/tagging_ai_batch.py`: extracted `_write_batch_meta`/`_read_batch_meta`
    (JSON meta persistence, `submit_batch_backfill`/`check_and_apply_batch_results`) and
    `_process_batch_results_file` (the full JSONL parse + `_apply_chunked_batch` DB pass
    in `apply_batch_results_chunked`, previously inline) into sync helpers, each
    dispatched via `asyncio.to_thread`; the streamed-download chunk write now goes
    through `asyncio.to_thread(f.write, chunk)` per chunk instead of blocking inline.
    The `tempfile.NamedTemporaryFile(...).close()` unclosed-handle pattern (line 437)
    is now a `with` block.
  - `app/services/tagging_ai_triage.py`: same treatment for `apply_triage_results` —
    extracted `_process_triage_results_file`, per-chunk `asyncio.to_thread(f.write, ...)`
    download, `with`-block temp file (line 228). Preserved the function's original
    no-reraise-on-exception behavior (unlike `tagging_ai_batch.py`, which re-raises).
  - `app/routers/avatars.py:126` (avatar write) and
    `app/routers/error_reports.py`'s `_save_screenshot` call site (the plan's
    `error_reports.py:342-344` line ref had drifted post-refactor to the actual
    blocking-write call at the time of this pass — `os.path.isfile`/`os.path.realpath`
    at the now-current 342-344 are cheap stat/string ops already followed by
    Starlette's own async-safe `FileResponse`, not the flagged large-file pattern):
    both now dispatch their synchronous disk write via `asyncio.to_thread`.
  - `app/routers/htmx/requisitions.py`'s `requisition_import_parse`: `openpyxl` parse
    extracted to a sync `_parse_xlsx_rows` helper run via `asyncio.to_thread`; added
    `MAX_IMPORT_UPLOAD_BYTES` (10MB, matching `resell.py`'s `MAX_UPLOAD_BYTES` /
    `requisitions/requirements.py`'s inline 10MB checks) with the standard
    `{"error", "status_code", "request_id"}` JSON shape in `format=json` mode and a
    matching HTML fragment (413) otherwise.
  Tests: `tests/test_tagging_ai_batch_event_loop.py` (new — direct coverage for the two
  previously `# pragma: no cover` functions plus the meta helpers),
  `tests/test_tagging_ai_batch.py` / `_coverage.py` / `tests/test_tagging_ai_triage.py`
  (all still green, unchanged behavior), `tests/test_avatar_upload_route.py` (added the
  OSError-on-worker-thread propagation case), `tests/test_error_reports_submit.py`
  (added the screenshot-write-failure case; strengthened 3 pre-existing
  assertion-theater-flagged tests in the same file per the lint gate),
  `tests/test_req_import_xlsx_upload.py` (new — `_parse_xlsx_rows` unit tests, xlsx
  upload end-to-end, oversized-upload rejection in both JSON/HTML modes, normal-size
  upload unaffected).

- [x] **P2.7 — Startup/health-check decoupling.**
  `app/main.py:124` runs ~20 sequential backfills/`ANALYZE` before `/health` can
  answer; on a prod-sized DB this can exceed both the compose healthcheck (~80s) and
  `deploy.sh`'s ~60s loop → false-failed deploys. Fix: split liveness (immediate) from
  readiness; add partial indexes on backfill `IS NULL` predicates; gate
  `_analyze_hot_tables` (`startup.py:1002-1005`) behind a since-last-deploy marker.

  **Fixed:** `run_startup_migrations()` (`app/startup.py`) now runs ONLY the FAST,
  order-critical ops synchronously pre-yield; the SLOW ops moved to a new
  `run_deferred_startup_backfills()`, launched by `app/main.py`'s lifespan as a
  post-yield background task via `asyncio.to_thread` + `safe_background_task` (never
  awaited inline). Per-op classification (full table in `run_startup_migrations`'s
  docstring):
  - **FAST (pre-yield, unchanged timing):** `_create_fts_triggers`,
    `_seed_system_config`, `_reconcile_system_config`, `_seed_manufacturers`,
    `_create_count_triggers`, `_reconcile_connector_active` (no-op),
    `_verify_encryption_canary`, `_create_default_user_if_env_set`,
    `_seed_admin_user_if_env_set`, `_seed_agent_user`,
    `_seed_verification_group_from_admin_emails`, `_seed_commodity_schemas` — all
    either DDL-only (CREATE OR REPLACE FUNCTION/TRIGGER, no data scan), single-row
    checks, or bounded by a small fixed catalog/env list.
  - **SLOW (deferred, post-yield background task):** `_backfill_fts`,
    `_seed_site_contacts`, `_backfill_company_counts`, the legacy `site_type`/
    `trouble_tickets` normalize UPDATEs, `_analyze_hot_tables` (now gated by
    `_maybe_analyze_hot_tables`), `_backfill_normalized_mpn`,
    `_backfill_sighting_offer_normalized_mpn`, `_backfill_sighting_vendor_normalized`,
    `_backfill_offer_vendor_normalized`, `_backfill_proactive_offer_qty`,
    `_backfill_ticket_defaults`, `_backfill_material_cards`,
    `_backfill_sweep_cooldown`, `_complete_reverted_active_plans`,
    `_warn_non_canonical_categories` — all full-table-shaped scans, chunked
    backfills, ANALYZE, or per-row business-logic sweeps.

  Added `GET /health/ready` (module flag `app.startup.deferred_backfills_ready`,
  read via `is_deferred_backfills_ready()`) reporting whether the deferred phase has
  finished; `/health` itself is unchanged (liveness only) and now answers immediately
  since it no longer waits on the deferred phase. `docker-compose.yml`'s healthcheck
  and `deploy.sh`'s wait loop deliberately stay pointed at `/health` (liveness) —
  `deploy.sh` additionally curls `/health/ready` once, post-liveness, purely to log
  readiness (never gates on it), documented inline in both files.

  New Alembic migration `187_startup_backfill_partial_idx` (merged with a concurrent
  `71d3fef96529` via `1223a56cbbbb`) adds 8 PostgreSQL partial indexes on the exact
  `IS NULL` predicates the deferred backfills scan (`requirements`/`material_cards`/
  `sightings`/`offers.normalized_mpn`, `sightings`/`offers.vendor_name_normalized`,
  `trouble_tickets` risk_tier+category, `prospect_accounts` sweep-cooldown) so
  repeat-boot scans are O(remaining rows), not O(table); no-op on SQLite
  (dialect-guarded). `_maybe_analyze_hot_tables` gates ANALYZE behind a
  `system_config` marker keyed to `BUILD_COMMIT` (same tag `/health` reports and
  `deploy.sh` verifies) — reruns once per genuine deploy, skips on a same-image
  restart, reruns if the marker row is cleared. Round-tripped
  upgrade→downgrade→upgrade on a throwaway local PostgreSQL 16 DB. `TESTING=1`
  behavior is unchanged: `run_startup_migrations` and
  `run_deferred_startup_backfills` both short-circuit under `TESTING=1`, and
  `main.py` only ever schedules the deferred task when `not _is_testing` (mirroring
  the existing scheduler/seed_api_sources gating). Tests:
  `tests/test_startup.py` (fast/deferred split, readiness flag, ANALYZE marker
  gating), `tests/test_main.py` (`/health/ready` endpoint, lifespan wiring, TESTING
  behavior unchanged).

- [x] **P2.8 — Insight-refresh latency hazard (P0.1 follow-up; needs a design
  decision).** Now that the four "Refresh AI insights" endpoints actually `await`
  `generate_*_insights(...)`, the HTMX request blocks for the full generation time:
  worst case ~96s (Claude call timeout 30s × 3 retries, plus extended-thinking
  budget) with the browser spinner held open and the app worker occupied the whole
  time. The root fix is background generation — kick the job off, return a polling
  partial (`hx-trigger="every 2s"` against a status endpoint) that swaps in the
  result when ready — or, as a cheaper stopgap, a tightened per-call timeout with a
  visible "generation timed out, retry" state. Which of the two (and the acceptable
  per-call budget) needs an explicit design decision before implementation; do not
  band-aid it inline in the routers.
  **Fixed (2026-07-09, stopgap — cheaper option chosen; no UI change):** rather than
  full background generation (a UI-affecting design change out of bounds for this
  pass), tightened the per-call Claude budget for the four interactive endpoints only.
  Added optional `timeout_seconds`-equivalent params (`timeout` already existed;
  added `max_attempts: int = 3`) all the way through
  `claude_structured` → `claude_structured_with_usage` (`app/utils/claude_client.py`)
  — defaults preserve the existing 30s/3-attempt behavior for every other caller.
  `knowledge_service._regenerate_insights` gained `interactive: bool = False`; when
  `True` it passes `timeout=25, max_attempts=1` to `claude_structured` (worst case
  ~25s instead of ~96s). `generate_insights` / `generate_vendor_insights` /
  `generate_company_insights` / `generate_pipeline_insights` forward the flag; the
  four `htmx_views.py` refresh endpoints now call with `interactive=True`.
  `generate_mpn_insights` (no HTMX caller) and the `knowledge_jobs._job_refresh_insights`
  background job are untouched — still get the original 30s/3-attempt budget.
  Verified the empty-list fallback path: on timeout/failure `_regenerate_insights`
  returns `[]` (existing behavior, unchanged), and the router's
  `entries or get_cached_*_insights(...)` serves the stale cached insights instead of
  an error. Tests: `tests/test_claude_client.py` (`max_attempts` default-unchanged /
  no-retry-at-1 / forwarded-to-`http.post`), `tests/test_knowledge_service_coverage.py`
  (interactive path forwards `timeout=25`/`max_attempts=1`; non-interactive path omits
  both kwargs entirely), `tests/test_insights_refresh.py` (all four endpoints assert
  `interactive=True` was forwarded via `AsyncMock` introspection).

- [x] **P2.9 — Pre-commit mypy hook env diverges from the real gate.**
  The pre-commit hook runs mypy 1.15.0 in an isolated env with NO project
  dependencies installed, while CI/dev runs full-deps mypy 2.1.0 — the two disagree
  on which errors exist, and 22 `# type: ignore[code, unused-ignore]` suffixes exist
  in the tree *solely* to keep both environments green. Root fix: make the hook run
  the same checker as CI — either pin `additional_dependencies` in
  `.pre-commit-config.yaml` to the project's mypy + type-stub set, or convert the
  hook to `language: system` so it uses the repo venv's mypy. Then sweep the 22
  `, unused-ignore` suffixes (they become genuinely unused and `warn_unused_ignores`
  will flag them).
  **Fixed:** bumped the mirrors-mypy hook `rev` v1.15.0 → v2.1.0 (matches the
  `mypy==2.1.0` pin in `requirements-dev.txt`; mirrors-mypy tags every PyPI mypy
  release as `v<version>`) and pinned the key typed runtime deps into the hook's
  `additional_dependencies` at the `requirements.txt` versions:
  `sqlalchemy==2.0.51`, `pydantic==2.13.4`, `pydantic-settings==2.14.2`,
  `fastapi==0.138.1` (plus the pre-existing `types-requests`). Bump these together
  with the requirements lockfiles. Hook-env replica for local verification (build
  fresh whenever `additional_dependencies` changes):
  `python3 -m venv hookenv && hookenv/bin/pip install mypy==2.1.0 types-requests
  sqlalchemy==2.0.51 pydantic==2.13.4 pydantic-settings==2.14.2 fastapi==0.138.1`,
  then run the hook's exact command from the repo root:
  `hookenv/bin/mypy --ignore-missing-imports --no-strict-optional
  --config-file=pyproject.toml app/` — exit 0 verified, identical to the
  full-deps run. Swept the (by-now 61) `, unused-ignore` suffixes: 60 removed —
  5 of those ignores (`call-arg` on pydantic `extra="allow"` class kwargs in
  `app/schemas/tags.py`, `app/schemas/knowledge.py`, `app/schemas/v13_features.py`)
  were unused in BOTH envs under mypy 2.1.0 and were deleted outright; the other 55
  kept their base `# type: ignore[code]` (still needed in both envs). Exactly 1
  suffix remains (`app/main.py:247`) because slowapi is not in the hook env, so its
  `arg-type` ignore is genuinely unused there but required in the full env.

---

## Phase 3 — Performance (~1 week)

- [x] **P3.1 — Index `requirements.assigned_buyer_id`** (`models/sourcing.py:163`, no
  index anywhere). Filtered on every buyer's default sightings board
  (`sightings.py:413,585`) and the offers alert source. One migration + `__table_args__`.
  **Fixed:** added `Index("ix_requirements_assigned_buyer", "assigned_buyer_id")` to
  `Requirement.__table_args__` (`models/sourcing.py`) and hand-wrote
  `alembic/versions/71d3fef96529_index_requirements_assigned_buyer_id.py` (autogenerate
  against the dev DB also picked up ~15 unrelated pre-existing drift ops — stripped so
  the migration carries only the new index). Verified upgrade → downgrade → upgrade on a
  throwaway Postgres 16 cluster; single head confirmed via `alembic heads` (a second
  concurrent PR's P2.7 migration produced a merge revision
  `1223a56cbbbb_merge_p2_7_partial_indexes_and_p3_1_.py` reconciling the two branch
  heads). `tests/test_alembic.py` passes.

- [x] **P3.2 — Batch the CSV contact-import lookups.**
  `htmx/companies.py:1025-1050` does up to ~2,000 sequential queries per 1,000-row
  import. Pre-fetch `CustomerSite` rows and `(site_id, email)` pairs in two queries,
  mirroring the batched pattern already used at `companies.py:837-840`.
  **Fixed:** `import_contacts_confirm` (`htmx/companies.py`) now resolves each row's
  matched company up front (same normalized-name/domain lookup, hoisted out of the
  write loop), then pre-fetches every matched company's first ACTIVE site
  (`company_id IN (...)`, ordered `company_id, id` — same ordering as the per-row
  `.order_by(CustomerSite.id).first()` it replaces) and every existing
  `(customer_site_id, email)` pair for those sites in one query each. Newly created
  sites and newly created contacts are cached/added back into the same in-memory
  dict/set as the loop runs, so within-batch site reuse and within-batch email dedup
  (both real behaviors of the original per-row/autoflush code) are preserved exactly —
  including the pre-existing case-sensitivity quirk (dedup compares the incoming
  lowercased email against the stored value as-is, never lowering the stored side).
  Extended `tests/test_crm_bulk_import.py` with
  `test_import_contacts_confirm_multi_row_batched_lookups` (7-row, 3-company batch
  covering site reuse, in-batch dup, unauthorized, and no-match skips — asserts exact
  created/skipped counts and site assignment).

- [x] **P3.3 — Bulk `require_requisition_access`.**
  6 batch endpoints in `sightings.py` (1163, 1228, 1280, 1346, 2492, 2641) call it
  per-item in loops (up to 50 sequential `db.get()` for SALES/TRADER users). Add
  `require_requisition_access_bulk()` (single `IN (...)` select), reuse the documented
  `_manageable_company_ids` pattern.
  **Fixed:** added `require_requisition_access_bulk(db, req_ids, user, *, label=...)` to
  `app/dependencies.py` — one `Requisition.id IN (...)` select resolving every
  `created_by` in a single round trip (no-op for unrestricted roles; raises the same
  `HTTPException(404)` as the single-item version for any missing/non-owned id; dedups
  `None`s and repeats). Swapped all 6 loop call sites in `app/routers/sightings.py`
  (`sightings_batch_search`/`batch_assign`/`batch_status`/`batch_notes`/
  `preview_inquiry`/`send_inquiry`, current lines 1164/1228/1279/1344/2489/2637) to one
  bulk call each. New `tests/test_requisition_access_bulk.py`: unit tests for the
  dependency (buyer no-op, sales owner passes, sales/trader non-owner 404s, missing id
  404s, empty/`None`/duplicate-id inputs) plus router-level multi-row-basket tests for
  all 6 endpoints (owner passes, non-owner 404s).

- [x] **P3.4 — (Opportunistic) batch phone-match `db.get()` chains** in
  `activity_service.py:244-320` if ever used for bulk reconciliation; bounded and fine
  today.
  **Fixed:** batched the 3 remaining per-row `db.get()` chains in
  `match_phone_to_entity` (`app/services/activity_service.py`) — priority-1
  (`SiteContact` → `CustomerSite` → `Company`), priority-3 (`CustomerSite` → `Company`),
  and priority-4 (`VendorContact` → `VendorCard`) each now do one `IN (...)` select per
  level instead of one `db.get()` per matched row, with plain dict lookups replacing the
  `.get()` calls in the existing loops. Match-priority order and the `seen`-based
  ambiguity/dedup logic are untouched. Extended `tests/test_unified_phone_matcher.py`
  with 3 cases exercising the batched dicts across shared and distinct
  companies/vendor-cards.

---

## Phase 4 — Structural refactors (staged, ~3-4 weeks)

Do these after Phases 0-2 so the new guardrails protect the refactor.

- [x] **P4.1 — Fix inverted layering (services importing router privates).**
  `buyer_affinity_service.py:153`, `quote_builder_service.py:216,276`,
  `health_monitor.py:150` lazily import `_private` helpers from routers. Move the
  helpers into services (`vendor_reachability.py`, `pricing_history.py`,
  `connector_registry.py`); both sides import the service. Also fixes the two
  cross-router imports (`htmx/offers.py:59`, `htmx/archive.py:45-46`).
  **Fixed:** new `app/services/vendor_reachability.py` (`cards_with_resolvable_email`,
  `dnc_emails_for_cards`, moved verbatim from `routers/sightings.py`) —
  `buyer_affinity_service._reachable_card_ids` now imports them at module scope;
  `sightings.py` re-imports both under their original private names (its own many
  call sites + the existing test suite patch/import them off that module unmodified).
  New `app/services/pricing_history.py` (`PRICED_STATUSES`, `quote_date_iso`,
  `preload_last_quoted_prices`, moved from `routers/crm/_helpers.py`) —
  `quote_builder_service.py` imports `preload_last_quoted_prices` at module scope
  (dropped the two lazy `from app.routers.crm._helpers import` calls);
  `routers/crm/_helpers.py` re-imports all three under their original private names
  (`crm/quotes.py`, `crm/offers.py`, `crm/__init__.py` keep importing from `._helpers`
  unmodified). New `app/services/connector_registry.py` (`get_connector_for_source`,
  `source_has_test_path`, the 7 keyless Test-connector classes, moved from
  `routers/sources.py`) — `health_monitor.py` imports `get_connector_for_source` at
  module scope (dropped its lazy `from ..routers.sources import` call);
  `routers/sources.py` re-imports `get_connector_for_source` under its original
  private name for its own Test-button call site (the Test-connector classes and
  `source_has_test_path` are NOT re-exported there — `htmx/settings.py`'s one lazy
  import and the handful of test files that referenced them off `routers.sources`
  now point at `services.connector_registry` directly). Cross-router fix: new
  `app/routers/htmx/_shared_tabs.py` holds `requisition_tab` / `company_tab` /
  `vendor_tab` (moved verbatim from `requisitions.py` / `companies.py` / `vendors.py`
  — genuinely HTTP-shaped route handlers, not data-assembly helpers, so they stay in
  `routers/htmx` rather than `app/services/` per the plan's own escape hatch); each
  owning router now does `xxx_tab = router.get(path, ...)(imported_impl)` (same
  route/URL/tag/importable name as before) instead of defining the body inline;
  `offers.py`, `archive.py`, and `htmx_views.py` import all three from
  `_shared_tabs` instead of reaching into the sibling router modules. `company_tab` /
  `vendor_tab` lazily import the couple of names genuinely local to their owning
  router (`companies.py`'s `FIELD_LABELS`/`CANONICAL_ROLES`/`_company_quotes_query`/
  `_company_buy_plans_query`, `vendors.py`'s `vendor_reviews`) to avoid a load-time
  import cycle — same established lazy service↔router reuse pattern used elsewhere.
  Also promoted `companies.py`'s `_manageable_company_ids` to
  `app.dependencies.manageable_company_ids` (batched sibling of the existing
  `can_manage_account`/`is_manager_or_admin` there) so the new
  `company_import_service.py` (P4.2) has a legitimate non-router import path;
  `companies.py` re-imports it under its original private name. Updated ~15 test
  files' patch targets/imports that referenced the old private names/router-internal
  patch paths (`app.routers.sources._get_connector_for_source` internal callers, etc.)
  to point at the new service modules; every test suite for the touched surfaces
  (sightings, buyer affinity, sources/health, quotes/quote-builder, requisitions/
  companies/vendors/offers/archive/htmx_views) passes unmodified in behavior.
  `docs/APP_MAP_INTERACTIONS.md`'s one stale `routers/sources.source_has_test_path`
  reference updated to `services.connector_registry.source_has_test_path`.

- [x] **P4.2 — Extract business logic from routers (quick, self-contained).**
  - CSV import (~450 lines): `companies.py:620-1089` → `services/company_import_service.py`.
  - Offer ingestion: `offers.py:190-301` → consolidate into existing
    `services/ai_offer_service.py`.
  **Fixed:** new `app/services/company_import_service.py` — `parse_csv_rows`,
  `preview_company_import`, `confirm_company_import`, `preview_contact_import`,
  `confirm_contact_import` (CSV decode/parse, dedup queries, authz-scoped row
  creation, moved verbatim including the P3.2 batched-lookup confirm path, which is
  untouched). `routers/htmx/companies.py`'s four routes
  (`import_companies_preview/confirm`, `import_contacts_preview/confirm`) are now
  thin: `await request.form()` / file read, call the service, map a `ValueError` (row
  cap) to `HTTPException(400)`, render the template / build the `HX-Trigger` toast.
  `_manageable_company_ids` (needed by both preview functions) promoted to
  `app.dependencies.manageable_company_ids` (see P4.1) since it's genuine
  authz-scoping logic, not import-specific. `ai_offer_service.py` gained
  `parse_offer_form_rows` + `save_form_parsed_offers` — the HTMX
  form-review-then-save sibling of the existing JSON-API `save_parsed_offers`
  (different behavior: EXACT mpn match not fuzzy, VendorCard resolve/create,
  qualification scoring, straight to ACTIVE not PENDING_REVIEW — kept as a distinct
  function per the plan's "two thin callers if the router flow differs", alongside
  the pre-existing near-identical `save_freeform_offers`). `routers/htmx/offers.py`'s
  `save_parsed_offers` route is now thin: parses the form, delegates, commits,
  renders. `tests/test_crm_bulk_import.py` (39 tests) and the offers/AI test suites
  (781 tests) pass UNCHANGED — the behavior-preservation proof. Added
  `tests/test_company_import_service.py` (11 tests) and extended
  `tests/test_ai_offer_service.py` (+4 tests) with direct service-level unit tests
  (happy path + one edge each) for every extracted function.

- [ ] **P4.3 — Split the god files along their audited seams** (one PR per split;
  re-export from a package `__init__.py` so callers don't all change at once):
  - `routers/htmx/companies.py` (5,234 lines) → ~8 modules: import, saved views,
    contacts, tags/segments, merge, custom fields, sites, detail-tab render.
    **Done:** split into `routers/htmx/companies/` package — `saved_views.py`
    (filter presets), `tags.py` (company + contact segment tags), `custom_fields.py`
    (WS3 label:value fields), `merge.py` (company + contact duplicate merge),
    `sites.py` (CustomerSite + site-scoped SiteContact CRUD), `contacts.py`
    (Contacts-tab CRUD, bulk actions, suggested-contacts discovery, notes/history/
    files — largest module, ~1.5k lines, since "contacts CRUD + bulk actions" was
    audited as one seam), `detail.py` (`company_detail_partial` /
    `_render_company_detail` / the `company_tab` route-registration wrapper), and
    `core.py` (list/create/typeahead/duplicate-check, tier/disposition/parent/
    primary-contact setters, deactivate/reactivate/archived, send-to-prospecting,
    AI dup/name suggestions, collaborators, edit forms + inline field editing —
    the seams not named individually in the audit). New leaf module
    `_registries.py` holds the shared field registries (`EDITABLE_ACCOUNT_FIELDS`/
    `EDITABLE_CONTACT_FIELDS`/`KNOWN_ACCOUNT_FIELDS`/`FIELD_LABELS`/
    `CANONICAL_ROLES`) and the pure field-apply helpers (`apply_company_field`/
    `apply_contact_field`/`_validate_role`/`_recompose_full_name`) — mirrors the
    existing `_shared.py`/`_shared_tabs.py` convention in `routers/htmx/`, letting
    both `core.py` and `detail.py` depend on it without a cycle. `__init__.py`
    defines the single shared `router` FIRST; every submodule does `from . import
    router` and decorates it directly (not `include_router`), so route
    registration is byte-for-byte the same mechanism as the pre-split file — this
    matters because 6 of `core.py`'s GET routes (`account-list`, `create-form`,
    `typeahead`, `check-duplicate`, `archived`) plus `saved_views.py`'s
    `saved-views` are single literal path segments under `/v2/partials/customers/`
    that MUST register before `.detail`'s `/v2/partials/customers/{company_id}`
    catch-all (FastAPI validates path-typed params post-match, so a shadowed
    catch-all returns 422, not a fall-through 404 — order is genuinely load-
    bearing, not just defensive). `__init__.py`'s import order enforces this
    (`.core` before `.detail`); `core.py` itself avoids a module-level `from
    .detail import ...` (which would trigger `.detail`'s registration as an import
    side-effect before `core.py`'s own routes) by resolving
    `_render_company_detail`/`company_detail_partial` off the package attribute
    (`_pkg._render_company_detail(...)`) at call time instead — the same
    indirection already required for `.sites.edit_site`'s call to `company_tab`
    and `.contacts.contacts_tab_suggested`'s scheduling of
    `_run_contact_discovery`, both of which tests monkeypatch via the package
    attribute (`app.routers.htmx.companies.company_tab` /
    `..._run_contact_discovery`), not the defining submodule. `__init__.py`
    re-exports every public + test-patched name (`company_detail_partial`,
    `company_tab`, `create_company`, `edit_company`, `edit_site`,
    `apply_company_field`, `apply_contact_field`, `CANONICAL_ROLES`,
    `_VALID_ROLES`, `FIELD_LABELS`, `contacts_tab_suggested`,
    `_run_contact_discovery`, `_manageable_company_ids`, `_company_quotes_query`,
    `_company_buy_plans_query`, `_staleness_tier`), so `app/main.py`'s
    registration and `_shared_tabs.py`'s lazy `from .companies import
    (CANONICAL_ROLES, FIELD_LABELS, _company_buy_plans_query,
    _company_quotes_query)` both keep working unchanged. P4.6 folded in: ~30
    function-local imports hoisted to module scope across the 9 new files
    (verified no cycles) — EXCEPT `contacts.py`'s
    `find_suggested_contacts_with_errors`, kept function-local inside
    `_run_contact_discovery` because 6 tests monkeypatch
    `app.enrichment_service.find_suggested_contacts_with_errors` by that exact
    module-attribute path, which only intercepts a fresh per-call lookup, not a
    name bound at import time. `ruff.toml`'s BLE001 legacy-freeze entry updated
    from the single `companies.py` path to the 2 new file paths
    (`core.py`, `contacts.py`) that still carry a broad `except Exception`. Full
    suite (22,916 tests) passes unmodified; the companies-surface subset (2,283
    tests across 66 files) passes with zero patch-path changes needed.
  - `services/buyplan_workflow.py` (1,855) → `buyplan_approval / buyplan_lines /
    buyplan_po / buyplan_reports`. **Done:** split into
    `services/buyplan_workflow/` package — `buyplan_approval.py` (submit/
    approve/reject, halt/resume, reset/cancel/resubmit, auto-completion — kept
    as one module since every transition shares the engine-request/prepayment
    teardown helpers), `buyplan_po.py` (PO confirm + approver verify/scan),
    `buyplan_lines.py` (claim/re-source, flag/resolve issue, add/edit/remove
    line, SO# editor), `buyplan_reports.py` (favoritism detection, case-report
    generation). `verify_po`'s completion check and `resource_line`'s
    prepayment-teardown call are lazy (function-local) imports back into
    `buyplan_approval` — the only two edges that would otherwise cycle;
    everything else is a top-level import (P4.6). `__init__.py` re-exports
    every public name + every test-patched internal. A handful of
    `unittest.mock.patch(...)`/`monkeypatch.setattr(...)` targets that
    isolate an internal collaborator (`assign_buyer`, `score_offer`,
    `settings`, `_generate_buyer_tasks`, `_cancel_open_engine_requests_for_plan`)
    were repointed from `app.services.buyplan_workflow.X` to
    `app.services.buyplan_workflow.buyplan_approval.X` so the mock still
    intercepts the call post-split (`tests/test_buyplan_workflow.py`,
    `tests/test_buy_plan_service.py`, `tests/test_c1_buyplan_gate.py`).
    `ruff.toml`'s BLE001 legacy-freeze entry updated to the 2 new file paths
    that still carry a broad `except Exception`.
  - `routers/htmx_views.py` (2,063) → `htmx/my_day.py, email_views.py,
    insights_views.py, search_views.py`. **Done:** split into 5 sibling
    modules under `routers/htmx/` — `my_day.py` (Tasks worklist + create/
    snooze/reopen), `email_views.py` (thread viewer, AI summary, reply send,
    intelligence dashboard), `insights_views.py` (AI insights panels for
    requisitions/vendors/customers/dashboard, activity digests, dashboard
    stats, knowledge-base list/create — moved verbatim, P0.1/P2.8 behavior
    untouched), `search_views.py` (global/AI search, search form + history
    panel, streaming search/run + SSE stream + filter + lead-detail,
    requisition-picker "add to requisition"), and `requisitions_edit.py`
    (bulk owner-reassign, inline cell edit/save, win-probability +
    opportunity-value, row actions, inbox-poll, requirement delete/update).
    `routers/htmx_views.py` itself shrank to the core full-page shell
    dispatcher (`v2_page`), the parts-workspace entry point, and the vendor
    stock-list upload; it imports the 5 new modules' routers and aggregates
    them via `router.include_router(...)` internally, so `app/main.py`'s
    registration (`app.include_router(htmx_views_router)`) is byte-for-byte
    unchanged — no new `main.py` mount lines. It also re-imports every name
    tests patch/import directly at `app.routers.htmx_views.X`
    (`_get_cached_search_results`, `_get_enabled_sources`, `add_to_requisition`,
    `requisition_picker`, `search_filter`, `search_run`, `send_email_reply`,
    `update_requirement`, plus `_safe_int`/`templates` re-exported from
    `_shared`). A handful of `unittest.mock.patch("app.routers.htmx_views.X")`
    targets that patched an internal collaborator actually called from
    within the moved function (`_get_cached_search_results`,
    `_get_enabled_sources`, `template_response`) were repointed to
    `app.routers.htmx.search_views.X` / `app.routers.htmx.requisitions_edit.X`
    so the mock still intercepts the call post-split
    (`tests/test_search_streaming.py`, `tests/test_htmx_views_nightly25.py`,
    `tests/test_htmx_views_nightly24.py`); patches on `get_user` (stays in
    core `v2_page`) were left untouched. `ruff.toml`'s BLE001 legacy-freeze
    entry updated: dropped `routers/htmx_views.py` (no broad catches remain
    there) and added `routers/htmx/insights_views.py` +
    `routers/htmx/search_views.py`.
  - `routers/htmx/offers.py` (1,905) → `offers_crud / rfq_compose / follow_ups /
    reply_handling`. **Done:** split into `routers/htmx/offers/` package —
    `crud.py` (AI offer parsing, offer CRUD/review/promote/reject/changelog,
    create-quote-from-offers), `rfq.py` (RFQ compose, AI cleanup/rephrase, RFQ
    send), `follow_ups.py` (queue, single/batch send, AI draft, badge),
    `replies.py` (vendor response review/reply, activity/phone-call logging).
    `__init__.py` re-exports `router` + every name tests patch/import at
    `app.routers.htmx.offers.X`; sub-modules pull `template_response` /
    `requisition_tab` / `maybe_release_on_offer` / `offer_review_queue` back via
    a function-local `from . import X` so those patches still intercept every
    call site post-split. `ruff.toml`'s BLE001 legacy-freeze entry updated to
    the 4 new file paths.

- [x] **P4.4 — Shared fuzzy-dedup helper.** `vendor_duplicates.py:51-75` and
  `company_utils.py:154-227` copy-paste the rapidfuzz fallback loop; extract
  `fuzzy_dedup_scan(rows, normalize_fn, threshold, limit)`. Done: `fuzzy_dedup_scan()`
  added to `app/vendor_utils.py` (pairwise + anchor modes, scan/filter only — sort and
  truncation stay caller-side so scoring/tie-order is byte-for-byte identical);
  `vendor_duplicates._fuzzy_match_python` and
  `company_utils._find_company_dedup_candidates_rapidfuzz` converted to call it.

- [x] **P4.5 — `spec_tiers.recategorize()` entry point** so
  `management/cleanup_known_bad.py:173` (direct `card.category` write) can go through
  the ladder like everything else. Done: `recategorize(db, card, new_category, *,
  source, confidence, force=False, reason=None)` added to `app/services/spec_tiers.py`
  — normal mode delegates to `set_category` (full ladder arbitration); `force=True`
  (the cleanup script's sole legitimate use) bypasses the tier comparison but still
  purges stale facet data via `_purge_stale_commodity_data` and never restamps
  provenance columns. Every write is audited (`MaterialCardAudit`,
  action=`category_recategorize`). `cleanup_known_bad.py`'s "normalized_in_place"
  branch now calls `recategorize(force=True)` instead of assigning `card.category`
  directly.

- [ ] **P4.6 — Hoist needless function-local imports** (~180 across the big htmx
  routers; verified no real cycles). Fold into each P4.3 split rather than standalone.

---

## Phase 5 — Frontend consolidation (~1 week)

- [x] **P5.1 — `lazy_body(id, url)` macro** so the `hx-target` guard (P0.3's root
  cause) is enforced structurally, not by "LANDMINE" comments. Migrate
  `approvals_hub.html`, `buy_plans/hub.html`, `settings/index.html`, `sightings/list.html`,
  `quotes/detail.html`, `resell/detail.html`, `resell/workspace.html`.
  **Fixed:** added `lazy_body(id=None, url, target=None, trigger='load', swap='innerHTML',
  class_=None, indicator=None, extra_attrs='')` to `shared/_macros.html` — a `{% call %}`
  macro (not a plain include) so every site keeps its own exact spinner/skeleton markup
  via `caller()` while the wrapper div's `hx-get`/`hx-trigger`/`hx-target`/`hx-swap`
  become structurally guaranteed (macro always emits `hx-target`, defaulting to `'#'+id`
  or `'this'`). All 7 listed templates migrated (approvals_hub.html, buy_plans/hub.html,
  settings/index.html, sightings/list.html, quotes/detail.html `trigger='revealed'`,
  resell/detail.html ×4 tab bodies `trigger='intersect once'`, resell/workspace.html).
  Per-site `LANDMINE` comments deleted; the rationale now lives once at the macro
  definition. Verified via `tests/test_approvals_hub_tabs.py`,
  `tests/test_buyplan_hub_routes.py`, `tests/test_sightings_router.py`,
  `tests/test_sprint5_quote_workflow.py`, `tests/test_proactive_prepare.py`,
  resell route tests (961 tests, all pass) — these assert `id="..."` / `hx-target="#..."`
  substrings only, not full markup, so the macro's harmless attribute-order/whitespace
  changes don't affect them.

- [x] **P5.2 — Kill the `fetch()` violations in `htmx_app.js`** (~16 sites).
  Convert `fetchCompanies()` (:1637) and `searchVendors()` (:2391) to server-rendered
  `hx-get` debounced dropdowns (pattern: `materials/workspace.html`); wrap the 5
  JSON-POST sites (trouble tickets, call-outcome, outreach, quote-builder save) in one
  `postJSON()` helper over `htmx.ajax`.
  **Fixed:**
  (a) `fetchCompanies()`/`customerPicker.filtered` → `GET /v2/partials/requisitions/customer-typeahead`
  (new HTML-partial endpoint in `app/routers/htmx/requisitions.py`, reusing the same
  active-Company+sites query as the untouched JSON `/api/companies/typeahead`), rendered
  by new `requisitions/_customer_typeahead_results.html`, wired via `hx-trigger="input
  changed delay:300ms, focus"` on the search input in `unified_modal.html`.
  `searchVendors()` → `GET /v2/partials/sightings/vendor-search` (new endpoint in
  `app/routers/sightings.py`, vendors-only sibling of the untouched JSON
  `/api/autocomplete/names`), rendered by new `sightings/_vendor_search_results.html`,
  wired the same way in `sightings/vendor_modal.html`. Both use single-quoted `@click`
  attributes with `|tojson` payloads per the CLAUDE.md landmine rule.
  (b)/(c) Added `postJSON(url, body)` (JSON body via the bundled `json-enc` extension,
  activated per-call via a throwaway source element so it never leaks to other htmx
  requests) and `postForm(url, values)` (form-urlencoded sibling, for the one Form()-based
  endpoint) to `htmx_app.js` — both resolve `{ok, status, json(), text}` off the real XHR
  by listening once for `htmx:afterRequest` on that throwaway element (rejects only on
  `status===0`, i.e. genuine network failure, matching `fetch()`'s reject semantics).
  Replaced fetch() at: trouble-ticket submit (`submitTroubleReport`, now swaps the
  response HTML via `htmx.swap` instead of a second manual step), bulk ticket action
  (`ticketBulkAction`), call-outcome log (`callOutcome.submit`), timezone auto-detect
  (`syncDisplayTimezone`, via `postForm` — its endpoint takes `Form(...)`, not JSON),
  quote-builder save (`saveQuote`). CSRF no longer set manually at these sites — it's
  already injected app-wide by the existing `htmx:configRequest` listener.
  **Exceptions kept on raw `fetch()` (documented in-code):** outreach log
  (`data-outreach-log` click handler) — needs `keepalive: true` (XHR/htmx.ajax has no
  equivalent) since the log POST must survive the browser navigating away for the
  tel:/mailto:/Teams handler firing in the same click, and branches on the parsed JSON
  body (`dropped_links`, activity id) that a `fetch()` `.json()` gives directly. Avatar
  upload — binary Blob/FormData multipart upload, not a JSON-shaped payload the
  `postJSON` pipeline fits. Endpoints reused/added: `/api/companies/typeahead` and
  `/api/autocomplete/names` (JSON, untouched at the time, note for cleanup if ever
  unused — **later removed** in a final-review pass once confirmed its only caller had
  fully moved to `/v2/partials/requisitions/customer-typeahead`; `/api/autocomplete/names`
  remains live) vs. new `/v2/partials/requisitions/customer-typeahead` and
  `/v2/partials/sightings/vendor-search` (HTML). Verified: `tests/frontend/*.test.ts`
  (168/168 — `rfq-vendor-modal.test.ts`'s `searchVendors` suite replaced with a
  `pickVendor` test since that state moved server-side; `trouble-screenshot.test.ts`
  updated to spy `window.postJSON`), `tests/test_unified_req_form.py`,
  `tests/test_sightings_router.py`, `tests/test_frontend_hardening.py`, `npm run build`
  + `npm run lint` clean.

- [x] **P5.3 — Empty-state dedup.** 11 templates hand-roll the markup that
  `shared/empty_state.html` already provides (vendors/list, requisitions/list,
  emails/*, follow_ups, offers/review_queue, proactive, prospecting, rfq_compose,
  search/full_results, vendors/contacts_list).
  **Fixed:** extended `shared/empty_state.html` with optional params (`message_class`,
  `message_safe`, `description_class`, `show_icon`, `icon_path`, `icon_class`,
  `icon_stroke_width`, `wrapper_class`, `action_open_modal`, `action_class`) — all
  default to the partial's pre-existing hard-coded look, so its 8 existing `{% include
  %}` callers are byte-identical. Each of the 11 sites now passes its exact prior
  classes/icon path through these params (pixel-equivalent — verified by diffing
  rendered output shape, not just `message` text). `proactive/_macros.html` had its own
  parallel `empty_state(icon_path, title, subtitle)` macro (already de-duped locally,
  supporting a `{% call %}` block for the "Check again" button) — added a matching
  call-block-friendly `macro empty_state(...)` to `shared/empty_state.html` itself (its
  defaults matched to proactive's prior look) and re-exported it from
  `proactive/_macros.html` via `{% import ... as _empty_state_shared %}{% set
  empty_state = _empty_state_shared.empty_state %}` (Jinja `{% from %}` does not chain
  re-exports) so `proactive/list.html`'s two call sites are untouched. `search/
  full_results.html`'s message embeds a bolded query term — built via `{% set %}...{%
  endset %}` capture (not `~` string concat, which re-escapes already-escaped pieces
  under autoescape) and passed with `message_safe=true`. Verified via
  `tests/test_empty_states_fixes.py` (asserts the shared partial's contract directly)
  plus the full route test sweep for all 11 owning surfaces (1026 tests, all pass).

- [x] **P5.4 — Single-quote the `tojson` attributes** in `quote_builder/modal.html:10`
  and `requisitions/rfq_compose.html:44` (latent Alpine-breakage per CLAUDE.md).
  **Fixed:** both `x-data`/`@change` attributes switched from double- to single-quoted
  delimiters; the JS string literals that were single-quoted inside `quote_builder/
  modal.html`'s `x-data` (`'{{ requirement_ids }}'`) flipped to double-quoted so they no
  longer collide with the new outer delimiter. Verified via a standalone Jinja render
  (correct output) plus the existing route test sweep for both templates (404 tests).

- [x] **P5.5 — Replace `_x_dataStack` in `tests/e2e/test_navigation_smoke.py:38`**
  with an `Alpine.store('nav')` read or `data-current-view` attribute.
  **Fixed:** `document.body`'s `currentView` (base.html) turned out to be dead state —
  set once at first paint and never updated by client-side HTMX navigation, so reading
  it would never have reflected real post-navigation state anyway. The Alpine component
  that actually owns and reactively updates current-view (`activeNav`, via
  `@htmx:pushed-into-history` and each nav link's `@click`) is `mobile_nav.html`'s own
  `<nav>` `x-data`. Added `:data-current-view="activeNav"` to that `<nav>` element (a
  public, non-visual attribute) and updated the test helper to read
  `document.querySelector('nav[aria-label="Main navigation"]').dataset.currentView`
  instead of `document.body._x_dataStack?.[0]?.currentView`. Playwright e2e is excluded
  from CI (per task) — verified with `python3 -m py_compile` only.

---

## Phase 6 — Test-suite trustworthiness (ongoing, start now)

- [ ] **P6.1 — Retrofit the 542 status-200-only tests** (14.3% of the nightly/coverage
  files; suite-wide 971/18,709). Seed matching + non-matching rows, assert rendered
  content. Start with `test_coverage_nightly_2026_06_30.py:211-277` (sourcing filters)
  and the `test_htmx_views_nightly{1..30}.py` series. Gate recurrence via P2.4.
  **Partial (2026-07-09) — explicitly a targeted start, not the full backfill:**
  `TestSourcingResultsFilters`/`TestSourcingWorkspaceFilters`
  (`tests/test_coverage_nightly_2026_06_30.py`) fully retrofitted — every one of the 13
  tests now seeds one matching `SourcingLead` + one non-matching lead per filter
  (safety band, buyer status, contactability has_phone/has_email, corroborated yes/no,
  source, plus a 3-way combined-filters case) and asserts the rendered
  `lead.vendor_name` set reflects the filter (present for the match, absent for the
  non-match), instead of a bare `status_code == 200`. Did **not** attempt the "2
  highest-density files" stretch goal (`test_htmx_views.py` — 125 violations;
  `test_htmx_views_nightly30.py` — 50) — surveyed both and confirmed the counts, but
  strengthening every entry requires tracing each endpoint's template output
  individually (seed data + rendered-content assertions per test, ~175 tests total),
  which is multi-PR-sized work, not a same-session addition on top of P6.2–P6.5.
  Deferred, not band-aided: baseline still carries those 175 as known offenders.
  Baseline delta this pass: 1130 → 1117 (−13, all from the two classes above; verified
  via `git diff` on the baseline file that ONLY those 13 keys were removed and nothing
  new was added).

- [x] **P6.2 — Close the Postgres blind spot.** 59 modules use
  ILIKE/JSONB/tsvector/pg_trgm; only 3 test files use `requires_postgres`. Priority:
  `vendor_duplicates.py` (pg_trgm ranking — currently "tested" via a full ORM-chain
  mock at `test_vendor_duplicates.py:159-188`) and `faceted_search_service.py:430-608`
  (FTS ranking — zero real coverage). Track the checklist in `docs/APP_MAP_DATABASE.md`.
  **Fixed (2026-07-09):** Re-ran the grep fresh against the current tree — 39 distinct
  files match `ILIKE|JSONB|tsvector|pg_trgm|plainto_tsquery|similarity(` (not 59; the
  audit's count was approximate/from an earlier tree state), tracked as a checklist in
  `docs/APP_MAP_DATABASE.md`'s new "PostgreSQL-Only Code Path Coverage" section.
  (a) `test_vendor_duplicates.py::TestFuzzyMatchPgTrgmDirect` rewritten as
  `@requires_postgres` tests against a real Postgres 16 (`pg_session`) with real
  `VendorCard` rows — ranking order (closer matches rank first), the 0.3 similarity
  threshold cutoff (dissimilar names never appear regardless of count), the
  anchor-vs-candidate dict shape, and the 5-result cap. The `OperationalError`/
  `ProgrammingError` fallback tests (`TestCheckVendorDuplicatePgTrgmFallback`) KEEP
  their whole-session mock — forcing a missing-extension error against a real Postgres
  would need to drop/recreate the extension per test, not worth it for a branch that
  never touches ranking.
  (b) `test_faceted_search_service.py::TestFacetedSearchFtsRealPostgres` (new) —
  `@requires_postgres` tests exercising `plainto_tsquery` AND-of-lexemes matching,
  real `ts_rank` ordering (weight-A field beats weight-C field, verified empirically
  against a real Postgres before writing the assertion), the ILIKE-on-mpn OR clause
  (isolated via a deliberately stale `search_vector`, since the normal
  trigger-computed vector would tokenize the same words and mask whether the OR
  clause contributes anything), and the single-term-only exclusion case. Since
  `pg_session`'s schema is `Base.metadata.create_all`-only (no
  `startup._create_fts_triggers`/`_backfill_fts`), each test populates
  `search_vector` with the identical weighted-field UPDATE `_backfill_fts` runs in
  production, scoped to the seeded ids.
  Verified locally against a REAL PostgreSQL 16 (not just SQLite-skip): the sandboxed
  `postgresql-16` package was already installed with a running `pg_ctlcluster`
  instance; `pg_hba.conf` temporarily set to `trust` for local root access (backed up
  first), a dedicated `availai`/`availai_pgtest` role+db created (mirroring CI's
  `postgres-paths` job exactly), `pg_trgm` extension created, and both new test
  classes run with `PG_TEST_DSN=postgresql://availai:availai@127.0.0.1:5432/availai_pgtest
  -n 0` — all pass. Also verified the `PG_TEST_DSN`-unset path: both files' new classes
  SKIP cleanly (not error) on the in-memory SQLite default.

- [x] **P6.3 — Replace whole-session `MagicMock()` tests** (11 files) with real
  `db_session` SQLite fixtures where expressible; keep mocks only for PG-only branches.
  **Fixed (2026-07-09) — per-file dispositions** (re-grepped
  `mock_session = MagicMock()\|db=MagicMock()`, 13 files matched):
  - `test_vendor_duplicates.py` — see P6.2a (converted).
  - `test_api_health.py` — CONVERTED: `run_health_checks`'s 4 success-path tests + the
    empty-sources test now patch `app.database.SessionLocal` to return the SAME real
    `db_session` (not a fresh `TestSessionLocal()` — that was tried first but doesn't
    share the in-memory DB with the autouse `db_session` fixture, because pytest's own
    conftest auto-import and this file's explicit `from tests.conftest import ...`
    resolve to two different `sys.modules` entries, each running conftest.py's
    `create_engine(...)` once — discovered and documented inline). An inactive
    `ApiSource` row is now seeded alongside the active ones in
    `test_run_health_checks_ping` to prove the real `is_active` filter is honored
    (the old mock ignored it entirely). `test_run_health_checks_db_error_rollback`
    KEPT mocked (forces the query itself to raise — a hard-failure path).
  - `test_startup.py` — CONVERTED: `TestCreateDefaultUserDefaultRole.test_default_role_is_buyer`
    was a near-exact duplicate of the already-real-session
    `TestCreateDefaultUser.test_default_role_is_buyer_when_role_unset`; repointed to the
    same `patch("app.startup.SessionLocal") + mock_sl.return_value = db_session` pattern
    already used by every sibling test in the file.
  - `test_knowledge_jobs_coverage.py` / `test_knowledge_jobs_coverage2.py` —
    CONVERTED: the req/vendor/company/MPN "id-fetching" tests (both success-path and
    generator-raises-continues) now seed real `Requisition`/`Offer`/`VendorCard`/
    `Company`/`CustomerSite` rows instead of a rotating whole-session mock that handed
    back canned `(id,)` tuples regardless of the real `updated_at`/`created_at`
    filter, join, or group-by. `TestJobExpireStale` converted to real `KnowledgeEntry`
    rows, asserting the REAL total/expired counts via a `loguru_info` sink fixture
    (loguru isn't bridged to `caplog` in this codebase). KEPT mocked: the DB-query-
    itself-raises tests in both files (`test_expire_stale_db_error_raises`,
    `test_refresh_insights_db_error_logs_and_continues`,
    `test_outer_exception_reraises_and_rollbacks`, `test_vendor_section_db_error_caught`)
    — all force a specific query call to raise mid-sequence, which a real SQLite
    session can't be coerced into cleanly.
  - `test_database_coverage.py` — KEPT (genuinely-unit test): `get_db()`'s own
    generator-lifecycle mechanics (close/rollback call verification) — `get_db` does
    no querying itself, so the mock hides no real behavior.
  - `test_main.py` — KEPT (hard-failure path): `/health`'s DB-error-returns-degraded
    test forces `session.execute` to raise; same rationale as the DB-error tests above.
  - `test_ics_worker.py`, `test_nc_worker_full.py` — NO CHANGE NEEDED (false positive):
    `mock_session` in both files names a mocked BROWSER session (`IcsSessionManager`/
    the nc worker's Selenium-ish session), not a DB session — the DB layer in both
    already goes through the real `db_session` fixture (`nc_worker`'s
    `_make_mock_db(db_session)` even uses `MagicMock(wraps=db_session)`, a spy that
    delegates to the real session while only suppressing `.close()`).
  - `test_routers_sources.py` — NO CHANGE NEEDED: `db=MagicMock()` is an inert
    placeholder arg to `_get_connector_for_source`; `credential_service.get_credential`
    (the only thing that would touch `db`) is patched out separately in every one of
    these tests, so the mock hides nothing.
  - `test_connectors.py` — NO CHANGE NEEDED: `db=MagicMock()` is a constructor
    placeholder for `EmailMiner` in tests of pure string-parsing helper methods
    (`_extract_vendor_info`) that never touch `db`.
  - `test_routers_vendors_crud.py` — NO CHANGE NEEDED (genuinely-unit test):
    `_background_enrich_vendor`'s tests mock `enrich_entity`/`apply_enrichment_to_vendor`
    (the real business logic) separately; the session mock only stands in for a
    trivial `db.get(VendorCard, id)` PK lookup + commit/close, not a filter chain.

- [x] **P6.4 — De-flake `test_circuit_breaker.py:78-82`** (50ms margin under xdist);
  inject a fake monotonic clock.
  **Fixed (2026-07-09):** `test_breaker_half_open_after_timeout` now monkeypatches
  `app.connectors.sources.time.monotonic` with a controllable fake clock advanced
  explicitly (`fake_now += 0.05` / `+= 0.06`) instead of a real `sleep(0.15)` — zero
  real sleep, fully deterministic under any xdist scheduling jitter. Strengthened
  while de-flaking: added a just-under-the-timeout boundary assertion (still `"open"`
  at +0.05s against a 0.1s `reset_timeout`) before the over-the-timeout transition to
  `"half_open"`, so the test now pins the exact boundary rather than merely
  "eventually transitions."

- [x] **P6.5 — Direct unit tests for `can_review_qp_sales_section` /
  `can_review_qp_purchasing_section`** (`dependencies.py:382-400`, zero direct tests).
  **Fixed (2026-07-09):** added `TestCanReviewQpSections` to
  `tests/test_auth_deps_unit.py` (mirrors the file's existing `TestGetUser`/
  `TestRequireAdmin`/etc. class-per-function pattern) — 9 tests covering both
  functions' full grant matrix: grant-set → True, grant-unset → False, an
  admin/buyer role WITHOUT the explicit per-user column still denied (the right is a
  per-user grant, not role-derived), the `None` user edge case for both functions, and
  cross-independence (holding the sales grant doesn't imply the purchasing grant and
  vice versa, including the both-set case). `_create_user` test helper extended with
  `**extra` kwargs to set arbitrary columns without a bespoke per-test helper.

---

## Explicitly verified clean (no action needed)

- SQL injection: none (only guarded `text()` with hardcoded identifiers + bound params)
- CSRF: double-submit middleware correctly scoped; `/metrics` fails closed
- XSS: all `|safe` piped through `nh3` or template-author literals
- SSRF: datasheet fetcher blocks private IPs per redirect hop
- Path traversal: realpath containment on all file-serving endpoints
- IDOR: ownership-scoped deps throughout; 404-not-403 on denial
- No `db.query(Model).get()`, no Pydantic `class Config`, no bare `except:`, no
  `print()` outside `app/management/`, single Alembic head, all timestamps `UTCDateTime`,
  `search_requirement()` stale-session contract honored at both call sites

## Sequencing summary

```
Week 1:  Phase 0 (all)  +  P1.1, P1.2
Week 2:  Phase 1 rest   +  P2.1, P2.2, P2.4
Week 3:  P2.3, P2.5-P2.7  +  Phase 3
Weeks 4-6: Phase 4 (one god-file split per PR)  ∥  Phase 5
Ongoing: Phase 6 (fold P6.1 retrofits into every PR touching those areas)
```

---

## Final review batch (2026-07-09) — 13 verified findings, all fixed

Closing pass over the P2.7/P5.x work above; each item below was independently
verified against current code before fixing (not taken on memory/spec alone).

**Security**
1. **Sentry query-string scrub only matched `key`.** `app/main.py`'s
   `_sentry_before_send` now checks the query string against the full
   `_SENSITIVE_VARS` name set (already includes `secret`), so `?secret=...` (the ACS
   webhook param) is masked, not just `?...key=...`. Tests: `tests/test_main.py`
   (existing Sentry scrub tests cover the qs-masking branch).

**Correctness**
2. **`/health/ready` reported `ready=true` even after a crashed deferred phase.**
   `app/startup.py` now tracks a tri-state (`app.constants.DeferredBackfillState`:
   RUNNING/COMPLETED/FAILED, module var `deferred_backfills_state`) instead of a
   bool — `run_deferred_startup_backfills` sets FAILED in an `except` branch
   (re-logged, re-raised) rather than always flipping ready in a bare `finally`.
   `GET /health/ready` (`app/main.py`) now returns `{"ready": bool, "state": str}` —
   `ready` stays backward-compatible (deploy.sh's informational curl still works),
   `state` is new. Tests: `tests/test_startup.py`, `tests/test_main.py`
   (crashed-phase → `ready=False`/`state=failed`).
3. **THE MONEY ONE — the deferred sweep silently dropped prepayment stand-down
   notifies.** `run_deferred_startup_backfills` runs via `asyncio.to_thread` (a
   worker thread with no running loop), so when it auto-completes a buy plan
   (`_complete_reverted_active_plans` → `check_completion` → `_complete_plan` →
   `_cancel_open_prepayment_requests_for_plan`), `schedule_prepayment_notify`
   (`app/services/prepayment_notifications.py`) hit `RuntimeError` on
   `get_running_loop()` and `coro.close()`'d the DO-NOT-WIRE stand-down
   notification. **Root fix (loop-handoff design):** `app/main.py`'s lifespan
   captures `asyncio.get_running_loop()` via a new
   `prepayment_notifications.set_main_event_loop()` immediately before dispatching
   the `asyncio.to_thread` task, storing it in a module-level `_main_event_loop`
   holder in `prepayment_notifications.py`. `schedule_prepayment_notify`'s
   no-running-loop branch now falls back to
   `asyncio.run_coroutine_threadsafe(wrapped_coro, main_loop)` instead of always
   closing the coroutine; `wrapped_coro` calls `hold_bg_task(asyncio.current_task())`
   from *inside* the coroutine once it actually starts running on the main loop
   (necessary because `run_coroutine_threadsafe` only returns a
   `concurrent.futures.Future`, not the underlying `asyncio.Task`, so retention
   can't be applied from the calling thread). No registered main loop (or a
   stopped one) still safely closes the coroutine — preserves the pre-fix
   behavior for bare CLI/test callers and TESTING=1 boots (which never reach the
   registration point). Tests: `tests/test_prepayment_notifications.py` (loop
   registered + called from a worker thread → notify executes and is retained via
   `hold_bg_task`; no loop registered → safe-close preserved; registered-but-
   stopped loop → safe-close), `tests/test_main.py` (lifespan registers the loop
   before dispatching the deferred task).
4. **CSV-upload `.read()` extracted outside its try, so a non-file `file` form
   field 500'd instead of rendering the friendly partial.**
   `app/routers/htmx/companies/core.py` and `contacts.py`'s import-preview routes
   now wrap the `.read()`/`.file.read()` extraction in the same `try` that already
   handled `parse_csv_rows`'s `None` return, catching `AttributeError` (a bare
   string form value has no `.read()`/`.file`) and rendering the same "Could not
   parse CSV" partial. Tests: `tests/test_crm_bulk_import.py` (non-file `file`
   field on both the company and contact import-preview routes → 200 + friendly
   partial, not 500).
5. **Offer condition `<select>` missing canonical values, comparing against
   retired legacy strings.** `edit_offer_form.html`'s condition select now lists
   all four `OfferCondition` values (new/new_no_pkg/pulls/refurb) with the same
   human labels used elsewhere (`offers/_qualification_fields.html`), comparing
   `selected` against the stored canonical value instead of legacy
   `used`/`refurbished`. Tests: `tests/test_sprint2_offer_mgmt.py` (`pulls` and
   `new_no_pkg` both render selected).

**Infra**
6. **`avail-backup-verify.service` had no failure alerting.** Added
   `OnFailure=avail-backup-verify-alert.service` wiring a new oneshot unit
   (`scripts/systemd/avail-backup-verify-alert.service`) running
   `scripts/backup-verify-alert.sh` — self-contained (no host mail/SMTP
   convention exists elsewhere): `systemd-cat -p err` + a `wall` broadcast + a
   durable `/root/backups/VERIFY_FAILED` marker file that `deploy.sh`'s final
   step checks and re-surfaces on every deploy until cleared. Documented in both
   unit files, `verify-backup.sh`'s header, and
   `docs/APP_MAP_ARCHITECTURE.md`'s backup-scripts note. Verified with `bash -n`
   and `systemd-analyze verify` (unit syntax parses; the "not executable" error
   is only because `/root/availai` doesn't exist in this sandbox).
7. **`187_startup_backfill_partial_idx.py` history excision undocumented.**
   Extended the migration's header docstring: merge revision
   `1223a56cbbbb_merge_p2_7_partial_indexes_and_p3_1_.py` was excised pre-merge
   (branch-only, no persistent environment ever ran `alembic upgrade` against
   it) in favor of chaining directly onto `71d3fef96529`; documents the
   `alembic stamp 187_startup_backfill_partial_idx` recovery path for a stray DB
   stamped at the excised revision (after verifying
   `ix_requirements_assigned_buyer` exists, else run `71d3fef96529`'s DDL by
   hand). Also fixed the stale `Revises:` header comment (said `a431c202afa4`,
   didn't match the real `down_revision = "71d3fef96529"`). Comment-only —
   `revision`/`down_revision` values unchanged, single Alembic head unaffected.

**Cleanup**
8. **`ruff.toml` inert old-path freeze entries.** Removed the
   `app/routers/htmx_views.py` (BLE001-clean today) and
   `app/routers/htmx/companies.py` (deleted, split into the `companies/` package)
   per-file-ignore entries and their explanatory comment block. Verified
   `ruff check app/` stays clean with them gone.
9. **Dead endpoint `GET /api/companies/typeahead`.** Zero remaining consumers
   (its only caller moved fully onto
   `GET /v2/partials/requisitions/customer-typeahead` in P5.2). Removed the
   endpoint (`app/routers/crm/companies.py`), its `@cached_endpoint` use, and all
   8 `invalidate_prefix("companies_typeahead")` call sites (`crm/companies.py`,
   `htmx/companies/core.py` ×4, `htmx/requisitions.py`,
   `services/prospect_claim.py`). Fixed the false "still-live caller" docstrings
   in `htmx/requisitions.py` and `unified_modal.html` (the latter also had a
   phantom hx-trigger `[filter]` description that doesn't exist on that
   trigger — fixed in the same edit). Updated the two `.claude/skills/redis`
   example snippets and two `docs/APP_MAP_INTERACTIONS.md` route descriptions
   that cited the now-removed prefix. Removed/updated the tests that only
   covered the dead endpoint (`test_load_test_fixes.py`'s
   `TestCompaniesTypeaheadCache`, `test_routers_crm.py::test_typeahead`,
   `test_prospect_claim.py`'s `invalidate_prefix("companies_typeahead")`
   assertion); left `/api/autocomplete/names` untouched (confirmed still live).
   `docs/CODE_AUDIT_AND_HARDENING_PLAN.md`'s original P5.2 entry annotated
   in-place noting the later removal, rather than rewritten.
10. **`ai_offer_service.py`'s private `_safe_int`/`_safe_float` duplicated
    `app.utils.safe_int`/`safe_float`.** **Honesty call:** the review flagged a
    real behavioral difference (`safe_int(0) == 0` vs the private
    `_safe_int(0) is None`, since the private version pre-checked falsiness
    rather than `is None`) — but every call site in `parse_offer_form_rows`
    feeds these functions Starlette `FormData.get()` values, which are always
    `str | None`. The string `"0"` is truthy (non-empty), so both
    implementations take the `int(val)`/`float(val)` branch and return `0`
    either way; `""` is falsy in both AND fails conversion regardless, landing
    on `None` either way. Confirmed behavior-identical for these specific form
    paths (verified with new tests, not assumed) — deleted the duplicate in
    favor of importing the shared `app.utils.safe_int`/`safe_float`, with a
    comment on `parse_offer_form_rows` explaining exactly why the dedup is safe
    here (and would NOT be, unexamined, for a caller that passes real numeric
    zeros). Tests: `tests/test_ai_offer_service.py` (`"0"` parses to `0`, not
    `None`; blank still parses to `None`).
11. **Stale route in two JS/template comments.** `app/static/htmx_app.js`'s
    `customerPicker` docblock cited `/v2/partials/customers/typeahead` (wrong —
    the real route is `/v2/partials/requisitions/customer-typeahead`); fixed
    both the prose mention and the `Depends on:` line. `unified_modal.html`'s
    search-input comment described an hx-trigger `[filter]` that isn't present
    on that trigger (`hx-trigger="input changed delay:300ms, focus"` has no
    `[...]` at all) — rewritten to describe what the trigger actually does
    (debounce + refocus-reopen, no filter, so no trailing-modifier
    `htmx:syntax:error` risk to guard against in the first place).
12. **Domain-extraction duplication.** Moved the validated, urlsplit-based
    `_parse_website_domain` (previously private to `app/routers/sightings.py`)
    into `app.utils.normalization.parse_website_domain` (public, shared).
    `sightings.py` now imports it (removed its now-unused `urlsplit`/`re`
    imports). `app/services/company_import_service.py`'s narrower
    regex-based `_company_domain` now delegates to the shared helper instead of
    duplicating a naive pattern that would accept junk like `"user@host:8080"`
    as a bogus domain. Left the other two legacy sites — `app.enrichment_
    service._clean_domain` and `app.utils.vendor_helpers.scrape_website_
    contacts`'s inline cache-key extractor — with `TODO` comments referencing
    the shared helper and explaining why each needs its own follow-up
    verification before migrating (different behavior/risk profile: one feeds
    AI-enrichment-normalized input, not raw user-typed website; the other
    derives a cache key only, never a persisted/user-facing domain). Tests:
    `tests/test_normalization.py` (`TestParseWebsiteDomain`,
    `TestCompanyDomainDelegatesToSharedValidator` — junk `"user@host:8080"`
    rejected, matching sightings' original behavior).
13. **`intelligence_dashboard.html` empty state lost its pre-migration icon
    spacing.** The P5.3 `empty_state.html` dedup's default `icon_class`
    (`"mx-auto mb-4 h-12 w-12 text-gray-300"`) added a `mb-4` this dashboard's
    hand-rolled markup never had (`"mx-auto h-12 w-12 text-gray-300"`). Passed
    the original `icon_class` explicitly through the `{% with %}` block so the
    rendered icon stays pixel-identical to pre-dedup. Tests:
    `tests/test_sprint7_email_integration.py` (`test_dashboard_empty_state`
    asserts the exact `mx-auto h-12 w-12 text-gray-300` class string, not the
    shared default with `mb-4`).

Verified: `ruff check`/`ruff format --check`/`docformatter --check` clean on
every touched file; CI-mypy and the hook-env replica (mypy 2.1.0,
`--ignore-missing-imports --no-strict-optional --config-file=pyproject.toml`)
both exit 0 (560 source files, no issues); assertion-theater lint reports no
new violations on touched tests; `npm run build` succeeds (htmx_app.js/template
changes); full suite: 22969 passed, 29 skipped, 0 failed.
