# Activity Timeline — Plan 5: Canonical `call_logged` + 8x8 Enablement

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Make every phone-call activity event — manual calls and 8x8 CDR-poll calls — use the canonical `ActivityType.CALL_LOGGED` event type, with inbound/outbound carried on the existing `direction` column, so calls render correctly on the unified timeline. Plus: document the operator steps to actually turn 8x8 on.

**Background:** 8x8 call logging is fully built but gated by `eight_by_eight_enabled` (default `False`, env-driven). The CDR poll already routes through `log_call_activity()` and links calls to requisitions — but `log_call_activity()` writes `activity_type = f"call_{direction}"` → `"call_outbound"` / `"call_inbound"`, which are **not** the canonical `ActivityType.CALL_LOGGED = "call_logged"` the timeline (Plans 1/4/6) standardizes on. This plan fixes that; call direction moves to the `direction` column (already populated). The DB is intentionally empty — no historical-row migration needed; the change is forward-only.

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy 2.0, pytest, Loguru.

**Spec:** `docs/superpowers/specs/2026-05-20-activity-timeline-design.md` (build step 5).

**Branch:** Create `feat/activity-timeline-5` off `feat/activity-timeline-4` (or `main` if Plans 1–4 merged).

---

### Task 1: Canonicalize phone-call activity to `call_logged`

`log_call_activity()` (and any sibling call-logging writers) in `app/services/activity_service.py` must write `activity_type = ActivityType.CALL_LOGGED`. Inbound/outbound is already stored in the `direction` column — keep that. Every reader that currently keys on the raw strings `"call_outbound"` / `"call_inbound"` must be updated to key on `activity_type == "call_logged"` + the `direction` column.

**Files:**
- Modify: `app/services/activity_service.py`
- Modify: `app/services/avail_score_service.py` (reads `call_outbound` — confirm with grep)
- Modify: any other reader found by the grep sweep in Step 3
- Test: `tests/test_activity_write_path.py` and the existing call/score test files

- [ ] **Step 1: Grep the blast radius**

Run and record the full output:
```bash
grep -rn "call_outbound\|call_inbound\|call_{direction}\|f\"call_" app/ tests/
```
Every hit is either a **writer** (`f"call_{direction}"` — must change to `CALL_LOGGED`) or a **reader** (a filter/comparison on `"call_outbound"`/`"call_inbound"` — must change to `activity_type == "call_logged"` and, where in/out matters, an additional `direction` check). List every file:line and classify each. The dossier identified at least: `app/services/activity_service.py` (the `log_call_activity` writer ~line 239, plus sibling call writers ~463 and ~577, plus a `legacy_outbound` reference ~396) and `app/services/avail_score_service.py` (~451, ~736). The grep is authoritative — handle EVERY hit.

- [ ] **Step 2: Write the failing test**

Append to `tests/test_activity_write_path.py`:

```python
def test_log_call_activity_writes_canonical_call_logged(db_session, test_user):
    """Phone calls are logged as the canonical call_logged type; direction holds in/out."""
    rec = log_call_activity(
        user_id=test_user.id,
        direction="outbound",
        phone="+15551234567",
        duration_seconds=120,
        external_id="call-canon-001",
        contact_name="Vendor Rep",
        db=db_session,
    )
    assert rec is not None
    assert rec.activity_type == ActivityType.CALL_LOGGED
    assert rec.direction == "outbound"
```

Verify `log_call_activity` and `ActivityType` are imported in the test file (reuse existing imports; Plan 1 added `log_call_activity` tests there).

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_activity_write_path.py -k call_logged -v --override-ini="addopts="`
Expected: FAIL — `activity_type` is `"call_outbound"`, not `"call_logged"`.

- [ ] **Step 3: Confirm the failure reason.**

- [ ] **Step 4: Update the writers**

In `app/services/activity_service.py`, in `log_call_activity()` change the line that builds `activity_type` from `f"call_{direction}"` to `ActivityType.CALL_LOGGED`. The `direction` column is already set from the `direction` parameter — leave it. Apply the identical change to every other call-logging writer the Step 1 grep found in this file (the dossier noted sibling writers ~lines 463 and ~577 that also use `f"call_{direction}"`). Confirm `ActivityType` is imported (Plan 4 added the import).

- [ ] **Step 5: Update every reader**

For each reader the Step 1 grep classified (e.g. `app/services/avail_score_service.py` ~451/~736, and the `legacy_outbound` handling in `activity_service.py` ~396): change a filter/comparison of `activity_type == "call_outbound"` (or `in ("call_outbound","call_inbound")`) to `activity_type == "call_logged"`, adding a `direction == "outbound"`/`"inbound"` condition only where the reader genuinely distinguishes the two. Where a reader treated both call strings the same (just "is this a call"), `activity_type == "call_logged"` alone is the replacement. Read each reader's surrounding logic — do not blindly substitute; preserve intent (especially the AVAIL scoring logic in `avail_score_service.py`).

If a reader is purely a backward-compat shim for old data (`legacy_outbound`), and the DB is empty so no old rows exist: simplify it to the canonical form rather than carrying the legacy string forward — but if removing it risks breaking a code path, keep a comment explaining and handle both. Prefer the clean canonical form (no band-aids).

- [ ] **Step 6: Update existing tests**

The Step 1 grep will have hit test files asserting `call_outbound`/`call_inbound`. Update each to expect `call_logged` + the `direction` column. Run `grep -rn "call_outbound\|call_inbound" tests/` after your edits — there should be no stale assertions left (except deliberately-kept legacy-compat tests, if any — justify them).

- [ ] **Step 7: Run the tests**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_activity_write_path.py tests/test_8x8_jobs.py tests/test_8x8_service.py tests/test_services_activity.py tests/test_avail_score_service.py -v --override-ini="addopts="
```
(Adjust the avail-score test filename to the real one.) Expected: PASS — the new test passes; all call/8x8/score tests pass with the canonical type. Then a broad sweep:
```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ -k "call or activity or 8x8 or score" --override-ini="addopts=" -q
```
Investigate any failure that is not pre-existing xdist pollution (compare against a `git stash` baseline).

- [ ] **Step 8: Lint and commit**

`ruff check` the modified files — fix issues. Commit hygiene: `git status --short` first; do NOT stage `Caddyfile`.
```bash
git add app/services/activity_service.py app/services/avail_score_service.py tests/test_activity_write_path.py <other modified files>
git commit -m "feat: phone calls log canonical call_logged activity type (direction on its column)"
```
`git show --stat HEAD` — confirm the staged set matches the grep blast radius.

---

### Task 2: APP_MAP doc + operator-enablement note

- [ ] **Step 1: Update the APP_MAP doc**

In `docs/APP_MAP_INTERACTIONS.md`, in the activity-logging section and the 8x8 section: note that phone calls (manual and 8x8 CDR-poll) now log the canonical `ActivityType.CALL_LOGGED` type with inbound/outbound on the `direction` column. Match the doc's style.

- [ ] **Step 2: Add the operator-enablement section**

Append a short subsection to `docs/PRE_ROLLOUT_CHECKLIST.md` (or, if that file does not exist, to the 8x8 area of `docs/APP_MAP_INTERACTIONS.md`) titled "Enabling 8x8 call logging", listing the operator/ops steps — these are NOT code and must be done by an operator with 8x8 credentials:
  - Set in `.env`: `EIGHT_BY_EIGHT_ENABLED=true`, `EIGHT_BY_EIGHT_API_KEY=…`, `EIGHT_BY_EIGHT_USERNAME=…`, `EIGHT_BY_EIGHT_PASSWORD=…`, `EIGHT_BY_EIGHT_PBX_ID=…` (timezone/poll-interval have defaults).
  - Per user who should have calls logged: set their `eight_by_eight_extension` and enable their per-user `eight_by_eight_enabled` toggle (in the user settings UI).
  - On restart, `register_eight_by_eight_jobs()` registers the CDR poll (every `EIGHT_BY_EIGHT_POLL_INTERVAL_MINUTES`, default 30). Calls reverse-matched to a CRM company with an open requisition land on that requisition's Activity tab as `call_logged` events.

- [ ] **Step 3: Commit**

```bash
git add docs/APP_MAP_INTERACTIONS.md docs/PRE_ROLLOUT_CHECKLIST.md
git commit -m "docs: canonical call_logged note + 8x8 operator-enablement steps"
```

---

## Self-Review

**Spec coverage (build step 5 — "enable 8x8"):**
- The code half — making 8x8 (and manual) calls log the canonical `call_logged` type so they render on the timeline → Task 1 ✓
- The ops half — flipping the env flag + credentials + per-user setup — is genuinely operator action; documented in Task 2 ✓ (cannot be done from code; the `eight_by_eight_enabled` default stays `False`, which is correct for environments without 8x8 credentials).

**Why the code task exists:** the spec called build step 5 "config/ops only", but discovery found `log_call_activity()` emits `call_outbound`/`call_inbound`, not the canonical `call_logged`. Without Task 1, enabling 8x8 would write non-canonical rows the Plan-6 timeline can't classify. Task 1 is the root-cause fix (no band-aid): canonical type + `direction` column, all readers updated.

**Blast radius:** Task 1 Step 1's grep is authoritative — every `call_outbound`/`call_inbound` writer and reader is enumerated and handled; Step 6 ensures no stale test assertions remain.

**No migration:** the DB is intentionally empty — no historical `call_outbound` rows to migrate; the change is forward-only.

**Scope:** Plan 5 does not flip the `eight_by_eight_enabled` default (env-driven, correctly `False` by default). Frontend timeline polish is Plan 6.
