"""Tests for CRM Phase 2b — AI interaction quality scoring.

Called by: pytest
Depends on: app.models.intelligence, app.services.activity_quality_service
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.auth import User
from app.models.intelligence import ActivityLog
from tests.conftest import engine  # noqa: F401


class TestActivityQualityColumns:
    """Test that ActivityLog has quality scoring columns."""

    def test_quality_score_column_exists(self, db_session: Session, test_user: User):
        """ActivityLog accepts quality_score."""
        log = ActivityLog(
            user_id=test_user.id,
            activity_type="phone_call",
            channel="phone",
            quality_score=75.0,
            quality_classification="conversation",
            is_meaningful=True,
        )
        db_session.add(log)
        db_session.flush()
        assert log.quality_score == 75.0
        assert log.quality_classification == "conversation"
        assert log.is_meaningful is True

    def test_quality_assessed_at_column(self, db_session: Session, test_user: User):
        """ActivityLog accepts quality_assessed_at timestamp."""
        now = datetime.now(timezone.utc)
        log = ActivityLog(
            user_id=test_user.id,
            activity_type="note",
            channel="manual",
            quality_assessed_at=now,
        )
        db_session.add(log)
        db_session.flush()
        assert log.quality_assessed_at == now


class TestActivityQualityService:
    """Test AI quality scoring service."""

    def test_score_activity_writes_quality_data(self, db_session: Session, test_user: User):
        """score_activity writes quality fields back to ActivityLog."""
        import asyncio

        from app.services.activity_quality_service import score_activity

        log = ActivityLog(
            user_id=test_user.id,
            activity_type="phone_call",
            channel="phone",
            event_type="call",
            direction="outbound",
            subject="Called about LM317 pricing",
            notes="Discussed pricing for 10K units",
            duration_seconds=300,
        )
        db_session.add(log)
        db_session.flush()
        log_id = log.id

        mock_result = {
            "is_meaningful": True,
            "quality_score": 82,
            "classification": "negotiation",
            "sentiment": "positive",
            "clean_summary": "Discussed LM317 pricing for 10K units, vendor quoted $0.42.",
        }

        with patch(
            "app.utils.claude_client.claude_structured",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            asyncio.get_event_loop().run_until_complete(score_activity(log_id, db_session))

        db_session.refresh(log)
        assert log.quality_score == 82.0
        assert log.quality_classification == "negotiation"
        assert log.is_meaningful is True
        assert log.quality_assessed_at is not None
        assert "LM317" in log.summary

    def test_score_activity_skips_already_scored(self, db_session: Session, test_user: User):
        """Already-scored activities are skipped."""
        import asyncio

        from app.services.activity_quality_service import score_activity

        now = datetime.now(timezone.utc)
        log = ActivityLog(
            user_id=test_user.id,
            activity_type="note",
            channel="manual",
            quality_assessed_at=now,
            quality_score=50.0,
        )
        db_session.add(log)
        db_session.flush()

        with patch(
            "app.utils.claude_client.claude_structured",
            new_callable=AsyncMock,
        ) as mock_claude:
            asyncio.get_event_loop().run_until_complete(score_activity(log.id, db_session))
            mock_claude.assert_not_called()


class TestAvailScoreInteractionQuality:
    """Test Interaction Quality sub-metric in Avail Score."""

    def test_sales_score_includes_interaction_quality(self, db_session: Session, test_user: User):
        """compute_sales_avail_score includes b6 interaction quality metric."""
        from datetime import date

        from app.services.avail_score_service import compute_sales_avail_score

        test_user.role = "sales"
        db_session.commit()

        result = compute_sales_avail_score(db_session, test_user.id, date.today())

        assert "b6" in result
        assert "b6_label" in result
        assert result["b6_label"] == "Interaction Quality"


class TestPerformanceTab:
    """Test Performance tab in CRM shell."""

    def test_performance_route_returns_200(self, client: TestClient):
        """GET /v2/partials/crm/performance returns 200."""
        resp = client.get("/v2/partials/crm/performance")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")

    def test_performance_shows_team_header(self, client: TestClient):
        """Performance tab renders Team Performance header."""
        resp = client.get("/v2/partials/crm/performance")
        assert "Team Performance" in resp.text

    def test_crm_shell_has_performance_tab(self, client: TestClient):
        """CRM shell renders Performance tab button."""
        resp = client.get("/v2/partials/crm/shell")
        assert "Performance" in resp.text


class TestQualityJobRegistration:
    """Test quality jobs are registered."""

    def test_register_quality_jobs_exists(self):
        """register_quality_jobs function exists."""
        from app.jobs.quality_jobs import register_quality_jobs

        assert callable(register_quality_jobs)


class TestActivityTimelineEnrichment:
    """Test quality badges and summaries on activity timelines."""

    def test_activity_tab_renders_ai_summary(self, client: TestClient, db_session: Session, test_user: User):
        """Customer activity tab shows AI summary when available."""
        from app.models.crm import Company

        company = Company(name="Timeline Test Co", is_active=True)
        db_session.add(company)
        db_session.flush()

        log = ActivityLog(
            user_id=test_user.id,
            activity_type="phone_call",
            channel="phone",
            company_id=company.id,
            quality_score=80.0,
            quality_classification="conversation",
            is_meaningful=True,
            summary="Discussed component availability and pricing",
            quality_assessed_at=datetime.now(timezone.utc),
        )
        db_session.add(log)
        db_session.commit()

        resp = client.get(f"/v2/partials/customers/{company.id}/tab/activity")
        assert resp.status_code == 200
        assert "Discussed component availability" in resp.text
