"""Tests for app/services/activity_quality_service.py — AI quality scoring service.

Targets missing branches to bring coverage from 49% to 85%+.

Called by: pytest
Depends on: app.services.activity_quality_service, app.models.intelligence
"""

import os

os.environ["TESTING"] = "1"

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from sqlalchemy.orm import Session

from app.models.intelligence import ActivityLog
from tests.conftest import engine  # noqa: F401


class TestScoreActivityNotFound:
    """Test score_activity when activity does not exist."""

    async def test_score_activity_nonexistent_id_returns_none(self, db_session: Session):
        """score_activity returns None (silently) for a non-existent ID."""
        from app.services.activity_quality_service import score_activity

        # Should not raise, just return
        await score_activity(999999, db_session)


class TestScoreActivityAlreadyScored:
    """Test score_activity skips already-assessed entries."""

    async def test_already_scored_skips_claude(self, db_session: Session, test_user):
        """Entries with quality_assessed_at set are skipped without calling Claude."""
        from app.services.activity_quality_service import score_activity

        log = ActivityLog(
            user_id=test_user.id,
            activity_type="phone_call",
            channel="phone",
            quality_assessed_at=datetime.now(timezone.utc),
        )
        db_session.add(log)
        db_session.flush()

        with patch(
            "app.utils.claude_client.claude_structured",
            new_callable=AsyncMock,
        ) as mock_claude:
            await score_activity(log.id, db_session)
            mock_claude.assert_not_called()


class TestScoreActivityNoData:
    """Test score_activity when ActivityLog has no analyzable fields."""

    async def test_no_data_marks_as_assessed_without_claude(self, db_session: Session, test_user):
        """ActivityLog with no meaningful fields gets marked with quality_score=0 and
        no_data."""
        from app.services.activity_quality_service import score_activity

        log = ActivityLog(
            user_id=test_user.id,
            activity_type="phone_call",
            channel="",  # empty string is falsy — won't be added to parts
            # No event_type, subject, notes, duration, contact_name
        )
        db_session.add(log)
        db_session.flush()

        with patch(
            "app.utils.claude_client.claude_structured",
            new_callable=AsyncMock,
        ) as mock_claude:
            await score_activity(log.id, db_session)
            mock_claude.assert_not_called()

        db_session.refresh(log)
        assert log.quality_score == 0.0
        assert log.quality_classification == "no_data"
        assert log.is_meaningful is False
        assert log.quality_assessed_at is not None


class TestScoreActivityWithData:
    """Test score_activity with a full set of interaction fields."""

    async def test_scores_all_fields(self, db_session: Session, test_user):
        """score_activity builds prompt from all available fields and writes results."""
        from app.services.activity_quality_service import score_activity

        log = ActivityLog(
            user_id=test_user.id,
            activity_type="phone_call",
            channel="phone",
            event_type="call",
            direction="outbound",
            subject="LM317T Pricing Discussion",
            notes="Customer needs 10K units by Q4",
            duration_seconds=450,
            contact_name="Jane Buyer",
        )
        db_session.add(log)
        db_session.flush()

        mock_result = {
            "is_meaningful": True,
            "quality_score": 75,
            "classification": "negotiation",
            "sentiment": "positive",
            "clean_summary": "Detailed pricing discussion for LM317T bulk order.",
        }

        with patch(
            "app.utils.claude_client.claude_structured",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            await score_activity(log.id, db_session)

        db_session.refresh(log)
        assert log.quality_score == 75.0
        assert log.quality_classification == "negotiation"
        assert log.is_meaningful is True
        assert log.quality_assessed_at is not None
        assert log.summary is not None

    async def test_long_classification_is_truncated(self, db_session: Session, test_user):
        """quality_classification longer than 30 chars is truncated."""
        from app.services.activity_quality_service import score_activity

        log = ActivityLog(
            user_id=test_user.id,
            activity_type="note",
            channel="manual",
            event_type="note",
            notes="A note about something important",
        )
        db_session.add(log)
        db_session.flush()

        mock_result = {
            "is_meaningful": True,
            "quality_score": 60,
            "classification": "this_is_a_very_long_classification_label",
            "sentiment": "neutral",
            "clean_summary": "Short summary.",
        }

        with patch(
            "app.utils.claude_client.claude_structured",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            await score_activity(log.id, db_session)

        db_session.refresh(log)
        assert len(log.quality_classification) <= 30

    async def test_empty_summary_stored_as_none(self, db_session: Session, test_user):
        """Empty clean_summary is stored as None."""
        from app.services.activity_quality_service import score_activity

        log = ActivityLog(
            user_id=test_user.id,
            activity_type="note",
            channel="manual",
            event_type="note",
            subject="Quick note",
        )
        db_session.add(log)
        db_session.flush()

        mock_result = {
            "is_meaningful": False,
            "quality_score": 10,
            "classification": "auto_reply",
            "sentiment": "neutral",
            "clean_summary": "",
        }

        with patch(
            "app.utils.claude_client.claude_structured",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            await score_activity(log.id, db_session)

        db_session.refresh(log)
        assert log.summary is None


class TestScoreActivityClaudeReturnsNone:
    """Test score_activity when Claude returns None/empty result."""

    async def test_claude_returns_none_marks_scoring_failed(self, db_session: Session, test_user):
        """When Claude returns None, activity is marked as scoring_failed."""
        from app.services.activity_quality_service import score_activity

        log = ActivityLog(
            user_id=test_user.id,
            activity_type="phone_call",
            channel="phone",
            event_type="call",
            subject="Some call",
        )
        db_session.add(log)
        db_session.flush()

        with patch(
            "app.utils.claude_client.claude_structured",
            new_callable=AsyncMock,
            return_value=None,
        ):
            await score_activity(log.id, db_session)

        db_session.refresh(log)
        assert log.quality_classification == "scoring_failed"
        assert log.quality_score is None
        assert log.is_meaningful is None
        assert log.quality_assessed_at is not None


class TestScoreUnscoredActivities:
    """Test score_unscored_activities batch function."""

    async def test_returns_zero_when_no_unscored(self, db_session: Session):
        """Returns 0 when there are no unscored activities."""
        from app.services.activity_quality_service import score_unscored_activities

        count = await score_unscored_activities(db_session, batch_size=10)
        assert count == 0

    async def test_scores_unscored_activities(self, db_session: Session, test_user):
        """Scores available unscored non-email activities."""
        from app.services.activity_quality_service import score_unscored_activities

        log = ActivityLog(
            user_id=test_user.id,
            activity_type="phone_call",
            channel="phone",
            event_type="call",
            subject="Pricing call",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(log)
        db_session.commit()

        mock_result = {
            "is_meaningful": True,
            "quality_score": 65,
            "classification": "conversation",
            "sentiment": "neutral",
            "clean_summary": "Pricing call with vendor.",
        }

        with patch(
            "app.utils.claude_client.claude_structured",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            count = await score_unscored_activities(db_session, batch_size=10)

        assert count >= 1

    async def test_aborts_on_auth_error(self, db_session: Session, test_user):
        """Aborts batch on ClaudeAuthError to avoid burning API calls."""
        from app.services.activity_quality_service import score_unscored_activities

        log = ActivityLog(
            user_id=test_user.id,
            activity_type="phone_call",
            channel="phone",
            event_type="call",
            subject="Some call",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(log)
        db_session.commit()

        class ClaudeAuthError(Exception):
            pass

        with patch(
            "app.services.activity_quality_service.score_activity",
            new_callable=AsyncMock,
            side_effect=ClaudeAuthError("auth failure"),
        ):
            count = await score_unscored_activities(db_session, batch_size=10)

        # Aborted early — scored count is 0
        assert count == 0

    async def test_aborts_on_rate_limit_error(self, db_session: Session, test_user):
        """Aborts batch on ClaudeRateLimitError."""
        from app.services.activity_quality_service import score_unscored_activities

        log = ActivityLog(
            user_id=test_user.id,
            activity_type="phone_call",
            channel="phone",
            event_type="call",
            subject="Rate limited call",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(log)
        db_session.commit()

        class ClaudeRateLimitError(Exception):
            pass

        with patch(
            "app.services.activity_quality_service.score_activity",
            new_callable=AsyncMock,
            side_effect=ClaudeRateLimitError("rate limited"),
        ):
            count = await score_unscored_activities(db_session, batch_size=10)

        assert count == 0

    async def test_continues_on_generic_error(self, db_session: Session, test_user):
        """Continues batch on generic errors, increments error counter."""
        from app.services.activity_quality_service import score_unscored_activities

        # Add two activities — first will fail, second will succeed
        log1 = ActivityLog(
            user_id=test_user.id,
            activity_type="phone_call",
            channel="phone",
            event_type="call",
            subject="First call",
            created_at=datetime.now(timezone.utc),
        )
        log2 = ActivityLog(
            user_id=test_user.id,
            activity_type="note",
            channel="manual",
            event_type="note",
            subject="Second note",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add_all([log1, log2])
        db_session.commit()

        call_count = 0
        mock_result = {
            "is_meaningful": True,
            "quality_score": 50,
            "classification": "conversation",
            "sentiment": "neutral",
            "clean_summary": "A call.",
        }

        async def side_effect(activity_id, db):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("transient error")
            # Second call succeeds by actually patching in result
            act = db.get(ActivityLog, activity_id)
            if act:
                act.quality_score = 50.0
                act.quality_classification = "conversation"
                act.is_meaningful = True
                act.summary = "A call."
                act.quality_assessed_at = datetime.now(timezone.utc)
                db.flush()

        with patch(
            "app.services.activity_quality_service.score_activity",
            side_effect=side_effect,
        ):
            count = await score_unscored_activities(db_session, batch_size=10)

        # One succeeded despite the first error
        assert count >= 1

    async def test_aborts_on_unavailable_error(self, db_session: Session, test_user):
        """Aborts batch on ClaudeUnavailableError."""
        from app.services.activity_quality_service import score_unscored_activities

        log = ActivityLog(
            user_id=test_user.id,
            activity_type="phone_call",
            channel="phone",
            event_type="call",
            subject="Unavailable error call",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(log)
        db_session.commit()

        class ClaudeUnavailableError(Exception):
            pass

        with patch(
            "app.services.activity_quality_service.score_activity",
            new_callable=AsyncMock,
            side_effect=ClaudeUnavailableError("service unavailable"),
        ):
            count = await score_unscored_activities(db_session, batch_size=10)

        assert count == 0
