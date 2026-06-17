# Proactive auto-feed: CPH from buy-plan completion — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Auto-populate `customer_part_history` (CPH) when a buy plan reaches COMPLETED, so the Proactive tab fills itself from work reps already do.

**Architecture:** Hook a best-effort recorder into `check_completion`; it upserts one CPH row per VERIFIED line (`source="buy_plan"`), guarded by a new idempotency column, then immediately re-matches live offers for the purchased parts. Retire the premature offer/quote-won CPH hooks (buy_plan is now the single source of truth) and make the matcher aggregate CPH per (company, card). A one-time backfill records existing COMPLETED plans.

**Tech Stack:** FastAPI, SQLAlchemy 2.0, PostgreSQL 16, Alembic, pytest (SQLite in tests), Loguru.

**Spec:** `docs/superpowers/specs/2026-06-17-proactive-buyplan-cph-feed-design.md`

## Global Constraints

- **Execute in an isolated git worktree** (superpowers:using-git-worktrees) — multiple Claude sessions share `/root/availai`; editing on `main` directly caused a collision this session.
- DB rule: schema changes via Alembic only; `alembic revision` ids **≤ 32 chars** (PG `version_num` is VARCHAR(32)); after creating a migration run `alembic heads` and ensure a single head.
- Status values come from `app/constants.py` StrEnums — `BuyPlanStatus`, `BuyPlanLineStatus` — never raw strings.
- `db.get(Model, id)`, not `db.query().get()`. Loguru, never `print()`. Ruff/mypy clean (pre-commit).
- Tests: `TESTING=1 PYTHONPATH=/root/availai python3 -m pytest <path> -v --override-ini="addopts="` for single files; full suite before the final commit.
- After code changes, update `docs/APP_MAP_INTERACTIONS.md` + `docs/APP_MAP_DATABASE.md`.
- Every new file gets a header comment (what it does / what calls it / what it depends on).
- CPH side effects must NEVER raise out of completion — wrap in try/except, log, continue.

---

### Task 1: Idempotency column `buy_plans_v3.purchase_history_recorded_at`

**Files:**
- Modify: `app/models/buy_plan.py` (BuyPlan class, near the other `UTCDateTime` columns ~line 115)
- Create: `alembic/versions/<hash>_buyplan_cph_recorded_at.py`
- Test: `tests/test_buyplan_cph_feed.py` (new)

**Interfaces:**
- Produces: `BuyPlan.purchase_history_recorded_at: datetime | None` (nullable).

- [ ] **Step 1: Add the column to the model**

In `app/models/buy_plan.py`, inside `class BuyPlan`, after `completed_at = Column(UTCDateTime)`:

```python
    # Set once CPH has been recorded from this plan's lines (idempotency guard for
    # the buy-plan→customer_part_history feed and its backfill).
    purchase_history_recorded_at = Column(UTCDateTime)
```

- [ ] **Step 2: Generate the migration**

Run: `alembic revision --autogenerate -m "buyplan purchase_history_recorded_at"`
Then open the generated file and confirm it contains exactly an `add_column`/`drop_column` for `buy_plans_v3.purchase_history_recorded_at` (remove any unrelated autogen drift). Ensure `revision` string ≤ 32 chars.

Expected `upgrade()` / `downgrade()` bodies:

```python
def upgrade() -> None:
    op.add_column("buy_plans_v3", sa.Column("purchase_history_recorded_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("buy_plans_v3", "purchase_history_recorded_at")
```

- [ ] **Step 3: Verify single head + round-trip**

Run: `alembic heads` (expect ONE head). Then `alembic upgrade head && alembic downgrade -1 && alembic upgrade head`.
Expected: all succeed, no error.

- [ ] **Step 4: Write a model smoke test**

In `tests/test_buyplan_cph_feed.py`:

```python
"""Tests for the buy-plan → customer_part_history auto-feed.

Called by: pytest. Depends on: buyplan_workflow, purchase_history_service.
"""
from datetime import datetime, timezone
from decimal import Decimal

from app.constants import BuyPlanLineStatus, BuyPlanStatus, SOVerificationStatus
from app.models import Company, CustomerSite, MaterialCard, Offer, Requirement, Requisition, User
from app.models.buy_plan import BuyPlan, BuyPlanLine
from app.models.purchase_history import CustomerPartHistory


def test_buyplan_has_recorded_at_column():
    bp = BuyPlan(quote_id=1, requisition_id=1)
    assert bp.purchase_history_recorded_at is None
```

- [ ] **Step 5: Run + commit**

Run: `TESTING=1 PYTHONPATH=/root/availai python3 -m pytest tests/test_buyplan_cph_feed.py -v --override-ini="addopts="`
Expected: PASS.
```bash
git add app/models/buy_plan.py alembic/versions/*buyplan_cph_recorded_at.py tests/test_buyplan_cph_feed.py
git commit -m "feat(buyplan): add purchase_history_recorded_at idempotency column"
```

---

### Task 2: `record_buyplan_purchase_history()` — record CPH from a completed plan

**Files:**
- Modify: `app/services/purchase_history_service.py` (add function; reuses existing `upsert_purchase`)
- Test: `tests/test_buyplan_cph_feed.py`

**Interfaces:**
- Consumes: `upsert_purchase(db, *, company_id, material_card_id, source, unit_price, quantity, purchased_at, source_ref)`.
- Produces: `record_buyplan_purchase_history(plan: BuyPlan, db: Session) -> list[int]` — returns the list of affected `material_card_id`s; sets `plan.purchase_history_recorded_at`; idempotent.

- [ ] **Step 1: Write failing tests**

Add to `tests/test_buyplan_cph_feed.py` a helper that builds a completed plan, plus tests. (Use the existing conftest `db_session` fixture.)

```python
def _completed_plan(db, *, line_specs, so="SO-1"):
    """line_specs: list of (status, unit_sell, qty, with_card). Returns (plan, company, cards)."""
    owner = User(email="rep@trioscs.com", name="Rep", role="sales", azure_id="rep-cph")
    db.add(owner); db.flush()
    company = Company(name="CPH Buyer", is_active=True, account_owner_id=owner.id)
    db.add(company); db.flush()
    site = CustomerSite(company_id=company.id, site_name="HQ", is_active=True)
    db.add(site); db.flush()
    req = Requisition(name="R", customer_site_id=site.id, status="archived", created_by=owner.id)
    db.add(req); db.flush()
    plan = BuyPlan(quote_id=1, requisition_id=req.id, status=BuyPlanStatus.COMPLETED.value,
                   so_status=SOVerificationStatus.APPROVED.value, sales_order_number=so,
                   completed_at=datetime.now(timezone.utc))
    db.add(plan); db.flush()
    cards = []
    for status, unit_sell, qty, with_card in line_specs:
        card = MaterialCard(normalized_mpn=f"CPHCARD{len(cards)}", display_mpn=f"CPH-{len(cards)}")
        db.add(card); db.flush()
        cards.append(card)
        requirement = Requirement(requisition_id=req.id, primary_mpn=card.display_mpn,
                                  normalized_mpn=card.normalized_mpn,
                                  material_card_id=(card.id if with_card else None))
        db.add(requirement); db.flush()
        line = BuyPlanLine(buy_plan_id=plan.id, requirement_id=requirement.id, quantity=qty,
                           unit_sell=Decimal(str(unit_sell)), status=status)
        db.add(line)
    db.commit()
    return plan, company, cards


def test_records_cph_for_verified_lines(db_session):
    from app.services.purchase_history_service import record_buyplan_purchase_history
    plan, company, cards = _completed_plan(
        db_session,
        line_specs=[(BuyPlanLineStatus.VERIFIED.value, 12.50, 100, True)],
        so="SO-XYZ",
    )
    affected = record_buyplan_purchase_history(plan, db_session)
    db_session.commit()
    assert cards[0].id in affected
    row = db_session.query(CustomerPartHistory).filter_by(
        company_id=company.id, material_card_id=cards[0].id, source="buy_plan").one()
    assert float(row.avg_unit_price) == 12.50
    assert row.last_quantity == 100
    assert row.source_ref == "SO-XYZ"
    assert plan.purchase_history_recorded_at is not None


def test_skips_cancelled_lines(db_session):
    from app.services.purchase_history_service import record_buyplan_purchase_history
    plan, company, cards = _completed_plan(
        db_session,
        line_specs=[(BuyPlanLineStatus.CANCELLED.value, 9.0, 10, True)],
    )
    record_buyplan_purchase_history(plan, db_session); db_session.commit()
    assert db_session.query(CustomerPartHistory).filter_by(source="buy_plan").count() == 0


def test_idempotent(db_session):
    from app.services.purchase_history_service import record_buyplan_purchase_history
    plan, company, cards = _completed_plan(
        db_session, line_specs=[(BuyPlanLineStatus.VERIFIED.value, 10.0, 5, True)])
    record_buyplan_purchase_history(plan, db_session); db_session.commit()
    record_buyplan_purchase_history(plan, db_session); db_session.commit()
    row = db_session.query(CustomerPartHistory).filter_by(source="buy_plan").one()
    assert row.purchase_count == 1  # not double-counted


def test_unresolvable_line_skipped_others_recorded(db_session):
    from app.services.purchase_history_service import record_buyplan_purchase_history
    plan, company, cards = _completed_plan(
        db_session,
        line_specs=[(BuyPlanLineStatus.VERIFIED.value, 10.0, 5, False),
                    (BuyPlanLineStatus.VERIFIED.value, 20.0, 5, True)])
    record_buyplan_purchase_history(plan, db_session); db_session.commit()
    rows = db_session.query(CustomerPartHistory).filter_by(source="buy_plan").all()
    assert len(rows) == 1 and rows[0].material_card_id == cards[1].id
```

- [ ] **Step 2: Run to verify they fail**

Run: `TESTING=1 PYTHONPATH=/root/availai python3 -m pytest tests/test_buyplan_cph_feed.py -v --override-ini="addopts="`
Expected: FAIL with `ImportError: cannot import name 'record_buyplan_purchase_history'`.

- [ ] **Step 3: Implement the recorder**

In `app/services/purchase_history_service.py`, add imports at top and the function (call `refresh_matches_for_cards` is added in Task 4 — for now do NOT call it; Task 4 wires it):

```python
from datetime import datetime, timezone  # (extend existing import)
from loguru import logger  # (already imported)


def record_buyplan_purchase_history(db, plan, *, refresh: bool = True) -> list[int]:
    """Record customer_part_history from a COMPLETED buy plan's verified lines.

    Idempotent via plan.purchase_history_recorded_at. Returns affected material_card_ids.
    Best-effort: callers must not let CPH errors break buy-plan completion.
    """
    from app.constants import BuyPlanLineStatus  # local import avoids cycles

    if plan.purchase_history_recorded_at is not None:
        return []

    req = plan.requisition
    site = req.customer_site if req else None
    company_id = site.company_id if site else None
    if not company_id:
        logger.warning("BUYPLAN_CPH: plan {} has no customer company — skipping", plan.id)
        plan.purchase_history_recorded_at = datetime.now(timezone.utc)
        db.flush()
        return []

    affected: list[int] = []
    for line in plan.lines:
        if line.status != BuyPlanLineStatus.VERIFIED.value:
            continue
        card_id = None
        if line.requirement_id and line.requirement:
            card_id = line.requirement.material_card_id
        if not card_id and line.offer_id and line.offer:
            card_id = line.offer.material_card_id
        if not card_id:
            logger.info("BUYPLAN_CPH: plan {} line {} has no material_card — skipping", plan.id, line.id)
            continue
        upsert_purchase(
            db,
            company_id=company_id,
            material_card_id=card_id,
            source="buy_plan",
            unit_price=line.unit_sell,
            quantity=line.quantity,
            purchased_at=plan.completed_at,
            source_ref=plan.sales_order_number,
        )
        affected.append(card_id)

    plan.purchase_history_recorded_at = datetime.now(timezone.utc)
    db.flush()
    logger.info("BUYPLAN_CPH: plan {} recorded {} parts for company {}", plan.id, len(affected), company_id)
    return affected
```

Note: the `refresh` param is unused until Task 4 — keep the signature now so the hook (Task 3) is stable.

- [ ] **Step 4: Run to verify pass**

Run: `TESTING=1 PYTHONPATH=/root/availai python3 -m pytest tests/test_buyplan_cph_feed.py -v --override-ini="addopts="`
Expected: 4 new tests PASS (the column test still passes).

- [ ] **Step 5: Commit**

```bash
git add app/services/purchase_history_service.py tests/test_buyplan_cph_feed.py
git commit -m "feat(buyplan): record_buyplan_purchase_history() upserts CPH from completed plans"
```

---

### Task 3: Hook the recorder into `check_completion`

**Files:**
- Modify: `app/services/buyplan_workflow.py` (`check_completion`, ~line 332)
- Test: `tests/test_buyplan_cph_feed.py`

**Interfaces:**
- Consumes: `record_buyplan_purchase_history(db, plan)`.

- [ ] **Step 1: Write the failing test**

```python
def test_check_completion_records_cph(db_session):
    from app.services.buyplan_workflow import check_completion
    plan, company, cards = _completed_plan(
        db_session, line_specs=[(BuyPlanLineStatus.VERIFIED.value, 15.0, 50, True)])
    # reset to the pre-completion state check_completion expects
    plan.status = BuyPlanStatus.ACTIVE.value
    plan.completed_at = None
    plan.purchase_history_recorded_at = None
    db_session.commit()
    check_completion(plan.id, db_session)
    db_session.commit()
    assert db_session.get(type(plan), plan.id).status == BuyPlanStatus.COMPLETED.value
    assert db_session.query(CustomerPartHistory).filter_by(
        company_id=company.id, source="buy_plan").count() == 1
```

- [ ] **Step 2: Run to verify it fails**

Run: `TESTING=1 PYTHONPATH=/root/availai python3 -m pytest tests/test_buyplan_cph_feed.py::test_check_completion_records_cph -v --override-ini="addopts="`
Expected: FAIL (CPH count == 0).

- [ ] **Step 3: Add the hook**

In `app/services/buyplan_workflow.py`, inside `check_completion`, the block that sets COMPLETED (after `plan.case_report = generate_case_report(plan, db)` and `db.flush()`):

```python
    if all_terminal and plan.so_status == SOVerificationStatus.APPROVED.value:
        plan.status = BuyPlanStatus.COMPLETED.value
        plan.completed_at = datetime.now(timezone.utc)
        plan.case_report = generate_case_report(plan, db)
        logger.info("Buy plan {} auto-completed (all lines terminal)", plan_id)
        db.flush()
        # Feed the proactive backbone from this confirmed customer purchase (best-effort).
        try:
            from app.services.purchase_history_service import record_buyplan_purchase_history

            record_buyplan_purchase_history(db, plan)
        except Exception:  # noqa: BLE001 — CPH must never break completion
            logger.exception("BUYPLAN_CPH: failed to record purchase history for plan {}", plan_id)
        db.flush()
```

- [ ] **Step 4: Run to verify pass**

Run: `TESTING=1 PYTHONPATH=/root/availai python3 -m pytest tests/test_buyplan_cph_feed.py -v --override-ini="addopts="`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/buyplan_workflow.py tests/test_buyplan_cph_feed.py
git commit -m "feat(buyplan): record CPH on completion (check_completion hook)"
```

---

### Task 4: Immediate re-match — `refresh_matches_for_cards()`

**Files:**
- Modify: `app/services/purchase_history_service.py` (add helper; call it from `record_buyplan_purchase_history` when `refresh=True`)
- Test: `tests/test_buyplan_cph_feed.py`

**Interfaces:**
- Consumes: `find_matches_for_offer(offer_id, db)` from `app.services.proactive_matching`.
- Produces: `refresh_matches_for_cards(db, card_ids: list[int]) -> int` — number of matches created.

- [ ] **Step 1: Write the failing test**

```python
def test_refresh_creates_match_when_live_offer_exists(db_session):
    from app.services.purchase_history_service import record_buyplan_purchase_history
    from app.models.intelligence import ProactiveMatch
    plan, company, cards = _completed_plan(
        db_session, line_specs=[(BuyPlanLineStatus.VERIFIED.value, 12.50, 100, True)])
    # live vendor stock for the purchased part, below the customer's historical price
    off = Offer(requisition_id=plan.requisition_id, material_card_id=cards[0].id,
                vendor_name="Avnet", mpn=cards[0].display_mpn, qty_available=500,
                unit_price=Decimal("8.00"), status="active")
    db_session.add(off); db_session.commit()
    record_buyplan_purchase_history(db_session, plan)  # refresh=True by default
    db_session.commit()
    assert db_session.query(ProactiveMatch).filter_by(
        company_id=company.id, material_card_id=cards[0].id).count() == 1
```

- [ ] **Step 2: Run to verify it fails**

Run: `TESTING=1 PYTHONPATH=/root/availai python3 -m pytest tests/test_buyplan_cph_feed.py::test_refresh_creates_match_when_live_offer_exists -v --override-ini="addopts="`
Expected: FAIL (match count == 0).

- [ ] **Step 3: Implement + wire**

In `app/services/purchase_history_service.py` add:

```python
def refresh_matches_for_cards(db, card_ids: list[int], *, per_card_limit: int = 5) -> int:
    """Re-run proactive matching for live offers of the given cards (immediate surfacing).

    Best-effort. Bounded to the newest `per_card_limit` offers per card so completion
    stays cheap. Engine dedup prevents duplicate matches.
    """
    from app.models import Offer
    from app.services.proactive_matching import find_matches_for_offer

    created = 0
    for card_id in set(card_ids):
        offers = (
            db.query(Offer.id)
            .filter(Offer.material_card_id == card_id, Offer.is_stale.isnot(True))
            .order_by(Offer.created_at.desc())
            .limit(per_card_limit)
            .all()
        )
        for (offer_id,) in offers:
            try:
                created += len(find_matches_for_offer(offer_id, db))
            except Exception:  # noqa: BLE001
                logger.exception("BUYPLAN_CPH: match refresh failed for offer {}", offer_id)
    return created
```

Then in `record_buyplan_purchase_history`, before `return affected`, add:

```python
    if refresh and affected:
        refresh_matches_for_cards(db, affected)
    return affected
```

- [ ] **Step 4: Run to verify pass**

Run: `TESTING=1 PYTHONPATH=/root/availai python3 -m pytest tests/test_buyplan_cph_feed.py -v --override-ini="addopts="`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/purchase_history_service.py tests/test_buyplan_cph_feed.py
git commit -m "feat(buyplan): immediate proactive re-match on completion"
```

---

### Task 5: Retire the premature offer-won / quote-won CPH hooks

**Files:**
- Modify: `app/routers/crm/offers.py` (remove `_record_offer_won_history` + its call site)
- Modify: `app/routers/crm/quotes.py` (remove `_record_quote_won_history` + its call site)
- Test: `tests/` — update existing tests that assert those hooks write CPH

**Interfaces:** none produced.

- [ ] **Step 1: Find call sites + tests**

Run: `grep -rn "_record_offer_won_history\|_record_quote_won_history\|avail_offer\|avail_quote_won" app/ tests/`
Note every hit; you will remove the two functions, their calls, and update/remove tests that asserted CPH was written on offer/quote won.

- [ ] **Step 2: Write/adjust the regression test**

In `tests/test_buyplan_cph_feed.py`:

```python
def test_offer_won_does_not_write_cph(db_session, monkeypatch):
    """Legacy avail_offer CPH hook is retired — only buy_plan feeds CPH now."""
    import app.routers.crm.offers as offers_mod
    assert not hasattr(offers_mod, "_record_offer_won_history")
```

(Adjust to whichever assertion fits once the function is removed; the point is the symbol is gone.)

- [ ] **Step 3: Remove the hooks**

In `app/routers/crm/offers.py`: delete the `_record_offer_won_history` function and the line that calls it (in the offer-won handler). In `app/routers/crm/quotes.py`: delete `_record_quote_won_history` and its call site. Remove now-unused imports of `upsert_purchase`. Leave all other won-handling logic intact.

- [ ] **Step 4: Update legacy tests**

For each test found in Step 1 that asserted CPH was written on offer/quote won, remove that assertion (or the test if it solely tested the hook). Run the affected test files.

Run: `TESTING=1 PYTHONPATH=/root/availai python3 -m pytest tests/test_buyplan_cph_feed.py tests/test_purchase_history*.py -v --override-ini="addopts="` plus any files touched.
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/routers/crm/offers.py app/routers/crm/quotes.py tests/
git commit -m "refactor(crm): retire offer/quote-won CPH hooks — buy_plan is the source of truth"
```

---

### Task 6: Aggregate CPH per (company, card) in the matcher

**Files:**
- Modify: `app/services/proactive_matching.py` (`_find_matches`, ~lines 162-289)
- Test: `tests/test_proactive_matching.py`

**Interfaces:**
- Consumes: existing `compute_match_score(last_purchased_at, purchase_count, avg_price, our_cost)`.

- [ ] **Step 1: Write the failing test**

In `tests/test_proactive_matching.py` (reuse its `_setup_scenario`/`_make_offer` helpers):

```python
def test_aggregates_cph_across_sources(db_session):
    from app.services.proactive_matching import find_matches_for_offer
    from app.models.purchase_history import CustomerPartHistory
    from datetime import datetime, timedelta, timezone
    from decimal import Decimal
    data = _setup_scenario(db_session)  # creates one CPH row (source="avail_offer")
    # add a second, newer buy_plan row for the same company+card
    db_session.add(CustomerPartHistory(
        company_id=data["company"].id, material_card_id=data["card"].id,
        mpn="STM32F407", source="buy_plan", purchase_count=2,
        last_purchased_at=datetime.now(timezone.utc) - timedelta(days=5),
        avg_unit_price=Decimal("20.00"), last_unit_price=Decimal("20.00"), total_quantity=40))
    db_session.commit()
    offer = _make_offer(db_session, data, unit_price=Decimal("8.00"))
    matches = find_matches_for_offer(offer.id, db_session)
    assert len(matches) == 1  # one match per (company, card), not two
    m = matches[0]
    assert m.customer_purchase_count == 3 + 2  # summed across sources
```

- [ ] **Step 2: Run to verify it fails**

Run: `TESTING=1 PYTHONPATH=/root/availai python3 -m pytest tests/test_proactive_matching.py::test_aggregates_cph_across_sources -v --override-ini="addopts="`
Expected: FAIL (count is from one arbitrary row, not summed).

- [ ] **Step 3: Add an aggregation helper + use it**

In `app/services/proactive_matching.py`, add near the top-level helpers:

```python
def _aggregate_cph_by_company(cph_rows: list) -> dict[int, dict]:
    """Collapse multiple CPH rows per company (different sources) into one record:
    summed purchase_count, max last_purchased_at, count-weighted avg price, and the
    most-recent row's last_unit_price/mpn."""
    by_company: dict[int, dict] = {}
    for cph in cph_rows:
        agg = by_company.get(cph.company_id)
        cnt = cph.purchase_count or 0
        if agg is None:
            by_company[cph.company_id] = {
                "count": cnt,
                "last_purchased_at": cph.last_purchased_at,
                "avg_price_num": (float(cph.avg_unit_price) * cnt) if cph.avg_unit_price else 0.0,
                "avg_price_den": cnt if cph.avg_unit_price else 0,
                "last_unit_price": float(cph.last_unit_price) if cph.last_unit_price else None,
                "_newest": cph.last_purchased_at,
                "mpn": cph.mpn,
            }
            continue
        agg["count"] += cnt
        if cph.avg_unit_price:
            agg["avg_price_num"] += float(cph.avg_unit_price) * cnt
            agg["avg_price_den"] += cnt
        newest = agg["_newest"]
        if cph.last_purchased_at and (newest is None or cph.last_purchased_at > newest):
            agg["_newest"] = cph.last_purchased_at
            agg["last_purchased_at"] = cph.last_purchased_at
            if cph.last_unit_price:
                agg["last_unit_price"] = float(cph.last_unit_price)
    for agg in by_company.values():
        agg["avg_price"] = (agg["avg_price_num"] / agg["avg_price_den"]) if agg["avg_price_den"] else None
    return by_company
```

Then in `_find_matches`, replace the `for cph in cph_rows:` loop body's per-row reads with the aggregate. Concretely: after building `cph_rows`, add `agg_by_company = _aggregate_cph_by_company(cph_rows)` and change the loop to iterate `for company_id, agg in agg_by_company.items():`, using:
- `company = companies.get(company_id)` (guard account_owner_id),
- `site = sites.get(company_id)` (guard),
- the dno/throttle/existing-match guards keyed on `company_id`/`site.id`,
- `score, margin_pct = compute_match_score(agg["last_purchased_at"], agg["count"], agg["avg_price"], our_cost)`,
- `ProactiveMatch(..., company_id=company_id, mpn=mpn_upper, customer_purchase_count=agg["count"], customer_last_price=agg["last_unit_price"], customer_last_purchased_at=agg["last_purchased_at"], ...)`,
- the ActivityLog + `_update_last_activity` calls keyed on `company_id`.

Keep `existing_match_company_ids` dedup (add `company_id` after creating each match). Preserve all other behavior (min-margin filter, requisition history lookup).

- [ ] **Step 4: Run to verify pass (and no regressions)**

Run: `TESTING=1 PYTHONPATH=/root/availai python3 -m pytest tests/test_proactive_matching.py -v --override-ini="addopts="`
Expected: the new test PASSES and all existing matching tests still PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/proactive_matching.py tests/test_proactive_matching.py
git commit -m "feat(proactive): aggregate CPH per (company, card) across sources"
```

---

### Task 7: Backfill command for existing COMPLETED plans

**Files:**
- Create: `app/management/backfill_buyplan_cph.py`
- Test: `tests/test_buyplan_cph_feed.py`

**Interfaces:**
- Consumes: `record_buyplan_purchase_history(db, plan, refresh=False)`.
- Produces: `python -m app.management.backfill_buyplan_cph`.

- [ ] **Step 1: Write the failing test**

```python
def test_backfill_records_completed_plans_idempotently(db_session):
    from app.management.backfill_buyplan_cph import backfill
    plan, company, cards = _completed_plan(
        db_session, line_specs=[(BuyPlanLineStatus.VERIFIED.value, 11.0, 7, True)])
    plan.purchase_history_recorded_at = None
    db_session.commit()
    n1 = backfill(db_session)
    n2 = backfill(db_session)  # idempotent
    assert n1 == 1 and n2 == 0
    assert db_session.query(CustomerPartHistory).filter_by(source="buy_plan").count() == 1
```

- [ ] **Step 2: Run to verify it fails**

Run: `TESTING=1 PYTHONPATH=/root/availai python3 -m pytest tests/test_buyplan_cph_feed.py::test_backfill_records_completed_plans_idempotently -v --override-ini="addopts="`
Expected: FAIL (ImportError).

- [ ] **Step 3: Implement the command**

```python
"""Backfill customer_part_history from already-COMPLETED buy plans.

Called by: ops, once after deploy — `docker compose exec app python -m app.management.backfill_buyplan_cph`.
Depends on: purchase_history_service.record_buyplan_purchase_history. Idempotent via
BuyPlan.purchase_history_recorded_at.
"""
from loguru import logger
from sqlalchemy.orm import Session

from app.constants import BuyPlanStatus
from app.database import SessionLocal
from app.models.buy_plan import BuyPlan
from app.services.purchase_history_service import record_buyplan_purchase_history


def backfill(db: Session) -> int:
    plans = (
        db.query(BuyPlan)
        .filter(BuyPlan.status == BuyPlanStatus.COMPLETED.value,
                BuyPlan.purchase_history_recorded_at.is_(None))
        .all()
    )
    done = 0
    for plan in plans:
        record_buyplan_purchase_history(db, plan, refresh=False)
        db.commit()
        done += 1
    logger.info("BUYPLAN_CPH backfill: recorded {} completed plans", done)
    return done


if __name__ == "__main__":
    db = SessionLocal()
    try:
        backfill(db)
    finally:
        db.close()
```

- [ ] **Step 4: Run to verify pass**

Run: `TESTING=1 PYTHONPATH=/root/availai python3 -m pytest tests/test_buyplan_cph_feed.py -v --override-ini="addopts="`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add app/management/backfill_buyplan_cph.py tests/test_buyplan_cph_feed.py
git commit -m "feat(buyplan): idempotent CPH backfill command for completed plans"
```

---

### Task 8: Docs + full suite + deploy/backfill notes

**Files:**
- Modify: `docs/APP_MAP_INTERACTIONS.md`, `docs/APP_MAP_DATABASE.md`

- [ ] **Step 1: Update APP_MAP docs**

In `docs/APP_MAP_INTERACTIONS.md`: note CPH is now fed by buy-plan completion (`check_completion` → `record_buyplan_purchase_history`) with immediate re-match, and the offer/quote-won hooks were retired. In `docs/APP_MAP_DATABASE.md`: add the `buy_plans_v3.purchase_history_recorded_at` column and the `customer_part_history.source = "buy_plan"` value.

- [ ] **Step 2: Run the full suite**

Run: `TESTING=1 PYTHONPATH=/root/availai python3 -m pytest tests/ -q`
Expected: green (same baseline as main; no new failures).

- [ ] **Step 3: pre-commit**

Run: `pre-commit run --all-files`
Expected: pass (ruff/format/mypy/docformatter).

- [ ] **Step 4: Commit + PR**

```bash
git add docs/APP_MAP_INTERACTIONS.md docs/APP_MAP_DATABASE.md
git commit -m "docs(proactive): APP_MAP — CPH fed by buy-plan completion"
```
Open a PR. **Post-deploy, run once:** `docker compose exec app python -m app.management.backfill_buyplan_cph`, then verify CPH rows + the Proactive tab reflect real completed-deal history.

---

## Self-Review

- **Spec coverage:** §4.1 recorder → Task 2; §4.2 hook → Task 3; §4.3 migration/column → Task 1; §4.4 immediate refresh → Task 4; §4.5 retire hooks → Task 5; §4.6 aggregation → Task 6; §4.7 backfill → Task 7; §6 error handling → best-effort wrappers in Tasks 2–4; §7 tests → each task is TDD; §8 rollout/docs → Task 8. All covered.
- **Placeholder scan:** none — every code/test step has concrete content; the one removal task (5) lists a grep to enumerate exact sites.
- **Type consistency:** `record_buyplan_purchase_history(db, plan, *, refresh=True) -> list[int]` is defined in Task 2 and consumed identically in Tasks 3 (`record_buyplan_purchase_history(db, plan)`), 4 (adds the `refresh` call), and 7 (`refresh=False`). `refresh_matches_for_cards(db, card_ids, *, per_card_limit=5) -> int` defined + used in Task 4. `backfill(db) -> int` defined + used in Task 7. `_aggregate_cph_by_company(cph_rows) -> dict[int, dict]` defined + used in Task 6.
