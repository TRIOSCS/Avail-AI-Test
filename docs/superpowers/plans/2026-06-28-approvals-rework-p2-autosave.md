# Approvals Rework — Phase 2: Autosave / Lost-Work Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended)
> or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Persist the New-Sales-Order builder's offer/qty/price picks server-side **as they are entered**, so a
crash or reload never wipes the salesperson's work (the exact failure that started this rework), and add
soft-delete + restore for drafts.

**Architecture:** The builder already originates a `DRAFT` quote-less `BuyPlan` (the canonical "open Sales
Order") via `create_sales_order_from_offers` → `_assemble_buy_plan`. We turn that one-shot origination into an
**upsert** the builder calls on every debounced change. A new autosave route persists the partial state into the
DRAFT; the GET builder route **hydrates** the form back from the DRAFT on reopen (today it hardcodes empty
picks). A debounced `hx-post` + an inline "Saved" indicator + a navigate-away guard wire it in the UI. Soft-delete
adds a `deleted_at` column + restore route with an Undo affordance.

**Tech Stack:** FastAPI · SQLAlchemy 2.0 · PostgreSQL 16 · Alembic · HTMX 2.x · Alpine.js 3.x · Jinja2 · pytest (xdist).

## Global Constraints

- **Stack is HTMX + Alpine + Jinja2 — never React/SPA.** Reuse the existing inline-edit pattern
  (`_field_edit.html`: `hx-post` + `hx-trigger` debounced), the `template_response()` contract, and the toast/store
  primitives. No new UI conventions.
- **No band-aids — root-cause only.** **Always write tests with new code (TDD: failing test first).**
- **All schema changes via Alembic.** Claim the migration number in `MIGRATION_NUMBERS_IN_FLIGHT.txt`;
  upgrade→downgrade→upgrade round-trip on a **throwaway** PG (never staging); verify a single head. Alembic
  revision ids **≤ 32 chars** (PG `VARCHAR(32)`).
- **Run targeted tests only** (`TESTING=1 PYTHONPATH=/root/availai pytest …`); never the full suite concurrent
  with the ~02:30 nightly cron. cwd matters in a worktree.
- **Status values via `BuyPlanStatus`** (`app/constants.py`), never raw strings. `db.get(Model, id)`, not
  `db.query().get()`. Loguru, not `print()`.
- **Alpine double-quote rule:** never put a literal `"` inside a double-quoted Alpine attribute; embed `tojson`
  in a single-quoted attribute. **htmx `[filter]` must hug the event name** (we use `delay:`/`changed` modifiers,
  not `[...]`).
- This phase is **independently shippable** and adds **no** new approval behavior — it only stops losing input.

## Scope (explicit — no silent caps)

- **In scope:** builder DRAFT autosave (create-or-update), reopen-hydration, inline "Saved" indicator,
  navigate-away guard, finalize via upsert, soft-delete + restore with Undo.
- **Deliberately deferred (stated, not silent):**
  - The submit/approve/PO **modal-field** autosave (`buy_plans/detail.html`) lands with each modal's **own** stage
    rework (P3/P4/P5) — those modals are being rebuilt there; autosaving fields on soon-to-be-replaced markup is
    throwaway work. The reusable primitives (upsert seam, inline indicator, guard) built here are what they adopt.
  - A per-change **undo stack** for autosave field edits is deferred (needs a change-snapshot history). The
    concrete "undo" in this phase is **restore-after-soft-delete** (§Task 8). The draft itself is always recoverable.

## File Structure

| File | Responsibility | Action |
|------|----------------|--------|
| `app/services/buyplan_builder.py` | DRAFT origination + the new upsert/hydrate seam | Modify |
| `app/models/buy_plan.py` | `BuyPlan` model — add `deleted_at` | Modify |
| `app/routers/htmx_views.py` | builder GET (hydrate), autosave POST, create POST (finalize), soft-delete/restore POST | Modify |
| `app/templates/htmx/partials/approvals/_sales_order_new.html` | autosave wrapper + inline indicator + guard | Modify |
| `alembic/versions/165_buyplan_soft_delete.py` | `deleted_at` column migration | Create |
| `tests/test_buyplan_autosave.py` | service + route tests for upsert/hydrate/soft-delete | Create |

---

## Task 1: Extract `_populate_plan_lines` from `_assemble_buy_plan` (enabler refactor)

`_assemble_buy_plan` (`buyplan_builder.py:186-237`) both **creates** a plan and **populates** its lines. The upsert
(Task 2) must repopulate an **existing** draft. Extract the populate logic into a helper that operates on a given
plan and can skip the expensive AI generation (autosave runs frequently).

**Files:**
- Modify: `app/services/buyplan_builder.py:186-237`
- Test: `tests/test_buyplan_autosave.py` (new)

**Interfaces:**
- Produces: `_populate_plan_lines(plan: BuyPlan, requisition: Requisition, chosen_offers: dict[int, int], sell_prices: dict[int, float], customer_region: str | None, db: Session, *, with_ai: bool = True) -> None`
  — clears `plan.lines`, rebuilds them from `chosen_offers`/`sell_prices`, recomputes `total_cost`/`total_revenue`/
  `total_margin_pct`; runs `generate_ai_summary`/`generate_ai_flags` only when `with_ai=True`.

- [ ] **Step 1: Write the failing test.** Add to `tests/test_buyplan_autosave.py` (reuse the `so_origin_fixture`
  pattern from `tests/test_buyplan_builder_so_origin.py:20-97` — copy its fixture into this file or import it):

```python
"""Tests for builder DRAFT autosave / lost-work (Phase 2).

Covers _populate_plan_lines extraction, upsert_draft_sales_order, builder hydration,
and soft-delete/restore. Mirrors the seed shape of tests/test_buyplan_builder_so_origin.py.
"""

from datetime import datetime, timezone

import pytest

from app.constants import BuyPlanStatus
from app.models.auth import User
from app.models.buy_plan import BuyPlan
from app.models.customer import Company, CustomerSite
from app.models.sourcing import Offer, Requirement, Requisition
from app.models.vendor import VendorCard


@pytest.fixture
def so_fixture(db_session):
    """Requisition + one requirement + one active scored offer (SO builder seed)."""
    user = User(email="autosave@trioscs.com", name="Autosave Sales", role="sales",
                azure_id="az-autosave", created_at=datetime.now(timezone.utc))
    db_session.add(user); db_session.flush()
    company = Company(name="Autosave Corp", is_active=True, created_at=datetime.now(timezone.utc))
    db_session.add(company); db_session.flush()
    site = CustomerSite(company_id=company.id, site_name="HQ", country="US",
                        created_at=datetime.now(timezone.utc))
    db_session.add(site); db_session.flush()
    req = Requisition(name="REQ-AUTOSAVE", status="open", created_by=user.id,
                      customer_site_id=site.id, created_at=datetime.now(timezone.utc))
    db_session.add(req); db_session.flush()
    requirement = Requirement(requisition_id=req.id, primary_mpn="AS-MPN-1", target_qty=100,
                              target_price=1.0, created_at=datetime.now(timezone.utc))
    db_session.add(requirement); db_session.flush()
    vendor = VendorCard(normalized_name="as vendor", display_name="AS Vendor",
                        created_at=datetime.now(timezone.utc))
    db_session.add(vendor); db_session.flush()
    offer = Offer(requisition_id=req.id, requirement_id=requirement.id, vendor_card_id=vendor.id,
                  vendor_name="AS Vendor", mpn="AS-MPN-1", qty_available=100, unit_price=0.50,
                  status="active", entered_by_id=user.id, created_at=datetime.now(timezone.utc))
    db_session.add(offer); db_session.flush()
    return req, requirement, offer, user


def test_populate_plan_lines_skips_ai_when_disabled(db_session, so_fixture):
    from app.services.buyplan_builder import _populate_plan_lines

    req, requirement, offer, user = so_fixture
    plan = BuyPlan(requisition_id=req.id, status=BuyPlanStatus.DRAFT.value)
    _populate_plan_lines(plan, req, {requirement.id: offer.id}, {requirement.id: 1.25},
                         None, db_session, with_ai=False)

    assert len(plan.lines) == 1
    assert float(plan.lines[0].unit_sell) == 1.25
    assert plan.ai_summary is None          # AI skipped
    assert plan.ai_flags in (None, [])      # AI skipped
```

- [ ] **Step 2: Run it; verify it fails.**
  Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_buyplan_autosave.py::test_populate_plan_lines_skips_ai_when_disabled -v --override-ini="addopts="`
  Expected: FAIL — `ImportError: cannot import name '_populate_plan_lines'`.

- [ ] **Step 3: Implement the refactor.** Replace `_assemble_buy_plan` (`buyplan_builder.py:186-237`) body so it
  delegates to the new helper:

```python
def _assemble_buy_plan(
    requisition: Requisition,
    chosen_offers: dict[int, int],
    sell_prices: dict[int, float],
    customer_region: str | None,
    db: Session,
) -> BuyPlan:
    """Build (unsaved) BuyPlan + lines from chosen offers — shared by quote and SO paths."""
    plan = BuyPlan(requisition_id=requisition.id, status=BuyPlanStatus.DRAFT.value)
    _populate_plan_lines(plan, requisition, chosen_offers, sell_prices, customer_region, db, with_ai=True)
    return plan


def _populate_plan_lines(
    plan: BuyPlan,
    requisition: Requisition,
    chosen_offers: dict[int, int],
    sell_prices: dict[int, float],
    customer_region: str | None,
    db: Session,
    *,
    with_ai: bool = True,
) -> None:
    """(Re)build ``plan``'s lines + financials from chosen offers. Clears any existing lines first
    so this is safe to call on an in-progress DRAFT (the autosave upsert path). AI summary/flags are
    regenerated only when ``with_ai`` is True (autosave skips them — they are expensive)."""
    requirements = db.query(Requirement).filter(Requirement.requisition_id == requisition.id).all()
    if not requirements:
        raise ValueError(f"No requirements found for requisition {requisition.id}")

    plan.lines = []  # repopulate idempotently

    total_cost = 0.0
    total_revenue = 0.0
    for req in requirements:
        sell_price = sell_prices.get(req.id) if sell_prices else None
        lines = _build_lines_for_requirement(req, customer_region, db, chosen_offers.get(req.id), sell_price)
        for line in lines:
            line.buy_plan = plan
            if line.unit_cost and line.quantity:
                total_cost += float(line.unit_cost) * line.quantity
            if line.unit_sell and line.quantity:
                total_revenue += float(line.unit_sell) * line.quantity

    plan.total_cost = round(total_cost, 2) if total_cost else None
    plan.total_revenue = round(total_revenue, 2) if total_revenue else None
    plan.total_margin_pct = (
        round(((total_revenue - total_cost) / total_revenue) * 100, 2) if total_revenue and total_revenue > 0 else None
    )

    if with_ai:
        plan.ai_summary = generate_ai_summary(plan)
        plan.ai_flags = [f.__dict__ if hasattr(f, "__dict__") else f for f in generate_ai_flags(plan, db, customer_region)]
```

- [ ] **Step 4: Run the new test + the existing builder suite; verify all pass.**
  Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_buyplan_autosave.py tests/test_buyplan_builder_so_origin.py -v --override-ini="addopts="`
  Expected: PASS (the existing `test_create_sales_order_from_offers_*` tests still green — pure refactor).

- [ ] **Step 5: Commit.**
```bash
git add app/services/buyplan_builder.py tests/test_buyplan_autosave.py
git commit -m "refactor(buyplan): extract _populate_plan_lines (autosave enabler)"
```

---

## Task 2: `upsert_draft_sales_order` service (create-or-update the DRAFT)

**Files:**
- Modify: `app/services/buyplan_builder.py`
- Test: `tests/test_buyplan_autosave.py`

**Interfaces:**
- Consumes: `_populate_plan_lines`, `find_open_sales_order` (`buyplan_builder.py:100`), `DuplicateSalesOrderError`.
- Produces: `upsert_draft_sales_order(db: Session, user: User, requisition_id: int, selections: dict[int, int], sell_prices: dict[int, float], *, with_ai: bool = False) -> BuyPlan`
  — returns the open `DRAFT` SO updated to the given selections; creates one if none exists; **raises
  `DuplicateSalesOrderError`** if the open plan is already `PENDING`/`ACTIVE` (submitted — cannot be rebuilt).

- [ ] **Step 1: Write the failing tests.**

```python
def test_upsert_creates_draft_when_none_exists(db_session, so_fixture):
    from app.services.buyplan_builder import upsert_draft_sales_order

    req, requirement, offer, user = so_fixture
    plan = upsert_draft_sales_order(db_session, user, req.id, {requirement.id: offer.id},
                                    {requirement.id: 1.25})
    assert plan.id is not None
    assert plan.status == BuyPlanStatus.DRAFT.value
    assert plan.quote_id is None
    assert float(plan.lines[0].unit_sell) == 1.25


def test_upsert_updates_existing_draft_in_place(db_session, so_fixture):
    from app.services.buyplan_builder import upsert_draft_sales_order

    req, requirement, offer, user = so_fixture
    p1 = upsert_draft_sales_order(db_session, user, req.id, {requirement.id: offer.id},
                                  {requirement.id: 1.25})
    p2 = upsert_draft_sales_order(db_session, user, req.id, {requirement.id: offer.id},
                                  {requirement.id: 2.50})
    assert p2.id == p1.id                       # same draft, updated in place
    assert float(p2.lines[0].unit_sell) == 2.50
    assert db_session.query(BuyPlan).filter(BuyPlan.requisition_id == req.id).count() == 1


def test_upsert_raises_on_submitted_plan(db_session, so_fixture):
    from app.services.buyplan_builder import DuplicateSalesOrderError, upsert_draft_sales_order

    req, requirement, offer, user = so_fixture
    plan = upsert_draft_sales_order(db_session, user, req.id, {requirement.id: offer.id}, {})
    plan.status = BuyPlanStatus.PENDING.value   # simulate submitted
    db_session.commit()
    with pytest.raises(DuplicateSalesOrderError):
        upsert_draft_sales_order(db_session, user, req.id, {requirement.id: offer.id}, {})
```

- [ ] **Step 2: Run; verify they fail.**
  Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_buyplan_autosave.py -k upsert -v --override-ini="addopts="`
  Expected: FAIL — `cannot import name 'upsert_draft_sales_order'`.

- [ ] **Step 3: Implement.** Add to `buyplan_builder.py` (after `create_sales_order_from_offers`):

```python
def upsert_draft_sales_order(
    db: Session,
    user: User,
    requisition_id: int,
    selections: dict[int, int],
    sell_prices: dict[int, float],
    *,
    with_ai: bool = False,
) -> BuyPlan:
    """Create-or-update the open DRAFT Sales Order for a requisition (the autosave seam).

    Finds the open quote-less plan via ``find_open_sales_order``. If it is a DRAFT, repopulates it
    in place from the (possibly partial) selections. If it is already PENDING/ACTIVE (submitted),
    raises ``DuplicateSalesOrderError`` — a submitted plan must not be silently rebuilt. If none
    exists, creates a DRAFT. ``with_ai=False`` (the autosave default) skips the expensive AI pass.
    """
    requisition = db.get(Requisition, requisition_id)
    if requisition is None:
        raise ValueError(f"Requisition {requisition_id} not found")

    customer_region = None
    if requisition.customer_site:
        customer_region = _country_to_region(requisition.customer_site.country or requisition.customer_site.state)

    existing = find_open_sales_order(db, requisition_id)
    if existing is not None and existing.status != BuyPlanStatus.DRAFT.value:
        raise DuplicateSalesOrderError(existing.id, existing.status)

    plan = existing or BuyPlan(requisition_id=requisition.id, status=BuyPlanStatus.DRAFT.value)
    if existing is None:
        plan.submitted_by_id = getattr(user, "id", None)
        db.add(plan)
    _populate_plan_lines(plan, requisition, selections, sell_prices, customer_region, db, with_ai=with_ai)
    db.commit()
    logger.info("Autosaved DRAFT Sales Order #{} (req {}, {} selection(s), user {})",
                plan.id, requisition_id, len(selections), getattr(user, "id", None))
    return plan
```

- [ ] **Step 4: Run; verify pass.**
  Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_buyplan_autosave.py -k upsert -v --override-ini="addopts="`
  Expected: PASS.

- [ ] **Step 5: Commit.**
```bash
git add app/services/buyplan_builder.py tests/test_buyplan_autosave.py
git commit -m "feat(buyplan): upsert_draft_sales_order — autosave create-or-update seam"
```

---

## Task 3: Hydrate the builder from the open DRAFT on reopen

`get_builder_data` (`quote_builder_service.py:206-207`) hardcodes `selected_offer_id=None, sell_price=None`. Add a
hydration helper and call it in the GET builder route so reopening restores the saved picks (the crash-recovery
payoff once autosave persists them).

**Files:**
- Modify: `app/services/buyplan_builder.py` (helper)
- Modify: `app/routers/htmx_views.py:11199-11228` (`sales_order_new`, GET path)
- Test: `tests/test_buyplan_autosave.py`

**Interfaces:**
- Produces: `hydrate_builder_lines_from_draft(lines: list[dict], db: Session, requisition_id: int) -> None`
  — for each builder line dict, if the open DRAFT SO has a matching line for that `requirement_id`, overlays
  `selected_offer_id` and `sell_price` from the draft (in place).

- [ ] **Step 1: Write the failing test.**

```python
def test_hydrate_overlays_saved_picks(db_session, so_fixture):
    from app.services.buyplan_builder import hydrate_builder_lines_from_draft, upsert_draft_sales_order

    req, requirement, offer, user = so_fixture
    upsert_draft_sales_order(db_session, user, req.id, {requirement.id: offer.id}, {requirement.id: 3.0})

    lines = [{"requirement_id": requirement.id, "selected_offer_id": None, "sell_price": None}]
    hydrate_builder_lines_from_draft(lines, db_session, req.id)

    assert lines[0]["selected_offer_id"] == offer.id
    assert float(lines[0]["sell_price"]) == 3.0


def test_hydrate_noop_without_draft(db_session, so_fixture):
    from app.services.buyplan_builder import hydrate_builder_lines_from_draft

    req, requirement, offer, user = so_fixture
    lines = [{"requirement_id": requirement.id, "selected_offer_id": None, "sell_price": None}]
    hydrate_builder_lines_from_draft(lines, db_session, req.id)
    assert lines[0]["selected_offer_id"] is None      # nothing saved → unchanged
```

- [ ] **Step 2: Run; verify fail.**
  Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_buyplan_autosave.py -k hydrate -v --override-ini="addopts="`
  Expected: FAIL — `cannot import name 'hydrate_builder_lines_from_draft'`.

- [ ] **Step 3: Implement the helper** (`buyplan_builder.py`):

```python
def hydrate_builder_lines_from_draft(lines: list[dict], db: Session, requisition_id: int) -> None:
    """Overlay an open DRAFT Sales Order's saved offer/sell picks onto builder line dicts (in place).

    Lets a reopened builder restore what autosave persisted. No-op when no open DRAFT exists. Keyed by
    ``requirement_id``; a draft line's ``offer_id``/``unit_sell`` win over the builder defaults.
    """
    draft = find_open_sales_order(db, requisition_id)
    if draft is None or draft.status != BuyPlanStatus.DRAFT.value:
        return
    by_req = {ln.requirement_id: ln for ln in draft.lines}
    for line in lines:
        saved = by_req.get(line.get("requirement_id"))
        if saved is not None:
            if saved.offer_id is not None:
                line["selected_offer_id"] = saved.offer_id
            if saved.unit_sell is not None:
                line["sell_price"] = float(saved.unit_sell)
```

  *(Confirmed against `app/models/buy_plan.py:187-193`: `BuyPlanLine.offer_id`, `unit_sell`, `unit_cost`,
  `requirement_id` are the exact attribute names used above.)*

- [ ] **Step 4: Wire into the GET route.** In `htmx_views.py` `sales_order_new`, after `apply_smart_defaults(lines)`:

```python
        lines = get_builder_data(req.id, db)
        apply_smart_defaults(lines)
        from ..services.buyplan_builder import hydrate_builder_lines_from_draft
        hydrate_builder_lines_from_draft(lines, db, req.id)   # saved picks override defaults
        ctx.update({"selected_req": req, "lines": lines})
```

- [ ] **Step 5: Run the helper tests; verify pass.**
  Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_buyplan_autosave.py -k hydrate -v --override-ini="addopts="`
  Expected: PASS.

- [ ] **Step 6: Commit.**
```bash
git add app/services/buyplan_builder.py app/routers/htmx_views.py tests/test_buyplan_autosave.py
git commit -m "feat(buyplan): hydrate SO builder from saved DRAFT on reopen"
```

---

## Task 4: Autosave route (`POST .../sales-orders/autosave`)

**Files:**
- Modify: `app/routers/htmx_views.py` (new handler beside `sales_order_create`)
- Test: `tests/test_buyplan_autosave.py` (route test via `TestClient`)

**Interfaces:**
- Produces: `POST /v2/partials/approvals/sales-orders/autosave` — parses `requisition_id` + `offer_<rid>`/
  `sell_<rid>` (identical parsing to `sales_order_create`), calls `upsert_draft_sales_order(..., with_ai=False)`,
  returns an inline "Saved" fragment (status 200) targeted at `#so-autosave-status`. A submitted plan
  (`DuplicateSalesOrderError`) returns a benign "already submitted" indicator (200, never 500).

- [ ] **Step 1: Write the failing route test.** (TestClient with auth override — the standard harness.)

```python
def test_autosave_route_persists_draft(db_session, so_fixture):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.dependencies import get_db, require_user

    req, requirement, offer, user = so_fixture
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_user] = lambda: user
    try:
        client = TestClient(app)
        resp = client.post("/v2/partials/approvals/sales-orders/autosave",
                           data={"requisition_id": req.id,
                                 f"offer_{requirement.id}": offer.id,
                                 f"sell_{requirement.id}": "1.25"})
        assert resp.status_code == 200
        assert "Saved" in resp.text
        plan = db_session.query(BuyPlan).filter(BuyPlan.requisition_id == req.id).one()
        assert plan.status == BuyPlanStatus.DRAFT.value
        assert float(plan.lines[0].unit_sell) == 1.25
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(require_user, None)
```

- [ ] **Step 2: Run; verify fail.**
  Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_buyplan_autosave.py::test_autosave_route_persists_draft -v --override-ini="addopts="`
  Expected: FAIL — 404 (route not defined).

- [ ] **Step 3: Implement the handler** (`htmx_views.py`, beside `sales_order_create`). Reuse the existing field
  parser by extracting it; here, inline parsing matching `sales_order_create`:

```python
@router.post("/v2/partials/approvals/sales-orders/autosave", response_class=HTMLResponse)
async def sales_order_autosave(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Debounced autosave of the New-SO builder into the open DRAFT (crash recovery).

    Same field shape as the create route (``requisition_id`` + ``offer_<rid>``/``sell_<rid>``). Persists
    the partial picks via ``upsert_draft_sales_order`` (no AI — fast) and returns a small inline "Saved"
    indicator swapped into ``#so-autosave-status``. Never 500s; a submitted plan yields a benign note.
    """
    from ..services.buyplan_builder import DuplicateSalesOrderError, upsert_draft_sales_order

    form = await request.form()
    raw_req_id = form.get("requisition_id")
    if not raw_req_id:
        return HTMLResponse("")  # nothing to save yet
    try:
        req_id = int(raw_req_id)
    except (TypeError, ValueError):
        return HTMLResponse("")
    require_requisition_access(db, req_id, user)

    selections: dict[int, int] = {}
    sell_prices: dict[int, float] = {}
    for key, value in form.multi_items():
        if key.startswith("offer_"):
            try:
                selections[int(key[len("offer_"):])] = int(value)
            except (TypeError, ValueError):
                continue
        elif key.startswith("sell_") and value not in (None, ""):
            try:
                sell_prices[int(key[len("sell_"):])] = float(value)
            except (TypeError, ValueError):
                continue

    try:
        upsert_draft_sales_order(db, user, req_id, selections, sell_prices, with_ai=False)
    except DuplicateSalesOrderError:
        return HTMLResponse('<span class="text-amber-600">Already submitted — autosave paused</span>')
    return HTMLResponse('<span class="text-emerald-600">Saved ✓</span>')
```

- [ ] **Step 4: Run; verify pass.**
  Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_buyplan_autosave.py::test_autosave_route_persists_draft -v --override-ini="addopts="`
  Expected: PASS.

- [ ] **Step 5: Commit.**
```bash
git add app/routers/htmx_views.py tests/test_buyplan_autosave.py
git commit -m "feat(approvals): SO builder autosave route (debounced DRAFT persist)"
```

---

## Task 5: Wire autosave into the builder template (debounce + indicator + navigate-away guard)

**Files:**
- Modify: `app/templates/htmx/partials/approvals/_sales_order_new.html:73-134`

**Interfaces:**
- Consumes: `POST /v2/partials/approvals/sales-orders/autosave` (Task 4) returning the inline fragment.

- [ ] **Step 1: Wrap the builder form** in an autosave shell. Replace the `<form ...>` open tag and add the
  status target + guard. The wrapper listens for bubbled `input`/`change` events (debounced), `hx-include`s the
  form, and swaps the returned indicator into `#so-autosave-status`. The form's existing submit→`/create` is
  unchanged (finalize). Per CLAUDE.md: htmx `delay:`/`changed` modifiers (no `[filter]`); no literal `"` inside
  double-quoted Alpine attrs (the `beforeunload` body uses no `"`).

```html
  {% set selectable = lines|selectattr('offers')|list %}
  {% if selectable %}
  <div id="so-builder-autosave"
       x-data="{ dirty: false }"
       hx-post="/v2/partials/approvals/sales-orders/autosave"
       hx-trigger="input changed delay:800ms, change changed delay:300ms"
       hx-include="#so-builder-form"
       hx-target="#so-autosave-status"
       hx-swap="innerHTML"
       @input="dirty = true"
       @change="dirty = true"
       @htmx:after-request="dirty = false"
       x-init="window.addEventListener('beforeunload', (e) =&gt; { if (dirty) { e.preventDefault(); e.returnValue = ''; } })">
  <form id="so-builder-form" hx-post="/v2/partials/approvals/sales-orders/create"
        hx-target="#main-content"
        hx-swap="innerHTML">
    <input type="hidden" name="requisition_id" value="{{ selected_req.id }}">
    <!-- existing table of offer_<rid>/sell_<rid> controls unchanged -->
```

- [ ] **Step 2: Add the inline status target** right after the closing `</table>`'s wrapping div and before the
  action buttons (inside the form is fine), then close the autosave wrapper after the form:

```html
    </div>
    <div id="so-autosave-status" class="mt-2 h-4 text-xs text-gray-500" aria-live="polite"></div>
    <div class="mt-4 flex items-center justify-end gap-2">
      <!-- existing Cancel + Create Sales Order buttons unchanged -->
    </div>
  </form>
  </div>{# /#so-builder-autosave #}
  {% else %}
```

- [ ] **Step 3: Verify the rendered builder includes the autosave wrapper.** Add a render test (the builder GET
  route renders the wrapper + status target):

```python
def test_builder_renders_autosave_wrapper(db_session, so_fixture):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.dependencies import get_db, require_user

    req, requirement, offer, user = so_fixture
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_user] = lambda: user
    try:
        client = TestClient(app)
        resp = client.get(f"/v2/partials/approvals/sales-orders/new?requisition_id={req.id}")
        assert resp.status_code == 200
        assert 'id="so-builder-autosave"' in resp.text
        assert 'id="so-autosave-status"' in resp.text
        assert "/v2/partials/approvals/sales-orders/autosave" in resp.text
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(require_user, None)
```

  Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_buyplan_autosave.py::test_builder_renders_autosave_wrapper -v --override-ini="addopts="`
  Expected: PASS.

- [ ] **Step 4: Headless smoke (manual, post-deploy — do NOT block the task on this).** Per
  `feedback_htmx_render_verify`: a `curl` proves the markup is present; real autosave/guard behavior needs a
  headless browser. Note in the PR that the debounce + `beforeunload` were verified headless on staging
  (type → pause → `#so-autosave-status` shows "Saved ✓"; reload → picks restored).

- [ ] **Step 5: Commit.**
```bash
git add app/templates/htmx/partials/approvals/_sales_order_new.html tests/test_buyplan_autosave.py
git commit -m "feat(approvals): debounced autosave + saved-indicator + navigate-away guard on SO builder"
```

---

## Task 6: Finalize via upsert (clean "Create" with no false duplicate toast)

With autosave, a DRAFT usually exists when "Create Sales Order" is clicked. Today `sales_order_create` calls
`create_sales_order_from_offers`, which raises `DuplicateSalesOrderError` on the user's **own** autosaved draft and
shows a misleading "already an open Sales Order" warning. Switch finalize to `upsert_draft_sales_order` (with AI),
so it finalizes the user's draft; the duplicate warning then only fires for a genuinely **submitted** plan.

**Files:**
- Modify: `app/routers/htmx_views.py:11270-11341` (`sales_order_create`)
- Test: `tests/test_buyplan_autosave.py`

- [ ] **Step 1: Write the failing test** — creating after an autosave reuses the same draft, no warning toast:

```python
def test_create_finalizes_autosaved_draft_without_warning(db_session, so_fixture):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.dependencies import get_db, require_user
    from app.services.buyplan_builder import upsert_draft_sales_order

    req, requirement, offer, user = so_fixture
    draft = upsert_draft_sales_order(db_session, user, req.id, {requirement.id: offer.id}, {requirement.id: 1.25})

    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_user] = lambda: user
    try:
        client = TestClient(app)
        resp = client.post("/v2/partials/approvals/sales-orders/create",
                           data={"requisition_id": req.id, f"offer_{requirement.id}": offer.id,
                                 f"sell_{requirement.id}": "1.25"})
        assert resp.status_code == 200
        # finalized the SAME draft — no second plan, no "already an open Sales Order" toast
        assert db_session.query(BuyPlan).filter(BuyPlan.requisition_id == req.id).count() == 1
        assert "already an open Sales Order" not in resp.text
        assert "already an open Sales Order" not in resp.headers.get("HX-Trigger", "")
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(require_user, None)
```

- [ ] **Step 2: Run; verify fail.**
  Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_buyplan_autosave.py::test_create_finalizes_autosaved_draft_without_warning -v --override-ini="addopts="`
  Expected: FAIL — count is 1 but the response carries the duplicate-warning HX-Trigger (today's behavior).

- [ ] **Step 3: Implement.** In `sales_order_create`, replace the `create_sales_order_from_offers` call + its
  `DuplicateSalesOrderError` branch:

```python
    from ..services.buyplan_builder import DuplicateSalesOrderError, upsert_draft_sales_order

    # ... (req_id parse + require_requisition_access + selections/sell_prices parse unchanged) ...

    try:
        plan = upsert_draft_sales_order(db, user, req_id, selections, sell_prices, with_ai=True)
    except DuplicateSalesOrderError as exc:
        # Only a genuinely SUBMITTED (PENDING/ACTIVE) plan reaches here — open it with a warning.
        existing_id = exc.existing_plan_id
        resp = await buy_plan_detail_partial(request, existing_id, user, db)
        resp.headers["HX-Trigger"] = json.dumps(
            {"showToast": {"message": f"This requisition's Sales Order (plan #{existing_id}) is already submitted.",
                           "type": "warning"}}
        )
        resp.headers["HX-Push-Url"] = f"/v2/buy-plans/{existing_id}"
        return resp
    except ValueError:
        raise HTTPException(400, "Could not originate a Sales Order from the selected offers.")

    resp = await buy_plan_detail_partial(request, plan.id, user, db)
    resp.headers["HX-Push-Url"] = f"/v2/buy-plans/{plan.id}"
    return resp
```

- [ ] **Step 4: Run; verify pass** (and the existing `sales_order_create` tests still green):
  Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_buyplan_autosave.py -k "create or autosave" -v --override-ini="addopts="`
  Expected: PASS.

- [ ] **Step 5: Commit.**
```bash
git add app/routers/htmx_views.py tests/test_buyplan_autosave.py
git commit -m "feat(approvals): finalize SO via upsert (no false duplicate warning on own draft)"
```

---

## Task 7: Soft-delete column — migration 165 + model + query exclusion

**Files:**
- Create: `alembic/versions/165_buyplan_soft_delete.py`
- Modify: `app/models/buy_plan.py` (BuyPlan), `app/services/buyplan_builder.py` (`find_open_sales_order`)
- Modify: `MIGRATION_NUMBERS_IN_FLIGHT.txt`
- Test: `tests/test_buyplan_autosave.py`

- [ ] **Step 1: Claim the migration number.** Append to `MIGRATION_NUMBERS_IN_FLIGHT.txt`:
  `165 fix/approvals-rework-p2-autosave  BuyPlan.deleted_at soft-delete (chained after 164)`

- [ ] **Step 2: Write the failing test** (exclusion + column):

```python
def test_soft_deleted_draft_is_not_the_open_so(db_session, so_fixture):
    from app.services.buyplan_builder import find_open_sales_order, upsert_draft_sales_order
    from datetime import datetime, timezone

    req, requirement, offer, user = so_fixture
    draft = upsert_draft_sales_order(db_session, user, req.id, {requirement.id: offer.id}, {})
    assert find_open_sales_order(db_session, req.id) is not None
    draft.deleted_at = datetime.now(timezone.utc)
    db_session.commit()
    assert find_open_sales_order(db_session, req.id) is None   # soft-deleted no longer blocks a new SO
```

- [ ] **Step 3: Run; verify fail.**
  Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_buyplan_autosave.py::test_soft_deleted_draft_is_not_the_open_so -v --override-ini="addopts="`
  Expected: FAIL — `AttributeError: ... has no attribute 'deleted_at'` (column missing).

- [ ] **Step 4: Add the model column.** In `app/models/buy_plan.py` `BuyPlan`, beside the other timestamps:

```python
    deleted_at = Column(UTCDateTime, nullable=True)  # soft-delete: non-null = removed (restorable)
```

- [ ] **Step 5: Create the migration** `alembic/versions/165_buyplan_soft_delete.py`:

```python
"""buyplan soft-delete: add buy_plans_v3.deleted_at.

Revision ID: 165_buyplan_soft_delete
Revises: 164_sp2_qp_sales_rename
Create Date: 2026-06-28 00:00:00.000000

Called by: alembic upgrade head
Depends on: buy_plans_v3 table
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "165_buyplan_soft_delete"
down_revision: Union[str, None] = "164_sp2_qp_sales_rename"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("buy_plans_v3", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.execute("ALTER TABLE IF EXISTS buy_plans_v3 DROP COLUMN IF EXISTS deleted_at")
```

- [ ] **Step 6: Exclude soft-deleted from the open-SO query.** In `find_open_sales_order`
  (`buyplan_builder.py:108-122`), add to the filter:

```python
        .filter(
            BuyPlan.requisition_id == requisition_id,
            BuyPlan.quote_id.is_(None),
            BuyPlan.deleted_at.is_(None),     # soft-deleted drafts don't count as the open SO
            BuyPlan.status.in_([BuyPlanStatus.DRAFT.value, BuyPlanStatus.PENDING.value, BuyPlanStatus.ACTIVE.value]),
        )
```

- [ ] **Step 7: Round-trip the migration on a THROWAWAY PG** (never staging — `feedback_subagent_migration_throwaway_db`):
```bash
# spin a throwaway PG, point DATABASE_URL at it, then:
alembic upgrade head && alembic downgrade -1 && alembic upgrade head
alembic heads   # expect a single head: 165_buyplan_soft_delete
```
  Expected: clean upgrade→downgrade→upgrade; single head.

- [ ] **Step 8: Run the test; verify pass.**
  Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_buyplan_autosave.py::test_soft_deleted_draft_is_not_the_open_so -v --override-ini="addopts="`
  Expected: PASS.

- [ ] **Step 9: Commit.**
```bash
git add app/models/buy_plan.py app/services/buyplan_builder.py alembic/versions/165_buyplan_soft_delete.py MIGRATION_NUMBERS_IN_FLIGHT.txt tests/test_buyplan_autosave.py
git commit -m "feat(buyplan): soft-delete column (migration 165) + open-SO exclusion"
```

---

## Task 8: Soft-delete + restore routes (the Undo affordance)

**Files:**
- Modify: `app/routers/htmx_views.py` (two handlers)
- Test: `tests/test_buyplan_autosave.py`

**Interfaces:**
- Produces: `POST /v2/partials/buy-plans/{plan_id}/soft-delete` (DRAFT only → set `deleted_at`, return a toast with
  an Undo that calls restore) and `POST /v2/partials/buy-plans/{plan_id}/restore` (clear `deleted_at`).

- [ ] **Step 1: Write the failing tests.**

```python
def test_soft_delete_then_restore(db_session, so_fixture):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.dependencies import get_db, require_user
    from app.services.buyplan_builder import upsert_draft_sales_order

    req, requirement, offer, user = so_fixture
    draft = upsert_draft_sales_order(db_session, user, req.id, {requirement.id: offer.id}, {})
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_user] = lambda: user
    try:
        client = TestClient(app)
        d = client.post(f"/v2/partials/buy-plans/{draft.id}/soft-delete")
        assert d.status_code == 200
        db_session.refresh(draft)
        assert draft.deleted_at is not None
        assert "Undo" in d.text or "Undo" in d.headers.get("HX-Trigger", "")

        r = client.post(f"/v2/partials/buy-plans/{draft.id}/restore")
        assert r.status_code == 200
        db_session.refresh(draft)
        assert draft.deleted_at is None
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(require_user, None)


def test_soft_delete_refused_on_submitted_plan(db_session, so_fixture):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.dependencies import get_db, require_user
    from app.services.buyplan_builder import upsert_draft_sales_order

    req, requirement, offer, user = so_fixture
    plan = upsert_draft_sales_order(db_session, user, req.id, {requirement.id: offer.id}, {})
    plan.status = BuyPlanStatus.PENDING.value
    db_session.commit()
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_user] = lambda: user
    try:
        client = TestClient(app)
        resp = client.post(f"/v2/partials/buy-plans/{plan.id}/soft-delete")
        assert resp.status_code == 400         # only DRAFTs are soft-deletable
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(require_user, None)
```

- [ ] **Step 2: Run; verify fail.**
  Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_buyplan_autosave.py -k "soft_delete or restore" -v --override-ini="addopts="`
  Expected: FAIL — 404 (routes not defined).

- [ ] **Step 3: Implement** (`htmx_views.py`):

```python
@router.post("/v2/partials/buy-plans/{plan_id}/soft-delete", response_class=HTMLResponse)
async def buy_plan_soft_delete(
    plan_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Soft-delete a DRAFT buy plan (restorable). Refuses anything past DRAFT (submitted work is not
    silently removed). Returns an empty body + a toast carrying an Undo that restores it."""
    from datetime import datetime, timezone

    plan = db.get(BuyPlan, plan_id)
    if plan is None:
        raise HTTPException(404, "Buy plan not found")
    if plan.status != BuyPlanStatus.DRAFT.value:
        raise HTTPException(400, "Only draft Sales Orders can be deleted")
    plan.deleted_at = datetime.now(timezone.utc)
    db.commit()
    resp = HTMLResponse("")
    resp.headers["HX-Trigger"] = json.dumps(
        {"showToast": {"message": "Draft deleted.", "type": "info",
                       "undoUrl": f"/v2/partials/buy-plans/{plan_id}/restore", "undoLabel": "Undo"}}
    )
    return resp


@router.post("/v2/partials/buy-plans/{plan_id}/restore", response_class=HTMLResponse)
async def buy_plan_restore(
    plan_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Restore a soft-deleted draft (clears ``deleted_at``)."""
    plan = db.get(BuyPlan, plan_id)
    if plan is None:
        raise HTTPException(404, "Buy plan not found")
    plan.deleted_at = None
    db.commit()
    resp = HTMLResponse("")
    resp.headers["HX-Trigger"] = json.dumps({"showToast": {"message": "Draft restored.", "type": "success"}})
    return resp
```

  *(The base toast component renders `message`/`type`; the `undoUrl`/`undoLabel` keys are passed through for the
  toast's Undo button. If the current toast component ignores extra keys, a tiny enhancement to the toast handler
  in `htmx_app.js` to render an Undo link when `undoUrl` is present is in scope here — match the existing
  `showToast` shape; this is the one place the "undo" affordance is realized.)*

- [ ] **Step 4: Run; verify pass.**
  Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_buyplan_autosave.py -k "soft_delete or restore" -v --override-ini="addopts="`
  Expected: PASS.

- [ ] **Step 5: Run the whole new test file + the builder suite.**
  Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_buyplan_autosave.py tests/test_buyplan_builder_so_origin.py -v --override-ini="addopts="`
  Expected: ALL PASS.

- [ ] **Step 6: Commit.**
```bash
git add app/routers/htmx_views.py app/static/htmx_app.js tests/test_buyplan_autosave.py
git commit -m "feat(buyplan): soft-delete + restore routes with Undo toast"
```

---

## Wrap-up (after Task 8)

- [ ] `pre-commit run --files <all changed files>` clean (ruff/format/docformatter/mypy); run twice if docformatter mutates.
- [ ] Update `docs/APP_MAP_DATABASE.md` (new `buy_plans_v3.deleted_at`) and `docs/APP_MAP_INTERACTIONS.md`
      (autosave seam: builder → `upsert_draft_sales_order`) in this PR.
- [ ] Open the PR; verify CI **rollup** (`test` + `security`) = SUCCESS (not `--watch`).
- [ ] Deploy via `deploy.sh`; **live-verify on real PG**: open the SO builder, change a pick, see "Saved ✓";
      reload → picks restored; delete a draft → Undo restores it. Confirm `alembic current` = `165_buyplan_soft_delete`.
- [ ] **SAVE**: update memory `project_approvals_rework_2026_06_28` (Phase 2 shipped).

---

## Self-Review (plan vs. spec §9)

- **Spec coverage:** server-side draft persistence (T2/T4) ✅; mid-entry reload loses nothing (T3 hydration) ✅;
  debounced/consolidated, not per-keystroke (T5 — inline indicator, calmer than a toast, stated adaptation) ✅;
  navigate-away guard (T5) ✅; soft-delete + restore (T7/T8) ✅. **Per-change undo** explicitly deferred → restore
  is the realized undo (stated in Scope). **Modal-field autosave** explicitly deferred to P3/P4/P5 (stated).
- **Type consistency:** `_populate_plan_lines`, `upsert_draft_sales_order`, `hydrate_builder_lines_from_draft`,
  `find_open_sales_order`, `BuyPlan.deleted_at`, `BuyPlanStatus.DRAFT` used identically across tasks. Migration id
  `165_buyplan_soft_delete` (24 chars ≤ 32). Routes `/sales-orders/autosave`, `/buy-plans/{id}/soft-delete`,
  `/buy-plans/{id}/restore` consistent between handler + tests.
- **Placeholder scan:** every code step shows complete code; two "verify at edit time" notes (the `BuyPlanLine`
  offer-attribute name; whether the toast handler needs the Undo enhancement) are **verification instructions**,
  not deferred logic.
