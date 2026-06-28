"""tests/test_nightly_seed_resell.py — Coverage tests for
app/management/seed_resell_demo.py.

Tests the idempotent demo-seeder's helper functions and the main seed() / _reset() entry
points with the test DB, mocking external service calls (excess_service, excess_mirror)
that would talk to the real supplier APIs.

Called by: pytest (nightly coverage run) Depends on: conftest (db_session)
"""

import os
from unittest.mock import patch

from sqlalchemy.orm import Session

os.environ.setdefault("TESTING", "1")

from app.constants import ExcessListStatus, UserRole
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
from app.models.excess import ExcessList

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
            status=ExcessListStatus.COLLECTING,
            close_in_days=5,
        )
        db_session.flush()
        assert created is True
        assert el.title == "Seed Test List"
        assert el.status == ExcessListStatus.COLLECTING

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
            status=ExcessListStatus.OPEN,
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
            status=ExcessListStatus.COLLECTING,
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
    def test_creates_awarded_list(self, db_session: Session):
        trader, _broker, company = _make_seed_users(db_session)
        _build_awarded(db_session, company, trader)
        db_session.flush()
        el = db_session.query(ExcessList).filter_by(title=_LIST_AWARDED).one()
        assert el.status == ExcessListStatus.AWARDED

    def test_idempotent(self, db_session: Session):
        trader, _broker, company = _make_seed_users(db_session)
        _build_awarded(db_session, company, trader)
        db_session.flush()
        _build_awarded(db_session, company, trader)
        db_session.flush()
        count = db_session.query(ExcessList).filter_by(title=_LIST_AWARDED).count()
        assert count == 1


class TestBuildCollecting:
    def test_creates_collecting_list(self, db_session: Session):
        trader, broker, company = _make_seed_users(db_session)
        with (
            patch("app.management.seed_resell_demo.excess_mirror.sync_list_mirror"),
            patch("app.management.seed_resell_demo.excess_service.submit_offer"),
            patch("app.management.seed_resell_demo.excess_service._resolve_line_material_card"),
        ):
            _build_collecting(db_session, company, trader, broker)
            db_session.commit()

        el = db_session.query(ExcessList).filter_by(title=_LIST_COLLECTING).one()
        assert el.status == ExcessListStatus.COLLECTING

    def test_idempotent_no_duplicate_lists(self, db_session: Session):
        trader, broker, company = _make_seed_users(db_session)
        with (
            patch("app.management.seed_resell_demo.excess_mirror.sync_list_mirror"),
            patch("app.management.seed_resell_demo.excess_service.submit_offer"),
            patch("app.management.seed_resell_demo.excess_service._resolve_line_material_card"),
        ):
            _build_collecting(db_session, company, trader, broker)
            db_session.commit()
            _build_collecting(db_session, company, trader, broker)
            db_session.commit()

        count = db_session.query(ExcessList).filter_by(title=_LIST_COLLECTING).count()
        assert count == 1


class TestBuildOneoff:
    def test_creates_oneoff_list(self, db_session: Session):
        trader, broker, company = _make_seed_users(db_session)
        with (
            patch("app.management.seed_resell_demo.excess_mirror.sync_list_mirror"),
            patch("app.management.seed_resell_demo.excess_service.submit_offer"),
            patch("app.management.seed_resell_demo.excess_service._resolve_line_material_card"),
        ):
            _build_oneoff(db_session, company, trader, broker)
            db_session.commit()

        el = db_session.query(ExcessList).filter_by(title=_LIST_ONEOFF).one()
        assert el.status == ExcessListStatus.OPEN


class TestSeed:
    def test_seed_creates_all_three_lists(self, db_session: Session):
        with (
            patch("app.management.seed_resell_demo.excess_mirror.sync_list_mirror"),
            patch("app.management.seed_resell_demo.excess_service.submit_offer"),
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
            patch("app.management.seed_resell_demo.excess_mirror.sync_list_mirror"),
            patch("app.management.seed_resell_demo.excess_service.submit_offer"),
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
            patch("app.management.seed_resell_demo.excess_mirror.sync_list_mirror"),
            patch("app.management.seed_resell_demo.excess_service.submit_offer"),
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
