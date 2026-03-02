"""
test_email_intelligence_phase6.py — Email Intelligence & Mining Fixes

Tests for:
- Fix 1: email_mining stores ALL classified emails (no regex gate)
- Fix 2: needs_review logic skips spam/ooo/general
- Fix 3: _job_email_health_update scheduler job
- Fix 4: _job_calendar_scan scheduler job

Called by: pytest
Depends on: app.connectors.email_mining, app.services.email_intelligence_service,
            app.scheduler
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.email_intelligence_service import store_email_intelligence

# ── Fix 1: EmailMiner stores all classified emails ─────────────────────


# Helper: a single fake message dict matching Graph API shape
def _fake_msg(msg_id="msg-1", body="Test body", subject="Test subject"):
    return {
        "id": msg_id,
        "from": {"emailAddress": {"name": "Vendor A", "address": "sales@vendor.com"}},
        "subject": subject,
        "body": {"content": body},
        "receivedDateTime": "2026-02-28T10:00:00Z",
        "conversationId": "conv-1",
    }


class TestScanInboxStoresRegexEmails:
    """Verify that scan_inbox calls process_email_intelligence for ALL emails,
    including those with 2+ regex matches (previously gated out)."""

    def _make_miner(self, db, user_id):
        """Create an EmailMiner with mocked GraphClient."""
        from app.connectors.email_mining import EmailMiner

        miner = EmailMiner.__new__(EmailMiner)
        miner.gc = MagicMock()
        miner.db = db
        miner.user_id = user_id
        return miner

    @pytest.mark.asyncio
    async def test_regex_offer_stored_in_email_intelligence(self):
        """2+ regex matches → process_email_intelligence still called."""
        mock_process = AsyncMock(return_value={"id": 1, "classification": "offer"})
        mock_db = MagicMock()

        from app.connectors.email_mining import EmailMiner

        # Body with 3 offer patterns: "in stock", "unit price", "lead time"
        msg = _fake_msg(body="We have LM358 in stock, unit price $1.50, lead time 2 weeks")

        with (
            patch.object(EmailMiner, "_already_processed", return_value=set()),
            patch.object(EmailMiner, "_mark_processed"),
            patch.object(EmailMiner, "_get_delta_token", return_value=None),
            patch.object(
                EmailMiner,
                "_search_messages",
                new_callable=AsyncMock,
                return_value=[msg],
            ),
            patch(
                "app.services.email_intelligence_service.process_email_intelligence",
                mock_process,
            ),
        ):
            miner = self._make_miner(mock_db, user_id=1)
            await miner.scan_inbox(lookback_days=7, max_messages=10, use_delta=False)

            mock_process.assert_called_once()
            call_kwargs = mock_process.call_args.kwargs
            assert call_kwargs["regex_offer_matches"] >= 2

    @pytest.mark.asyncio
    async def test_ambiguous_email_still_stored(self):
        """0 regex matches → process_email_intelligence still called (regression)."""
        mock_process = AsyncMock(return_value={"id": 2, "classification": "general"})
        mock_db = MagicMock()

        from app.connectors.email_mining import EmailMiner

        msg = _fake_msg(body="Sounds good, see you then.")

        with (
            patch.object(EmailMiner, "_already_processed", return_value=set()),
            patch.object(EmailMiner, "_mark_processed"),
            patch.object(EmailMiner, "_get_delta_token", return_value=None),
            patch.object(
                EmailMiner,
                "_search_messages",
                new_callable=AsyncMock,
                return_value=[msg],
            ),
            patch(
                "app.services.email_intelligence_service.process_email_intelligence",
                mock_process,
            ),
        ):
            miner = self._make_miner(mock_db, user_id=1)
            await miner.scan_inbox(lookback_days=7, max_messages=10, use_delta=False)

            mock_process.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_db_skips_intelligence(self):
        """EmailMiner(db=None) → process_email_intelligence never called."""
        mock_process = AsyncMock()

        from app.connectors.email_mining import EmailMiner

        msg = _fake_msg(body="We have LM358 in stock, unit price $2.00")

        with (
            patch.object(EmailMiner, "_already_processed", return_value=set()),
            patch.object(EmailMiner, "_mark_processed"),
            patch.object(EmailMiner, "_get_delta_token", return_value=None),
            patch.object(
                EmailMiner,
                "_search_messages",
                new_callable=AsyncMock,
                return_value=[msg],
            ),
            patch(
                "app.services.email_intelligence_service.process_email_intelligence",
                mock_process,
            ),
        ):
            miner = self._make_miner(db=None, user_id=None)
            await miner.scan_inbox(lookback_days=7, max_messages=10, use_delta=False)

            mock_process.assert_not_called()


# ── Fix 2: needs_review logic ──────────────────────────────────────────


class TestNeedsReviewLogic:
    """Verify store_email_intelligence sets needs_review/auto_applied correctly
    based on classification type and confidence."""

    def _store(self, db, cls_type, confidence, parsed_quotes=None):
        """Helper: call store_email_intelligence with given params."""
        return store_email_intelligence(
            db,
            message_id=f"msg-{cls_type}-{confidence}",
            user_id=1,
            sender_email="test@vendor.com",
            subject="Test",
            received_at=datetime.now(timezone.utc),
            conversation_id=None,
            classification={
                "classification": cls_type,
                "confidence": confidence,
                "has_pricing": False,
                "parts_mentioned": [],
                "brands_detected": [],
                "commodities_detected": [],
            },
            parsed_quotes=parsed_quotes,
        )

    def test_spam_high_confidence_no_review(self, db_session, test_user):
        """spam at 0.9 → needs_review=False, auto_applied=False."""
        rec = self._store(db_session, "spam", 0.9)
        assert rec.needs_review is False
        assert rec.auto_applied is False

    def test_ooo_high_confidence_no_review(self, db_session, test_user):
        """ooo at 0.8 → needs_review=False, auto_applied=False."""
        rec = self._store(db_session, "ooo", 0.8)
        assert rec.needs_review is False
        assert rec.auto_applied is False

    def test_general_mid_confidence_no_review(self, db_session, test_user):
        """general at 0.6 → needs_review=False, auto_applied=False."""
        rec = self._store(db_session, "general", 0.6)
        assert rec.needs_review is False
        assert rec.auto_applied is False

    def test_offer_high_with_quotes_auto_applied(self, db_session, test_user):
        """offer at 0.85 + parsed_quotes → auto_applied=True, needs_review=False."""
        rec = self._store(
            db_session,
            "offer",
            0.85,
            parsed_quotes={"lines": [{"mpn": "LM358", "price": 1.5}]},
        )
        assert rec.auto_applied is True
        assert rec.needs_review is False

    def test_offer_high_without_quotes_informational(self, db_session, test_user):
        """offer at 0.85 without parsed_quotes → both False."""
        rec = self._store(db_session, "offer", 0.85)
        assert rec.auto_applied is False
        assert rec.needs_review is False

    def test_offer_mid_confidence_needs_review(self, db_session, test_user):
        """offer at 0.65 → needs_review=True."""
        rec = self._store(db_session, "offer", 0.65)
        assert rec.needs_review is True
        assert rec.auto_applied is False

    def test_offer_low_confidence_no_flags(self, db_session, test_user):
        """offer at 0.3 → both False."""
        rec = self._store(db_session, "offer", 0.3)
        assert rec.needs_review is False
        assert rec.auto_applied is False


# ── Fix 3: _job_email_health_update ────────────────────────────────────


class TestJobEmailHealthUpdate:
    """Tests for the _job_email_health_update scheduler job."""

    @pytest.mark.asyncio
    async def test_calls_batch_update(self):
        """Job calls batch_update_email_health and logs result."""
        mock_batch = MagicMock(return_value={"updated": 42, "skipped": 5})
        mock_db = MagicMock()

        with (
            patch("app.database.SessionLocal", return_value=mock_db),
            patch(
                "app.services.response_analytics.batch_update_email_health",
                mock_batch,
            ),
        ):
            from app.jobs.email_jobs import _job_email_health_update

            await _job_email_health_update()

            mock_db.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_handles_error(self):
        """Job catches exceptions without propagating."""
        mock_db = MagicMock()

        with (
            patch("app.database.SessionLocal", return_value=mock_db),
            patch(
                "app.services.response_analytics.batch_update_email_health",
                side_effect=RuntimeError("db down"),
            ),
        ):
            from app.jobs.email_jobs import _job_email_health_update

            # Should not raise
            await _job_email_health_update()

            mock_db.rollback.assert_called()
            mock_db.close.assert_called_once()

    def test_registered_in_scheduler(self):
        """email_health_update job is registered in configure_scheduler."""
        from app.scheduler import configure_scheduler

        mock_scheduler = MagicMock()
        with (
            patch("app.scheduler.scheduler", mock_scheduler),
            patch(
                "app.config.settings",
                MagicMock(
                    inbox_scan_interval_min=30,
                    contacts_sync_enabled=False,
                    activity_tracking_enabled=False,
                    po_verify_interval_min=15,
                    buyplan_auto_complete_hour=18,
                    buyplan_auto_complete_tz="UTC",
                    proactive_matching_enabled=False,
                    deep_email_mining_enabled=False,
                    deep_enrichment_enabled=False,
                    contact_scoring_enabled=False,
                ),
            ),
        ):
            configure_scheduler()

            job_ids = [c.kwargs.get("id") for c in mock_scheduler.add_job.call_args_list]
            assert "email_health_update" in job_ids


# ── Fix 4: _job_calendar_scan ──────────────────────────────────────────


class TestJobCalendarScan:
    """Tests for the _job_calendar_scan scheduler job."""

    @pytest.mark.asyncio
    async def test_calls_scan_calendar_events(self):
        """Connected m365 user → scan_calendar_events called."""
        mock_user = MagicMock()
        mock_user.id = 1
        mock_user.email = "test@trioscs.com"
        mock_user.access_token = "tok"
        mock_user.m365_connected = True
        mock_user.refresh_token = "ref"

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.all.return_value = [mock_user]
        mock_db.get.return_value = mock_user

        mock_scan = AsyncMock(return_value={"events_found": 3})

        with (
            patch("app.database.SessionLocal", return_value=mock_db),
            patch(
                "app.scheduler.get_valid_token",
                new_callable=AsyncMock,
                return_value="valid-token",
            ),
            patch(
                "app.services.calendar_intelligence.scan_calendar_events",
                mock_scan,
            ),
        ):
            from app.jobs.email_jobs import _job_calendar_scan

            await _job_calendar_scan()

            mock_scan.assert_called_once()
            args = mock_scan.call_args
            assert args[0][0] == "valid-token"
            assert args[0][1] == 1

    @pytest.mark.asyncio
    async def test_skips_disconnected(self):
        """m365_connected=False → scan_calendar_events not called."""
        mock_user = MagicMock()
        mock_user.id = 2
        mock_user.access_token = "tok"
        mock_user.m365_connected = False
        mock_user.refresh_token = "ref"

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.all.return_value = [mock_user]

        mock_scan = AsyncMock()

        with (
            patch("app.database.SessionLocal", return_value=mock_db),
            patch(
                "app.services.calendar_intelligence.scan_calendar_events",
                mock_scan,
            ),
        ):
            from app.jobs.email_jobs import _job_calendar_scan

            await _job_calendar_scan()

            mock_scan.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_no_token(self):
        """get_valid_token returns None → scan_calendar_events not called."""
        mock_user = MagicMock()
        mock_user.id = 3
        mock_user.email = "notoken@trioscs.com"
        mock_user.access_token = "tok"
        mock_user.m365_connected = True
        mock_user.refresh_token = "ref"

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.all.return_value = [mock_user]
        mock_db.get.return_value = mock_user

        mock_scan = AsyncMock()

        with (
            patch("app.database.SessionLocal", return_value=mock_db),
            patch(
                "app.scheduler.get_valid_token",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "app.services.calendar_intelligence.scan_calendar_events",
                mock_scan,
            ),
        ):
            from app.jobs.email_jobs import _job_calendar_scan

            await _job_calendar_scan()

            mock_scan.assert_not_called()

    def test_registered_in_scheduler(self):
        """calendar_scan job is registered in configure_scheduler."""
        from app.scheduler import configure_scheduler

        mock_scheduler = MagicMock()
        with (
            patch("app.scheduler.scheduler", mock_scheduler),
            patch(
                "app.config.settings",
                MagicMock(
                    inbox_scan_interval_min=30,
                    contacts_sync_enabled=False,
                    activity_tracking_enabled=False,
                    po_verify_interval_min=15,
                    buyplan_auto_complete_hour=18,
                    buyplan_auto_complete_tz="UTC",
                    proactive_matching_enabled=False,
                    deep_email_mining_enabled=False,
                    deep_enrichment_enabled=False,
                    contact_scoring_enabled=False,
                ),
            ),
        ):
            configure_scheduler()

            job_ids = [c.kwargs.get("id") for c in mock_scheduler.add_job.call_args_list]
            assert "calendar_scan" in job_ids
