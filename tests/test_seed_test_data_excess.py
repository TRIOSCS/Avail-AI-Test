"""Tests for the rewritten excess section of scripts/seed_test_data.py.

Verifies seed_excess_lists writes only post-rework shapes (deep-review-2 findings
#29/#63): no removed ExcessLineItem kwargs (the old market_price/demand_score
TypeError), no legacy active/bidding statuses, offers submitted by a non-owner broker
through excess_service.submit_offer (real rollups), and the awarded list derived
through excess_service.award_offer (WON offer owns the awarded lines).

Called by: pytest
Depends on: tests.conftest (db_session), scripts.seed_test_data, excess models
"""

import os

from sqlalchemy.orm import Session

os.environ.setdefault("TESTING", "1")

from app.constants import (
    ExcessLineItemStatus,
    ExcessListStatus,
    ExcessOfferStatus,
    UserRole,
)
from app.models.auth import User
from app.models.excess import ExcessLineItem, ExcessList, ExcessOffer
from scripts import seed_test_data as std


def _seed_prereqs(db: Session):
    admin = User(email="seed-admin@example.com", name="Seed Admin", role=UserRole.ADMIN, is_active=True)
    db.add(admin)
    db.flush()
    companies, sites = std.seed_companies_and_sites(db, admin)
    return admin, companies, sites


def _seed(db: Session):
    admin, companies, sites = _seed_prereqs(db)
    std.seed_excess_lists(db, admin, companies, sites)
    db.commit()
    return admin


class TestSeedExcessLists:
    def test_seeds_post_rework_statuses_only(self, db_session: Session):
        _seed(db_session)
        lists = db_session.query(ExcessList).all()
        assert len(lists) == 5
        statuses = {el.status for el in lists}
        # The legacy pre-rework statuses must never be seeded again (finding #63a;
        # W1.9 removed active/legacy-bidding; W3's migration 207 retired
        # open/collecting/bid_out/expired for the 5-state ladder).
        assert {"active", "open", "collecting", "bid_out", "expired"} & statuses == set()
        assert statuses == {
            ExcessListStatus.DRAFT.value,
            ExcessListStatus.POSTED.value,
            ExcessListStatus.BIDDING.value,
            ExcessListStatus.AWARDED.value,
            ExcessListStatus.CLOSED.value,
        }

    def test_offers_come_from_a_non_owner_broker(self, db_session: Session):
        admin = _seed(db_session)
        offers = db_session.query(ExcessOffer).all()
        assert offers, "seed must submit at least one inbound offer"
        for offer in offers:
            el = db_session.get(ExcessList, offer.excess_list_id)
            assert offer.submitted_by != el.owner_id, "self-offer forbidden (Phase-1 guard)"
            assert offer.submitted_by != admin.id
            assert offer.offerer_vendor_card_id is not None, "offers carry buyer attribution"

    def test_collecting_list_has_real_rollups(self, db_session: Session):
        _seed(db_session)
        collecting = db_session.query(ExcessList).filter_by(status=ExcessListStatus.BIDDING.value).one()
        lines = db_session.query(ExcessLineItem).filter_by(excess_list_id=collecting.id).all()
        priced = [ln for ln in lines if ln.best_offer_unit_price is not None]
        assert priced, "submit_offer must produce genuine best-price rollups"
        for ln in priced:
            assert ln.offer_count >= 1
            assert db_session.get(ExcessOffer, ln.best_offer_id) is not None

    def test_awarded_list_satisfies_award_invariant(self, db_session: Session):
        _seed(db_session)
        awarded = db_session.query(ExcessList).filter_by(status=ExcessListStatus.AWARDED.value).one()
        offers = db_session.query(ExcessOffer).filter_by(excess_list_id=awarded.id).all()
        won = [o for o in offers if o.status == ExcessOfferStatus.WON.value]
        assert len(won) == 1, "the awarded list must carry exactly one WON offer"
        # Competing offers were genuinely closed by award_offer, never left dangling open.
        assert all(o.status != ExcessOfferStatus.OPEN.value for o in offers)
        lines = db_session.query(ExcessLineItem).filter_by(excess_list_id=awarded.id).all()
        assert lines
        assert all(ln.status == ExcessLineItemStatus.AWARDED.value for ln in lines)

    def test_idempotent_rerun_creates_nothing(self, db_session: Session):
        admin = _seed(db_session)
        before_lists = db_session.query(ExcessList).count()
        before_offers = db_session.query(ExcessOffer).count()
        companies, sites = std.seed_companies_and_sites(db_session, admin)
        std.seed_excess_lists(db_session, admin, companies, sites)
        db_session.commit()
        assert db_session.query(ExcessList).count() == before_lists
        assert db_session.query(ExcessOffer).count() == before_offers
