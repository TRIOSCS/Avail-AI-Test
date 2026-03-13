# Phase 2: Merge Buy Plans Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Consolidate Buy Plans v1 and v3 into a single unified system using v3 as the base.

**Architecture:** V3 is the canonical system. Add v1-only features (token-based approval, PO verification scanning) to v3. Redirect v1 endpoints to v3. Delete v1-specific code.

**Tech Stack:** Python/FastAPI, SQLAlchemy, Alembic, pytest

**Spec:** `docs/superpowers/specs/2026-03-13-mvp-strip-down-design.md` (Phase 2 section)

---

## File Inventory

### V1 files (to merge or delete)
- `app/routers/crm/buy_plans.py` — V1 router (token endpoints, CRUD)
- `app/services/buyplan_service.py` — V1 re-export facade
- `app/services/buyplan_notifications.py` — V1 notifications (email/Teams/in-app)
- `app/services/buyplan_po.py` — PO email verification + auto-complete stock sales
- `app/models/quotes.py` — contains `BuyPlan` model (v1, table `buy_plans`)
- `app/schemas/crm.py` — V1 schemas (`BuyPlanSubmit`, `BuyPlanApprove`, `BuyPlanReject`, etc.)

### V3 files (canonical, to keep/extend)
- `app/routers/crm/buy_plans_v3.py` — V3 router
- `app/services/buy_plan_v3_service.py` — V3 service layer
- `app/services/buyplan_v3_notifications.py` — V3 notifications
- `app/services/buyplan_builder.py` — AI plan builder
- `app/services/buyplan_scoring.py` — AI scoring
- `app/services/buyplan_workflow.py` — V3 workflow state machine
- `app/models/buy_plan.py` — `BuyPlanV3` model (table `buy_plans_v3`), `BuyPlanLine`, `VerificationGroupMember`
- `app/schemas/buy_plan.py` — V3 schemas

### Test files
- `tests/test_buy_plans.py` — V1 router tests
- `tests/test_buyplan_service.py` — V1 service tests
- `tests/test_buy_plan_v3_router.py` — V3 router tests
- `tests/test_buy_plan_service_v3.py` — V3 service tests
- `tests/test_buy_plan_v3_service.py` — V3 service tests (duplicate?)
- `tests/test_buy_plan_models.py` — model tests
- `tests/test_buy_plan_schemas.py` — schema tests
- `tests/test_buyplan_scoring.py` — scoring tests
- `tests/test_buyplan_builder_guards.py` — builder guard tests
- `tests/test_buyplan_workflow_bugs.py` — workflow bug tests
- `tests/test_buyplan_v3_notifications.py` — V3 notification tests

### Frontend
- `app/static/crm.js` — ~30 references to `/api/buy-plans/` (v1) and `/api/buy-plans-v3/` (v3)

### Current migration head: `074`

---

## Task 1: Add token-based approval endpoints to V3 router

**Goal:** Port the v1 token-based email approval flow to v3 so managers can approve/reject buy plans via emailed links without logging in.

**V1 reference:** `app/routers/crm/buy_plans.py` lines 359-460 (token endpoints)

### Pre-work: Check model columns
- [ ] Confirm `BuyPlanV3` in `app/models/buy_plan.py` does NOT have `approval_token` or `token_expires_at` (confirmed: absent)
- [ ] Confirm `BuyPlan` in `app/models/quotes.py` DOES have them (confirmed: lines 129-130)

### Step 1.1: Write tests (TDD — red)
- [ ] Create `tests/test_buyplan_v3_token.py` with:
  - `test_token_generated_on_submit` — submit a v3 plan, verify `approval_token` and `token_expires_at` populated
  - `test_get_plan_by_token` — GET `/api/buy-plans-v3/token/{token}` returns plan without auth
  - `test_approve_by_token` — PUT `/api/buy-plans-v3/token/{token}/approve` with SO# transitions to `active`
  - `test_reject_by_token` — PUT `/api/buy-plans-v3/token/{token}/reject` with reason transitions to draft (rejected)
  - `test_expired_token_rejected` — token with past `token_expires_at` returns 410
  - `test_invalid_token_404` — random token returns 404
  - `test_token_invalidated_after_use` — after approve, same token returns 404
  - `test_stock_sale_token_approve_auto_completes` — stock sale approve → completed
- [ ] Run tests — all should FAIL (endpoints and columns don't exist)

### Step 1.2: Add model columns
- [ ] Add to `BuyPlanV3` in `app/models/buy_plan.py`:
  ```python
  approval_token = Column(String(100), unique=True)
  token_expires_at = Column(DateTime)
  ```
- [ ] Add index: `Index("ix_bpv3_token", "approval_token")` in `__table_args__`

### Step 1.3: Create Alembic migration
- [ ] Run: `alembic revision --autogenerate -m "add approval_token to buy_plans_v3"`
- [ ] Expected migration number: `075`
- [ ] Review generated migration — should only add 2 columns + 1 index
- [ ] Test: `alembic upgrade head` then `alembic downgrade -1` then `alembic upgrade head`

### Step 1.4: Add token approval schemas
- [ ] Add to `app/schemas/buy_plan.py`:
  ```python
  class BuyPlanV3TokenApproval(BaseModel):
      sales_order_number: str
      notes: str | None = None

  class BuyPlanV3TokenReject(BaseModel):
      reason: str = ""
  ```

### Step 1.5: Add token endpoints to V3 router
- [ ] Add to `app/routers/crm/buy_plans_v3.py` (before `/{plan_id}` routes to avoid path conflicts):
  - `GET /api/buy-plans-v3/token/{token}` — public, no auth, returns `_plan_to_dict(plan)`
  - `PUT /api/buy-plans-v3/token/{token}/approve` — public, accepts `BuyPlanV3TokenApproval`, sets status=active, invalidates token
  - `PUT /api/buy-plans-v3/token/{token}/reject` — public, accepts `BuyPlanV3TokenReject`, resets to draft, invalidates token
- [ ] Handle stock sale fast-track: if `is_stock_sale`, approve → completed

### Step 1.6: Generate token on submit
- [ ] In `app/services/buy_plan_v3_service.py` `submit_buy_plan()`: generate `secrets.token_urlsafe(32)`, set `token_expires_at` to 30 days
- [ ] In `app/services/buy_plan_v3_service.py` `resubmit_buy_plan()`: same token generation

### Step 1.7: Run tests (TDD — green)
- [ ] Run `tests/test_buyplan_v3_token.py` — all should PASS
- [ ] Run full suite — no regressions

### Step 1.8: Commit
- [ ] `git add app/models/buy_plan.py app/routers/crm/buy_plans_v3.py app/schemas/buy_plan.py app/services/buy_plan_v3_service.py alembic/versions/075_*.py tests/test_buyplan_v3_token.py`
- [ ] `git commit -m "feat: add token-based approval endpoints to Buy Plan V3"`

---

## Task 2: Add PO verification scanning to V3 workflow

**Goal:** Port the Graph API PO verification scan from v1 (`buyplan_po.py`) to work with v3's `BuyPlanLine` rows instead of JSON `line_items`.

**V1 reference:** `app/services/buyplan_po.py` — `verify_po_sent()` scans buyer's Outlook sent folder for PO numbers

### Step 2.1: Write tests (TDD — red)
- [ ] Create `tests/test_buyplan_v3_po_verify.py` with:
  - `test_verify_po_found_in_sent_folder` — mock Graph API, PO found, line marked `verified`
  - `test_verify_po_not_found` — PO not in sent folder, line stays `pending_verify`
  - `test_verify_po_graph_error` — Graph API error handled gracefully, returns error reason
  - `test_verify_po_all_verified_auto_completes` — all lines verified → plan status `completed`
  - `test_verify_po_no_buyer_skips_line` — line without buyer skipped
  - `test_verify_po_endpoint` — GET `/api/buy-plans-v3/{plan_id}/verify-po` returns verification results
- [ ] Run tests — all should FAIL

### Step 2.2: Create V3-compatible `verify_po_sent_v3()` in `buyplan_workflow.py`
- [ ] Add `async def verify_po_sent_v3(plan: BuyPlanV3, db: Session) -> dict` to `app/services/buyplan_workflow.py`
- [ ] Logic: iterate `plan.lines` where `status == "pending_verify"` and `po_number` is set
- [ ] For each line: find buyer via `line.buyer_id`, get their Graph token, search sent folder for `line.po_number`
- [ ] On match: set `line.po_verified_by_id` (system/None), `line.po_verified_at`, `line.status = "verified"`
- [ ] After loop: call `check_completion(plan.id, db)` if any lines were verified
- [ ] Return dict of `{po_number: {verified, recipient, sent_at, reason}}`

### Step 2.3: Add endpoint to V3 router
- [ ] Add to `app/routers/crm/buy_plans_v3.py`:
  ```python
  @router.get("/api/buy-plans-v3/{plan_id}/verify-po")
  async def check_po_verification_v3(plan_id, user, db):
  ```
- [ ] Call `verify_po_sent_v3(plan, db)`, return results + line statuses

### Step 2.4: Wire PO confirmation to trigger verification
- [ ] In `confirm_po_v3()` endpoint (already exists), after PO is confirmed, trigger background verification:
  ```python
  run_v3_notify_bg(verify_po_sent_v3, plan_id)
  ```
  Or use a separate background runner since this isn't a notification.

### Step 2.5: Run tests (TDD — green)
- [ ] Run `tests/test_buyplan_v3_po_verify.py` — all should PASS
- [ ] Run full suite — no regressions

### Step 2.6: Commit
- [ ] `git add app/services/buyplan_workflow.py app/routers/crm/buy_plans_v3.py tests/test_buyplan_v3_po_verify.py`
- [ ] `git commit -m "feat: add PO verification scanning to Buy Plan V3 workflow"`

---

## Task 3: Create v1 to v3 endpoint redirects

**Goal:** Replace all v1 buy plan endpoints with thin redirects/adapters that read from v3 data and return v1-shaped responses. Existing v1 API consumers (frontend, integrations) continue working without changes.

**V1 status mapping to V3:**
| V1 Status | V3 Status |
|---|---|
| `draft` | `draft` |
| `pending_approval` | `pending` |
| `approved` | `active` |
| `po_entered` | `active` (line has `po_number` set) |
| `po_confirmed` | `active` (line status `pending_verify` or `verified`) |
| `complete` | `completed` |
| `rejected` | `draft` (with rejection note) |
| `cancelled` | `cancelled` |

### Step 3.1: Write tests (TDD — red)
- [ ] Add to `tests/test_buy_plans.py` or create `tests/test_buyplan_v1_redirect.py`:
  - `test_v1_list_returns_v3_data_in_v1_format` — GET `/api/buy-plans` returns v1-shaped dicts from v3 data
  - `test_v1_get_returns_v3_data` — GET `/api/buy-plans/{id}` returns v1-shaped response
  - `test_v1_for_quote_returns_v3_data` — GET `/api/buy-plans/for-quote/{id}` returns v1-shaped response
  - `test_v1_token_endpoints_redirect` — token endpoints redirect to v3
  - `test_v1_status_mapping` — v3 `pending` maps to v1 `pending_approval` in response
- [ ] Run tests — should FAIL

### Step 3.2: Rewrite `app/routers/crm/buy_plans.py`
- [ ] Strip all v1 CRUD logic
- [ ] Replace with adapter functions that:
  1. Query `BuyPlanV3` + `BuyPlanLine` tables
  2. Convert to v1-shaped JSON response using `_v3_to_v1_dict(plan)` helper
  3. Map v3 statuses to v1 status names
  4. Generate `line_items` JSON array from `BuyPlanLine` rows
- [ ] Read-only endpoints (`GET /api/buy-plans`, `GET /api/buy-plans/{id}`, `GET /api/buy-plans/for-quote/{id}`) return adapted data
- [ ] Mutation endpoints (`POST`, `PUT`) return 301/410 pointing to v3 equivalents
- [ ] Token endpoints (`GET/PUT /api/buy-plans/token/{token}/*`) redirect to v3 token endpoints

### Step 3.3: Run tests (TDD — green)
- [ ] Run redirect tests — all should PASS
- [ ] Run full suite — no regressions

### Step 3.4: Commit
- [ ] `git add app/routers/crm/buy_plans.py tests/test_buyplan_v1_redirect.py`
- [ ] `git commit -m "feat: replace v1 buy plan endpoints with v3 adapters"`

---

## Task 4: Data migration script

**Goal:** Migrate existing `buy_plans` (v1) rows to `buy_plans_v3` + `buy_plan_lines` (v3) tables. This is a one-way data migration — v1 rows are preserved but marked deprecated.

### Step 4.1: Write migration test
- [ ] Create `tests/test_buyplan_migration.py`:
  - `test_v1_plan_converts_to_v3` — insert v1 plan with JSON line_items, run migration function, verify v3 plan + lines created
  - `test_status_mapping` — each v1 status maps to correct v3 status
  - `test_line_items_to_lines` — JSON line_items correctly become BuyPlanLine rows
  - `test_plan_with_po_numbers_migrated` — PO data preserved on lines
  - `test_pending_approval_plan` — plans in `pending_approval` get tokens migrated
  - `test_migration_idempotent` — running twice doesn't create duplicates
- [ ] Run tests — should FAIL

### Step 4.2: Create Alembic migration
- [ ] Run: `alembic revision -m "migrate buy_plans v1 data to v3"`
- [ ] Expected migration number: `076`
- [ ] Migration `upgrade()`:
  1. Query all `buy_plans` rows
  2. For each, create `buy_plans_v3` row with mapped fields:
     - `status`: map v1 → v3 (`pending_approval` → `pending`, `approved/po_entered/po_confirmed` → `active`, `complete` → `completed`)
     - `so_status`: `approved` if plan had SO#
     - `approval_token`, `token_expires_at`: copy if present
     - Copy: `quote_id`, `requisition_id`, `sales_order_number`, `salesperson_notes`, `submitted_by_id`, `approved_by_id`, timestamps
  3. For each `line_items` JSON entry, create `buy_plan_lines` row:
     - Map `offer_id`, `plan_qty` → `quantity`, `cost_price` → `unit_cost`, `sell_price` → `unit_sell`
     - Set `buyer_id` from `entered_by_id`
     - Map PO fields: `po_number`, `po_verified` → status (`verified`/`awaiting_po`/`pending_verify`)
  4. Add `migrated_from_v1 = True` column to `buy_plans_v3` (Boolean, default False) for tracking
  5. Mark v1 plans: add `migrated_to_v3_id` column to `buy_plans` pointing to the new v3 plan
- [ ] Migration `downgrade()`:
  1. Delete all `buy_plans_v3` rows where `migrated_from_v1 = True`
  2. Drop `migrated_from_v1` column
  3. Drop `migrated_to_v3_id` column from `buy_plans`
- [ ] Test: `alembic upgrade head` then `alembic downgrade -1` then `alembic upgrade head`

### Step 4.3: Run tests (TDD — green)
- [ ] Run `tests/test_buyplan_migration.py` — all should PASS
- [ ] Run full suite — no regressions

### Step 4.4: Commit
- [ ] `git add alembic/versions/076_*.py app/models/buy_plan.py app/models/quotes.py tests/test_buyplan_migration.py`
- [ ] `git commit -m "feat: Alembic migration to convert v1 buy plans to v3"`

---

## Task 5: Merge notification services

**Goal:** Combine `buyplan_notifications.py` (v1) and `buyplan_v3_notifications.py` (v3) into a single unified notification service.

### V1 notification functions (from `buyplan_notifications.py`):
- `run_buyplan_bg()` — background runner
- `log_buyplan_activity()` — audit trail
- `notify_buyplan_submitted()` — submit notification
- `notify_buyplan_approved()` — approve notification
- `notify_buyplan_rejected()` — reject notification
- `notify_buyplan_completed()` — complete notification
- `notify_buyplan_cancelled()` — cancel notification
- `notify_stock_sale_approved()` — stock sale auto-complete notification

### V3 notification functions (from `buyplan_v3_notifications.py`):
- `run_v3_notify_bg()` — background runner
- `notify_v3_submitted()`, `notify_v3_approved()`, `notify_v3_rejected()`
- `notify_v3_completed()`, `notify_v3_so_verified()`, `notify_v3_so_rejected()`
- `notify_v3_po_confirmed()`

### Step 5.1: Write tests (TDD — red)
- [ ] Add to `tests/test_buyplan_v3_notifications.py`:
  - `test_notify_stock_sale_approved` — stock sale notification fires (v1-only feature being added)
  - `test_notify_token_approved` — token-based approval notification fires
  - `test_notify_token_rejected` — token-based rejection notification fires
- [ ] Run tests — should FAIL (functions don't exist in v3 notifications yet)

### Step 5.2: Merge into `buyplan_v3_notifications.py`
- [ ] Add to `app/services/buyplan_v3_notifications.py`:
  - `notify_v3_stock_sale_approved()` — port from v1's `notify_stock_sale_approved()`
  - `notify_v3_token_approved()` — notification when approved via email token (no user context)
  - `notify_v3_token_rejected()` — notification when rejected via email token
  - `log_buyplan_activity()` — port from v1 (adapt to use `BuyPlanV3` model)
- [ ] V3 patterns win: use `run_v3_notify_bg()` as the single background runner

### Step 5.3: Update `buyplan_service.py` facade
- [ ] Update `app/services/buyplan_service.py` to re-export from `buyplan_v3_notifications.py` instead of `buyplan_notifications.py`
- [ ] Keep backward-compatible names for any code still importing from the facade

### Step 5.4: Delete `buyplan_notifications.py`
- [ ] Verify no remaining imports of `buyplan_notifications` except through the facade
- [ ] Delete `app/services/buyplan_notifications.py`

### Step 5.5: Run tests (TDD — green)
- [ ] Run `tests/test_buyplan_v3_notifications.py` — all should PASS
- [ ] Run full suite — no regressions

### Step 5.6: Commit
- [ ] `git add app/services/buyplan_v3_notifications.py app/services/buyplan_service.py tests/test_buyplan_v3_notifications.py`
- [ ] `git rm app/services/buyplan_notifications.py`
- [ ] `git commit -m "feat: merge v1 notifications into v3 notification service"`

---

## Task 6: Merge service files and cleanup

**Goal:** Consolidate the service layer. Rename v3 files to canonical names. Delete v1-specific code. Update all imports.

### Step 6.1: Plan the renames
| Current file | Action | New name |
|---|---|---|
| `app/routers/crm/buy_plans.py` | Keep as redirect shim | (stays) |
| `app/routers/crm/buy_plans_v3.py` | Canonical router | (stays, becomes primary) |
| `app/services/buy_plan_v3_service.py` | Rename | `app/services/buyplan_service.py` |
| `app/services/buyplan_service.py` | Delete (facade) | — |
| `app/services/buyplan_po.py` | Delete (ported to workflow) | — |
| `app/services/buyplan_v3_notifications.py` | Rename | `app/services/buyplan_notifications.py` |
| `app/services/buyplan_builder.py` | Keep | (stays) |
| `app/services/buyplan_scoring.py` | Keep | (stays) |
| `app/services/buyplan_workflow.py` | Keep | (stays) |

### Step 6.2: Write tests (TDD — red)
- [ ] Verify all existing tests still import correctly after renames
- [ ] Add import validation tests or update test imports preemptively

### Step 6.3: Perform renames and import updates
- [ ] Rename `app/services/buyplan_v3_notifications.py` → `app/services/buyplan_notifications.py` (via git mv)
- [ ] Rename `app/services/buy_plan_v3_service.py` → `app/services/buyplan_service.py` (via git mv, delete old facade first)
- [ ] Delete `app/services/buyplan_po.py` (functionality ported to `buyplan_workflow.py` in Task 2)
- [ ] Update all imports across the codebase:
  - `app/routers/crm/buy_plans_v3.py`: update service and notification imports
  - `app/routers/crm/buy_plans.py`: update imports for redirect shim
  - `app/services/buyplan_builder.py`: update any cross-service imports
  - `app/services/buyplan_workflow.py`: update imports
  - `app/jobs/*.py`: update any buy plan job imports
  - `tests/test_buy_plan_*.py`: update all test imports
  - `tests/test_buyplan_*.py`: update all test imports
- [ ] Search entire codebase for stale imports: `grep -r "buyplan_po\|buy_plan_v3_service\|buyplan_v3_notifications" app/ tests/`

### Step 6.4: Run tests (TDD — green)
- [ ] Run full suite — all should PASS
- [ ] Run coverage check — no reduction

### Step 6.5: Commit
- [ ] Stage all renamed/deleted/updated files
- [ ] `git commit -m "refactor: consolidate buy plan services into canonical file names"`

---

## Task 7: Update frontend references

**Goal:** Update `crm.js` to use v3 endpoints directly. Remove v1-specific UI code. Verify end-to-end buy plan workflow.

### Step 7.1: Audit frontend references
- [ ] Count all `/api/buy-plans/` (v1) references in `app/static/crm.js` (~30 occurrences)
- [ ] Count all `/api/buy-plans-v3/` (v3) references
- [ ] Identify v1-only UI functions that have no v3 equivalent

### Step 7.2: Update API calls in `crm.js`
- [ ] Replace `/api/buy-plans/for-quote/{id}` → keep (redirect shim handles it) OR update to v3 equivalent
- [ ] Replace `/api/buy-plans/{id}/submit` → `/api/buy-plans-v3/{id}/submit`
- [ ] Replace `/api/buy-plans/{id}/approve` → `/api/buy-plans-v3/{id}/approve` (with body format change)
- [ ] Replace `/api/buy-plans/{id}/reject` → `/api/buy-plans-v3/{id}/approve` with `action: "reject"`
- [ ] Replace `/api/buy-plans/{id}/po-bulk` → individual line PO confirmations via v3
- [ ] Replace `/api/buy-plans/{id}/complete` → v3 auto-completes via verification
- [ ] Replace `/api/buy-plans/{id}/cancel` → v3 halt/cancel
- [ ] Replace `/api/buy-plans/{id}/resubmit` → `/api/buy-plans-v3/{id}/resubmit`
- [ ] Replace `/api/buy-plans/{id}/verify-po` → `/api/buy-plans-v3/{id}/verify-po`
- [ ] Replace `/api/buy-plans` (list) → `/api/buy-plans-v3`
- [ ] Update response parsing: v3 list returns `{items, count}` not bare array

### Step 7.3: Remove v1-specific UI code
- [ ] Remove v1 buy plan section in `crm.js` (around line 2776) if fully superseded by v3 section
- [ ] Remove v1 helper functions: `loadBuyPlan()`, `renderBuyPlanDrawer()` etc. if v3 equivalents exist
- [ ] Keep any UI code that works with both versions (status badges, margin formatting)

### Step 7.4: Verify end-to-end
- [ ] Test: Build buy plan from quote (v3 build endpoint)
- [ ] Test: Submit with SO# (v3 submit)
- [ ] Test: Manager approve via UI (v3 approve)
- [ ] Test: Manager approve via email token link (v3 token approve)
- [ ] Test: Buyer confirm PO per line (v3 confirm-po)
- [ ] Test: PO verification scan (v3 verify-po)
- [ ] Test: Plan completion (auto-complete on all lines verified)
- [ ] Test: Buy plan list/queue views load correctly

### Step 7.5: Commit
- [ ] `git add app/static/crm.js`
- [ ] `git commit -m "feat: update frontend to use unified v3 buy plan endpoints"`

---

## Execution Order & Dependencies

```
Task 1 (token endpoints) ──┐
                            ├── Task 3 (redirects) ── Task 4 (data migration)
Task 2 (PO verification) ──┘         │
                                      ├── Task 5 (merge notifications)
                                      │
                                      └── Task 6 (merge services) ── Task 7 (frontend)
```

- Tasks 1 and 2 are independent — can run in parallel
- Task 3 depends on Tasks 1 and 2 (redirects must point to working v3 endpoints)
- Task 4 depends on Task 3 (migration needs the redirect layer for backward compat)
- Task 5 can run after Task 1 (needs token notification functions)
- Task 6 depends on Tasks 2, 3, and 5 (all code must be in final locations before renaming)
- Task 7 depends on Task 6 (import paths must be stable)

## Risk Mitigation

- **Data loss**: Migration (Task 4) preserves v1 rows, adds `migrated_to_v3_id` for traceability. Downgrade reverses cleanly.
- **Frontend breakage**: Redirect shim (Task 3) keeps v1 URLs working. Frontend update (Task 7) is last.
- **Notification gaps**: Merge (Task 5) adds v1-only notifications to v3 before deleting v1 file.
- **Import breakage**: Task 6 includes full codebase grep for stale imports.

## Definition of Done

- [ ] All 7 tasks complete with passing tests
- [ ] Zero references to `BuyPlan` (v1 model) in service/router code (only in migration + redirect shim)
- [ ] Zero imports of deleted files (`buyplan_po.py`, old `buyplan_notifications.py`, old `buyplan_service.py`)
- [ ] Full test suite passes with no coverage reduction
- [ ] Frontend buy plan workflow works end-to-end
- [ ] `docker compose up -d --build` starts cleanly
