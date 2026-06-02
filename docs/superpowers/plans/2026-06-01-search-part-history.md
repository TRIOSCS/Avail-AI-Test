# Search Page — Part History Column Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a "What we know" column to the search page that surfaces the searched part's internal history (offers, buyers, confirmed/won, sightings, requisitions, price trend) beside the live supplier results.

**Architecture:** A new read-only `part_history_service.get_part_history()` aggregates the history (scoped by `material_card_id`) into a `PartHistory` dataclass. A new HTMX partial endpoint renders it into the right column, loaded in parallel with the existing SSE live-search stream. The materials detail router is refactored to consume the same service helpers, removing query duplication.

**Tech Stack:** FastAPI, SQLAlchemy 2.0, Jinja2, HTMX 2.x, Alpine.js 3.x, Tailwind, pytest (SQLite in-memory).

**Spec:** `docs/superpowers/specs/2026-06-01-search-part-history-design.md`

**Working dir:** `/root/availai-worktrees/search-part-history` (worktree on branch `feat/search-part-history`). All `pytest` runs use `TESTING=1 PYTHONPATH=/root/availai-worktrees/search-part-history`.

---

## File Structure

- **Create** `app/services/part_history_service.py` — `PartHistory`/`PriceTrend` dataclasses + `get_part_history()` + discrete per-section query helpers. One responsibility: assemble a part's internal history.
- **Create** `app/templates/htmx/partials/search/history_panel.html` — renders `PartHistory` (header, stat row, accordion sections, buyers row, empty state).
- **Modify** `app/templates/htmx/partials/search/results_shell.html` — wrap existing content in a 2-column grid; add the right column with the `hx-get` trigger + skeleton.
- **Modify** `app/routers/htmx_views.py` — add `GET /v2/partials/search/history`; refactor `material_detail_partial` + `material_tab_partial` to call the shared helpers.
- **Create** `tests/test_part_history_service.py` — service unit tests.
- **Create** `tests/test_search_history_endpoint.py` — endpoint tests.
- **Modify** `tests/` (materials parity) — add a regression assertion if not already covered.
- **Modify** `docs/APP_MAP_INTERACTIONS.md`, `docs/APP_MAP_ARCHITECTURE.md`.

---

## Task 1: `PartHistory` dataclass + card resolution (read-only)

**Files:**
- Create: `app/services/part_history_service.py`
- Test: `tests/test_part_history_service.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_part_history_service.py
"""Tests for part_history_service — assembles a part's internal history.
Called by: the search history endpoint and the materials detail router.
Depends on: MaterialCard, Offer, Sighting, Requirement, CustomerPartHistory, MaterialPriceSnapshot.
"""
from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session

from app.models.intelligence import MaterialCard
from app.services.part_history_service import PartHistory, get_part_history


def _make_card(db: Session, norm="lm317t", display="LM317T", mfr="TI") -> MaterialCard:
    card = MaterialCard(normalized_mpn=norm, display_mpn=display, manufacturer=mfr,
                        lifecycle_status="active", search_count=0)
    db.add(card)
    db.commit()
    db.refresh(card)
    return card


def test_no_card_returns_not_found(db_session: Session):
    result = get_part_history(db_session, "doesnotexist")
    assert isinstance(result, PartHistory)
    assert result.found is False
    assert result.card_id is None


def test_soft_deleted_card_is_not_found(db_session: Session):
    card = _make_card(db_session)
    card.deleted_at = datetime.now(timezone.utc)
    db_session.commit()
    assert get_part_history(db_session, "lm317t").found is False


def test_card_found_populates_header(db_session: Session):
    card = _make_card(db_session)
    result = get_part_history(db_session, "lm317t")
    assert result.found is True
    assert result.card_id == card.id
    assert result.display_mpn == "LM317T"
    assert result.manufacturer == "TI"
    assert result.lifecycle_status == "active"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TESTING=1 PYTHONPATH=/root/availai-worktrees/search-part-history pytest tests/test_part_history_service.py -v --override-ini="addopts="`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.part_history_service'`.

- [ ] **Step 3: Write minimal implementation**

```python
# app/services/part_history_service.py
"""Assemble a material part's internal history for display.

What it does: given a MaterialCard's normalized key, returns a PartHistory summary
(offers, buyers, confirmed/won, sightings, requirements, price trend).
Called by: htmx_views search-history endpoint and materials detail/tab partials.
Depends on: MaterialCard, Offer, Sighting, Requirement, CustomerPartHistory,
            MaterialPriceSnapshot, User.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models.intelligence import MaterialCard


@dataclass
class PriceTrend:
    min_price: Decimal | None = None
    max_price: Decimal | None = None
    last_price: Decimal | None = None
    currency: str = "USD"


@dataclass
class PartHistory:
    found: bool = False
    card_id: int | None = None
    display_mpn: str = ""
    manufacturer: str = ""
    lifecycle_status: str | None = None
    offers: list = field(default_factory=list)
    offers_count: int = 0
    buyers: list = field(default_factory=list)
    won_offers: list = field(default_factory=list)
    customer_purchases: list = field(default_factory=list)
    confirmed_count: int = 0
    sightings: list = field(default_factory=list)
    sightings_count: int = 0
    requirements: list = field(default_factory=list)
    requirements_count: int = 0
    price_trend: PriceTrend | None = None


def _resolve_card(db: Session, normalized_key: str) -> MaterialCard | None:
    if not normalized_key:
        return None
    return (
        db.query(MaterialCard)
        .filter(MaterialCard.normalized_mpn == normalized_key)
        .filter(MaterialCard.deleted_at.is_(None))
        .first()
    )


def get_part_history(db: Session, normalized_key: str) -> PartHistory:
    card = _resolve_card(db, normalized_key)
    if card is None:
        return PartHistory(found=False)
    return PartHistory(
        found=True,
        card_id=card.id,
        display_mpn=card.display_mpn or "",
        manufacturer=card.manufacturer or "",
        lifecycle_status=card.lifecycle_status,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `TESTING=1 PYTHONPATH=/root/availai-worktrees/search-part-history pytest tests/test_part_history_service.py -v --override-ini="addopts="`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add app/services/part_history_service.py tests/test_part_history_service.py
git commit -m "feat(part-history): PartHistory dataclass + read-only card resolution"
```

---

## Task 2: Offers + buyers aggregation

**Files:**
- Modify: `app/services/part_history_service.py`
- Test: `tests/test_part_history_service.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_part_history_service.py`:

```python
from app.models.auth import User
from app.models.offers import Offer
from app.models.sourcing import Requisition


def _make_user(db: Session, email="b1@trioscs.com", name="Buyer One") -> User:
    u = User(email=email, name=name, role="buyer", azure_id=f"az-{email}",
             created_at=datetime.now(timezone.utc))
    db.add(u); db.commit(); db.refresh(u)
    return u


def _make_requisition(db: Session, status="active", customer="ACME") -> Requisition:
    r = Requisition(name="R", customer_name=customer, status=status)
    db.add(r); db.commit(); db.refresh(r)
    return r


def _make_offer(db: Session, card, req, user, status="active", vendor="Avnet"):
    o = Offer(requisition_id=req.id, material_card_id=card.id, vendor_name=vendor,
              mpn=card.display_mpn, qty_available=100, unit_price=Decimal("4.10"),
              status=status, entered_by_id=user.id,
              created_at=datetime.now(timezone.utc))
    db.add(o); db.commit(); db.refresh(o)
    return o


def test_offers_and_buyers(db_session: Session):
    from decimal import Decimal
    card = _make_card(db_session)
    req = _make_requisition(db_session)
    u1 = _make_user(db_session)
    u2 = _make_user(db_session, email="b2@trioscs.com", name="Buyer Two")
    _make_offer(db_session, card, req, u1, vendor="Avnet")
    _make_offer(db_session, card, req, u1, vendor="TTI")
    _make_offer(db_session, card, req, u2, vendor="Mouser")

    h = get_part_history(db_session, "lm317t")
    assert h.offers_count == 3
    assert len(h.offers) == 3            # top-N (<=5) most recent
    buyer_names = {b.name for b in h.buyers}
    assert buyer_names == {"Buyer One", "Buyer Two"}   # distinct
```

(Note: add `from decimal import Decimal` at top of the test module if not already present.)

- [ ] **Step 2: Run test to verify it fails**

Run: `TESTING=1 PYTHONPATH=/root/availai-worktrees/search-part-history pytest tests/test_part_history_service.py::test_offers_and_buyers -v --override-ini="addopts="`
Expected: FAIL — `offers_count == 0`.

- [ ] **Step 3: Write minimal implementation**

In `app/services/part_history_service.py`, add the constant and helpers, and call them from `get_part_history`:

```python
TOP_N = 5

# add to imports at top:
from sqlalchemy import func
from app.models.offers import Offer
from app.models.auth import User


def offers_for_card(db: Session, card_id: int, limit: int = TOP_N) -> list[Offer]:
    return (
        db.query(Offer)
        .filter(Offer.material_card_id == card_id)
        .order_by(Offer.created_at.desc().nullslast())
        .limit(limit)
        .all()
    )


def buyers_for_card(db: Session, card_id: int) -> list[User]:
    return (
        db.query(User)
        .join(Offer, Offer.entered_by_id == User.id)
        .filter(Offer.material_card_id == card_id)
        .distinct()
        .all()
    )
```

Then in `get_part_history`, replace the final `return PartHistory(...)` with assembly that fills these fields:

```python
    offers = offers_for_card(db, card.id)
    offers_count = db.query(func.count(Offer.id)).filter(Offer.material_card_id == card.id).scalar() or 0
    buyers = buyers_for_card(db, card.id)
    return PartHistory(
        found=True,
        card_id=card.id,
        display_mpn=card.display_mpn or "",
        manufacturer=card.manufacturer or "",
        lifecycle_status=card.lifecycle_status,
        offers=offers,
        offers_count=offers_count,
        buyers=buyers,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `TESTING=1 PYTHONPATH=/root/availai-worktrees/search-part-history pytest tests/test_part_history_service.py -v --override-ini="addopts="`
Expected: all passed.

- [ ] **Step 5: Commit**

```bash
git add app/services/part_history_service.py tests/test_part_history_service.py
git commit -m "feat(part-history): offers + distinct buyers aggregation"
```

---

## Task 3: Confirmed/Won aggregation (won offers + won reqs + customer purchases)

**Files:**
- Modify: `app/services/part_history_service.py`
- Test: `tests/test_part_history_service.py`

- [ ] **Step 1: Write the failing test**

```python
from app.constants import OfferStatus, SourcingStatus
from app.models.purchase_history import CustomerPartHistory
from app.models.crm import Company
from app.models.sourcing import Requirement


def test_confirmed_won_composition(db_session: Session):
    from decimal import Decimal
    card = _make_card(db_session)
    req = _make_requisition(db_session)
    u1 = _make_user(db_session)
    # 1 won offer + 1 sold offer + 1 active offer
    _make_offer(db_session, card, req, u1, status=OfferStatus.WON, vendor="Avnet")
    _make_offer(db_session, card, req, u1, status=OfferStatus.SOLD, vendor="TTI")
    _make_offer(db_session, card, req, u1, status=OfferStatus.ACTIVE, vendor="Mouser")
    # 1 won requirement
    wr = Requirement(requisition_id=req.id, primary_mpn="LM317T",
                     material_card_id=card.id, sourcing_status=SourcingStatus.WON)
    db_session.add(wr); db_session.commit()
    # 1 customer purchase row
    co = Company(name="ACME Inc")
    db_session.add(co); db_session.commit(); db_session.refresh(co)
    cph = CustomerPartHistory(company_id=co.id, material_card_id=card.id, mpn="LM317T",
                              source="acctivate_po", purchase_count=2, total_quantity=500,
                              avg_unit_price=Decimal("3.90"))
    db_session.add(cph); db_session.commit()

    h = get_part_history(db_session, "lm317t")
    assert len(h.won_offers) == 2                  # won + sold
    assert len(h.customer_purchases) == 1
    # confirmed_count = won/sold offers (2) + won reqs (1) + customer rows (1) = 4
    assert h.confirmed_count == 4
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TESTING=1 PYTHONPATH=/root/availai-worktrees/search-part-history pytest tests/test_part_history_service.py::test_confirmed_won_composition -v --override-ini="addopts="`
Expected: FAIL — `won_offers` empty.

- [ ] **Step 3: Write minimal implementation**

Add imports and helpers to `part_history_service.py`:

```python
from app.constants import OfferStatus, SourcingStatus
from app.models.purchase_history import CustomerPartHistory
from app.models.sourcing import Requirement

_WON_OFFER_STATUSES = (OfferStatus.WON, OfferStatus.SOLD)


def won_offers_for_card(db: Session, card_id: int, limit: int = TOP_N) -> list[Offer]:
    return (
        db.query(Offer)
        .filter(Offer.material_card_id == card_id, Offer.status.in_(_WON_OFFER_STATUSES))
        .order_by(Offer.created_at.desc().nullslast())
        .limit(limit)
        .all()
    )


def customer_purchases_for_card(db: Session, card_id: int, limit: int = TOP_N) -> list[CustomerPartHistory]:
    return (
        db.query(CustomerPartHistory)
        .filter(CustomerPartHistory.material_card_id == card_id)
        .order_by(CustomerPartHistory.last_purchased_at.desc().nullslast())
        .limit(limit)
        .all()
    )
```

In `get_part_history`, before the `return`, add:

```python
    won_offers = won_offers_for_card(db, card.id)
    customer_purchases = customer_purchases_for_card(db, card.id)
    won_offer_count = (
        db.query(func.count(Offer.id))
        .filter(Offer.material_card_id == card.id, Offer.status.in_(_WON_OFFER_STATUSES))
        .scalar()
    ) or 0
    won_req_count = (
        db.query(func.count(Requirement.id))
        .filter(Requirement.material_card_id == card.id, Requirement.sourcing_status == SourcingStatus.WON)
        .scalar()
    ) or 0
    customer_count = (
        db.query(func.count(CustomerPartHistory.id))
        .filter(CustomerPartHistory.material_card_id == card.id)
        .scalar()
    ) or 0
    confirmed_count = won_offer_count + won_req_count + customer_count
```

Add to the `PartHistory(...)` call: `won_offers=won_offers, customer_purchases=customer_purchases, confirmed_count=confirmed_count,`.

- [ ] **Step 4: Run test to verify it passes**

Run: `TESTING=1 PYTHONPATH=/root/availai-worktrees/search-part-history pytest tests/test_part_history_service.py -v --override-ini="addopts="`
Expected: all passed.

- [ ] **Step 5: Commit**

```bash
git add app/services/part_history_service.py tests/test_part_history_service.py
git commit -m "feat(part-history): confirmed/won aggregation (offers + reqs + purchases)"
```

---

## Task 4: Sightings + requirements + price trend

**Files:**
- Modify: `app/services/part_history_service.py`
- Test: `tests/test_part_history_service.py`

- [ ] **Step 1: Write the failing test**

```python
from app.models.sourcing import Sighting
from app.models.price_snapshot import MaterialPriceSnapshot


def test_sightings_requirements_price_trend(db_session: Session):
    from decimal import Decimal
    card = _make_card(db_session)
    req = _make_requisition(db_session)
    requirement = Requirement(requisition_id=req.id, primary_mpn="LM317T",
                              material_card_id=card.id, sourcing_status="open")
    db_session.add(requirement); db_session.commit(); db_session.refresh(requirement)
    db_session.add(Sighting(requirement_id=requirement.id, material_card_id=card.id,
                            vendor_name="Avnet", qty_available=50, unit_price=Decimal("4.0"),
                            source_type="brokerbin"))
    db_session.commit()
    for p in (Decimal("3.0"), Decimal("5.0"), Decimal("4.0")):
        db_session.add(MaterialPriceSnapshot(material_card_id=card.id, vendor_name="Avnet",
                                             price=p, source="brokerbin"))
    db_session.commit()

    h = get_part_history(db_session, "lm317t")
    assert h.sightings_count == 1
    assert h.requirements_count == 1
    assert h.price_trend is not None
    assert h.price_trend.min_price == Decimal("3.0")
    assert h.price_trend.max_price == Decimal("5.0")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TESTING=1 PYTHONPATH=/root/availai-worktrees/search-part-history pytest tests/test_part_history_service.py::test_sightings_requirements_price_trend -v --override-ini="addopts="`
Expected: FAIL — `sightings_count == 0`.

- [ ] **Step 3: Write minimal implementation**

Add imports and helpers:

```python
from app.models.sourcing import Sighting
from app.models.price_snapshot import MaterialPriceSnapshot


def sightings_for_card(db: Session, card_id: int, limit: int = TOP_N) -> list[Sighting]:
    return (
        db.query(Sighting)
        .filter(Sighting.material_card_id == card_id)
        .order_by(Sighting.created_at.desc().nullslast())
        .limit(limit)
        .all()
    )


def requirements_for_card(db: Session, card_id: int, limit: int = TOP_N) -> list[Requirement]:
    return (
        db.query(Requirement)
        .filter(Requirement.material_card_id == card_id)
        .order_by(Requirement.created_at.desc().nullslast())
        .limit(limit)
        .all()
    )


def price_trend_for_card(db: Session, card_id: int) -> PriceTrend | None:
    agg = (
        db.query(func.min(MaterialPriceSnapshot.price), func.max(MaterialPriceSnapshot.price))
        .filter(MaterialPriceSnapshot.material_card_id == card_id)
        .first()
    )
    if not agg or agg[0] is None:
        return None
    last = (
        db.query(MaterialPriceSnapshot)
        .filter(MaterialPriceSnapshot.material_card_id == card_id)
        .order_by(MaterialPriceSnapshot.recorded_at.desc().nullslast())
        .first()
    )
    return PriceTrend(
        min_price=agg[0],
        max_price=agg[1],
        last_price=last.price if last else None,
        currency=(last.currency if last else "USD"),
    )
```

In `get_part_history`, before `return`, add:

```python
    sightings = sightings_for_card(db, card.id)
    sightings_count = db.query(func.count(Sighting.id)).filter(Sighting.material_card_id == card.id).scalar() or 0
    requirements = requirements_for_card(db, card.id)
    requirements_count = db.query(func.count(Requirement.id)).filter(Requirement.material_card_id == card.id).scalar() or 0
    price_trend = price_trend_for_card(db, card.id)
```

Add to `PartHistory(...)`: `sightings=sightings, sightings_count=sightings_count, requirements=requirements, requirements_count=requirements_count, price_trend=price_trend,`.

- [ ] **Step 4: Run test to verify it passes**

Run: `TESTING=1 PYTHONPATH=/root/availai-worktrees/search-part-history pytest tests/test_part_history_service.py -v --override-ini="addopts="`
Expected: all passed.

- [ ] **Step 5: Commit**

```bash
git add app/services/part_history_service.py tests/test_part_history_service.py
git commit -m "feat(part-history): sightings, requirements, price-trend aggregation"
```

---

## Task 5: Refactor materials detail router to use shared helpers

**Files:**
- Modify: `app/routers/htmx_views.py` (`material_detail_partial` ~line 7192, `material_tab_partial` ~line 7233)
- Test: `tests/test_materials_detail_parity.py` (create)

**Verify line numbers first:** `grep -n "async def material_detail_partial\|async def material_tab_partial" app/routers/htmx_views.py`.

- [ ] **Step 1: Write the failing test (parity guard)**

```python
# tests/test_materials_detail_parity.py
"""Guards that the materials detail + tabs still render the same history
after refactoring them onto part_history_service helpers.
"""
from datetime import datetime, timezone
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.intelligence import MaterialCard
from app.models.offers import Offer
from app.models.sourcing import Requisition, Sighting, Requirement


def _seed(db: Session):
    card = MaterialCard(normalized_mpn="lm317t", display_mpn="LM317T", manufacturer="TI", search_count=0)
    db.add(card); db.commit(); db.refresh(card)
    req = Requisition(name="R", customer_name="ACME", status="active")
    db.add(req); db.commit(); db.refresh(req)
    db.add(Offer(requisition_id=req.id, material_card_id=card.id, vendor_name="Avnet",
                 mpn="LM317T", qty_available=10, unit_price=Decimal("4.1"), status="active",
                 created_at=datetime.now(timezone.utc)))
    requirement = Requirement(requisition_id=req.id, primary_mpn="LM317T",
                              material_card_id=card.id, sourcing_status="open")
    db.add(requirement); db.commit(); db.refresh(requirement)
    db.add(Sighting(requirement_id=requirement.id, material_card_id=card.id, vendor_name="TTI",
                    qty_available=5, unit_price=Decimal("4.3"), source_type="brokerbin"))
    db.commit()
    return card


def test_material_detail_renders_offer_and_sighting(client: TestClient, db_session: Session):
    card = _seed(db_session)
    resp = client.get(f"/v2/partials/materials/{card.id}", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert "Avnet" in resp.text   # offer vendor
    assert "TTI" in resp.text     # sighting vendor


def test_material_sourcing_tab_renders_requirement(client: TestClient, db_session: Session):
    card = _seed(db_session)
    resp = client.get(f"/v2/partials/materials/{card.id}/tab/sourcing", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert "LM317T" in resp.text
```

- [ ] **Step 2: Run test to verify current behavior (should PASS before refactor)**

Run: `TESTING=1 PYTHONPATH=/root/availai-worktrees/search-part-history pytest tests/test_materials_detail_parity.py -v --override-ini="addopts="`
Expected: 2 passed (this is a characterization test — it passes now and must keep passing after the refactor).

- [ ] **Step 3: Refactor the router to call the shared helpers**

In `app/routers/htmx_views.py`, add to the top-level imports:

```python
from app.services.part_history_service import (
    offers_for_card,
    sightings_for_card,
    requirements_for_card,
    customer_purchases_for_card,
    price_trend_for_card,
)
```

In `material_detail_partial`, replace the inline `sightings = ...` and `offers = ...` queries with (keep the existing limit of 50 by passing it explicitly):

```python
    sightings = sightings_for_card(db, card_id, limit=50)
    offers = offers_for_card(db, card_id, limit=50)
```

In `material_tab_partial`, replace the inline queries in the `sourcing`, `customers`, and `price_history` branches:

```python
    elif tab_name == "sourcing":
        ctx["requirements"] = requirements_for_card(db, card_id, limit=200)
        return template_response("htmx/partials/materials/tabs/sourcing.html", ctx)
    elif tab_name == "customers":
        ctx["customers"] = customer_purchases_for_card(db, card_id, limit=200)
        return template_response("htmx/partials/materials/tabs/customers.html", ctx)
    elif tab_name == "price_history":
        ctx["snapshots"] = (
            db.query(MaterialPriceSnapshot)
            .filter_by(material_card_id=card_id)
            .order_by(MaterialPriceSnapshot.recorded_at.desc())
            .limit(200)
            .all()
        )
        return template_response("htmx/partials/materials/tabs/price_history.html", ctx)
```

(The `vendors` branch keeps its `MaterialVendorHistory` query unchanged — that data is not part of `PartHistory`.) The `price_history` branch is left inline because the materials tab needs an ordered list of 200 snapshots, not the min/max/last summary; `price_trend_for_card` is only for the search panel. Keep the `MaterialPriceSnapshot` import in that branch.

- [ ] **Step 4: Run parity tests + service tests**

Run: `TESTING=1 PYTHONPATH=/root/availai-worktrees/search-part-history pytest tests/test_materials_detail_parity.py tests/test_part_history_service.py -v --override-ini="addopts="`
Expected: all passed.

- [ ] **Step 5: Commit**

```bash
git add app/routers/htmx_views.py tests/test_materials_detail_parity.py
git commit -m "refactor(materials): consume part_history_service helpers (dedup queries)"
```

---

## Task 6: Search history endpoint

**Files:**
- Modify: `app/routers/htmx_views.py` (add endpoint near other `/v2/partials/search/*` routes, ~line 2930)
- Test: `tests/test_search_history_endpoint.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_search_history_endpoint.py
"""Tests for GET /v2/partials/search/history — the search-page history panel."""
from datetime import datetime, timezone
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.intelligence import MaterialCard
from app.models.offers import Offer
from app.models.sourcing import Requisition


def test_unknown_mpn_returns_empty_state(client: TestClient, db_session: Session):
    resp = client.get("/v2/partials/search/history?mpn=NOSUCHPART", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert "looks new" in resp.text.lower()


def test_known_mpn_renders_history(client: TestClient, db_session: Session):
    card = MaterialCard(normalized_mpn="lm317t", display_mpn="LM317T", manufacturer="TI", search_count=0)
    db_session.add(card); db_session.commit(); db_session.refresh(card)
    req = Requisition(name="R", customer_name="ACME", status="active")
    db_session.add(req); db_session.commit(); db_session.refresh(req)
    db_session.add(Offer(requisition_id=req.id, material_card_id=card.id, vendor_name="Avnet",
                         mpn="LM317T", qty_available=10, unit_price=Decimal("4.1"), status="active",
                         created_at=datetime.now(timezone.utc)))
    db_session.commit()

    resp = client.get("/v2/partials/search/history?mpn=LM-317T", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert "Avnet" in resp.text                 # offer rendered
    assert f"/v2/materials/{card.id}" in resp.text   # deep link to full part page
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TESTING=1 PYTHONPATH=/root/availai-worktrees/search-part-history pytest tests/test_search_history_endpoint.py -v --override-ini="addopts="`
Expected: FAIL — 404 (route not defined).

- [ ] **Step 3: Write the endpoint**

Add near the other search partials in `app/routers/htmx_views.py` (and ensure `from app.utils.normalization import normalize_mpn_key` and `from app.services.part_history_service import get_part_history` are imported at top):

```python
@router.get("/v2/partials/search/history", response_class=HTMLResponse)
async def search_history_panel(
    request: Request,
    mpn: str = "",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Render the 'What we know' history panel for the searched MPN.

    Called by: results_shell.html right column (hx-get).
    Depends on: part_history_service.get_part_history, normalize_mpn_key.
    """
    from app.services.part_history_service import PartHistory, get_part_history

    try:
        key = normalize_mpn_key(mpn)
        history = get_part_history(db, key)
    except Exception:
        logger.exception("search_history_panel failed for mpn={}", mpn)
        history = PartHistory(found=False)
        return template_response(
            "htmx/partials/search/history_panel.html",
            {**_base_ctx(request, user, "search"), "history": history, "error": True},
        )
    ctx = _base_ctx(request, user, "search")
    ctx.update({"history": history, "error": False})
    return template_response("htmx/partials/search/history_panel.html", ctx)
```

- [ ] **Step 4: Run test (will fail until template exists — Task 7)**

Run: `TESTING=1 PYTHONPATH=/root/availai-worktrees/search-part-history pytest tests/test_search_history_endpoint.py -v --override-ini="addopts="`
Expected: FAIL — `jinja2.exceptions.TemplateNotFound: htmx/partials/search/history_panel.html`. (Endpoint is wired; template comes next.)

- [ ] **Step 5: Commit**

```bash
git add app/routers/htmx_views.py tests/test_search_history_endpoint.py
git commit -m "feat(search): add /v2/partials/search/history endpoint"
```

---

## Task 7: History panel template

**Files:**
- Create: `app/templates/htmx/partials/search/history_panel.html`

- [ ] **Step 1: Create the template**

```jinja
{# What-we-know panel — searched part's internal history.
   Called by: htmx_views.search_history_panel
   Depends on: PartHistory (history), Alpine.js for the accordion. #}
{% if not history.found %}
<div class="rounded-xl bg-white p-6 text-center border border-gray-100 shadow-sm">
  <p class="text-sm font-medium text-gray-500">No prior history</p>
  <p class="text-xs text-gray-400 mt-1">This part looks new to us.</p>
</div>
{% else %}
<div class="rounded-xl bg-white border border-gray-100 shadow-sm" x-data="{ open: 'offers' }">
  {# Header #}
  <div class="p-4 border-b border-gray-100">
    <div class="flex items-center justify-between gap-2">
      <div class="min-w-0">
        <p class="text-sm font-semibold text-gray-900 truncate">{{ history.display_mpn }}</p>
        <p class="text-xs text-gray-500 truncate">
          {{ history.manufacturer or "—" }}
          {% if history.lifecycle_status %}
          <span class="ml-1 inline-block rounded px-1.5 py-0.5 text-[10px] font-medium bg-gray-100 text-gray-600">
            {{ history.lifecycle_status }}</span>
          {% endif %}
        </p>
      </div>
      <a href="/v2/materials/{{ history.card_id }}" hx-get="/v2/partials/materials/{{ history.card_id }}"
         hx-target="#main-content" hx-push-url="/v2/materials/{{ history.card_id }}"
         class="shrink-0 text-xs font-medium text-blue-600 hover:text-blue-700">Open full part page →</a>
    </div>
    {% if error %}
    <p class="mt-2 text-xs text-amber-600">Some history could not be loaded.</p>
    {% endif %}
    {# Summary stat row #}
    <div class="mt-3 flex flex-wrap gap-x-3 gap-y-1 text-xs text-gray-600">
      <span><span class="font-semibold text-gray-900">{{ history.offers_count }}</span> Offers</span>
      <span><span class="font-semibold text-gray-900">{{ history.confirmed_count }}</span> Won</span>
      <span><span class="font-semibold text-gray-900">{{ history.sightings_count }}</span> Sightings</span>
      <span><span class="font-semibold text-gray-900">{{ history.requirements_count }}</span> Reqs</span>
      <span><span class="font-semibold text-gray-900">{{ history.buyers|length }}</span> Buyers</span>
    </div>
    {% if history.buyers %}
    <div class="mt-2 flex flex-wrap gap-1">
      {% for b in history.buyers %}
      <span class="inline-block rounded-full bg-blue-50 px-2 py-0.5 text-[11px] text-blue-700">{{ b.name or b.email }}</span>
      {% endfor %}
    </div>
    {% endif %}
  </div>

  {# Accordion sections #}
  {% macro section(key, label, count) %}
  <div class="border-b border-gray-100 last:border-b-0">
    <button type="button" @click="open = (open === '{{ key }}' ? '' : '{{ key }}')"
            class="flex w-full items-center justify-between px-4 py-2.5 text-left text-sm font-medium text-gray-700 hover:bg-gray-50">
      <span>{{ label }} <span class="text-gray-400">({{ count }})</span></span>
      <span x-text="open === '{{ key }}' ? '▾' : '▸'" class="text-gray-400"></span>
    </button>
    <div x-show="open === '{{ key }}'" x-collapse class="px-4 pb-3 text-xs text-gray-600">{{ caller() }}</div>
  </div>
  {% endmacro %}

  {% call section('offers', 'Offers', history.offers_count) %}
    {% for o in history.offers %}
    <div class="flex items-center justify-between py-1 border-b border-gray-50 last:border-0">
      <span class="truncate">{{ o.vendor_name }}</span>
      <span class="shrink-0 text-gray-500">{{ o.qty_available or "—" }} · ${{ o.unit_price or "—" }} · {{ o.status }}</span>
    </div>
    {% else %}<p class="py-1 text-gray-400">None.</p>{% endfor %}
    {% if history.offers_count > history.offers|length %}
    <a href="/v2/materials/{{ history.card_id }}" class="mt-1 inline-block text-blue-600">View all on part page →</a>{% endif %}
  {% endcall %}

  {% call section('confirmed', 'Confirmed / Won', history.confirmed_count) %}
    {% for o in history.won_offers %}
    <div class="flex items-center justify-between py-1 border-b border-gray-50 last:border-0">
      <span class="truncate">{{ o.vendor_name }}</span>
      <span class="shrink-0 text-emerald-600">{{ o.status }} · ${{ o.unit_price or "—" }}</span>
    </div>
    {% endfor %}
    {% for c in history.customer_purchases %}
    <div class="flex items-center justify-between py-1 border-b border-gray-50 last:border-0">
      <span class="truncate">{{ c.company.name if c.company else "Customer" }}</span>
      <span class="shrink-0 text-gray-500">×{{ c.purchase_count }} · ${{ c.avg_unit_price or "—" }}</span>
    </div>
    {% endfor %}
    {% if not history.won_offers and not history.customer_purchases %}<p class="py-1 text-gray-400">None.</p>{% endif %}
  {% endcall %}

  {% call section('sightings', 'Sightings', history.sightings_count) %}
    {% for s in history.sightings %}
    <div class="flex items-center justify-between py-1 border-b border-gray-50 last:border-0">
      <span class="truncate">{{ s.vendor_name }}</span>
      <span class="shrink-0 text-gray-500">{{ s.qty_available or "—" }} · ${{ s.unit_price or "—" }} · {{ s.source_type or "—" }}</span>
    </div>
    {% else %}<p class="py-1 text-gray-400">None.</p>{% endfor %}
  {% endcall %}

  {% call section('reqs', 'Requisitions', history.requirements_count) %}
    {% for r in history.requirements %}
    <div class="flex items-center justify-between py-1 border-b border-gray-50 last:border-0">
      <span class="truncate">Req #{{ r.requisition_id }}</span>
      <span class="shrink-0 text-gray-500">{{ r.sourcing_status }}</span>
    </div>
    {% else %}<p class="py-1 text-gray-400">None.</p>{% endfor %}
  {% endcall %}

  {% if history.price_trend %}
  <div class="px-4 py-2.5 text-xs text-gray-600 border-t border-gray-100">
    Price ({{ history.price_trend.currency }}):
    <span class="font-medium">${{ history.price_trend.min_price }}</span> –
    <span class="font-medium">${{ history.price_trend.max_price }}</span>
    · last ${{ history.price_trend.last_price or "—" }}
  </div>
  {% endif %}
</div>
{% endif %}
```

- [ ] **Step 2: Run the endpoint tests**

Run: `TESTING=1 PYTHONPATH=/root/availai-worktrees/search-part-history pytest tests/test_search_history_endpoint.py -v --override-ini="addopts="`
Expected: 2 passed.

- [ ] **Step 3: Verify `x-collapse` is available** — confirm Alpine's collapse plugin is loaded (the codebase already uses `x-collapse` elsewhere):

Run: `grep -rl "x-collapse" app/templates | head -1` (expect a hit, confirming the plugin is registered). If no existing usage, replace `x-collapse` with nothing (plain `x-show`) to avoid depending on an unregistered plugin.

- [ ] **Step 4: Commit**

```bash
git add app/templates/htmx/partials/search/history_panel.html
git commit -m "feat(search): history panel template (what-we-know)"
```

---

## Task 8: Two-column layout in results shell

**Files:**
- Modify: `app/templates/htmx/partials/search/results_shell.html`

- [ ] **Step 1: Wrap existing content + add right column**

Replace the **opening** of `results_shell.html` — change the outer wrapper from:

```html
<div id="search-results-wrapper" class="space-y-4">
```

to a two-column grid whose first child is the existing live-results column. Concretely, wrap the current contents:

```html
<div class="grid grid-cols-1 lg:grid-cols-[1fr_360px] gap-4 items-start">
  <div id="search-results-wrapper" class="space-y-4">
    {# … all existing source-progress / SSE / stats / empty-state / shortlist content stays here unchanged … #}
  </div>

  {# Right column — "What we know" history panel, loaded in parallel with the SSE stream #}
  <aside id="part-history-col"
         hx-get="/v2/partials/search/history?mpn={{ mpn | urlencode }}"
         hx-trigger="load"
         hx-swap="innerHTML"
         class="lg:sticky lg:top-4">
    <div class="rounded-xl bg-white p-6 border border-gray-100 shadow-sm animate-pulse">
      <div class="h-4 w-2/3 bg-gray-100 rounded"></div>
      <div class="mt-3 h-3 w-1/2 bg-gray-100 rounded"></div>
      <div class="mt-4 space-y-2">
        <div class="h-3 w-full bg-gray-100 rounded"></div>
        <div class="h-3 w-5/6 bg-gray-100 rounded"></div>
      </div>
    </div>
  </aside>
</div>
```

The trailing `<script>` block (SSE chip handler) stays after the grid, unchanged. **Do not** alter the inner IDs (`search-results-cards`, `source-progress`, etc.) — the SSE wiring depends on them.

- [ ] **Step 2: Verify the existing search-streaming tests still pass**

Run: `TESTING=1 PYTHONPATH=/root/availai-worktrees/search-part-history pytest tests/test_search_streaming.py -v --override-ini="addopts="`
Expected: all passed (the shell still renders; only its outer layout changed).

- [ ] **Step 3: Validate the template renders with a sample context** — run the endpoint test that exercises `search_run` if one exists, else rely on Step 2. Confirm no Jinja syntax error:

Run: `TESTING=1 PYTHONPATH=/root/availai-worktrees/search-part-history pytest tests/ -k "search" -v --override-ini="addopts="`
Expected: all passed.

- [ ] **Step 4: Commit**

```bash
git add app/templates/htmx/partials/search/results_shell.html
git commit -m "feat(search): two-column results shell with history panel"
```

---

## Task 9: Docs + full suite + lint

**Files:**
- Modify: `docs/APP_MAP_INTERACTIONS.md`, `docs/APP_MAP_ARCHITECTURE.md`

- [ ] **Step 1: Update APP_MAP_INTERACTIONS.md** — add a subsection under the search data-flow describing: search → `results_shell` renders 2-column → right column `hx-get /v2/partials/search/history` → `normalize_mpn_key` → `get_part_history` (scoped by `material_card_id`) → `history_panel.html`, loaded in parallel with the SSE live stream.

- [ ] **Step 2: Update APP_MAP_ARCHITECTURE.md** — add `app/services/part_history_service.py` to the services list with a one-line description ("assembles a part's internal history; consumed by the search history panel and the materials detail router").

- [ ] **Step 3: Run the full suite**

Run: `TESTING=1 PYTHONPATH=/root/availai-worktrees/search-part-history pytest tests/ -q`
Expected: all passed (no regressions).

- [ ] **Step 4: Lint + type + format (changed files)**

Run:
```bash
ruff check app/ && ruff format --check app/ && mypy app/services/part_history_service.py
```
Expected: clean. Fix any findings.

- [ ] **Step 5: Pre-commit on changed files**

Run: `pre-commit run --files app/services/part_history_service.py app/routers/htmx_views.py app/templates/htmx/partials/search/history_panel.html app/templates/htmx/partials/search/results_shell.html`
Expected: all hooks pass.

- [ ] **Step 6: Commit**

```bash
git add docs/APP_MAP_INTERACTIONS.md docs/APP_MAP_ARCHITECTURE.md
git commit -m "docs(app-map): search part-history flow + part_history_service"
```

---

## Self-Review notes (resolved)

- **Spec coverage:** offers (T2), buyers (T2), confirmed/won = won offers+reqs+purchases (T3), sightings+reqs+price (T4), shared-service refactor/dedup (T5), endpoint with `normalize_mpn_key` read-only lookup (T6), clean organized panel + empty state (T7), split-column layout (T8), error handling (T6 try/except + T7 banner), tests (T1–T6), docs + no migration (T9). All covered.
- **Type consistency:** helper names (`offers_for_card`, `sightings_for_card`, `requirements_for_card`, `customer_purchases_for_card`, `won_offers_for_card`, `buyers_for_card`, `price_trend_for_card`) are used identically across T2–T6. `PartHistory`/`PriceTrend` field names match template accesses in T7. `MaterialPriceSnapshot.price` (not `unit_price`) used consistently.
- **Placeholder scan:** no TBD/TODO; all code blocks complete.
- **Risk note:** Task 5 line numbers (~7192/7233) must be re-verified before editing per the grep in its preamble.
