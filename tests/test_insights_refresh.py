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


class TestInsightsRefreshAwaited:
    def test_requisition_insights_refresh_awaits_generate_insights(
        self, client: TestClient, test_requisition: Requisition
    ):
        with patch("app.services.knowledge_service.generate_insights", new_callable=AsyncMock) as mock_gen:
            resp = client.post(f"/v2/partials/requisitions/{test_requisition.id}/insights/refresh")
        assert resp.status_code == 200
        mock_gen.assert_awaited_once()
        assert mock_gen.await_args.args[1] == test_requisition.id

    def test_vendor_insights_refresh_awaits_generate_vendor_insights(
        self, client: TestClient, test_vendor_card: VendorCard
    ):
        with patch("app.services.knowledge_service.generate_vendor_insights", new_callable=AsyncMock) as mock_gen:
            resp = client.post(f"/v2/partials/vendors/{test_vendor_card.id}/insights/refresh")
        assert resp.status_code == 200
        mock_gen.assert_awaited_once()
        assert mock_gen.await_args.args[1] == test_vendor_card.id

    def test_company_insights_refresh_awaits_generate_company_insights(self, client: TestClient, test_company: Company):
        with patch("app.services.knowledge_service.generate_company_insights", new_callable=AsyncMock) as mock_gen:
            resp = client.post(f"/v2/partials/customers/{test_company.id}/insights/refresh")
        assert resp.status_code == 200
        mock_gen.assert_awaited_once()
        assert mock_gen.await_args.args[1] == test_company.id

    def test_pipeline_insights_refresh_awaits_generate_pipeline_insights(self, client: TestClient):
        with patch("app.services.knowledge_service.generate_pipeline_insights", new_callable=AsyncMock) as mock_gen:
            resp = client.post("/v2/partials/dashboard/pipeline-insights/refresh")
        assert resp.status_code == 200
        mock_gen.assert_awaited_once()


class TestInsightsRefreshRollbackOnFailure:
    def test_requisition_refresh_rolls_back_and_falls_back_to_cache(
        self, client: TestClient, test_requisition: Requisition, db_session: Session
    ):
        with patch(
            "app.services.knowledge_service.generate_insights",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        ):
            resp = client.post(f"/v2/partials/requisitions/{test_requisition.id}/insights/refresh")
        assert resp.status_code == 200

    def test_vendor_refresh_rolls_back_and_falls_back_to_cache(self, client: TestClient, test_vendor_card: VendorCard):
        with patch(
            "app.services.knowledge_service.generate_vendor_insights",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        ):
            resp = client.post(f"/v2/partials/vendors/{test_vendor_card.id}/insights/refresh")
        assert resp.status_code == 200

    def test_company_refresh_rolls_back_and_falls_back_to_cache(self, client: TestClient, test_company: Company):
        with patch(
            "app.services.knowledge_service.generate_company_insights",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        ):
            resp = client.post(f"/v2/partials/customers/{test_company.id}/insights/refresh")
        assert resp.status_code == 200

    def test_pipeline_refresh_rolls_back_and_falls_back_to_cache(self, client: TestClient):
        with patch(
            "app.services.knowledge_service.generate_pipeline_insights",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        ):
            resp = client.post("/v2/partials/dashboard/pipeline-insights/refresh")
        assert resp.status_code == 200
