"""test_resell_mirror.py — Sighting live-mirror + virtual requirement (Chunk C).

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

from datetime import UTC, datetime

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.constants import ExcessLineItemStatus, ExcessListStatus
from app.models import Company, User
from app.models.excess import ExcessLineItem, ExcessList
from app.models.sourcing import Requirement, Requisition, Sighting
from app.services.excess_mirror import (
    _virtual_req_name,
    ensure_virtual_requirement,
    mirror_line,
    publish_list,
    retire_line,
    sync_list_mirror,
    teardown_list_mirror,
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


def test_same_company_same_part_two_lists_no_collapse(db_session: Session):
    """SAME company, SAME part on TWO different published lists → TWO distinct
    Sightings.

    Fix: upsert key now includes requirement_id (one per list's virtual requirement)
    so a second list with the same (company, material_card) does NOT overwrite or
    collapse the first list's Sighting. Each list's qty is preserved independently.
    """
    company = _make_company(db_session)
    owner = _make_user(db_session)
    # Two lists for the SAME company with the SAME part number.
    el1 = _make_list_with_lines(db_session, owner, company, ["LM358N"])
    el2 = _make_list_with_lines(db_session, owner, company, ["LM358N"])

    # Give them different quantities so we can tell them apart.
    _lines(db_session, el1)[0].quantity = 50
    _lines(db_session, el2)[0].quantity = 75
    db_session.commit()

    sync_list_mirror(db_session, el1)
    sync_list_mirror(db_session, el2)
    db_session.commit()

    rows = _customer_excess_sightings(db_session, company.id)
    # Must be TWO rows — one per list's virtual requirement — not one collapsed row.
    assert len(rows) == 2, f"Expected 2 sightings (one per list), got {len(rows)}"
    qtys = {r.qty_available for r in rows}
    assert qtys == {50, 75}, f"Expected qtys {{50, 75}}, got {qtys}"
    # Each row must be on a DIFFERENT virtual requirement.
    req_ids = {r.requirement_id for r in rows}
    assert len(req_ids) == 2, "Both sightings share the same requirement_id — upsert key is still too broad"


# ---------------------------------------------------------------------------
# Within-list line identity (finding #18) — duplicate-part lines each keep a
# distinct Sighting, keyed on excess_line_item_id (migration 199).
# ---------------------------------------------------------------------------


def _dup_part_list(db_session: Session, company: Company, owner: User) -> ExcessList:
    """A single list with TWO lines for the SAME part (same material_card), distinct
    qty."""
    el = create_excess_list(db_session, title="Excess", company_id=company.id, owner_id=owner.id)
    import_line_items(
        db_session,
        el.id,
        [
            {"part_number": "LM358N", "quantity": "50"},
            {"part_number": "LM358N", "quantity": "75"},
        ],
    )
    return el


def test_within_list_two_lines_same_part_two_sightings(db_session: Session):
    """Two lines on ONE list with the SAME part/material_card but distinct qty → TWO
    distinct Sightings (was: collapsed into one row, hiding live supply).

    Finding #18.
    """
    company = _make_company(db_session)
    owner = _make_user(db_session)
    el = _dup_part_list(db_session, company, owner)
    lines = _lines(db_session, el)
    assert len(lines) == 2
    assert lines[0].material_card_id == lines[1].material_card_id  # same card — the trap

    sync_list_mirror(db_session, el)
    db_session.commit()

    rows = _customer_excess_sightings(db_session, company.id)
    assert len(rows) == 2, f"Expected 2 sightings (one per line), got {len(rows)}"
    assert {r.qty_available for r in rows} == {50, 75}
    # Each Sighting is keyed to its OWN line.
    assert {r.excess_line_item_id for r in rows} == {lines[0].id, lines[1].id}


def test_resync_dup_part_lines_update_not_duplicate(db_session: Session):
    """Re-syncing duplicate-part lines UPDATES each line's own Sighting (still 2
    rows)."""
    company = _make_company(db_session)
    owner = _make_user(db_session)
    el = _dup_part_list(db_session, company, owner)
    sync_list_mirror(db_session, el)
    db_session.commit()
    assert len(_customer_excess_sightings(db_session, company.id)) == 2

    lines = _lines(db_session, el)
    lines[0].quantity = 111
    db_session.commit()
    sync_list_mirror(db_session, el)
    db_session.commit()

    rows = _customer_excess_sightings(db_session, company.id)
    assert len(rows) == 2  # updated in place, not duplicated
    assert {r.qty_available for r in rows} == {111, 75}


def test_retire_one_twin_leaves_sibling_sighting(db_session: Session):
    """Retiring ONE duplicate-part line deletes ONLY its own Sighting; the twin survives
    (was: shared-row deletion wiped both).

    Finding #18.
    """
    company = _make_company(db_session)
    owner = _make_user(db_session)
    el = _dup_part_list(db_session, company, owner)
    sync_list_mirror(db_session, el)
    db_session.commit()
    assert len(_customer_excess_sightings(db_session, company.id)) == 2

    first, second = _lines(db_session, el)
    retire_line(db_session, first)
    db_session.commit()

    rows = _customer_excess_sightings(db_session, company.id)
    assert len(rows) == 1
    assert rows[0].excess_line_item_id == second.id
    assert rows[0].qty_available == 75


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
# teardown_list_mirror — list/company DELETION (P2 strand fix)
# ---------------------------------------------------------------------------


def _virtual_req(db: Session, el: ExcessList) -> Requisition | None:
    return (
        db.query(Requisition)
        .filter(Requisition.is_scratch.is_(True), Requisition.name == _virtual_req_name(el))
        .one_or_none()
    )


def test_teardown_deletes_mirror_and_virtual_req(db_session: Session):
    """Tearing down a published list's mirror DELETES its customer_excess Sightings AND
    the virtual scratch Requisition/Requirement (no orphan advertising live supply)."""
    company = _make_company(db_session)
    owner = _make_user(db_session)
    el = _make_list_with_lines(db_session, owner, company, ["LM358N", "MAX232"])
    publish_list(db_session, el.id, owner)
    assert len(_customer_excess_sightings(db_session, company.id)) == 2
    req = _virtual_req(db_session, el)
    assert req is not None
    req_id = req.id  # capture before teardown deletes + expires the row

    teardown_list_mirror(db_session, el)
    db_session.commit()

    assert _customer_excess_sightings(db_session, company.id) == []
    assert _virtual_req(db_session, el) is None
    assert db_session.query(Requirement).filter(Requirement.requisition_id == req_id).count() == 0


def test_teardown_scoped_to_one_list(db_session: Session):
    """Teardown of ONE list leaves a SIBLING list's mirror + virtual req intact (each
    list owns a distinct virtual req — never wipe by company)."""
    company = _make_company(db_session)
    owner = _make_user(db_session)
    el_a = _make_list_with_lines(db_session, owner, company, ["LM358N"])
    el_b = _make_list_with_lines(db_session, owner, company, ["MAX232"])
    publish_list(db_session, el_a.id, owner)
    publish_list(db_session, el_b.id, owner)
    assert len(_customer_excess_sightings(db_session, company.id)) == 2

    teardown_list_mirror(db_session, el_a)
    db_session.commit()

    # Only B's sighting + virtual req survive.
    rows = _customer_excess_sightings(db_session, company.id)
    assert len(rows) == 1
    assert _virtual_req(db_session, el_a) is None
    assert _virtual_req(db_session, el_b) is not None


def test_teardown_robust_to_null_material_card(db_session: Session):
    """Teardown deletes a mirror Sighting even when material_card_id/source_company_id
    are NULL (retire_line, which keys on the card, could not) — it keys on the virtual
    req id."""
    company = _make_company(db_session)
    owner = _make_user(db_session)
    el = _make_list_with_lines(db_session, owner, company, ["LM358N"])
    publish_list(db_session, el.id, owner)
    # Simulate a mirror row whose card + company links were later NULLed (SET NULL cascades).
    row = _customer_excess_sightings(db_session, company.id)[0]
    row.material_card_id = None
    row.source_company_id = None
    db_session.commit()

    teardown_list_mirror(db_session, el)
    db_session.commit()

    assert _virtual_req(db_session, el) is None
    assert db_session.query(Sighting).filter(Sighting.source_type == "customer_excess").count() == 0


def test_teardown_noop_on_unpublished_draft(db_session: Session):
    """A draft never mirrored → teardown is an idempotent no-op (no virtual req to
    remove)."""
    company = _make_company(db_session)
    owner = _make_user(db_session)
    el = _make_list_with_lines(db_session, owner, company, ["LM358N"])

    result = teardown_list_mirror(db_session, el)
    db_session.commit()

    assert result["sightings"] == 0
    assert result["requisitions"] == 0
    # A second call is still a clean no-op.
    assert teardown_list_mirror(db_session, el)["requisitions"] == 0


def test_delete_excess_list_leaves_no_mirror(db_session: Session):
    """delete_excess_list runs teardown (no-op for a draft) then deletes — no orphan
    req."""
    from app.services.excess_service import delete_excess_list

    company = _make_company(db_session)
    owner = _make_user(db_session)
    el = _make_list_with_lines(db_session, owner, company, ["LM358N"])
    el_id = el.id

    delete_excess_list(db_session, el_id, owner)

    assert db_session.get(ExcessList, el_id) is None
    assert db_session.query(Sighting).filter(Sighting.source_type == "customer_excess").count() == 0


def test_seed_resell_demo_reset_tears_down_mirror(db_session: Session):
    """`seed_resell_demo --reset` deletes every demo list's mirror — zero
    customer_excess Sightings + no leftover virtual scratch Requisition after a
    seed→reset cycle."""
    from app.management import seed_resell_demo

    seed_resell_demo.seed(db_session)
    # The demo builds mirrored postings, so customer_excess sightings exist post-seed.
    assert db_session.query(Sighting).filter(Sighting.source_type == "customer_excess").count() > 0

    seed_resell_demo._reset(db_session)

    assert db_session.query(Sighting).filter(Sighting.source_type == "customer_excess").count() == 0
    leftover = (
        db_session.query(Requisition)
        .filter(Requisition.is_scratch.is_(True), Requisition.name.like("Customer Excess (list %"))
        .count()
    )
    assert leftover == 0


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


def test_resync_no_second_virtual_req(db_session: Session):
    """Re-syncing an already-published list's mirror is idempotent — one virtual req,
    one Sighting (``ensure_virtual_requirement`` get-or-creates).

    Split out of the old publish-twice test now that a second publish is a 409 (see the
    publish-guard pack).
    """
    company = _make_company(db_session)
    owner = _make_user(db_session)
    el = _make_list_with_lines(db_session, owner, company, ["LM358N"])

    publish_list(db_session, el.id, owner)
    sync_list_mirror(db_session, el)
    db_session.commit()

    assert db_session.query(Requisition).filter(Requisition.is_scratch.is_(True)).count() == 1
    assert len(_customer_excess_sightings(db_session, company.id)) == 1


# ---------------------------------------------------------------------------
# publish_list guard — 409 unless DRAFT + clear stale close_at
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status",
    [
        ExcessListStatus.OPEN,
        ExcessListStatus.COLLECTING,
        ExcessListStatus.BID_OUT,
        ExcessListStatus.AWARDED,
        ExcessListStatus.CLOSED,
        ExcessListStatus.EXPIRED,
    ],
)
def test_publish_rejects_non_draft(db_session: Session, status):
    """Publishing anything but a DRAFT is a 409 (a re-publish would reopen a resolved
    posting and re-mirror sold-through supply) — status is left untouched."""
    company = _make_company(db_session)
    owner = _make_user(db_session)
    el = _make_list_with_lines(db_session, owner, company, ["LM358N"])
    el.status = status
    db_session.commit()

    with pytest.raises(HTTPException) as exc:
        publish_list(db_session, el.id, owner)

    assert exc.value.status_code == 409
    db_session.refresh(el)
    assert el.status == status  # no mutation on the rejected publish


def test_publish_preserves_future_close_at(db_session: Session):
    """A draft carrying a FUTURE ``close_at`` keeps it on publish (T1/#8 fix) — the
    posting deadline the owner set at create must survive so the nightly expiry backstop
    has a real window to act on.

    (Was: publish unconditionally nulled close_at.)
    """
    from datetime import timedelta

    company = _make_company(db_session)
    owner = _make_user(db_session)
    el = _make_list_with_lines(db_session, owner, company, ["LM358N"])
    future = datetime.now(UTC) + timedelta(days=4)
    el.close_at = future
    db_session.commit()

    publish_list(db_session, el.id, owner)

    db_session.refresh(el)
    assert el.status == ExcessListStatus.OPEN
    stored = el.close_at if el.close_at.tzinfo else el.close_at.replace(tzinfo=UTC)
    assert abs((stored - future).total_seconds()) < 2
    assert el.open_at is not None


def test_publish_nulls_stale_close_at(db_session: Session):
    """A draft that somehow carries a STALE (past) ``close_at`` gets it cleared on
    publish — an open posting must not advertise a lapsed close time."""
    from datetime import timedelta

    company = _make_company(db_session)
    owner = _make_user(db_session)
    el = _make_list_with_lines(db_session, owner, company, ["LM358N"])
    el.close_at = datetime.now(UTC) - timedelta(hours=1)
    db_session.commit()

    publish_list(db_session, el.id, owner)

    db_session.refresh(el)
    assert el.status == ExcessListStatus.OPEN
    assert el.close_at is None
    assert el.open_at is not None


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
