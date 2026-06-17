import os

os.environ["TESTING"] = "1"
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.onedrive_files import upload_bytes_to_onedrive


async def test_upload_returns_metadata_on_success(db_session):
    user = MagicMock()
    resp = MagicMock(status_code=201)
    resp.json.return_value = {"id": "01ITEM", "webUrl": "https://od/x"}
    with (
        patch("app.services.onedrive_files.get_valid_token", AsyncMock(return_value="tok")),
        patch("app.services.onedrive_files.http") as http,
    ):
        http.put = AsyncMock(return_value=resp)
        out = await upload_bytes_to_onedrive(
            user, db_session, "AvailAI/Datasheets/1", "x.pdf", b"%PDF-1.4", "application/pdf"
        )
    assert out == {"onedrive_item_id": "01ITEM", "onedrive_url": "https://od/x", "size_bytes": 8}


async def test_upload_returns_none_without_token(db_session):
    with patch("app.services.onedrive_files.get_valid_token", AsyncMock(return_value=None)):
        out = await upload_bytes_to_onedrive(MagicMock(), db_session, "f", "x.pdf", b"x", "application/pdf")
    assert out is None
