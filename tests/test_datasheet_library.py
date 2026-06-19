import os

os.environ["TESTING"] = "1"
from unittest.mock import AsyncMock, MagicMock, patch

from app.services import datasheet_library as dl


async def test_upload_returns_none_when_unconfigured():
    with patch.object(dl.settings, "datasheet_library_drive_id", ""):
        assert await dl.upload_datasheet_to_library("x.pdf", b"%PDF", "application/pdf") is None


async def test_upload_returns_metadata_on_success():
    resp = MagicMock(status_code=201)
    resp.json.return_value = {"id": "ITM", "webUrl": "https://sp/x"}
    with (
        patch.object(dl.settings, "datasheet_library_drive_id", "DRV"),
        patch.object(dl.settings, "datasheet_library_subpath", "Datasheets"),
        patch("app.services.datasheet_library.get_app_graph_token", AsyncMock(return_value="T")),
        patch("app.services.datasheet_library.http") as http,
    ):
        http.put = AsyncMock(return_value=resp)
        out = await dl.upload_datasheet_to_library(
            "LM317-datasheet.pdf", b"%PDF-1.4", "application/pdf", manufacturer="TI"
        )
    assert out == {
        "library_item_id": "ITM",
        "library_web_url": "https://sp/x",
        "size_bytes": 8,
        "library_drive_id": "DRV",
    }
    # path used the manufacturer folder under the configured subpath
    called_url = http.put.call_args[0][0]
    assert "/drives/DRV/root:/Datasheets/TI/LM317-datasheet.pdf:/content" in called_url


async def test_upload_none_on_non_2xx():
    resp = MagicMock(status_code=500)
    resp.text = "err"
    with (
        patch.object(dl.settings, "datasheet_library_drive_id", "DRV"),
        patch("app.services.datasheet_library.get_app_graph_token", AsyncMock(return_value="T")),
        patch("app.services.datasheet_library.http") as http,
    ):
        http.put = AsyncMock(return_value=resp)
        assert await dl.upload_datasheet_to_library("x.pdf", b"x", "application/pdf") is None


async def test_upload_none_on_exception():
    with (
        patch.object(dl.settings, "datasheet_library_drive_id", "DRV"),
        patch("app.services.datasheet_library.get_app_graph_token", AsyncMock(return_value="T")),
        patch("app.services.datasheet_library.http") as http,
    ):
        http.put = AsyncMock(side_effect=RuntimeError("boom"))
        assert await dl.upload_datasheet_to_library("x.pdf", b"x", "application/pdf") is None


async def test_fetch_bytes_ok():
    resp = MagicMock(status_code=200, content=b"%PDF-bytes")
    with (
        patch("app.services.datasheet_library.get_app_graph_token", AsyncMock(return_value="T")),
        patch("app.services.datasheet_library.http_redirect") as httpr,
    ):
        httpr.get = AsyncMock(return_value=resp)
        assert await dl.fetch_datasheet_bytes("DRV", "ITM") == b"%PDF-bytes"


def test_sanitize_blocks_traversal_and_specials():
    from app.services.datasheet_library import _sanitize

    assert "/" not in _sanitize("../../etc/passwd") and ".." not in _sanitize("../../etc/passwd")
    assert ":" not in _sanitize("Acme: Inc*?<>|")
    assert _sanitize("") == "_unknown"
    assert _sanitize("...") == "_unknown"
    assert _sanitize("LM317-datasheet.pdf") == "LM317-datasheet.pdf"
    assert len(_sanitize("x" * 500)) <= 128
