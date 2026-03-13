"""
test_coverage_services_100.py — Close every coverage gap in app/services/*.

Covers all remaining uncovered lines across 30+ service modules.
Organized by file, smallest gaps first.

Called by: pytest
Depends on: conftest.py, all service modules
"""

import os

os.environ["TESTING"] = "1"
os.environ["RATE_LIMIT_ENABLED"] = "false"

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy import text as sqltext
from sqlalchemy.pool import StaticPool

from app.models import (
    ActivityLog,
    Company,
    CustomerSite,
    Offer,
    ProactiveDoNotOffer,
    ProactiveMatch,
    ProactiveOffer,
    Requirement,
    Requisition,
    SiteContact,
    User,
    VendorCard,
)

# ═══════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════


def _make_user(db, email="test@trioscs.com", name="Test User", role="sales"):
    u = User(
        email=email,
        name=name,
        role=role,
        azure_id=f"az-{email}",
        m365_connected=True,
        created_at=datetime.now(timezone.utc),
    )
    db.add(u)
    db.flush()
    return u


def _make_company(db, name="Acme Corp", domain=None, owner_id=None, last_activity_at=None):
    co = Company(
        name=name,
        is_active=True,
        domain=domain,
        account_owner_id=owner_id,
        last_activity_at=last_activity_at,
        created_at=datetime.now(timezone.utc),
    )
    db.add(co)
    db.flush()
    return co


def _make_site(db, company_id, site_name="HQ", email=None):
    site = CustomerSite(
        company_id=company_id,
        site_name=site_name,
        contact_email=email,
    )
    db.add(site)
    db.flush()
    return site


def _make_contact(db, site_id, email="contact@example.com", full_name="Contact"):
    sc = SiteContact(
        customer_site_id=site_id,
        full_name=full_name,
        email=email,
    )
    db.add(sc)
    db.flush()
    return sc


def _make_requisition(db, user_id, site_id=None, status="archived", mpn="LM317T"):
    from app.models import MaterialCard

    norm = mpn.strip().lower()
    card = db.query(MaterialCard).filter_by(normalized_mpn=norm).first()
    if not card:
        card = MaterialCard(normalized_mpn=norm, display_mpn=mpn, search_count=0)
        db.add(card)
        db.flush()
    req = Requisition(
        name=f"Req-{mpn}",
        customer_name="Test Co",
        status=status,
        created_by=user_id,
        customer_site_id=site_id,
        created_at=datetime.now(timezone.utc) - timedelta(days=60),
    )
    db.add(req)
    db.flush()
    item = Requirement(
        requisition_id=req.id,
        primary_mpn=mpn,
        normalized_mpn=norm,
        material_card_id=card.id,
        target_qty=500,
        created_at=datetime.now(timezone.utc) - timedelta(days=60),
    )
    db.add(item)
    db.flush()
    return req, item


def _make_offer(db, req, user_id, mpn="LM317T", qty=1000, price=0.50):
    from app.models import MaterialCard

    norm = mpn.strip().lower()
    card = db.query(MaterialCard).filter_by(normalized_mpn=norm).first()
    if not card:
        card = MaterialCard(normalized_mpn=norm, display_mpn=mpn, search_count=0)
        db.add(card)
        db.flush()
    o = Offer(
        requisition_id=req.id,
        vendor_name="SupplierCo",
        mpn=mpn,
        qty_available=qty,
        unit_price=price,
        entered_by_id=user_id,
        material_card_id=card.id,
        status="active",
        created_at=datetime.now(timezone.utc),
    )
    db.add(o)
    db.flush()
    return o


def _make_sqlite_engine():
    return create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


# ═══════════════════════════════════════════════════════════════════════
# 1. prospect_discovery_apollo.py — line 35 (_get_api_key)
# ═══════════════════════════════════════════════════════════════════════


class TestProspectDiscoveryApolloApiKey:
    def test_get_api_key_returns_setting(self):
        """_get_api_key returns the apollo_api_key setting value."""
        from app.services.prospect_discovery_apollo import _get_api_key

        with patch("app.services.prospect_discovery_apollo.settings") as mock_settings:
            mock_settings.apollo_api_key = "test-key-123"
            assert _get_api_key() == "test-key-123"

    def test_get_api_key_returns_empty_when_missing(self):
        """_get_api_key returns '' when setting is not present."""
        from app.services.prospect_discovery_apollo import _get_api_key

        with patch("app.services.prospect_discovery_apollo.settings") as mock_settings:
            del mock_settings.apollo_api_key
            assert _get_api_key() == ""


# ═══════════════════════════════════════════════════════════════════════
# 2. buyplan_v3_notifications.py — lines 66-67
# ═══════════════════════════════════════════════════════════════════════


class TestBuyplanV3NotificationsCustomerSiteNoCompany:
    def test_plan_context_site_without_company(self, db_session):
        """When quote has customer_site but site.company is None, use site_name."""
        from app.services.buyplan_v3_notifications import _plan_context

        # Create a mock quote with customer_site that has no company
        mock_site = MagicMock()
        mock_site.company = None
        mock_site.site_name = "Standalone Site"

        mock_quote = MagicMock()
        mock_quote.quote_number = "Q-TEST-001"
        mock_quote.customer_site = mock_site

        mock_plan = MagicMock()
        mock_plan.submitted_by_id = None
        mock_plan.quote_id = 1

        # db.get returns None for User (no submitter), mock_quote for Quote
        def mock_get(model, id):
            if model is User:
                return None
            return mock_quote

        with patch.object(db_session, "get", side_effect=mock_get):
            ctx = _plan_context(mock_plan, db_session)

        assert ctx["customer_name"] == "Standalone Site"

    def test_plan_context_site_no_company_no_sitename(self, db_session):
        """When site.company is None and site_name is None, return empty string."""
        from app.services.buyplan_v3_notifications import _plan_context

        mock_site = MagicMock()
        mock_site.company = None
        mock_site.site_name = None

        mock_quote = MagicMock()
        mock_quote.quote_number = "Q-002"
        mock_quote.customer_site = mock_site

        mock_plan = MagicMock()
        mock_plan.submitted_by_id = None
        mock_plan.quote_id = 1

        def mock_get(model, id):
            if model is User:
                return None
            return mock_quote

        with patch.object(db_session, "get", side_effect=mock_get):
            ctx = _plan_context(mock_plan, db_session)

        assert ctx["customer_name"] == ""


# ═══════════════════════════════════════════════════════════════════════
# 3. requisition_state.py — lines 60-61
# ═══════════════════════════════════════════════════════════════════════


class TestRequisitionStateLogFailure:
    def test_transition_log_failure_swallowed(self, db_session, test_user):
        """Exception in ActivityLog creation is caught and logged."""
        from app.services.requisition_state import transition

        req = Requisition(
            name="Log-Fail",
            status="active",
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.commit()

        # Force ActivityLog creation to raise by making db.add raise
        original_add = db_session.add

        def failing_add(obj):
            if isinstance(obj, ActivityLog):
                raise Exception("DB error")
            original_add(obj)

        with patch.object(db_session, "add", side_effect=failing_add):
            transition(req, "sourcing", test_user, db_session)

        assert req.status == "sourcing"


# ═══════════════════════════════════════════════════════════════════════
# 4. ownership_service.py — lines 583, 601
# ═══════════════════════════════════════════════════════════════════════


class TestOwnershipNaiveDatetimes:
    def test_site_days_since_activity_naive_datetime(self):
        """Naive datetime on site.last_activity_at gets UTC tzinfo."""
        from app.services.ownership_service import _site_days_since_activity

        site = MagicMock()
        site.last_activity_at = datetime(2026, 2, 15, 12, 0, 0)

        now = datetime(2026, 2, 25, 12, 0, 0, tzinfo=timezone.utc)
        days = _site_days_since_activity(site, now)
        assert days == 10

    def test_days_since_activity_naive_datetime(self):
        """Naive datetime on company.last_activity_at gets UTC tzinfo."""
        from app.services.ownership_service import _days_since_activity

        company = MagicMock()
        company.last_activity_at = datetime(2026, 2, 20, 12, 0, 0)

        now = datetime(2026, 2, 25, 12, 0, 0, tzinfo=timezone.utc)
        days = _days_since_activity(company, now)
        assert days == 5


# ═══════════════════════════════════════════════════════════════════════
# 5. deep_enrichment_service.py — lines 537-538
# ═══════════════════════════════════════════════════════════════════════


class TestDeepEnrichCompanyCustomerEnrichError:
    @pytest.mark.asyncio
    async def test_customer_enrichment_exception_handled(self, db_session):
        """Exception in customer enrichment waterfall is caught and logged."""
        from app.services.deep_enrichment_service import deep_enrich_company

        co = _make_company(db_session, "Customer Co", domain="customer.com")
        co.account_type = "Customer"
        co.deep_enrichment_at = None
        db_session.commit()

        mock_settings = MagicMock()
        mock_settings.customer_enrichment_enabled = True
        mock_settings.deep_enrichment_stale_days = 30
        mock_settings.deep_enrichment_clearbit_key = ""
        mock_settings.explorium_api_key = ""
        mock_settings.apollo_api_key = ""
        mock_settings.gradient_api_key = ""
        mock_settings.anthropic_api_key = ""
        mock_settings.lusha_api_key = ""
        mock_settings.hunter_api_key = ""

        with (
            patch("app.config.settings", mock_settings),
            patch(
                "app.services.customer_enrichment_service.enrich_customer_account",
                new_callable=AsyncMock,
                side_effect=Exception("API timeout"),
            ),
        ):
            result = await deep_enrich_company(co.id, db_session, force=True)

        assert result is not None


# ═══════════════════════════════════════════════════════════════════════
# 6. mailbox_intelligence.py — lines 95-96
# ═══════════════════════════════════════════════════════════════════════


class TestMailboxIntelligenceInvalidHours:
    def test_invalid_working_hours_format(self):
        """Invalid working hours format returns True (fail-open)."""
        from app.services.mailbox_intelligence import is_within_working_hours

        user = MagicMock()
        user.working_hours_start = "invalid"
        user.working_hours_end = "also-invalid"
        assert is_within_working_hours(user, 10) is True

    def test_empty_colon_working_hours(self):
        """Working hours with just colons but no valid numbers return True."""
        from app.services.mailbox_intelligence import is_within_working_hours

        user = MagicMock()
        user.working_hours_start = ":"
        user.working_hours_end = ":"
        assert is_within_working_hours(user, 10) is True


# ═══════════════════════════════════════════════════════════════════════
# 7. calendar_intelligence.py — lines 90, 115-117
# ═══════════════════════════════════════════════════════════════════════


class TestCalendarIntelligenceEdgeCases:
    @pytest.mark.asyncio
    async def test_attendee_without_at_sign_skipped(self, db_session):
        """Attendees without @ in email are skipped."""
        from app.services.calendar_intelligence import scan_calendar_events

        user = _make_user(db_session, "cal-user@trioscs.com")
        db_session.commit()

        event = {
            "subject": "Meeting with vendor",
            "attendees": [
                {"emailAddress": {"address": "no-at-sign", "name": "Bad"}},
                {"emailAddress": {"address": "vendor@ext.com", "name": "Good"}},
            ],
            "start": {"dateTime": "2026-02-20T10:00:00"},
            "end": {"dateTime": "2026-02-20T11:00:00"},
        }

        mock_gc = MagicMock()
        mock_gc.get_all_pages = AsyncMock(return_value=[event])

        with (
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.config.settings") as mock_settings,
        ):
            mock_settings.own_domains = ["trioscs.com"]
            result = await scan_calendar_events("fake-token", user.id, db_session, lookback_days=30)

        assert result["vendor_meetings"] == 1
        assert result["events_scanned"] == 1

    @pytest.mark.asyncio
    async def test_commit_failure_rolls_back(self, db_session):
        """Commit failure during activity logging rolls back gracefully."""
        from app.services.calendar_intelligence import scan_calendar_events

        user = _make_user(db_session, "cal-commit@trioscs.com")
        db_session.commit()

        event = {
            "subject": "APEC conference",
            "attendees": [
                {"emailAddress": {"address": "vendor@ext.com", "name": "V"}},
            ],
            "start": {"dateTime": "2026-02-20T10:00:00"},
            "end": {"dateTime": "2026-02-20T11:00:00"},
        }

        mock_gc = MagicMock()
        mock_gc.get_all_pages = AsyncMock(return_value=[event])

        original_commit = db_session.commit
        commit_called = [False]

        def failing_commit():
            if commit_called[0]:
                raise Exception("commit fail")
            commit_called[0] = True
            original_commit()

        with (
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.config.settings") as mock_settings,
        ):
            mock_settings.own_domains = ["trioscs.com"]
            # Patch commit to fail on the second call (activities commit)
            db_session.commit = failing_commit
            result = await scan_calendar_events("fake-token", user.id, db_session, lookback_days=30)

        assert result["activities_logged"] >= 1

    @pytest.mark.asyncio
    async def test_commit_exception_triggers_rollback(self, db_session):
        """Lines 115-117: commit raises on first call, triggering rollback."""
        from app.services.calendar_intelligence import scan_calendar_events

        user = _make_user(db_session, "cal-rollback@trioscs.com")
        db_session.commit()

        event = {
            "subject": "APEC conference",
            "attendees": [
                {"emailAddress": {"address": "vendor@ext.com", "name": "V"}},
            ],
            "start": {"dateTime": "2026-02-20T10:00:00"},
            "end": {"dateTime": "2026-02-20T11:00:00"},
        }

        mock_gc = MagicMock()
        mock_gc.get_all_pages = AsyncMock(return_value=[event])

        original_rollback = db_session.rollback

        def always_fail_commit():
            raise Exception("DB commit exploded")

        with (
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.config.settings") as mock_settings,
        ):
            mock_settings.own_domains = ["trioscs.com"]
            db_session.commit = always_fail_commit
            db_session.rollback = MagicMock(side_effect=original_rollback)
            result = await scan_calendar_events("fake-token", user.id, db_session, lookback_days=30)

        # The result should still be returned (commit failed but was handled)
        assert result["events_scanned"] == 1
        assert result["trade_shows"] == 1


# ═══════════════════════════════════════════════════════════════════════
# 8. company_merge_service.py — lines 125-126, 136-137
# ═══════════════════════════════════════════════════════════════════════


class TestCompanyMergeEdgeCases:
    def test_fk_reassignment_failure_handled(self, db_session):
        """Exception in FK reassignment is caught and logged."""
        from app.services.company_merge_service import merge_companies

        keep = _make_company(db_session, "Keep Corp")
        remove = _make_company(db_session, "Remove Corp")
        db_session.commit()

        original_query = db_session.query

        def failing_query(model):
            q = original_query(model)
            if model.__tablename__ in ("activity_log", "enrichment_queue", "sightings"):
                mock_q = MagicMock()
                mock_q.filter.return_value.update.side_effect = Exception("FK error")
                return mock_q
            return q

        with patch.object(db_session, "query", side_effect=failing_query):
            result = merge_companies(keep.id, remove.id, db_session)

        assert result["ok"] is True

    def test_cache_invalidation_failure_handled(self, db_session):
        """Exception in cache invalidation is caught and logged."""
        from app.services.company_merge_service import merge_companies

        keep = _make_company(db_session, "Keep2 Corp")
        remove = _make_company(db_session, "Remove2 Corp")
        db_session.commit()

        with patch(
            "app.cache.decorators.invalidate_prefix",
            side_effect=Exception("cache error"),
        ):
            result = merge_companies(keep.id, remove.id, db_session)

        assert result["ok"] is True


# ═══════════════════════════════════════════════════════════════════════
# 9. proactive_service.py — lines 105, 212, 233, 393
# ═══════════════════════════════════════════════════════════════════════


class TestProactiveServiceDNOAndMargin:
    def test_scan_skips_dno_matches(self, db_session):
        """Scanning skips matches suppressed by do-not-offer."""
        import app.services.proactive_service as mod

        mod._last_proactive_scan = datetime.min.replace(tzinfo=timezone.utc)

        raw_conn = db_session.get_bind().raw_connection()
        raw_conn.create_function("btrim", 1, lambda s: s.strip() if s else s)
        raw_conn.close()

        user = _make_user(db_session, "dno-sales@trioscs.com")
        co = _make_company(db_session, "DNO Corp")
        site = _make_site(db_session, co.id, "DNO Site", "x@dno.com")

        req, item = _make_requisition(db_session, user.id, site.id, "archived", "DNO_PART")

        source_req = Requisition(
            name="Source-DNO",
            customer_name="OtherCo",
            status="open",
            created_by=user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(source_req)
        db_session.flush()
        _make_offer(db_session, source_req, user.id, "DNO_PART")

        dno = ProactiveDoNotOffer(
            mpn="DNO_PART",
            company_id=co.id,
            created_by_id=user.id,
        )
        db_session.add(dno)
        db_session.commit()

        from app.services.proactive_service import scan_new_offers_for_matches

        result = scan_new_offers_for_matches(db_session)

        assert result["scanned"] >= 1
        matches = db_session.query(ProactiveMatch).filter_by(mpn="DNO_PART").all()
        assert len(matches) == 0

    def test_get_matches_skips_dno_set(self, db_session):
        """get_matches_for_user filters matches by do-not-offer set."""
        from app.services.proactive_service import get_matches_for_user

        user = _make_user(db_session, "dno2-sales@trioscs.com")
        co = _make_company(db_session, "DNO2 Corp")
        site = _make_site(db_session, co.id, "DNO2 Site", "x@dno2.com")
        req, item = _make_requisition(db_session, user.id, site.id, "archived", "DNO2PART")
        offer = _make_offer(db_session, req, user.id, "DNO2PART")

        m = ProactiveMatch(
            offer_id=offer.id,
            requirement_id=item.id,
            requisition_id=req.id,
            customer_site_id=site.id,
            company_id=co.id,
            salesperson_id=user.id,
            mpn="DNO2PART",
            status="new",
        )
        db_session.add(m)

        dno = ProactiveDoNotOffer(
            mpn="DNO2PART",
            company_id=co.id,
            created_by_id=user.id,
        )
        db_session.add(dno)
        db_session.commit()

        result = get_matches_for_user(db_session, user.id, status="new")
        assert result["stats"]["total"] == 0

    def test_get_matches_high_margin_count(self, db_session):
        """Matches with margin_pct > 30 are counted in high_margin_count."""
        from app.services.proactive_service import get_matches_for_user

        user = _make_user(db_session, "margin-sales@trioscs.com")
        co = _make_company(db_session, "Margin Corp")
        site = _make_site(db_session, co.id, "Margin Site", "x@margin.com")
        req, item = _make_requisition(db_session, user.id, site.id, "archived", "HIGHMPN")
        offer = _make_offer(db_session, req, user.id, "HIGHMPN")

        m = ProactiveMatch(
            offer_id=offer.id,
            requirement_id=item.id,
            requisition_id=req.id,
            customer_site_id=site.id,
            salesperson_id=user.id,
            mpn="HIGHMPN",
            status="new",
            margin_pct=45.0,
        )
        db_session.add(m)
        db_session.commit()

        result = get_matches_for_user(db_session, user.id, status="new")
        assert result["stats"]["high_margin_count"] == 1

    @pytest.mark.asyncio
    async def test_send_proactive_offer_with_email_html(self, db_session):
        """When email_html is provided, it is used directly instead of template."""
        from app.services.proactive_service import send_proactive_offer

        user = _make_user(db_session, "html-sales@trioscs.com")
        co = _make_company(db_session, "HTML Corp")
        site = _make_site(db_session, co.id, "HTML Site", "x@html.com")
        contact = _make_contact(db_session, site.id, "buyer@html.com", "Buyer")
        req, item = _make_requisition(db_session, user.id, site.id, "archived", "HTMLMPN")
        offer = _make_offer(db_session, req, user.id, "HTMLMPN")

        m = ProactiveMatch(
            offer_id=offer.id,
            requirement_id=item.id,
            requisition_id=req.id,
            customer_site_id=site.id,
            salesperson_id=user.id,
            mpn="HTMLMPN",
        )
        db_session.add(m)
        db_session.commit()

        mock_gc = MagicMock()
        mock_gc.post_json = AsyncMock(return_value=None)

        with patch("app.utils.graph_client.GraphClient", return_value=mock_gc):
            result = await send_proactive_offer(
                db=db_session,
                user=user,
                token="fake-token",
                match_ids=[m.id],
                contact_ids=[contact.id],
                sell_prices={},
                subject="Custom Subject",
                email_html="<p>Custom HTML body</p>",
            )

        po = db_session.get(ProactiveOffer, result["id"])
        assert po.email_body_html == "<p>Custom HTML body</p>"
        assert po.subject == "Custom Subject"


# ═══════════════════════════════════════════════════════════════════════
# 10. startup.py — lines 57-89 (_create_default_user_if_env_set), 542
# ═══════════════════════════════════════════════════════════════════════


class TestStartupDefaultUser:
    def test_create_default_user_env_set(self):
        """_create_default_user_if_env_set creates user when env vars set."""
        from app.startup import _create_default_user_if_env_set

        mock_db = MagicMock()
        mock_db.query.return_value.filter_by.return_value.first.return_value = None

        with (
            patch.dict(
                os.environ,
                {
                    "DEFAULT_USER_EMAIL": "admin@test.com",
                    "DEFAULT_USER_PASSWORD": "secret123",
                    "DEFAULT_USER_ROLE": "admin",
                },
            ),
            patch("app.startup.SessionLocal", return_value=mock_db),
        ):
            _create_default_user_if_env_set()

        mock_db.add.assert_called_once()
        mock_db.commit.assert_called_once()
        mock_db.close.assert_called_once()

    def test_create_default_user_already_exists(self):
        """_create_default_user_if_env_set skips when user exists."""
        from app.startup import _create_default_user_if_env_set

        mock_db = MagicMock()
        mock_db.query.return_value.filter_by.return_value.first.return_value = MagicMock()

        with (
            patch.dict(
                os.environ,
                {
                    "DEFAULT_USER_EMAIL": "admin@test.com",
                    "DEFAULT_USER_PASSWORD": "secret123",
                },
            ),
            patch("app.startup.SessionLocal", return_value=mock_db),
        ):
            _create_default_user_if_env_set()

        mock_db.add.assert_not_called()

    def test_create_default_user_no_env(self):
        """_create_default_user_if_env_set does nothing without env vars."""
        from app.startup import _create_default_user_if_env_set

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DEFAULT_USER_EMAIL", None)
            os.environ.pop("DEFAULT_USER_PASSWORD", None)
            _create_default_user_if_env_set()

    def test_create_default_user_exception(self):
        """_create_default_user_if_env_set catches exceptions."""
        from app.startup import _create_default_user_if_env_set

        mock_db = MagicMock()
        mock_db.query.return_value.filter_by.return_value.first.side_effect = Exception("boom")

        with (
            patch.dict(
                os.environ,
                {
                    "DEFAULT_USER_EMAIL": "admin@test.com",
                    "DEFAULT_USER_PASSWORD": "secret123",
                },
            ),
            patch("app.startup.SessionLocal", return_value=mock_db),
        ):
            _create_default_user_if_env_set()  # should not raise

        mock_db.close.assert_called_once()


class TestStartupBackfillNormKey:
    def test_key_with_empty_string_returns_empty(self):
        """_key('') in _backfill_sighting_offer_normalized_mpn returns ''."""
        from app.startup import _backfill_sighting_offer_normalized_mpn

        eng = _make_sqlite_engine()
        with eng.connect() as conn:
            conn.execute(
                sqltext("CREATE TABLE sightings (id INTEGER PRIMARY KEY, mpn_matched TEXT, normalized_mpn TEXT)")
            )
            # Row with empty mpn_matched triggers _key('') -> return ""
            conn.execute(sqltext("INSERT INTO sightings (id, mpn_matched, normalized_mpn) VALUES (1, '', NULL)"))
            # Row with actual MPN
            conn.execute(sqltext("INSERT INTO sightings (id, mpn_matched, normalized_mpn) VALUES (2, 'LM317T', NULL)"))
            conn.execute(sqltext("CREATE TABLE offers (id INTEGER PRIMARY KEY, mpn TEXT, normalized_mpn TEXT)"))
            conn.execute(sqltext("INSERT INTO offers (id, mpn, normalized_mpn) VALUES (1, '', NULL)"))
            conn.commit()

        with patch("app.startup.engine", eng):
            _backfill_sighting_offer_normalized_mpn()


# ═══════════════════════════════════════════════════════════════════════
# 11. contact_quality.py — lines 63, 68-69, 73, 75, 143, 171, 176-181
# ═══════════════════════════════════════════════════════════════════════


class TestContactQualityDedup:
    def test_dedup_skips_no_email_contacts(self, db_session):
        """Contacts without email are skipped during dedup."""
        from app.services.contact_quality import dedup_contacts

        co = _make_company(db_session, "Dedup Corp")
        site = _make_site(db_session, co.id, "Dedup Site")

        # Contact with no email (should be skipped)
        c1 = SiteContact(customer_site_id=site.id, full_name="No Email", email=None)
        # Contact with email
        c2 = SiteContact(customer_site_id=site.id, full_name="Has Email", email="a@test.com")
        db_session.add_all([c1, c2])
        db_session.flush()

        merged = dedup_contacts(db_session, site.id)
        assert merged == 0

    def test_dedup_merges_phone_linkedin_role(self, db_session):
        """Duplicate contacts merge phone, linkedin, and contact_role fields."""
        from app.services.contact_quality import dedup_contacts

        co = _make_company(db_session, "Merge Fields Corp")
        site = _make_site(db_session, co.id, "Merge Site")

        # Primary contact (created first)
        c1 = SiteContact(
            customer_site_id=site.id,
            full_name="Jane",
            email="jane@merge.com",
            phone=None,
            linkedin_url=None,
            contact_role=None,
            created_at=datetime.now(timezone.utc) - timedelta(days=1),
        )
        # Duplicate with extra fields
        c2 = SiteContact(
            customer_site_id=site.id,
            full_name="Jane Doe",
            email="jane@merge.com",
            phone="+1234567890",
            phone_verified=True,
            linkedin_url="https://linkedin.com/in/jane",
            contact_role="procurement",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add_all([c1, c2])
        db_session.flush()

        merged = dedup_contacts(db_session, site.id)
        assert merged == 1

        # Check that primary got the merged fields
        db_session.refresh(c1)
        assert c1.phone == "+1234567890"
        assert c1.linkedin_url == "https://linkedin.com/in/jane"
        assert c1.contact_role == "procurement"


class TestContactQualityEnrichmentStatus:
    def test_compute_status_no_sites(self, db_session):
        """Company with no sites returns 'missing'."""
        from app.services.contact_quality import compute_enrichment_status

        co = _make_company(db_session, "No Sites Corp")
        db_session.commit()

        with patch("app.services.contact_quality.settings") as mock_settings:
            mock_settings.customer_enrichment_contacts_per_account = 3
            status = compute_enrichment_status(db_session, co.id)

        assert status == "missing"

    def test_update_company_enrichment_status(self, db_session):
        """update_company_enrichment_status persists the status."""
        from app.services.contact_quality import update_company_enrichment_status

        co = _make_company(db_session, "Status Corp")
        site = _make_site(db_session, co.id, "Status Site")
        _make_contact(db_session, site.id, "a@status.com", "Alice")
        db_session.commit()

        with patch("app.services.contact_quality.settings") as mock_settings:
            mock_settings.customer_enrichment_contacts_per_account = 3
            status = update_company_enrichment_status(db_session, co.id)

        assert status == "partial"
        db_session.refresh(co)
        assert co.customer_enrichment_status == "partial"

    def test_update_company_enrichment_status_nonexistent(self, db_session):
        """update_company_enrichment_status with nonexistent company."""
        from app.services.contact_quality import update_company_enrichment_status

        with patch("app.services.contact_quality.settings") as mock_settings:
            mock_settings.customer_enrichment_contacts_per_account = 3
            status = update_company_enrichment_status(db_session, 99999)

        assert status == "missing"


# ═══════════════════════════════════════════════════════════════════════
# 12. email_intelligence_service.py — lines 84-85, 101-108, 220, 250-253
# ═══════════════════════════════════════════════════════════════════════


class TestEmailIntelligenceGaps:
    @pytest.mark.asyncio
    async def test_classify_email_ai_invalid_confidence(self):
        """Invalid confidence value gets clamped to 0.5."""
        from app.services.email_intelligence_service import classify_email_ai

        mock_result = {
            "classification": "offer",
            "confidence": "not_a_number",
            "has_pricing": True,
            "parts_mentioned": [],
            "brands_detected": [],
            "commodities_detected": [],
        }

        with patch(
            "app.services.gradient_service.gradient_json",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            result = await classify_email_ai("Re: Quote", "Price: $1.50", "vendor@test.com")

        assert result["confidence"] == 0.5

    @pytest.mark.asyncio
    async def test_extract_pricing_intelligence(self):
        """extract_pricing_intelligence delegates to parse_email."""
        from app.services.email_intelligence_service import extract_pricing_intelligence

        mock_parsed = {"line_items": [{"mpn": "LM317T", "price": 1.50}]}

        with patch(
            "app.services.ai_email_parser.parse_email",
            new_callable=AsyncMock,
            return_value=mock_parsed,
        ):
            result = await extract_pricing_intelligence("Quote", "Price: $1.50", "vendor@test.com", "Vendor")

        assert result == mock_parsed

    @pytest.mark.asyncio
    async def test_process_email_ai_classification_fails(self, db_session):
        """When AI classification returns None, process_email_intelligence returns None."""
        from app.services.email_intelligence_service import process_email_intelligence

        user = _make_user(db_session, "intel-user@trioscs.com")
        db_session.commit()

        with patch(
            "app.services.email_intelligence_service.classify_email_ai",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await process_email_intelligence(
                db=db_session,
                message_id="msg-123",
                user_id=user.id,
                sender_email="vendor@test.com",
                sender_name="Vendor",
                subject="Ambiguous email",
                body="Some content",
                received_at=datetime.now(timezone.utc),
                conversation_id=None,
                regex_offer_matches=0,
            )

        assert result is None

    @pytest.mark.asyncio
    async def test_process_email_store_failure(self, db_session):
        """When store_email_intelligence raises, returns None."""
        from app.services.email_intelligence_service import process_email_intelligence

        user = _make_user(db_session, "intel2-user@trioscs.com")
        db_session.commit()

        classification = {
            "classification": "general",
            "confidence": 0.9,
            "has_pricing": False,
            "parts_mentioned": [],
            "brands_detected": [],
            "commodities_detected": [],
        }

        with (
            patch(
                "app.services.email_intelligence_service.classify_email_ai",
                new_callable=AsyncMock,
                return_value=classification,
            ),
            patch(
                "app.services.email_intelligence_service.store_email_intelligence",
                side_effect=Exception("DB error"),
            ),
            patch.object(db_session, "rollback"),
        ):
            result = await process_email_intelligence(
                db=db_session,
                message_id="msg-456",
                user_id=user.id,
                sender_email="vendor@test.com",
                sender_name="Vendor",
                subject="General email",
                body="Some content",
                received_at=datetime.now(timezone.utc),
                conversation_id=None,
                regex_offer_matches=0,
            )

        assert result is None


# ═══════════════════════════════════════════════════════════════════════
# 13. integrity_service.py — lines 162-164, 186-188, 210-212, 338
# ═══════════════════════════════════════════════════════════════════════


class TestIntegrityServiceGaps:
    def test_heal_orphans_requirement_error(self, db_session):
        """Exception healing a requirement is caught."""
        from app.services.integrity_service import heal_orphaned_records

        # Create a requirement with no material card
        user = _make_user(db_session, "integrity-user@trioscs.com")
        req = Requisition(
            name="IntReq",
            status="active",
            created_by=user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.flush()
        item = Requirement(
            requisition_id=req.id,
            primary_mpn="ORPHAN1",
            target_qty=100,
            material_card_id=None,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(item)
        db_session.commit()

        with patch(
            "app.search_service.resolve_material_card",
            side_effect=Exception("resolve failed"),
        ):
            result = heal_orphaned_records(db_session, batch_size=10)

        assert isinstance(result, dict)

    def test_full_check_critical_status(self, db_session):
        """Full check returns 'critical' when many orphans and dup cards."""
        from app.services.integrity_service import run_integrity_check

        with (
            patch("app.services.integrity_service.check_orphaned_requirements", return_value=100),
            patch("app.services.integrity_service.check_orphaned_sightings", return_value=100),
            patch("app.services.integrity_service.check_orphaned_offers", return_value=100),
            patch(
                "app.services.integrity_service.check_dangling_fks",
                return_value={
                    "requirements": 0,
                    "sightings": 0,
                    "offers": 0,
                },
            ),
            patch("app.services.integrity_service.check_duplicate_cards", return_value=5),
            patch("app.services.integrity_service.check_vendor_history_duplicates", return_value=0),
            patch(
                "app.services.integrity_service.heal_orphaned_records",
                return_value={
                    "requirements": 0,
                    "sightings": 0,
                    "offers": 0,
                },
            ),
            patch(
                "app.services.integrity_service.clear_dangling_fks",
                return_value={
                    "requirements": 0,
                    "sightings": 0,
                    "offers": 0,
                },
            ),
            patch("app.services.integrity_service._compute_linkage_coverage", return_value={}),
        ):
            result = run_integrity_check(db_session)

        assert result["status"] == "critical"


# ═══════════════════════════════════════════════════════════════════════
# 14. prospect_discovery_email.py — lines 68-72, 172-173
# ═══════════════════════════════════════════════════════════════════════


class TestProspectDiscoveryEmailGaps:
    @pytest.mark.asyncio
    async def test_mine_unknown_domains_with_vendor_emails(self, db_session):
        """Vendor card emails are normalized for domain extraction."""
        from app.services.prospect_discovery_email import mine_unknown_domains

        vc = VendorCard(
            display_name="Test Vendor",
            normalized_name="test vendor",
            emails=["sales@vendor.com", "info@vendor.com"],
        )
        db_session.add(vc)
        db_session.commit()

        mock_gc = MagicMock()
        mock_gc.get_all_pages = AsyncMock(return_value=[])

        result = await mine_unknown_domains(mock_gc, db_session, days_back=30)
        assert isinstance(result, list)

    @pytest.mark.asyncio
    async def test_enrich_email_domains_apollo_fallback_error(self):
        """Apollo fallback exception is caught in enrich_email_domains."""
        from app.services.prospect_discovery_email import enrich_email_domains

        domains = [{"domain": "unknown.com", "email_count": 5, "sample_senders": ["a@unknown.com"]}]

        async def failing_apollo(domain):
            raise Exception("Apollo down")

        result = await enrich_email_domains(
            domains=domains,
            enrich_fn=AsyncMock(return_value=None),
            apollo_enrich_fn=failing_apollo,
        )

        assert isinstance(result, list)


# ═══════════════════════════════════════════════════════════════════════
# 15. customer_enrichment_batch.py — lines 62-63, 71-74, 126, 132
# ═══════════════════════════════════════════════════════════════════════


class TestCustomerEnrichmentBatchGaps:
    @pytest.mark.asyncio
    async def test_batch_stops_when_credits_exhausted(self, db_session):
        """Batch enrichment stops early when all credit budgets exhausted."""
        from app.services.customer_enrichment_batch import run_customer_enrichment_batch

        with (
            patch("app.services.customer_enrichment_batch.settings") as mock_settings,
            patch(
                "app.services.customer_enrichment_batch.get_enrichment_gaps",
                return_value=[{"company_id": 1}, {"company_id": 2}],
            ),
            patch(
                "app.services.customer_enrichment_batch.can_use_credits",
                return_value=False,
            ),
            patch(
                "app.services.customer_enrichment_batch.enrich_customer_account",
                new_callable=AsyncMock,
            ) as mock_enrich,
        ):
            mock_settings.customer_enrichment_enabled = True
            result = await run_customer_enrichment_batch(db_session)

        assert result["processed"] == 0
        mock_enrich.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_batch_enrichment_error_handled(self, db_session):
        """Exception enriching an account is caught and logged."""
        from app.services.customer_enrichment_batch import run_customer_enrichment_batch

        with (
            patch("app.services.customer_enrichment_batch.settings") as mock_settings,
            patch(
                "app.services.customer_enrichment_batch.get_enrichment_gaps",
                return_value=[{"company_id": 1}],
            ),
            patch(
                "app.services.customer_enrichment_batch.can_use_credits",
                return_value=True,
            ),
            patch(
                "app.services.customer_enrichment_batch.enrich_customer_account",
                new_callable=AsyncMock,
                side_effect=Exception("API error"),
            ),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_settings.customer_enrichment_enabled = True
            result = await run_customer_enrichment_batch(db_session)

        assert result["errors"] == 1

    @pytest.mark.asyncio
    async def test_email_reverification_no_contacts(self, db_session):
        """Re-verification with no stale contacts returns early."""
        from app.services.customer_enrichment_batch import run_email_reverification

        result = await run_email_reverification(db_session, max_contacts=10)
        assert result["status"] == "no_contacts_to_reverify"

    @pytest.mark.asyncio
    async def test_email_reverification_credit_exhausted(self, db_session):
        """Re-verification stops when credits exhausted."""
        from app.services.customer_enrichment_batch import run_email_reverification

        co = _make_company(db_session, "Verify Corp")
        site = _make_site(db_session, co.id, "Verify Site")
        contact = SiteContact(
            customer_site_id=site.id,
            full_name="Old Contact",
            email="old@verify.com",
            email_verified=True,
            email_verified_at=datetime.now(timezone.utc) - timedelta(days=120),
        )
        db_session.add(contact)
        db_session.commit()

        with patch(
            "app.services.customer_enrichment_batch.can_use_credits",
            return_value=False,
        ):
            result = await run_email_reverification(db_session, max_contacts=10)

        assert result["processed"] == 0


# ═══════════════════════════════════════════════════════════════════════
# 16. material_enrichment_service.py — lines 131-133, 137-141
# ═══════════════════════════════════════════════════════════════════════


class TestMaterialEnrichmentGaps:
    @pytest.mark.asyncio
    async def test_enrich_batch_ai_failure(self, db_session):
        """Exception during AI enrichment is caught per-card."""
        from app.models import MaterialCard
        from app.services.material_enrichment_service import _enrich_batch

        card = MaterialCard(
            normalized_mpn="fail-mpn",
            display_mpn="FAIL-MPN",
            search_count=1,
        )
        db_session.add(card)
        db_session.flush()

        stats = {"enriched": 0, "errors": 0}

        with (
            patch(
                "app.services.material_enrichment_service.gradient_json",
                new_callable=AsyncMock,
                side_effect=Exception("AI error"),
            ),
        ):
            await _enrich_batch([card], db_session, stats)

        assert stats["errors"] == 1

    @pytest.mark.asyncio
    async def test_enrich_batch_commit_failure(self, db_session):
        """Commit failure after batch enrichment is caught."""
        from app.models import MaterialCard
        from app.services.material_enrichment_service import _enrich_batch

        db_session.rollback()  # Clear any pending state from prior tests
        card = MaterialCard(
            normalized_mpn="commit-fail-mpn",
            display_mpn="COMMIT-FAIL",
            search_count=1,
        )
        db_session.add(card)
        db_session.flush()

        stats = {"enriched": 0, "errors": 0}

        with (
            patch(
                "app.utils.claude_client.claude_structured",
                new_callable=AsyncMock,
                return_value={"parts": [{"description": "Test component", "category": "ic"}]},
            ),
            patch.object(db_session, "commit", side_effect=Exception("commit fail")),
            patch.object(db_session, "rollback"),
        ):
            await _enrich_batch([card], db_session, stats)

        assert stats["errors"] >= 1
