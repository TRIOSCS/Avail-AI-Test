"""test_p1e_call_teams_grading.py — TDD for P1e: duration-gated meaningful calls
+ AI-graded Teams for honest reply clock.

Tests are written FIRST (RED) then the implementation makes them GREEN.

Business rules being tested:
- A connected inbound call (duration_seconds >= 30) is meaningful → advances last_reply_at
- A voicemail/missed inbound call (duration_seconds < 30 or None) is NOT meaningful
- An outbound connected call advances last_outbound_at (existing behaviour, unchanged)
- TEAMS_MESSAGE is in _AI_SCORED_TYPES so score_unscored_activities picks it up

Called by: pytest
Depends on: app/services/activity_service.py, app/services/activity_quality_service.py,
            app/services/cadence_service.py, conftest.py
"""

from sqlalchemy.orm import Session

from app.constants import ActivityType
from app.models import Company, VendorCard
from app.services.activity_service import log_call_activity, log_company_call, log_vendor_call

# ═══════════════════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════════════════


def _make_company(db: Session) -> Company:
    c = Company(name="Acme Corp")
    db.add(c)
    db.flush()
    return c


def _make_vendor(db: Session) -> VendorCard:
    v = VendorCard(normalized_name="acme-vendor", display_name="Acme Vendor")
    db.add(v)
    db.flush()
    return v


# ═══════════════════════════════════════════════════════════════════════
#  CALL_MEANINGFUL_MIN_SECONDS constant
# ═══════════════════════════════════════════════════════════════════════


class TestCallMeaningfulThreshold:
    """The module must export the duration threshold constant."""

    def test_constant_is_defined(self):
        """CALL_MEANINGFUL_MIN_SECONDS must exist in activity_service."""
        from app.services.activity_service import CALL_MEANINGFUL_MIN_SECONDS

        assert isinstance(CALL_MEANINGFUL_MIN_SECONDS, int)
        assert CALL_MEANINGFUL_MIN_SECONDS > 0

    def test_constant_is_30_seconds(self):
        """Default threshold is 30 seconds."""
        from app.services.activity_service import CALL_MEANINGFUL_MIN_SECONDS

        assert CALL_MEANINGFUL_MIN_SECONDS == 30


# ═══════════════════════════════════════════════════════════════════════
#  log_call_activity — generic phone path (used by 8x8 CDR + Teams call sync)
# ═══════════════════════════════════════════════════════════════════════


class TestLogCallActivityMeaningfulness:
    """log_call_activity must set is_meaningful based on duration, not hardcode True."""

    def test_connected_inbound_call_is_meaningful(self, db_session: Session, test_user):
        """duration_seconds=120 inbound → is_meaningful=True."""
        record = log_call_activity(
            user_id=test_user.id,
            direction="inbound",
            phone="+15551234567",
            duration_seconds=120,
            external_id="test-ext-connected",
            contact_name="Alice",
            db=db_session,
        )
        assert record is not None
        assert record.is_meaningful is True

    def test_voicemail_inbound_call_is_not_meaningful(self, db_session: Session, test_user):
        """duration_seconds=0 inbound → is_meaningful=False (voicemail/missed)."""
        record = log_call_activity(
            user_id=test_user.id,
            direction="inbound",
            phone="+15551234568",
            duration_seconds=0,
            external_id="test-ext-voicemail",
            contact_name="Bob",
            db=db_session,
        )
        assert record is not None
        assert record.is_meaningful is False

    def test_none_duration_inbound_call_is_not_meaningful(self, db_session: Session, test_user):
        """duration_seconds=None inbound → is_meaningful=False (no duration data)."""
        record = log_call_activity(
            user_id=test_user.id,
            direction="inbound",
            phone="+15551234569",
            duration_seconds=None,
            external_id="test-ext-no-duration",
            contact_name="Carol",
            db=db_session,
        )
        assert record is not None
        assert record.is_meaningful is False

    def test_short_call_under_threshold_is_not_meaningful(self, db_session: Session, test_user):
        """duration_seconds=10 (below threshold) → is_meaningful=False."""
        record = log_call_activity(
            user_id=test_user.id,
            direction="inbound",
            phone="+15551234570",
            duration_seconds=10,
            external_id="test-ext-short",
            contact_name="Dan",
            db=db_session,
        )
        assert record is not None
        assert record.is_meaningful is False

    def test_call_exactly_at_threshold_is_meaningful(self, db_session: Session, test_user):
        """duration_seconds=30 (exactly at threshold) → is_meaningful=True."""
        record = log_call_activity(
            user_id=test_user.id,
            direction="inbound",
            phone="+15551234571",
            duration_seconds=30,
            external_id="test-ext-threshold",
            contact_name="Eve",
            db=db_session,
        )
        assert record is not None
        assert record.is_meaningful is True

    def test_outbound_connected_call_is_meaningful(self, db_session: Session, test_user):
        """Outbound calls with sufficient duration are also marked meaningful."""
        record = log_call_activity(
            user_id=test_user.id,
            direction="outbound",
            phone="+15551234572",
            duration_seconds=90,
            external_id="test-ext-outbound",
            contact_name="Frank",
            db=db_session,
        )
        assert record is not None
        assert record.is_meaningful is True

    def test_outbound_voicemail_is_not_meaningful(self, db_session: Session, test_user):
        """Outbound with duration=0 is also not meaningful."""
        record = log_call_activity(
            user_id=test_user.id,
            direction="outbound",
            phone="+15551234573",
            duration_seconds=0,
            external_id="test-ext-outbound-vm",
            contact_name="Grace",
            db=db_session,
        )
        assert record is not None
        assert record.is_meaningful is False


# ═══════════════════════════════════════════════════════════════════════
#  log_company_call — manual company call path
# ═══════════════════════════════════════════════════════════════════════


class TestLogCompanyCallMeaningfulness:
    """log_company_call must also apply the duration rule."""

    def test_connected_company_call_is_meaningful(self, db_session: Session, test_user):
        """Company call with duration >= 30 → is_meaningful=True."""
        company = _make_company(db_session)
        record = log_company_call(
            user_id=test_user.id,
            company_id=company.id,
            direction="inbound",
            phone="+15551234574",
            duration_seconds=60,
            contact_name="Heidi",
            notes="Good call",
            db=db_session,
        )
        assert record.is_meaningful is True

    def test_voicemail_company_call_is_not_meaningful(self, db_session: Session, test_user):
        """Company call with duration=0 → is_meaningful=False."""
        company = _make_company(db_session)
        record = log_company_call(
            user_id=test_user.id,
            company_id=company.id,
            direction="inbound",
            phone="+15551234575",
            duration_seconds=0,
            contact_name="Ivan",
            notes=None,
            db=db_session,
        )
        assert record.is_meaningful is False

    def test_none_duration_company_call_is_not_meaningful(self, db_session: Session, test_user):
        """Company call with no duration → is_meaningful=False."""
        company = _make_company(db_session)
        record = log_company_call(
            user_id=test_user.id,
            company_id=company.id,
            direction="inbound",
            phone=None,
            duration_seconds=None,
            contact_name=None,
            notes=None,
            db=db_session,
        )
        assert record.is_meaningful is False


# ═══════════════════════════════════════════════════════════════════════
#  log_vendor_call — manual vendor call path
# ═══════════════════════════════════════════════════════════════════════


class TestLogVendorCallMeaningfulness:
    """log_vendor_call must also apply the duration rule."""

    def test_connected_vendor_call_is_meaningful(self, db_session: Session, test_user):
        """Vendor call with duration >= 30 → is_meaningful=True."""
        vendor = _make_vendor(db_session)
        record = log_vendor_call(
            user_id=test_user.id,
            vendor_card_id=vendor.id,
            vendor_contact_id=None,
            direction="inbound",
            phone="+15551234576",
            duration_seconds=45,
            contact_name="Judy",
            notes=None,
            db=db_session,
        )
        assert record.is_meaningful is True

    def test_voicemail_vendor_call_is_not_meaningful(self, db_session: Session, test_user):
        """Vendor call with duration=0 → is_meaningful=False."""
        vendor = _make_vendor(db_session)
        record = log_vendor_call(
            user_id=test_user.id,
            vendor_card_id=vendor.id,
            vendor_contact_id=None,
            direction="inbound",
            phone="+15551234577",
            duration_seconds=0,
            contact_name="Karl",
            notes=None,
            db=db_session,
        )
        assert record.is_meaningful is False


# ═══════════════════════════════════════════════════════════════════════
#  Reply clock advancement
# ═══════════════════════════════════════════════════════════════════════


class TestReplyClockAdvancement:
    """Verify bump_clocks_from_activity correctly gates on is_meaningful."""

    def test_meaningful_inbound_call_advances_last_reply_at(self, db_session: Session, test_user):
        """Connected inbound call (duration >= 30) → last_reply_at updated on
        company."""
        company = _make_company(db_session)
        assert company.last_reply_at is None

        log_company_call(
            user_id=test_user.id,
            company_id=company.id,
            direction="inbound",
            phone="+15551234578",
            duration_seconds=120,
            contact_name="Lara",
            notes=None,
            db=db_session,
        )
        db_session.flush()
        db_session.refresh(company)

        assert company.last_reply_at is not None

    def test_voicemail_inbound_call_does_not_advance_last_reply_at(self, db_session: Session, test_user):
        """Voicemail/missed inbound call (duration=0) → last_reply_at NOT updated."""
        company = _make_company(db_session)
        assert company.last_reply_at is None

        log_company_call(
            user_id=test_user.id,
            company_id=company.id,
            direction="inbound",
            phone="+15551234579",
            duration_seconds=0,
            contact_name="Mike",
            notes=None,
            db=db_session,
        )
        db_session.flush()
        db_session.refresh(company)

        assert company.last_reply_at is None

    def test_outbound_connected_call_advances_last_outbound_at(self, db_session: Session, test_user):
        """Connected outbound call → last_outbound_at updated on company (unchanged
        behaviour)."""
        company = _make_company(db_session)
        assert company.last_outbound_at is None

        log_company_call(
            user_id=test_user.id,
            company_id=company.id,
            direction="outbound",
            phone="+15551234580",
            duration_seconds=90,
            contact_name="Nina",
            notes=None,
            db=db_session,
        )
        db_session.flush()
        db_session.refresh(company)

        assert company.last_outbound_at is not None

    def test_outbound_call_does_not_advance_last_reply_at(self, db_session: Session, test_user):
        """Outbound call (even connected) must NOT advance last_reply_at."""
        company = _make_company(db_session)
        assert company.last_reply_at is None

        log_company_call(
            user_id=test_user.id,
            company_id=company.id,
            direction="outbound",
            phone="+15551234581",
            duration_seconds=90,
            contact_name="Otto",
            notes=None,
            db=db_session,
        )
        db_session.flush()
        db_session.refresh(company)

        assert company.last_reply_at is None


# ═══════════════════════════════════════════════════════════════════════
#  TEAMS_MESSAGE in _AI_SCORED_TYPES
# ═══════════════════════════════════════════════════════════════════════


class TestTeamsMessageAIScored:
    """TEAMS_MESSAGE must appear in _AI_SCORED_TYPES so it gets picked up by the quality
    scoring batch job — and thus feed the reply clock via the score_activity →
    bump_clocks path when graded meaningful."""

    def test_teams_message_in_ai_scored_types(self):
        """ActivityType.TEAMS_MESSAGE must be in _AI_SCORED_TYPES."""
        from app.constants import ActivityType
        from app.services.activity_quality_service import _AI_SCORED_TYPES

        assert ActivityType.TEAMS_MESSAGE in _AI_SCORED_TYPES

    def test_ai_scored_types_still_contains_email_received(self):
        """Adding TEAMS_MESSAGE must not drop EMAIL_RECEIVED from _AI_SCORED_TYPES."""
        from app.constants import ActivityType
        from app.services.activity_quality_service import _AI_SCORED_TYPES

        assert ActivityType.EMAIL_RECEIVED in _AI_SCORED_TYPES

    def test_ai_scored_types_still_contains_sighting_added(self):
        """Adding TEAMS_MESSAGE must not drop SIGHTING_ADDED from _AI_SCORED_TYPES."""
        from app.constants import ActivityType
        from app.services.activity_quality_service import _AI_SCORED_TYPES

        assert ActivityType.SIGHTING_ADDED in _AI_SCORED_TYPES

    def test_score_unscored_activities_queries_teams_messages(self, db_session: Session, test_user):
        """score_unscored_activities() selects TEAMS_MESSAGE rows with no
        quality_assessed_at."""
        from unittest.mock import AsyncMock, patch

        from app.models.intelligence import ActivityLog
        from app.services.activity_quality_service import score_unscored_activities

        teams_log = ActivityLog(
            user_id=test_user.id,
            activity_type=ActivityType.TEAMS_MESSAGE,
            channel="teams",
            direction="inbound",
            subject="Hi, do you have 50 units of MPN X?",
            quality_assessed_at=None,
        )
        db_session.add(teams_log)
        db_session.flush()

        # score_activity is patched so no real AI call happens
        with patch(
            "app.services.activity_quality_service.score_activity",
            new_callable=AsyncMock,
        ) as mock_score:
            import asyncio

            asyncio.get_event_loop().run_until_complete(score_unscored_activities(db_session))

        # The TEAMS_MESSAGE row should have triggered a score_activity call
        assert mock_score.call_count >= 1
        called_ids = [call.args[0] for call in mock_score.call_args_list]
        assert teams_log.id in called_ids
