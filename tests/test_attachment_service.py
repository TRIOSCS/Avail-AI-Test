"""tests/test_attachment_service.py — Unit tests for app/services/attachment_service.py.

Covers all paths through the shared attachment service:
  - Company-library backend (drive_id set): stores non-NULL library_drive_id, calls
    library PUT, does NOT call OneDrive.
  - OneDrive fallback (drive_id empty): stores NULL library_drive_id, calls OneDrive PUT.
  - Configured library PUT failure (500) → raises 502, OneDrive was NOT called.
  - No drive_id + no user token → 401 with clear message.
  - Oversize file → 400.
  - Bad extension → 400.
  - serialize() returns kind="library" / kind="onedrive" correctly.
  - open_attachment() streams for library rows, redirects for OneDrive rows.
  - remove_attachment() warning path (cloud delete fails → warning in response).

Called by: pytest
Depends on: conftest.py (db_session, test_user fixtures), app models, attachment_service
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.responses import RedirectResponse, StreamingResponse
from sqlalchemy.orm import Session

from app.models import Company, CompanyAttachment, User

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DRIVE_ID = "drive-abc-123"
_ITEM_ID = "item-xyz-456"
_WEB_URL = "https://sharepoint.example.com/file.pdf"
_ONEDRIVE_URL = "https://onedrive.example.com/file.pdf"


def _make_upload_file(
    filename: str = "test.pdf",
    content_type: str = "application/pdf",
    content: bytes = b"x" * 100,
):
    """Create a minimal UploadFile-like mock."""
    f = MagicMock()
    f.filename = filename
    f.content_type = content_type
    f.read = AsyncMock(return_value=content)
    return f


def _make_attachment_row(
    *,
    db: Session,
    company_id: int,
    user_id: int,
    library_drive_id: str | None = None,
    library_item_id: str = _ITEM_ID,
    library_web_url: str = _WEB_URL,
) -> CompanyAttachment:
    att = CompanyAttachment(
        company_id=company_id,
        file_name="test.pdf",
        library_item_id=library_item_id,
        library_drive_id=library_drive_id,
        library_web_url=library_web_url,
        content_type="application/pdf",
        size_bytes=100,
        uploaded_by_id=user_id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(att)
    db.commit()
    db.refresh(att)
    return att


# ---------------------------------------------------------------------------
# store_and_attach — company-library path (drive_id configured)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_store_library_path(db_session: Session, test_user: User, test_company: Company):
    """When drive_id is set, attachment is stored via company library; library_drive_id
    is saved on the row."""
    mock_response = MagicMock()
    mock_response.status_code = 201
    mock_response.json.return_value = {"id": _ITEM_ID, "webUrl": _WEB_URL}

    with (
        patch("app.services.attachment_service.settings") as mock_settings,
        patch("app.services.attachment_service._store") as mock_store,
    ):
        mock_settings.datasheet_library_drive_id = _DRIVE_ID
        mock_store.return_value = (_ITEM_ID, _DRIVE_ID, _WEB_URL)

        from app.services.attachment_service import store_and_attach

        file = _make_upload_file()
        att = await store_and_attach(
            db_session,
            model=CompanyAttachment,
            fk_field="company_id",
            entity_label="Companies",
            entity_id=test_company.id,
            file=file,
            user=test_user,
        )

    assert att.library_drive_id == _DRIVE_ID
    assert att.library_item_id == _ITEM_ID
    assert att.library_web_url == _WEB_URL
    assert att.company_id == test_company.id
    assert att.uploaded_by_id == test_user.id


@pytest.mark.asyncio
async def test_store_library_calls_library_not_onedrive(db_session: Session, test_user: User, test_company: Company):
    """When drive_id is configured, only the library PUT is called; OneDrive is never
    touched."""
    mock_response = MagicMock()
    mock_response.status_code = 201
    mock_response.json.return_value = {"id": _ITEM_ID, "webUrl": _WEB_URL}
    mock_put = AsyncMock(return_value=mock_response)

    with (
        patch("app.services.attachment_service.settings") as mock_settings,
        patch("app.services.graph_app_auth.get_app_graph_token", new_callable=AsyncMock, return_value="app-token"),
        patch("app.http_client.http") as mock_http,
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock) as mock_user_token,
    ):
        mock_settings.datasheet_library_drive_id = _DRIVE_ID
        mock_http.put = mock_put

        from app.services.attachment_service import _store

        item_id, drive_id, web_url = await _store(
            b"content",
            content_type="application/pdf",
            file_name="test.pdf",
            entity_label="Companies",
            entity_id=1,
            user=test_user,
            db=db_session,
        )

    assert drive_id == _DRIVE_ID
    assert item_id == _ITEM_ID
    mock_user_token.assert_not_called()
    # The PUT URL must contain the structural path for the company library
    put_url = mock_put.call_args[0][0]
    assert f"/drives/{_DRIVE_ID}/root:/Attachments/" in put_url


# ---------------------------------------------------------------------------
# store_and_attach — OneDrive fallback path (drive_id empty)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_store_onedrive_path(db_session: Session, test_user: User, test_company: Company):
    """When drive_id is empty, attachment lands on OneDrive; library_drive_id is
    NULL."""
    with (
        patch("app.services.attachment_service.settings") as mock_settings,
        patch("app.services.attachment_service._store") as mock_store,
    ):
        mock_settings.datasheet_library_drive_id = ""
        mock_store.return_value = (_ITEM_ID, None, _ONEDRIVE_URL)

        from app.services.attachment_service import store_and_attach

        file = _make_upload_file()
        att = await store_and_attach(
            db_session,
            model=CompanyAttachment,
            fk_field="company_id",
            entity_label="Companies",
            entity_id=test_company.id,
            file=file,
            user=test_user,
        )

    assert att.library_drive_id is None
    assert att.library_item_id == _ITEM_ID
    assert att.library_web_url == _ONEDRIVE_URL


@pytest.mark.asyncio
async def test_store_onedrive_calls_onedrive_not_library(db_session: Session, test_user: User):
    """When drive_id is empty, only the OneDrive PUT is called."""
    mock_response = MagicMock()
    mock_response.status_code = 201
    mock_response.json.return_value = {"id": _ITEM_ID, "webUrl": _ONEDRIVE_URL}
    mock_put = AsyncMock(return_value=mock_response)

    with (
        patch("app.services.attachment_service.settings") as mock_settings,
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="user-token"),
        patch("app.http_client.http") as mock_http,
    ):
        mock_settings.datasheet_library_drive_id = ""
        mock_http.put = mock_put

        from app.services.attachment_service import _store

        item_id, drive_id, web_url = await _store(
            b"content",
            content_type="application/pdf",
            file_name="test.pdf",
            entity_label="Companies",
            entity_id=1,
            user=test_user,
            db=db_session,
        )

    assert drive_id is None
    assert web_url == _ONEDRIVE_URL
    # Company-library token was never requested
    mock_put.assert_called_once()


# ---------------------------------------------------------------------------
# Honest errors — configured library PUT failure → 502, NOT OneDrive
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_library_put_failure_raises_502(db_session: Session, test_user: User):
    """When drive_id is set but the PUT returns 500, service raises 502 and does NOT
    fall back to OneDrive."""
    mock_response = MagicMock()
    mock_response.status_code = 500
    mock_response.text = "Internal Server Error"
    mock_put = AsyncMock(return_value=mock_response)

    with (
        patch("app.services.attachment_service.settings") as mock_settings,
        patch("app.services.graph_app_auth.get_app_graph_token", new_callable=AsyncMock, return_value="app-token"),
        patch("app.http_client.http") as mock_http,
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock) as mock_user_token,
    ):
        mock_settings.datasheet_library_drive_id = _DRIVE_ID
        mock_http.put = mock_put

        from app.services.attachment_service import _store

        with pytest.raises(HTTPException) as exc_info:
            await _store(
                b"content",
                content_type="application/pdf",
                file_name="test.pdf",
                entity_label="Companies",
                entity_id=1,
                user=test_user,
                db=db_session,
            )

    assert exc_info.value.status_code == 502
    assert "company library" in exc_info.value.detail.lower()
    mock_user_token.assert_not_called()


# ---------------------------------------------------------------------------
# Honest errors — library PUT returns 201 but body is non-JSON or missing 'id'
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_library_put_non_json_body_raises_502_no_db_row(
    db_session: Session, test_user: User, test_company: Company
):
    """Library PUT returns 201 but .json() raises → 502 is raised and NO DB row is
    created."""
    mock_response = MagicMock()
    mock_response.status_code = 201
    mock_response.json.side_effect = ValueError("not JSON")
    mock_put = AsyncMock(return_value=mock_response)

    with (
        patch("app.services.attachment_service.settings") as mock_settings,
        patch("app.services.graph_app_auth.get_app_graph_token", new_callable=AsyncMock, return_value="app-token"),
        patch("app.http_client.http") as mock_http,
    ):
        mock_settings.datasheet_library_drive_id = _DRIVE_ID
        mock_http.put = mock_put

        from app.services.attachment_service import store_and_attach

        file = _make_upload_file()
        with pytest.raises(HTTPException) as exc_info:
            await store_and_attach(
                db_session,
                model=CompanyAttachment,
                fk_field="company_id",
                entity_label="Companies",
                entity_id=test_company.id,
                file=file,
                user=test_user,
            )

    assert exc_info.value.status_code == 502
    assert "company library" in exc_info.value.detail.lower()
    # No DB row should have been created
    rows = db_session.query(CompanyAttachment).filter_by(company_id=test_company.id).all()
    assert rows == []


@pytest.mark.asyncio
async def test_library_put_missing_id_raises_502_no_db_row(db_session: Session, test_user: User, test_company: Company):
    """Library PUT returns 201 but body has no 'id' key → 502 is raised and NO DB row is
    created."""
    mock_response = MagicMock()
    mock_response.status_code = 201
    mock_response.json.return_value = {"webUrl": _WEB_URL}  # no 'id'
    mock_put = AsyncMock(return_value=mock_response)

    with (
        patch("app.services.attachment_service.settings") as mock_settings,
        patch("app.services.graph_app_auth.get_app_graph_token", new_callable=AsyncMock, return_value="app-token"),
        patch("app.http_client.http") as mock_http,
    ):
        mock_settings.datasheet_library_drive_id = _DRIVE_ID
        mock_http.put = mock_put

        from app.services.attachment_service import store_and_attach

        file = _make_upload_file()
        with pytest.raises(HTTPException) as exc_info:
            await store_and_attach(
                db_session,
                model=CompanyAttachment,
                fk_field="company_id",
                entity_label="Companies",
                entity_id=test_company.id,
                file=file,
                user=test_user,
            )

    assert exc_info.value.status_code == 502
    assert "company library" in exc_info.value.detail.lower()
    rows = db_session.query(CompanyAttachment).filter_by(company_id=test_company.id).all()
    assert rows == []


@pytest.mark.asyncio
async def test_onedrive_put_non_json_body_raises_502_no_db_row(
    db_session: Session, test_user: User, test_company: Company
):
    """OneDrive PUT returns 201 but .json() raises → 502 is raised and NO DB row is
    created."""
    mock_response = MagicMock()
    mock_response.status_code = 201
    mock_response.json.side_effect = ValueError("not JSON")
    mock_put = AsyncMock(return_value=mock_response)

    with (
        patch("app.services.attachment_service.settings") as mock_settings,
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="user-token"),
        patch("app.http_client.http") as mock_http,
    ):
        mock_settings.datasheet_library_drive_id = ""
        mock_http.put = mock_put

        from app.services.attachment_service import store_and_attach

        file = _make_upload_file()
        with pytest.raises(HTTPException) as exc_info:
            await store_and_attach(
                db_session,
                model=CompanyAttachment,
                fk_field="company_id",
                entity_label="Companies",
                entity_id=test_company.id,
                file=file,
                user=test_user,
            )

    assert exc_info.value.status_code == 502
    assert "onedrive" in exc_info.value.detail.lower()
    rows = db_session.query(CompanyAttachment).filter_by(company_id=test_company.id).all()
    assert rows == []


@pytest.mark.asyncio
async def test_onedrive_put_missing_id_raises_502_no_db_row(
    db_session: Session, test_user: User, test_company: Company
):
    """OneDrive PUT returns 201 but body has no 'id' key → 502 is raised and NO DB row
    is created."""
    mock_response = MagicMock()
    mock_response.status_code = 201
    mock_response.json.return_value = {"webUrl": _ONEDRIVE_URL}  # no 'id'
    mock_put = AsyncMock(return_value=mock_response)

    with (
        patch("app.services.attachment_service.settings") as mock_settings,
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="user-token"),
        patch("app.http_client.http") as mock_http,
    ):
        mock_settings.datasheet_library_drive_id = ""
        mock_http.put = mock_put

        from app.services.attachment_service import store_and_attach

        file = _make_upload_file()
        with pytest.raises(HTTPException) as exc_info:
            await store_and_attach(
                db_session,
                model=CompanyAttachment,
                fk_field="company_id",
                entity_label="Companies",
                entity_id=test_company.id,
                file=file,
                user=test_user,
            )

    assert exc_info.value.status_code == 502
    assert "onedrive" in exc_info.value.detail.lower()
    rows = db_session.query(CompanyAttachment).filter_by(company_id=test_company.id).all()
    assert rows == []


# ---------------------------------------------------------------------------
# Honest errors — unset drive_id + no user token → 401
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_drive_id_and_no_user_token_raises_401(db_session: Session, test_user: User):
    """When drive_id is empty and user has no Microsoft token, raises 401 with a clear
    message."""
    with (
        patch("app.services.attachment_service.settings") as mock_settings,
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value=None),
    ):
        mock_settings.datasheet_library_drive_id = ""

        from app.services.attachment_service import _store

        with pytest.raises(HTTPException) as exc_info:
            await _store(
                b"content",
                content_type="application/pdf",
                file_name="test.pdf",
                entity_label="Companies",
                entity_id=1,
                user=test_user,
                db=db_session,
            )

    assert exc_info.value.status_code == 401
    assert "microsoft account" in exc_info.value.detail.lower()


# ---------------------------------------------------------------------------
# Validation — oversize → 400
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_oversize_raises_400(db_session: Session, test_user: User, test_company: Company):
    """A file exceeding MAX_ATTACHMENT_BYTES raises HTTPException 400."""
    big_content = b"x" * (10 * 1024 * 1024 + 1)

    with patch("app.services.attachment_service.settings") as mock_settings:
        mock_settings.datasheet_library_drive_id = ""

        from app.services.attachment_service import store_and_attach

        file = _make_upload_file(content=big_content)
        with pytest.raises(HTTPException) as exc_info:
            await store_and_attach(
                db_session,
                model=CompanyAttachment,
                fk_field="company_id",
                entity_label="Companies",
                entity_id=test_company.id,
                file=file,
                user=test_user,
            )

    assert exc_info.value.status_code == 400
    assert "10 mb" in exc_info.value.detail.lower()


# ---------------------------------------------------------------------------
# Validation — bad extension → 400
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bad_extension_raises_400(db_session: Session, test_user: User, test_company: Company):
    """A file with a disallowed extension raises HTTPException 400 with the allowed
    list."""
    with patch("app.services.attachment_service.settings") as mock_settings:
        mock_settings.datasheet_library_drive_id = ""

        from app.services.attachment_service import store_and_attach

        file = _make_upload_file(filename="malware.exe", content_type="application/octet-stream")
        with pytest.raises(HTTPException) as exc_info:
            await store_and_attach(
                db_session,
                model=CompanyAttachment,
                fk_field="company_id",
                entity_label="Companies",
                entity_id=test_company.id,
                file=file,
                user=test_user,
            )

    assert exc_info.value.status_code == 400
    assert ".exe" in exc_info.value.detail
    assert ".pdf" in exc_info.value.detail  # allowed list present


# ---------------------------------------------------------------------------
# serialize — kind field
# ---------------------------------------------------------------------------


def test_serialize_kind_library(db_session: Session, test_user: User, test_company: Company):
    """Serialize() returns kind='library' when library_drive_id is set."""
    att = _make_attachment_row(
        db=db_session,
        company_id=test_company.id,
        user_id=test_user.id,
        library_drive_id=_DRIVE_ID,
    )
    from app.services.attachment_service import serialize

    data = serialize(att)
    assert data["kind"] == "library"
    assert data["id"] == att.id
    assert data["file_name"] == att.file_name


def test_serialize_kind_onedrive(db_session: Session, test_user: User, test_company: Company):
    """Serialize() returns kind='onedrive' when library_drive_id is NULL."""
    att = _make_attachment_row(
        db=db_session,
        company_id=test_company.id,
        user_id=test_user.id,
        library_drive_id=None,
        library_web_url=_ONEDRIVE_URL,
    )
    from app.services.attachment_service import serialize

    data = serialize(att)
    assert data["kind"] == "onedrive"
    assert data["web_url"] == _ONEDRIVE_URL


# ---------------------------------------------------------------------------
# open_attachment — library row streams, onedrive row redirects
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_attachment_library_streams(db_session: Session, test_user: User, test_company: Company):
    """open_attachment() returns a StreamingResponse for a company-library row."""
    att = _make_attachment_row(
        db=db_session,
        company_id=test_company.id,
        user_id=test_user.id,
        library_drive_id=_DRIVE_ID,
    )

    with patch(
        "app.services.datasheet_library.fetch_datasheet_bytes",
        new_callable=AsyncMock,
        return_value=b"PDF bytes",
    ):
        from app.services.attachment_service import open_attachment

        response = await open_attachment(att, test_user)

    assert isinstance(response, StreamingResponse)
    assert response.media_type == att.content_type
    assert "Content-Disposition" in response.headers
    assert att.file_name.split(".")[0] in response.headers["Content-Disposition"]
    # Consume the iterator and verify the streamed body equals the mocked bytes
    body = b"".join([chunk async for chunk in response.body_iterator])
    assert body == b"PDF bytes"


@pytest.mark.asyncio
async def test_open_attachment_onedrive_redirects(db_session: Session, test_user: User, test_company: Company):
    """open_attachment() returns a RedirectResponse for an OneDrive fallback row."""
    att = _make_attachment_row(
        db=db_session,
        company_id=test_company.id,
        user_id=test_user.id,
        library_drive_id=None,
        library_web_url=_ONEDRIVE_URL,
    )
    from app.services.attachment_service import open_attachment

    response = await open_attachment(att, test_user)

    assert isinstance(response, RedirectResponse)


@pytest.mark.asyncio
async def test_open_attachment_library_404_when_bytes_none(db_session: Session, test_user: User, test_company: Company):
    """open_attachment() raises 404 when library returns no bytes."""
    att = _make_attachment_row(
        db=db_session,
        company_id=test_company.id,
        user_id=test_user.id,
        library_drive_id=_DRIVE_ID,
    )

    with patch(
        "app.services.datasheet_library.fetch_datasheet_bytes",
        new_callable=AsyncMock,
        return_value=None,
    ):
        from app.services.attachment_service import open_attachment

        with pytest.raises(HTTPException) as exc_info:
            await open_attachment(att, test_user)

    assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# remove_attachment — warning path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remove_attachment_warning_on_cloud_delete_failure(
    db_session: Session, test_user: User, test_company: Company
):
    """remove_attachment() returns ok=True plus a warning when cloud delete fails, but
    the DB row IS removed."""
    att = _make_attachment_row(
        db=db_session,
        company_id=test_company.id,
        user_id=test_user.id,
        library_drive_id=_DRIVE_ID,
    )
    att_id = att.id

    mock_del_response = MagicMock()
    mock_del_response.status_code = 500
    mock_delete = AsyncMock(return_value=mock_del_response)

    with (
        patch("app.services.graph_app_auth.get_app_graph_token", new_callable=AsyncMock, return_value="app-token"),
        patch("app.http_client.http") as mock_http,
    ):
        mock_http.delete = mock_delete

        from app.services.attachment_service import remove_attachment

        result = await remove_attachment(db_session, att, test_user)

    assert result["ok"] is True
    assert "warning" in result
    # DB row should be gone
    assert db_session.get(CompanyAttachment, att_id) is None


@pytest.mark.asyncio
async def test_remove_attachment_onedrive_warning_on_failure(
    db_session: Session, test_user: User, test_company: Company
):
    """remove_attachment() warns (doesn't 500) when OneDrive delete call fails."""
    att = _make_attachment_row(
        db=db_session,
        company_id=test_company.id,
        user_id=test_user.id,
        library_drive_id=None,
        library_web_url=_ONEDRIVE_URL,
    )
    att_id = att.id

    mock_del_response = MagicMock()
    mock_del_response.status_code = 500
    mock_delete = AsyncMock(return_value=mock_del_response)

    with (
        patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="user-token"),
        patch("app.http_client.http") as mock_http,
    ):
        mock_http.delete = mock_delete

        from app.services.attachment_service import remove_attachment

        result = await remove_attachment(db_session, att, test_user)

    assert result["ok"] is True
    assert "warning" in result
    assert db_session.get(CompanyAttachment, att_id) is None


@pytest.mark.asyncio
async def test_remove_attachment_clean_no_warning(db_session: Session, test_user: User, test_company: Company):
    """remove_attachment() returns ok=True without warning when cloud delete
    succeeds."""
    att = _make_attachment_row(
        db=db_session,
        company_id=test_company.id,
        user_id=test_user.id,
        library_drive_id=_DRIVE_ID,
    )

    mock_del_response = MagicMock()
    mock_del_response.status_code = 204
    mock_delete = AsyncMock(return_value=mock_del_response)

    with (
        patch("app.services.graph_app_auth.get_app_graph_token", new_callable=AsyncMock, return_value="app-token"),
        patch("app.http_client.http") as mock_http,
    ):
        mock_http.delete = mock_delete

        from app.services.attachment_service import remove_attachment

        result = await remove_attachment(db_session, att, test_user)

    assert result == {"ok": True}
    assert "warning" not in result
