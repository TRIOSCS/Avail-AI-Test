# Approvals Module SP-2: Sales Order → Manager Gate — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Formalize the front half of the deal lifecycle as a "Sales Order" (the buy plan at its front stage) — originate a buy plan directly from RFQ offers, make the SO# canonical on the buy plan, and de-collide the Quality-Plan sales-section approval gate — all without a new entity or a new approval gate.

**Architecture:** The Sales Order *is* the `BuyPlan` row at DRAFT/PENDING; SO approval = the existing `BUY_PLAN` approval gate. We (1) make `BuyPlan.quote_id` nullable so a buy plan can be born from offers with no customer quote, (2) add a sibling builder `create_sales_order_from_offers` sharing the quote path's scoring core, (3) rename the colliding QP sales gate (`ApprovalGateType.SALES_ORDER`→`QP_SALES`, `can_approve_sales_orders`→`can_approve_qp_sales`) and give it an inline approve/reject in the QP view, (4) retire `QualityPlan.sales_so_number` in favor of `BuyPlan.sales_order_number`, and (5) repoint the Approvals "Sales Orders" tab to the `BUY_PLAN` gate with a backward-compatible board status filter.

**Tech Stack:** FastAPI + SQLAlchemy 2.0 + PostgreSQL 16 + Alembic + HTMX 2 + Alpine 3 + Jinja2 + Tailwind 3; pytest (`-n auto` xdist, in-memory SQLite).

**Spec:** `docs/superpowers/specs/2026-06-27-approvals-module-sp2-sales-order-gate-design.md` — its **Appendix A** is the canonical exhaustive edit-site inventory; tasks reference it by section (A.1–A.5) rather than re-listing every test-file line.

## Global Constraints

- **Branch:** `feat/approvals-module-sp2-sales-order` (already checked out, pushed). Merge, don't rebase. Use `git -C /root/availai`.
- **Stack is HTMX + Alpine + Jinja2 — never React.** Server-render + HTMX swap.
- **ONE migration, revision `163`, `down_revision="162_resource_and_cancellations"`.** Claim `163` in `MIGRATION_NUMBERS_IN_FLIGHT.txt`. Revision id ≤32 chars. Migrations `160`/`161` are immutable — never edit them. Each schema task **appends its op** to the same `163` file; it is round-tripped only in the final task.
- **Tests build SQLite from models**, not migrations — model + code edits must be coherent within a task to keep the suite green. The migration is verified separately on a **throwaway** Postgres (`docker run --rm -d … postgres:16-alpine`), never the staging `db`.
- **Status/StrEnum constants only** (`app/constants.py`) — never raw strings.
- `db.get(Model, id)`, not `db.query(...).get(id)`. Loguru, not `print()`.
- **Run before pushing:** `pre-commit run --all-files`. Don't start docstrings with `"`. Don't put `"` inside double-quoted Alpine attrs.
- **After code changes, update the relevant `docs/APP_MAP_*.md`** in the same PR (folded into Task 11).
- Test commands: `TESTING=1 PYTHONPATH=/root/availai pytest tests/<file> -v`; single file without xdist add `--override-ini="addopts="`.
- **Naming asymmetry is intentional this SP:** gate value becomes `qp_sales` and the column `can_approve_qp_sales`, but the admin route `/sales-order-approver` + handler `set_sales_order_approver` are **left unchanged** (cosmetic follow-up, spec §13). Do not rename them.

---

## File Structure

**Models / schema**
- `app/models/buy_plan.py` — `quote_id` → nullable.
- `app/models/auth.py` — `can_approve_sales_orders` → `can_approve_qp_sales`.
- `app/models/quality_plan.py` — drop `sales_so_number`.
- `app/constants.py` — `ApprovalGateType.SALES_ORDER` → `QP_SALES`.
- `alembic/versions/163_sp2_sales_order_gate.py` — **new**, all 5 ops.
- `MIGRATION_NUMBERS_IN_FLIGHT.txt` — claim `163`.

**Services**
- `app/services/buyplan_builder.py` — extract `_assemble_buy_plan`; add `create_sales_order_from_offers`.
- `app/services/buyplan_hub.py` — `_customer_name` requisition fallback + board `statuses` param + joinedload.
- `app/services/buyplan_workflow.py` — `generate_case_report` requisition fallback.
- `app/services/buyplan_notifications.py` — `_plan_context` requisition fallback.
- `app/services/quality_plan_service.py` — gate value/label renames; buy-plan-sourced SO# completeness check.
- `app/services/approvals/queue.py` — `TAB_*` remap.
- `app/services/approvals/routing.py` — column/enum renames.
- `app/services/approvals/service.py` — enum rename in the decide section-dispatch tuple.

**Routers**
- `app/routers/quality_plans.py` — enum renames; drop `_SALES_FIELDS['sales_so_number']`; QP-view inline approve/reject.
- `app/routers/admin/users.py` — column renames + audit gate value.
- `app/routers/htmx_views.py` — `_TAB_APPROVE_ATTR` remap + handler guard; New-SO + create endpoints.

**Templates**
- `app/templates/htmx/partials/settings/users.html` — column-key renames.
- `app/templates/htmx/partials/qp/_section_sales.html` — remove editable SO# input; read-only from buy plan.
- `app/templates/htmx/partials/qp/detail.html` — inline approve/reject affordance on the Sales section header.
- `app/templates/htmx/partials/approvals/_tab_buy_plans.html` — delete `_pending_section` include.
- `app/templates/htmx/partials/approvals/_tab_sales_orders.html` — New-SO button + DRAFT/PENDING work surface.
- `app/templates/htmx/partials/approvals/_sales_order_new.html` — **new**, requisition picker + builder launch.

**Docs**
- `docs/APP_MAP_DATABASE.md`, `docs/APP_MAP_INTERACTIONS.md` — sync (Task 11).

---

## Task 1: Make `BuyPlan.quote_id` nullable + claim migration 163

**Files:**
- Modify: `app/models/buy_plan.py:78`
- Create: `alembic/versions/163_sp2_sales_order_gate.py`
- Modify: `MIGRATION_NUMBERS_IN_FLIGHT.txt`
- Test: `tests/test_buy_plan_models.py`

**Interfaces:**
- Produces: `BuyPlan(quote_id=None, requisition_id=…)` persists (the nullable contract every later origination task relies on). Migration `163` exists with `down_revision="162_resource_and_cancellations"`; later schema tasks append ops to it.

- [ ] **Step 1: Write the failing test** — `tests/test_buy_plan_models.py`

```python
def test_buy_plan_persists_without_quote(db_session):
    """An SO-origin buy plan has no customer quote (quote_id is nullable)."""
    from app.models.buy_plan import BuyPlan
    from app.models.requisition import Requisition
    req = Requisition(customer_name="Acme")
    db_session.add(req)
    db_session.flush()
    plan = BuyPlan(quote_id=None, requisition_id=req.id)
    db_session.add(plan)
    db_session.commit()
    assert plan.id is not None
    assert plan.quote is None
```

- [ ] **Step 2: Run it, verify it fails**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_buy_plan_models.py::test_buy_plan_persists_without_quote -v --override-ini="addopts="`
Expected: FAIL — `IntegrityError` / NOT NULL on `quote_id`.

- [ ] **Step 3: Make the column nullable** — `app/models/buy_plan.py:78`

```python
    # ── Quote / Deal linkage
    quote_id = Column(Integer, ForeignKey("quotes.id", ondelete="CASCADE"), nullable=True)
```

- [ ] **Step 4: Run it, verify it passes**

Run: same as Step 2. Expected: PASS.

- [ ] **Step 5: Create the migration `163` with op #1 only** — `alembic/versions/163_sp2_sales_order_gate.py`

```python
"""SP-2 sales-order gate: quote_id nullable, qp_sales rename, drop sales_so_number.

Revision ID: 163_sp2_sales_order_gate
Revises: 162_resource_and_cancellations
"""

import sqlalchemy as sa
from alembic import op

revision = "163_sp2_sales_order_gate"
down_revision = "162_resource_and_cancellations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Op 1 — BuyPlan.quote_id nullable (SO origination from offers, no quote).
    op.alter_column("buy_plans_v3", "quote_id", existing_type=sa.Integer(), nullable=True)
    # (ops 2–5 appended in Tasks 4, 5, 8)


def downgrade() -> None:
    # WARNING: re-asserting NOT NULL fails if any SO-origin (quote_id IS NULL) rows
    # exist; delete/backfill them before downgrading. Roll back code + schema together.
    op.alter_column("buy_plans_v3", "quote_id", existing_type=sa.Integer(), nullable=False)
```

- [ ] **Step 6: Claim the migration number** — append to `MIGRATION_NUMBERS_IN_FLIGHT.txt`

```
163 feat/approvals-module-sp2-sales-order  SP-2: buy_plans_v3.quote_id->nullable; users.can_approve_sales_orders->can_approve_qp_sales; approval_requests.gate_type sales_order->qp_sales (data); quality_plans.sales_so_number backfill->buy_plan + drop. down_revision=162_resource_and_cancellations
```

- [ ] **Step 7: Verify single head + the file imports**

Run: `cd /root/availai && TESTING=1 PYTHONPATH=/root/availai python -c "from alembic.config import Config; from alembic.script import ScriptDirectory; s=ScriptDirectory.from_config(Config('alembic.ini')); print('HEADS', s.get_heads())"`
Expected: `HEADS ('163_sp2_sales_order_gate',)` — exactly one head.

- [ ] **Step 8: Commit**

```bash
git -C /root/availai add app/models/buy_plan.py alembic/versions/163_sp2_sales_order_gate.py MIGRATION_NUMBERS_IN_FLIGHT.txt tests/test_buy_plan_models.py
git -C /root/availai commit -m "feat(approvals): SP-2 make BuyPlan.quote_id nullable + claim migration 163"
```

---

## Task 2: Requisition customer fallbacks for SO-origin plans

SO-origin plans (`quote_id=None`) don't crash, but three helpers render a blank/Unknown customer because they only read the quote. Add a requisition-first fallback (the pattern already used in `buy_plans/detail.html:87-104`).

**Files:**
- Modify: `app/services/buyplan_hub.py:65-77` (`_customer_name`)
- Modify: `app/services/buyplan_workflow.py:1108-1118` (`generate_case_report`)
- Modify: `app/services/buyplan_notifications.py:63-82` (`_plan_context`)
- Test: `tests/test_buyplan_hub_board.py`, `tests/test_buyplan_notifications.py`

**Interfaces:**
- Consumes: `BuyPlan(quote_id=None)` from Task 1.
- Produces: `_customer_name(plan)` returns `plan.requisition.customer_name` when there is no quote; case report + notification context carry the same fallback.

- [ ] **Step 1: Write the failing test** — `tests/test_buyplan_hub_board.py`

```python
def test_customer_name_falls_back_to_requisition_for_so_origin(db_session):
    from app.services.buyplan_hub import _customer_name
    from app.models.buy_plan import BuyPlan
    from app.models.requisition import Requisition
    req = Requisition(customer_name="Globex Corp")
    db_session.add(req); db_session.flush()
    plan = BuyPlan(quote_id=None, requisition_id=req.id)
    db_session.add(plan); db_session.flush()
    plan.requisition = req
    assert _customer_name(plan) == "Globex Corp"
```

- [ ] **Step 2: Run it, verify it fails**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_buyplan_hub_board.py::test_customer_name_falls_back_to_requisition_for_so_origin -v --override-ini="addopts="`
Expected: FAIL — `_customer_name` returns `None`.

- [ ] **Step 3: Add the fallback** — `app/services/buyplan_hub.py` `_customer_name`

```python
def _customer_name(plan):
    """Customer label for a plan card: quote's customer, else the requisition's."""
    if plan.quote and plan.quote.customer_site and plan.quote.customer_site.company:
        return plan.quote.customer_site.company.name
    req = plan.requisition
    if req:
        if req.customer_name:
            return req.customer_name
        if req.customer_site and req.customer_site.company:
            return req.customer_site.company.name
    return None
```

- [ ] **Step 4: Mirror the fallback** in `generate_case_report` (`buyplan_workflow.py`) and `_plan_context` (`buyplan_notifications.py`) — when `quote is None`, set the customer line from `plan.requisition.customer_name`; keep `quote_number` blank/"—" when there is no quote.

```python
# buyplan_notifications._plan_context — after `quote = db.get(Quote, plan.quote_id) if plan.quote_id else None`
customer_name = ""
if quote and quote.customer_site and quote.customer_site.company:
    customer_name = quote.customer_site.company.name
elif plan.requisition and plan.requisition.customer_name:
    customer_name = plan.requisition.customer_name
quote_number = quote.quote_number if quote else ""
```

```python
# buyplan_workflow.generate_case_report — replace the customer default
customer = "Unknown"
if quote and quote.customer_site and quote.customer_site.company:
    customer = quote.customer_site.company.name
elif plan.requisition and plan.requisition.customer_name:
    customer = plan.requisition.customer_name
```

- [ ] **Step 5: Add a notification-context test** — `tests/test_buyplan_notifications.py`

```python
def test_plan_context_uses_requisition_customer_without_quote(db_session):
    from app.services.buyplan_notifications import _plan_context
    from app.models.buy_plan import BuyPlan
    from app.models.requisition import Requisition
    req = Requisition(customer_name="Initech")
    db_session.add(req); db_session.flush()
    plan = BuyPlan(quote_id=None, requisition_id=req.id)
    db_session.add(plan); db_session.flush()
    plan.requisition = req
    ctx = _plan_context(plan, db_session)
    assert ctx["customer_name"] == "Initech"
    assert ctx["quote_number"] == ""
```

- [ ] **Step 6: Run both test files, verify pass**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_buyplan_hub_board.py tests/test_buyplan_notifications.py -v`
Expected: PASS (existing quote-path tests still green).

- [ ] **Step 7: Commit**

```bash
git -C /root/availai add app/services/buyplan_hub.py app/services/buyplan_workflow.py app/services/buyplan_notifications.py tests/test_buyplan_hub_board.py tests/test_buyplan_notifications.py
git -C /root/availai commit -m "feat(approvals): requisition customer fallback for SO-origin buy plans"
```

---

## Task 3: `create_sales_order_from_offers` builder (+ shared `_assemble_buy_plan`)

**Files:**
- Modify: `app/services/buyplan_builder.py` (extract `_assemble_buy_plan`; add `create_sales_order_from_offers`)
- Test: `tests/test_buyplan_builder_so_origin.py` (new)

**Interfaces:**
- Consumes: nullable `quote_id` (Task 1).
- Produces: `create_sales_order_from_offers(requisition_id: int, selections: dict[int, int], sell_prices: dict[int, float], db: Session, user: User) -> BuyPlan` — `selections` maps `requirement_id → chosen offer_id`; persists a DRAFT `BuyPlan` with `quote_id=None`; raises `ValueError` if a non-terminal plan already exists for the requisition. `_assemble_buy_plan(requisition, chosen_offers, sell_prices, customer_region, db) -> BuyPlan` (unsaved) is the shared scoring/assignment core used by both this and `build_buy_plan`.

- [ ] **Step 1: Write the failing test** — `tests/test_buyplan_builder_so_origin.py`

```python
import pytest
from app.constants import BuyPlanStatus


def test_create_sales_order_from_offers_makes_draft_without_quote(db_session, so_origin_fixture):
    from app.services.buyplan_builder import create_sales_order_from_offers
    req, selections, sell_prices, user = so_origin_fixture
    plan = create_sales_order_from_offers(req.id, selections, sell_prices, db_session, user)
    assert plan.id is not None
    assert plan.quote_id is None
    assert plan.requisition_id == req.id
    assert plan.status == BuyPlanStatus.DRAFT.value
    assert len(plan.lines) == len(selections)


def test_duplicate_so_for_requisition_is_blocked(db_session, so_origin_fixture):
    from app.services.buyplan_builder import create_sales_order_from_offers
    req, selections, sell_prices, user = so_origin_fixture
    create_sales_order_from_offers(req.id, selections, sell_prices, db_session, user)
    with pytest.raises(ValueError, match="already an open"):
        create_sales_order_from_offers(req.id, selections, sell_prices, db_session, user)
```

Add a `so_origin_fixture` to `tests/conftest.py` (or the test file) building a requisition with ≥1 requirement that has ≥1 scored offer, plus `selections`/`sell_prices` dicts and a buyer `User`. Mirror the seed shape used by `tests/test_buyplan_builder_*` (reuse their offer/requirement factories).

- [ ] **Step 2: Run it, verify it fails**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_buyplan_builder_so_origin.py -v --override-ini="addopts="`
Expected: FAIL — `create_sales_order_from_offers` undefined.

- [ ] **Step 3: Extract the shared core** in `buyplan_builder.py` — pull the offer-scoring + buyer-assignment + line-building + margin/AI-summary body of `build_buy_plan` into:

```python
def _assemble_buy_plan(requisition, chosen_offers, sell_prices, customer_region, db):
    """Build (unsaved) BuyPlan + lines from chosen offers. Shared by the quote and SO-origin paths.

    chosen_offers: dict requirement_id -> offer_id. sell_prices: dict requirement_id -> float.
    customer_region: optional str for the geo-mismatch flag (None skips it).
    """
    plan = BuyPlan(requisition_id=requisition.id)
    # ... existing scoring/assignment/line-building/margin/AI-summary logic, parameterized
    # on chosen_offers / sell_prices / customer_region instead of reading them off a quote ...
    return plan
```

Refactor `build_buy_plan(quote_id, db)` to keep its WON/SENT guard (`:59-60`) and its `quote_id`-keyed duplicate guard (`:62-74`), then call `_assemble_buy_plan(quote.requisition, _quote_chosen_offers(quote_id, db), <quote sell prices>, <quote-derived region>, db)` and set `plan.quote_id = quote_id`. **Run `tests/test_buyplan_builder_*` after the refactor to prove the quote path is unchanged before adding the new entry point.**

- [ ] **Step 4: Add the new entry point** — `buyplan_builder.py`

```python
def create_sales_order_from_offers(requisition_id, selections, sell_prices, db, user):
    """Originate a DRAFT buy plan (Sales Order) directly from chosen RFQ offers — no quote."""
    requisition = db.get(Requisition, requisition_id)
    if requisition is None:
        raise ValueError(f"Requisition {requisition_id} not found")
    existing = (
        db.query(BuyPlan)
        .filter(
            BuyPlan.requisition_id == requisition_id,
            BuyPlan.quote_id.is_(None),
            BuyPlan.status.in_([BuyPlanStatus.DRAFT.value, BuyPlanStatus.PENDING.value, BuyPlanStatus.ACTIVE.value]),
        )
        .first()
    )
    if existing:
        raise ValueError(f"There is already an open Sales Order (#{existing.id}) for this requisition")
    region = None
    if requisition.customer_site and requisition.customer_site.country:
        region = _country_to_region(requisition.customer_site.country)
    plan = _assemble_buy_plan(requisition, selections, sell_prices, region, db)
    plan.quote_id = None
    db.add(plan)
    db.commit()
    return plan
```

- [ ] **Step 5: Run the new + builder tests, verify pass**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_buyplan_builder_so_origin.py tests/ -k buyplan_builder -v`
Expected: PASS (both new tests + all existing builder tests).

- [ ] **Step 6: Commit**

```bash
git -C /root/availai add app/services/buyplan_builder.py tests/test_buyplan_builder_so_origin.py tests/conftest.py
git -C /root/availai commit -m "feat(approvals): create_sales_order_from_offers builder + shared _assemble_buy_plan"
```

---

## Task 4: Rename `can_approve_sales_orders` → `can_approve_qp_sales`

Atomic rename (column + every call site) so the suite stays green. Full site list = **spec Appendix A.1**.

**Files:** `app/models/auth.py:77`; `app/routers/admin/users.py:162,412,415`; `app/routers/htmx_views.py:11046`; `app/services/approvals/routing.py:94`; `app/templates/htmx/partials/settings/users.html:146,150`; tests per A.1; append migration op #2.

**Interfaces:**
- Produces: `User.can_approve_qp_sales` (the QP Sales-section approver right). `can_approve_buy_plans`/`can_approve_pos` untouched.

- [ ] **Step 1: Rename the model attribute** — `app/models/auth.py:77`

```python
    can_approve_qp_sales = Column(Boolean, nullable=False, default=False, server_default=text("false"))
```

- [ ] **Step 2: Update every MANDATORY call site in A.1** — `admin/users.py:162` (read **and** the dict key → `"can_approve_qp_sales"`), `:412,:415`; `htmx_views.py:11046` (value string → `"can_approve_qp_sales"` — this fails *soft* via `getattr`, so it must change); `routing.py:94` (`User.can_approve_qp_sales.is_(True)`); `settings/users.html:146,150` (`row.can_approve_qp_sales`, coupled to the dict key). Update the test kwargs/asserts listed in A.1 (`test_c2a_gates.py`, `test_c2b_sections.py`, `test_approvals_queue.py:548`). Update the DOC-only sites in A.1.

- [ ] **Step 3: Append migration op #2** — in `163_sp2_sales_order_gate.py` `upgrade()`:

```python
    # Op 2 — rename the QP Sales-section approver toggle.
    op.alter_column("users", "can_approve_sales_orders", new_column_name="can_approve_qp_sales")
```

and in `downgrade()` (reverse order, before op 1's reverse):

```python
    op.alter_column("users", "can_approve_qp_sales", new_column_name="can_approve_sales_orders")
```

- [ ] **Step 4: Prove the rename is total**

Run: `grep -rn can_approve_sales_orders app/ alembic/ tests/`
Expected: ONLY `alembic/versions/160_qp_so_po_approvers.py` (immutable history).

- [ ] **Step 5: Run the affected suites, verify pass**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_c2a_gates.py tests/test_c2b_sections.py tests/test_approvals_queue.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git -C /root/availai add app/models/auth.py app/routers/admin/users.py app/routers/htmx_views.py app/services/approvals/routing.py app/templates/htmx/partials/settings/users.html alembic/versions/163_sp2_sales_order_gate.py tests/test_c2a_gates.py tests/test_c2b_sections.py tests/test_approvals_queue.py
git -C /root/availai commit -m "feat(approvals): rename can_approve_sales_orders -> can_approve_qp_sales"
```

---

## Task 5: Rename `ApprovalGateType.SALES_ORDER` → `QP_SALES` (+ gate_type data migration)

Atomic enum + value-string rename. Full site list = **spec Appendix A.2**. The persisted `approval_requests.gate_type` value (`String(50)`, no CHECK) needs a data migration.

**Files:** `app/constants.py:1004`; `quality_plans.py` (6 member refs + `:440` string); `service.py:256`; `routing.py:87`; `queue.py:51` (value only — keeps the `"sales_orders"` lens key); `quality_plan_service.py:42,235,267`; `admin/users.py:422`; tests per A.2; append migration op #3.

**Interfaces:**
- Produces: `ApprovalGateType.QP_SALES == "qp_sales"`; the QP-sales gate routes/queues/section-dispatches under the new value.

- [ ] **Step 1: Rename the enum member** — `app/constants.py:1004`

```python
    QP_SALES = "qp_sales"
```

- [ ] **Step 2: Update every member + bare-value site in A.2.** Member refs → `ApprovalGateType.QP_SALES`; bare strings `"sales_order"` (gate compares) → `"qp_sales"`. **Do not** touch the false positives in A.2 (`sales_order_number`, `can_approve_*`, the `"sales_orders"` lens/tab keys, `set_sales_order_approver`, `CARD_KIND_SALES_ORDER`). Update the tests in A.2 (`test_approval_constants.py:25,34`, `test_approvals_queue.py:185` + member refs, `test_c2a_gates.py`, `test_c2b_sections.py`).

- [ ] **Step 3: Append migration op #3** — `163` `upgrade()`:

```python
    # Op 3 — rewrite persisted QP-sales gate values (free String(50) column, no CHECK).
    op.execute("UPDATE approval_requests SET gate_type = 'qp_sales' WHERE gate_type = 'sales_order'")
```

`downgrade()`:

```python
    op.execute("UPDATE approval_requests SET gate_type = 'sales_order' WHERE gate_type = 'qp_sales'")
```

- [ ] **Step 4: Prove the rename is total (no stray gate uses)**

Run: `grep -rn '"sales_order"' app/ ; grep -rn 'SALES_ORDER' app/`
Expected: no `"sales_order"` gate-value matches; `SALES_ORDER` only inside doc comments / `CARD_KIND_SALES_ORDER` (a card-kind constant, unrelated).

- [ ] **Step 5: Run the affected suites, verify pass**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_approval_constants.py tests/test_approvals_queue.py tests/test_c2a_gates.py tests/test_c2b_sections.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git -C /root/availai add app/constants.py app/routers/quality_plans.py app/services/approvals/service.py app/services/approvals/routing.py app/services/approvals/queue.py app/services/quality_plan_service.py app/routers/admin/users.py alembic/versions/163_sp2_sales_order_gate.py tests/test_approval_constants.py tests/test_approvals_queue.py tests/test_c2a_gates.py tests/test_c2b_sections.py
git -C /root/availai commit -m "feat(approvals): rename SALES_ORDER gate -> QP_SALES (+ gate_type data migration)"
```

---

## Task 6: QP-view inline Approve/Reject for the `qp_sales` section

The `qp_sales` gate leaves the lifecycle tabs in Task 9, so it must be actionable from the QP view first (spec §8 ordering constraint).

**Files:**
- Modify: `app/templates/htmx/partials/qp/detail.html` (Sales-section header)
- Modify: `app/routers/quality_plans.py` (pass the open `qp_sales` request + recipient eligibility into the QP view context)
- Test: `tests/test_c2b_sections.py`

**Interfaces:**
- Consumes: `QP_SALES` gate (Task 5), the existing `POST /v2/approvals/requests/{id}/decision` engine endpoint, the `approval_row` macro.
- Produces: an inline approve/reject control rendered in the QP Sales-section header for eligible PENDING recipients.

- [ ] **Step 1: Write the failing test** — `tests/test_c2b_sections.py`

```python
def test_qp_view_renders_inline_sales_approve_for_recipient(nonadmin_client, db_session, qp_with_open_sales_gate):
    qp, approver = qp_with_open_sales_gate  # approver has can_approve_qp_sales=True, routed PENDING
    r = nonadmin_client(approver).get(f"/v2/qp/{qp.id}")
    assert r.status_code == 200
    assert "/v2/approvals/requests/" in r.text  # inline decision control present
    assert "Approve" in r.text and "Reject" in r.text
```

- [ ] **Step 2: Run it, verify it fails**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_c2b_sections.py::test_qp_view_renders_inline_sales_approve_for_recipient -v --override-ini="addopts="`
Expected: FAIL — no inline control in the QP view.

- [ ] **Step 3: Thread the open request into the QP context** — in the `quality_plans.py` view handler, look up the open `QP_SALES` `ApprovalRequest` for this QP and whether `user` is a PENDING recipient (reuse the queue's RowVM builder or the recipient query at `service.py:177-185`), and pass `sales_gate_request` + `sales_gate_can_act` into the template context.

- [ ] **Step 4: Render the affordance** — `qp/detail.html` Sales-section header (single-quote any `|tojson`; keep `"` out of double-quoted Alpine attrs):

```jinja
{% if sales_gate_request and sales_gate_can_act %}
  {% from "htmx/partials/approvals/_macros.html" import approval_row %}
  {{ approval_row(sales_gate_request) }}
{% endif %}
```

If `approval_row` assumes a buy-plan subject, add a QP-subject branch (subject label `QP #<id>`, href `/v2/qp/<id>`) — the queue already builds QP-subject RowVMs, so the data is available.

- [ ] **Step 5: Run it, verify it passes; run the section suite**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_c2b_sections.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git -C /root/availai add app/routers/quality_plans.py app/templates/htmx/partials/qp/detail.html tests/test_c2b_sections.py
git -C /root/availai commit -m "feat(approvals): inline approve/reject for the QP Sales section (qp_sales gate)"
```

---

## Task 7: Retire `QualityPlan.sales_so_number` → canonical `BuyPlan.sales_order_number`

Full site list = **spec Appendix A.3**. Backfill-then-drop in migration `163`.

**Files:** `app/models/quality_plan.py:67` (drop); `_section_sales.html:34,95`; `quality_plans.py:67` (drop `_SALES_FIELDS` entry); `quality_plan_service.py:185` (replace required-tuple entry with a buy-plan check); tests `test_c2a_gates.py:124`, `test_c2b_sections.py:95,113-117,366`; append migration ops #4–5.

**Interfaces:**
- Consumes: `BuyPlan.sales_order_number` (already canonical).
- Produces: QP Sales-section completeness reads `qp.buy_plan.sales_order_number`; no editable SO# in the QP form (decision #8).

- [ ] **Step 1: Update the completeness test to source the SO# from the buy plan** — `tests/test_c2b_sections.py` (the `fill_sales` fixture sets `bp.sales_order_number` instead of `qp.sales_so_number`; `test_sales_section_missing_so_number_blocks` leaves `bp.sales_order_number` blank). Run it — it FAILS because the check still reads `qp.sales_so_number`.

- [ ] **Step 2: Drop the model column** — `app/models/quality_plan.py:67` (remove the line; update the `:64-66` comment block so it no longer claims the SO# lives here).

- [ ] **Step 3: Repoint the completeness check** — `quality_plan_service.py`: remove `("sales_so_number", "Sales Order #")` from `_SALES_REQUIRED` and add, in `_validate_sales_section`:

```python
    bp = qp.buy_plan
    if bp is None or not (bp.sales_order_number or "").strip():
        errors.append("Sales Order # is required")
```

Add `joinedload(QualityPlan.buy_plan)` to the sales-section read path so `qp.buy_plan` is loaded.

- [ ] **Step 4: Remove the editable input + drop the whitelist entry** — delete `_section_sales.html:95` (the `txt('sales_so_number', …)` input); repoint the read-only display `:34` to `qp.buy_plan.sales_order_number` (or delete it — the QP header already shows it at `detail.html:90`); delete `"sales_so_number": "str"` from `_SALES_FIELDS` (`quality_plans.py:67`).

- [ ] **Step 5: Append migration ops #4–5** — `163` `upgrade()`:

```python
    # Op 4 — length pre-check (no silent truncation: BuyPlan col is String(100)).
    bind = op.get_bind()
    over = bind.execute(sa.text(
        "SELECT count(*) FROM quality_plans WHERE length(sales_so_number) > 100"
    )).scalar()
    if over:
        raise RuntimeError(f"{over} quality_plans.sales_so_number values exceed 100 chars; widen target or clean data first")
    # Op 4b — backfill the canonical SO# where the buy plan's is blank.
    op.execute(
        "UPDATE buy_plans_v3 SET sales_order_number = q.sales_so_number "
        "FROM quality_plans q WHERE q.buy_plan_id = buy_plans_v3.id "
        "AND (buy_plans_v3.sales_order_number IS NULL OR buy_plans_v3.sales_order_number = '') "
        "AND q.sales_so_number IS NOT NULL"
    )
    # Op 5 — drop the duplicate column.
    op.drop_column("quality_plans", "sales_so_number")
```

`downgrade()` (re-add empty; backfill not reversed):

```python
    op.add_column("quality_plans", sa.Column("sales_so_number", sa.String(length=255), nullable=True))
```

- [ ] **Step 6: Prove the retirement is total + run the suite**

Run: `grep -rn sales_so_number app/ ; TESTING=1 PYTHONPATH=/root/availai pytest tests/test_c2a_gates.py tests/test_c2b_sections.py -v`
Expected: `grep` returns nothing under `app/`; tests PASS.

- [ ] **Step 7: Commit**

```bash
git -C /root/availai add app/models/quality_plan.py app/services/quality_plan_service.py app/routers/quality_plans.py app/templates/htmx/partials/qp/_section_sales.html alembic/versions/163_sp2_sales_order_gate.py tests/test_c2a_gates.py tests/test_c2b_sections.py
git -C /root/availai commit -m "feat(approvals): retire QualityPlan.sales_so_number -> canonical BuyPlan.sales_order_number"
```

---

## Task 8: Queue/tab remap — move the BUY_PLAN gate to the Sales Orders tab (no KeyError)

Full edits = **spec Appendix A.5**.

**Files:** `app/services/approvals/queue.py:48-61`; `app/routers/htmx_views.py:11044-11049,11128-11132`; `app/templates/htmx/partials/approvals/_tab_buy_plans.html:9`; tests `test_approvals_queue.py` (A.5 list), `test_approvals_module_shell.py:197-205,187-194`.

**Interfaces:**
- Consumes: `BUY_PLAN` gate; `can_approve_buy_plans` right.
- Produces: the `sales_orders` lens surfaces BUY_PLAN-gate pending requests; the `buy_plans` lens is gate-less (no pending section, no KeyError).

- [ ] **Step 1: Update the queue tests first** — `tests/test_approvals_queue.py` per A.5 (rebind the smart-default test to `sales_orders`, point BUY_PLAN-seed tests at the `"sales_orders"` tab, move QP-subject coverage to `purchase_orders`). Run — FAILS against the current mapping.

- [ ] **Step 2: Remap `queue.py:48-61`**

```python
TAB_ORDER = ["sales_orders", "purchase_orders", "prepayments"]
TAB_GATE = {
    "sales_orders": ApprovalGateType.BUY_PLAN,
    "purchase_orders": ApprovalGateType.PURCHASE_ORDER,
    "prepayments": ApprovalGateType.PREPAYMENT,
}
TAB_LABEL = {
    "sales_orders": "Sales Orders",
    "purchase_orders": "Purchase Orders",
    "prepayments": "Vendor Prepayments",
}
DEFAULT_TAB = "sales_orders"
```

Update the module + `_smart_default_tab` docstrings ("three gate tabs"; "tie/zero → Sales Orders").

- [ ] **Step 3: Remap `_TAB_APPROVE_ATTR` + guard the handler** — `htmx_views.py:11044-11049`

```python
_TAB_APPROVE_ATTR = {
    "sales_orders": "can_approve_buy_plans",
    "purchase_orders": "can_approve_pos",
    "prepayments": "can_approve_prepayments",
}
```

`:11128-11132`:

```python
    ctx = _base_ctx(request, user, "buy-plans")
    if lens in _TAB_APPROVE_ATTR:
        from ..services.approvals.queue import build_queue_view
        ctx["view"] = build_queue_view(db, user, lens)
        ctx["show_pending"] = bool(getattr(user, _TAB_APPROVE_ATTR[lens], False))
```

- [ ] **Step 4: Delete the pending include from the Buy Plans tab** — `_tab_buy_plans.html:9` (remove `{% include "htmx/partials/approvals/_pending_section.html" %}`; update the header comment).

- [ ] **Step 5: Update the shell test URLs** — `test_approvals_module_shell.py:197-205` (approver section now at `/v2/partials/approvals/sales-orders`), `:187-194` (move the absent-section assertion). Run the suites.

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_approvals_queue.py tests/test_approvals_module_shell.py -v`
Expected: PASS, no `KeyError` on any tab.

- [ ] **Step 6: Commit**

```bash
git -C /root/availai add app/services/approvals/queue.py app/routers/htmx_views.py app/templates/htmx/partials/approvals/_tab_buy_plans.html tests/test_approvals_queue.py tests/test_approvals_module_shell.py
git -C /root/availai commit -m "feat(approvals): repoint Sales Orders tab to BUY_PLAN gate; Buy Plans tab gate-less"
```

---

## Task 9: Board `statuses` filter — lifecycle split across the two tabs

Backward-compatible: the default keeps the standalone `/board` route + Supervise unchanged.

**Files:**
- Modify: `app/services/buyplan_hub.py` (`deals_board` optional `statuses` param)
- Modify: `app/templates/htmx/partials/approvals/_tab_buy_plans.html` (board → `statuses=[ACTIVE, HALTED]`)
- Modify: `app/templates/htmx/partials/approvals/_tab_sales_orders.html` (board → `statuses=[DRAFT, PENDING]`)
- Test: `tests/test_buyplan_hub_board.py`

**Interfaces:**
- Produces: `deals_board(db, user, scope, statuses=None)` — `statuses=None` ⇒ today's full set (DRAFT/PENDING/ACTIVE/HALTED); a list filters to those statuses.

- [ ] **Step 1: Write the failing test** — `tests/test_buyplan_hub_board.py`

```python
def test_deals_board_status_filter(db_session, board_fixture):
    from app.services.buyplan_hub import deals_board
    from app.constants import BuyPlanStatus
    user, _ = board_fixture  # seeds DRAFT + ACTIVE plans
    active_only = deals_board(db_session, user, scope="all", statuses=[BuyPlanStatus.ACTIVE.value])
    ids = {row["id"] for col in active_only["columns"] for row in col["cards"]}
    assert all(db_session.get(__import__("app.models.buy_plan", fromlist=["BuyPlan"]).BuyPlan, i).status
               == BuyPlanStatus.ACTIVE.value for i in ids)
    full = deals_board(db_session, user, scope="all")  # default unchanged
    assert len(full["columns"]) >= len(active_only["columns"])
```

(Adapt the assertion shape to the real `deals_board` return — the point is: filtered ⊆ default, and default is unchanged.)

- [ ] **Step 2: Run it, verify it fails**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_buyplan_hub_board.py::test_deals_board_status_filter -v --override-ini="addopts="`
Expected: FAIL — `deals_board` has no `statuses` kwarg.

- [ ] **Step 3: Add the optional filter** — `buyplan_hub.py` `deals_board(..., statuses=None)`: when `statuses` is provided, add `BuyPlan.status.in_(statuses)` to the base query; when `None`, behave exactly as today. Add `joinedload(BuyPlan.requisition)→customer_site→company` alongside the existing quote joinedload so SO-origin cards are N+1-free.

- [ ] **Step 4: Pass filters from the two tabs** — `_tab_buy_plans.html` renders the board with `statuses=[ACTIVE, HALTED]`; `_tab_sales_orders.html` renders it with `statuses=[DRAFT, PENDING]` (whichever layer — view function or template macro — currently supplies the board; thread the kwarg from there). The standalone `GET /v2/partials/buy-plans/board` route passes **no** `statuses` (default).

- [ ] **Step 5: Run board + standalone-route tests, verify pass**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_buyplan_hub_board.py -v`
Expected: PASS; the standalone `/board` test (if present) unchanged.

- [ ] **Step 6: Commit**

```bash
git -C /root/availai add app/services/buyplan_hub.py app/templates/htmx/partials/approvals/_tab_buy_plans.html app/templates/htmx/partials/approvals/_tab_sales_orders.html tests/test_buyplan_hub_board.py
git -C /root/availai commit -m "feat(approvals): backward-compatible board statuses filter for the SO/buy-plan tab split"
```

---

## Task 10: New Sales Order origination UI

Wire the New-SO button → requisition picker → quote-builder picker → create endpoint, on the Sales Orders tab.

**Files:**
- Modify: `app/routers/htmx_views.py` (`GET /v2/partials/approvals/sales-orders/new`, `POST /v2/partials/approvals/sales-orders/create`)
- Create: `app/templates/htmx/partials/approvals/_sales_order_new.html` (requisition picker + builder launch)
- Modify: `app/templates/htmx/partials/approvals/_tab_sales_orders.html` (New SO button)
- Test: `tests/test_sales_order_origination.py` (new)

**Interfaces:**
- Consumes: `create_sales_order_from_offers` (Task 3); the quote-builder picker partials (`quote_builder.py`); the buy-plan detail partial (carries the submit form).
- Produces: the end-to-end UI path New SO → DRAFT BuyPlan → submit.

- [ ] **Step 1: Write the failing route tests** — `tests/test_sales_order_origination.py`

```python
def test_new_sales_order_picker_lists_open_requisitions(nonadmin_client, db_session, buyer, open_req_with_offers):
    r = nonadmin_client(buyer).get("/v2/partials/approvals/sales-orders/new")
    assert r.status_code == 200
    assert str(open_req_with_offers.id) in r.text


def test_create_sales_order_returns_draft_detail(nonadmin_client, db_session, buyer, open_req_with_offers):
    sel = open_req_with_offers  # fixture exposes requirement->offer selections + sell prices
    r = nonadmin_client(buyer).post(
        "/v2/partials/approvals/sales-orders/create",
        data={"requisition_id": sel.id, **sel.form_fields},
    )
    assert r.status_code == 200
    assert "Submit" in r.text  # buy-plan detail submit form
```

- [ ] **Step 2: Run them, verify they fail**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sales_order_origination.py -v --override-ini="addopts="`
Expected: FAIL — routes 404.

- [ ] **Step 3: Add the picker route** — `htmx_views.py` `GET /v2/partials/approvals/sales-orders/new`: render `_sales_order_new.html` with open requisitions that have ≥1 selectable offer (`RequisitionStatus in {open, rfqs_sent, offers, quoted}` and ≥1 requirement with an offer). Selecting one launches the existing quote-builder picker pointed at that requisition (`get_builder_data`/`apply_smart_defaults`).

- [ ] **Step 4: Add the create route** — `POST /v2/partials/approvals/sales-orders/create`: parse per-requirement offer selections + sell prices, call `create_sales_order_from_offers(requisition_id, selections, sell_prices, db, user)`, then render the buy-plan detail partial (the existing submit form) swapped inline. On the builder's `ValueError` (duplicate open SO), return the existing SO's detail with a toast, not a 500.

- [ ] **Step 5: Add the New SO button** — `_tab_sales_orders.html`: `<button hx-get="/v2/partials/approvals/sales-orders/new" hx-target="..." ...>New Sales Order</button>` (follow the tab's existing button/hx conventions; explicit `hx-target`).

- [ ] **Step 6: Run the origination suite, verify pass**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sales_order_origination.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git -C /root/availai add app/routers/htmx_views.py app/templates/htmx/partials/approvals/_sales_order_new.html app/templates/htmx/partials/approvals/_tab_sales_orders.html tests/test_sales_order_origination.py
git -C /root/availai commit -m "feat(approvals): New Sales Order origination UI (picker -> builder -> submit)"
```

---

## Task 11: Migration round-trip, full suite, docs, live verify

**Files:**
- Verify: `alembic/versions/163_sp2_sales_order_gate.py`
- Modify: `docs/APP_MAP_DATABASE.md` (38, 359), `docs/APP_MAP_INTERACTIONS.md` (1344,1975,5196,5271)

- [ ] **Step 1: Round-trip migration 163 on a throwaway Postgres** (never the staging `db`)

```bash
docker run --rm -d --name sp2pg -e POSTGRES_PASSWORD=x -e POSTGRES_DB=avail -p 55432:5432 postgres:16-alpine
sleep 4
cd /root/availai
DATABASE_URL=postgresql://postgres:x@localhost:55432/avail alembic upgrade head
DATABASE_URL=postgresql://postgres:x@localhost:55432/avail alembic downgrade -1
DATABASE_URL=postgresql://postgres:x@localhost:55432/avail alembic upgrade head
docker stop sp2pg
```
Expected: clean upgrade→downgrade→upgrade. (Downgrade is clean here because the throwaway DB has no `quote_id IS NULL` rows.)

- [ ] **Step 2: Verify single head**

Run: `cd /root/availai && alembic heads`
Expected: one head, `163_sp2_sales_order_gate`.

- [ ] **Step 3: Update the APP_MAP docs** — `APP_MAP_DATABASE.md:38` (toggle rename), `:359` (QP sales cols 18→17, drop `sales_so_number`); `APP_MAP_INTERACTIONS.md:1344,1975,5196,5271` (gate/column renames + the SO-origination flow). Note `BuyPlan.quote_id` is now nullable and the Sales Orders tab maps to the BUY_PLAN gate.

- [ ] **Step 4: Full suite + pre-commit**

```bash
cd /root/availai
TESTING=1 PYTHONPATH=/root/availai pytest tests/ -q
pre-commit run --all-files   # run twice if docformatter rewraps
```
Expected: green suite; clean pre-commit.

- [ ] **Step 5: Commit + push**

```bash
git -C /root/availai add docs/APP_MAP_DATABASE.md docs/APP_MAP_INTERACTIONS.md
git -C /root/availai commit -m "docs(approvals): SP-2 APP_MAP sync (quote_id nullable, qp_sales gate, canonical SO#)"
git -C /root/availai push
```

- [ ] **Step 6: Open the PR** (use `gh api -X PATCH` for edits; `gh pr create` for open). Summarize the 5 migration ops + the ordering note (QP-view inline action ships with the tab remap). Then deploy from main after merge + live-verify on real PG (no 500s on `/v2/approvals` tabs, the New-SO flow, and the QP inline approve).

---

## Self-Review

**Spec coverage:** §4.1 quote_id→T1; §4.2 origination→T3+T10; §4.3 fallbacks→T2; §4.4 Sales Orders tab→T8+T9+T10; §4.5 Buy Plans tab→T8+T9; §4.6 canonical SO# + editable-input removal→T7; §4.7 de-collision→T4+T5+T6; §4.8 landing→T8 (unchanged, intentional). §5 data model→T1/T4/T5/T7 + migration T11. §6 origination→T3/T10. §7 tabs→T8/T9. §8 QP de-collision + ordering→T4/T5/T6/T7. §10 migration→T1/T4/T5/T7 ops, round-trip T11. §11 testing→each task's TDD steps. §12 risks→split-deploy (atomic migration T5/T11), getattr-soft-fail (T4), blank customer (T2), orphaned gate (T6 before T8). All covered.

**Placeholder scan:** mechanical-rename tasks reference spec Appendix A for the exhaustive site list (the Appendix is committed alongside, canonical, and DRY — re-typing 40 test-file line numbers would duplicate it); new-logic tasks (T2/T3/T6/T9/T10) carry actual code. No TBD/TODO.

**Type consistency:** `create_sales_order_from_offers(requisition_id, selections, sell_prices, db, user)` and `_assemble_buy_plan(requisition, chosen_offers, sell_prices, customer_region, db)` are used identically in T3 and T10. `deals_board(..., statuses=None)` consistent T9↔T8 tabs. Gate value `qp_sales` / column `can_approve_qp_sales` consistent T4↔T5↔T6↔T8.
