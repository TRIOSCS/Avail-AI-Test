"""test_trading_mirror.py — Sighting live-mirror + virtual requirement (Chunk C).

Covers the additive Sighting live-mirror (spec §"Sighting live-mirror"): each posted
``ExcessLineItem`` mirrors into a ``Sighting`` so the existing matcher sees it for free,
via a single dual-write owner method. The hard correctness points:

- ``ensure_virtual_requirement`` get-or-creates ONE system-owned (``is_scratch``)
  "Customer Excess" Requisition + Requirement per ExcessList so the mirrored
  ``Sighting.requirement_id`` (NOT NULL) is satisfied — idempotent (publishing twice
  does NOT create a second virtual req).
- ``mirror_line`` writes a Sighting with the EXACT contract fields: ``source_type=
  'customer_excess'``, ``source_company_id = list.company_id``, ``requirement_id`` =
  virtual req, synthesized internal ``vendor_name`` (NOT the customer name),
  ``normalized_mpn`` via ``normalize_mpn_key``, ``material_card_id`` from the line.
- Upsert key ``(source_company_id, material_card_id)`` — a re-sync with a changed qty
  UPDATES the existing Sighting rather than tripping the connector-aware delete that
  wipes sibling ``customer_excess`` rows (the dedup trap).
- ``retire_line`` removes/deactivates a line's mirrored Sighting on award / withdraw /
  qty→0.
- ``publish_list`` flips the list to ``open`` (+ ``open_at`` semantics) then syncs the
  whole list mirror.

Called by: pytest
Depends on: app.services.excess_mirror, app.services.excess_service, app.models, conftest
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.constants import ExcessLineItemStatus, ExcessListStatus
from app.models import Company, User
from app.models.excess import ExcessLineItem, ExcessList
from app.models.sourcing import Requirement, Requisition, Sighting
from app.services.excess_mirror import (
    ensure_virtual_requirement,
    mirror_line,
    publish_list,
    retire_line,
    sync_list_mirror,
)
from app.services.excess_service import create_excess_list, import_line_items
from app.utils.normalization import normalize_mpn_key
from tests.conftest import engine

_ = engine  # Ensure test DB tables are created


# ---------------------------------------------------------------------------
# Helpers (mirror tests/test_trading_offers.py fixture style)
# ---------------------------------------------------------------------------


def _make_company(db: Session, name: str = "Seller Corp") -> Company:
    co = Company(name=name)
    db.add(co)
    db.commit()
    db.refresh(co)
    return co


def _make_user(db: Session, *, email: str = "owner@test.com", role: str = "trader") -> User:
    user = User(email=email, name=email.split("@")[0], role=role, azure_id=f"az-{email}")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _make_list_with_lines(db: Session, owner: User, company: Company, parts: list[str]) -> ExcessList:
    """Create an ExcessList with one line per part (via import path so MC resolve
    fires)."""
    el = create_excess_list(db, title="Excess", company_id=company.id, owner_id=owner.id)
    rows = [{"part_number": p, "quantity": "100"} for p in parts]
    import_line_items(db, el.id, rows)
    return el


def _lines(db: Session, el: ExcessList) -> list[ExcessLineItem]:
    return db.query(ExcessLineItem).filter_by(excess_list_id=el.id).order_by(ExcessLineItem.id).all()


def _customer_excess_sightings(db: Session, company_id: int) -> list[Sighting]:
    return (
        db.query(Sighting)
        .filter(Sighting.source_type == "customer_excess", Sighting.source_company_id == company_id)
        .all()
    )


# ---------------------------------------------------------------------------
# Virtual requirement modelling
# ---------------------------------------------------------------------------


def test_ensure_virtual_requirement_creates_system_owned_req(db_session: Session):
    company = _make_company(db_session)
    owner = _make_user(db_session)
    el = _make_list_with_lines(db_session, owner, company, ["LM358N"])

    req = ensure_virtual_requirement(db_session, el)

    assert isinstance(req, Requirement)
    requisition = db_session.get(Requisition, req.requisition_id)
    # System-owned via the established is_scratch marker (hidden from sales views).
    assert requisition.is_scratch is True


def test_ensure_virtual_requirement_idempotent(db_session: Session):
    company = _make_company(db_session)
    owner = _make_user(db_session)
    el = _make_list_with_lines(db_session, owner, company, ["LM358N"])

    req1 = ensure_virtual_requirement(db_session, el)
    req2 = ensure_virtual_requirement(db_session, el)

    assert req1.id == req2.id
    # Exactly one virtual requisition + requirement for the list.
    assert db_session.query(Requisition).filter(Requisition.is_scratch.is_(True)).count() == 1
    assert db_session.query(Requirement).filter(Requirement.requisition_id == req1.requisition_id).count() == 1


def test_virtual_requirement_excluded_from_normal_sales_views(db_session: Session):
    """The virtual requisition must NOT appear in the non-scratch sales view query."""
    company = _make_company(db_session)
    owner = _make_user(db_session)
    el = _make_list_with_lines(db_session, owner, company, ["LM358N"])
    ensure_virtual_requirement(db_session, el)

    visible = db_session.query(Requisition).filter(Requisition.is_scratch.is_(False)).all()
    assert visible == []


# ---------------------------------------------------------------------------
# mirror_line — exact field contract
# ---------------------------------------------------------------------------


def test_mirror_line_sets_exact_contract_fields(db_session: Session):
    company = _make_company(db_session)
    owner = _make_user(db_session)
    el = _make_list_with_lines(db_session, owner, company, ["LM358N"])
    req = ensure_virtual_requirement(db_session, el)
    line = _lines(db_session, el)[0]

    sighting = mirror_line(db_session, line)
    db_session.commit()

    assert sighting.source_type == "customer_excess"
    assert sighting.source_company_id == company.id
    assert sighting.requirement_id == req.id
    assert sighting.normalized_mpn == normalize_mpn_key(line.part_number)
    assert sighting.material_card_id == line.material_card_id
    assert sighting.material_card_id is not None
    assert sighting.qty_available == line.quantity
    assert sighting.condition == line.condition
    # Synthesized internal label — NOT the customer/company name.
    assert sighting.vendor_name and sighting.vendor_name != company.name


# ---------------------------------------------------------------------------
# Upsert by (source_company_id, material_card_id) — the dedup trap
# ---------------------------------------------------------------------------


def test_resync_updates_existing_sighting_not_duplicate(db_session: Session):
    """Changing a line's qty then re-syncing UPDATES the existing Sighting."""
    company = _make_company(db_session)
    owner = _make_user(db_session)
    el = _make_list_with_lines(db_session, owner, company, ["LM358N"])

    sync_list_mirror(db_session, el)
    db_session.commit()
    rows = _customer_excess_sightings(db_session, company.id)
    assert len(rows) == 1
    assert rows[0].qty_available == 100

    line = _lines(db_session, el)[0]
    line.quantity = 42
    db_session.commit()

    sync_list_mirror(db_session, el)
    db_session.commit()

    rows = _customer_excess_sightings(db_session, company.id)
    assert len(rows) == 1  # updated, not duplicated
    assert rows[0].qty_available == 42


def test_resync_does_not_wipe_sibling_customer_excess_sightings(db_session: Session):
    """Re-publishing one list must NOT wipe a SIBLING list's customer_excess sightings.

    The dedup trap: ``_save_sightings`` deletes by (requirement_id, source_type).
    Because every list has its OWN virtual requirement AND we upsert by
    (source_company_id, material_card_id), re-syncing list A leaves list B's rows intact.
    """
    company_a = _make_company(db_session, name="Seller A")
    company_b = _make_company(db_session, name="Seller B")
    owner = _make_user(db_session)
    el_a = _make_list_with_lines(db_session, owner, company_a, ["LM358N"])
    el_b = _make_list_with_lines(db_session, owner, company_b, ["MAX232"])

    sync_list_mirror(db_session, el_a)
    sync_list_mirror(db_session, el_b)
    db_session.commit()
    assert len(_customer_excess_sightings(db_session, company_a.id)) == 1
    assert len(_customer_excess_sightings(db_session, company_b.id)) == 1

    # Re-sync A — B's sibling row must survive.
    sync_list_mirror(db_session, el_a)
    db_session.commit()

    assert len(_customer_excess_sightings(db_session, company_a.id)) == 1
    assert len(_customer_excess_sightings(db_session, company_b.id)) == 1


def test_same_company_two_lists_distinct_material_cards_coexist(db_session: Session):
    """Two lists for the SAME company with DIFFERENT parts each keep their sighting."""
    company = _make_company(db_session)
    owner = _make_user(db_session)
    el1 = _make_list_with_lines(db_session, owner, company, ["LM358N"])
    el2 = _make_list_with_lines(db_session, owner, company, ["MAX232"])

    sync_list_mirror(db_session, el1)
    sync_list_mirror(db_session, el2)
    db_session.commit()

    rows = _customer_excess_sightings(db_session, company.id)
    assert len(rows) == 2
    cards = {r.material_card_id for r in rows}
    assert len(cards) == 2


# ---------------------------------------------------------------------------
# retire_line — award / withdraw / qty→0
# ---------------------------------------------------------------------------


def test_retire_line_removes_mirrored_sighting(db_session: Session):
    company = _make_company(db_session)
    owner = _make_user(db_session)
    el = _make_list_with_lines(db_session, owner, company, ["LM358N"])

    sync_list_mirror(db_session, el)
    db_session.commit()
    assert len(_customer_excess_sightings(db_session, company.id)) == 1

    line = _lines(db_session, el)[0]
    retire_line(db_session, line)
    db_session.commit()

    assert _customer_excess_sightings(db_session, company.id) == []


def test_sync_retires_awarded_line(db_session: Session):
    """A line awarded/withdrawn after a publish drops out of the mirror on re-sync."""
    company = _make_company(db_session)
    owner = _make_user(db_session)
    el = _make_list_with_lines(db_session, owner, company, ["LM358N", "MAX232"])

    sync_list_mirror(db_session, el)
    db_session.commit()
    assert len(_customer_excess_sightings(db_session, company.id)) == 2

    line = _lines(db_session, el)[0]
    line.status = ExcessLineItemStatus.AWARDED
    db_session.commit()

    sync_list_mirror(db_session, el)
    db_session.commit()

    rows = _customer_excess_sightings(db_session, company.id)
    assert len(rows) == 1
    surviving_line = _lines(db_session, el)[1]
    assert rows[0].material_card_id == surviving_line.material_card_id


# ---------------------------------------------------------------------------
# publish_list — the testable entry point
# ---------------------------------------------------------------------------


def test_publish_list_opens_and_mirrors(db_session: Session):
    company = _make_company(db_session)
    owner = _make_user(db_session)
    el = _make_list_with_lines(db_session, owner, company, ["LM358N", "MAX232"])
    assert el.status == ExcessListStatus.DRAFT

    publish_list(db_session, el.id, owner)

    db_session.refresh(el)
    assert el.status == ExcessListStatus.OPEN
    rows = _customer_excess_sightings(db_session, company.id)
    assert len(rows) == 2  # one per line


def test_publish_twice_no_second_virtual_req(db_session: Session):
    company = _make_company(db_session)
    owner = _make_user(db_session)
    el = _make_list_with_lines(db_session, owner, company, ["LM358N"])

    publish_list(db_session, el.id, owner)
    publish_list(db_session, el.id, owner)

    assert db_session.query(Requisition).filter(Requisition.is_scratch.is_(True)).count() == 1
    assert len(_customer_excess_sightings(db_session, company.id)) == 1


def test_unresolvable_part_skips_mirror(db_session: Session):
    """A line whose MPN won't resolve to a MaterialCard cannot be upserted by card key
    and is skipped (never raises)."""
    company = _make_company(db_session)
    owner = _make_user(db_session)
    el = create_excess_list(db_session, title="L", company_id=company.id, owner_id=owner.id)
    # An all-punctuation MPN yields an empty normalize_mpn_key → resolve_material_card
    # returns None → no card → cannot be upserted by the (company, card) key.
    line = ExcessLineItem(excess_list_id=el.id, part_number="!!!", quantity=5)
    db_session.add(line)
    db_session.commit()
    assert line.material_card_id is None

    result = mirror_line(db_session, line)
    db_session.commit()

    assert result is None
    assert _customer_excess_sightings(db_session, company.id) == []
