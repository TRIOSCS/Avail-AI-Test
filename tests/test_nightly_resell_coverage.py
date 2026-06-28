"""tests/test_nightly_resell_coverage.py — Nightly coverage boost for
app/routers/resell.py.

Targets uncovered helper functions (_file_extension, _hours_until, _offer_coverage) and
error paths in route handlers (403/404/409/400 branches).

Called by: pytest (nightly coverage run) Depends on: conftest (db_session, client,
test_user, test_company)
"""

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

os.environ.setdefault("TESTING", "1")

from app.constants import ExcessListStatus
from app.models import Company, User
from app.models.excess import ExcessLineItem, ExcessList
from app.routers.resell import _file_extension, _hours_until, _offer_coverage

# ── Pure-function unit tests ──────────────────────────────────────────────────


class TestFileExtension:
    def test_no_dot_returns_empty(self):
        assert _file_extension("filename") == ""

    def test_csv_extension(self):
        assert _file_extension("data.CSV") == ".csv"

    def test_multiple_dots(self):
        assert _file_extension("my.file.xlsx") == ".xlsx"

    def test_empty_string(self):
        assert _file_extension("") == ""


class TestHoursUntil:
    def test_none_close_at_returns_none(self):
        assert _hours_until(None) is None

    def test_future_close_at_positive(self):
        future = datetime.now(timezone.utc) + timedelta(hours=2)
        result = _hours_until(future)
        assert result is not None
        assert 1.9 < result < 2.1

    def test_past_close_at_negative(self):
        past = datetime.now(timezone.utc) - timedelta(hours=3)
        result = _hours_until(past)
        assert result is not None
        assert -3.1 < result < -2.9

    def test_naive_datetime_tolerated(self):
        naive = datetime.utcnow() + timedelta(hours=1)
        result = _hours_until(naive)
        assert result is not None
        assert result > 0


class TestOfferCoverage:
    def test_empty_list_zero_zero(self):
        assert _offer_coverage([]) == (0, 0)

    def test_all_lines_covered(self):
        items = [_mock_item(2), _mock_item(3)]
        assert _offer_coverage(items) == (2, 2)

    def test_no_lines_covered(self):
        items = [_mock_item(0), _mock_item(0)]
        assert _offer_coverage(items) == (0, 2)

    def test_partial_coverage(self):
        items = [_mock_item(1), _mock_item(0), _mock_item(5)]
        assert _offer_coverage(items) == (2, 3)


def _mock_item(offer_count: int):
    """Create a minimal object with an offer_count attribute."""

    class _Item:
        pass

    obj = _Item()
    obj.offer_count = offer_count
    return obj


# ── Fixtures ─────────────────────────────────────────────────────────────────


def _make_trader(db: Session) -> User:
    u = User(
        email=f"trader-nr-{uuid.uuid4().hex[:8]}@test.com",
        name="NR Trader",
        role="trader",
        azure_id=f"azure-nr-{uuid.uuid4().hex[:8]}",
        is_active=True,
    )
    db.add(u)
    db.flush()
    return u


def _make_buyer(db: Session) -> User:
    u = User(
        email=f"buyer-nr-{uuid.uuid4().hex[:8]}@test.com",
        name="NR Buyer",
        role="buyer",
        azure_id=f"azure-nb-{uuid.uuid4().hex[:8]}",
        is_active=True,
    )
    db.add(u)
    db.flush()
    return u


def _make_sales_user(db: Session) -> User:
    """A 'sales' user: can post but cannot offer — useful for testing offer 403."""
    u = User(
        email=f"sales-nr-{uuid.uuid4().hex[:8]}@test.com",
        name="NR Sales",
        role="sales",
        azure_id=f"azure-ns-{uuid.uuid4().hex[:8]}",
        is_active=True,
    )
    db.add(u)
    db.flush()
    return u


def _make_list(db: Session, owner: User, company: Company, status: str = ExcessListStatus.COLLECTING) -> ExcessList:
    el = ExcessList(
        title=f"NR-List-{uuid.uuid4().hex[:6]}",
        company_id=company.id,
        owner_id=owner.id,
        status=status,
        total_line_items=0,
        created_at=datetime.now(timezone.utc),
    )
    db.add(el)
    db.flush()
    return el


def _make_draft_list(db: Session, owner: User, company: Company) -> ExcessList:
    return _make_list(db, owner, company, ExcessListStatus.DRAFT)


def _make_line(db: Session, el: ExcessList, mpn: str = "LM317T") -> ExcessLineItem:
    item = ExcessLineItem(
        excess_list_id=el.id,
        part_number=mpn,
        quantity=10,
        status="available",
        created_at=datetime.now(timezone.utc),
    )
    db.add(item)
    db.flush()
    return item


@pytest.fixture()
def _trader_client(db_session: Session, test_company: Company):
    from app.database import get_db
    from app.dependencies import require_user
    from app.main import app

    trader = _make_trader(db_session)
    db_session.commit()

    def _db():
        yield db_session

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[require_user] = lambda: trader
    try:
        yield TestClient(app, raise_server_exceptions=False), trader
    finally:
        for dep in (get_db, require_user):
            app.dependency_overrides.pop(dep, None)


@pytest.fixture()
def _buyer_client(db_session: Session, test_company: Company):
    from app.database import get_db
    from app.dependencies import require_user
    from app.main import app

    buyer = _make_buyer(db_session)
    db_session.commit()

    def _db():
        yield db_session

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[require_user] = lambda: buyer
    try:
        yield TestClient(app, raise_server_exceptions=False), buyer
    finally:
        for dep in (get_db, require_user):
            app.dependency_overrides.pop(dep, None)


# ── Route error-path tests ─────────────────────────────────────────────────────


class TestResellCreateFormErrors:
    def test_buyer_cannot_access_create_form(self, _buyer_client):
        """Non-trader users get 403 from the create-form route."""
        client, _buyer = _buyer_client
        r = client.get("/v2/partials/resell/create-form")
        assert r.status_code == 403


class TestResellLineOfferCompareErrors:
    def test_non_owner_gets_403(self, _buyer_client, db_session: Session, test_company: Company):
        client, buyer = _buyer_client
        trader = _make_trader(db_session)
        el = _make_list(db_session, trader, test_company)
        line = _make_line(db_session, el)
        db_session.commit()
        r = client.get(f"/v2/partials/resell/{el.id}/lines/{line.id}/offers")
        assert r.status_code == 403

    def test_owner_with_missing_line_gets_404(self, _trader_client, db_session: Session, test_company: Company):
        client, trader = _trader_client
        el = _make_list(db_session, trader, test_company)
        db_session.commit()
        r = client.get(f"/v2/partials/resell/{el.id}/lines/999999/offers")
        assert r.status_code == 404

    def test_owner_with_line_from_different_list_gets_404(
        self, _trader_client, db_session: Session, test_company: Company
    ):
        client, trader = _trader_client
        el1 = _make_list(db_session, trader, test_company)
        el2 = _make_list(db_session, trader, test_company)
        line_on_el2 = _make_line(db_session, el2)
        db_session.commit()
        r = client.get(f"/v2/partials/resell/{el1.id}/lines/{line_on_el2.id}/offers")
        assert r.status_code == 404


class TestResellAddLineFormErrors:
    def test_posted_list_returns_409(self, _trader_client, db_session: Session, test_company: Company):
        client, trader = _trader_client
        el = _make_list(db_session, trader, test_company, ExcessListStatus.COLLECTING)
        db_session.commit()
        r = client.get(f"/v2/partials/resell/{el.id}/add-line-form")
        assert r.status_code == 409

    def test_draft_list_returns_200(self, _trader_client, db_session: Session, test_company: Company):
        client, trader = _trader_client
        el = _make_draft_list(db_session, trader, test_company)
        db_session.commit()
        r = client.get(f"/v2/partials/resell/{el.id}/add-line-form")
        assert r.status_code == 200


@pytest.fixture()
def _sales_client(db_session: Session, test_company: Company):
    """A 'sales' user client: can post but not offer."""
    from app.database import get_db
    from app.dependencies import require_user
    from app.main import app

    sales = _make_sales_user(db_session)
    db_session.commit()

    def _db():
        yield db_session

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[require_user] = lambda: sales
    try:
        yield TestClient(app, raise_server_exceptions=False), sales
    finally:
        for dep in (get_db, require_user):
            app.dependency_overrides.pop(dep, None)


class TestResellOfferFormErrors:
    def test_owner_cannot_offer_on_own_list(self, _trader_client, db_session: Session, test_company: Company):
        """List owner gets 403 when trying to offer on their own list."""
        client, trader = _trader_client
        el = _make_list(db_session, trader, test_company)
        db_session.commit()
        r = client.get(f"/v2/partials/resell/{el.id}/offer-form")
        assert r.status_code == 403

    def test_sales_user_cannot_offer(self, _sales_client, db_session: Session, test_company: Company):
        """Sales role users cannot submit offers (not in _CAN_OFFER_ROLES)."""
        client, sales = _sales_client
        trader = _make_trader(db_session)
        el = _make_list(db_session, trader, test_company)
        db_session.commit()
        r = client.get(f"/v2/partials/resell/{el.id}/offer-form")
        assert r.status_code == 403


class TestResellListFiltering:
    def test_lists_stage_filter(self, _trader_client, db_session: Session, test_company: Company):
        """Lists filtered by stage returns 200."""
        client, trader = _trader_client
        _make_list(db_session, trader, test_company)
        db_session.commit()
        r = client.get("/v2/partials/resell/lists?stage=collecting&lens=mine")
        assert r.status_code == 200

    def test_lists_q_filter(self, _trader_client, db_session: Session, test_company: Company):
        """Lists filtered by search query returns 200."""
        client, trader = _trader_client
        _make_list(db_session, trader, test_company)
        db_session.commit()
        r = client.get("/v2/partials/resell/lists?q=surplus&lens=mine")
        assert r.status_code == 200


class TestResellCreateListErrors:
    def test_buyer_cannot_create_list(self, _buyer_client, test_company: Company):
        client, _buyer = _buyer_client
        r = client.post(
            "/api/resell/lists",
            data={"title": "Test List", "company_id": test_company.id, "notes": ""},
        )
        assert r.status_code == 403


class TestResellAddLineErrors:
    def test_posted_list_add_line_returns_409(self, _trader_client, db_session: Session, test_company: Company):
        client, trader = _trader_client
        el = _make_list(db_session, trader, test_company, ExcessListStatus.COLLECTING)
        db_session.commit()
        r = client.post(
            f"/api/resell/{el.id}/lines",
            data={"part_number": "LM317T", "quantity": 10},
        )
        assert r.status_code == 409


class TestResellBidBack:
    def test_build_bid_invalid_json_returns_400(self, _trader_client, db_session: Session, test_company: Company):
        client, trader = _trader_client
        el = _make_list(db_session, trader, test_company)
        db_session.commit()
        r = client.post(
            f"/api/resell/{el.id}/bid",
            data={"selections_json": "not-json"},
        )
        assert r.status_code == 400

    def test_build_bid_empty_list_returns_400(self, _trader_client, db_session: Session, test_company: Company):
        client, trader = _trader_client
        el = _make_list(db_session, trader, test_company)
        db_session.commit()
        r = client.post(
            f"/api/resell/{el.id}/bid",
            data={"selections_json": "[]"},
        )
        assert r.status_code == 400


class TestResellPublishErrors:
    def test_non_owner_cannot_publish(self, _buyer_client, db_session: Session, test_company: Company):
        client, buyer = _buyer_client
        trader = _make_trader(db_session)
        el = _make_draft_list(db_session, trader, test_company)
        db_session.commit()
        r = client.post(f"/api/resell/{el.id}/publish")
        assert r.status_code == 403
