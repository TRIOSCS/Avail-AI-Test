# Alembic Migration 001 — Explicit-DDL Baseline Rewrite

**Status:** Design approved. Awaiting user review of written spec, then writing-plans.
**Date:** 2026-04-30 (revised 2026-05-04 — reconstruction strategy switched from git-archaeology to live-models; see "Reconstruction strategy revision" below)
**Branch:** `fix/ci-unblock-alembic-and-audit`
**Drives PR:** Single cohesive "unblock CI" PR containing the 001 rewrite + already-staged CVE bumps.
**Origin:** Brainstorm started 2026-04-23, paused at reconstruction-strategy question, resumed and completed 2026-04-30.

## Reconstruction strategy revision (2026-05-04)

The original strategy (git-archaeology of commit `d6ffe05d`) was abandoned during execution. `git ls-tree -r d6ffe05d -- app/models.py` shows that commit's `app/models.py` declared ~28 tables. Migrations 002–130 collectively touch ~94 tables (today's 84 + ~10 since-removed). Starting from the 28-table d6ffe05d snapshot would surface ~60 missing tables in the validator (a much heavier triage burden than starting from today's models). Starting from today's live `app.models` (84 tables) and letting the validator surface only the ~10 since-removed tables/columns minimizes the triage workload while still satisfying the chain.

**The new strategy:** run `Base.metadata.create_all()` from today's live `app.models` against an ephemeral Postgres container, capture `pg_dump --schema-only`, transcribe to explicit `op.create_table()` calls. Then walk migrations 002–130 with the validator and add back any historical tables/columns the chain references (buy_plans, error_reports, trouble_tickets, inventory_snapshots, material_card_audit, plus ~10 since-dropped columns).

**Validator outcome (one-shot run):** 129 migrations walked, **203 gaps reported** — these are the historical tables/columns that need to be added back to 001 in Task 6 Step 2 triage. Concentrated in 5 historical tables and ~10 columns (the 203 count is mostly duplicate references from many migrations to the same dropped table).

This spec is preserved as the design record; sections below describing "Feb-2026 baseline commit `d6ffe05d`" / "git archaeology" reflect the original design intent. The committed `scripts/reconstruct_001_baseline.py` and `scripts/validate_001_against_chain.py` implement the live-models strategy. Where the two diverge, **the script is authoritative**.

---

## Why this exists

Main-branch CI has been red for weeks. The visible symptom alternates between `DuplicateTable` on `ix_vr_scanned_by` (migration 003 on a fresh DB) and `UndefinedTable` on `buy_plans` / `error_reports` (migration 003 on other DB states).

Root cause: `alembic/versions/001_initial_schema.py` is 39 lines and uses `Base.metadata.create_all()` as its `upgrade()` body. This violates the project's ABSOLUTE rule (CLAUDE.md → Database & Migration Rules: "Never use Base.metadata.create_all() for schema changes"). The "baseline" silently drifts with today's models — so historical tables that were later renamed (`buy_plans` → `buy_plans_v3`) or restructured (`error_reports`) vanish from the baseline, while the rest of the migration chain (130 files on `origin/main` as of 2026-04-30, of which 002+ make up the forward chain after 001) still references them by their original names.

The CI smoke test (`alembic upgrade head` → `alembic downgrade base` → `alembic upgrade head` on a fresh DB) exposes this every run.

This rewrite replaces the 39-line `create_all()` body with explicit `op.create_table()` / `op.create_index()` / `op.create_foreign_key()` calls covering today's 84 model tables plus the historical tables/columns that migrations 002+ still reference (per the live-models + validator strategy — see "Reconstruction strategy revision" above). All non-001 migrations are unchanged. Production is unaffected.

---

## Locked decisions (from brainstorm)

| Decision | Choice | Rationale |
|---|---|---|
| Reconstruction strategy | **Live-models + chain validator** (revised 2026-05-04, see "Reconstruction strategy revision" above): `Base.metadata.create_all()` from today's `app.models` → `pg_dump --schema-only` → transcribe to explicit `op.create_table()`. Then walk migrations 002 through end-of-chain (verified 130 migration files on `origin/main` as of 2026-04-30) and add back any historical tables/columns the chain references but live models no longer have. | Live models cover ~84 of ~94 tables the chain ever references; d6ffe05d would have covered only ~28. Live-models minimizes triage workload while still surfacing the ~10 since-removed tables/columns via the validator. |
| Downgrade behavior | **Explicit `op.drop_table()` per table, reverse FK-dependency order** | Symmetric with upgrade; honors alembic convention; makes the roundtrip smoke test meaningful. |
| Smoke test | **Round trip + `Base.metadata` diff**: fresh DB → `upgrade head` → assert `alembic.autogenerate.compare_metadata()` returns an empty diff (modulo a small documented allowlist) → `downgrade base` → `upgrade head` | Round trip alone catches missing-table failures, asymmetric drops, and non-idempotent leftovers (the bug class currently red on main). The added metadata-diff catches a second class — model/migration drift — closing the loop the no-band-aids rule depends on going forward. |
| Prod compatibility | **Fresh-DB bootstrap only; prod stays stamped at current head and is untouched** | Prod's `alembic_version` already equals current head. The new 001 never executes there. Verified via `alembic current` pre/post deploy. |

---

## Scope

### In scope
- Rewrite `alembic/versions/001_initial_schema.py`: explicit `op.create_table()` / index / FK calls in `upgrade()`; symmetric `op.drop_*` in `downgrade()` in reverse FK order.
- All migrations other than 001 are unchanged (130-file chain on `origin/main` as of 2026-04-30; revision IDs are mixed numeric-prefix and hash-prefix — count is from `git ls-tree -r origin/main -- alembic/versions/`).
- New `scripts/reconstruct_001_baseline.py` — committed reconstruction tool.
- New `scripts/validate_001_against_chain.py` — committed validation tool.
- Final validator output committed as `scripts/validate_001_against_chain.last_run.txt`.
- CI workflow update: ensure the upgrade→downgrade→upgrade smoke test is wired and gating.
- CVE bumps already on the branch: `cryptography 46.0.5 → 46.0.7`, `python-multipart 0.0.22 → 0.0.26`. Folded into this PR.

### Out of scope (explicit)
- Audit / fix of migration 049 (`reconcile_schema_drift`) which itself contains `if_not_exists` patterns. Separate problem; on a fresh chain run, 049's idempotency branches are dormant. Audit becomes a follow-up PR if those branches fire.
- All migrations other than 001 (the rest of the chain — 129 files as of 2026-04-30 — get no audit and no rewrite in this PR).
- PR #95 (`fix/requisitions2-shell-chrome`) closure — handled when shell-arch brainstorm resumes.
- Dropping `stash@{0}` (cosmetic; do anytime).
- Performance work on fresh-DB bootstrap time.
- Differential testing against prod's actual live schema (covered indirectly by the post-merge `alembic current` check).

---

## Architecture

### File-level changes
| File | Change |
|---|---|
| `alembic/versions/001_initial_schema.py` | Full rewrite (~39 lines → ~1500–3000 lines explicit DDL). Same revision ID `001_initial`. Same down_revision (`None`). |
| `scripts/reconstruct_001_baseline.py` | New. Committed dev tool. |
| `scripts/validate_001_against_chain.py` | New. Committed dev tool. |
| `scripts/validate_001_against_chain.last_run.txt` | New. Committed evidence-of-validation artifact. |
| `requirements.txt` | Already-staged CVE bumps preserved. |
| `.github/workflows/ci.yml` | **No change needed.** The upgrade→downgrade→upgrade smoke test already exists at lines 101–110 ("Alembic upgrade/downgrade smoke test"); fixing 001 turns the currently-red job green. The plan must verify this assumption holds at execution time. |

### Revision identity invariant
The alembic revision ID `001_initial` does not change. The down_revision (`None`) does not change. Only the *body* of `upgrade()` and `downgrade()` changes. Any DB stamped at any revision ≥ `001_initial` is therefore unaffected — alembic's version table will not see a new value because of this PR.

---

## Components

### 1. Reconstruction script — `scripts/reconstruct_001_baseline.py`

**Purpose:** generate the first-draft explicit DDL from today's live `app.models` (revised 2026-05-04 — see "Reconstruction strategy revision" at the top of this spec).

**Steps:**
1. Spin up an ephemeral `postgres:16` container (matching prod and CI versions): `docker run --rm -d postgres:16` with a random port.
2. In a subprocess with `PYTHONPATH` set to the repo root and `DATABASE_URL`/`SECRET_KEY`/`SESSION_SECRET` env vars set to throwaway values, `from app.models import Base` and run `Base.metadata.create_all(bind=engine)` against the ephemeral DB.
3. `pg_dump --schema-only --no-owner --no-privileges` against the ephemeral DB.
4. Parse the dump and emit a Python file of `op.create_table()`, `op.create_index()`, `op.create_foreign_key()` calls in FK-dependency order. Output goes to `alembic/versions/001_initial_schema.py.draft`.
5. Tear down the container.

**The live-models choice has a known cost:** today's models don't include historical tables/columns that earlier migrations (002–130) still reference (e.g. `buy_plans` was renamed to `buy_plans_v3`; `error_reports`, `trouble_tickets`, `inventory_snapshots`, `material_card_audit` were dropped; ~10 columns like `sf_*` and `acctivate_*` were removed). The validator (Component 2) surfaces these gaps; Task 6 Step 2 triages them by adding the missing definitions back into 001. This is by-design — the validator's whole job is to enumerate what the chain assumes vs. what the draft provides.

### 2. Validation harness — `scripts/validate_001_against_chain.py`

**Purpose:** confirm the new 001 provides everything migrations 002 through end-of-chain (verified 130 migration files on `origin/main` as of 2026-04-30) assume.

**Steps:**
1. Parse new `001_initial_schema.py` to build a model of the resulting schema state (tables, columns, indices, FKs, ENUMs).
2. Walk every revision after `001_initial` in revision order (resolved via `alembic history` rather than filename pattern, since revision IDs in this repo are mixed numeric-prefix and hash-prefix).
3. For each operation in each migration, simulate its effect on the schema model:
   - `op.create_table` / `create_index` / `create_foreign_key` / `add_column` → add to model
   - `op.drop_table` / `drop_index` / `drop_constraint` / `drop_column` → require target exists in model; remove if so; report gap if not
   - `op.alter_column` / `rename_table` → require target exists; mutate accordingly
4. Emit a structured report — plain text, one finding per line in the format `<migration_filename>:<op_index>: <op_summary>: missing <table_or_column>`, followed by a final summary line `<N> migrations walked, <M> gaps found`.
5. Exit non-zero if gaps remain; zero if clean.

**Manual triage rule:** every gap must be triaged by reading the offending migration. Never auto-add columns based on a later `op.add_column` "expecting" them, because the later migration may be re-creating something 001 should already have under a different name. Read each migration; decide explicitly.

**Iteration loop:** developer runs validator → reads gaps → patches 001 → re-runs validator → repeats until clean. Final clean output is committed as `scripts/validate_001_against_chain.last_run.txt`.

### 3. Rewritten `alembic/versions/001_initial_schema.py`

**Header docstring (verbatim — load-bearing for future readers):**
> Initial schema — explicit DDL baseline. Generated 2026-05-04 from today's live `app.models` via `scripts/reconstruct_001_baseline.py`, then augmented with historical tables/columns referenced by migrations 002+ (per `scripts/validate_001_against_chain.py`'s gap report). Validated against migrations 002 through end-of-chain (verified 130 migration files on `origin/main` as of 2026-04-30). **For fresh DBs only.** Production and any DB already stamped at any revision ≥ `001_initial` is unaffected — alembic's version table is not modified by this rewrite.

**Body:**
- `revision: str = "001_initial"` — unchanged
- `down_revision: Union[str, None] = None` — unchanged
- `upgrade()`: explicit `op.create_table()` calls in FK-dependency order; FKs declared inline in `create_table` where the referenced table has already been created in this same migration; standalone `op.create_foreign_key()` calls only where cycles or forward-references force it. `op.create_index()` calls follow each block of related tables.
- `downgrade()`: `op.drop_table()` calls in *reverse* FK-dependency order; `op.drop_index()` calls before drops where required; explicit `DROP TYPE` calls for any ENUMs created via `sa.Enum(..., create_type=True)`

### 4. CI smoke-test job

**Already wired** at `.github/workflows/ci.yml` lines 101–110 (`Alembic upgrade/downgrade smoke test`). No new job to add — fixing 001 is precisely what turns this currently-red step green.

**What it runs (in order, on a fresh empty `postgres:16` container provisioned by the workflow):**
1. `alembic upgrade head`
2. **Schema-equivalence check** — assert `alembic.autogenerate.compare_metadata(MigrationContext.configure(connection), Base.metadata)` returns an empty diff list, modulo a small documented allowlist of accepted false-positive diff types (e.g., `Numeric(10,2)` rendering, server-side default representation). Allowlist lives inline in the test file with one comment per entry explaining the third-party-tool quirk.
3. `alembic downgrade base`
4. `alembic upgrade head`

**Pass criteria:** all four steps pass (steps 1, 3, 4 exit 0; step 2's diff list is empty modulo the allowlist).

**Why the diff layer was added:** without it, the smoke test only catches "the chain doesn't apply." With it, the test also catches "the chain applies, but it doesn't produce what the models say it should" — the very class of drift the create_all-baseline was hiding. This is the long-term regression detector the no-band-aids rule depends on.

**Implementation note:** `alembic.autogenerate.compare_metadata` is alembic stdlib — no new dependency. Battle-tested. Returns `[]` on a clean match.

**Plan responsibility:** verify the workflow file still contains this exact step before assuming it's there — guard against drift between this spec being written and the plan executing. If the existing workflow has only the round-trip test, the plan adds the diff step.

### 5. PR description / commit message

**Required content (load-bearing):**
- Explicit "fresh-DB bootstrap only, prod untouched" callout in both the commit body and the PR description's TL;DR.
- Link to this design spec.
- Reconstruction provenance (live-models strategy, see spec §"Reconstruction strategy revision").
- Validation method + reference to committed `last_run.txt`.
- Pre/post `alembic current` output from prod (proving zero version-table change).

---

## Data flow

### Path 1 — fresh-DB bootstrap (the path being fixed)
```
empty DB
  → alembic upgrade head
    → 001_initial: CREATE TABLE × ~84 + indices + FKs + ENUMs
    → all subsequent revisions in alembic-history order: ALTER/ADD/DROP/RENAME applied
  → DB at "head" revision; schema matches current models
```
Today: fails at 003. After rewrite: clean.

### Path 2 — CI roundtrip-plus-diff smoke test
```
empty DB
  → alembic upgrade head        (forward chain)
  → compare_metadata(...)       (assert empty diff vs. Base.metadata, modulo allowlist)
  → alembic downgrade base      (reverse chain; 001's drop_table set runs last)
  → alembic upgrade head        (forward chain again)
  ⇒ all four steps pass
```
The diff step catches model/migration drift (e.g., a column added to a model but never migrated, or vice versa). The second `upgrade head` catches any non-idempotent leftover after downgrade (orphan ENUMs, sequences, etc.).

### Path 3 — prod (the no-op path; must be no-op)
```
prod DB at revision <head>
  → alembic upgrade head
  → no migrations run (already at head)
  → alembic_version unchanged
```
Verification: `alembic current` byte-identical pre and post deploy.

### Path 4 — reconstruction & validation (one-time, dev machine)
```
app.models (live)  ──→ scripts/reconstruct_001_baseline.py
                          │
                          ├→ ephemeral postgres:16
                          │   └→ Base.metadata.create_all()
                          │   └→ pg_dump --schema-only
                          │
                          └→ alembic/versions/001_initial_schema.py.draft (~85 tables)

draft 001 + alembic/versions/002+  ──→ scripts/validate_001_against_chain.py
                                          │
                                          ├→ "all references resolve"  → finalize 001 (rename .draft → real)
                                          └→ "gap list" → triage each gap → patch 001 with historical
                                              tables/columns the chain still references → re-run
```

### Invariant
The alembic_version table never sees a different value because of this PR. We're swapping the *body* of revision `001_initial`, not the *identity*.

---

## Error handling

### Reconstruction-script failures
**Failure:** today's `app.models` import fails inside the subprocess (e.g. missing env vars, importable side-effects).
**Mitigation:** the script sets `DATABASE_URL`, `SECRET_KEY`, `SESSION_SECRET` to throwaway values and prepends repo root to `PYTHONPATH`. If a new module-level side effect breaks import, fix it at the source (the model module shouldn't have hard runtime requirements at import time anyway).
**Note on the abandoned d6ffe05d path:** the original spec called for shimming Feb-2026 models with a `~2-hour time-box`. The 2026-05-04 strategy revision rendered this moot — see "Reconstruction strategy revision" at the top of the spec.

### Validation-harness output
**This is the expected path, not a failure** — the validator's whole purpose is to surface what migrations 002+ reference that the live-models snapshot doesn't include. The 2026-05-04 one-shot run reported 203 gaps clustered around 5 since-removed tables (`buy_plans`, `error_reports`, `trouble_tickets`, `inventory_snapshots`, `material_card_audit`) and ~10 since-removed columns.
**Process for each gap:** read the offending migration, decide what 001 needs to provide, patch 001, re-run.
**Hard rule:** never auto-add columns based on a later `op.add_column`. Always read the migration first. Some 002+ migrations create things that 001 should already have under a different name; auto-adding would mask the real fix.

### CI smoke-test failures
| Where it fails (4-step smoke test) | Likely cause | Action |
|---|---|---|
| Step 1 `upgrade head` | 001 still missing a table or column | Add to 001, re-run validator, push. |
| Step 2 `compare_metadata` non-empty diff | Model-vs-migration drift: a column added to a model but not in any migration, or vice versa. Could be 001's fault (bad reconstruction) or a later migration's fault (missing follow-up to a model change). | Read each diff entry. If 001 is the source → fix here. If a later migration is the source → in scope only if the fix is purely additive in 001 (the cleanest closure); otherwise raise as a follow-up issue and add the diff entry to the allowlist with a comment linking the issue. |
| Step 3 `downgrade base` | Asymmetric drop somewhere in chain | If in 001 → fix here. If in 002+ → out of scope; mark smoke test xfail with linked follow-up issue, get this PR through. |
| Step 4 `upgrade head` (re-upgrade after downgrade) | Non-idempotent leftover (orphan ENUM, sequence) | Likely fix: explicit `DROP TYPE` in 001's downgrade. Or, if originating in 002+, same xfail-with-issue escape hatch. |

**Two escape hatches, both bounded:**
- **xfail** (for downgrade-asymmetry or non-idempotency failures whose root cause is in a non-001 migration): mark the smoke-test step xfail with an inline comment linking a freshly-filed follow-up issue and named owner.
- **`compare_metadata` allowlist** (for diff entries whose root cause is a non-001 migration that this PR's scope cannot fix): add the specific diff signature to the allowlist with a comment linking a freshly-filed follow-up issue.

Both exist so that drift we discover *elsewhere in the chain* doesn't blow up this PR's scope to a chain-wide audit (out of scope). Any 001-internal issue must be fixed in this PR — escape hatches do not cover 001 itself.

### Post-merge prod surprise
**Failure:** `alembic current` on prod after deploy shows a different revision than before.
**Action:** stop, roll back. Should be impossible (we don't change revision IDs) but verify anyway.

**Failure:** developer's fresh local DB fails `alembic upgrade head` after merge.
**Action:** regression in 001; the smoke test had a hole. Fix-forward PR.

---

## Testing

### CI gate (mandatory, blocking)
The roundtrip-plus-diff smoke test described in Component 4 / Path 2. All four steps must pass: upgrade head, `compare_metadata` empty diff, downgrade base, upgrade head.

### Local developer smoke test (documented in PR)
```bash
docker compose down -v && docker compose up -d db
docker compose exec app alembic upgrade head
docker compose exec app pytest tests/test_alembic_round_trip.py::test_schema_matches_models -v
docker compose exec app alembic downgrade base
docker compose exec app alembic upgrade head
```
Reviewers run this before approving. The `compare_metadata` assertion is exposed as a pytest test (lives in `tests/test_alembic_round_trip.py` per Component 4) so the local check is the same code path as the CI check.

### Schema-equivalence sanity check (one-time, during dev)
After 001 is drafted, before opening the PR: run the new chain on an empty DB, then run `Base.metadata.create_all()` from current models on another empty DB, and diff the two `pg_dump --schema-only` outputs. This is a stricter check than `compare_metadata` (it surfaces server-side comment-formatting and PG-version differences). Any unexpected diff = bug to fix in 001 before opening the PR.

**Not a CI gate** — too noisy, too many environment-dependent diffs. One-time dev sanity check that catches things `compare_metadata` waves through.

### Existing pytest suite
Must still pass, modulo the 6 known failures tracked in the pre-rollout-checklist tech-debt register (the doc lands with PR #94 at `docs/PRE_ROLLOUT_CHECKLIST.md`; until #94 merges, the register lives only on that branch). The 001 rewrite is pure DDL; no Python application code paths should care.

### Post-merge prod gate
```bash
ssh prod 'cd /root/availai && docker compose exec app alembic current'   # capture before merge
# merge + deploy
ssh prod 'cd /root/availai && docker compose exec app alembic current'   # capture after deploy
diff <before> <after>   # must be empty
```
Documented in PR description as the deploy verification step.

### Validator's last-run report
Committed as `scripts/validate_001_against_chain.last_run.txt`. Shows reviewers the validation actually ran clean. Re-runnable any time.

---

## Hard gates / sequencing

1. **No code touching `alembic/versions/001_initial_schema.py`** until this spec is approved by user and a `writing-plans` plan is in hand. (Per memory's standing rule for this thread.)
2. **No merging this PR** until the CI smoke test is green on it.
3. **No merging Phase 4 PRs (#92, #93, #94, #96, #97, #98)** until this PR lands and main CI is green. Then merge order is: #96 first → #92/#93/#94/#97/#98 in any order.
4. **No resuming the shell-arch brainstorm** (which closes PR #95) until step 3 is complete.

---

## Related docs / memory

- `/root/.claude/projects/-root/memory/project_migration_001_rewrite_2026_04_23.md` — original brainstorm context (stale parts now superseded by this spec)
- `/root/.claude/projects/-root/memory/project_sourcing_engine_phase4_2026_04_22.md` — the 5 PRs blocked behind this work
- `/root/.claude/projects/-root/memory/project_shell_arch_brainstorm_2026_04_23.md` — parked thread; resumes after this lands
- `/root/.claude/projects/-root/memory/feedback_no_band_aids.md` — the rule driving this rewrite (root-cause over band-aid)
- `/root/availai/CLAUDE.md` → "Database & Migration Rules" → ABSOLUTE rule #2: "Never use Base.metadata.create_all() for schema changes" — the rule currently violated by 001
- `/root/availai/docs/PRE_ROLLOUT_CHECKLIST.md` — references this CI gate as part of pre-import readiness (file lands with PR #94)
