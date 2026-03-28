"""Tests for app/routers/quote_builder.py — quote builder modal, data, save, export.

Called by: pytest
Depends on: conftest fixtures (client, test_requisition, test_user, test_customer_site, test_quote)
"""

from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Quote, Requisition


class TestQuoteBuilderData:
    def test_get_data_valid_req(self, client: TestClient, test_requisition: Requisition):
        with patch("app.dependencies.get_req_for_user", return_value=test_requisition):
            with patch("app.services.quote_builder_service.get_builder_data", return_value=[]):
                with patch("app.services.quote_builder_service.apply_smart_defaults"):
                    resp = client.get(f"/v2/partials/quote-builder/{test_requisition.id}/data")
        assert resp.status_code == 200
        assert "lines" in resp.json()

    def test_get_data_invalid_req(self, client: TestClient):
        with patch("app.dependencies.get_req_for_user", return_value=None):
            resp = client.get("/v2/partials/quote-builder/99999/data")
        assert resp.status_code == 404

    def test_get_data_with_requirement_ids(self, client: TestClient, test_requisition: Requisition):
        with patch("app.dependencies.get_req_for_user", return_value=test_requisition):
            with patch("app.services.quote_builder_service.get_builder_data", return_value=[]) as mock_data:
                with patch("app.services.quote_builder_service.apply_smart_defaults"):
                    resp = client.get(
                        f"/v2/partials/quote-builder/{test_requisition.id}/data",
                        params={"requirement_ids": "1,2,3"},
                    )
        assert resp.status_code == 200
        mock_data.assert_called_once()
        call_kwargs = mock_data.call_args
        assert call_kwargs[1]["requirement_ids"] == [1, 2, 3]


class TestQuoteBuilderMultiData:
    def test_multi_data_valid(self, client: TestClient, test_requisition: Requisition):
        with patch("app.dependencies.get_req_for_user", return_value=test_requisition):
            with patch("app.services.quote_builder_service.get_builder_data", return_value=[]):
                with patch("app.services.quote_builder_service.apply_smart_defaults"):
                    resp = client.get(
                        "/v2/partials/quote-builder/multi/data",
                        params={"requisition_ids": str(test_requisition.id)},
                    )
        assert resp.status_code == 200
        assert "lines" in resp.json()

    def test_multi_data_empty_ids(self, client: TestClient):
        resp = client.get("/v2/partials/quote-builder/multi/data", params={"requisition_ids": ""})
        assert resp.status_code == 400

    def test_multi_data_invalid_ids(self, client: TestClient):
        resp = client.get("/v2/partials/quote-builder/multi/data", params={"requisition_ids": "abc"})
        assert resp.status_code == 400


class TestQuoteBuilderSave:
    _VALID_LINE = {
        "requirement_id": 1,
        "mpn": "LM317T",
        "manufacturer": "Texas Instruments",
        "qty": 100,
        "cost_price": 0.50,
        "sell_price": 0.75,
        "margin_pct": 33.3,
    }

    def test_save_missing_customer_site(self, client: TestClient, test_requisition: Requisition):
        test_requisition.customer_site_id = None
        with patch("app.dependencies.get_req_for_user", return_value=test_requisition):
            resp = client.post(
                f"/v2/partials/quote-builder/{test_requisition.id}/save",
                json={"lines": [self._VALID_LINE]},
            )
        assert resp.status_code == 400

    def test_save_req_not_found(self, client: TestClient):
        with patch("app.dependencies.get_req_for_user", return_value=None):
            resp = client.post(
                "/v2/partials/quote-builder/99999/save",
                json={"lines": [self._VALID_LINE]},
            )
        assert resp.status_code == 404

    def test_save_success(
        self,
        client: TestClient,
        test_requisition: Requisition,
        test_customer_site,
        db_session: Session,
    ):
        test_requisition.customer_site_id = test_customer_site.id
        db_session.flush()
        save_result = {"quote_id": 1, "quote_number": "Q-001"}
        with patch("app.dependencies.get_req_for_user", return_value=test_requisition):
            with patch(
                "app.services.quote_builder_service.save_quote_from_builder",
                return_value=save_result,
            ):
                resp = client.post(
                    f"/v2/partials/quote-builder/{test_requisition.id}/save",
                    json={"lines": [self._VALID_LINE]},
                )
        assert resp.status_code == 200
        assert resp.json()["quote_number"] == "Q-001"


class TestQuoteBuilderExportExcel:
    def test_export_excel_not_found(self, client: TestClient):
        resp = client.get("/v2/partials/quote-builder/1/export/excel", params={"quote_id": 99999})
        assert resp.status_code == 404

    def test_export_excel_success(self, client: TestClient, test_quote: Quote):
        with patch(
            "app.services.quote_builder_service.build_excel_export",
            return_value=b"fake-xlsx-content",
        ):
            resp = client.get(
                f"/v2/partials/quote-builder/{test_quote.requisition_id}/export/excel",
                params={"quote_id": test_quote.id},
            )
        assert resp.status_code == 200
        assert "spreadsheetml" in resp.headers["content-type"]

    def test_export_excel_wrong_req_id(self, client: TestClient, test_quote: Quote):
        resp = client.get(
            "/v2/partials/quote-builder/99999/export/excel",
            params={"quote_id": test_quote.id},
        )
        assert resp.status_code == 404


class TestQuoteBuilderExportPdf:
    def test_export_pdf_not_found(self, client: TestClient):
        resp = client.get("/v2/partials/quote-builder/1/export/pdf", params={"quote_id": 99999})
        assert resp.status_code == 404

    def test_export_pdf_success(self, client: TestClient, test_quote: Quote):
        with patch(
            "app.services.document_service.generate_quote_report_pdf",
            return_value=b"%PDF-fake",
        ):
            resp = client.get(
                f"/v2/partials/quote-builder/{test_quote.requisition_id}/export/pdf",
                params={"quote_id": test_quote.id},
            )
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/pdf"
