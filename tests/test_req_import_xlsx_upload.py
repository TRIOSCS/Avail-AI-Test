"""test_req_import_xlsx_upload.py — P2.6 coverage for POST
/v2/partials/requisitions/import-parse.

Covers: openpyxl parse moved onto a worker thread (asyncio.to_thread) via
_parse_xlsx_rows, and the new MAX_IMPORT_UPLOAD_BYTES (10MB) upload cap — both
JSON-mode and HTML-fragment-mode responses.

Called by: pytest
Depends on: app/routers/htmx/requisitions.py, tests/conftest.py (client)
"""

from io import BytesIO
from unittest.mock import AsyncMock, patch

import openpyxl

from app.routers.htmx.requisitions import MAX_IMPORT_UPLOAD_BYTES, _parse_xlsx_rows


def _make_xlsx_bytes(rows: list[list[str]]) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


class TestParseXlsxRowsHelper:
    """Direct unit coverage for the sync helper dispatched via asyncio.to_thread."""

    def test_parses_rows_into_tab_separated_text(self):
        content = _make_xlsx_bytes([["LM317T", "500", "TI"], ["STM32F407", "100", "ST"]])
        text = _parse_xlsx_rows(content)
        assert "LM317T\t500\tTI" in text
        assert "STM32F407\t100\tST" in text

    def test_blank_rows_are_skipped(self):
        content = _make_xlsx_bytes([["LM317T", "500"], ["", ""], ["STM32F407", "100"]])
        text = _parse_xlsx_rows(content)
        lines = [line for line in text.split("\n") if line]
        assert len(lines) == 2


class TestImportParseXlsxUploadEndToEnd:
    def test_xlsx_upload_parses_via_worker_thread(self, client):
        content = _make_xlsx_bytes([["LM358DR", "500", "TI", "new"]])
        mock_result = {
            "name": "XLSX Import",
            "requirements": [{"primary_mpn": "LM358DR", "target_qty": 500, "brand": "TI", "condition": "new"}],
        }

        with patch(
            "app.routers.htmx.requisitions.parse_freeform_rfq",
            new_callable=AsyncMock,
            return_value=mock_result,
        ) as mock_parse:
            resp = client.post(
                "/v2/partials/requisitions/import-parse",
                data={"name": "Import Test", "raw_text": ""},
                files={
                    "file": ("parts.xlsx", content, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                },
            )

        assert resp.status_code == 200
        mock_parse.assert_awaited_once()
        parsed_text = mock_parse.await_args.args[0]
        assert "LM358DR" in parsed_text


class TestImportParseUploadSizeCap:
    def test_oversized_upload_rejected_json_mode(self, client):
        oversized = b"x" * (MAX_IMPORT_UPLOAD_BYTES + 1)
        resp = client.post(
            "/v2/partials/requisitions/import-parse?format=json",
            data={"name": "Import Test", "raw_text": ""},
            files={"file": ("huge.csv", oversized, "text/csv")},
        )
        assert resp.status_code == 413
        body = resp.json()
        assert body["error"] == "File too large — 10MB maximum."
        assert body["status_code"] == 413
        assert "request_id" in body

    def test_oversized_upload_rejected_html_mode(self, client):
        oversized = b"x" * (MAX_IMPORT_UPLOAD_BYTES + 1)
        resp = client.post(
            "/v2/partials/requisitions/import-parse",
            data={"name": "Import Test", "raw_text": ""},
            files={"file": ("huge.csv", oversized, "text/csv")},
        )
        assert resp.status_code == 413
        assert "10MB maximum" in resp.text

    def test_normal_sized_upload_still_parses(self, client):
        """A file well under the cap is unaffected by the new size check."""
        content = b"LM358DR,500,TI,new\n"
        mock_result = {
            "name": "CSV Import",
            "requirements": [{"primary_mpn": "LM358DR", "target_qty": 500, "brand": "TI", "condition": "new"}],
        }
        with patch(
            "app.routers.htmx.requisitions.parse_freeform_rfq",
            new_callable=AsyncMock,
            return_value=mock_result,
        ) as mock_parse:
            resp = client.post(
                "/v2/partials/requisitions/import-parse",
                data={"name": "Import Test", "raw_text": ""},
                files={"file": ("parts.csv", content, "text/csv")},
            )
        assert resp.status_code == 200
        mock_parse.assert_awaited_once()
        # The uploaded CSV content reached the AI parser unchanged (the size-cap check
        # doesn't consume or corrupt the payload) and the parsed MPN made it into the
        # returned unified-modal HTML.
        assert mock_parse.await_args.args[0] == content.decode()
        assert "LM358DR" in resp.text
