"""tests/test_nightly_resell_coverage.py — Nightly coverage boost for
app/routers/resell.py.

Targets uncovered helper functions (_file_extension, _hours_until, _offer_coverage) and
error paths in route handlers (403/404/409/400 branches).

Called by: pytest (nightly coverage run) Depends on: conftest (db_session, client,
test_user, test_company)
"""

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

os.environ.setdefault("TESTING", "1")

from app.constants import ExcessListStatus, ExcessOutreachStatus
from app.models import Company, User, VendorCard
from app.models.excess import ExcessLineItem, ExcessList, ExcessOutreach
from app.routers.resell import _file_extension, _hours_until, _offer_coverage, _outreach_tracker_context
from app.services import resell_outreach_service as outreach_svc

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
        future = datetime.now(UTC) + timedelta(hours=2)
        result = _hours_until(future)
        assert result is not None
        assert 1.9 < result < 2.1

    def test_past_close_at_negative(self):
        past = datetime.now(UTC) - timedelta(hours=3)
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
        created_at=datetime.now(UTC),
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
        created_at=datetime.now(UTC),
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


def _titled_list(db, owner, company, title, status=ExcessListStatus.COLLECTING):
    """A list with an EXPLICIT title/status — for inclusion/exclusion filter asserts."""
    el = ExcessList(
        title=title,
        company_id=company.id,
        owner_id=owner.id,
        status=status,
        total_line_items=0,
        created_at=datetime.now(UTC),
    )
    db.add(el)
    db.flush()
    return el


class TestResellListFiltering:
    def test_lists_stage_filter(self, _trader_client, db_session: Session, test_company: Company):
        """Stage=collecting returns ONLY collecting lists — an awarded list is
        excluded."""
        client, trader = _trader_client
        _titled_list(db_session, trader, test_company, "Collecting-Widget-List", ExcessListStatus.COLLECTING)
        _titled_list(db_session, trader, test_company, "Awarded-Gizmo-List", ExcessListStatus.AWARDED)
        db_session.commit()

        body = client.get("/v2/partials/resell/lists?stage=collecting&lens=mine").text
        assert "Collecting-Widget-List" in body  # matching stage included
        assert "Awarded-Gizmo-List" not in body  # non-matching stage excluded

    def test_stage_live_includes_open_and_collecting_only(self, _trader_client, db_session: Session, test_company):
        """Task 6 (finding #16): ``stage=live`` expands to [open, collecting] — matching
        the "Open" glance card's count — and excludes resolved (bid_out/awarded)
        lists."""
        client, trader = _trader_client
        _titled_list(db_session, trader, test_company, "Live-Open-List", ExcessListStatus.OPEN)
        _titled_list(db_session, trader, test_company, "Live-Collecting-List", ExcessListStatus.COLLECTING)
        _titled_list(db_session, trader, test_company, "Live-BidOut-List", ExcessListStatus.BID_OUT)
        _titled_list(db_session, trader, test_company, "Live-Awarded-List", ExcessListStatus.AWARDED)
        db_session.commit()

        body = client.get("/v2/partials/resell/lists?stage=live&lens=mine").text
        assert "Live-Open-List" in body  # open is live
        assert "Live-Collecting-List" in body  # collecting is live
        assert "Live-BidOut-List" not in body  # resolved — excluded
        assert "Live-Awarded-List" not in body  # resolved — excluded

    def test_stage_open_stays_strict(self, _trader_client, db_session: Session, test_company: Company):
        """The strict ``stage=open`` pill still means EXACTLY status=open — collecting
        is NOT overloaded into it (only the ``live`` token widens to both)."""
        client, trader = _trader_client
        _titled_list(db_session, trader, test_company, "Strict-Open-List", ExcessListStatus.OPEN)
        _titled_list(db_session, trader, test_company, "Strict-Collecting-List", ExcessListStatus.COLLECTING)
        db_session.commit()

        body = client.get("/v2/partials/resell/lists?stage=open&lens=mine").text
        assert "Strict-Open-List" in body
        assert "Strict-Collecting-List" not in body  # collecting is NOT status=open

    def test_workspace_open_card_links_to_stage_live(self, _trader_client, db_session: Session):
        """The "Open" glance card links to stage=live (matching its open+collecting
        count), not the strict stage=open it mismatched before."""
        client, _trader = _trader_client
        body = client.get("/v2/partials/resell/workspace?lens=mine").text
        assert "lens=mine&stage=live" in body

    def test_lists_q_filter(self, _trader_client, db_session: Session, test_company: Company):
        """Q matches the title in the mine lens — matching title included, non-matching
        excluded (proves the search actually filters, not just returns 200)."""
        client, trader = _trader_client
        _titled_list(db_session, trader, test_company, "Titanium capacitors surplus")
        _titled_list(db_session, trader, test_company, "Ceramic resistors clearance")
        db_session.commit()

        body = client.get("/v2/partials/resell/lists?q=Titanium&lens=mine").text
        assert "Titanium capacitors surplus" in body  # title contains the query term
        assert "Ceramic resistors clearance" not in body  # non-matching title excluded

    def test_lists_mine_lens_excludes_other_owners(self, _trader_client, db_session: Session, test_company: Company):
        """Owner-scoping guard (multi-user): the mine lens returns ONLY the caller's
        lists — another trader's list must never appear."""
        client, trader = _trader_client
        other = _make_trader(db_session)
        _titled_list(db_session, trader, test_company, "My-Own-Surplus")
        _titled_list(db_session, other, test_company, "Someone-Elses-Surplus")
        db_session.commit()

        body = client.get("/v2/partials/resell/lists?lens=mine").text
        assert "My-Own-Surplus" in body  # caller's own list present
        assert "Someone-Elses-Surplus" not in body  # another owner's list absent


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


# ── Task 4: stale-``sending`` sweeper ─────────────────────────────────────────


def _make_outreach(
    db: Session,
    el: ExcessList,
    owner: User,
    *,
    status: str,
    created_at: datetime,
    card_id: int | None = None,
) -> ExcessOutreach:
    row = ExcessOutreach(
        excess_list_id=el.id,
        submitted_by=owner.id,
        target_vendor_card_id=card_id,
        channel="email",
        status=status,
        created_at=created_at,
    )
    db.add(row)
    db.flush()
    return row


def _make_card(db: Session, name: str) -> VendorCard:
    vc = VendorCard(normalized_name=name.lower(), display_name=name, emails=[f"{name.lower()}@x.com"])
    db.add(vc)
    db.flush()
    return vc


class TestStaleSendingSweeper:
    def test_flips_aged_sending_to_interrupted_leaves_fresh_and_settled(
        self, db_session: Session, test_company: Company
    ):
        """A ``sending`` row older than the threshold is flipped to ``interrupted`` (its
        background job died mid-flight); a fresh ``sending`` row and any already-settled
        row are untouched.

        The sweep NEVER assumes not-sent (interrupted, not no_response), and never
        resends.
        """
        trader = _make_trader(db_session)
        el = _make_list(db_session, trader, test_company)
        now = datetime.now(UTC)

        aged = _make_outreach(
            db_session, el, trader, status=ExcessOutreachStatus.SENDING, created_at=now - timedelta(hours=2)
        )
        fresh = _make_outreach(db_session, el, trader, status=ExcessOutreachStatus.SENDING, created_at=now)
        settled = _make_outreach(
            db_session, el, trader, status=ExcessOutreachStatus.SENT, created_at=now - timedelta(hours=2)
        )
        db_session.commit()

        flipped = outreach_svc.sweep_stale_sending_outreach(db_session, now=now)

        assert flipped == 1
        db_session.expire_all()
        assert db_session.get(ExcessOutreach, aged.id).status == ExcessOutreachStatus.INTERRUPTED
        assert db_session.get(ExcessOutreach, aged.id).send_error  # a reason is recorded
        assert db_session.get(ExcessOutreach, fresh.id).status == ExcessOutreachStatus.SENDING
        assert db_session.get(ExcessOutreach, settled.id).status == ExcessOutreachStatus.SENT


class TestReclassifyStaleSendingScoping:
    def test_scoped_to_one_list_leaves_other_lists_stale_rows_untouched(
        self, db_session: Session, test_company: Company
    ):
        """Finding B7: ``reclassify_stale_sending(excess_list_id=...)`` only reclassifies
        rows on THAT list — a stale row on a different list is left for its own tab load
        (or the nightly sweep) to catch."""
        trader = _make_trader(db_session)
        list_a = _make_list(db_session, trader, test_company)
        list_b = _make_list(db_session, trader, test_company)
        now = datetime.now(UTC)

        stale_a = _make_outreach(
            db_session, list_a, trader, status=ExcessOutreachStatus.SENDING, created_at=now - timedelta(hours=2)
        )
        stale_b = _make_outreach(
            db_session, list_b, trader, status=ExcessOutreachStatus.SENDING, created_at=now - timedelta(hours=2)
        )
        db_session.commit()

        flipped = outreach_svc.reclassify_stale_sending(db_session, excess_list_id=list_a.id, now=now)

        assert flipped == 1
        db_session.expire_all()
        assert db_session.get(ExcessOutreach, stale_a.id).status == ExcessOutreachStatus.INTERRUPTED
        assert db_session.get(ExcessOutreach, stale_b.id).status == ExcessOutreachStatus.SENDING

    def test_scoped_to_one_outreach_id_leaves_sibling_rows_untouched(self, db_session: Session, test_company: Company):
        """Scoping by ``outreach_id`` (the retry guard's use) reclassifies ONLY that
        row, even when a sibling row on the same list is equally stale."""
        trader = _make_trader(db_session)
        el = _make_list(db_session, trader, test_company)
        now = datetime.now(UTC)

        target = _make_outreach(
            db_session, el, trader, status=ExcessOutreachStatus.SENDING, created_at=now - timedelta(hours=2)
        )
        sibling = _make_outreach(
            db_session, el, trader, status=ExcessOutreachStatus.SENDING, created_at=now - timedelta(hours=2)
        )
        db_session.commit()

        flipped = outreach_svc.reclassify_stale_sending(db_session, outreach_id=target.id, now=now)

        assert flipped == 1
        db_session.expire_all()
        assert db_session.get(ExcessOutreach, target.id).status == ExcessOutreachStatus.INTERRUPTED
        assert db_session.get(ExcessOutreach, sibling.id).status == ExcessOutreachStatus.SENDING


class TestOfferedSummaryExcludesNonSent:
    def test_offered_count_ignores_failed_buyer(self, db_session: Session, test_company: Company):
        """The tracker glance 'offered N' counts only buyers genuinely offered — a buyer
        whose only row is FAILED is not counted (finding: a failed send must not inflate
        the offered tally)."""
        trader = _make_trader(db_session)
        el = _make_list(db_session, trader, test_company)
        now = datetime.now(UTC)
        card_sent = _make_card(db_session, "SentBuyer")
        card_failed = _make_card(db_session, "FailedBuyer")
        _make_outreach(db_session, el, trader, status=ExcessOutreachStatus.SENT, created_at=now, card_id=card_sent.id)
        _make_outreach(
            db_session, el, trader, status=ExcessOutreachStatus.FAILED, created_at=now, card_id=card_failed.id
        )
        db_session.commit()

        ctx = _outreach_tracker_context(None, db_session, el, trader)
        assert ctx["summary"]["offered"] == 1


class TestExpiryPerListIsolation:
    def test_one_bad_list_mirror_error_does_not_block_the_others(self, db_session: Session, test_company: Company):
        """One overdue list whose mirror-sync raises must NOT abort the whole batch —
        the other overdue lists still expire (finding #6 silent-failure isolation)."""
        from unittest.mock import patch

        from app.services import excess_service

        trader = _make_trader(db_session)
        past = datetime.now(UTC) - timedelta(hours=1)
        bad = _make_list(db_session, trader, test_company, ExcessListStatus.COLLECTING)
        good = _make_list(db_session, trader, test_company, ExcessListStatus.COLLECTING)
        bad.close_at = past
        good.close_at = past
        db_session.commit()
        bad_id, good_id = bad.id, good.id

        def _sync(_db, el):
            if el.id == bad_id:
                raise RuntimeError("mirror boom")

        with patch("app.services.excess_mirror.sync_list_mirror", side_effect=_sync):
            expired = excess_service.expire_overdue_lists(db_session, now=datetime.now(UTC))

        db_session.expire_all()
        # The good list expired despite the bad list's mirror error; the bad one stayed put.
        assert db_session.get(ExcessList, good_id).status == ExcessListStatus.EXPIRED
        assert db_session.get(ExcessList, bad_id).status == ExcessListStatus.COLLECTING
        assert expired == 1
