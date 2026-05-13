# Buy Plan System — Post-Cleanup Audit Report

**Date:** 2026-05-13
**Branch:** `claude/review-buy-plan-system-t484q`
**Scope:** Full audit of models, services, routes, templates, jobs, tests, and cross-references after admin cleanup pass.

---

## Executive Summary

The Buy Plan system is functional end-to-end for the happy path (build → submit → approve → PO → verify → complete). However the audit found **5 bugs**, **10 dead code / orphaned feature issues**, **8 code quality violations**, and **3 performance concerns**. The token-based approval feature was not cleaned up. Several template branches reference V1 status values that can never match.

---

## 1. BUGS (will cause runtime errors or incorrect behavior)

### BUG-1: `build_buy_plan_htmx` missing `user` in template context — will crash

**File:** `app/routers/htmx_views.py` lines 7768–7772
**Template:** `app/templates/htmx/partials/buy_plans/detail.html` lines 17, 21

The `build_buy_plan_htmx` route constructs the context but never sets `ctx["user"] = user`. The detail template accesses `user.id` (line 17: `selectattr('buyer_id', 'eq', user.id)`) and `user.role` (line 21). `_base_ctx()` only provides `user_name` and `user_email`, not the `User` ORM object. This will raise `jinja2.exceptions.UndefinedError` when rendering `detail.html` via the "Build Buy Plan" button on the quote page.

**Fix:** Add `ctx["user"] = user` at line 7769.

### BUG-2: `is_stock_sale` never included in list dict — stock badge silently hidden

**File:** `app/routers/htmx_views.py` lines 5904–5920
**Template:** `app/templates/htmx/partials/buy_plans/list.html` line 111

The list partial route builds plain dicts for each buy plan. The dict never includes `is_stock_sale`. The template checks `bp.is_stock_sale` — Jinja2 dict key lookup returns `undefined` (falsy), so the stock badge never renders on the list view. No crash, but data silently missing.

**Fix:** Add `"is_stock_sale": p.is_stock_sale or False` to the dict at line 5904.

### BUG-3: Inline cancel route bypasses service layer — no notification, no activity log

**File:** `app/routers/htmx_views.py` lines 6175–6197

The cancel route sets `bp.status = BuyPlanStatus.CANCELLED.value` directly on the ORM object and commits. No service function call, no notification dispatch, no `ActivityLog` entry, no cascade to line statuses. Every other mutation route (submit, approve, verify-so, confirm-po, verify-po, flag-issue, reset) delegates to `buyplan_workflow.*` and fires a notification. Cancel is the only route that bypasses both layers. A cancelled plan leaves orphaned `awaiting_po` lines and buyer tasks.

**Fix:** Extract into `buyplan_workflow.cancel_buy_plan()`, add `notify_cancelled()`, cancel all active lines.

### BUG-4: `verify_po_sent` uses `db.commit()` instead of `db.flush()`

**File:** `app/services/buyplan_workflow.py` line 867

Every other workflow function uses `db.flush()` and lets the caller commit. `verify_po_sent` calls `db.commit()` directly, which commits the caller's entire transaction context prematurely. If the caller has other pending changes, they get committed unexpectedly.

**Fix:** Change to `db.flush()` (as `verify_po_sent_v3` already does at line 978).

### BUG-5: Empty-state colspan mismatch in detail table

**File:** `app/templates/htmx/partials/buy_plans/detail.html` line 359

The line items table has 7 columns (Part, Description, Vendor, Qty, Unit Cost, Status, Action). The empty-state row uses `colspan="6"` instead of `colspan="7"`, causing misalignment.

**Fix:** Change to `colspan="7"`.

---

## 2. DEAD CODE / ORPHANED FEATURES

### DEAD-1: Token-based approval infrastructure — not cleaned up

The token approval flow was apparently removed (no router exists) but all supporting code remains:

| Location | What |
|---|---|
| `app/models/buy_plan.py:129–130` | `approval_token` + `token_expires_at` columns |
| `app/models/buy_plan.py:167` | Index `ix_bpv3_token` on `approval_token` |
| `app/services/buyplan_workflow.py:77–78, 419–420` | Token generated on every submit/resubmit |
| `app/services/proactive_service.py:529–530` | Token generated on proactive offer conversion |
| `app/services/buyplan_notifications.py:554–623` | `notify_token_approved` + `notify_token_rejected` — no callers |
| `app/schemas/buy_plan.py:112–118` | `BuyPlanTokenApproval` + `BuyPlanTokenReject` schemas — unused |
| `app/services/buyplan_service.py:34–37` | Re-exports of token notification functions |
| `app/main.py:272` | CSRF exemption for `/api/buy-plans/token/.*` — route doesn't exist |
| `tests/test_buyplan_token.py:117` | Comment: "most tests removed (endpoint deleted in CRM redesign)" |

**Decision needed:** Either rebuild the token approval endpoint, or remove all of the above.

### DEAD-2: `verify_po_sent_v3` — duplicate of `verify_po_sent`

**File:** `app/services/buyplan_workflow.py` lines 874–979

Both functions scan Graph API sent folders for PO emails. Different return shapes (`list[dict]` vs `dict[str, dict]`). `verify_po_sent_v3` docstring references `routers/crm/buy_plans.py GET /api/buy-plans/{plan_id}/verify-po` — a route that does not exist. Neither function is called from any router (only from the background job).

**Fix:** Delete `verify_po_sent_v3`, keep `verify_po_sent` (which the job uses).

### DEAD-3: `notify_stock_sale_approved` — never called

**File:** `app/services/buyplan_notifications.py` lines 467–551

Defined and re-exported but no router or service calls it. The stock sale auto-approve path sets `plan.is_stock_sale = True` but never dispatches this notification.

### DEAD-4: `BUY_PLAN_TRANSITIONS` in status machine — never used

**File:** `app/services/status_machine.py` lines 44–51

Defines valid transitions but `validate_transition("buy_plan", ...)` is never called by any buy plan code. All transition validation is inline `if plan.status != X` checks.

### DEAD-5: `ActivityLog.buy_plan_id` FK — always NULL

**Migration:** `021_activity_log_buy_plan_id.py`

Column exists but `buyplan_notifications.py` never sets it. All activity entries use `requisition_id` and put the plan id in `subject`/`notes` text.

### DEAD-6: All "Called by" docstring headers reference phantom file

Every buy plan module header says "Called by: routers/crm/buy_plans.py". That file has never existed. Actual caller is `app/routers/htmx_views.py`.

Files affected: `buyplan_service.py:11`, `buyplan_workflow.py:6,883`, `buyplan_builder.py:5`, `buyplan_notifications.py:17`, `models/buy_plan.py:21`, `schemas/buy_plan.py:13`, `status_machine.py:7`.

### DEAD-7: Deprecated import path in `verify_po_sent` and notifications

**Files:** `buyplan_workflow.py:797`, `buyplan_notifications.py:122,473`

Import `get_valid_token` from `..scheduler` instead of `..utils.token_manager`. The scheduler module itself says production code should use `token_manager`.

### DEAD-8: Tombstone log line in stock autocomplete job

**File:** `app/jobs/inventory_jobs.py:481`

`logger.debug("Teams stock match notification skipped (removed)")` — leftover from removed notification path. Delete.

### DEAD-9: `_job_stock_autocomplete` bypasses all notification and audit trail

**File:** `app/jobs/inventory_jobs.py` lines 90–93

Force-completes stuck stock sales by setting `plan.status = completed` directly. No `notify_completed`, no `notify_stock_sale_approved`, no `generate_case_report`, no `ActivityLog` entry. Completed stock-sale plans leave no audit trail.

### DEAD-10: `_generate_buyer_tasks` swallows all exceptions silently

**File:** `app/services/buyplan_workflow.py` lines 429–453

Bare `except Exception` catches everything with only `logger.warning`. If task generation fails, no indication of which plan or line failed. Silent data loss.

---

## 3. CODE QUALITY

### QUAL-1: V1 status values in templates — unreachable branches

**`app/templates/htmx/partials/buy_plans/list.html` lines 81–101:**

Progress bar logic uses `pending_approval`, `approved`, `po_entered`, `po_confirmed`, `complete` — none exist in `BuyPlanStatus` enum. The V4 values are: `draft`, `pending`, `active`, `halted`, `completed`, `cancelled`.

**`app/templates/htmx/partials/requisitions/tabs/buy_plans.html` lines 34–44:**

`bp_colors` dict maps `pending_approval`, `approved`, `rejected` — none are V4 values. `pending` and `halted` have no color mapping and fall through to grey.

### QUAL-2: Raw status strings instead of enum constants

| File | Lines | Raw string used |
|---|---|---|
| `buyplan_builder.py` | 57 | `"won"`, `"sent"` (should be `QuoteStatus.*`) |
| `buyplan_builder.py` | 65 | `["cancelled"]` (should be `BuyPlanStatus.CANCELLED.value`) |
| `buyplan_builder.py` | 134, 353 | `"active"` (should be `OfferStatus.ACTIVE.value`) |
| `vendor_score.py` | 29–31, 164 | `"cancelled"`, `"pending"`, `"active"`, `"completed"` |
| `avail_score_service.py` | 136 | `("completed",)` |
| `multiplier_score_service.py` | 102 | `("completed",)` |
| `buyer_leaderboard.py` | 70 | `("completed",)` |
| `proactive_service.py` | 647, 726 | `["active", "completed"]` |
| `routers/crm/quotes.py` | 279, 321 | `"draft"` (enum is already imported) |

CLAUDE.md rule: "Always use StrEnum constants from `app/constants.py`, never raw strings."

### QUAL-3: `HALTED` status inconsistent across scoring services

`BuyPlanStatus.HALTED` exists in the enum but is absent from all scoring status sets. In `vendor_score.py`, the SQL filter excludes only `"cancelled"` (passing halted through), but the Python-level `AWARDED_STATUSES` set also excludes halted. The SQL and Python layers disagree.

### QUAL-4: Mypy `no-any-return` errors (13 total)

All in `buyplan_workflow.py` and `buyplan_scoring.py`. Caused by `db.get()` returning `Any` in SQLAlchemy stubs. Functions declare `-> BuyPlan` but return `Any`.

**Fix:** Add explicit type annotations: `plan: BuyPlan | None = db.get(BuyPlan, plan_id)`.

### QUAL-5: `reset_buy_plan_to_draft` missing `db.flush()`

**File:** `app/services/buyplan_workflow.py` line 371

Every sibling function ends with `db.flush(); return plan`. This one just `return plan`. If called without the route's explicit `db.commit()`, changes silently don't persist.

### QUAL-6: Missing ORM relationships on FK columns

**File:** `app/models/buy_plan.py`

Three FK columns exist without corresponding `relationship()`:
- `BuyPlan.cancelled_by_id` (line 120) — no `cancelled_by` relationship
- `BuyPlan.halted_by_id` (line 122) — no `halted_by` relationship
- `BuyPlanLine.po_verified_by_id` (line 211) — no `po_verified_by` relationship

These FKs are written to in workflow functions but can never be traversed via ORM (e.g., `plan.cancelled_by.name` would fail). Not currently needed but a latent trap.

### QUAL-7: `issue_type` column has no `@validates` guard

**File:** `app/models/buy_plan.py` line 216

`BuyPlanLine` validates `status` via `@validates` but not `issue_type`. Invalid values can be written to DB if the Pydantic schema is bypassed (e.g., direct service-layer calls).

### QUAL-8: `halt` action leaves `so_status` as `rejected` with no distinction

**File:** `app/services/buyplan_workflow.py` lines 172–178

When SO is halted, `so_status` is set to `SOVerificationStatus.REJECTED` (same as a plain reject). No `HALTED` value exists in `SOVerificationStatus`. The only way to distinguish halt from reject is checking `plan.status == BuyPlanStatus.HALTED`. Coherent but confusing — document this.

Every sibling function ends with `db.flush(); return plan`. This one just `return plan`. If called without the route's explicit `db.commit()`, changes silently don't persist.

---

## 4. PERFORMANCE

### PERF-1: N+1 queries in `generate_case_report`

**File:** `app/services/buyplan_workflow.py` lines 679–681

```python
for line in lines:
    offer = line.offer or (db.get(Offer, line.offer_id) if line.offer_id else None)
```

`check_completion` calls this with `joinedload(BuyPlan.lines)` but not `BuyPlanLine.offer`. Each line fires a separate `db.get(Offer, ...)`.

### PERF-2: N+1 queries in `detect_favoritism`

**File:** `app/services/buyplan_workflow.py` lines 627–628

```python
buyer = db.get(User, buyer_id)  # inside loop over buyer_counts.items()
```

### PERF-3: N+1 queries in `notify_approved`

**File:** `app/services/buyplan_notifications.py` line 261

```python
buyers = [db.get(User, bid) for bid in buyer_ids]
```

Replace with `db.query(User).filter(User.id.in_(buyer_ids)).all()`.

---

## 5. TEST STATUS

Tests are currently running. Summary will be appended when complete.

### Known test file inventory (11 files, ~7,400 lines):

| File | Lines | Coverage |
|---|---|---|
| `test_buy_plan_service.py` | 2,536 | Scoring, assignment, build, flags, workflow |
| `test_buy_plan_models.py` | 343 | Enum + ORM validation |
| `test_buyplan_workflow.py` | 1,442 | Duplicate of service tests |
| `test_buyplan_workflow_bugs.py` | 300 | Edge cases |
| `test_buyplan_builder_guards.py` | 214 | Build guards |
| `test_buyplan_scoring.py` | 532 | Scoring unit tests |
| `test_buyplan_notifications.py` | 1,034 | Notification coverage |
| `test_buyplan_v3_notifications.py` | 426 | Duplicate notification tests |
| `test_buyplan_po_verify.py` | 194 | PO verification |
| `test_buyplan_v3_po_verify.py` | 263 | Duplicate PO verification |
| `test_buyplan_token.py` | 163 | Mostly deleted; only token generation test remains |

### Duplicate test concerns:

- `test_buyplan_po_verify.py` and `test_buyplan_v3_po_verify.py` cover the same function
- `test_buyplan_notifications.py` and `test_buyplan_v3_notifications.py` overlap heavily
- `test_buyplan_workflow.py` and `test_buy_plan_service.py` share identical class names (`TestSubmitBuyPlan`, `TestApproveBuyPlan`, etc.)

---

## Priority Order for Fixes

| Priority | Item | Risk |
|---|---|---|
| P0 | BUG-1: `build_buy_plan_htmx` missing `user` in context | Runtime crash on build |
| P0 | BUG-3: Cancel route bypasses service layer | Silent data corruption |
| P1 | BUG-4: `verify_po_sent` uses `db.commit()` | Transaction leak |
| P1 | DEAD-1: Decide on token approval (keep or delete) | Security surface + dead code |
| P1 | QUAL-1: V1 status values in templates | Progress bars wrong |
| P2 | BUG-2: `is_stock_sale` missing from list dict | Cosmetic data gap |
| P2 | BUG-5: colspan mismatch | Visual glitch |
| P2 | DEAD-2: Delete `verify_po_sent_v3` | Dead code |
| P2 | QUAL-2: Raw status strings | Enum consistency |
| P3 | PERF-1/2/3: N+1 queries | Performance |
| P3 | All remaining DEAD items | Cleanup |
