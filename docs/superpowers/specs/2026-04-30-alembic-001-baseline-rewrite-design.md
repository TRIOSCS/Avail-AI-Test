# Alembic Migration 001 — Explicit-DDL Baseline Rewrite

**Status:** Design approved. Awaiting user review of written spec, then writing-plans.
**Date:** 2026-04-30
**Branch:** `fix/ci-unblock-alembic-and-audit`
**Drives PR:** Single cohesive "unblock CI" PR containing the 001 rewrite + already-staged CVE bumps.
**Origin:** Brainstorm started 2026-04-23, paused at reconstruction-strategy question, resumed and completed 2026-04-30.

---

## Why this exists

Main-branch CI has been red for weeks. The visible symptom alternates between `DuplicateTable` on `ix_vr_scanned_by` (migration 003 on a fresh DB) and `UndefinedTable` on `buy_plans` / `error_reports` (migration 003 on other DB states).

Root cause: `alembic/versions/001_initial_schema.py` is 39 lines and uses `Base.metadata.create_all()` as its `upgrade()` body. This violates the project's ABSOLUTE rule (CLAUDE.md → Database & Migration Rules: "Never use Base.metadata.create_all() for schema changes"). The "baseline" silently drifts with today's models — so historical tables that were later renamed (`buy_plans` → `buy_plans_v3`) or restructured (`error_reports`) vanish from the baseline, while migrations 002 through 131 still reference them by their original names.

The CI smoke test (`alembic upgrade head` → `alembic downgrade base` → `alembic upgrade head` on a fresh DB) exposes this every run.

This rewrite replaces the 39-line `create_all()` body with explicit `op.create_table()` / `op.create_index()` / `op.create_foreign_key()` calls reflecting the schema as-of the Feb-2026 baseline commit. Migrations 002–131 are unchanged. Production is unaffected.

---

## Locked decisions (from brainstorm)

| Decision | Choice | Rationale |
|---|---|---|
| Reconstruction strategy | **Hybrid (a + b)**: git archaeology to generate first-draft DDL, then validate by walking migrations 002–131 | Reproducible from git AND validated against the forward chain. Highest rigor available. |
| Downgrade behavior | **Explicit `op.drop_table()` per table, reverse FK-dependency order** | Symmetric with upgrade; honors alembic convention; makes the roundtrip smoke test meaningful. |
| Smoke test | **Full chain: fresh DB → `upgrade head` → `downgrade base` → `upgrade head`** | Catches missing-table failures, asymmetric drops, and non-idempotent leftovers in one test. This is the test currently failing on main. |
| Prod compatibility | **Fresh-DB bootstrap only; prod stays stamped at current head and is untouched** | Prod's `alembic_version` already equals current head. The new 001 never executes there. Verified via `alembic current` pre/post deploy. |

---

## Scope

### In scope
- Rewrite `alembic/versions/001_initial_schema.py`: explicit `op.create_table()` / index / FK calls in `upgrade()`; symmetric `op.drop_*` in `downgrade()` in reverse FK order.
- Migrations 002–131 unchanged.
- New `scripts/reconstruct_001_baseline.py` — committed reconstruction tool.
- New `scripts/validate_001_against_chain.py` — committed validation tool.
- Final validator output committed as `scripts/validate_001_against_chain.last_run.txt`.
- CI workflow update: ensure the upgrade→downgrade→upgrade smoke test is wired and gating.
- CVE bumps already on the branch: `cryptography 46.0.5 → 46.0.7`, `python-multipart 0.0.22 → 0.0.26`. Folded into this PR.

### Out of scope (explicit)
- Audit / fix of migration 049 (`reconcile_schema_drift`) which itself contains `if_not_exists` patterns. Separate problem; on a fresh chain run, 049's idempotency branches are dormant. Audit becomes a follow-up PR if those branches fire.
- The other 130 migrations (no audit, no rewrite).
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

**Purpose:** generate the first-draft explicit DDL from the Feb-2026 baseline commit `d6ffe05d`.

**Steps:**
1. `git show d6ffe05d:app/models.py > <tempfile>` and `git show d6ffe05d:app/database.py > <tempfile>` (does not pollute working tree)
2. Load the two files into an isolated namespace via `importlib.util.spec_from_file_location`. If import-time side effects break this, fall back to a subprocess with curated `PYTHONPATH` and shim modules for any missing dependencies.
3. Spin up an ephemeral `postgres:16` container (matching prod and CI versions): `docker run --rm -d postgres:16` with a random port.
4. Run `Base.metadata.create_all()` against the ephemeral DB.
5. `pg_dump --schema-only --no-owner --no-privileges` against the ephemeral DB.
6. Parse the dump and emit a Python file of `op.create_table()`, `op.create_index()`, `op.create_foreign_key()` calls in FK-dependency order. Output goes to `alembic/versions/001_initial_schema.py.draft`.
7. Tear down the container.

**Fallback if step 2 fails (Feb-2026 models can't load cleanly):** drop steps 2 and skip create_all-from-models. Hand-define a minimal SQLAlchemy `Base` with just the table definitions transcribed from a known-good production schema dump. Then resume from step 4. The validation pass (component 2) catches any drift this introduces.

**Decision rule:** if loading Feb-2026 models needs more than ~2 hours of shimming, fall back. Time-box, don't rabbit-hole.

### 2. Validation harness — `scripts/validate_001_against_chain.py`

**Purpose:** confirm the new 001 provides everything migrations 002–131 assume.

**Steps:**
1. Parse new `001_initial_schema.py` to build a model of the resulting schema state (tables, columns, indices, FKs, ENUMs).
2. Walk `alembic/versions/002_*` through `alembic/versions/131_*` in revision order.
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
> Initial schema — explicit DDL baseline. Generated 2026-04-30 from `d6ffe05d` models via `scripts/reconstruct_001_baseline.py`, validated against migrations 002–131 via `scripts/validate_001_against_chain.py`. **For fresh DBs only.** Production and any DB already stamped at any revision ≥ `001_initial` is unaffected — alembic's version table is not modified by this rewrite.

**Body:**
- `revision: str = "001_initial"` — unchanged
- `down_revision: Union[str, None] = None` — unchanged
- `upgrade()`: explicit `op.create_table()` calls in FK-dependency order; FKs declared inline in `create_table` where the referenced table has already been created in this same migration; standalone `op.create_foreign_key()` calls only where cycles or forward-references force it. `op.create_index()` calls follow each block of related tables.
- `downgrade()`: `op.drop_table()` calls in *reverse* FK-dependency order; `op.drop_index()` calls before drops where required; explicit `DROP TYPE` calls for any ENUMs created via `sa.Enum(..., create_type=True)`

### 4. CI smoke-test job

**Already wired** at `.github/workflows/ci.yml` lines 101–110 (`Alembic upgrade/downgrade smoke test`). No new job to add — fixing 001 is precisely what turns this currently-red step green.

**What it runs (in order, on a fresh empty `postgres:16` container provisioned by the workflow):**
1. `alembic upgrade head`
2. `alembic downgrade base`
3. `alembic upgrade head`

**Pass criteria:** all three steps exit 0.

**Plan responsibility:** verify the workflow file still contains this exact step before assuming it's there — guard against drift between this spec being written and the plan executing.

### 5. PR description / commit message

**Required content (load-bearing):**
- Explicit "fresh-DB bootstrap only, prod untouched" callout in both the commit body and the PR description's TL;DR.
- Link to this design spec.
- Reconstruction provenance (commit `d6ffe05d`, hybrid a+b strategy).
- Validation method + reference to committed `last_run.txt`.
- Pre/post `alembic current` output from prod (proving zero version-table change).

---

## Data flow

### Path 1 — fresh-DB bootstrap (the path being fixed)
```
empty DB
  → alembic upgrade head
    → 001_initial: CREATE TABLE × ~84 + indices + FKs + ENUMs
    → 002 ... 131: ALTER/ADD/DROP/RENAME applied in order
  → DB at "head" revision; schema matches current models
```
Today: fails at 003. After rewrite: clean.

### Path 2 — CI roundtrip smoke test
```
empty DB
  → alembic upgrade head        (forward chain, 131 steps)
  → alembic downgrade base      (reverse chain, 131 steps; 001's drop_table set runs last)
  → alembic upgrade head        (forward chain again)
  ⇒ exit 0 on all three
```
Step 3 catches any non-idempotent leftover after downgrade (orphan ENUMs, sequences, etc.).

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
git show d6ffe05d:app/models.py     ─┐
git show d6ffe05d:app/database.py   ─┴→ scripts/reconstruct_001_baseline.py
                                          │
                                          ├→ ephemeral postgres
                                          │   └→ Base.metadata.create_all()
                                          │   └→ pg_dump --schema-only
                                          │
                                          └→ alembic/versions/001_initial_schema.py.draft

draft 001 + alembic/versions/002..131  ──→ scripts/validate_001_against_chain.py
                                          │
                                          ├→ "all references resolve"  → finalize 001 (rename .draft → real)
                                          └→ "gap list" → patch 001 → re-run
```

### Invariant
The alembic_version table never sees a different value because of this PR. We're swapping the *body* of revision `001_initial`, not the *identity*.

---

## Error handling

### Reconstruction-script failures
**Failure:** Feb-2026 models can't load cleanly (import-time side effects, missing relative-import contexts, env-dependent code).
**Mitigation:** load via subprocess with curated PYTHONPATH; stub missing imports with shim modules.
**Time-box:** if shimming exceeds ~2 hours of work, fall back to handwriting `op.create_table()` from a known-good prod schema dump. Lean on the validation pass to catch any drift this introduces.

### Validation-harness output
**This is the expected path, not a failure** — the validator's whole purpose is to surface drift between Feb-2026 models and what 002+ assumed.
**Process for each gap:** read the offending migration, decide what 001 needs to provide, patch 001, re-run.
**Hard rule:** never auto-add columns based on a later `op.add_column`. Always read the migration first. Some 002+ migrations create things that 001 should already have under a different name; auto-adding would mask the real fix.

### CI smoke-test failures
| Where it fails | Likely cause | Action |
|---|---|---|
| `upgrade head` step 1 | 001 still missing a table or column | Add to 001, re-run validator, push. |
| `downgrade base` step 2 | Asymmetric drop somewhere in chain | If in 001 → fix here. If in 002+ → out of scope; mark smoke test xfail with linked follow-up issue, get this PR through. |
| `upgrade head` step 3 (re-upgrade after downgrade) | Non-idempotent leftover (orphan ENUM, sequence) | Likely fix: explicit `DROP TYPE` in 001's downgrade. Or, if originating in 002+, same xfail-with-issue escape hatch. |

**xfail escape hatch:** the only band-aid permitted in this PR's scope, and only for failures *outside* 001 itself. Any 001-internal issue must be fixed in this PR. Each xfail must link to a follow-up issue and have an owner. The escape exists so that downgrade asymmetries we discover *elsewhere in the chain* don't blow up this PR's scope to "audit all 130 migrations" — which is explicitly out of scope.

### Post-merge prod surprise
**Failure:** `alembic current` on prod after deploy shows a different revision than before.
**Action:** stop, roll back. Should be impossible (we don't change revision IDs) but verify anyway.

**Failure:** developer's fresh local DB fails `alembic upgrade head` after merge.
**Action:** regression in 001; the smoke test had a hole. Fix-forward PR.

---

## Testing

### CI gate (mandatory, blocking)
The upgrade→downgrade→upgrade smoke test described in Component 4 / Path 2.

### Local developer smoke test (documented in PR)
```bash
docker compose down -v && docker compose up -d db
docker compose exec app alembic upgrade head
docker compose exec app alembic downgrade base
docker compose exec app alembic upgrade head
```
Reviewers run this before approving.

### Schema-equivalence sanity check (one-time, during dev)
After 001 is drafted: run the new chain on an empty DB, run `Base.metadata.create_all()` from current models on another empty DB, diff the two `pg_dump --schema-only` outputs. Should match modulo expected drift (alembic_version table, comment formatting, PG version-specific quirks). Any unexpected diff = bug to fix in 001 or somewhere in 002+ before merging.

**Not a CI gate** — too noisy, too many environment-dependent diffs. One-time dev sanity check.

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
