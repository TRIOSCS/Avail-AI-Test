"""tests/test_crm_service.py — Unit tests for CRM service helpers.

Tests for company_commercial_stats() and next_best_touch() which are being
extracted / added to app/services/crm_service.py as part of CRM cockpit P3-2.

These tests are written FIRST (TDD) — they will FAIL until the production
functions are implemented.

Called by: pytest
Depends on: app.services.crm_service, app.models
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from app.models import Company, CustomerSite, Quote, Requisition

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

NOW = datetime(2026, 6, 18, 12, 0, 0, tzinfo=timezone.utc)


def _make_company(db: Session, name: str = "Test Co") -> Company:
    co = Company(name=name, is_active=True)
    db.add(co)
    db.flush()
    return co


def _make_site(db: Session, company: Company) -> CustomerSite:
    site = CustomerSite(company_id=company.id, site_name="HQ")
    db.add(site)
    db.flush()
    return site


def _make_req(
    db: Session,
    site: CustomerSite,
    status: str,
    created_at: datetime | None = None,
) -> Requisition:
    req = Requisition(
        name=f"REQ-{status[:3].upper()}-001",
        customer_name="Test Co",
        status=status,
        customer_site_id=site.id,
        company_id=site.company_id,
        created_at=created_at or NOW,
    )
    db.add(req)
    db.flush()
    return req


def _make_quote(
    db: Session,
    req: Requisition,
    site: CustomerSite,
    subtotal: float,
    created_at: datetime | None = None,
) -> Quote:
    q = Quote(
        requisition_id=req.id,
        customer_site_id=site.id,
        quote_number=f"Q-{req.id}-001",
        status="sent",
        line_items=[],
        subtotal=subtotal,
        total_cost=subtotal * 0.5,
        total_margin_pct=50.0,
        created_at=created_at or NOW,
    )
    db.add(q)
    db.flush()
    return q


# ─────────────────────────────────────────────────────────────────────────────
# TestCompanyCommercialStats
# ─────────────────────────────────────────────────────────────────────────────


class TestCompanyCommercialStats:
    """Tests for company_commercial_stats(db, company_ids) → dict[int, dict].

    The function does NOT exist yet — all tests here will fail with ImportError until it
    is implemented.
    """

    def test_returns_win_rate_from_won_and_decided(self, db_session: Session):
        """Win rate = round(won / (won + lost) * 100), only WON+LOST are 'decided'."""
        from app.services.crm_service import company_commercial_stats

        co = _make_company(db_session, "WinRate Co")
        site = _make_site(db_session, co)

        _make_req(db_session, site, "won")
        _make_req(db_session, site, "won")
        _make_req(db_session, site, "lost")
        # active req should NOT count as decided
        _make_req(db_session, site, "active")
        db_session.commit()

        result = company_commercial_stats(db_session, [co.id])
        assert co.id in result
        stats = result[co.id]
        # 2 won / 3 decided (won+lost) = 67%
        assert stats["win_rate"] == 67

    def test_returns_none_win_rate_when_no_decided(self, db_session: Session):
        """Win rate is None when there are no WON or LOST requisitions."""
        from app.services.crm_service import company_commercial_stats

        co = _make_company(db_session, "NoneWin Co")
        site = _make_site(db_session, co)

        _make_req(db_session, site, "active")
        _make_req(db_session, site, "sourcing")
        db_session.commit()

        result = company_commercial_stats(db_session, [co.id])
        assert co.id in result
        assert result[co.id]["win_rate"] is None

    def test_revenue_90d_sums_won_quotes_within_window(self, db_session: Session):
        """revenue_90d sums Quote.subtotal for WON reqs with quotes created <= 90d
        ago."""
        from app.services.crm_service import company_commercial_stats

        co = _make_company(db_session, "Revenue Co")
        site = _make_site(db_session, co)

        # WON req within the 90d window
        req_in = _make_req(db_session, site, "won", created_at=NOW - timedelta(days=30))
        _make_quote(db_session, req_in, site, subtotal=5000.0, created_at=NOW - timedelta(days=30))

        # WON req OUTSIDE the 90d window — should NOT be included
        req_out = _make_req(db_session, site, "won", created_at=NOW - timedelta(days=120))
        _make_quote(db_session, req_out, site, subtotal=9999.0, created_at=NOW - timedelta(days=120))

        db_session.commit()

        result = company_commercial_stats(db_session, [co.id])
        assert co.id in result
        assert result[co.id]["revenue_90d"] == pytest.approx(5000.0)

    def test_last_req_date_is_max_created_at(self, db_session: Session):
        """last_req_date is the ISO string of the most recent requisition's
        created_at."""
        from app.services.crm_service import company_commercial_stats

        co = _make_company(db_session, "MaxDate Co")
        site = _make_site(db_session, co)

        older = NOW - timedelta(days=60)
        newer = NOW - timedelta(days=10)

        _make_req(db_session, site, "won", created_at=older)
        _make_req(db_session, site, "active", created_at=newer)
        db_session.commit()

        result = company_commercial_stats(db_session, [co.id])
        assert co.id in result
        # Should return the newer date as an ISO string
        last_req = result[co.id]["last_req_date"]
        assert last_req is not None
        assert newer.isoformat()[:10] in last_req  # date portion matches

    def test_empty_company_ids_returns_empty_dict(self, db_session: Session):
        """Passing [] returns {} without querying."""
        from app.services.crm_service import company_commercial_stats

        result = company_commercial_stats(db_session, [])
        assert result == {}

    def test_multi_company_returns_separate_stats(self, db_session: Session):
        """Each company gets its own stats dict, correctly isolated."""
        from app.services.crm_service import company_commercial_stats

        co_a = _make_company(db_session, "Alpha Corp")
        co_b = _make_company(db_session, "Beta Corp")
        site_a = _make_site(db_session, co_a)
        site_b = _make_site(db_session, co_b)

        _make_req(db_session, site_a, "won")
        _make_req(db_session, site_a, "lost")
        _make_req(db_session, site_b, "won")
        _make_req(db_session, site_b, "won")
        _make_req(db_session, site_b, "won")
        db_session.commit()

        result = company_commercial_stats(db_session, [co_a.id, co_b.id])
        assert co_a.id in result
        assert co_b.id in result

        # Alpha: 1 won / 2 decided = 50%
        assert result[co_a.id]["win_rate"] == 50
        # Beta: 3 won / 3 decided = 100%
        assert result[co_b.id]["win_rate"] == 100


# ─────────────────────────────────────────────────────────────────────────────
# TestNextBestTouch
# ─────────────────────────────────────────────────────────────────────────────


class TestNextBestTouch:
    """Tests for next_best_touch(tier, last_outbound_at, now) → str.

    The function does NOT exist yet — all tests here will fail with ImportError until it
    is implemented.
    """

    def test_never_contacted_returns_reach_out(self):
        """last_outbound_at=None → 'Never contacted — reach out'."""
        from app.services.crm_service import next_best_touch

        result = next_best_touch(tier="standard", last_outbound_at=None, now=NOW)
        assert result == "Never contacted — reach out"

    def test_never_contacted_with_none_tier(self):
        """Tier=None and last_outbound_at=None → 'Never contacted — reach out'."""
        from app.services.crm_service import next_best_touch

        result = next_best_touch(tier=None, last_outbound_at=None, now=NOW)
        assert result == "Never contacted — reach out"

    def test_overdue_returns_reach_out_now(self):
        """35 days ago on standard tier (30d target) → 'Overdue — reach out now'."""
        from app.services.crm_service import next_best_touch

        last_outbound = NOW - timedelta(days=35)
        result = next_best_touch(tier="standard", last_outbound_at=last_outbound, now=NOW)
        assert result == "Overdue — reach out now"

    def test_due_returns_due_for_outreach(self):
        """10 days ago on key tier (7d target) → 'Due for outreach'."""
        from app.services.crm_service import next_best_touch

        last_outbound = NOW - timedelta(days=10)
        result = next_best_touch(tier="key", last_outbound_at=last_outbound, now=NOW)
        assert result == "Due for outreach"

    def test_on_target_returns_on_track(self):
        """5 days ago on standard tier (30d target) → 'On track'."""
        from app.services.crm_service import next_best_touch

        last_outbound = NOW - timedelta(days=5)
        result = next_best_touch(tier="standard", last_outbound_at=last_outbound, now=NOW)
        assert result == "On track"

    def test_on_target_key_tier_within_window(self):
        """3 days ago on key tier (7d target) → 'On track'."""
        from app.services.crm_service import next_best_touch

        last_outbound = NOW - timedelta(days=3)
        result = next_best_touch(tier="key", last_outbound_at=last_outbound, now=NOW)
        assert result == "On track"


# ─────────────────────────────────────────────────────────────────────────────
# TestOrderByClock — P3-5 generalization (VendorCard support)
# ─────────────────────────────────────────────────────────────────────────────


class TestOrderByClock:
    """Tests for order_by_clock generalization: model= param, VendorCard support,
    Company regression guard.

    Written FIRST (TDD RED) — will fail until order_by_clock accepts model=.
    """

    def test_vendor_outbound_nulls_first(self, db_session: Session):
        """order_by_clock with model=VendorCard puts NULL outbound vendors first."""
        from app.models.vendors import VendorCard
        from app.services.crm_service import order_by_clock

        v_null = VendorCard(normalized_name="null-vendor", display_name="Null Vendor")
        v_old = VendorCard(
            normalized_name="old-vendor",
            display_name="Old Vendor",
            last_outbound_at=NOW - timedelta(days=30),
        )
        v_recent = VendorCard(
            normalized_name="recent-vendor",
            display_name="Recent Vendor",
            last_outbound_at=NOW - timedelta(days=5),
        )
        db_session.add_all([v_recent, v_old, v_null])
        db_session.commit()

        query = db_session.query(VendorCard)
        results = order_by_clock(query, "outbound", model=VendorCard).all()

        names = [v.normalized_name for v in results]
        assert names.index("null-vendor") < names.index("old-vendor")
        assert names.index("old-vendor") < names.index("recent-vendor")

    def test_vendor_outbound_oldest_before_recent(self, db_session: Session):
        """order_by_clock VendorCard: oldest non-NULL outbound comes before recent."""
        from app.models.vendors import VendorCard
        from app.services.crm_service import order_by_clock

        v_old = VendorCard(
            normalized_name="stalest-vendor",
            display_name="Stalest Vendor",
            last_outbound_at=NOW - timedelta(days=60),
        )
        v_recent = VendorCard(
            normalized_name="freshest-vendor",
            display_name="Freshest Vendor",
            last_outbound_at=NOW - timedelta(days=2),
        )
        db_session.add_all([v_recent, v_old])
        db_session.commit()

        query = db_session.query(VendorCard)
        results = order_by_clock(query, "outbound", model=VendorCard).all()

        names = [v.normalized_name for v in results]
        assert names.index("stalest-vendor") < names.index("freshest-vendor")

    def test_company_order_by_clock_default_unchanged(self, db_session: Session):
        """Regression guard: order_by_clock with no model= arg (default Company) still
        orders companies by stalest outbound first."""
        from app.models import Company
        from app.services.crm_service import order_by_clock

        c_null = Company(name="Null Outbound Co", is_active=True, last_outbound_at=None)
        c_old = Company(
            name="Old Outbound Co",
            is_active=True,
            last_outbound_at=NOW - timedelta(days=45),
        )
        c_recent = Company(
            name="Recent Outbound Co",
            is_active=True,
            last_outbound_at=NOW - timedelta(days=3),
        )
        db_session.add_all([c_recent, c_old, c_null])
        db_session.commit()

        query = db_session.query(Company)
        results = order_by_clock(query, "outbound").all()

        names = [c.name for c in results]
        # NULL first, then oldest, then most recent
        null_pos = names.index("Null Outbound Co")
        old_pos = names.index("Old Outbound Co")
        recent_pos = names.index("Recent Outbound Co")
        assert null_pos < old_pos < recent_pos

    def test_vendor_reply_clock_nulls_first(self, db_session: Session):
        """order_by_clock('reply', model=VendorCard) orders by last_reply_at."""
        from app.models.vendors import VendorCard
        from app.services.crm_service import order_by_clock

        v_null = VendorCard(normalized_name="no-reply-vendor", display_name="No Reply", last_reply_at=None)
        v_old = VendorCard(
            normalized_name="old-reply-vendor",
            display_name="Old Reply",
            last_reply_at=NOW - timedelta(days=20),
        )
        db_session.add_all([v_old, v_null])
        db_session.commit()

        query = db_session.query(VendorCard)
        results = order_by_clock(query, "reply", model=VendorCard).all()

        names = [v.normalized_name for v in results]
        assert names.index("no-reply-vendor") < names.index("old-reply-vendor")

    def test_positional_now_raises_type_error(self, db_session: Session):
        """Keyword-only guard: passing now as a positional arg must raise TypeError,
        not silently mis-bind to model= and cause KeyError downstream."""
        from app.services.crm_service import order_by_clock

        query = db_session.query(Company)
        with pytest.raises(TypeError):
            order_by_clock(query, "outbound", NOW)  # type: ignore[call-arg]


class TestCdmCompanyQueryClockSorts:
    """Regression tests for P3-5: cdm_company_query outbound_asc / reply_asc sorts.

    The original bug: order_by_clock(query, "outbound", now) — positional now —
    bound the datetime to model=, causing _CLOCK_COLUMNS[<datetime>] → KeyError
    → 500 on the CDM page.  These tests call the real cdm_company_query path so
    any recurrence will surface here first.
    """

    def test_outbound_asc_sort_does_not_raise(self, db_session: Session):
        """cdm_company_query with sort='outbound_asc' returns sorted results, no
        KeyError."""
        from app.models import User
        from app.services.crm_service import cdm_company_query

        user = User(email="test-clock@example.com", name="Clock Tester", is_active=True)
        db_session.add(user)

        c_null = Company(name="Clock-Null Co", is_active=True, last_outbound_at=None)
        c_old = Company(
            name="Clock-Old Co",
            is_active=True,
            last_outbound_at=NOW - timedelta(days=30),
        )
        c_recent = Company(
            name="Clock-Recent Co",
            is_active=True,
            last_outbound_at=NOW - timedelta(days=3),
        )
        db_session.add_all([c_recent, c_old, c_null])
        db_session.commit()

        # Must not raise KeyError / TypeError
        results = cdm_company_query(
            db_session,
            user,
            search="",
            staleness="",
            account_type="",
            my_only=False,
            sort="outbound_asc",
            now=NOW,
        ).all()

        names = [c.name for c in results if c.name.startswith("Clock-")]
        assert names.index("Clock-Null Co") < names.index("Clock-Old Co")
        assert names.index("Clock-Old Co") < names.index("Clock-Recent Co")

    def test_reply_asc_sort_does_not_raise(self, db_session: Session):
        """cdm_company_query with sort='reply_asc' returns sorted results, no
        KeyError."""
        from app.models import User
        from app.services.crm_service import cdm_company_query

        user = User(email="test-reply-clock@example.com", name="Reply Tester", is_active=True)
        db_session.add(user)

        c_null = Company(name="Reply-Null Co", is_active=True, last_reply_at=None)
        c_old = Company(
            name="Reply-Old Co",
            is_active=True,
            last_reply_at=NOW - timedelta(days=25),
        )
        c_recent = Company(
            name="Reply-Recent Co",
            is_active=True,
            last_reply_at=NOW - timedelta(days=2),
        )
        db_session.add_all([c_recent, c_old, c_null])
        db_session.commit()

        # Must not raise KeyError / TypeError
        results = cdm_company_query(
            db_session,
            user,
            search="",
            staleness="",
            account_type="",
            my_only=False,
            sort="reply_asc",
            now=NOW,
        ).all()

        names = [c.name for c in results if c.name.startswith("Reply-")]
        assert names.index("Reply-Null Co") < names.index("Reply-Old Co")
        assert names.index("Reply-Old Co") < names.index("Reply-Recent Co")
