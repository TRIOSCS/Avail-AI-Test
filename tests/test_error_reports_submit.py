"""test_error_reports_submit.py — Tests for submit_trouble_ticket endpoint.

Covers lines 187-255 (JSON body path, form-encoded path, validation errors).

Called by: pytest
Depends on: app/routers/error_reports.py, tests/conftest.py
"""

import os

os.environ["TESTING"] = "1"

from unittest.mock import AsyncMock, patch

import pytest


def _post_json(client, body):
    """POST a JSON ticket with _generate_ai_summary stubbed out."""
    with patch(
        "app.routers.error_reports._generate_ai_summary",
        new_callable=AsyncMock,
    ):
        return client.post(
            "/api/trouble-tickets/submit",
            json=body,
            headers={"Content-Type": "application/json"},
        )


def test_submit_ticket_json_success(client):
    """POST /api/trouble-tickets/submit with JSON body creates a ticket."""
    resp = _post_json(
        client,
        {
            "description": "The search button is broken",
            "page_url": "/v2/sightings",
            "user_agent": "Mozilla/5.0",
            "viewport": {"width": 1920, "height": 1080},
        },
    )

    assert resp.status_code == 200
    assert "submitted" in resp.text.lower() or "TT-" in resp.text


def test_submit_ticket_json_missing_description(client):
    """JSON body with empty description returns 422 HTML fragment."""
    resp = client.post(
        "/api/trouble-tickets/submit",
        json={"description": "", "page_url": "/v2/sightings"},
        headers={"Content-Type": "application/json"},
    )

    assert resp.status_code == 422
    assert "describe" in resp.text.lower() or "problem" in resp.text.lower()


def test_submit_ticket_json_description_too_long(client):
    """Description exceeding MAX_MESSAGE_LEN returns 422."""
    resp = client.post(
        "/api/trouble-tickets/submit",
        json={"description": "x" * 6000},
        headers={"Content-Type": "application/json"},
    )

    assert resp.status_code == 422
    assert "long" in resp.text.lower() or "max" in resp.text.lower()


def test_submit_ticket_json_invalid_body(client):
    """Malformed JSON body returns 422 HTML fragment."""
    resp = client.post(
        "/api/trouble-tickets/submit",
        content=b"not-valid-json{{{",
        headers={"Content-Type": "application/json"},
    )

    assert resp.status_code == 422
    assert "invalid" in resp.text.lower()


def test_submit_ticket_form_encoded(client):
    """POST with form-encoded data (legacy path) creates a ticket."""
    with patch(
        "app.routers.error_reports._generate_ai_summary",
        new_callable=AsyncMock,
    ):
        resp = client.post(
            "/api/trouble-tickets/submit",
            data={"message": "Legacy form submission works", "current_url": "/v2/"},
        )

    assert resp.status_code == 200
    assert "submitted" in resp.text.lower() or "TT-" in resp.text


def test_submit_ticket_form_missing_message(client):
    """Form-encoded with empty message returns 422."""
    resp = client.post(
        "/api/trouble-tickets/submit",
        data={"message": "", "current_url": "/v2/"},
    )

    assert resp.status_code == 422


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(
            {
                "description": "Modal won't close on mobile",
                "user_agent": "iPhone Safari/16",
                "viewport": {"width": 390, "height": 844},
            },
            id="ua_and_viewport",
        ),
        pytest.param(
            {
                "description": "API call returned 500",
                "network_log": '[{"url": "/api/x", "status": 500}]',
            },
            id="network_log_json",
        ),
        pytest.param(
            {
                "description": "Some error happened",
                "network_log": "not-valid-json{{",
            },
            id="invalid_network_log",
        ),
    ],
)
def test_submit_ticket_optional_fields_return_200(client, body):
    """JSON bodies with optional browser_info / network_log fields create a ticket.

    Covers user_agent+viewport storage, valid network_log parsing, and graceful handling
    of invalid network_log JSON (must not 500).
    """
    resp = _post_json(client, body)

    assert resp.status_code == 200
    assert "submitted" in resp.text.lower() or "TT-" in resp.text


def test_submit_ticket_with_screenshot(client):
    """JSON body with a small valid base64 screenshot saves it."""
    import base64

    small_png = base64.b64encode(b"\x89PNG\r\n" + b"\x00" * 50).decode()

    with (
        patch(
            "app.routers.error_reports._generate_ai_summary",
            new_callable=AsyncMock,
        ),
        patch("app.routers.error_reports._save_screenshot", return_value="/tmp/TT-1.png") as mock_save,
    ):
        resp = client.post(
            "/api/trouble-tickets/submit",
            json={
                "description": "Screenshot attached",
                "screenshot": small_png,
            },
            headers={"Content-Type": "application/json"},
        )

    assert resp.status_code == 200
    assert "submitted" in resp.text.lower() or "TT-" in resp.text
    mock_save.assert_called_once()
    assert mock_save.call_args.args[1] == small_png


def test_submit_ticket_screenshot_write_failure_returns_500(client):
    """P2.6: _save_screenshot now runs via asyncio.to_thread — an OSError raised on that
    worker thread must still propagate back to the route's (PermissionError, OSError)
    except clause (TT-0002), not be swallowed."""
    import base64

    small_png = base64.b64encode(b"\x89PNG\r\n" + b"\x00" * 50).decode()

    with (
        patch(
            "app.routers.error_reports._generate_ai_summary",
            new_callable=AsyncMock,
        ),
        patch("app.routers.error_reports._save_screenshot", side_effect=OSError("disk full")),
    ):
        resp = client.post(
            "/api/trouble-tickets/submit",
            json={
                "description": "Screenshot attached",
                "screenshot": small_png,
            },
            headers={"Content-Type": "application/json"},
        )

    assert resp.status_code == 500
    assert "not writable" in resp.json()["error"]


def test_submit_ticket_db_error_returns_500(client):
    """When DB raises an exception, returns 500 HTML fragment."""
    with patch(
        "app.routers.error_reports._create_ticket",
        side_effect=Exception("DB is down"),
    ):
        resp = client.post(
            "/api/trouble-tickets/submit",
            json={"description": "Something broke"},
            headers={"Content-Type": "application/json"},
        )

    assert resp.status_code == 500
    assert "wrong" in resp.text.lower() or "error" in resp.text.lower()
