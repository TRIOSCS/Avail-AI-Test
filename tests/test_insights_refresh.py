"""tests/test_insights_refresh.py — Regression tests for P0.1: the four AI insights
"Refresh" HTMX endpoints must await the async knowledge_service generator instead of
firing-and-discarding the coroutine.

Targets: app/routers/htmx_views.py::{requisition,vendor,company,pipeline}_insights_refresh
Called by: pytest autodiscovery
Depends on: conftest.py fixtures (client, db_session, test_requisition, test_vendor_card,
    test_company), app.services.knowledge_service (patched with AsyncMock at source)
"""

import os
from unittest.mock import AsyncMock, patch

os.environ["TESTING"] = "1"

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Company, Requisition, VendorCard
from app.models.knowledge import KnowledgeEntry


class TestInsightsRefreshAwaited:
    def test_requisition_insights_refresh_awaits_generate_insights(
        self, client: TestClient, test_requisition: Requisition
    ):
        with patch("app.services.knowledge_service.generate_insights", new_callable=AsyncMock) as mock_gen:
            resp = client.post(f"/v2/partials/requisitions/{test_requisition.id}/insights/refresh")
        assert resp.status_code == 200
        mock_gen.assert_awaited_once()
        assert mock_gen.await_args.args[1] == test_requisition.id
        # P2.8: the HTMX refresh endpoint must request the tightened interactive
        # Claude-call budget (25s timeout, single attempt).
        assert mock_gen.await_args.kwargs.get("interactive") is True

    def test_vendor_insights_refresh_awaits_generate_vendor_insights(
        self, client: TestClient, test_vendor_card: VendorCard
    ):
        with patch("app.services.knowledge_service.generate_vendor_insights", new_callable=AsyncMock) as mock_gen:
            resp = client.post(f"/v2/partials/vendors/{test_vendor_card.id}/insights/refresh")
        assert resp.status_code == 200
        mock_gen.assert_awaited_once()
        assert mock_gen.await_args.args[1] == test_vendor_card.id
        assert mock_gen.await_args.kwargs.get("interactive") is True

    def test_company_insights_refresh_awaits_generate_company_insights(self, client: TestClient, test_company: Company):
        with patch("app.services.knowledge_service.generate_company_insights", new_callable=AsyncMock) as mock_gen:
            resp = client.post(f"/v2/partials/customers/{test_company.id}/insights/refresh")
        assert resp.status_code == 200
        mock_gen.assert_awaited_once()
        assert mock_gen.await_args.args[1] == test_company.id
        assert mock_gen.await_args.kwargs.get("interactive") is True

    def test_pipeline_insights_refresh_awaits_generate_pipeline_insights(self, client: TestClient):
        with patch("app.services.knowledge_service.generate_pipeline_insights", new_callable=AsyncMock) as mock_gen:
            resp = client.post("/v2/partials/dashboard/pipeline-insights/refresh")
        assert resp.status_code == 200
        mock_gen.assert_awaited_once()
        assert mock_gen.await_args.kwargs.get("interactive") is True
        # The endpoint must actually return the rendered insights panel, not an
        # empty/error fragment, after awaiting the generator.
        assert "AI Insights" in resp.text
        assert 'id="insights-panel"' in resp.text


class TestInsightsRefreshRollbackOnFailure:
    def test_requisition_refresh_rolls_back_and_falls_back_to_cache(
        self, client: TestClient, test_requisition: Requisition, db_session: Session
    ):
        stale = KnowledgeEntry(
            entry_type="ai_insight",
            content="STALE-CACHED-REQ-INSIGHT",
            source="ai_generated",
            requisition_id=test_requisition.id,
        )
        db_session.add(stale)
        db_session.commit()
        with patch(
            "app.services.knowledge_service.generate_insights",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        ):
            resp = client.post(f"/v2/partials/requisitions/{test_requisition.id}/insights/refresh")
        assert resp.status_code == 200
        # Fell back to the stale cached insight instead of showing nothing/an error.
        assert "STALE-CACHED-REQ-INSIGHT" in resp.text
        # The failed generate_insights() must have been rolled back, not left the
        # session in a broken/pending-rollback state for the fallback query.
        assert db_session.query(KnowledgeEntry).filter_by(requisition_id=test_requisition.id).count() == 1

    def test_vendor_refresh_rolls_back_and_falls_back_to_cache(
        self, client: TestClient, test_vendor_card: VendorCard, db_session: Session
    ):
        stale = KnowledgeEntry(
            entry_type="ai_insight",
            content="STALE-CACHED-VENDOR-INSIGHT",
            source="ai_generated",
            vendor_card_id=test_vendor_card.id,
        )
        db_session.add(stale)
        db_session.commit()
        with patch(
            "app.services.knowledge_service.generate_vendor_insights",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        ):
            resp = client.post(f"/v2/partials/vendors/{test_vendor_card.id}/insights/refresh")
        assert resp.status_code == 200
        assert "STALE-CACHED-VENDOR-INSIGHT" in resp.text
        assert db_session.query(KnowledgeEntry).filter_by(vendor_card_id=test_vendor_card.id).count() == 1

    def test_company_refresh_rolls_back_and_falls_back_to_cache(
        self, client: TestClient, test_company: Company, db_session: Session
    ):
        stale = KnowledgeEntry(
            entry_type="ai_insight",
            content="STALE-CACHED-COMPANY-INSIGHT",
            source="ai_generated",
            company_id=test_company.id,
        )
        db_session.add(stale)
        db_session.commit()
        with patch(
            "app.services.knowledge_service.generate_company_insights",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        ):
            resp = client.post(f"/v2/partials/customers/{test_company.id}/insights/refresh")
        assert resp.status_code == 200
        assert "STALE-CACHED-COMPANY-INSIGHT" in resp.text
        assert db_session.query(KnowledgeEntry).filter_by(company_id=test_company.id).count() == 1

    def test_pipeline_refresh_rolls_back_and_falls_back_to_cache(self, client: TestClient, db_session: Session):
        stale = KnowledgeEntry(
            entry_type="ai_insight",
            content="STALE-CACHED-PIPELINE-INSIGHT",
            source="ai_generated",
            mpn="__pipeline__",
        )
        db_session.add(stale)
        db_session.commit()
        with patch(
            "app.services.knowledge_service.generate_pipeline_insights",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        ):
            resp = client.post("/v2/partials/dashboard/pipeline-insights/refresh")
        assert resp.status_code == 200
        assert "STALE-CACHED-PIPELINE-INSIGHT" in resp.text
        assert db_session.query(KnowledgeEntry).filter_by(mpn="__pipeline__").count() == 1
