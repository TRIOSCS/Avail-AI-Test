"""test_resell_offers.py — Inbound-offer submission + line MaterialCard resolve.

Covers the additive Resell offer-collection core (spec §"Offer collection"):
- ``ExcessLineItem.material_card_id`` resolved on the import/create path (reusing
  the canonical ``resolve_material_card``; nullable when the MPN won't resolve).
- ``submit_offer`` for both scopes:
    - ``take_all`` → one ExcessOffer (status open), lump price, NO lines.
    - ``per_line`` → ExcessOffer + one ExcessOfferLine per row, matched on part
      number only via ``normalize_mpn_key`` (price never affects matching):
      exactly-one match → matched; none → unmatched (QUEUED, never dropped);
      duplicate posting MPN → ambiguous.
- Guards: self-offer blocked (submitted_by == list.owner_id); non-can_offer blocked.

Called by: pytest
Depends on: app.services.excess_service, app.models.excess, tests.conftest
"""

from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.constants import ExcessListStatus, ExcessOfferStatus
from app.models import Company, MaterialCard, User
from app.models.excess import ExcessLineItem, ExcessList, ExcessOfferLine
from app.services.excess_service import (
    confirm_import,
    create_excess_list,
    import_line_items,
    submit_offer,
)
from app.utils.normalization import normalize_mpn_key
from tests.conftest import engine

_ = engine  # Ensure test DB tables are created


# ---------------------------------------------------------------------------
# Helpers (mirror tests/test_excess_crud.py fixture style)
# ---------------------------------------------------------------------------


def _make_company(db: Session, name: str = "Seller Corp") -> Company:
    co = Company(name=name)
    db.add(co)
    db.commit()
    db.refresh(co)
    return co


def _make_user(db: Session, *, email: str, role: str = "trader") -> User:
    user = User(email=email, name=email.split("@")[0], role=role, azure_id=f"az-{email}")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _make_list_with_lines(db: Session, owner: User, company: Company, parts: list[str]) -> ExcessList:
    """Create an ExcessList with one line per part (via import path so resolve
    fires)."""
    el = create_excess_list(db, title="Excess", company_id=company.id, owner_id=owner.id)
    rows = [{"part_number": p, "quantity": "100"} for p in parts]
    import_line_items(db, el.id, rows)
    return el


# ---------------------------------------------------------------------------
# MaterialCard resolve on line create
# ---------------------------------------------------------------------------


def test_import_resolves_material_card_id(db_session: Session):
    company = _make_company(db_session)
    owner = _make_user(db_session, email="owner@test.com", role="sales")
    el = create_excess_list(db_session, title="L", company_id=company.id, owner_id=owner.id)

    import_line_items(db_session, el.id, [{"part_number": "LM358N", "quantity": "100"}])

    item = db_session.query(ExcessLineItem).filter_by(excess_list_id=el.id).one()
    assert item.material_card_id is not None
    card = db_session.get(MaterialCard, item.material_card_id)
    assert card.normalized_mpn == "lm358n"


def test_confirm_import_resolves_material_card_id(db_session: Session):
    company = _make_company(db_session)
    owner = _make_user(db_session, email="owner2@test.com", role="sales")
    el = create_excess_list(db_session, title="L", company_id=company.id, owner_id=owner.id)

    confirm_import(db_session, el.id, [{"part_number": "MAX232", "quantity": 50}])

    item = db_session.query(ExcessLineItem).filter_by(excess_list_id=el.id).one()
    assert item.material_card_id is not None
    # The line resolves to the ACTUAL MAX232 card (not just some non-null row): confirm the
    # linked card's normalized MPN matches the imported part.
    card = db_session.get(MaterialCard, item.material_card_id)
    assert card is not None
    assert card.normalized_mpn == normalize_mpn_key("MAX232")
    assert item.part_number == "MAX232"


def test_import_unresolvable_mpn_leaves_material_card_null(db_session: Session):
    """A punctuation-only MPN normalizes to empty — card stays null, line still
    imports."""
    company = _make_company(db_session)
    owner = _make_user(db_session, email="owner3@test.com", role="sales")
    el = create_excess_list(db_session, title="L", company_id=company.id, owner_id=owner.id)

    result = import_line_items(db_session, el.id, [{"part_number": "--", "quantity": "10"}])

    assert result["imported"] == 1
    item = db_session.query(ExcessLineItem).filter_by(excess_list_id=el.id).one()
    assert item.material_card_id is None


# ---------------------------------------------------------------------------
# submit_offer — take_all
# ---------------------------------------------------------------------------


def test_take_all_offer_binds_list_no_lines(db_session: Session):
    company = _make_company(db_session)
    owner = _make_user(db_session, email="o@test.com", role="sales")
    offerer = _make_user(db_session, email="b@test.com", role="buyer")
    el = _make_list_with_lines(db_session, owner, company, ["LM358N", "MAX232"])

    offer = submit_offer(
        db_session,
        list_id=el.id,
        user=offerer,
        scope="take_all",
        take_all_total_price=Decimal("9999.00"),
    )

    assert offer.id is not None
    assert offer.scope == "take_all"
    assert offer.status == "open"
    assert offer.take_all_total_price == Decimal("9999.00")
    assert db_session.query(ExcessOfferLine).filter_by(offer_id=offer.id).count() == 0


# ---------------------------------------------------------------------------
# submit_offer — per_line matching (part number only)
# ---------------------------------------------------------------------------


def test_per_line_matches_known_mpn(db_session: Session):
    company = _make_company(db_session)
    owner = _make_user(db_session, email="o2@test.com", role="sales")
    offerer = _make_user(db_session, email="b2@test.com", role="buyer")
    el = _make_list_with_lines(db_session, owner, company, ["LM358N"])
    target = db_session.query(ExcessLineItem).filter_by(excess_list_id=el.id).one()

    offer = submit_offer(
        db_session,
        list_id=el.id,
        user=offerer,
        scope="per_line",
        lines=[{"mpn_raw": "lm-358-n", "quantity": 50, "unit_price": Decimal("1.25")}],
    )

    line = db_session.query(ExcessOfferLine).filter_by(offer_id=offer.id).one()
    assert line.match_status == "matched"
    assert line.excess_line_item_id == target.id
    assert line.unit_price == Decimal("1.25")
    assert line.mpn_raw == "lm-358-n"


def test_per_line_queues_unknown_mpn_as_unmatched(db_session: Session):
    company = _make_company(db_session)
    owner = _make_user(db_session, email="o3@test.com", role="sales")
    offerer = _make_user(db_session, email="b3@test.com", role="buyer")
    el = _make_list_with_lines(db_session, owner, company, ["LM358N"])

    offer = submit_offer(
        db_session,
        list_id=el.id,
        user=offerer,
        scope="per_line",
        lines=[{"mpn_raw": "NOTONTHELIST99", "quantity": 5}],
    )

    line = db_session.query(ExcessOfferLine).filter_by(offer_id=offer.id).one()
    assert line.match_status == "unmatched"
    assert line.excess_line_item_id is None
    # QUEUED, never dropped — raw MPN preserved for manual resolution.
    assert line.mpn_raw == "NOTONTHELIST99"


def test_per_line_flags_duplicate_posting_mpn_as_ambiguous(db_session: Session):
    company = _make_company(db_session)
    owner = _make_user(db_session, email="o4@test.com", role="sales")
    offerer = _make_user(db_session, email="b4@test.com", role="buyer")
    # Two posted lines share the same normalized part number → ambiguous.
    el = _make_list_with_lines(db_session, owner, company, ["LM358N", "LM-358-N"])

    offer = submit_offer(
        db_session,
        list_id=el.id,
        user=offerer,
        scope="per_line",
        lines=[{"mpn_raw": "LM358N", "quantity": 10}],
    )

    line = db_session.query(ExcessOfferLine).filter_by(offer_id=offer.id).one()
    assert line.match_status == "ambiguous"
    assert line.excess_line_item_id is None


def test_per_line_nullable_unit_price_allowed(db_session: Session):
    company = _make_company(db_session)
    owner = _make_user(db_session, email="o5@test.com", role="sales")
    offerer = _make_user(db_session, email="b5@test.com", role="buyer")
    el = _make_list_with_lines(db_session, owner, company, ["LM358N"])

    offer = submit_offer(
        db_session,
        list_id=el.id,
        user=offerer,
        scope="per_line",
        lines=[{"mpn_raw": "LM358N", "quantity": 10}],  # no unit_price
    )

    line = db_session.query(ExcessOfferLine).filter_by(offer_id=offer.id).one()
    assert line.unit_price is None
    assert line.match_status == "matched"


# ---------------------------------------------------------------------------
# submit_offer — guards
# ---------------------------------------------------------------------------


def test_self_offer_blocked(db_session: Session):
    company = _make_company(db_session)
    owner = _make_user(db_session, email="owner-self@test.com", role="trader")
    el = _make_list_with_lines(db_session, owner, company, ["LM358N"])

    with pytest.raises(HTTPException) as exc:
        submit_offer(db_session, list_id=el.id, user=owner, scope="take_all")
    assert exc.value.status_code == 403
    assert "own" in exc.value.detail.lower()


def test_non_can_offer_user_blocked(db_session: Session):
    company = _make_company(db_session)
    owner = _make_user(db_session, email="owner-x@test.com", role="sales")
    sales_offerer = _make_user(db_session, email="sales-offerer@test.com", role="sales")
    el = _make_list_with_lines(db_session, owner, company, ["LM358N"])

    with pytest.raises(HTTPException) as exc:
        submit_offer(db_session, list_id=el.id, user=sales_offerer, scope="take_all")
    assert exc.value.status_code == 403


def test_submit_offer_missing_list_404(db_session: Session):
    offerer = _make_user(db_session, email="b-404@test.com", role="buyer")
    with pytest.raises(HTTPException) as exc:
        submit_offer(db_session, list_id=999999, user=offerer, scope="take_all")
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# COLLECTING status wiring (Chunk D)
# ---------------------------------------------------------------------------


def test_open_list_flips_to_collecting_on_first_offer(db_session: Session):
    """An OPEN list flips to COLLECTING when the first offer lands (take_all)."""
    from app.constants import ExcessListStatus

    company = _make_company(db_session, name="SellerCo-Collecting")
    owner = _make_user(db_session, email="owner-coll@test.com", role="sales")
    offerer = _make_user(db_session, email="buyer-coll@test.com", role="buyer")
    el = _make_list_with_lines(db_session, owner, company, ["LM358N"])

    # Manually flip to OPEN to simulate a published list (DRAFT → OPEN via publish endpoint).
    el.status = ExcessListStatus.OPEN
    db_session.commit()
    db_session.refresh(el)
    assert el.status == ExcessListStatus.OPEN

    submit_offer(db_session, list_id=el.id, user=offerer, scope="take_all")

    db_session.refresh(el)
    assert el.status == ExcessListStatus.COLLECTING


def test_collecting_list_stays_collecting_on_subsequent_offer(db_session: Session):
    """A list already in COLLECTING stays in COLLECTING (idempotent flip)."""
    from app.constants import ExcessListStatus

    company = _make_company(db_session, name="SellerCo-Coll2")
    owner = _make_user(db_session, email="owner-coll2@test.com", role="sales")
    offerer1 = _make_user(db_session, email="buyer-coll2a@test.com", role="buyer")
    offerer2 = _make_user(db_session, email="buyer-coll2b@test.com", role="buyer")
    el = _make_list_with_lines(db_session, owner, company, ["MAX232"])

    el.status = ExcessListStatus.OPEN
    db_session.commit()

    submit_offer(db_session, list_id=el.id, user=offerer1, scope="take_all")
    db_session.refresh(el)
    assert el.status == ExcessListStatus.COLLECTING

    # Second offer — status stays COLLECTING (not re-flipped to something else).
    submit_offer(db_session, list_id=el.id, user=offerer2, scope="take_all")
    db_session.refresh(el)
    assert el.status == ExcessListStatus.COLLECTING


# ---------------------------------------------------------------------------
# Late-offer flagging (M3): an offer landing after the posting window closed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("closed_status", [ExcessListStatus.BID_OUT, ExcessListStatus.AWARDED])
def test_offer_on_closed_list_is_flagged_late(db_session: Session, closed_status):
    """An inbound offer on a bid_out/awarded list is accepted but flagged ``late``
    (queued for review, never dropped) instead of a plain on-time ``open``."""
    company = _make_company(db_session, name=f"SellerCo-Late-{closed_status}")
    owner = _make_user(db_session, email=f"owner-late-{closed_status}@test.com", role="sales")
    offerer = _make_user(db_session, email=f"buyer-late-{closed_status}@test.com", role="buyer")
    el = _make_list_with_lines(db_session, owner, company, ["LM358N"])
    el.status = closed_status
    db_session.commit()

    offer = submit_offer(db_session, list_id=el.id, user=offerer, scope="take_all")

    assert offer.status == ExcessOfferStatus.LATE


@pytest.mark.parametrize("open_status", [ExcessListStatus.OPEN, ExcessListStatus.COLLECTING])
def test_offer_on_open_list_is_on_time(db_session: Session, open_status):
    """An offer while the window is still open (open/collecting) lands ``open``."""
    company = _make_company(db_session, name=f"SellerCo-OnTime-{open_status}")
    owner = _make_user(db_session, email=f"owner-ontime-{open_status}@test.com", role="sales")
    offerer = _make_user(db_session, email=f"buyer-ontime-{open_status}@test.com", role="buyer")
    el = _make_list_with_lines(db_session, owner, company, ["LM358N"])
    el.status = open_status
    db_session.commit()

    offer = submit_offer(db_session, list_id=el.id, user=offerer, scope="take_all")

    assert offer.status == ExcessOfferStatus.OPEN
