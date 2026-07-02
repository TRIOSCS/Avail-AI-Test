"""test_resell_award.py — the Resell award path (offer → won) + the buyer-score hook.

Awarding an inbound ExcessOffer is the SINGLE chokepoint where an offer flips to
``won``. The award MUST recompute the winning buyer's scorecard (the
``recompute_buyer_score_on_win`` hook) inside the same transaction, so a won offer is
always reflected in that buyer's BuyerScore rollup. These tests cover:

  - ``excess_service.award_offer`` — flips the offer to ``won``, marks its matched
    lines ``awarded``, recomputes the line rollups, and upserts the buyer's BuyerScore
    (the orphaned win-hook, now wired). Owner-gated (403) and 404 on a missing offer.
  - the None-safe case: an offer with NO canonical buyer card still wins, no BuyerScore.
  - the POST award endpoint end-to-end: owner awards → 200 + offer won + score; a
    non-owner is forbidden (403).

Called by: pytest
Depends on: app.services.excess_service, app.routers.resell, tests.conftest
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.constants import ExcessLineItemStatus, ExcessListStatus, ExcessOfferStatus, OfferLineMatchStatus
from app.models import Company, User, VendorCard
from app.models.excess import (
    BuyerScore,
    ExcessLineItem,
    ExcessList,
    ExcessOffer,
    ExcessOfferLine,
)
from app.models.intelligence import MaterialCard
from app.models.sourcing import Sighting
from app.services import excess_service
from app.services.excess_mirror import publish_list
from tests.conftest import engine

_ = engine


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture()
def owner(db_session: Session) -> User:
    """The list owner — the trader who awards offers on their own list."""
    u = User(email="award-owner@trioscs.com", name="Award Owner", role="trader", azure_id="award-owner-001")
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


@pytest.fixture()
def broker(db_session: Session) -> User:
    """The offerer — a buyer who submitted the inbound offer (submitted_by)."""
    u = User(email="award-broker@trioscs.com", name="Award Broker", role="buyer", azure_id="award-broker-001")
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


@pytest.fixture()
def outsider(db_session: Session) -> User:
    """A teammate who does NOT own the list — must not be able to award its offers."""
    u = User(email="award-outsider@trioscs.com", name="Award Outsider", role="trader", azure_id="award-outsider-001")
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


@pytest.fixture()
def seller_company(db_session: Session) -> Company:
    co = Company(name="Award Seller Co")
    db_session.add(co)
    db_session.commit()
    db_session.refresh(co)
    return co


@pytest.fixture()
def excess_list(db_session: Session, seller_company: Company, owner: User) -> ExcessList:
    el = ExcessList(company_id=seller_company.id, owner_id=owner.id, title="Award Excess")
    db_session.add(el)
    db_session.commit()
    db_session.refresh(el)
    return el


@pytest.fixture()
def cap_line(db_session: Session, excess_list: ExcessList) -> ExcessLineItem:
    mc = MaterialCard(normalized_mpn="grm188r", display_mpn="GRM188R", category="capacitors")
    db_session.add(mc)
    db_session.flush()
    li = ExcessLineItem(
        excess_list_id=excess_list.id,
        part_number="GRM188R",
        quantity=1000,
        material_card_id=mc.id,
        asking_price=Decimal("1.00"),
    )
    db_session.add(li)
    db_session.commit()
    db_session.refresh(li)
    return li


def _buyer_card(db: Session, name: str) -> VendorCard:
    vc = VendorCard(normalized_name=name.lower(), display_name=name)
    db.add(vc)
    db.flush()
    return vc


def _open_offer(
    db: Session,
    *,
    excess_list: ExcessList,
    submitter: User,
    line: ExcessLineItem,
    buyer: VendorCard | None,
    unit_price: Decimal,
) -> ExcessOffer:
    """An OPEN per-line ExcessOffer carrying one matched line — ready to be awarded."""
    offer = ExcessOffer(
        excess_list_id=excess_list.id,
        submitted_by=submitter.id,
        offerer_vendor_card_id=buyer.id if buyer else None,
        scope="per_line",
        status=ExcessOfferStatus.OPEN,
    )
    db.add(offer)
    db.flush()
    db.add(
        ExcessOfferLine(
            offer_id=offer.id,
            excess_line_item_id=line.id,
            mpn_raw=line.part_number,
            quantity=line.quantity,
            unit_price=unit_price,
            match_status=OfferLineMatchStatus.MATCHED,
        )
    )
    db.flush()
    return offer


def _take_all_offer(
    db: Session,
    *,
    excess_list: ExcessList,
    submitter: User,
    buyer: VendorCard | None = None,
    total_price: Decimal | None = Decimal("500.00"),
) -> ExcessOffer:
    """An OPEN take_all ExcessOffer — binds the whole list, carries NO offer lines."""
    offer = ExcessOffer(
        excess_list_id=excess_list.id,
        submitted_by=submitter.id,
        offerer_vendor_card_id=buyer.id if buyer else None,
        scope="take_all",
        status=ExcessOfferStatus.OPEN,
        take_all_total_price=total_price,
    )
    db.add(offer)
    db.flush()
    return offer


def _line(
    db: Session,
    excess_list: ExcessList,
    part_number: str,
    *,
    with_card: bool = True,
    qty: int = 100,
) -> ExcessLineItem:
    """Add one AVAILABLE ExcessLineItem (optionally card-linked for mirror tests)."""
    card_id = None
    if with_card:
        mc = MaterialCard(normalized_mpn=part_number.lower(), display_mpn=part_number, category="capacitors")
        db.add(mc)
        db.flush()
        card_id = mc.id
    li = ExcessLineItem(
        excess_list_id=excess_list.id,
        part_number=part_number,
        quantity=qty,
        material_card_id=card_id,
        asking_price=Decimal("1.00"),
    )
    db.add(li)
    db.flush()
    return li


def _customer_excess_sightings(db: Session, company_id: int) -> list[Sighting]:
    """The list's live Sighting mirror rows (source_type='customer_excess')."""
    return (
        db.query(Sighting)
        .filter(Sighting.source_type == "customer_excess", Sighting.source_company_id == company_id)
        .all()
    )


# ═══════════════════════════════════════════════════════════════════════
#  award_offer (service)
# ═══════════════════════════════════════════════════════════════════════


class TestAwardOfferService:
    def test_award_flips_won_and_recomputes_buyer_score(
        self, db_session: Session, excess_list: ExcessList, cap_line: ExcessLineItem, owner: User, broker: User
    ):
        """The centerpiece: awarding an offer flips it to won, awards its line, and
        upserts the winning buyer's BuyerScore (the orphaned win-hook is now wired)."""
        buyer = _buyer_card(db_session, "Award Buyer")
        offer = _open_offer(
            db_session,
            excess_list=excess_list,
            submitter=broker,
            line=cap_line,
            buyer=buyer,
            unit_price=Decimal("0.80"),
        )
        db_session.commit()
        # Precondition: no scorecard yet, offer is still open.
        assert db_session.query(BuyerScore).filter_by(vendor_card_id=buyer.id).count() == 0

        result = excess_service.award_offer(db_session, offer.id, owner)

        assert result.status == ExcessOfferStatus.WON
        db_session.refresh(cap_line)
        assert cap_line.status == ExcessLineItemStatus.AWARDED
        # The win-hook fired: a BuyerScore row exists and counts the win.
        score = db_session.query(BuyerScore).filter_by(vendor_card_id=buyer.id).one()
        assert score.offers_received == 1
        assert score.wins == 1

    def test_award_with_no_canonical_buyer_wins_without_score(
        self, db_session: Session, excess_list: ExcessList, cap_line: ExcessLineItem, owner: User, broker: User
    ):
        """An offer with no canonical buyer card still wins; the hook no-ops (no
        BuyerScore, no crash)."""
        offer = _open_offer(
            db_session,
            excess_list=excess_list,
            submitter=broker,
            line=cap_line,
            buyer=None,
            unit_price=Decimal("0.50"),
        )
        db_session.commit()

        result = excess_service.award_offer(db_session, offer.id, owner)

        assert result.status == ExcessOfferStatus.WON
        assert db_session.query(BuyerScore).count() == 0

    def test_award_by_non_owner_forbidden(
        self,
        db_session: Session,
        excess_list: ExcessList,
        cap_line: ExcessLineItem,
        owner: User,
        broker: User,
        outsider: User,
    ):
        buyer = _buyer_card(db_session, "Guard Buyer")
        offer = _open_offer(
            db_session,
            excess_list=excess_list,
            submitter=broker,
            line=cap_line,
            buyer=buyer,
            unit_price=Decimal("0.80"),
        )
        db_session.commit()
        with pytest.raises(HTTPException) as exc:
            excess_service.award_offer(db_session, offer.id, outsider)
        assert exc.value.status_code == 403
        db_session.refresh(offer)
        assert offer.status == ExcessOfferStatus.OPEN  # unchanged

    def test_award_missing_offer_404(self, db_session: Session, owner: User):
        with pytest.raises(HTTPException) as exc:
            excess_service.award_offer(db_session, 999_999, owner)
        assert exc.value.status_code == 404

    def test_award_take_all_marks_all_lines(
        self, db_session: Session, excess_list: ExcessList, owner: User, broker: User
    ):
        """FIX #2: a take_all offer carries NO offer lines — awarding it must still flip
        EVERY (non-withdrawn) line to awarded, not zero."""
        a = _line(db_session, excess_list, "TAKEALL-A")
        b = _line(db_session, excess_list, "TAKEALL-B")
        buyer = _buyer_card(db_session, "Take All Buyer")
        offer = _take_all_offer(db_session, excess_list=excess_list, submitter=broker, buyer=buyer)
        db_session.commit()

        result = excess_service.award_offer(db_session, offer.id, owner)

        assert result.status == ExcessOfferStatus.WON
        db_session.refresh(a)
        db_session.refresh(b)
        assert a.status == ExcessLineItemStatus.AWARDED
        assert b.status == ExcessLineItemStatus.AWARDED

    def test_award_with_no_live_lines_rejected_not_fake_won(
        self, db_session: Session, excess_list: ExcessList, owner: User, broker: User
    ):
        """A take_all offer whose only line is withdrawn has NO live lines to award — it
        must 409 and stay OPEN, not flip to won with zero lines (fake success)."""
        li = _line(db_session, excess_list, "WITHDRAWN-ONLY")
        li.status = ExcessLineItemStatus.WITHDRAWN
        offer = _take_all_offer(db_session, excess_list=excess_list, submitter=broker, buyer=None)
        db_session.commit()

        with pytest.raises(HTTPException) as exc:
            excess_service.award_offer(db_session, offer.id, owner)
        assert exc.value.status_code == 409
        db_session.refresh(offer)
        assert offer.status == ExcessOfferStatus.OPEN

    def test_award_flips_list_when_all_decided(
        self, db_session: Session, excess_list: ExcessList, owner: User, broker: User
    ):
        """FIX #1: once every line is decided (awarded), the LIST itself flips to
        awarded so the workspace "Awarded" glance is no longer always-empty."""
        excess_list.status = ExcessListStatus.COLLECTING
        _line(db_session, excess_list, "LISTFLIP-A")
        _line(db_session, excess_list, "LISTFLIP-B")
        offer = _take_all_offer(db_session, excess_list=excess_list, submitter=broker, buyer=None)
        db_session.commit()

        excess_service.award_offer(db_session, offer.id, owner)

        db_session.refresh(excess_list)
        assert excess_list.status == ExcessListStatus.AWARDED

    def test_award_partial_does_not_flip_list(
        self, db_session: Session, excess_list: ExcessList, owner: User, broker: User
    ):
        """A per_line award of ONLY some lines must NOT flip the list — offers are still
        being collected on the rest."""
        excess_list.status = ExcessListStatus.COLLECTING
        a = _line(db_session, excess_list, "PARTIAL-A")
        _line(db_session, excess_list, "PARTIAL-B")  # left open
        offer = _open_offer(
            db_session, excess_list=excess_list, submitter=broker, line=a, buyer=None, unit_price=Decimal("0.50")
        )
        db_session.commit()

        excess_service.award_offer(db_session, offer.id, owner)

        db_session.refresh(excess_list)
        assert excess_list.status == ExcessListStatus.COLLECTING

    def test_award_idempotent_double_award(
        self, db_session: Session, excess_list: ExcessList, cap_line: ExcessLineItem, owner: User, broker: User
    ):
        """Awarding the same offer twice is a no-op the second time — the buyer's win
        count stays 1, never doubling."""
        buyer = _buyer_card(db_session, "Idempotent Buyer")
        offer = _open_offer(
            db_session,
            excess_list=excess_list,
            submitter=broker,
            line=cap_line,
            buyer=buyer,
            unit_price=Decimal("0.80"),
        )
        db_session.commit()

        excess_service.award_offer(db_session, offer.id, owner)
        again = excess_service.award_offer(db_session, offer.id, owner)

        assert again.status == ExcessOfferStatus.WON
        score = db_session.query(BuyerScore).filter_by(vendor_card_id=buyer.id).one()
        assert score.wins == 1

    def test_award_conflict_409_line_already_awarded(
        self, db_session: Session, excess_list: ExcessList, cap_line: ExcessLineItem, owner: User, broker: User
    ):
        """Two competing offers on the same line: awarding the second must 409 (the line
        is already sold) — never silently steal it."""
        first = _open_offer(
            db_session, excess_list=excess_list, submitter=broker, line=cap_line, buyer=None, unit_price=Decimal("0.80")
        )
        second = _open_offer(
            db_session, excess_list=excess_list, submitter=broker, line=cap_line, buyer=None, unit_price=Decimal("0.70")
        )
        db_session.commit()
        excess_service.award_offer(db_session, first.id, owner)

        with pytest.raises(HTTPException) as exc:
            excess_service.award_offer(db_session, second.id, owner)
        assert exc.value.status_code == 409
        assert "already awarded" in exc.value.detail
        db_session.refresh(second)
        assert second.status == ExcessOfferStatus.OPEN

    def test_award_retires_sighting_mirror(
        self, db_session: Session, excess_list: ExcessList, cap_line: ExcessLineItem, owner: User, broker: User
    ):
        """FIX #3: a sold line must stop advertising as live supply — awarding retires
        its Sighting mirror row."""
        publish_list(db_session, excess_list.id, owner)
        assert len(_customer_excess_sightings(db_session, excess_list.company_id)) == 1
        offer = _open_offer(
            db_session, excess_list=excess_list, submitter=broker, line=cap_line, buyer=None, unit_price=Decimal("0.80")
        )
        db_session.commit()

        excess_service.award_offer(db_session, offer.id, owner)

        assert _customer_excess_sightings(db_session, excess_list.company_id) == []


# ═══════════════════════════════════════════════════════════════════════
#  unaward_offer (service) — the explicit inverse
# ═══════════════════════════════════════════════════════════════════════


class TestUnawardOfferService:
    def test_unaward_reverts_offer_line_and_score(
        self, db_session: Session, excess_list: ExcessList, cap_line: ExcessLineItem, owner: User, broker: User
    ):
        """Unaward flips the offer back to open, its line back to available, and drops
        the buyer's win count back to 0 (a full-history recompute self-heals)."""
        buyer = _buyer_card(db_session, "Reverse Buyer")
        offer = _open_offer(
            db_session,
            excess_list=excess_list,
            submitter=broker,
            line=cap_line,
            buyer=buyer,
            unit_price=Decimal("0.80"),
        )
        db_session.commit()
        excess_service.award_offer(db_session, offer.id, owner)

        result = excess_service.unaward_offer(db_session, offer.id, owner)

        assert result.status == ExcessOfferStatus.OPEN
        db_session.refresh(cap_line)
        assert cap_line.status == ExcessLineItemStatus.AVAILABLE
        score = db_session.query(BuyerScore).filter_by(vendor_card_id=buyer.id).one()
        assert score.wins == 0

    def test_unaward_reverts_list_status_to_bid_out(
        self, db_session: Session, excess_list: ExcessList, cap_line: ExcessLineItem, owner: User, broker: User
    ):
        """A closed (close_at stamped) list that was awarded steps back to bid_out on
        unaward, not to the pre-close collecting."""
        from datetime import datetime, timezone

        excess_list.status = ExcessListStatus.BID_OUT
        excess_list.close_at = datetime.now(timezone.utc)
        offer = _open_offer(
            db_session, excess_list=excess_list, submitter=broker, line=cap_line, buyer=None, unit_price=Decimal("0.80")
        )
        db_session.commit()
        excess_service.award_offer(db_session, offer.id, owner)
        db_session.refresh(excess_list)
        assert excess_list.status == ExcessListStatus.AWARDED  # precondition

        excess_service.unaward_offer(db_session, offer.id, owner)

        db_session.refresh(excess_list)
        assert excess_list.status == ExcessListStatus.BID_OUT

    def test_unaward_re_mirrors_line(
        self, db_session: Session, excess_list: ExcessList, cap_line: ExcessLineItem, owner: User, broker: User
    ):
        """Reversing an award re-mirrors the line so it advertises as live supply
        again."""
        publish_list(db_session, excess_list.id, owner)
        offer = _open_offer(
            db_session, excess_list=excess_list, submitter=broker, line=cap_line, buyer=None, unit_price=Decimal("0.80")
        )
        db_session.commit()
        excess_service.award_offer(db_session, offer.id, owner)
        assert _customer_excess_sightings(db_session, excess_list.company_id) == []  # retired

        excess_service.unaward_offer(db_session, offer.id, owner)

        assert len(_customer_excess_sightings(db_session, excess_list.company_id)) == 1

    def test_unaward_by_non_owner_forbidden(
        self,
        db_session: Session,
        excess_list: ExcessList,
        cap_line: ExcessLineItem,
        owner: User,
        broker: User,
        outsider: User,
    ):
        offer = _open_offer(
            db_session, excess_list=excess_list, submitter=broker, line=cap_line, buyer=None, unit_price=Decimal("0.80")
        )
        db_session.commit()
        excess_service.award_offer(db_session, offer.id, owner)

        with pytest.raises(HTTPException) as exc:
            excess_service.unaward_offer(db_session, offer.id, outsider)
        assert exc.value.status_code == 403
        db_session.refresh(offer)
        assert offer.status == ExcessOfferStatus.WON  # unchanged

    def test_unaward_not_awarded_409(
        self, db_session: Session, excess_list: ExcessList, cap_line: ExcessLineItem, owner: User, broker: User
    ):
        """Unawarding an offer that never won is a 409 — there is nothing to reverse."""
        offer = _open_offer(
            db_session, excess_list=excess_list, submitter=broker, line=cap_line, buyer=None, unit_price=Decimal("0.80")
        )
        db_session.commit()

        with pytest.raises(HTTPException) as exc:
            excess_service.unaward_offer(db_session, offer.id, owner)
        assert exc.value.status_code == 409

    def test_unaward_missing_offer_404(self, db_session: Session, owner: User):
        with pytest.raises(HTTPException) as exc:
            excess_service.unaward_offer(db_session, 999_999, owner)
        assert exc.value.status_code == 404


# ═══════════════════════════════════════════════════════════════════════
#  POST award endpoint
# ═══════════════════════════════════════════════════════════════════════


class TestAwardRoute:
    def test_award_route_marks_won_and_recomputes_score(
        self, client, db_session: Session, excess_list: ExcessList, cap_line: ExcessLineItem, owner: User, broker: User
    ):
        from app.dependencies import require_user
        from app.main import app

        buyer = _buyer_card(db_session, "Route Buyer")
        offer = _open_offer(
            db_session,
            excess_list=excess_list,
            submitter=broker,
            line=cap_line,
            buyer=buyer,
            unit_price=Decimal("0.80"),
        )
        db_session.commit()

        app.dependency_overrides[require_user] = lambda: owner
        try:
            resp = client.post(f"/api/resell/{excess_list.id}/offers/{offer.id}/award")
        finally:
            app.dependency_overrides.pop(require_user, None)

        assert resp.status_code == 200
        db_session.refresh(offer)
        assert offer.status == ExcessOfferStatus.WON
        assert db_session.query(BuyerScore).filter_by(vendor_card_id=buyer.id).count() == 1

    def test_award_route_non_owner_forbidden(
        self, client, db_session: Session, excess_list: ExcessList, cap_line: ExcessLineItem, broker: User
    ):
        """The default client user is not the list owner → 403 (the award stays
        open)."""
        buyer = _buyer_card(db_session, "Route Guard Buyer")
        offer = _open_offer(
            db_session,
            excess_list=excess_list,
            submitter=broker,
            line=cap_line,
            buyer=buyer,
            unit_price=Decimal("0.80"),
        )
        db_session.commit()

        resp = client.post(f"/api/resell/{excess_list.id}/offers/{offer.id}/award")
        assert resp.status_code == 403
        db_session.refresh(offer)
        assert offer.status == ExcessOfferStatus.OPEN

    def test_award_route_shows_unaward_and_hides_award(
        self, client, db_session: Session, excess_list: ExcessList, cap_line: ExcessLineItem, owner: User, broker: User
    ):
        """Awarding via the route returns the refreshed Offers tab: the won row now offers
        Unaward, and no Award button for that offer remains."""
        from app.dependencies import require_user
        from app.main import app

        excess_list.status = ExcessListStatus.COLLECTING
        offer = _open_offer(
            db_session, excess_list=excess_list, submitter=broker, line=cap_line, buyer=None, unit_price=Decimal("0.80")
        )
        db_session.commit()

        app.dependency_overrides[require_user] = lambda: owner
        try:
            resp = client.post(f"/api/resell/{excess_list.id}/offers/{offer.id}/award")
        finally:
            app.dependency_overrides.pop(require_user, None)

        assert resp.status_code == 200
        assert "Awarded" in resp.text
        assert f"/offers/{offer.id}/unaward" in resp.text
        assert f"/offers/{offer.id}/award" not in resp.text

    def test_award_route_conflict_409_json_error_shape(
        self, client, db_session: Session, excess_list: ExcessList, cap_line: ExcessLineItem, owner: User, broker: User
    ):
        """A conflicting award returns a 409 with the ``{"error": ...}`` shape the
        global htmx error toast reads."""
        from app.dependencies import require_user
        from app.main import app

        first = _open_offer(
            db_session, excess_list=excess_list, submitter=broker, line=cap_line, buyer=None, unit_price=Decimal("0.80")
        )
        second = _open_offer(
            db_session, excess_list=excess_list, submitter=broker, line=cap_line, buyer=None, unit_price=Decimal("0.70")
        )
        db_session.commit()
        excess_service.award_offer(db_session, first.id, owner)

        app.dependency_overrides[require_user] = lambda: owner
        try:
            resp = client.post(f"/api/resell/{excess_list.id}/offers/{second.id}/award")
        finally:
            app.dependency_overrides.pop(require_user, None)

        assert resp.status_code == 409
        assert "already awarded" in resp.json()["error"]

    def test_unaward_route_reverts_200(
        self, client, db_session: Session, excess_list: ExcessList, cap_line: ExcessLineItem, owner: User, broker: User
    ):
        from app.dependencies import require_user
        from app.main import app

        offer = _open_offer(
            db_session, excess_list=excess_list, submitter=broker, line=cap_line, buyer=None, unit_price=Decimal("0.80")
        )
        db_session.commit()
        excess_service.award_offer(db_session, offer.id, owner)

        app.dependency_overrides[require_user] = lambda: owner
        try:
            resp = client.post(f"/api/resell/{excess_list.id}/offers/{offer.id}/unaward")
        finally:
            app.dependency_overrides.pop(require_user, None)

        assert resp.status_code == 200
        db_session.refresh(offer)
        assert offer.status == ExcessOfferStatus.OPEN

    def test_unaward_route_non_owner_forbidden(
        self, client, db_session: Session, excess_list: ExcessList, cap_line: ExcessLineItem, owner: User, broker: User
    ):
        """The default client user is not the owner → unaward is 403 (the award
        stands)."""
        offer = _open_offer(
            db_session, excess_list=excess_list, submitter=broker, line=cap_line, buyer=None, unit_price=Decimal("0.80")
        )
        db_session.commit()
        excess_service.award_offer(db_session, offer.id, owner)

        resp = client.post(f"/api/resell/{excess_list.id}/offers/{offer.id}/unaward")
        assert resp.status_code == 403
        db_session.refresh(offer)
        assert offer.status == ExcessOfferStatus.WON
