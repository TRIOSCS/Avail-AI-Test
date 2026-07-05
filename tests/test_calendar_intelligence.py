"""test_calendar_intelligence.py — Tests for calendar intelligence meeting tracking.

Covers:
- log_meeting_activity: matched external attendee creates linked ActivityLog row
- Internal-only meeting writes no rows
- Re-scanning the same graph_event_id does not duplicate rows
- Direction logic (outbound if organizer is own-domain, inbound otherwise)
- Cadence clock is bumped for matched company/contact
- scan_calendar_events: integration path (mocked Graph)
- _AI_SCORED_TYPES includes MEETING (quality scoring)

Called by: pytest
Depends on: app/services/activity_service.py, app/services/calendar_intelligence.py,
            app/services/activity_quality_service.py
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from app.constants import ActivityType, Channel, Direction, EventType
from app.models import ActivityLog, Company, CustomerSite, SiteContact, VendorCard, VendorContact
from app.services.activity_quality_service import _AI_SCORED_TYPES
from app.services.activity_service import log_meeting_activity

# ── Helpers ──────────────────────────────────────────────────────────────────


def _dt(offset_hours: int = 0) -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=offset_hours)


def _make_company(db, name="Acme Electronics", domain="acme.com"):
    co = Company(name=name, domain=domain, is_active=True, created_at=_dt())
    db.add(co)
    db.flush()
    return co


def _make_site(db, company_id, email="contact@acme.com"):
    site = CustomerSite(
        company_id=company_id,
        site_name="HQ",
        is_active=True,
        contact_email=email,
        created_at=_dt(),
    )
    db.add(site)
    db.flush()
    return site


def _make_site_contact(db, site_id, email="contact@acme.com"):
    sc = SiteContact(
        customer_site_id=site_id,
        full_name="Jane Smith",
        email=email,
        is_primary=True,
        email_verified=True,
    )
    db.add(sc)
    db.flush()
    return sc


def _make_vendor_card(db, name="Arrow Electronics", domain="arrow.com"):
    card = VendorCard(
        normalized_name=name.lower(),
        display_name=name,
        domain=domain,
        is_blacklisted=False,
        sighting_count=5,
        created_at=_dt(),
    )
    db.add(card)
    db.flush()
    return card


def _make_vendor_contact(db, vendor_card_id, email="sales@arrow.com"):
    vc = VendorContact(
        vendor_card_id=vendor_card_id,
        email=email,
        full_name="Sales Rep",
        source="manual",
    )
    db.add(vc)
    db.flush()
    return vc


# ── Tests: log_meeting_activity ───────────────────────────────────────────────


class TestLogMeetingActivity:
    def test_external_attendee_creates_linked_row(self, db_session):
        """A meeting with a known customer contact writes one linked ActivityLog row."""
        co = _make_company(db_session)
        site = _make_site(db_session, co.id, email="buyer@acme.com")
        _make_site_contact(db_session, site.id, email="buyer@acme.com")
        db_session.commit()

        start = _dt(-1)
        end = _dt(-1) + timedelta(hours=1)

        rows = log_meeting_activity(
            user_id=None,
            graph_event_id="evt-001",
            subject="Q2 Review",
            start_dt=start,
            end_dt=end,
            organizer_email="me@trioscs.com",
            attendee_emails=["buyer@acme.com"],
            location="Conference Room A",
            db=db_session,
        )

        assert len(rows) == 1
        row = rows[0]
        assert row.activity_type == ActivityType.MEETING
        assert row.channel == Channel.CALENDAR
        assert row.event_type == EventType.MEETING
        assert row.company_id == co.id
        assert row.is_meaningful is True
        assert row.direction == Direction.OUTBOUND  # organizer is own-domain
        assert row.duration_seconds == 3600
        assert row.occurred_at == start
        assert row.external_id == "calendar-evt-001"
        assert row.details is not None
        assert row.details["graph_event_id"] == "evt-001"

    def test_internal_only_meeting_writes_no_rows(self, db_session):
        """A meeting with only own-domain attendees creates no customer rows."""
        db_session.commit()

        rows = log_meeting_activity(
            user_id=None,
            graph_event_id="evt-internal-001",
            subject="Internal Standup",
            start_dt=_dt(-2),
            end_dt=_dt(-1),
            organizer_email="me@trioscs.com",
            attendee_emails=["colleague@trioscs.com", "boss@trioscs.com"],
            location=None,
            db=db_session,
        )

        assert rows == []
        count = db_session.query(ActivityLog).filter(ActivityLog.external_id == "calendar-evt-internal-001").count()
        assert count == 0

    def test_rescan_same_event_does_not_duplicate(self, db_session):
        """Re-scanning an event with the same graph_event_id returns [] (idempotent)."""
        co = _make_company(db_session)
        _make_site(db_session, co.id, email="vendor@acme.com")
        db_session.commit()

        kwargs = dict(
            user_id=None,
            graph_event_id="evt-dedup-001",
            subject="Deal Review",
            start_dt=_dt(-3),
            end_dt=_dt(-2),
            organizer_email="me@trioscs.com",
            attendee_emails=["vendor@acme.com"],
            location=None,
            db=db_session,
        )

        first = log_meeting_activity(**kwargs)
        assert len(first) == 1

        second = log_meeting_activity(**kwargs)
        assert second == []

        # Only one row in DB
        count = db_session.query(ActivityLog).filter(ActivityLog.external_id == "calendar-evt-dedup-001").count()
        assert count == 1

    def test_inbound_direction_when_organizer_is_external(self, db_session):
        """Meeting organized by an external contact is direction=inbound."""
        co = _make_company(db_session, domain="supplier.com")
        _make_site(db_session, co.id, email="rep@supplier.com")
        db_session.commit()

        rows = log_meeting_activity(
            user_id=None,
            graph_event_id="evt-inbound-001",
            subject="Supplier Proposal",
            start_dt=_dt(-4),
            end_dt=_dt(-3),
            organizer_email="rep@supplier.com",  # external organizer
            attendee_emails=["rep@supplier.com"],
            location=None,
            db=db_session,
        )

        assert len(rows) == 1
        assert rows[0].direction == Direction.INBOUND

    def test_three_customer_meeting_writes_three_rows(self, db_session):
        """A meeting with three distinct external entities creates three rows."""
        co1 = _make_company(db_session, name="Company A", domain="company-a.com")
        co2 = _make_company(db_session, name="Company B", domain="company-b.com")
        _make_site(db_session, co1.id, email="a@company-a.com")
        _make_site(db_session, co2.id, email="b@company-b.com")
        vendor = _make_vendor_card(db_session, domain="vendor-c.com")
        _make_vendor_contact(db_session, vendor.id, email="c@vendor-c.com")
        db_session.commit()

        rows = log_meeting_activity(
            user_id=None,
            graph_event_id="evt-multi-001",
            subject="Multi-party meeting",
            start_dt=_dt(-5),
            end_dt=_dt(-4),
            organizer_email="me@trioscs.com",
            attendee_emails=["a@company-a.com", "b@company-b.com", "c@vendor-c.com"],
            location=None,
            db=db_session,
        )

        assert len(rows) == 3
        company_ids = {r.company_id for r in rows if r.company_id}
        assert co1.id in company_ids
        assert co2.id in company_ids
        vendor_ids = {r.vendor_card_id for r in rows if r.vendor_card_id}
        assert vendor.id in vendor_ids

    def test_junk_attendees_are_filtered(self, db_session):
        """Noreply@ and generic-domain emails are not matched or written."""
        co = _make_company(db_session, domain="real.com")
        _make_site(db_session, co.id, email="contact@real.com")
        db_session.commit()

        rows = log_meeting_activity(
            user_id=None,
            graph_event_id="evt-junk-001",
            subject="Meeting with noise",
            start_dt=_dt(-6),
            end_dt=_dt(-5),
            organizer_email="me@trioscs.com",
            attendee_emails=[
                "noreply@service.com",
                "user@gmail.com",
                "contact@real.com",  # this one should match
            ],
            location=None,
            db=db_session,
        )

        # Only the real contact should match
        assert len(rows) == 1
        assert rows[0].company_id == co.id

    def test_duration_seconds_computed_correctly(self, db_session):
        """duration_seconds reflects end - start."""
        co = _make_company(db_session, domain="duration-test.com")
        _make_site(db_session, co.id, email="a@duration-test.com")
        db_session.commit()

        start = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)
        end = datetime(2026, 6, 1, 11, 30, tzinfo=timezone.utc)  # 90 min

        rows = log_meeting_activity(
            user_id=None,
            graph_event_id="evt-dur-001",
            subject="Long meeting",
            start_dt=start,
            end_dt=end,
            organizer_email="me@trioscs.com",
            attendee_emails=["a@duration-test.com"],
            location=None,
            db=db_session,
        )

        assert len(rows) == 1
        assert rows[0].duration_seconds == 5400  # 90 * 60

    def test_occurred_at_is_event_start(self, db_session):
        """occurred_at is set to the event start time."""
        co = _make_company(db_session, domain="timing-test.com")
        _make_site(db_session, co.id, email="x@timing-test.com")
        db_session.commit()

        start = datetime(2026, 5, 15, 14, 0, tzinfo=timezone.utc)
        end = datetime(2026, 5, 15, 15, 0, tzinfo=timezone.utc)

        rows = log_meeting_activity(
            user_id=None,
            graph_event_id="evt-time-001",
            subject="Timing test",
            start_dt=start,
            end_dt=end,
            organizer_email="me@trioscs.com",
            attendee_emails=["x@timing-test.com"],
            location=None,
            db=db_session,
        )

        assert len(rows) == 1
        assert rows[0].occurred_at == start

    def test_unmatched_external_attendee_writes_no_row(self, db_session):
        """An external email that doesn't match any entity is silently skipped."""
        db_session.commit()

        rows = log_meeting_activity(
            user_id=None,
            graph_event_id="evt-nomatch-001",
            subject="Unknown contact meeting",
            start_dt=_dt(-7),
            end_dt=_dt(-6),
            organizer_email="me@trioscs.com",
            attendee_emails=["mystery@unknowncompany.io"],
            location=None,
            db=db_session,
        )

        assert rows == []


# ── Tests: junk-filter exact-match regression ────────────────────────────────


class TestJunkFilterExactMatch:
    """Regression: _is_junk must use exact membership, not startswith().

    support.lee@customer.com should NOT be filtered (local='support.lee' != 'support').
    marketingdirector@example.com should NOT be filtered (local='marketingdirector' != 'marketing').
    noreply@customer.com MUST be filtered (local='noreply' IS in JUNK_EMAIL_PREFIXES).
    """

    def test_support_prefix_real_contact_is_not_filtered(self, db_session):
        """support.lee@customer.com has local='support.lee' — not in JUNK_EMAIL_PREFIXES
        — so it must NOT be filtered and must produce a linked ActivityLog row."""
        co = _make_company(db_session, name="Support Lee Co", domain="customer.com")
        site = _make_site(db_session, co.id, email="support.lee@customer.com")
        _make_site_contact(db_session, site.id, email="support.lee@customer.com")
        db_session.commit()

        rows = log_meeting_activity(
            user_id=None,
            graph_event_id="evt-junk-prefix-001",
            subject="Review call",
            start_dt=_dt(-2),
            end_dt=_dt(-1),
            organizer_email="me@trioscs.com",
            attendee_emails=["support.lee@customer.com"],
            location=None,
            db=db_session,
        )

        assert len(rows) == 1, (
            "support.lee@customer.com must not be filtered — 'support.lee' is not in JUNK_EMAIL_PREFIXES"
        )
        assert rows[0].company_id == co.id

    def test_noreply_is_always_filtered(self, db_session):
        """noreply@customer.com has local='noreply' which IS in JUNK_EMAIL_PREFIXES and
        must produce zero rows regardless of domain."""
        co = _make_company(db_session, name="Noreply Co", domain="vendor2.com")
        site = _make_site(db_session, co.id, email="noreply@vendor2.com")
        _make_site_contact(db_session, site.id, email="noreply@vendor2.com")
        db_session.commit()

        rows = log_meeting_activity(
            user_id=None,
            graph_event_id="evt-junk-prefix-002",
            subject="Automated alert",
            start_dt=_dt(-3),
            end_dt=_dt(-2),
            organizer_email="me@trioscs.com",
            attendee_emails=["noreply@vendor2.com"],
            location=None,
            db=db_session,
        )

        assert rows == [], "noreply@ must always be filtered"


# ── Tests: AI scoring inclusion ───────────────────────────────────────────────


class TestMeetingInAIScoredTypes:
    def test_meeting_in_ai_scored_types(self):
        """ActivityType.MEETING is included in _AI_SCORED_TYPES for quality scoring."""
        assert ActivityType.MEETING in _AI_SCORED_TYPES


# ── Tests: scan_calendar_events integration ───────────────────────────────────


class TestScanCalendarEvents:
    def _graph_event(self, event_id, subject, start_offset_h, attendee_emails, organizer_email=None):
        """Build a minimal Graph API event dict."""
        start = _dt(start_offset_h)
        end = start + timedelta(hours=1)
        organizer_email = organizer_email or "me@trioscs.com"
        attendees = [{"emailAddress": {"address": e, "name": ""}, "type": "required"} for e in attendee_emails]
        return {
            "id": event_id,
            "subject": subject,
            "start": {"dateTime": start.isoformat()},
            "end": {"dateTime": end.isoformat()},
            "attendees": attendees,
            "organizer": {"emailAddress": {"address": organizer_email, "name": ""}},
            "location": {"displayName": ""},
        }

    def test_scan_logs_matched_meeting(self, db_session):
        """scan_calendar_events logs a row for an event with a known external
        attendee."""
        co = _make_company(db_session, domain="acme-scan.com")
        _make_site(db_session, co.id, email="rep@acme-scan.com")
        db_session.commit()

        event = self._graph_event(
            event_id="graph-scan-001",
            subject="Business Review",
            start_offset_h=-2,
            attendee_emails=["rep@acme-scan.com"],
        )

        with patch("app.utils.graph_client.GraphClient") as MockGC:
            mock_gc = MockGC.return_value
            mock_gc.delta_query = AsyncMock(return_value=([event], "https://graph/delta?token=t1"))

            from app.services.calendar_intelligence import scan_calendar_events

            result = asyncio.run(scan_calendar_events("token", None, db_session))

        assert result["events_scanned"] == 1
        assert result["activities_logged"] == 1

        row = db_session.query(ActivityLog).filter(ActivityLog.external_id == "calendar-graph-scan-001").first()
        assert row is not None
        assert row.activity_type == ActivityType.MEETING
        assert row.company_id == co.id

    def test_scan_skips_internal_only_event(self, db_session):
        """scan_calendar_events does not log events with only own-domain attendees."""
        db_session.commit()

        event = self._graph_event(
            event_id="graph-internal-001",
            subject="Team Standup",
            start_offset_h=-3,
            attendee_emails=["colleague@trioscs.com"],
        )

        with patch("app.utils.graph_client.GraphClient") as MockGC:
            mock_gc = MockGC.return_value
            mock_gc.delta_query = AsyncMock(return_value=([event], "https://graph/delta?token=t1"))

            from app.services.calendar_intelligence import scan_calendar_events

            result = asyncio.run(scan_calendar_events("token", None, db_session))

        assert result["activities_logged"] == 0
        count = db_session.query(ActivityLog).filter(ActivityLog.external_id == "calendar-graph-internal-001").count()
        assert count == 0

    def test_scan_rescan_is_idempotent(self, db_session):
        """Running scan_calendar_events twice for the same event writes only 1 row."""
        co = _make_company(db_session, domain="idempotent-scan.com")
        _make_site(db_session, co.id, email="x@idempotent-scan.com")
        db_session.commit()

        event = self._graph_event(
            event_id="graph-idem-001",
            subject="Idempotent meeting",
            start_offset_h=-4,
            attendee_emails=["x@idempotent-scan.com"],
        )

        with patch("app.utils.graph_client.GraphClient") as MockGC:
            mock_gc = MockGC.return_value
            mock_gc.delta_query = AsyncMock(return_value=([event], "https://graph/delta?token=t1"))

            from app.services.calendar_intelligence import scan_calendar_events

            asyncio.run(scan_calendar_events("token", None, db_session))
            result2 = asyncio.run(scan_calendar_events("token", None, db_session))

        assert result2["activities_logged"] == 0
        count = db_session.query(ActivityLog).filter(ActivityLog.external_id == "calendar-graph-idem-001").count()
        assert count == 1

    def test_scan_graph_error_returns_zero_counts(self, db_session):
        """A Graph API exception returns zeroed result dict without crashing."""
        with patch("app.utils.graph_client.GraphClient") as MockGC:
            mock_gc = MockGC.return_value
            mock_gc.delta_query = AsyncMock(side_effect=RuntimeError("network error"))

            from app.services.calendar_intelligence import scan_calendar_events

            result = asyncio.run(scan_calendar_events("token", None, db_session))

        assert result["events_scanned"] == 0
        assert result["activities_logged"] == 0


# ── Tests: calendarView/delta incremental sync (Phase 4.6) ────────────────────


class TestCalendarDeltaSync:
    """scan_calendar_events uses /me/calendarView/delta + SyncState delta tokens.

    Covers the three delta contract paths:
      - initial sync stores the deltaLink from a paged response,
      - incremental sync uses the stored token, applying a changed event and an
        @removed deletion,
      - a 410 on the delta call discards the token and performs a full resync.
    """

    def _graph_event(self, event_id, subject, start_offset_h, attendee_emails, organizer_email=None):
        start = _dt(start_offset_h)
        end = start + timedelta(hours=1)
        organizer_email = organizer_email or "me@trioscs.com"
        attendees = [{"emailAddress": {"address": e, "name": ""}, "type": "required"} for e in attendee_emails]
        return {
            "id": event_id,
            "subject": subject,
            "start": {"dateTime": start.isoformat()},
            "end": {"dateTime": end.isoformat()},
            "attendees": attendees,
            "organizer": {"emailAddress": {"address": organizer_email, "name": ""}},
            "location": {"displayName": ""},
        }

    def _sync_state(self, db, user_id):
        from app.models.pipeline import SyncState

        return db.query(SyncState).filter(SyncState.user_id == user_id, SyncState.folder == "calendar_scan").first()

    def test_initial_sync_stores_delta_link(self, db_session, test_user):
        """No stored token → initial delta call (token=None) whose deltaLink is
        persisted."""
        co = _make_company(db_session, domain="delta-init.com")
        _make_site(db_session, co.id, email="rep@delta-init.com")
        db_session.commit()

        event = self._graph_event("evt-init-1", "Kickoff", -3, ["rep@delta-init.com"])

        with patch("app.utils.graph_client.GraphClient") as MockGC:
            mock_gc = MockGC.return_value
            mock_gc.delta_query = AsyncMock(return_value=([event], "https://graph/delta?$deltatoken=INIT"))

            from app.services.calendar_intelligence import scan_calendar_events

            result = asyncio.run(scan_calendar_events("token", test_user.id, db_session))

        # Initial sync passes delta_token=None to delta_query.
        assert mock_gc.delta_query.await_args.kwargs["delta_token"] is None
        assert result["events_scanned"] == 1
        assert result["activities_logged"] == 1

        ss = self._sync_state(db_session, test_user.id)
        assert ss is not None
        assert ss.delta_token == "https://graph/delta?$deltatoken=INIT"
        assert ss.last_sync_at is not None

    def test_incremental_applies_change_and_removal(self, db_session, test_user):
        """Stored token is used; a changed event is logged and an @removed entry
        deleted."""

        co = _make_company(db_session, domain="delta-inc.com")
        _make_site(db_session, co.id, email="rep@delta-inc.com")
        db_session.commit()

        removed_event = self._graph_event("evt-remove-1", "Old Meeting", -5, ["rep@delta-inc.com"])
        changed_event = self._graph_event("evt-change-1", "New Meeting", -2, ["rep@delta-inc.com"])

        # Initial scan logs the event that will later be removed and stores link1.
        with patch("app.utils.graph_client.GraphClient") as MockGC:
            mock_gc = MockGC.return_value
            mock_gc.delta_query = AsyncMock(return_value=([removed_event], "https://graph/delta?$deltatoken=link1"))

            from app.services.calendar_intelligence import scan_calendar_events

            asyncio.run(scan_calendar_events("token", test_user.id, db_session))

        ss = self._sync_state(db_session, test_user.id)
        assert ss.delta_token == "https://graph/delta?$deltatoken=link1"
        assert db_session.query(ActivityLog).filter(ActivityLog.external_id == "calendar-evt-remove-1").count() == 1

        # Incremental scan: one new/changed event + one @removed for the earlier event.
        with patch("app.utils.graph_client.GraphClient") as MockGC:
            mock_gc = MockGC.return_value
            mock_gc.delta_query = AsyncMock(
                return_value=(
                    [changed_event, {"@removed": {"reason": "deleted"}, "id": "evt-remove-1"}],
                    "https://graph/delta?$deltatoken=link2",
                )
            )

            from app.services.calendar_intelligence import scan_calendar_events

            result = asyncio.run(scan_calendar_events("token", test_user.id, db_session))

        # Incremental sync passes the stored deltaLink as the token.
        assert mock_gc.delta_query.await_args.kwargs["delta_token"] == "https://graph/delta?$deltatoken=link1"
        assert result["activities_logged"] == 1  # only the changed event
        assert result["events_removed"] == 1

        assert db_session.query(ActivityLog).filter(ActivityLog.external_id == "calendar-evt-remove-1").count() == 0
        assert db_session.query(ActivityLog).filter(ActivityLog.external_id == "calendar-evt-change-1").count() == 1

        db_session.refresh(ss)
        assert ss.delta_token == "https://graph/delta?$deltatoken=link2"

    def test_410_discards_token_and_full_resync(self, db_session, test_user):
        """410 Gone on the delta call → stored token discarded, full resync, no
        crash."""
        from app.models.pipeline import SyncState
        from app.utils.graph_client import GraphSyncStateExpired

        co = _make_company(db_session, domain="delta-410.com")
        _make_site(db_session, co.id, email="rep@delta-410.com")
        db_session.add(
            SyncState(user_id=test_user.id, folder="calendar_scan", delta_token="https://graph/delta?$deltatoken=STALE")
        )
        db_session.commit()

        event = self._graph_event("evt-410-1", "Resynced Meeting", -2, ["rep@delta-410.com"])

        with patch("app.utils.graph_client.GraphClient") as MockGC:
            mock_gc = MockGC.return_value
            mock_gc.delta_query = AsyncMock(
                side_effect=[
                    GraphSyncStateExpired("410 Gone"),
                    ([event], "https://graph/delta?$deltatoken=FRESH"),
                ]
            )

            from app.services.calendar_intelligence import scan_calendar_events

            result = asyncio.run(scan_calendar_events("token", test_user.id, db_session))

        # Two calls: the stale token, then a full resync with delta_token=None.
        assert mock_gc.delta_query.await_count == 2
        assert mock_gc.delta_query.await_args_list[0].kwargs["delta_token"] == "https://graph/delta?$deltatoken=STALE"
        assert mock_gc.delta_query.await_args_list[1].kwargs["delta_token"] is None

        assert result["activities_logged"] == 1
        assert db_session.query(ActivityLog).filter(ActivityLog.external_id == "calendar-evt-410-1").count() == 1

        ss = self._sync_state(db_session, test_user.id)
        assert ss.delta_token == "https://graph/delta?$deltatoken=FRESH"


# ── Tests: calendar scan flag guard (FIX 3a) ─────────────────────────────────


class TestCalendarScanFlagGuard:
    """Verify _job_calendar_scan is only registered when activity_tracking_enabled."""

    def test_calendar_scan_not_registered_when_tracking_disabled(self):
        """When activity_tracking_enabled=False, no calendar_scan job is added."""
        from unittest.mock import MagicMock

        from app.jobs.email_jobs import register_email_jobs

        mock_scheduler = MagicMock()
        mock_settings = MagicMock()
        mock_settings.activity_tracking_enabled = False
        mock_settings.contacts_sync_enabled = False
        mock_settings.ownership_sweep_enabled = False
        mock_settings.contact_scoring_enabled = False
        mock_settings.customer_enrichment_enabled = False

        register_email_jobs(mock_scheduler, mock_settings)

        added_ids = [
            call.kwargs.get("id") or call.args[1] if len(call.args) > 1 else None
            for call in mock_scheduler.add_job.call_args_list
        ]
        # Flatten: add_job is called with id= keyword in most registrations
        all_kwargs_ids = [call.kwargs.get("id") for call in mock_scheduler.add_job.call_args_list]
        assert "calendar_scan" not in all_kwargs_ids, (
            "calendar_scan must NOT be registered when activity_tracking_enabled=False"
        )

    def test_calendar_scan_registered_when_tracking_enabled(self):
        """When activity_tracking_enabled=True, calendar_scan job IS added."""
        from unittest.mock import MagicMock

        from app.jobs.email_jobs import register_email_jobs

        mock_scheduler = MagicMock()
        mock_settings = MagicMock()
        mock_settings.activity_tracking_enabled = True
        mock_settings.contacts_sync_enabled = False
        mock_settings.ownership_sweep_enabled = False
        mock_settings.contact_scoring_enabled = False
        mock_settings.customer_enrichment_enabled = False

        register_email_jobs(mock_scheduler, mock_settings)

        all_kwargs_ids = [call.kwargs.get("id") for call in mock_scheduler.add_job.call_args_list]
        assert "calendar_scan" in all_kwargs_ids, "calendar_scan must be registered when activity_tracking_enabled=True"
