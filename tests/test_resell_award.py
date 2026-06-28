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

from app.constants import ExcessLineItemStatus, ExcessOfferStatus, OfferLineMatchStatus
from app.models import Company, User, VendorCard
from app.models.excess import (
    BuyerScore,
    ExcessLineItem,
    ExcessList,
    ExcessOffer,
    ExcessOfferLine,
)
from app.models.intelligence import MaterialCard
from app.services import excess_service
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
