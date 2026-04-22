"""test_error_reports_async_direct.py — Direct async invocation of submit_trouble_ticket.

Covers lines 187-255 which are inside the async view function body and cannot be
traced through TestClient (which uses greenlet/thread concurrency bridge).

By calling the async function directly in an asyncio test, coverage.py traces
the async code natively.

Called by: pytest (asyncio_mode = auto)
Depends on: app/routers/error_reports.py, conftest.py (db_session, test_user)
"""

import os

os.environ["TESTING"] = "1"

import base64
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import BackgroundTasks
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.testclient import TestClient as StarletteTestClient

from app.models import User
from app.models.trouble_ticket import TroubleTicket
from app.routers.error_reports import submit_trouble_ticket


def _make_mock_request(content_type: str, body: bytes) -> MagicMock:
    """Create a mock Starlette Request with the given content-type and body."""
    mock_req = MagicMock(spec=Request)
    mock_req.headers = {"content-type": content_type}

    async def _json():
        return json.loads(body)

    async def _form():
        from urllib.parse import parse_qs

        parsed = parse_qs(body.decode("utf-8"))
        form_mock = MagicMock()
        form_mock.get = lambda key, default=None: (parsed.get(key, [default])[0])
        return form_mock

    mock_req.json = _json
    mock_req.form = _form
    return mock_req


# ── JSON path (lines 187-193) ────────────────────────────────────────────────


async def test_submit_json_path_basic(db_session: Session, test_user: User):
    """Covers lines 187-193: JSON body parsing branch."""
    mock_req = _make_mock_request(
        "application/json",
        b'{"description": "Button broken", "page_url": "/v2/search"}',
    )
    bg_tasks = BackgroundTasks()

    resp = await submit_trouble_ticket(
        request=mock_req,
        background_tasks=bg_tasks,
        user=test_user,
        db=db_session,
    )
    assert resp.status_code == 200
    assert "Report submitted" in resp.body.decode()


async def test_submit_json_with_ua_and_viewport(db_session: Session, test_user: User):
    """Covers lines 190-192, 217-218: user_agent + viewport → browser_info."""
    mock_req = _make_mock_request(
        "application/json",
        json.dumps({
            "description": "Chrome bug",
            "user_agent": "Mozilla/5.0 Chrome/120",
            "viewport": "1920x1080",
        }).encode(),
    )
    bg_tasks = BackgroundTasks()

    resp = await submit_trouble_ticket(
        request=mock_req,
        background_tasks=bg_tasks,
        user=test_user,
        db=db_session,
    )
    assert resp.status_code == 200

    ticket = db_session.query(TroubleTicket).filter(
        TroubleTicket.description == "Chrome bug"
    ).first()
    assert ticket is not None
    assert ticket.browser_info is not None
    info = json.loads(ticket.browser_info)
    assert info["user_agent"] == "Mozilla/5.0 Chrome/120"
    assert info["viewport"] == "1920x1080"


async def test_submit_json_with_network_log_string(db_session: Session, test_user: User):
    """Covers lines 221-225: network_log JSON string is parsed."""
    network_log = json.dumps([{"url": "/api/search", "status": 500}])
    mock_req = _make_mock_request(
        "application/json",
        json.dumps({
            "description": "Network error test",
            "network_log": network_log,
        }).encode(),
    )
    bg_tasks = BackgroundTasks()

    resp = await submit_trouble_ticket(
        request=mock_req,
        background_tasks=bg_tasks,
        user=test_user,
        db=db_session,
    )
    assert resp.status_code == 200


async def test_submit_json_with_invalid_network_log(db_session: Session, test_user: User):
    """Covers line 225: invalid network_log JSON → network_errors = None."""
    mock_req = _make_mock_request(
        "application/json",
        json.dumps({
            "description": "Bad network log",
            "network_log": "{{{not json}}}",
        }).encode(),
    )
    bg_tasks = BackgroundTasks()

    resp = await submit_trouble_ticket(
        request=mock_req,
        background_tasks=bg_tasks,
        user=test_user,
        db=db_session,
    )
    assert resp.status_code == 200


async def test_submit_json_with_dict_network_log(db_session: Session, test_user: User):
    """Covers line 223 (isinstance branch): network_log as list → stored directly."""
    mock_req = _make_mock_request(
        "application/json",
        json.dumps({
            "description": "List network log",
            "network_log": [{"url": "/api/test", "status": 200}],
        }).encode(),
    )
    bg_tasks = BackgroundTasks()

    resp = await submit_trouble_ticket(
        request=mock_req,
        background_tasks=bg_tasks,
        user=test_user,
        db=db_session,
    )
    assert resp.status_code == 200


async def test_submit_json_empty_description_returns_422(db_session: Session, test_user: User):
    """Covers lines 205-208: empty description → 422 HTML."""
    mock_req = _make_mock_request(
        "application/json",
        b'{"description": "   "}',
    )
    bg_tasks = BackgroundTasks()

    resp = await submit_trouble_ticket(
        request=mock_req,
        background_tasks=bg_tasks,
        user=test_user,
        db=db_session,
    )
    assert resp.status_code == 422
    assert "describe" in resp.body.decode().lower() or "problem" in resp.body.decode().lower()


async def test_submit_json_too_long_description_returns_422(db_session: Session, test_user: User):
    """Covers lines 210-213: over MAX_MESSAGE_LEN → 422 HTML."""
    from app.routers.error_reports import MAX_MESSAGE_LEN

    mock_req = _make_mock_request(
        "application/json",
        json.dumps({"description": "X" * (MAX_MESSAGE_LEN + 1)}).encode(),
    )
    bg_tasks = BackgroundTasks()

    resp = await submit_trouble_ticket(
        request=mock_req,
        background_tasks=bg_tasks,
        user=test_user,
        db=db_session,
    )
    assert resp.status_code == 422
    assert "too long" in resp.body.decode().lower() or "max" in resp.body.decode().lower()


async def test_submit_json_with_screenshot_saves_path(
    db_session: Session, test_user: User, tmp_path
):
    """Covers lines 240-244: screenshot_b64 → saved to disk → path stored."""
    import app.routers.error_reports as er_mod

    er_mod._upload_dir_ready = False
    original_dir = er_mod.UPLOAD_DIR
    er_mod.UPLOAD_DIR = str(tmp_path)

    try:
        png_bytes = b"\x89PNG\r\n\x1a\n" + b"fake image data"
        b64 = base64.b64encode(png_bytes).decode()

        mock_req = _make_mock_request(
            "application/json",
            json.dumps({"description": "Screenshot test", "screenshot": b64}).encode(),
        )
        bg_tasks = BackgroundTasks()

        resp = await submit_trouble_ticket(
            request=mock_req,
            background_tasks=bg_tasks,
            user=test_user,
            db=db_session,
        )
        assert resp.status_code == 200

        ticket = db_session.query(TroubleTicket).filter(
            TroubleTicket.description == "Screenshot test"
        ).first()
        assert ticket is not None
        assert ticket.screenshot_path is not None
    finally:
        er_mod.UPLOAD_DIR = original_dir


async def test_submit_json_screenshot_bad_b64_no_path(
    db_session: Session, test_user: User, tmp_path
):
    """Covers lines 240-244 (path=None branch): bad b64 → no path stored."""
    import app.routers.error_reports as er_mod

    er_mod._upload_dir_ready = False
    original_dir = er_mod.UPLOAD_DIR
    er_mod.UPLOAD_DIR = str(tmp_path)

    try:
        # Invalid base64 → _save_screenshot returns None
        mock_req = _make_mock_request(
            "application/json",
            json.dumps({"description": "Bad screenshot test", "screenshot": "invalid!!!"}).encode(),
        )
        bg_tasks = BackgroundTasks()

        resp = await submit_trouble_ticket(
            request=mock_req,
            background_tasks=bg_tasks,
            user=test_user,
            db=db_session,
        )
        assert resp.status_code == 200
    finally:
        er_mod.UPLOAD_DIR = original_dir


# ── Form-encoded path (lines 197-203) ────────────────────────────────────────


async def test_submit_form_path_basic(db_session: Session, test_user: User):
    """Covers lines 197-203: form-encoded body parsing branch."""
    mock_req = _make_mock_request(
        "application/x-www-form-urlencoded",
        b"message=Form+encoded+ticket&current_url=%2Fv2%2Fsearch",
    )
    bg_tasks = BackgroundTasks()

    resp = await submit_trouble_ticket(
        request=mock_req,
        background_tasks=bg_tasks,
        user=test_user,
        db=db_session,
    )
    assert resp.status_code == 200
    assert "Report submitted" in resp.body.decode()


async def test_submit_form_path_empty_message(db_session: Session, test_user: User):
    """Covers lines 197-208: form with empty message → 422."""
    mock_req = _make_mock_request(
        "application/x-www-form-urlencoded",
        b"message=+",
    )
    bg_tasks = BackgroundTasks()

    resp = await submit_trouble_ticket(
        request=mock_req,
        background_tasks=bg_tasks,
        user=test_user,
        db=db_session,
    )
    assert resp.status_code == 422


# ── Exception handling (lines 245-251) ─────────────────────────────────────


async def test_submit_db_error_returns_500_html(db_session: Session, test_user: User):
    """Covers lines 245-251: exception during _create_ticket → 500 HTML."""
    mock_req = _make_mock_request(
        "application/json",
        b'{"description": "DB error test"}',
    )
    bg_tasks = BackgroundTasks()

    with patch("app.routers.error_reports._create_ticket", side_effect=Exception("DB down")):
        resp = await submit_trouble_ticket(
            request=mock_req,
            background_tasks=bg_tasks,
            user=test_user,
            db=db_session,
        )
    assert resp.status_code == 500
    assert "went wrong" in resp.body.decode().lower()


# ── Invalid JSON body (lines 181-186) ────────────────────────────────────────


async def test_submit_json_invalid_body_returns_422(db_session: Session, test_user: User):
    """Covers lines 182-186: invalid JSON body → 422 HTML."""
    mock_req = MagicMock(spec=Request)
    mock_req.headers = {"content-type": "application/json"}

    async def _bad_json():
        raise ValueError("invalid json")

    mock_req.json = _bad_json
    bg_tasks = BackgroundTasks()

    resp = await submit_trouble_ticket(
        request=mock_req,
        background_tasks=bg_tasks,
        user=test_user,
        db=db_session,
    )
    assert resp.status_code == 422
    assert "Invalid request" in resp.body.decode()


# ── Background task (line 253) ────────────────────────────────────────────────


async def test_submit_adds_background_task(db_session: Session, test_user: User):
    """Covers line 253: background_tasks.add_task called after successful create."""
    mock_req = _make_mock_request(
        "application/json",
        b'{"description": "Background task test"}',
    )

    bg_tasks = MagicMock(spec=BackgroundTasks)

    resp = await submit_trouble_ticket(
        request=mock_req,
        background_tasks=bg_tasks,
        user=test_user,
        db=db_session,
    )
    assert resp.status_code == 200
    bg_tasks.add_task.assert_called_once()


# ── Only UA (no viewport) sets browser_info (line 217) ───────────────────────


async def test_submit_json_only_ua_sets_browser_info(db_session: Session, test_user: User):
    """Covers line 217: ua without viewport still creates browser_info."""
    mock_req = _make_mock_request(
        "application/json",
        json.dumps({
            "description": "UA only",
            "user_agent": "Firefox/122",
        }).encode(),
    )
    bg_tasks = BackgroundTasks()

    resp = await submit_trouble_ticket(
        request=mock_req,
        background_tasks=bg_tasks,
        user=test_user,
        db=db_session,
    )
    assert resp.status_code == 200

    ticket = db_session.query(TroubleTicket).filter(
        TroubleTicket.description == "UA only"
    ).first()
    assert ticket is not None
    assert ticket.browser_info is not None


async def test_submit_json_with_error_log(db_session: Session, test_user: User):
    """Covers line 192: error_log field is read from JSON body."""
    mock_req = _make_mock_request(
        "application/json",
        json.dumps({
            "description": "JS error test",
            "error_log": "[{\"msg\":\"TypeError\",\"ts\":\"2026-01-01\"}]",
        }).encode(),
    )
    bg_tasks = BackgroundTasks()

    resp = await submit_trouble_ticket(
        request=mock_req,
        background_tasks=bg_tasks,
        user=test_user,
        db=db_session,
    )
    assert resp.status_code == 200

    ticket = db_session.query(TroubleTicket).filter(
        TroubleTicket.description == "JS error test"
    ).first()
    assert ticket is not None
    assert ticket.console_errors is not None
