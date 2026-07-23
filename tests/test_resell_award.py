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

import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session, sessionmaker

from app.constants import ExcessLineItemStatus, ExcessListStatus, ExcessOfferStatus, OfferLineMatchStatus
from app.models import Company, User, VendorCard
from app.models.base import Base
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
from tests.conftest import engine, requires_postgres

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

    def test_ui_submit_with_buyer_company_scores_on_award(
        self,
        db_session: Session,
        excess_list: ExcessList,
        cap_line: ExcessLineItem,
        owner: User,
        broker: User,
        seller_company: Company,
    ):
        """End-to-end (finding #17 UI half): a UI-submit offer attributed to a buyer
        company resolves offerer_vendor_card_id, and awarding it WRITES a BuyerScore for
        that card — the gap the whole finding is about (previously NULL card → no score
        on manual offers)."""
        excess_list.status = ExcessListStatus.OPEN
        buyer_company = Company(name="UI Buyer Co")
        db_session.add(buyer_company)
        db_session.commit()

        offer = excess_service.submit_offer(
            db_session,
            list_id=excess_list.id,
            user=broker,
            scope="per_line",
            lines=[{"mpn_raw": cap_line.part_number, "quantity": 10, "unit_price": Decimal("0.80")}],
            buyer_company_id=buyer_company.id,
        )
        assert offer.offerer_vendor_card_id is not None
        card_id = offer.offerer_vendor_card_id

        result = excess_service.award_offer(db_session, offer.id, owner)

        assert result.status == ExcessOfferStatus.WON
        # The win-hook fired for the attributed buyer — a BuyerScore now exists.
        score = db_session.query(BuyerScore).filter_by(vendor_card_id=card_id).one()
        assert score.wins == 1

    def test_ui_submit_without_buyer_company_wins_without_score(
        self, db_session: Session, excess_list: ExcessList, cap_line: ExcessLineItem, owner: User, broker: User
    ):
        """A UI-submit offer with NO buyer attribution leaves offerer_vendor_card_id
        None and still awards (no regression) — just no BuyerScore, exactly as
        before."""
        excess_list.status = ExcessListStatus.OPEN
        db_session.commit()

        offer = excess_service.submit_offer(
            db_session,
            list_id=excess_list.id,
            user=broker,
            scope="per_line",
            lines=[{"mpn_raw": cap_line.part_number, "quantity": 10, "unit_price": Decimal("0.80")}],
        )
        assert offer.offerer_vendor_card_id is None

        result = excess_service.award_offer(db_session, offer.id, owner)

        assert result.status == ExcessOfferStatus.WON
        assert db_session.query(BuyerScore).count() == 0

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

    @pytest.mark.parametrize("terminal_status", [ExcessListStatus.CLOSED, ExcessListStatus.EXPIRED])
    def test_award_on_terminal_list_409_no_reopen(
        self,
        db_session: Session,
        excess_list: ExcessList,
        cap_line: ExcessLineItem,
        owner: User,
        broker: User,
        terminal_status,
    ):
        """A CLOSED (D5 'close without bid') or EXPIRED list is terminal — awarding a
        still-open offer on it must 409, never flip it to AWARDED (finding #4).

        Without the guard, award reopens the dead list to AWARDED and a subsequent
        unaward steps it to BID_OUT — exactly the reopen the D5 contract forbids.
        """
        offer = _open_offer(
            db_session, excess_list=excess_list, submitter=broker, line=cap_line, buyer=None, unit_price=Decimal("0.80")
        )
        excess_list.status = terminal_status
        db_session.commit()

        with pytest.raises(HTTPException) as exc:
            excess_service.award_offer(db_session, offer.id, owner)
        assert exc.value.status_code == 409
        db_session.refresh(offer)
        db_session.refresh(excess_list)
        assert offer.status == ExcessOfferStatus.OPEN  # not flipped to won
        assert excess_list.status == terminal_status  # stayed terminal — no reopen
        db_session.refresh(cap_line)
        assert cap_line.status == ExcessLineItemStatus.AVAILABLE

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
        """A still-open offer landing on an already-sold line must 409 (never silently
        steal it) — this is the line-level ``already awarded`` guard, distinct from the
        offer-status guard that rejects a lost/withdrawn offer.

        ``second`` is created AFTER
        the award so it is genuinely open (not closed as a pre-existing competitor — that
        M1 path is covered in TestAwardClosesCompetingOffers).
        """
        first = _open_offer(
            db_session, excess_list=excess_list, submitter=broker, line=cap_line, buyer=None, unit_price=Decimal("0.80")
        )
        db_session.commit()
        excess_service.award_offer(db_session, first.id, owner)  # cap_line -> awarded

        second = _open_offer(
            db_session, excess_list=excess_list, submitter=broker, line=cap_line, buyer=None, unit_price=Decimal("0.70")
        )
        db_session.commit()

        with pytest.raises(HTTPException) as exc:
            excess_service.award_offer(db_session, second.id, owner)
        assert exc.value.status_code == 409
        assert "already awarded" in exc.value.detail
        db_session.refresh(second)
        assert second.status == ExcessOfferStatus.OPEN  # the failed award left it untouched

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

    def test_award_lock_refreshes_stale_excess_list_status(
        self, db_session: Session, excess_list: ExcessList, cap_line: ExcessLineItem, owner: User, broker: User
    ):
        """Finding #8: the M9 lock's ExcessList query must ``populate_existing`` so the
        terminal-list guard reads POST-lock committed state, never a stale identity-
        mapped object.

        Simulates the race within one SQLite session: a raw core UPDATE (bypassing the
        ORM entirely — exactly like a second transaction's committed write the row lock
        just blocked on) flips the list to CLOSED behind the already-identity-mapped
        ``excess_list`` object's back, with NO intervening commit (so ``expire_on_commit``
        never auto-refreshes it — it stays genuinely stale until something explicitly
        re-populates it). Without ``populate_existing`` on the ExcessList lock query,
        ``award_offer`` would read the STALE in-memory 'collecting' status and award a
        dead list; with the fix the terminal-list guard sees 'closed' and 409s.
        """
        excess_list.status = ExcessListStatus.COLLECTING
        offer = _open_offer(
            db_session, excess_list=excess_list, submitter=broker, line=cap_line, buyer=None, unit_price=Decimal("0.80")
        )
        db_session.commit()

        # Bypass the ORM: a raw UPDATE the identity-mapped ``excess_list`` object never
        # sees on its own (no commit → no expire-on-commit refresh).
        db_session.execute(
            sa_text("UPDATE excess_lists SET status = 'closed' WHERE id = :id").bindparams(id=excess_list.id)
        )
        assert excess_list.status == ExcessListStatus.COLLECTING  # still stale, pre-call

        with pytest.raises(HTTPException) as exc:
            excess_service.award_offer(db_session, offer.id, owner)
        assert exc.value.status_code == 409
        assert excess_list.status == ExcessListStatus.CLOSED  # the lock refreshed it in place
        db_session.refresh(offer)
        db_session.refresh(cap_line)
        assert offer.status == ExcessOfferStatus.OPEN  # never awarded
        assert cap_line.status == ExcessLineItemStatus.AVAILABLE


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
        from datetime import datetime

        excess_list.status = ExcessListStatus.BID_OUT
        excess_list.close_at = datetime.now(UTC)
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

    def test_unaward_future_deadline_list_steps_to_collecting_and_re_mirrors(
        self, db_session: Session, excess_list: ExcessList, cap_line: ExcessLineItem, owner: User, broker: User
    ):
        """A D1 list published with a FUTURE 'Offers close by' deadline, awarded then
        unawarded, steps back to COLLECTING and re-advertises its supply — NOT bid_out.

        Findings #1/#3: once Phase 5 preserves a create-set future ``close_at`` through
        publish, a truthy ``close_at`` is no longer proof the posting window was closed.
        The window here never closed (its deadline is still in the future), so reversing
        the award must re-open the list to ``collecting`` and re-mirror its live supply —
        not strand it in ``bid_out`` with its mirror retired.
        """
        from datetime import datetime, timedelta

        excess_list.close_at = datetime.now(UTC) + timedelta(days=3)
        db_session.commit()
        publish_list(db_session, excess_list.id, owner)
        db_session.refresh(excess_list)
        assert excess_list.close_at is not None  # future deadline preserved through publish

        offer = _open_offer(
            db_session, excess_list=excess_list, submitter=broker, line=cap_line, buyer=None, unit_price=Decimal("0.80")
        )
        db_session.commit()
        excess_service.award_offer(db_session, offer.id, owner)
        db_session.refresh(excess_list)
        assert excess_list.status == ExcessListStatus.AWARDED  # precondition
        assert _customer_excess_sightings(db_session, excess_list.company_id) == []  # retired on award

        excess_service.unaward_offer(db_session, offer.id, owner)

        db_session.refresh(excess_list)
        assert excess_list.status == ExcessListStatus.COLLECTING  # window still open → re-advertise
        assert len(_customer_excess_sightings(db_session, excess_list.company_id)) == 1  # re-mirrored

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

    @pytest.mark.parametrize("terminal_status", [ExcessListStatus.CLOSED, ExcessListStatus.EXPIRED])
    def test_unaward_on_terminal_list_409_no_reopen(
        self,
        db_session: Session,
        excess_list: ExcessList,
        cap_line: ExcessLineItem,
        owner: User,
        broker: User,
        terminal_status,
    ):
        """Finding #4: unaward mirrors award's terminal-list guard — a CLOSED (D5) or
        EXPIRED list is dead, so reversing a win there must 409, never unwind the sale.

        Without the guard, the winner flips back to open, its awarded line back to
        available, and any lost competitor revives — all on a list where award_offer
        permanently 409s, leaving the reversed offer stuck open forever.
        """
        offer = _open_offer(
            db_session, excess_list=excess_list, submitter=broker, line=cap_line, buyer=None, unit_price=Decimal("0.80")
        )
        db_session.commit()
        excess_service.award_offer(db_session, offer.id, owner)
        excess_list.status = terminal_status
        db_session.commit()

        with pytest.raises(HTTPException) as exc:
            excess_service.unaward_offer(db_session, offer.id, owner)
        assert exc.value.status_code == 409
        db_session.refresh(offer)
        db_session.refresh(cap_line)
        db_session.refresh(excess_list)
        assert offer.status == ExcessOfferStatus.WON  # unchanged — the sale stands
        assert cap_line.status == ExcessLineItemStatus.AWARDED
        assert excess_list.status == terminal_status  # stayed terminal — no reopen

    def test_unaward_on_bid_out_list_still_works(
        self, db_session: Session, excess_list: ExcessList, owner: User, broker: User
    ):
        """BID_OUT is NOT terminal (finding #4 distinguishes it from CLOSED/EXPIRED) —
        unaward must still succeed on a BID_OUT list.

        A PARTIAL award (one of two lines) leaves the list at BID_OUT (not flipped to
        AWARDED, since ``_apply_award_list_status`` only fires when every line is
        decided) — the realistic way a WON offer coexists with a non-AWARDED, non-
        terminal list status.
        """
        a = _line(db_session, excess_list, "PARTIAL-BIDOUT-A")
        _line(db_session, excess_list, "PARTIAL-BIDOUT-B")
        excess_list.status = ExcessListStatus.BID_OUT
        offer = _open_offer(
            db_session, excess_list=excess_list, submitter=broker, line=a, buyer=None, unit_price=Decimal("0.80")
        )
        db_session.commit()
        excess_service.award_offer(db_session, offer.id, owner)
        db_session.refresh(excess_list)
        assert excess_list.status == ExcessListStatus.BID_OUT  # precondition — partial award

        result = excess_service.unaward_offer(db_session, offer.id, owner)

        assert result.status in {ExcessOfferStatus.OPEN, ExcessOfferStatus.LATE}
        db_session.refresh(a)
        assert a.status == ExcessLineItemStatus.AVAILABLE


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
        db_session.commit()
        excess_service.award_offer(db_session, first.id, owner)  # cap_line -> awarded
        # A new open offer on the now-sold line (created after the award) hits the
        # line-level ``already awarded`` guard.
        second = _open_offer(
            db_session, excess_list=excess_list, submitter=broker, line=cap_line, buyer=None, unit_price=Decimal("0.70")
        )
        db_session.commit()

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

    def test_award_route_wrong_list_id_404_nothing_mutated(
        self,
        client,
        db_session: Session,
        excess_list: ExcessList,
        cap_line: ExcessLineItem,
        owner: User,
        broker: User,
        seller_company: Company,
    ):
        """Finding #32: awarding via a URL whose {list_id} does NOT match the offer's
        real list must 404 (existence not revealed across lists) — never mutate the
        offer under the wrong list's URL."""
        from app.dependencies import require_user
        from app.main import app

        other_list = ExcessList(company_id=seller_company.id, owner_id=owner.id, title="Other List (award)")
        db_session.add(other_list)
        db_session.flush()
        offer = _open_offer(
            db_session, excess_list=excess_list, submitter=broker, line=cap_line, buyer=None, unit_price=Decimal("0.80")
        )
        db_session.commit()

        app.dependency_overrides[require_user] = lambda: owner
        try:
            resp = client.post(f"/api/resell/{other_list.id}/offers/{offer.id}/award")
        finally:
            app.dependency_overrides.pop(require_user, None)

        assert resp.status_code == 404
        db_session.refresh(offer)
        assert offer.status == ExcessOfferStatus.OPEN  # never awarded

    def test_unaward_route_wrong_list_id_404_nothing_mutated(
        self,
        client,
        db_session: Session,
        excess_list: ExcessList,
        cap_line: ExcessLineItem,
        owner: User,
        broker: User,
        seller_company: Company,
    ):
        """Finding #32: the same URL-vs-real-list guard on unaward."""
        from app.dependencies import require_user
        from app.main import app

        other_list = ExcessList(company_id=seller_company.id, owner_id=owner.id, title="Other List (unaward)")
        db_session.add(other_list)
        db_session.flush()
        offer = _open_offer(
            db_session, excess_list=excess_list, submitter=broker, line=cap_line, buyer=None, unit_price=Decimal("0.80")
        )
        db_session.commit()
        excess_service.award_offer(db_session, offer.id, owner)

        app.dependency_overrides[require_user] = lambda: owner
        try:
            resp = client.post(f"/api/resell/{other_list.id}/offers/{offer.id}/unaward")
        finally:
            app.dependency_overrides.pop(require_user, None)

        assert resp.status_code == 404
        db_session.refresh(offer)
        assert offer.status == ExcessOfferStatus.WON  # unchanged, still awarded


# ═══════════════════════════════════════════════════════════════════════
#  award closes competing offers (M1: the ``lost`` state)
# ═══════════════════════════════════════════════════════════════════════


def _multi_line_offer(
    db: Session, *, excess_list: ExcessList, submitter: User, lines: list[ExcessLineItem], unit_price: Decimal
) -> ExcessOffer:
    """An OPEN per-line offer bidding on several matched lines at once."""
    offer = ExcessOffer(
        excess_list_id=excess_list.id,
        submitted_by=submitter.id,
        scope="per_line",
        status=ExcessOfferStatus.OPEN,
    )
    db.add(offer)
    db.flush()
    for li in lines:
        db.add(
            ExcessOfferLine(
                offer_id=offer.id,
                excess_line_item_id=li.id,
                mpn_raw=li.part_number,
                quantity=li.quantity,
                unit_price=unit_price,
                match_status=OfferLineMatchStatus.MATCHED,
            )
        )
    db.flush()
    return offer


class TestAwardClosesCompetingOffers:
    def test_award_marks_competing_offer_lost(
        self, db_session: Session, excess_list: ExcessList, cap_line: ExcessLineItem, owner: User, broker: User
    ):
        """Awarding one offer on a line closes the other open offers on it as ``lost`` —
        losing bids stop lingering ``open``."""
        winner = _open_offer(
            db_session, excess_list=excess_list, submitter=broker, line=cap_line, buyer=None, unit_price=Decimal("0.90")
        )
        loser = _open_offer(
            db_session, excess_list=excess_list, submitter=broker, line=cap_line, buyer=None, unit_price=Decimal("0.80")
        )
        db_session.commit()

        excess_service.award_offer(db_session, winner.id, owner)

        db_session.refresh(loser)
        assert loser.status == ExcessOfferStatus.LOST

    def test_lost_bid_drops_from_line_rollup(
        self, db_session: Session, excess_list: ExcessList, cap_line: ExcessLineItem, owner: User, broker: User
    ):
        """A HIGHER losing bid owns the rollup before award; once it is marked ``lost``
        it stops owning ``best_offer_id`` (the rollup counts only open/won)."""
        winner = _open_offer(
            db_session, excess_list=excess_list, submitter=broker, line=cap_line, buyer=None, unit_price=Decimal("0.90")
        )
        higher_loser = _open_offer(
            db_session, excess_list=excess_list, submitter=broker, line=cap_line, buyer=None, unit_price=Decimal("1.50")
        )
        db_session.commit()
        # _open_offer builds rows directly (no rollup hook) — compute it once to establish
        # the pre-award state: the highest bid owns the line.
        excess_service.recompute_line_rollup(db_session, cap_line.id)
        db_session.commit()
        db_session.refresh(cap_line)
        assert cap_line.best_offer_id == higher_loser.id  # highest bid owns it pre-award

        excess_service.award_offer(db_session, winner.id, owner)

        db_session.refresh(higher_loser)
        db_session.refresh(cap_line)
        assert higher_loser.status == ExcessOfferStatus.LOST
        assert cap_line.best_offer_id == winner.id  # the lost bid no longer owns the rollup

    def test_competitor_on_still_open_line_stays_open(
        self, db_session: Session, excess_list: ExcessList, owner: User, broker: User
    ):
        """A per-line offer that also bids on an UN-awarded line is not closed — it can
        still win that line."""
        a = _line(db_session, excess_list, "COMP-A")
        b = _line(db_session, excess_list, "COMP-B")
        competitor = _multi_line_offer(
            db_session, excess_list=excess_list, submitter=broker, lines=[a, b], unit_price=Decimal("0.50")
        )
        winner = _open_offer(
            db_session, excess_list=excess_list, submitter=broker, line=a, buyer=None, unit_price=Decimal("0.90")
        )
        db_session.commit()

        excess_service.award_offer(db_session, winner.id, owner)

        db_session.refresh(competitor)
        assert competitor.status == ExcessOfferStatus.OPEN  # line b is still winnable

    def test_take_all_award_closes_every_other_open_offer(
        self, db_session: Session, excess_list: ExcessList, owner: User, broker: User
    ):
        """A take_all win takes the whole list — every other open offer (per-line and
        take_all) is closed ``lost``."""
        a = _line(db_session, excess_list, "TAKEALL-CLOSE-A")
        _line(db_session, excess_list, "TAKEALL-CLOSE-B")
        per_line = _open_offer(
            db_session, excess_list=excess_list, submitter=broker, line=a, buyer=None, unit_price=Decimal("0.50")
        )
        other_take_all = _take_all_offer(db_session, excess_list=excess_list, submitter=broker, buyer=None)
        winner = _take_all_offer(db_session, excess_list=excess_list, submitter=broker, buyer=None)
        db_session.commit()

        excess_service.award_offer(db_session, winner.id, owner)

        db_session.refresh(per_line)
        db_session.refresh(other_take_all)
        assert per_line.status == ExcessOfferStatus.LOST
        assert other_take_all.status == ExcessOfferStatus.LOST

    def test_per_line_award_leaves_take_all_competitor_open(
        self, db_session: Session, excess_list: ExcessList, owner: User, broker: User
    ):
        """A per-line award of one line does NOT close a take_all competitor — it is
        blocked but revivable (unaward re-opens the path)."""
        a = _line(db_session, excess_list, "TAKEALL-KEEP-A")
        _line(db_session, excess_list, "TAKEALL-KEEP-B")
        take_all = _take_all_offer(db_session, excess_list=excess_list, submitter=broker, buyer=None)
        winner = _open_offer(
            db_session, excess_list=excess_list, submitter=broker, line=a, buyer=None, unit_price=Decimal("0.90")
        )
        db_session.commit()

        excess_service.award_offer(db_session, winner.id, owner)

        db_session.refresh(take_all)
        assert take_all.status == ExcessOfferStatus.OPEN

    def test_unaward_reopens_lost_competitor(
        self, db_session: Session, excess_list: ExcessList, cap_line: ExcessLineItem, owner: User, broker: User
    ):
        """Reversing an award revives the offers it had closed and hands the rollup back
        to the (higher) revived bid."""
        winner = _open_offer(
            db_session, excess_list=excess_list, submitter=broker, line=cap_line, buyer=None, unit_price=Decimal("0.90")
        )
        loser = _open_offer(
            db_session, excess_list=excess_list, submitter=broker, line=cap_line, buyer=None, unit_price=Decimal("1.50")
        )
        db_session.commit()
        excess_service.award_offer(db_session, winner.id, owner)
        db_session.refresh(loser)
        assert loser.status == ExcessOfferStatus.LOST

        excess_service.unaward_offer(db_session, winner.id, owner)

        db_session.refresh(loser)
        db_session.refresh(cap_line)
        assert loser.status == ExcessOfferStatus.OPEN
        assert cap_line.best_offer_id == loser.id  # revived higher bid owns the rollup again

    def test_unaward_revives_late_born_winner_as_late_not_open(
        self, db_session: Session, excess_list: ExcessList, cap_line: ExcessLineItem, owner: User, broker: User
    ):
        """Finding #37: a WON offer that landed LATE (after the posting window's
        close_at) must revive ``late`` on unaward — never a blanket ``open`` that erases
        the late-arrival provenance."""
        excess_list.close_at = datetime.now(UTC) - timedelta(hours=1)
        db_session.commit()
        winner = _open_offer(
            db_session, excess_list=excess_list, submitter=broker, line=cap_line, buyer=None, unit_price=Decimal("0.90")
        )
        winner.created_at = datetime.now(UTC)  # landed AFTER close_at → was late
        db_session.commit()
        excess_service.award_offer(db_session, winner.id, owner)

        result = excess_service.unaward_offer(db_session, winner.id, owner)

        assert result.status == ExcessOfferStatus.LATE

    def test_unaward_revives_on_time_winner_as_open(
        self, db_session: Session, excess_list: ExcessList, cap_line: ExcessLineItem, owner: User, broker: User
    ):
        """Control: a winner that landed BEFORE close_at still revives ``open`` (no
        close_at at all is also covered by ``test_unaward_reopens_lost_competitor``)."""
        excess_list.close_at = datetime.now(UTC) + timedelta(hours=1)
        db_session.commit()
        winner = _open_offer(
            db_session, excess_list=excess_list, submitter=broker, line=cap_line, buyer=None, unit_price=Decimal("0.90")
        )
        db_session.commit()
        excess_service.award_offer(db_session, winner.id, owner)

        result = excess_service.unaward_offer(db_session, winner.id, owner)

        assert result.status == ExcessOfferStatus.OPEN

    def test_unaward_reopens_late_born_competitor_as_late_not_open(
        self, db_session: Session, excess_list: ExcessList, cap_line: ExcessLineItem, owner: User, broker: User
    ):
        """Finding #46: a competing offer that was LATE when the award closed it (M1)
        must revive ``late`` on unaward, not the flattened ``open`` — the "landed after
        your window closed" signal must survive the award→unaward round-trip."""
        excess_list.close_at = datetime.now(UTC) - timedelta(hours=1)
        db_session.commit()
        winner = _open_offer(
            db_session, excess_list=excess_list, submitter=broker, line=cap_line, buyer=None, unit_price=Decimal("0.90")
        )
        late_competitor = _open_offer(
            db_session, excess_list=excess_list, submitter=broker, line=cap_line, buyer=None, unit_price=Decimal("1.50")
        )
        late_competitor.created_at = datetime.now(UTC)  # landed AFTER close_at → was late
        db_session.commit()
        excess_service.award_offer(db_session, winner.id, owner)
        db_session.refresh(late_competitor)
        assert late_competitor.status == ExcessOfferStatus.LOST  # precondition

        excess_service.unaward_offer(db_session, winner.id, owner)

        db_session.refresh(late_competitor)
        assert late_competitor.status == ExcessOfferStatus.LATE

    def test_revived_offer_status_awarded_close_at_none_is_late(
        self, excess_list: ExcessList, cap_line: ExcessLineItem, broker: User
    ):
        """Deep-review #2 residual R3: ``_revived_offer_status`` only modeled close_at
        lateness — a list closed by STATUS (e.g. ``awarded``, with another line STILL
        awarded — i.e. genuinely resolved beyond just this reversal) with no
        ``close_at`` ever recorded must still revive ``late``, not a blanket ``open``
        that hides a resolved posting."""
        cap_line.status = ExcessLineItemStatus.AWARDED  # some OTHER sale still stands
        excess_list.status = ExcessListStatus.AWARDED
        excess_list.close_at = None
        offer = ExcessOffer(excess_list_id=excess_list.id, submitted_by=broker.id, scope="per_line", status="lost")

        assert excess_service._revived_offer_status(excess_list, offer, competitor=True) == ExcessOfferStatus.LATE

    def test_revived_offer_status_collecting_close_at_none_is_open(
        self, excess_list: ExcessList, cap_line: ExcessLineItem, broker: User
    ):
        """Control: a list that is NOT closed by status (still ``collecting``) with no
        ``close_at`` still revives ``open`` — the R3 fallback only fires when the list
        currently reads resolved."""
        excess_list.status = ExcessListStatus.COLLECTING
        excess_list.close_at = None
        offer = ExcessOffer(excess_list_id=excess_list.id, submitted_by=broker.id, scope="per_line", status="lost")

        assert excess_service._revived_offer_status(excess_list, offer, competitor=True) == ExcessOfferStatus.OPEN

    def test_unaward_on_close_at_null_awarded_list_revives_competitor_as_late(
        self, db_session: Session, excess_list: ExcessList, cap_line: ExcessLineItem, owner: User, broker: User
    ):
        """R3 end-to-end: a list that reaches ``awarded`` with no ``close_at`` ever set
        (no D1 deadline configured) still revives a reopened competitor as ``late``, not
        an indistinguishable ``open``, using the list's CURRENT resolved status as the
        only remaining lateness signal."""
        second_line = _line(db_session, excess_list, "R3-SECOND-LINE")
        winner1 = _open_offer(
            db_session, excess_list=excess_list, submitter=broker, line=cap_line, buyer=None, unit_price=Decimal("0.90")
        )
        competitor = _open_offer(
            db_session, excess_list=excess_list, submitter=broker, line=cap_line, buyer=None, unit_price=Decimal("0.80")
        )
        db_session.commit()
        excess_service.award_offer(db_session, winner1.id, owner)
        db_session.refresh(competitor)
        assert competitor.status == ExcessOfferStatus.LOST  # precondition

        winner2 = _open_offer(
            db_session,
            excess_list=excess_list,
            submitter=broker,
            line=second_line,
            buyer=None,
            unit_price=Decimal("0.70"),
        )
        db_session.commit()
        excess_service.award_offer(db_session, winner2.id, owner)
        db_session.refresh(excess_list)
        assert excess_list.status == ExcessListStatus.AWARDED  # all lines now decided
        assert excess_list.close_at is None  # no D1 deadline was ever configured

        excess_service.unaward_offer(db_session, winner1.id, owner)

        db_session.refresh(competitor)
        assert competitor.status == ExcessOfferStatus.LATE


# ═══════════════════════════════════════════════════════════════════════
#  POST withdraw endpoint (M2)
# ═══════════════════════════════════════════════════════════════════════


class TestWithdrawRoute:
    def _offer(self, db, excess_list, cap_line, broker):
        offer = _open_offer(
            db, excess_list=excess_list, submitter=broker, line=cap_line, buyer=None, unit_price=Decimal("0.80")
        )
        db.commit()
        return offer

    def test_owner_withdraws_open_offer(
        self, client, db_session: Session, excess_list: ExcessList, cap_line: ExcessLineItem, owner: User, broker: User
    ):
        from app.dependencies import require_user
        from app.main import app

        offer = self._offer(db_session, excess_list, cap_line, broker)
        app.dependency_overrides[require_user] = lambda: owner
        try:
            resp = client.post(f"/api/resell/{excess_list.id}/offers/{offer.id}/withdraw")
        finally:
            app.dependency_overrides.pop(require_user, None)

        assert resp.status_code == 200
        db_session.refresh(offer)
        assert offer.status == ExcessOfferStatus.WITHDRAWN

    def test_submitter_withdraws_own_offer(
        self, client, db_session: Session, excess_list: ExcessList, cap_line: ExcessLineItem, owner: User, broker: User
    ):
        """The offerer (submitted_by) may retract their own bid."""
        from app.dependencies import require_user
        from app.main import app

        offer = self._offer(db_session, excess_list, cap_line, broker)
        app.dependency_overrides[require_user] = lambda: broker
        try:
            resp = client.post(f"/api/resell/{excess_list.id}/offers/{offer.id}/withdraw")
        finally:
            app.dependency_overrides.pop(require_user, None)

        assert resp.status_code == 200
        db_session.refresh(offer)
        assert offer.status == ExcessOfferStatus.WITHDRAWN

    def test_non_owner_non_submitter_forbidden(
        self, client, db_session: Session, excess_list: ExcessList, cap_line: ExcessLineItem, broker: User
    ):
        """The default client user is neither the owner nor the submitter → 403; the
        offer stays open."""
        offer = self._offer(db_session, excess_list, cap_line, broker)
        resp = client.post(f"/api/resell/{excess_list.id}/offers/{offer.id}/withdraw")
        assert resp.status_code == 403
        db_session.refresh(offer)
        assert offer.status == ExcessOfferStatus.OPEN

    def test_withdraw_won_offer_409(
        self, client, db_session: Session, excess_list: ExcessList, cap_line: ExcessLineItem, owner: User, broker: User
    ):
        """A won offer cannot be withdrawn (unaward it first) — 409, stays won."""
        from app.dependencies import require_user
        from app.main import app

        offer = self._offer(db_session, excess_list, cap_line, broker)
        excess_service.award_offer(db_session, offer.id, owner)
        app.dependency_overrides[require_user] = lambda: owner
        try:
            resp = client.post(f"/api/resell/{excess_list.id}/offers/{offer.id}/withdraw")
        finally:
            app.dependency_overrides.pop(require_user, None)

        assert resp.status_code == 409
        db_session.refresh(offer)
        assert offer.status == ExcessOfferStatus.WON

    def test_withdraw_missing_offer_404(self, client, db_session: Session, excess_list: ExcessList, owner: User):
        from app.dependencies import require_user
        from app.main import app

        app.dependency_overrides[require_user] = lambda: owner
        try:
            resp = client.post(f"/api/resell/{excess_list.id}/offers/999999/withdraw")
        finally:
            app.dependency_overrides.pop(require_user, None)
        assert resp.status_code == 404

    def test_offers_tab_shows_withdraw_button_for_open_offer(
        self, client, db_session: Session, excess_list: ExcessList, cap_line: ExcessLineItem, owner: User, broker: User
    ):
        from app.dependencies import require_user
        from app.main import app

        excess_list.status = ExcessListStatus.COLLECTING
        offer = self._offer(db_session, excess_list, cap_line, broker)
        app.dependency_overrides[require_user] = lambda: owner
        try:
            resp = client.get(f"/v2/partials/resell/{excess_list.id}/offers")
        finally:
            app.dependency_overrides.pop(require_user, None)

        assert resp.status_code == 200
        assert f"/offers/{offer.id}/withdraw" in resp.text

    def test_withdrawn_offer_drops_from_offers_tab(
        self, client, db_session: Session, excess_list: ExcessList, cap_line: ExcessLineItem, owner: User, broker: User
    ):
        """Once withdrawn, the offer no longer renders in the re-rendered Offers tab."""
        from app.dependencies import require_user
        from app.main import app

        excess_list.status = ExcessListStatus.COLLECTING
        offer = self._offer(db_session, excess_list, cap_line, broker)
        app.dependency_overrides[require_user] = lambda: owner
        try:
            resp = client.post(f"/api/resell/{excess_list.id}/offers/{offer.id}/withdraw")
        finally:
            app.dependency_overrides.pop(require_user, None)

        assert resp.status_code == 200
        assert f"/offers/{offer.id}/award" not in resp.text
        assert f"/offers/{offer.id}/withdraw" not in resp.text


# ═══════════════════════════════════════════════════════════════════════
#  award_offer / withdraw_offer — service-level status guards (Phase 1)
# ═══════════════════════════════════════════════════════════════════════


class TestAwardOfferStatusGuard:
    """award_offer only acts on an in-play offer (open/late).

    A withdrawn or lost offer is already closed — awarding it would resurrect a dead
    bid. A WON offer returns early via the idempotency guard, so it never reaches this
    precondition.
    """

    @pytest.mark.parametrize("dead_status", [ExcessOfferStatus.WITHDRAWN, ExcessOfferStatus.LOST])
    def test_award_dead_offer_409(
        self,
        db_session: Session,
        excess_list: ExcessList,
        cap_line: ExcessLineItem,
        owner: User,
        broker: User,
        dead_status,
    ):
        offer = _open_offer(
            db_session, excess_list=excess_list, submitter=broker, line=cap_line, buyer=None, unit_price=Decimal("0.80")
        )
        offer.status = dead_status
        db_session.commit()

        with pytest.raises(HTTPException) as exc:
            excess_service.award_offer(db_session, offer.id, owner)

        assert exc.value.status_code == 409
        db_session.refresh(offer)
        assert offer.status == dead_status  # unchanged
        db_session.refresh(cap_line)
        assert cap_line.status != ExcessLineItemStatus.AWARDED

    def test_award_late_offer_wins(
        self, db_session: Session, excess_list: ExcessList, cap_line: ExcessLineItem, owner: User, broker: User
    ):
        """A LATE offer (landed after the window closed) is still awardable."""
        offer = _open_offer(
            db_session, excess_list=excess_list, submitter=broker, line=cap_line, buyer=None, unit_price=Decimal("0.80")
        )
        offer.status = ExcessOfferStatus.LATE
        db_session.commit()

        result = excess_service.award_offer(db_session, offer.id, owner)

        assert result.status == ExcessOfferStatus.WON
        db_session.refresh(cap_line)
        assert cap_line.status == ExcessLineItemStatus.AWARDED


class TestWithdrawOfferServiceGuard:
    """withdraw_offer must enforce the same (open/late) precondition the router does — a
    direct service call on a won offer is a 409 (unaward it first), not a silent flip
    that would strand its awarded lines."""

    def test_withdraw_won_offer_service_409(
        self, db_session: Session, excess_list: ExcessList, cap_line: ExcessLineItem, owner: User, broker: User
    ):
        offer = _open_offer(
            db_session, excess_list=excess_list, submitter=broker, line=cap_line, buyer=None, unit_price=Decimal("0.80")
        )
        db_session.commit()
        excess_service.award_offer(db_session, offer.id, owner)  # -> won
        db_session.refresh(offer)
        assert offer.status == ExcessOfferStatus.WON

        with pytest.raises(HTTPException) as exc:
            excess_service.withdraw_offer(db_session, offer.id)

        assert exc.value.status_code == 409
        db_session.refresh(offer)
        assert offer.status == ExcessOfferStatus.WON  # not withdrawn

    def test_withdraw_open_offer_service_ok(
        self, db_session: Session, excess_list: ExcessList, cap_line: ExcessLineItem, owner: User, broker: User
    ):
        """The happy path still works — an open offer withdraws cleanly to withdrawn."""
        offer = _open_offer(
            db_session, excess_list=excess_list, submitter=broker, line=cap_line, buyer=None, unit_price=Decimal("0.80")
        )
        db_session.commit()

        result = excess_service.withdraw_offer(db_session, offer.id)

        assert result.status == ExcessOfferStatus.WITHDRAWN

    def test_withdraw_offer_locks_list_for_serialization(
        self,
        db_session: Session,
        excess_list: ExcessList,
        cap_line: ExcessLineItem,
        owner: User,
        broker: User,
        monkeypatch,
    ):
        """withdraw_offer must take the same list/line lock award/unaward use (M9)
        BEFORE its status guard, so a concurrent award can't commit (offer->won,
        line->awarded) between an unlocked read and the withdraw's UPDATE and strand an
        awarded line on a withdrawn offer.

        Spy the lock hook to prove it's wired (with_for_update itself is a no-op on the
        SQLite test engine, so the race is unobservable here — this guards the hook
        against regression).
        """
        offer = _open_offer(
            db_session, excess_list=excess_list, submitter=broker, line=cap_line, buyer=None, unit_price=Decimal("0.80")
        )
        db_session.commit()

        calls: list[tuple[int, int]] = []
        real_lock = excess_service._lock_list_for_award

        def _spy(db, off, list_id):
            calls.append((off.id, list_id))
            return real_lock(db, off, list_id)

        monkeypatch.setattr(excess_service, "_lock_list_for_award", _spy)

        excess_service.withdraw_offer(db_session, offer.id)

        assert calls == [(offer.id, excess_list.id)]


# ═══════════════════════════════════════════════════════════════════════
#  assign_offer_line (Task 4 / finding #15) — salvage an unmatched offer line
# ═══════════════════════════════════════════════════════════════════════


def _unmatched_offer_line(
    db: Session, *, excess_list: ExcessList, submitter: User, mpn_raw: str, unit_price: Decimal
) -> ExcessOfferLine:
    """An OPEN per-line offer carrying ONE unmatched line (excess_line_item_id=None)."""
    offer = ExcessOffer(
        excess_list_id=excess_list.id,
        submitted_by=submitter.id,
        scope="per_line",
        status=ExcessOfferStatus.OPEN,
    )
    db.add(offer)
    db.flush()
    line = ExcessOfferLine(
        offer_id=offer.id,
        excess_line_item_id=None,
        mpn_raw=mpn_raw,
        quantity=50,
        unit_price=unit_price,
        match_status=OfferLineMatchStatus.UNMATCHED,
    )
    db.add(line)
    db.flush()
    return line


class TestAssignOfferLineService:
    def test_assign_matches_and_rolls_up_target(
        self, db_session: Session, excess_list: ExcessList, cap_line: ExcessLineItem, owner: User, broker: User
    ):
        """Assigning an unmatched line to a target line flips it MATCHED, links it, and
        recomputes the target's best-price rollup (the salvaged bid now owns the
        line)."""
        offer_line = _unmatched_offer_line(
            db_session, excess_list=excess_list, submitter=broker, mpn_raw="TYPO-GRM188", unit_price=Decimal("0.95")
        )
        db_session.commit()
        assert cap_line.best_offer_id is None  # nothing on the target yet

        result = excess_service.assign_offer_line(db_session, excess_list.id, offer_line.id, cap_line.id, owner)

        assert result.match_status == OfferLineMatchStatus.MATCHED
        assert result.excess_line_item_id == cap_line.id
        db_session.refresh(cap_line)
        assert cap_line.best_offer_id == offer_line.offer_id
        assert cap_line.best_offer_unit_price == Decimal("0.95")

    def test_assigned_offer_becomes_awardable(
        self, db_session: Session, excess_list: ExcessList, cap_line: ExcessLineItem, owner: User, broker: User
    ):
        """The salvaged offer is now a real matched bid → the owner can award it."""
        offer_line = _unmatched_offer_line(
            db_session, excess_list=excess_list, submitter=broker, mpn_raw="TYPO-GRM188", unit_price=Decimal("0.95")
        )
        db_session.commit()
        excess_service.assign_offer_line(db_session, excess_list.id, offer_line.id, cap_line.id, owner)

        awarded = excess_service.award_offer(db_session, offer_line.offer_id, owner)
        assert awarded.status == ExcessOfferStatus.WON
        db_session.refresh(cap_line)
        assert cap_line.status == ExcessLineItemStatus.AWARDED

    def test_target_line_on_other_list_404(
        self,
        db_session: Session,
        excess_list: ExcessList,
        cap_line: ExcessLineItem,
        owner: User,
        broker: User,
        seller_company: Company,
    ):
        """A target line that belongs to a DIFFERENT list is rejected — 404."""
        other_list = ExcessList(company_id=seller_company.id, owner_id=owner.id, title="Other")
        db_session.add(other_list)
        db_session.flush()
        stray = _line(db_session, other_list, "STRAY-PART")
        offer_line = _unmatched_offer_line(
            db_session, excess_list=excess_list, submitter=broker, mpn_raw="TYPO", unit_price=Decimal("0.95")
        )
        db_session.commit()

        with pytest.raises(HTTPException) as exc:
            excess_service.assign_offer_line(db_session, excess_list.id, offer_line.id, stray.id, owner)
        assert exc.value.status_code == 404
        db_session.refresh(offer_line)
        assert offer_line.match_status == OfferLineMatchStatus.UNMATCHED  # untouched

    def test_offer_line_on_other_list_404(
        self,
        db_session: Session,
        excess_list: ExcessList,
        cap_line: ExcessLineItem,
        owner: User,
        broker: User,
        seller_company: Company,
    ):
        """An offer line whose offer is on a DIFFERENT list is not assignable here —
        404."""
        other_list = ExcessList(company_id=seller_company.id, owner_id=owner.id, title="Other")
        db_session.add(other_list)
        db_session.flush()
        stray_line = _unmatched_offer_line(
            db_session, excess_list=other_list, submitter=broker, mpn_raw="TYPO", unit_price=Decimal("0.95")
        )
        db_session.commit()

        with pytest.raises(HTTPException) as exc:
            excess_service.assign_offer_line(db_session, excess_list.id, stray_line.id, cap_line.id, owner)
        assert exc.value.status_code == 404

    def test_non_owner_403(
        self,
        db_session: Session,
        excess_list: ExcessList,
        cap_line: ExcessLineItem,
        owner: User,
        broker: User,
        outsider: User,
    ):
        offer_line = _unmatched_offer_line(
            db_session, excess_list=excess_list, submitter=broker, mpn_raw="TYPO", unit_price=Decimal("0.95")
        )
        db_session.commit()
        with pytest.raises(HTTPException) as exc:
            excess_service.assign_offer_line(db_session, excess_list.id, offer_line.id, cap_line.id, outsider)
        assert exc.value.status_code == 403

    def test_reassign_moves_and_recomputes_both_lines(
        self, db_session: Session, excess_list: ExcessList, owner: User, broker: User
    ):
        """Re-assigning to a different line recomputes BOTH the old and the new target
        (the old loses the bid, the new gains it)."""
        a = _line(db_session, excess_list, "REASSIGN-A")
        b = _line(db_session, excess_list, "REASSIGN-B")
        offer_line = _unmatched_offer_line(
            db_session, excess_list=excess_list, submitter=broker, mpn_raw="TYPO", unit_price=Decimal("1.10")
        )
        db_session.commit()

        excess_service.assign_offer_line(db_session, excess_list.id, offer_line.id, a.id, owner)
        db_session.refresh(a)
        assert a.best_offer_id == offer_line.offer_id  # A owns it after the first assign

        excess_service.assign_offer_line(db_session, excess_list.id, offer_line.id, b.id, owner)

        db_session.refresh(a)
        db_session.refresh(b)
        assert a.best_offer_id is None  # A recomputed — the bid moved away
        assert a.offer_count == 0
        assert b.best_offer_id == offer_line.offer_id  # B now owns it
        assert b.best_offer_unit_price == Decimal("1.10")

    @pytest.mark.parametrize(
        "blocked_status",
        [ExcessListStatus.AWARDED, ExcessListStatus.CLOSED, ExcessListStatus.EXPIRED],
    )
    def test_assign_on_resolved_list_409(
        self,
        db_session: Session,
        excess_list: ExcessList,
        cap_line: ExcessLineItem,
        owner: User,
        broker: User,
        blocked_status,
    ):
        """Assign is unmatched-queue resolution — a resolved/terminal list (awarded,
        closed, expired) must reject it (finding #2), closing the 'second vector' to a
        reopen (finding #4).

        The line stays unmatched.
        """
        offer_line = _unmatched_offer_line(
            db_session, excess_list=excess_list, submitter=broker, mpn_raw="TYPO", unit_price=Decimal("0.95")
        )
        excess_list.status = blocked_status
        db_session.commit()

        with pytest.raises(HTTPException) as exc:
            excess_service.assign_offer_line(db_session, excess_list.id, offer_line.id, cap_line.id, owner)
        assert exc.value.status_code == 409
        db_session.refresh(offer_line)
        assert offer_line.match_status == OfferLineMatchStatus.UNMATCHED  # untouched

    def test_assign_won_offer_line_409(
        self, db_session: Session, excess_list: ExcessList, cap_line: ExcessLineItem, owner: User, broker: User
    ):
        """A line whose parent offer is WON (awarded) can't be reassigned — that would
        strand the award's line linkage (finding #2).

        Only an open/late offer's line is assignable.
        """
        offer_line = _unmatched_offer_line(
            db_session, excess_list=excess_list, submitter=broker, mpn_raw="TYPO", unit_price=Decimal("0.95")
        )
        db_session.get(ExcessOffer, offer_line.offer_id).status = ExcessOfferStatus.WON
        db_session.commit()

        with pytest.raises(HTTPException) as exc:
            excess_service.assign_offer_line(db_session, excess_list.id, offer_line.id, cap_line.id, owner)
        assert exc.value.status_code == 409
        db_session.refresh(offer_line)
        assert offer_line.match_status == OfferLineMatchStatus.UNMATCHED  # untouched

    def test_assign_onto_awarded_target_line_409_winner_intact(
        self, db_session: Session, excess_list: ExcessList, owner: User, broker: User
    ):
        """Finding #12: assigning an unmatched bid onto an already-AWARDED line must 409
        from the TARGET-LINE guard — never silently displace the winner from
        ``best_offer_id``.

        A second, still-undecided line keeps the award partial, so the list stays BID_OUT
        (not blocked by ``_ASSIGN_BLOCKED_LIST_STATUSES``, which only blocks
        awarded/closed/expired at the LIST level) and the assign gets past the list-level
        guard to the new ``target.status == AWARDED`` check — the exact non-concurrent
        scenario the finding describes.
        """
        winner_line = _line(db_session, excess_list, "AWARDED-TARGET")
        _line(db_session, excess_list, "STILL-OPEN")  # keeps the award partial
        excess_list.status = ExcessListStatus.BID_OUT
        winner_offer = _open_offer(
            db_session,
            excess_list=excess_list,
            submitter=broker,
            line=winner_line,
            buyer=None,
            unit_price=Decimal("1.00"),
        )
        db_session.commit()
        excess_service.award_offer(db_session, winner_offer.id, owner)
        db_session.refresh(winner_line)
        db_session.refresh(excess_list)
        assert winner_line.status == ExcessLineItemStatus.AWARDED  # precondition
        assert winner_line.best_offer_id == winner_offer.id
        # Partial award: list must NOT be AWARDED, or the 409 below would come from the
        # pre-existing list-level guard instead of the target-line guard under test.
        assert excess_list.status == ExcessListStatus.BID_OUT

        offer_line = _unmatched_offer_line(
            db_session, excess_list=excess_list, submitter=broker, mpn_raw="TYPO-HIGHER", unit_price=Decimal("5.00")
        )
        db_session.commit()

        with pytest.raises(HTTPException) as exc:
            excess_service.assign_offer_line(db_session, excess_list.id, offer_line.id, winner_line.id, owner)
        assert exc.value.status_code == 409
        assert "already awarded" in exc.value.detail  # the target-line guard, not the list-level one
        db_session.refresh(offer_line)
        db_session.refresh(winner_line)
        assert offer_line.match_status == OfferLineMatchStatus.UNMATCHED  # untouched
        assert winner_line.best_offer_id == winner_offer.id  # winner marker intact
        assert winner_line.best_offer_unit_price == Decimal("1.00")  # not displaced by 5.00

    def test_assign_onto_withdrawn_target_line_409(
        self, db_session: Session, excess_list: ExcessList, owner: User, broker: User
    ):
        """Finding #12, WITHDRAWN branch: a withdrawn line can't accept new offers."""
        withdrawn_line = _line(db_session, excess_list, "PULLED")
        withdrawn_line.status = ExcessLineItemStatus.WITHDRAWN
        excess_list.status = ExcessListStatus.BID_OUT
        offer_line = _unmatched_offer_line(
            db_session, excess_list=excess_list, submitter=broker, mpn_raw="TYPO", unit_price=Decimal("0.95")
        )
        db_session.commit()

        with pytest.raises(HTTPException) as exc:
            excess_service.assign_offer_line(db_session, excess_list.id, offer_line.id, withdrawn_line.id, owner)
        assert exc.value.status_code == 409
        assert "has been withdrawn" in exc.value.detail
        db_session.refresh(offer_line)
        assert offer_line.match_status == OfferLineMatchStatus.UNMATCHED  # untouched

    def test_assign_onto_available_line_on_bid_out_list_still_works(
        self, db_session: Session, excess_list: ExcessList, cap_line: ExcessLineItem, owner: User, broker: User
    ):
        """Control: an available (undecided) target line on a BID_OUT list still accepts
        an assign — the new AWARDED/WITHDRAWN target guard must not over-block."""
        excess_list.status = ExcessListStatus.BID_OUT
        offer_line = _unmatched_offer_line(
            db_session, excess_list=excess_list, submitter=broker, mpn_raw="TYPO", unit_price=Decimal("0.95")
        )
        db_session.commit()

        result = excess_service.assign_offer_line(db_session, excess_list.id, offer_line.id, cap_line.id, owner)

        assert result.match_status == OfferLineMatchStatus.MATCHED
        db_session.refresh(cap_line)
        assert cap_line.best_offer_id == offer_line.offer_id


class TestAssignOfferLineRoute:
    def test_assign_route_200_and_salvages(
        self, client, db_session: Session, excess_list: ExcessList, cap_line: ExcessLineItem, owner: User, broker: User
    ):
        from app.dependencies import require_user
        from app.main import app

        excess_list.status = ExcessListStatus.COLLECTING
        offer_line = _unmatched_offer_line(
            db_session, excess_list=excess_list, submitter=broker, mpn_raw="TYPO-GRM188", unit_price=Decimal("0.95")
        )
        db_session.commit()

        app.dependency_overrides[require_user] = lambda: owner
        try:
            resp = client.post(
                f"/api/resell/{excess_list.id}/offer-lines/{offer_line.id}/assign",
                data={"target_line_item_id": str(cap_line.id)},
            )
        finally:
            app.dependency_overrides.pop(require_user, None)

        assert resp.status_code == 200
        db_session.refresh(offer_line)
        assert offer_line.match_status == OfferLineMatchStatus.MATCHED
        assert offer_line.excess_line_item_id == cap_line.id

    def test_assign_route_non_owner_403(
        self, client, db_session: Session, excess_list: ExcessList, cap_line: ExcessLineItem, broker: User
    ):
        """The default client user is not the owner → 403 (the line stays unmatched)."""
        offer_line = _unmatched_offer_line(
            db_session, excess_list=excess_list, submitter=broker, mpn_raw="TYPO", unit_price=Decimal("0.95")
        )
        db_session.commit()
        resp = client.post(
            f"/api/resell/{excess_list.id}/offer-lines/{offer_line.id}/assign",
            data={"target_line_item_id": str(cap_line.id)},
        )
        assert resp.status_code == 403
        db_session.refresh(offer_line)
        assert offer_line.match_status == OfferLineMatchStatus.UNMATCHED

    def test_offers_tab_renders_assign_control_for_unmatched(
        self, client, db_session: Session, excess_list: ExcessList, cap_line: ExcessLineItem, owner: User, broker: User
    ):
        """The unmatched queue surfaces an assign form pointed at the offer line + a
        target option for the posted line (the manual-resolution affordance)."""
        from app.dependencies import require_user
        from app.main import app

        excess_list.status = ExcessListStatus.COLLECTING
        offer_line = _unmatched_offer_line(
            db_session, excess_list=excess_list, submitter=broker, mpn_raw="TYPO-GRM188", unit_price=Decimal("0.95")
        )
        db_session.commit()

        app.dependency_overrides[require_user] = lambda: owner
        try:
            body = client.get(f"/v2/partials/resell/{excess_list.id}/offers").text
        finally:
            app.dependency_overrides.pop(require_user, None)

        assert f"/offer-lines/{offer_line.id}/assign" in body
        assert f'value="{cap_line.id}"' in body  # the posted line is an assign target


# ═══════════════════════════════════════════════════════════════════════
#  RESELL-TEST-3 — M9 award-race concurrency (PostgreSQL row lock)
# ═══════════════════════════════════════════════════════════════════════


@requires_postgres
class TestAwardRaceConcurrency:
    """Proves the M9 ``_lock_list_for_award`` ``with_for_update`` lock SERIALIZES two
    concurrent awards touching the same line (a no-op on SQLite, so this is PG-only).

    Two independent sessions on the real PG engine each award a DIFFERENT open offer on the
    SAME line, released together by a ``threading.Barrier`` so both enter the award path at
    once. The row lock forces one to block until the other commits, so exactly ONE award
    wins and the other fails the already-awarded guard (409) — never a double-award. Without
    the lock both would read the line as available and both win.
    """

    @staticmethod
    def _truncate_all(pg_engine) -> None:
        all_tables = ", ".join(f'"{name}"' for name in Base.metadata.tables)
        with pg_engine.begin() as conn:
            conn.execute(sa_text(f"TRUNCATE {all_tables} RESTART IDENTITY CASCADE"))

    def test_concurrent_award_serialized_one_wins_one_409(self, pg_engine):
        session_factory = sessionmaker(bind=pg_engine, autoflush=False, expire_on_commit=True)

        # The ENTIRE body is wrapped so ``_truncate_all`` runs even when an assertion below
        # raises. This test exists to catch a lock regression, and on that regression the
        # pass/fail assertions raise BEFORE any success-path cleanup would run — leaving the
        # seeded rows on the session-scoped ``pg_engine`` to pollute every later
        # ``@requires_postgres`` test. Cleanup MUST be on the outer ``finally``, not the
        # success path, so a real regression fails THIS test only and cascades to no other.
        try:
            # ── Seed one list + one line + two open offers (distinct buyer cards) ──
            setup = session_factory()
            try:
                owner = User(
                    email="race-owner@trioscs.com", name="Race Owner", role="trader", azure_id="race-owner-001"
                )
                setup.add(owner)
                setup.flush()
                co = Company(name="Race Seller Co")
                setup.add(co)
                setup.flush()
                el = ExcessList(
                    company_id=co.id, owner_id=owner.id, title="Race Excess", status=ExcessListStatus.COLLECTING
                )
                setup.add(el)
                setup.flush()
                mc = MaterialCard(normalized_mpn="grm188r", display_mpn="GRM188R", category="capacitors")
                setup.add(mc)
                setup.flush()
                line = ExcessLineItem(
                    excess_list_id=el.id,
                    part_number="GRM188R",
                    quantity=1000,
                    material_card_id=mc.id,
                    asking_price=Decimal("1.00"),
                )
                setup.add(line)
                setup.flush()
                card_a = _buyer_card(setup, "Race Buyer A")
                card_b = _buyer_card(setup, "Race Buyer B")
                offer_a = _open_offer(
                    setup, excess_list=el, submitter=owner, line=line, buyer=card_a, unit_price=Decimal("0.80")
                )
                offer_b = _open_offer(
                    setup, excess_list=el, submitter=owner, line=line, buyer=card_b, unit_price=Decimal("0.90")
                )
                setup.commit()
                owner_id, line_id = owner.id, line.id
                offer_a_id, offer_b_id = offer_a.id, offer_b.id
                card_ids = [card_a.id, card_b.id]
            finally:
                setup.close()

            # ── Two workers race the award, released together by the barrier ──
            barrier = threading.Barrier(2)
            results: dict[str, tuple[str, object]] = {}

            def _worker(name: str, offer_id: int) -> None:
                worker_db = session_factory()
                try:
                    worker_owner = worker_db.get(User, owner_id)
                    barrier.wait(timeout=20)
                    try:
                        res = excess_service.award_offer(worker_db, offer_id, worker_owner)
                        results[name] = ("won", res.status)
                    except HTTPException as exc:
                        results[name] = ("http", exc.status_code)
                    except Exception as exc:  # pragma: no cover - surfaced in the assert below
                        results[name] = ("error", repr(exc))
                finally:
                    worker_db.close()

            t1 = threading.Thread(target=_worker, args=("a", offer_a_id))
            t2 = threading.Thread(target=_worker, args=("b", offer_b_id))
            t1.start()
            t2.start()
            t1.join(timeout=25)
            t2.join(timeout=25)

            # ── Exactly one won; the other got a 409 (never a double-award, never an error) ──
            assert set(results) == {"a", "b"}, f"a worker did not finish: {results}"
            won = [n for n, r in results.items() if r[0] == "won"]
            http = [r[1] for r in results.values() if r[0] == "http"]
            errors = [r for r in results.values() if r[0] == "error"]
            assert not errors, f"unexpected worker error: {results}"
            assert len(won) == 1, f"expected exactly ONE award to win, got {results}"
            assert http == [409], f"expected the loser to raise HTTPException(409), got {results}"

            # ── Final DB state: the line is awarded ONCE, exactly one offer WON, score fires once ──
            check = session_factory()
            try:
                assert check.get(ExcessLineItem, line_id).status == ExcessLineItemStatus.AWARDED
                won_offers = [
                    oid
                    for oid in (offer_a_id, offer_b_id)
                    if check.get(ExcessOffer, oid).status == ExcessOfferStatus.WON
                ]
                assert won_offers == won_offers[:1], f"expected exactly one WON offer, got {won_offers}"
                assert len(won_offers) == 1
                scores = check.query(BuyerScore).filter(BuyerScore.vendor_card_id.in_(card_ids)).all()
                assert len(scores) == 1, f"the win-hook must fire once (one BuyerScore), got {len(scores)}"
                assert scores[0].wins == 1
            finally:
                check.close()
        finally:
            self._truncate_all(pg_engine)
