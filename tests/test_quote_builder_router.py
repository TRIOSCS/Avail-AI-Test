"""Tests for app/routers/quote_builder.py — PDF export + deleted-modal route sweep.

The two-panel modal's /data + /save JSON endpoints and the Excel export were
deleted in the Wave 3 quote-builder consolidation (the Build-Quote tab is the ONE
builder; the multi entry renders the same body — covered in
test_build_quote_multi_req.py / test_build_quote_tab.py). This file keeps the
surviving PDF export coverage and pins the deleted routes to 404/405.

Called by: pytest
Depends on: conftest fixtures (client, test_quote)
"""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.models import Quote


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


class TestModalRoutesDeleted:
    """The modal-era endpoints must stay dead — no shell, no JSON data/save, no
    Excel."""

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            pytest.param("GET", "/v2/partials/quote-builder/1", id="modal_shell"),
            pytest.param("GET", "/v2/partials/quote-builder/1/data", id="single_data"),
            pytest.param("GET", "/v2/partials/quote-builder/multi/data", id="multi_data"),
            pytest.param("POST", "/v2/partials/quote-builder/1/save", id="single_save"),
            pytest.param("POST", "/v2/partials/quote-builder/multi/save", id="multi_save"),
            pytest.param("GET", "/v2/partials/quote-builder/1/export/excel", id="excel_export"),
        ],
    )
    def test_deleted_route_is_gone(self, client: TestClient, method: str, path: str):
        resp = client.request(method, path)
        # 404 (no route) or 405 (path prefix matches a surviving route, method doesn't).
        assert resp.status_code in (404, 405)
