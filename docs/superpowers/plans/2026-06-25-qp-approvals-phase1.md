# QP + Approvals Engine — Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a shared Approvals Engine + a native Quality Plan shell + the Prepayment gate, bridging the existing buy-plan approval, per `docs/superpowers/specs/2026-06-25-qp-approvals-phase1-design.md`.

**Architecture:** A generic engine (`approval_request → approval_step → approval_step_recipient`, with `approval_event` audit + `approval_outbox` dispatch + `approval_gate_config` routing). The Prepayment gate is the first native subject; the QP is a header object whose Buy-Plan section reuses the existing `BuyPlan`. Thin routers, fat services. The existing buy-plan approval is surfaced read-only (the bridge); migrating it to decide *through* the engine is Phase 1.5.

**Tech Stack:** FastAPI + SQLAlchemy 2.0 + PostgreSQL 16 + Alembic + HTMX 2 + Alpine 3 + Jinja2.

## Global Constraints

- Money is `NUMERIC`, never float; carry `currency` (default `USD`). Timezone via `UTCDateTime`.
- Status/type values are `StrEnum` in `app/constants.py` — never raw strings. Use `db.get(Model, id)`.
- Thin routers (HTTP only); business logic in `app/services/`. Loguru, never `print`.
- Migration is **additive only**, reversible, revision id ≤32 chars, chained onto the current head (after the in-flight `155`/`156` land — verify with `alembic heads`), claim line in `MIGRATION_NUMBERS_IN_FLIGHT.txt`.
- Every new file gets a header comment (what/calls/depends). Tests alongside every change.
- Server-side authz on every mutating route; only assigned recipients/delegates decide. Build sequentially.
- Run `TESTING=1 PYTHONPATH=$PWD .venv/bin/python -m pytest <files> -q --override-ini="addopts="`; `pre-commit` before each commit (twice if docformatter rewraps).

---

### Task 1: Constants — gate types, statuses, sourcing/payment enums

**Files:**
- Modify: `app/constants.py`
- Test: `tests/test_approval_constants.py`

**Interfaces:**
- Produces: `ApprovalGateType` (`buy_plan|prepayment|sales_order|purchase_order`), `ApprovalRequestStatus` (`requested|approved|rejected|cancelled|expired`), `ApprovalRecipientStatus` (`pending|approved|rejected|reassigned`), `ApprovalStepRule` (`any|all`), `PaymentMethod` (`cc|paypal|wire`), `SourcingType` (`spot|contract|commodity|preferred`), `QualityPlanStatus` (`draft|in_review|approved|rejected`), `QPOrderType` (`new|revision`). All `StrEnum`.

- [ ] **Step 1: Write the failing test**
```python
# tests/test_approval_constants.py
from app.constants import ApprovalGateType, ApprovalRequestStatus, PaymentMethod

def test_gate_types_are_strenum_values():
    assert ApprovalGateType.PREPAYMENT == "prepayment"
    assert set(ApprovalGateType) >= {"buy_plan", "prepayment", "sales_order", "purchase_order"}
    assert ApprovalRequestStatus.REQUESTED == "requested"
    assert PaymentMethod.WIRE == "wire"
```
- [ ] **Step 2: Run it — expect ImportError.** `pytest tests/test_approval_constants.py -q`
- [ ] **Step 3: Add the StrEnums** to `app/constants.py` (follow the existing `StrEnum` blocks, e.g. `BuyPlanStatus`). Each member value is its lowercase name.
- [ ] **Step 4: Run — expect PASS.**
- [ ] **Step 5: Commit** `feat(approvals): add gate/status/payment StrEnums`.

---

### Task 2: Schema — migration + models for the engine, QP, prepayment, Offer extension

**Files:**
- Create: `app/models/approvals.py`, `app/models/quality_plan.py`
- Modify: `app/models/__init__.py` (export), `app/models/offers.py` (+6 columns)
- Create: `alembic/versions/<NNN>_qp_approvals.py` (NNN = next free after 156; claim it)
- Modify: `MIGRATION_NUMBERS_IN_FLIGHT.txt`, `docs/APP_MAP_DATABASE.md`
- Test: `tests/test_approvals_models.py`, `tests/test_qp_migration.py`

**Interfaces:**
- Produces ORM models: `ApprovalRequest`, `ApprovalStep`, `ApprovalStepRecipient`, `ApprovalEvent`, `ApprovalOutbox`, `ApprovalGateConfig`, `QualityPlan`, `Prepayment`. `Offer` gains `is_primary` (Boolean, default false), `sourcing_type` (String), `vendor_rating` (Numeric(3,1)), `terms` (JSON), `location` (String), `specifics` (Text).

- [ ] **Step 1: Write the failing model test**
```python
# tests/test_approvals_models.py
from decimal import Decimal
from app.models.approvals import ApprovalRequest, ApprovalStep, ApprovalStepRecipient, ApprovalGateConfig
from app.constants import ApprovalGateType, ApprovalRequestStatus

def test_request_step_recipient_chain(db_session, test_user):
    req = ApprovalRequest(gate_type=ApprovalGateType.PREPAYMENT, status=ApprovalRequestStatus.REQUESTED,
                          amount=Decimal("400.00"), currency="USD", requested_by_id=test_user.id, owner_id=test_user.id)
    db_session.add(req); db_session.flush()
    step = ApprovalStep(request_id=req.id, seq=1, rule="any", status="pending"); db_session.add(step); db_session.flush()
    rec = ApprovalStepRecipient(step_id=step.id, user_id=test_user.id, status="pending"); db_session.add(rec); db_session.flush()
    assert rec.id and step.request_id == req.id

def test_gate_config_cap(db_session, test_user):
    cfg = ApprovalGateConfig(gate_type=ApprovalGateType.PREPAYMENT, approver_user_id=test_user.id,
                             max_amount=Decimal("1000"), active=True)
    db_session.add(cfg); db_session.flush()
    assert cfg.max_amount == Decimal("1000")
```
- [ ] **Step 2: Run — expect ImportError.**
- [ ] **Step 3: Write `app/models/approvals.py`** — the 6 tables. Money columns `Numeric(12, 2)`. `ApprovalStepRecipient` has `UniqueConstraint("step_id", "user_id")`. Index `approval_request(owner_id)`, `(status)`, `(gate_type)`, `(subject_quality_plan_id)`, `(subject_prepayment_id)`. Header comment. Mirror an existing model's style (`app/models/buy_plan.py`).
- [ ] **Step 4: Write `app/models/quality_plan.py`** — `QualityPlan` + `Prepayment` (FK `vendor_card_id`, `buy_plan_id`, `total_incl_fees Numeric(12,2)`, `payment_method`, `test_report_sent Boolean`, `buyer_remarks Text`). Export all from `app/models/__init__.py`.
- [ ] **Step 5: Add the 6 Offer columns** in `app/models/offers.py` (nullable; `is_primary` default `False`).
- [ ] **Step 6: Generate + hand-review the migration.** `alembic revision --autogenerate -m "qp + approvals engine"`; confirm it only CREATEs the new tables + ADD-COLUMNs the 6 Offer fields (no drops); set `down_revision` to the current head (`alembic heads`); write a symmetric `downgrade()`. Add the claim line + `docs/APP_MAP_DATABASE.md` entry.
- [ ] **Step 7: Migration round-trip test**
```python
# tests/test_qp_migration.py — assert tables exist after upgrade; mirror tests/test_migration_chain.py style
```
Run upgrade→downgrade→upgrade on a throwaway PG; `alembic heads` single. Model tests PASS.
- [ ] **Step 8: Commit** `feat(approvals): schema — engine tables, QP, prepayment, offer fields (migration NNN)`.

---

### Task 3: RoutingService — eligibility + step/recipient creation

**Files:**
- Create: `app/services/approvals/routing.py`
- Test: `tests/test_approval_routing.py`

**Interfaces:**
- Produces: `route_request(db, request) -> ApprovalStep` — reads `ApprovalGateConfig` for `request.gate_type`, selects active approvers where `max_amount IS NULL OR request.amount <= max_amount`, creates one `ApprovalStep(rule="any")` + one `ApprovalStepRecipient(status="pending")` per eligible approver. Raises `NoEligibleApproverError` if none.

- [ ] **Step 1: Failing test** — seed configs Myrna(cap 1000)/Mike(None)/Marcus(None); a `400` request routes to all three; a `2500` request routes to Mike+Marcus only; a `0`-config gate raises `NoEligibleApproverError`.
```python
def test_threshold_excludes_capped_approver(db_session, seed_prepayment_config):
    req = make_request(db_session, gate=ApprovalGateType.PREPAYMENT, amount=Decimal("2500"))
    step = route_request(db_session, req)
    user_ids = {r.user_id for r in step.recipients}
    assert user_ids == {mike.id, marcus.id}  # myrna excluded (cap 1000)
```
- [ ] **Step 2: Run — fail.**
- [ ] **Step 3: Implement `route_request`** + `NoEligibleApproverError`. Header comment. `db.get`/2.0 query style.
- [ ] **Step 4: Run — pass.**
- [ ] **Step 5: Commit** `feat(approvals): routing with amount-threshold eligibility`.

---

### Task 4: ApprovalService — create + decide (first-responder-wins, idempotent)

**Files:**
- Create: `app/services/approvals/service.py`
- Modify: `app/services/approvals/__init__.py`
- Test: `tests/test_approval_service.py`

**Interfaces:**
- Consumes: `route_request` (Task 3), `ApprovalEventService.record` (Task 5 — import lazily; tests in this task assert the event row exists via a thin inline writer, replaced in Task 5).
- Produces: `create_request(db, *, gate_type, amount, subject, requested_by, owner) -> ApprovalRequest` (creates request + routes); `decide(db, request_id, user, action, comment=None) -> ApprovalRequest` where `action ∈ {"approve","reject"}`. `decide` takes `SELECT … FOR UPDATE` on the request, rejects if not `requested`, records the recipient decision, closes the request (`approved`/`rejected`), enqueues an outbox event. Reject requires non-blank `comment` → `ValueError` otherwise. A non-recipient → `PermissionError`.

- [ ] **Step 1: Failing tests**
```python
def test_first_responder_wins(db_session, prepayment_request_with_two_recipients):
    req = prepayment_request_with_two_recipients
    decide(db_session, req.id, mike, "approve")
    assert db_session.get(ApprovalRequest, req.id).status == ApprovalRequestStatus.APPROVED
    with pytest.raises(ValueError):  # already decided → terminal
        decide(db_session, req.id, marcus, "approve")

def test_reject_requires_reason(db_session, prepayment_request_with_two_recipients):
    with pytest.raises(ValueError):
        decide(db_session, prepayment_request_with_two_recipients.id, mike, "reject", comment="")

def test_non_recipient_forbidden(db_session, prepayment_request_with_two_recipients, other_user):
    with pytest.raises(PermissionError):
        decide(db_session, prepayment_request_with_two_recipients.id, other_user, "approve")
```
- [ ] **Step 2: Run — fail.**
- [ ] **Step 3: Implement `create_request` + `decide`** with `db.execute(select(ApprovalRequest).where(...).with_for_update())`; guard `status == REQUESTED`; set recipient + request statuses; enqueue `ApprovalOutbox(event_type="decided")`. Header comment.
- [ ] **Step 4: Run — pass.**
- [ ] **Step 5: Commit** `feat(approvals): create + decide with row-lock first-responder-wins`.

---

### Task 5: ApprovalEventService + reassign/cancel

**Files:**
- Create: `app/services/approvals/events.py`
- Modify: `app/services/approvals/service.py` (reassign, cancel; call `events.record`)
- Test: `tests/test_approval_events.py`

**Interfaces:**
- Produces: `events.record(db, request, actor, event_type, metadata=None)` — append-only `ApprovalEvent` row **and** a summary `ActivityLog` row (reuse `app/services/activity_service.log_activity`, `activity_type` an `ActivityType` member; populate the subject id). `reassign(db, request_id, from_user, to_user, actor)` and `cancel(db, request_id, actor)`.

- [ ] **Step 1: Failing tests** — a decide writes exactly one `ApprovalEvent` + one `ActivityLog`; reassign moves the pending recipient (sets `reassigned_from_id`, adds the new recipient `pending`); cancel on a terminal request raises `ValueError`.
- [ ] **Step 2: Run — fail.**
- [ ] **Step 3: Implement** `events.record`, wire it into `decide`/`reassign`/`cancel`. Add the `ActivityType` approval members if missing (Task 1 follow-up — add `APPROVAL_REQUESTED/APPROVED/REJECTED/DELEGATED` to `ActivityType`).
- [ ] **Step 4: Run — pass.**
- [ ] **Step 5: Commit** `feat(approvals): append-only audit + reassign/cancel`.

---

### Task 6: Outbox dispatcher + NotificationService (email + in-app)

**Files:**
- Create: `app/services/approvals/notifications.py`, `app/jobs/approval_outbox.py`
- Modify: `app/scheduler.py` (register the drain job)
- Test: `tests/test_approval_outbox.py`

**Interfaces:**
- Consumes: the Graph email path (reuse the existing send helper used by buy-plan notifications) + the in-app `Notification` model.
- Produces: `dispatch_pending(db)` — drains `approval_outbox` where `status="pending"`, sends email + writes a `Notification` per recipient, marks `sent` (idempotent: a `(request_id,event_type)` already `sent` is skipped). `notify_request_created(request)` enqueues; the dispatcher sends.

- [ ] **Step 1: Failing tests** — a `decided` outbox row, after `dispatch_pending`, is `sent` and produced one `Notification`; running `dispatch_pending` twice does NOT double-send (idempotency key).
- [ ] **Step 2: Run — fail.** (Mock the Graph send at its source module.)
- [ ] **Step 3: Implement** the dispatcher + NotificationService; register a periodic job in `app/scheduler.py` (guard with `TESTING`).
- [ ] **Step 4: Run — pass.**
- [ ] **Step 5: Commit** `feat(approvals): idempotent outbox dispatch — email + in-app`.

---

### Task 7: Prepayment object + create route (spawns the gate)

**Files:**
- Create: `app/services/prepayment_service.py`, `app/routers/prepayments.py`
- Modify: `app/main.py` (include router)
- Test: `tests/test_prepayment.py`

**Interfaces:**
- Consumes: `create_request` (Task 4).
- Produces: `create_prepayment(db, *, buy_plan_id, vendor_card_id, payment_method, total_incl_fees, test_report_sent, buyer_remarks, created_by) -> Prepayment` (persists + spawns an `ApprovalRequest(gate_type=PREPAYMENT, amount=total_incl_fees, subject_prepayment_id=...)`). `POST /v2/prepayments` (require_user) → JSON.

- [ ] **Step 1: Failing test** — creating a `$400` prepayment spawns a `requested` prepayment request routed to all three approvers; the route returns 200 + the request id; unauth → 401.
- [ ] **Step 2: Run — fail.**
- [ ] **Step 3: Implement** the service + thin router; register in `app/main.py`.
- [ ] **Step 4: Run — pass.**
- [ ] **Step 5: Commit** `feat(prepayment): native prepayment spawning the engine gate`.

---

### Task 8: QualityPlanService — create/auto-fill + completeness gate + submit

**Files:**
- Create: `app/services/quality_plan_service.py`
- Test: `tests/test_quality_plan_service.py`

**Interfaces:**
- Produces: `create_qp(db, *, customer_id, owner_id, buy_plan_id=None) -> QualityPlan` (header, status `draft`, auto-fill owner/customer); `validate_complete(qp) -> list[str]` (returns blank-required-field messages); `submit(db, qp_id, user) -> QualityPlan` (raises `IncompleteQPError` with the field list if not complete; else status `in_review`, records an event). Phase-1 required fields: customer, owner, order_type, and a linked buy_plan_id.

- [ ] **Step 1: Failing tests** — `validate_complete` flags a QP missing `buy_plan_id`; `submit` on an incomplete QP raises `IncompleteQPError`; `submit` on a complete QP sets `in_review` + writes one event.
- [ ] **Step 2: Run — fail.**
- [ ] **Step 3: Implement** the service + `IncompleteQPError`.
- [ ] **Step 4: Run — pass.**
- [ ] **Step 5: Commit** `feat(qp): create/auto-fill + completeness gate + submit`.

---

### Task 9: Approvals API + `require_approval_gatekeeper`

**Files:**
- Create: `app/routers/approvals.py`
- Modify: `app/dependencies.py` (`require_approval_gatekeeper`), `app/main.py`
- Test: `tests/test_approvals_routes.py`

**Interfaces:**
- Consumes: `decide`/`reassign`/`cancel` (Tasks 4–5).
- Produces: `require_approval_gatekeeper(request, db) -> User` (resolves `request_id` from path; 403 unless the user is a pending recipient or delegate). Routes (HTMX): `POST /v2/approvals/requests/{id}/decision` (form `action`,`comment`), `…/reassign`, `…/cancel`, `GET /v2/approvals/requests` (filter `gate_type`,`status`), `GET /v2/approvals/requests/{id}`.

- [ ] **Step 1: Failing tests** — a pending recipient can POST a decision (200, request closes); a non-recipient gets 403 (on real PG via the live-verify pattern); reject without comment → 400 with `{"error": ...}`.
- [ ] **Step 2: Run — fail.**
- [ ] **Step 3: Implement** the dependency + thin routes (HTML partials); register in `app/main.py`.
- [ ] **Step 4: Run — pass.**
- [ ] **Step 5: Commit** `feat(approvals): decision/reassign/cancel API + gatekeeper authz`.

---

### Task 10: Gate-config admin UI (extend manager-approval page) + seed

**Files:**
- Modify: `app/routers/admin/users.py` (gate-config CRUD), `app/templates/htmx/partials/settings/users.html` (per-gate approver+threshold table)
- Modify: `app/startup.py` (idempotent seed of Prepayment config: Myrna 1000 / Mike NULL / Marcus NULL — by email lookup, skip if users absent)
- Test: `tests/test_gate_config_admin.py`

**Interfaces:**
- Consumes: `ApprovalGateConfig` (Task 2).
- Produces: admin routes to add/remove an approver+cap per gate (admin-gated, audited); the seed.

- [ ] **Step 1: Failing tests** — admin can add Myrna@cap-1000 to the prepayment gate (row created, audited); non-admin → 403; seed is idempotent (re-run adds no duplicates).
- [ ] **Step 2: Run — fail.**
- [ ] **Step 3: Implement** the routes + the Users-tab table (follow the existing `set_buyplan_approver` toggle pattern) + the seed in `startup.py` (runtime op, no DDL).
- [ ] **Step 4: Run — pass.**
- [ ] **Step 5: Commit** `feat(approvals): per-gate approver config on the manager page + prepayment seed`.

---

### Task 11: QP native one-screen view + Buy-Plan section

**Files:**
- Create: `app/templates/htmx/partials/qp/detail.html`, `app/routers/quality_plans.py`
- Modify: `app/main.py`, the CRM/nav entry point that opens a QP
- Test: `tests/test_qp_view.py`

**Interfaces:**
- Consumes: `QualityPlanService`, the existing buy-plan detail partial for the Buy-Plan section.
- Produces: `GET /v2/qp/{id}` (HTMX partial) rendering the header + collapsible sections (Sales/Purchasing/Buy Plan/Serial; only Buy Plan populated, others collapsed "Phase 2") + per-section approval chips + a Submit button (blocked with field errors when incomplete).

- [ ] **Step 1: Failing tests** — `GET /v2/qp/{id}` renders the customer/owner header + a Buy-Plan section including the linked buy plan's lines; an incomplete QP shows the completeness errors inline; template parses.
- [ ] **Step 2: Run — fail.**
- [ ] **Step 3: Implement** the route + dense HTMX template (Alpine collapsible, accent chips, `.compact-table`), reusing the buy-plan detail include for the section.
- [ ] **Step 4: Run — pass** + headless-verify the page renders.
- [ ] **Step 5: Commit** `feat(qp): native one-screen QP view with Buy-Plan section`.

---

### Task 12: Bridge — surface the existing buy-plan approval in the Approvals UI

**Files:**
- Modify: `app/routers/approvals.py` (the `GET /v2/approvals/requests` list also reads pending `BuyPlan` approvals read-only), `app/templates/htmx/partials/approvals/_queue.html`
- Test: `tests/test_approvals_bridge.py`

**Interfaces:**
- Consumes: the existing `BuyPlan` approval state (status `pending`, `approved_by_id`).
- Produces: the unified approvals queue shows both engine `prepayment` requests and existing `buy_plan` pending approvals (the latter link to the existing buy-plan detail approve/reject — already gated on `can_approve_buy_plans`). No behavior change to buy-plan approval (read-only surfacing).

- [ ] **Step 1: Failing test** — the approvals queue for an approver lists a pending prepayment request AND a pending buy plan; clicking the buy plan links to its existing detail.
- [ ] **Step 2: Run — fail.**
- [ ] **Step 3: Implement** the read-only merge in the queue view.
- [ ] **Step 4: Run — pass.**
- [ ] **Step 5: Commit** `feat(approvals): unified queue bridges existing buy-plan approval`.

---

## Self-Review

- **Spec coverage:** engine tables (T2), routing+thresholds (T3), decide concurrency (T4), audit+reassign/cancel (T5), outbox+notifications (T6), prepayment gate (T7), QP+completeness (T8), API+authz (T9), config UI+seed (T10), native QP view (T11), bridge (T12). Out-of-scope items (SO/PO gates, Serial, Board, Teams, Acctivate write-back, xlsx) are correctly absent.
- **Placeholders:** none — each task names files, interfaces, and concrete TDD steps. Where boilerplate (HTML partials, routine routes) is referenced to an existing pattern, the pattern file is named.
- **Type consistency:** `decide(db, request_id, user, action, comment)`, `route_request(db, request)`, `create_request(db, *, gate_type, amount, subject, requested_by, owner)`, `require_approval_gatekeeper(request, db) -> User` used consistently across T4/T9.

## Phase 1.5 (immediate follow-on, separate plan)
Migrate the existing buy-plan approval to decide *through* the engine (`gate_type='buy_plan'`), retiring the bespoke approve route — replacing the T12 read-only bridge with a real engine gate.
