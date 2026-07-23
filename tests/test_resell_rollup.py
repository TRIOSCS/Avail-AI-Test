"""test_resell_rollup.py — Best-price rollup across a line's inbound offers.

Covers ``recompute_line_rollup`` / ``withdraw_offer`` (spec §"Offer collection",
"best-price rollup per line"). An inbound offer is a broker bidding to BUY the excess, so
the best bid is the HIGHEST price: best_offer_unit_price = max unit_price across the line's
ExcessOfferLines whose parent offer is active (open/won), best_offer_id = the offer
providing that max, offer_count = distinct offers touching the line. None prices are
ignored; withdrawing an offer recomputes the rollup.

Called by: pytest
Depends on: app.services.excess_service, app.models.excess, tests.conftest
"""

from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.constants import ExcessListStatus, ExcessOfferStatus
from app.models import Company, User
from app.models.excess import ExcessLineItem, ExcessList
from app.services.excess_service import (
    create_excess_list,
    import_line_items,
    recompute_line_rollup,
    submit_offer,
    withdraw_offer,
)
from tests.conftest import engine

_ = engine  # Ensure test DB tables are created


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_company(db: Session, name: str = "Seller Corp") -> Company:
    co = Company(name=name)
    db.add(co)
    db.commit()
    db.refresh(co)
    return co


def _make_user(db: Session, *, email: str, role: str) -> User:
    user = User(email=email, name=email.split("@")[0], role=role, azure_id=f"az-{email}")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@pytest.fixture()
def rollup_fixture(db_session: Session) -> tuple[ExcessList, ExcessLineItem, User, User]:
    """A posted (open) list with one line + an owner and two distinct buyer-offerers.

    Posted, not draft: ``submit_offer`` rejects a non-posted list (finding #47).
    """
    company = _make_company(db_session)
    owner = _make_user(db_session, email="owner@roll.com", role="sales")
    buyer_a = _make_user(db_session, email="a@roll.com", role="buyer")
    buyer_b = _make_user(db_session, email="b@roll.com", role="buyer")
    el = create_excess_list(db_session, title="Roll", company_id=company.id, owner_id=owner.id)
    import_line_items(db_session, el.id, [{"part_number": "LM358N", "quantity": "100"}])
    el.status = ExcessListStatus.OPEN
    db_session.commit()
    line = db_session.query(ExcessLineItem).filter_by(excess_list_id=el.id).one()
    return el, line, buyer_a, buyer_b


def _offer(db, el, user, price):
    return submit_offer(
        db,
        list_id=el.id,
        user=user,
        scope="per_line",
        lines=[{"mpn_raw": "LM358N", "quantity": 10, "unit_price": price}],
    )


# ---------------------------------------------------------------------------
# Rollup behaviour
# ---------------------------------------------------------------------------


def test_rollup_picks_max_price_and_counts_offers(db_session: Session, rollup_fixture):
    el, line, buyer_a, buyer_b = rollup_fixture

    _offer(db_session, el, buyer_a, Decimal("1.50"))
    highest = _offer(db_session, el, buyer_b, Decimal("2.00"))

    db_session.refresh(line)
    # Buy-side auction: the HIGHEST bid is the best (most money for the excess).
    assert line.best_offer_unit_price == Decimal("2.00")
    assert line.best_offer_id == highest.id
    assert line.offer_count == 2


def test_rollup_picks_highest_of_three_and_ignores_null(db_session: Session, rollup_fixture):
    el, line, buyer_a, buyer_b = rollup_fixture
    buyer_c = _make_user(db_session, email="c@roll.com", role="buyer")
    buyer_d = _make_user(db_session, email="d@roll.com", role="buyer")

    _offer(db_session, el, buyer_a, Decimal("1.00"))
    top = _offer(db_session, el, buyer_b, Decimal("3.50"))
    _offer(db_session, el, buyer_c, Decimal("2.25"))
    # A fourth, unpriced bid must be ignored for price selection — and never crash max().
    submit_offer(
        db_session,
        list_id=el.id,
        user=buyer_d,
        scope="per_line",
        lines=[{"mpn_raw": "LM358N", "quantity": 10}],  # no unit_price
    )

    db_session.refresh(line)
    assert line.best_offer_unit_price == Decimal("3.50")
    assert line.best_offer_id == top.id
    assert line.offer_count == 4  # all four offers touch the line


def test_rollup_ignores_null_prices(db_session: Session, rollup_fixture):
    el, line, buyer_a, buyer_b = rollup_fixture

    # Priced offer + an unpriced ("price TBD") offer.
    priced = _offer(db_session, el, buyer_a, Decimal("3.00"))
    submit_offer(
        db_session,
        list_id=el.id,
        user=buyer_b,
        scope="per_line",
        lines=[{"mpn_raw": "LM358N", "quantity": 10}],  # no unit_price
    )

    db_session.refresh(line)
    # Both offers touch the line (count=2) but only the priced one drives best price.
    assert line.offer_count == 2
    assert line.best_offer_unit_price == Decimal("3.00")
    assert line.best_offer_id == priced.id


def test_rollup_all_null_leaves_best_price_none(db_session: Session, rollup_fixture):
    el, line, buyer_a, _ = rollup_fixture

    submit_offer(
        db_session,
        list_id=el.id,
        user=buyer_a,
        scope="per_line",
        lines=[{"mpn_raw": "LM358N", "quantity": 10}],  # no unit_price
    )

    db_session.refresh(line)
    assert line.offer_count == 1
    assert line.best_offer_unit_price is None
    assert line.best_offer_id is None


def test_rollup_recomputes_after_withdraw(db_session: Session, rollup_fixture):
    el, line, buyer_a, buyer_b = rollup_fixture

    lower = _offer(db_session, el, buyer_a, Decimal("1.50"))
    higher = _offer(db_session, el, buyer_b, Decimal("2.00"))

    db_session.refresh(line)
    # Best = the highest bid.
    assert line.best_offer_id == higher.id
    assert line.offer_count == 2

    withdraw_offer(db_session, higher.id)

    db_session.refresh(line)
    # The withdrawn (best) offer drops out — next-best (highest remaining) wins, count falls.
    assert line.best_offer_unit_price == Decimal("1.50")
    assert line.best_offer_id == lower.id
    assert line.offer_count == 1


def test_rollup_zero_when_last_offer_withdrawn(db_session: Session, rollup_fixture):
    el, line, buyer_a, _ = rollup_fixture

    only = _offer(db_session, el, buyer_a, Decimal("2.00"))
    withdraw_offer(db_session, only.id)

    db_session.refresh(line)
    assert line.offer_count == 0
    assert line.best_offer_unit_price is None
    assert line.best_offer_id is None


def test_rollup_includes_late_offer(db_session: Session, rollup_fixture):
    """A LATE offer (landed after the window closed) still drives the line rollup.

    A late bid is counted in the stat strip (unactioned = open/late), shown in the
    Offers tab, and awardable — so the rollup (offer_count / best_offer_id /
    best_offer_unit_price) must include it too. Otherwise the line card reads 0-covered
    while the strip says there's an offer to review, and the late bid can never be
    marked "Best".
    """
    el, line, buyer_a, _ = rollup_fixture
    # Close the posting window so the next inbound offer lands LATE (not OPEN).
    el.status = ExcessListStatus.BID_OUT
    db_session.commit()

    late = _offer(db_session, el, buyer_a, Decimal("2.00"))
    db_session.refresh(late)
    assert late.status == ExcessOfferStatus.LATE

    db_session.refresh(line)
    assert line.offer_count == 1
    assert line.best_offer_unit_price == Decimal("2.00")
    assert line.best_offer_id == late.id


def test_rollup_higher_late_beats_lower_open(db_session: Session, rollup_fixture):
    """A higher LATE bid beats a lower OPEN bid for "Best" — LATE and OPEN rank
    together.

    The owner must not be steered to a lower on-time bid when a higher late bid exists;
    both live states feed the same best-price selection.
    """
    el, line, buyer_a, buyer_b = rollup_fixture
    # An on-time (open) lower bid first.
    _offer(db_session, el, buyer_a, Decimal("1.50"))
    # Close the window, then a higher LATE bid.
    el.status = ExcessListStatus.BID_OUT
    db_session.commit()
    higher_late = _offer(db_session, el, buyer_b, Decimal("2.75"))
    db_session.refresh(higher_late)
    assert higher_late.status == ExcessOfferStatus.LATE

    db_session.refresh(line)
    assert line.offer_count == 2
    assert line.best_offer_unit_price == Decimal("2.75")
    assert line.best_offer_id == higher_late.id


def test_recompute_missing_line_is_noop(db_session: Session):
    # Must not raise on an unknown line id.
    recompute_line_rollup(db_session, 999999)
