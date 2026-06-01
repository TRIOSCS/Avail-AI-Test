# Open-PR Backlog Landing — Implementation Plan (rev. 2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Resolve the entire open-PR backlog — close noise, land verified work, harden-then-merge feature PRs, correct docs, decide deferrals, and deploy — in dependency-safe order.

**Architecture:** Phased by risk and dependency. Each phase leaves `main` green and deployable. Two reviews (architect + simplify) shaped rev. 2: the critical-path was trimmed of refactor churn and over-decomposition, and two deploy-blocking risks were fixed — the **alembic multi-head fork** (085 + the spec-resolver migration both fork off `084_description`) and the **resolver `flush()` IntegrityError path** (must be a `begin_nested()` savepoint so a race can't abort saved sightings).

**Tech Stack:** FastAPI + SQLAlchemy 2.0 + PostgreSQL + HTMX/Alpine + Jinja2; `gh` CLI; Alembic; pytest (SQLite); `./deploy.sh`.

**Conventions (`docs/BRANCH_AND_CI_WORKFLOW.md`):** branch off current `main`; small PRs; rebase stale branches (the changed-files gate means rebasing never re-introduces drift); merged → delete; unmerged → `archive/*` tag before delete. Verify each command's output before checking a step off — evidence before "done".

**Scope discipline:** every code change below is a correctness or security finding from the 11-agent review. Pure-polish items (helper extractions, an invariant guard against a bug the code can't currently hit) were cut to keep this honestly scoped to *landing* the backlog, not improving it while landing.

---

## Phase 0 — Remove noise (no code; reversible)

### Task 0.1: Close the coverage-bot drafts

- [ ] **Step 1: Close 5 drafts with a reason**
```bash
for n in 182 181 156 155 146; do
  gh pr close "$n" --comment "Closing: nightly-coverage churn — red/conflicting, mutually superseded, no production code. Per 2026-06-01 backlog review. Branch on origin; reopen if a specific coverage gain is wanted."
done
```
Expected: 5 × "Closed pull request #N".

- [ ] **Step 2: Verify**
Run: `gh pr list --state open --json number -q '[.[].number]|sort'` — Expected: 182/181/156/155/146 absent.

- [ ] **Step 3 (inline note, not a PR):** when Phase 3 edits `docs/BRANCH_AND_CI_WORKFLOW.md`, append one line to §5: "Do not run `scripts/frprp.py` to open coverage PRs — it produces perpetually-red drafts." No separate branch/PR for one sentence.

---

## Phase 1 — Land verified work (CI-green, no code changes)

Three merge-only actions. #183 carries migration `085` — **deploy is gated in Phase 6**, but merging the code now is safe (UTCDateTime tolerates the not-yet-converted columns: reads tag UTC, writes normalize).

### Task 1.1: Merge the ready set

- [ ] **Step 1: For each PR, rebase + confirm green + merge** (do #166, #158, #180, then #183, in that order):
```bash
for n in 166 158 180 183; do
  gh pr update-branch "$n" 2>/dev/null || true     # pull main in if BEHIND
  gh pr checks "$n"                                 # wait for green
  gh pr view "$n" --json mergeable -q .mergeable    # expect MERGEABLE
  gh pr merge "$n" --squash --delete-branch
done
```
Expected: 4 merged. **#183: do NOT deploy yet** — Phase 6 gate.

- [ ] **Step 2: Migration-scope sanity (architect Finding 6).** Confirm `085_utcdatetime_timestamptz` converting `material_cards.last_searched_at` doesn't break already-merged #180. Since #180 merges *before* #183 and isn't re-run, risk is nil today; just note: `grep -n "last_searched_at" alembic/versions/085_utcdatetime_timestamptz.py` — it is in scope (a real UTCDateTime column), which is correct.

---

## Phase 2 — #177 hardening, then merge

**All Phase-2 edits are on branch `refactor/bulk-endpoints-followups` (PR #177)** — the bulk endpoints + their new response schemas live there, NOT on `main` (architect Findings 3 & 5). The inline terminal-status lists to replace are in `app/routers/requisitions/core.py` (the `bulk_archive` / `batch_archive_by_ids` `where()` clauses) and #177's own service code.

### Task 2.1: Characterization tests for the bulk rules + the terminal constant

**Files (on PR #177 branch):** `app/constants.py`, `app/routers/requisitions/core.py`, `app/services/requisition_service.py`, `tests/test_requisition_service_bulk.py` (new).

- [ ] **Step 1: Write the missing tests** (the SALES ownership filter and terminal-status exclusion are currently unpinned — the real gaps the review flagged):
```python
# tests/test_requisition_service_bulk.py
def test_batch_archive_for_user_sales_only_archives_own(db_session, ...):
    # SALES user; ids = [own_req, other_owned_req] -> result == [own_req]
def test_batch_archive_excludes_terminal(db_session, ...):
    # seed a WON req; request its id -> not in returned ids
```
(Use `tests/conftest.py` fixtures + the existing `_make_*` helper pattern.)

- [ ] **Step 2: Run → FAIL/ERROR.** `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_requisition_service_bulk.py -q --override-ini="addopts="`.

- [ ] **Step 3: Source changes (no behavior change expected — review judged the logic correct):** add a `TERMINAL` frozenset classvar to `RequisitionStatus` in `app/constants.py`; replace the inline status lists in `app/routers/requisitions/core.py` and the `_TERMINAL_STATUSES` global in `requisition_service.py` with `RequisitionStatus.TERMINAL`. If a characterization test surfaces a real bug, fix it here.

- [ ] **Step 4: Run → PASS** (those + `tests/test_routers_requisitions.py`).

- [ ] **Step 5 (OPTIONAL polish — only if it rides this commit; do NOT gate the merge):** add `@model_validator(mode="after")` to `BulkArchiveResponse`/`BatchAssignResponse` enforcing `count == len(ids)` and make `assigned_to` required. Skip if it adds round-trips — the service builds both sides together, so the invariant can't currently be violated.

- [ ] **Step 6: Commit, push, merge**
```bash
git add -A && git commit -m "test+refactor(requisitions): pin SALES/terminal bulk rules; TERMINAL constant (#177)"
git push origin refactor/bulk-endpoints-followups
gh pr update-branch 177; gh pr checks 177      # green
gh pr merge 177 --squash --delete-branch
```

---

## Phase 3 — Correct docs, then merge/close

#178/#179 carry the obsolete "blanket PR-1.3 → all-197 TIMESTAMPTZ" plan and mis-scope the 38 `timezone=True` columns. #178 Phases 2–4 stay valid.

### Task 3.1: #179 (UTCDateTime audit) — close (it's now historical)

- [ ] **Step 1:** Confirm not referenced: `git grep -l 2026-05-27-phase1-utcdatetime-audit` (review found zero refs).
- [ ] **Step 2:** `gh pr close 179 --comment "Superseded by #183 (shipped symmetric UTCDateTime + migration 085). Audit's blanket-TIMESTAMPTZ decision was inaccurate; #183 is the source of truth."` (Reopen+correct only if a reference surfaces.)

### Task 3.2: #178 roadmap — correct Phase 1, keep the rest, merge

**Files (on `docs/deferred-high-tier-roadmap-2026-05-27`):** `docs/superpowers/plans/2026-05-27-deferred-high-tier-roadmap.md`, `docs/BRANCH_AND_CI_WORKFLOW.md` (the §5 frprp line from Phase 0).

- [ ] **Step 1:** Rewrite the HIGH-DB-2 section → "DONE via #183 (symmetric `UTCDateTime` + migration 085); blanket PR-1.3 dropped." Leave HIGH-BE-11 (~1,163 `db.query()`), HIGH-SEC-4 (`validationToken` echo), HIGH-BE-1/2 (`htmx_views.py` ~10k LOC) — still valid. Append the frprp note to the workflow doc.
- [ ] **Step 2:** Commit/push; `gh pr update-branch 178 && gh pr merge 178 --squash --delete-branch` after CI green.

---

## Phase 4 — Spec-resolver hardening, then merge bottom-up

Apply fixes on the stack tip `feat/spec-resolver-5-admin` (cumulative). Stack: #167→#170→#171→#172→#173→#175.

### Task 4.1: Caller-owned transaction via a savepoint (architect Finding 2 — BLOCKER)

**Files:** `app/services/spec_code_resolver.py`, `app/search_service.py`. Reference patterns: `app/connectors/email_mining.py:109-124`, `app/services/tagging.py:182-188`.

- [ ] **Step 1: Test** — `SpecCodeResolver.resolve()` must not commit the caller's session (patch `db.commit` to raise; assert resolve still returns the pending result via `flush`), AND a concurrent-insert `IntegrityError` must NOT abort the surrounding write session's saved sightings.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3:** In `spec_code_resolver.py`, change the internal `self._db.commit()` (~L211–212) to `self._db.flush()`. In `search_service.py`, wrap the resolver call in a savepoint so only the spec-code insert rolls back on a race — NOT the whole write session:
```python
from sqlalchemy.exc import IntegrityError
try:
    with write_db.begin_nested():           # SAVEPOINT — isolates the resolver insert
        result = SpecCodeResolver(write_db).resolve(mpn, oem=oem_hint)
except IntegrityError:
    write_db.rollback()                      # rolls back to the savepoint only
    result = None                            # treat as unresolved; sightings stay intact
# ... caller's existing write_db.commit() still owns the outer transaction
```
Keep the admin `re_resolve` SAVEPOINT (`app/routers/admin/spec_codes.py`) as the canonical reference.
- [ ] **Step 4: Run → PASS:** `pytest tests/test_spec_code_resolver.py tests/test_search_service_with_spec_resolver.py tests/routers/admin/test_spec_codes_pending.py -q --override-ini="addopts="`.

### Task 4.2: Stop silently dropping good LLM resolutions

**Files:** `app/schemas/spec_codes.py` (`ResolverLlmResponse`).

- [ ] **Step 1: Test** — a payload with an extra top-level key (e.g. `{"notes": "..."}`) parses OK; nested `AvlEntry`/`Citation` keep `extra="forbid"`.
- [ ] **Step 2: Run → FAIL** (outer `extra="forbid"` currently turns a valid resolution into `unresolved`).
- [ ] **Step 3:** Set the outer `ResolverLlmResponse` to `extra="ignore"`; keep `forbid` on the nested models.
- [ ] **Step 4: Run → PASS.**

### Task 4.3: Invariants at the right layer (architect Finding 4 — no cross-layer import)

**Files:** `app/schemas/spec_codes.py` (`Citation.url`), `app/models/sourcing.py` (`OemSpecCodePending`, `OemSpecCode`), `app/constants.py` (`SpecCodeSource`).

- [ ] **Step 1: Tests** — `Citation` accepts `http(s)` via `urlparse` and rejects `javascript:`, `data:`, and leading-whitespace tricks; `OemSpecCodePending.llm_confidence=42.0` rejected; `OemSpecCode.source` accepts only `SpecCodeSource` values; a `OemSpecCodePending.citations` entry with a non-`http(s)` `url` is rejected at the model layer.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3 — keep model/schema layers separate (do NOT import Pydantic `Citation` into `app/models/`):**
  - In `app/schemas/spec_codes.py`: change `Citation`'s scheme check to `urllib.parse.urlparse(v.strip()).scheme.lower() in {"http","https"}`.
  - In `app/constants.py`: add `SpecCodeSource(StrEnum)` (`TABLE/LLM/NONE`).
  - In `app/models/sourcing.py`: `@validates("llm_confidence")` (0.0–1.0, pure numeric); `@validates("source")` (against `SpecCodeSource`); `@validates("citations")` doing a **structural** check only — each item is a dict with a `url`, and `urllib.parse.urlparse(url.strip()).scheme in {"http","https"}` — using `urllib.parse` directly, **not** importing `Citation`. Full citation validation stays in the schema layer.
- [ ] **Step 4: Run → PASS.**

### Task 4.4: Admin UX consistency (design review)

**Files:** `app/routers/admin/spec_codes.py`, `app/templates/htmx/partials/admin/spec_codes_pending.html`.

- [ ] **Step 1: Test** — approve/reject responses include an `HX-Trigger` toast header (match the `showToast` pattern in `app/routers/requisitions2.py`).
- [ ] **Step 2: Run → FAIL** (currently return empty body → row vanishes silently).
- [ ] **Step 3:** Emit `HX-Trigger: {"showToast": {...}}` on approve/reject; render the "No pending mappings" empty-state when the last row is removed (OOB swap); change the Reject button `red-*` → safelisted `rose-*` (per the Tailwind-safelist memory).
- [ ] **Step 4: Run admin tests → PASS.**

### Task 4.5: Coverage + comment-rot cleanup (helper refactors CUT)

**Files:** `tests/test_search_service_with_spec_resolver.py`, plus comment sites. **Cut:** the `_pending_result` / `_enqueue_avl_mpn` extractions (refactor churn — defer to a dedicated cleanup, not this landing PR).

- [ ] **Step 1:** Add the AVL connector-crash branch test (force `_fetch_fresh` to raise; assert workers still enqueue + sightings persist) and a non-IBM `oem_hint` test.
- [ ] **Step 2:** Fix stale comments: remove "wired in a later PR" header in `spec_code_resolver.py`; drop the "schema-validation failure" raising-case from the `re_resolve` docstring; fix the `queue_manager.py` worker-propagation claim; add `"opus"` to `claude_client.py` `model_tier` docs; replace absolute line-number cross-refs with symbol names.
- [ ] **Step 3:** Green slice: `pytest tests/ -k "spec_code or spec_resolver or sourcing" -q --override-ini="addopts="`.

### Task 4.6: Resolve the migration fork, then merge bottom-up (architect Finding 1 — BLOCKER)

- [ ] **Step 1 — kill the fork at the source.** When rebasing `feat/spec-resolver-1-migration` (#170) onto post-#183 `main`, **edit its migration's `down_revision` from `"084_description"` to `"085_utcdatetime_tz"`** so the chain is linear (no two-head fork). Verify: `alembic heads` shows a single head after the rebase. (Preferred over a merge migration.)
- [ ] **Step 2 — merge in order, each after CI green and a base update:**
```bash
for n in 167 170 171 172 173 175; do
  gh pr update-branch "$n"; gh pr checks "$n"      # green
  gh pr merge "$n" --squash --delete-branch
done
```
- [ ] **Step 3 — fallback if Step 1 wasn't done before merge:** if `main` ends with two heads (`alembic heads` shows 2), `alembic merge heads -m "merge_utcdatetime_and_spec_resolver"` → commit → PR → merge; confirm single head. The spec-resolver-6 flag-flip (`archive/feat/spec-resolver-6-enable`) follows as its own small PR if still wanted.

---

## Phase 5 — Deferrals (decide, don't drift)

### Task 5.1: #143 tailwind v4 — its own plan, don't bump-merge
- [ ] Write `docs/superpowers/plans/2026-06-XX-tailwind-v4.md` (the `@tailwindcss/postcss` plugin, `@import "tailwindcss"`, 26 `@apply` rules + 2 `@layer` blocks, `brand-*` → `@theme {}` for the 156 templates, safelist rework). Close #143 pointing at it.

### Task 5.2: #130 / #129 docker base-image majors — close
- [ ] Green checks are vacuous (CI pins py3.12 / node20; the Dockerfile is never built; #129 is 115 commits stale with unrelated reverts). **Close both** with that rationale; add `ignore` rules for base-image majors to `.github/dependabot.yml` so they stop reopening.

---

## Phase 6 — Deploy (after Phases 1–4 land on main)

### Task 6.1: Validate the FULL pending migration chain on real PostgreSQL (architect Finding 7)

By now main has 2+ new migrations (`085_utcdatetime_tz`, the spec-resolver migration, and a merge migration only if Task 4.6 Step 3 was used). Migration 085 has never run on real PG.

- [ ] **Step 1:** On a disposable PG (or a DB snapshot), exercise the **whole pending chain**:
```bash
alembic upgrade head            # applies 085 + spec-resolver (+ merge)
alembic downgrade -3            # or `downgrade base` against a snapshot
alembic upgrade head
```
Expected: clean. Spot-check: `\d+ requisitions` shows `created_at` = `timestamp with time zone`; the new `oem_spec_codes*` tables exist.
- [ ] **Step 2:** If anything fails, fix on a branch → PR → merge before deploying.

### Task 6.2: Deploy
- [ ] **Step 1:** From `main`: `./deploy.sh` (`--no-cache --force-recreate`; runs Alembic at entrypoint).
- [ ] **Step 2:** Verify health; if `could not translate host name "db"` crash-loop → `docker compose down && up` (per memory). Spot-check a datetime-heavy view (sightings age, offers expiry) renders aware-UTC correctly.

---

## Self-review notes (rev. 2)

- **Coverage:** every open PR has a task or explicit close. ✓
- **Architect blockers fixed:** migration fork (Task 4.6 Step 1 rebases `down_revision`; Step 3 fallback merge migration); `flush()` race (Task 4.1 `begin_nested()` savepoint); cross-layer import avoided (Task 4.3 keeps Pydantic out of models); Phase-2 branch-explicit; Phase-6 tests the full chain. ✓
- **Simplify cuts applied:** Phase 1 collapsed to one task; Task 2.1 absorbed the constant + dropped its trivial test; response-invariant demoted to optional/non-blocking; Task 4.5 helper extractions cut; frprp note inlined (no extra PR). ✓
- **Correctness/security fixes preserved:** caller-owned transaction, silent-failure `extra`, `javascript:`-URL rejection, migration-085 real-PG gate — all kept. ✓
- **Deploy gate:** full-chain real-PG cycle test precedes `./deploy.sh`. ✓
