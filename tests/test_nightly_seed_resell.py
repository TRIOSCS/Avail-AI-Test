"""tests/test_nightly_seed_resell.py — Coverage tests for
app/management/seed_resell_demo.py.

Tests the idempotent demo-seeder's helper functions and the main seed() / _reset() entry
points with the test DB, mocking external service calls (excess_service, excess_mirror)
that would talk to the real supplier APIs.

Called by: pytest (nightly coverage run) Depends on: conftest (db_session)
"""

import os
from datetime import UTC, timedelta
from unittest.mock import patch

from sqlalchemy.orm import Session

os.environ.setdefault("TESTING", "1")

from app.constants import ExcessLineItemStatus, ExcessListStatus, ExcessOfferStatus, UserRole
from app.management.seed_resell_demo import (
    _BROKER_EMAIL,
    _CUSTOMER_NAME,
    _LIST_AWARDED,
    _LIST_COLLECTING,
    _LIST_ONEOFF,
    _TRADER_EMAIL,
    _build_awarded,
    _build_collecting,
    _build_oneoff,
    _get_or_create_company,
    _get_or_create_list,
    _get_or_create_user,
    _list_has_offers,
    _now,
    _reset,
    seed,
)
from app.models import Company, User
from app.models.excess import BuyerScore, ExcessLineItem, ExcessList, ExcessOffer

# ── Pure helper tests ─────────────────────────────────────────────────────────


def test_now_returns_utc_datetime():
    ts = _now()
    assert ts.tzinfo is not None
    assert ts.year >= 2025


class TestGetOrCreateUser:
    def test_creates_new_user(self, db_session: Session):
        email = "seed-test-create@example.com"
        user = _get_or_create_user(db_session, email, "Test User", UserRole.BUYER)
        db_session.flush()
        assert user.email == email
        assert user.role == UserRole.BUYER

    def test_returns_existing_user(self, db_session: Session):
        email = "seed-test-existing@example.com"
        u1 = _get_or_create_user(db_session, email, "User One", UserRole.BUYER)
        db_session.flush()
        u2 = _get_or_create_user(db_session, email, "User Two", UserRole.TRADER)
        db_session.flush()
        assert u1.id == u2.id


class TestGetOrCreateCompany:
    def test_creates_new_company(self, db_session: Session):
        name = "Seed Test Company Inc."
        co = _get_or_create_company(db_session, name)
        db_session.flush()
        assert co.name == name

    def test_returns_existing_company(self, db_session: Session):
        name = "Seed Test Company Duplicate"
        co1 = _get_or_create_company(db_session, name)
        db_session.flush()
        co2 = _get_or_create_company(db_session, name)
        db_session.flush()
        assert co1.id == co2.id


class TestGetOrCreateList:
    def _make_owner(self, db: Session) -> User:
        u = User(
            email="seed-list-owner@example.com",
            name="List Owner",
            role=UserRole.TRADER,
            created_at=_now(),
        )
        db.add(u)
        db.flush()
        return u

    def _make_company(self, db: Session) -> Company:
        co = Company(name="List Co", account_type="Customer", is_active=True, created_at=_now())
        db.add(co)
        db.flush()
        return co

    def test_creates_new_list(self, db_session: Session):
        owner = self._make_owner(db_session)
        co = self._make_company(db_session)
        el, created = _get_or_create_list(
            db_session,
            title="Seed Test List",
            company=co,
            owner=owner,
            status=ExcessListStatus.BIDDING,
            close_in_days=5,
        )
        db_session.flush()
        assert created is True
        assert el.title == "Seed Test List"
        assert el.status == ExcessListStatus.BIDDING

    def test_returns_existing_list(self, db_session: Session):
        owner = self._make_owner(db_session)
        co = self._make_company(db_session)
        el1, c1 = _get_or_create_list(
            db_session,
            title="Seed Idempotent List",
            company=co,
            owner=owner,
            status=ExcessListStatus.DRAFT,
            close_in_days=None,
        )
        db_session.flush()
        el2, c2 = _get_or_create_list(
            db_session,
            title="Seed Idempotent List",
            company=co,
            owner=owner,
            status=ExcessListStatus.POSTED,
            close_in_days=None,
        )
        db_session.flush()
        assert c1 is True
        assert c2 is False
        assert el1.id == el2.id

    def test_no_close_at_when_close_in_days_none(self, db_session: Session):
        owner = self._make_owner(db_session)
        co = self._make_company(db_session)
        el, _ = _get_or_create_list(
            db_session,
            title="Seed No Close Date",
            company=co,
            owner=owner,
            status=ExcessListStatus.AWARDED,
            close_in_days=None,
        )
        db_session.flush()
        if hasattr(el, "close_at"):
            assert el.close_at is None


class TestListHasOffers:
    def test_no_offers_returns_false(self, db_session: Session):
        owner = User(email="ho-owner@example.com", name="HO", role="trader", created_at=_now())
        db_session.add(owner)
        co = Company(name="HO Co", account_type="Customer", is_active=True, created_at=_now())
        db_session.add(co)
        db_session.flush()
        el = ExcessList(
            title="HO Test",
            company_id=co.id,
            owner_id=owner.id,
            status=ExcessListStatus.BIDDING,
            created_at=_now(),
        )
        db_session.add(el)
        db_session.flush()
        assert _list_has_offers(db_session, el) is False


# ── Build helpers (with mocked external calls) ────────────────────────────────


def _make_seed_users(db: Session):
    trader = User(
        email=_TRADER_EMAIL,
        name="Demo Trader",
        role=UserRole.TRADER,
        created_at=_now(),
    )
    db.add(trader)
    broker = User(
        email=_BROKER_EMAIL,
        name="Demo Broker",
        role=UserRole.BUYER,
        created_at=_now(),
    )
    db.add(broker)
    company = Company(name=_CUSTOMER_NAME, account_type="Customer", is_active=True, created_at=_now())
    db.add(company)
    db.flush()
    return trader, broker, company


class TestBuildAwarded:
    """The awarded demo derives through the REAL submit_offer + award_offer chain (deep-
    review-2 finding #60) — no mocks, so the WON offer / rollups / line statuses are
    asserted as the genuine service output."""

    def test_creates_awarded_list_through_award_offer(self, db_session: Session):
        trader, broker, company = _make_seed_users(db_session)
        _build_awarded(db_session, company, trader, broker)
        el = db_session.query(ExcessList).filter_by(title=_LIST_AWARDED).one()
        assert el.status == ExcessListStatus.AWARDED

        offers = db_session.query(ExcessOffer).filter_by(excess_list_id=el.id).all()
        won = [o for o in offers if o.status == ExcessOfferStatus.WON]
        assert len(won) == 1, "the awarded demo must carry exactly one WON offer"
        assert won[0].submitted_by == broker.id
        assert won[0].submitted_by != el.owner_id
        assert won[0].offerer_vendor_card_id is not None, "the demo award carries buyer attribution"

        lines = db_session.query(ExcessLineItem).filter_by(excess_list_id=el.id).all()
        assert lines
        assert all(ln.status == ExcessLineItemStatus.AWARDED for ln in lines)
        # Rollups derive from the real chokepoint, never hand-stamped.
        assert all(ln.best_offer_id == won[0].id for ln in lines)
        assert all(ln.offer_count == 1 for ln in lines)

        # The win-hook produced a genuine BuyerScore for the attributed buyer.
        score = db_session.query(BuyerScore).filter_by(vendor_card_id=won[0].offerer_vendor_card_id).one()
        assert score.wins >= 1

    def test_idempotent(self, db_session: Session):
        trader, broker, company = _make_seed_users(db_session)
        _build_awarded(db_session, company, trader, broker)
        _build_awarded(db_session, company, trader, broker)
        assert db_session.query(ExcessList).filter_by(title=_LIST_AWARDED).count() == 1
        el = db_session.query(ExcessList).filter_by(title=_LIST_AWARDED).one()
        assert db_session.query(ExcessOffer).filter_by(excess_list_id=el.id).count() == 1

    def test_heals_legacy_hand_stamped_awarded_demo(self, db_session: Session):
        """The OLD seeder hand-stamped the awarded demo (list AWARDED, lines AWARDED,
        ZERO offers) — the exact deployed state that motivated finding #60.

        A re-seed must heal it through the real chain, not crash in award_offer with a
        409 on the already-awarded lines (after having committed a dangling late offer).
        """
        trader, broker, company = _make_seed_users(db_session)
        el = ExcessList(
            title=_LIST_AWARDED,
            company_id=company.id,
            owner_id=trader.id,
            status=ExcessListStatus.AWARDED,
            created_at=_now(),
        )
        db_session.add(el)
        db_session.flush()
        for mpn in ("XC7A100T-2FGG484I", "XC7K325T-2FFG900C"):
            db_session.add(
                ExcessLineItem(
                    excess_list_id=el.id,
                    part_number=mpn,
                    quantity=100,
                    condition="New",
                    status=ExcessLineItemStatus.AWARDED,  # hand-stamped, no offer behind it
                    created_at=_now(),
                )
            )
        el.total_line_items = 2
        db_session.commit()

        _build_awarded(db_session, company, trader, broker)

        el = db_session.query(ExcessList).filter_by(title=_LIST_AWARDED).one()
        assert el.status == ExcessListStatus.AWARDED
        offers = db_session.query(ExcessOffer).filter_by(excess_list_id=el.id).all()
        won = [o for o in offers if o.status == ExcessOfferStatus.WON]
        assert len(won) == 1, "healing must leave exactly one genuine WON offer"
        assert all(o.status == ExcessOfferStatus.WON for o in offers), "no dangling never-won offers"
        lines = db_session.query(ExcessLineItem).filter_by(excess_list_id=el.id).all()
        assert all(ln.status == ExcessLineItemStatus.AWARDED for ln in lines)
        assert all(ln.best_offer_id == won[0].id for ln in lines), "rollups derive from the WON offer"

        # And the healed state is stable: a further re-seed is a no-op.
        _build_awarded(db_session, company, trader, broker)
        assert db_session.query(ExcessOffer).filter_by(excess_list_id=el.id).count() == 1


class TestBuildCollecting:
    def test_creates_collecting_list(self, db_session: Session):
        trader, broker, company = _make_seed_users(db_session)
        with (
            patch("app.management.seed_resell_demo.excess_service.submit_offer"),
            patch("app.management.seed_resell_demo.excess_service.award_offer"),
            patch("app.management.seed_resell_demo.excess_service._resolve_line_material_card"),
        ):
            _build_collecting(db_session, company, trader, broker)
            db_session.commit()

        el = db_session.query(ExcessList).filter_by(title=_LIST_COLLECTING).one()
        assert el.status == ExcessListStatus.BIDDING

    def test_idempotent_no_duplicate_lists(self, db_session: Session):
        trader, broker, company = _make_seed_users(db_session)
        with (
            patch("app.management.seed_resell_demo.excess_service.submit_offer"),
            patch("app.management.seed_resell_demo.excess_service.award_offer"),
            patch("app.management.seed_resell_demo.excess_service._resolve_line_material_card"),
        ):
            _build_collecting(db_session, company, trader, broker)
            db_session.commit()
            _build_collecting(db_session, company, trader, broker)
            db_session.commit()

        count = db_session.query(ExcessList).filter_by(title=_LIST_COLLECTING).count()
        assert count == 1


class TestCollectingRefresh:
    """Finding #62: a re-seed must re-arm the collecting demo after the nightly expiry
    has decayed it (expired status / lapsed close_at) — find-by-title used to return
    early and leave the flagship demo permanently expired."""

    def _seed_collecting(self, db_session: Session):
        trader, broker, company = _make_seed_users(db_session)
        with (
            patch("app.management.seed_resell_demo.excess_service.submit_offer"),
            patch("app.management.seed_resell_demo.excess_service.award_offer"),
            patch("app.management.seed_resell_demo.excess_service._resolve_line_material_card"),
        ):
            _build_collecting(db_session, company, trader, broker)
            db_session.commit()
        return trader, broker, company

    def test_reseed_restores_decayed_demo(self, db_session: Session):
        trader, broker, company = self._seed_collecting(db_session)
        el = db_session.query(ExcessList).filter_by(title=_LIST_COLLECTING).one()
        # Decay shape post-W3: the terminal 'expired' status is gone (migration 207) —
        # a decayed demo is a bidding list whose close_at lapsed un-actioned.
        el.status = ExcessListStatus.BIDDING
        el.close_at = _now() - timedelta(days=1)
        db_session.commit()

        with (
            patch("app.management.seed_resell_demo.excess_service.submit_offer"),
            patch("app.management.seed_resell_demo.excess_service.award_offer"),
            patch("app.management.seed_resell_demo.excess_service._resolve_line_material_card"),
        ):
            _build_collecting(db_session, company, trader, broker)
            db_session.commit()

        db_session.refresh(el)
        assert el.status == ExcessListStatus.BIDDING
        close_at = el.close_at
        if close_at.tzinfo is None:
            close_at = close_at.replace(tzinfo=UTC)
        assert close_at > _now(), "refresh must push the posting window forward"

    def test_reseed_rearms_lapsed_window_before_nightly_flip(self, db_session: Session):
        trader, broker, company = self._seed_collecting(db_session)
        el = db_session.query(ExcessList).filter_by(title=_LIST_COLLECTING).one()
        # close_at lapsed but the nightly sweep has not flipped the status yet.
        el.close_at = _now() - timedelta(hours=2)
        db_session.commit()

        with (
            patch("app.management.seed_resell_demo.excess_service.submit_offer"),
            patch("app.management.seed_resell_demo.excess_service.award_offer"),
            patch("app.management.seed_resell_demo.excess_service._resolve_line_material_card"),
        ):
            _build_collecting(db_session, company, trader, broker)
            db_session.commit()

        db_session.refresh(el)
        assert el.status == ExcessListStatus.BIDDING
        close_at = el.close_at
        if close_at.tzinfo is None:
            close_at = close_at.replace(tzinfo=UTC)
        assert close_at > _now()

    def test_reseed_leaves_live_demo_untouched(self, db_session: Session):
        trader, broker, company = self._seed_collecting(db_session)
        el = db_session.query(ExcessList).filter_by(title=_LIST_COLLECTING).one()
        assert el.status == ExcessListStatus.BIDDING
        close_at_before = el.close_at

        with (
            patch("app.management.seed_resell_demo.excess_service.submit_offer"),
            patch("app.management.seed_resell_demo.excess_service.award_offer"),
            patch("app.management.seed_resell_demo.excess_service._resolve_line_material_card"),
        ):
            _build_collecting(db_session, company, trader, broker)
            db_session.commit()

        db_session.refresh(el)
        assert el.status == ExcessListStatus.BIDDING
        assert el.close_at == close_at_before, "a live window needs no refresh"

    def test_reseed_never_tramples_an_awarded_demo(self, db_session: Session):
        trader, broker, company = self._seed_collecting(db_session)
        el = db_session.query(ExcessList).filter_by(title=_LIST_COLLECTING).one()
        # A user awarded the demo list in the UI; its window may read as closed.
        el.status = ExcessListStatus.AWARDED
        el.close_at = _now() - timedelta(days=1)
        db_session.commit()

        with (
            patch("app.management.seed_resell_demo.excess_service.submit_offer"),
            patch("app.management.seed_resell_demo.excess_service.award_offer"),
            patch("app.management.seed_resell_demo.excess_service._resolve_line_material_card"),
        ):
            _build_collecting(db_session, company, trader, broker)
            db_session.commit()

        db_session.refresh(el)
        assert el.status == ExcessListStatus.AWARDED, "refresh must not undo a user's award"


class TestBuildOneoff:
    def test_creates_oneoff_list(self, db_session: Session):
        trader, broker, company = _make_seed_users(db_session)
        with (
            patch("app.management.seed_resell_demo.excess_service.submit_offer"),
            patch("app.management.seed_resell_demo.excess_service.award_offer"),
            patch("app.management.seed_resell_demo.excess_service._resolve_line_material_card"),
        ):
            _build_oneoff(db_session, company, trader, broker)
            db_session.commit()

        el = db_session.query(ExcessList).filter_by(title=_LIST_ONEOFF).one()
        assert el.status == ExcessListStatus.POSTED


class TestSeed:
    def test_seed_creates_all_three_lists(self, db_session: Session):
        with (
            patch("app.management.seed_resell_demo.excess_service.submit_offer"),
            patch("app.management.seed_resell_demo.excess_service.award_offer"),
            patch("app.management.seed_resell_demo.excess_service._resolve_line_material_card"),
        ):
            seed(db_session)
            db_session.commit()

        titles = {el.title for el in db_session.query(ExcessList).all()}
        assert _LIST_COLLECTING in titles
        assert _LIST_ONEOFF in titles
        assert _LIST_AWARDED in titles

    def test_seed_idempotent(self, db_session: Session):
        with (
            patch("app.management.seed_resell_demo.excess_service.submit_offer"),
            patch("app.management.seed_resell_demo.excess_service.award_offer"),
            patch("app.management.seed_resell_demo.excess_service._resolve_line_material_card"),
        ):
            seed(db_session)
            db_session.commit()
            seed(db_session)
            db_session.commit()

        count = (
            db_session.query(ExcessList)
            .filter(ExcessList.title.in_([_LIST_COLLECTING, _LIST_ONEOFF, _LIST_AWARDED]))
            .count()
        )
        assert count == 3


class TestReset:
    def test_reset_removes_demo_data(self, db_session: Session):
        with (
            patch("app.management.seed_resell_demo.excess_service.submit_offer"),
            patch("app.management.seed_resell_demo.excess_service.award_offer"),
            patch("app.management.seed_resell_demo.excess_service._resolve_line_material_card"),
        ):
            seed(db_session)
            db_session.commit()

        _reset(db_session)
        count = (
            db_session.query(ExcessList)
            .filter(ExcessList.title.in_([_LIST_COLLECTING, _LIST_ONEOFF, _LIST_AWARDED]))
            .count()
        )
        assert count == 0

    def test_reset_noop_when_no_data(self, db_session: Session):
        # Should not raise even when there's nothing to delete
        _reset(db_session)

    def test_reset_cleans_awarded_artifacts(self, db_session: Session):
        """A real awarded build leaves a buyer VendorCard + BuyerScore (the win-hook);
        reset must remove them along with the lists (finding #60)."""
        from app.management.seed_resell_demo import _BROKER_COMPANY_NAME
        from app.models.vendors import VendorCard
        from app.vendor_utils import normalize_vendor_name

        trader, broker, company = _make_seed_users(db_session)
        _build_awarded(db_session, company, trader, broker)
        norm = normalize_vendor_name(_BROKER_COMPANY_NAME)
        card = db_session.query(VendorCard).filter_by(normalized_name=norm).one()
        assert db_session.query(BuyerScore).filter_by(vendor_card_id=card.id).count() == 1

        _reset(db_session)

        assert db_session.query(ExcessList).filter_by(title=_LIST_AWARDED).count() == 0
        assert db_session.query(VendorCard).filter_by(normalized_name=norm).count() == 0
        assert db_session.query(BuyerScore).filter_by(vendor_card_id=card.id).count() == 0
        assert db_session.query(Company).filter_by(name=_BROKER_COMPANY_NAME).count() == 0
