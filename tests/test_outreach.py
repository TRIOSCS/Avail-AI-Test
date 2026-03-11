"""Tests for the outreach router — POST /api/outreach/send.

Covers: validation, personalisation, send success/failure, activity logging.
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def outreach_payload():
    return {
        "recipients": [
            {"name": "Holly Fortson", "email": "holly@waldom.com", "company": "Waldom Electronics"},
            {"name": "Grant Moore", "email": "gmoore@bluestarinc.com", "company": "BlueStar US"},
        ],
        "subject": "Test Outreach",
        "body": "Hi,\n\nWe have inventory available.\n\nThanks",
    }


def test_send_outreach_success(client: TestClient, outreach_payload):
    """All recipients succeed — returns sent list."""
    with patch("app.routers.outreach.GraphClient") as MockGC:
        mock_gc = MockGC.return_value
        mock_gc.post_json = AsyncMock(return_value={})

        resp = client.post("/api/outreach/send", json=outreach_payload)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["sent"]) == 2
        assert len(data["failed"]) == 0
        assert data["sent"][0]["email"] == "holly@waldom.com"
        assert data["sent"][1]["company"] == "BlueStar US"
        assert mock_gc.post_json.call_count == 2


def test_send_outreach_personalises_greeting(client: TestClient):
    """Body starting with 'Hi,' gets personalised to 'Hi {first_name},'."""
    with patch("app.routers.outreach.GraphClient") as MockGC:
        mock_gc = MockGC.return_value
        mock_gc.post_json = AsyncMock(return_value={})

        payload = {
            "recipients": [{"name": "Holly Fortson", "email": "holly@waldom.com", "company": ""}],
            "subject": "Test",
            "body": "Hi,\n\nTest body.",
        }
        resp = client.post("/api/outreach/send", json=payload)
        assert resp.status_code == 200

        call_args = mock_gc.post_json.call_args
        html_content = call_args[0][1]["message"]["body"]["content"]
        assert "Hi Holly," in html_content


def test_send_outreach_partial_failure(client: TestClient, outreach_payload):
    """One send fails — returns in failed list, other in sent."""
    with patch("app.routers.outreach.GraphClient") as MockGC:
        mock_gc = MockGC.return_value
        mock_gc.post_json = AsyncMock(side_effect=[{}, Exception("timeout")])

        resp = client.post("/api/outreach/send", json=outreach_payload)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["sent"]) == 1
        assert len(data["failed"]) == 1
        assert data["failed"][0]["email"] == "gmoore@bluestarinc.com"


def test_send_outreach_no_recipients(client: TestClient):
    """Empty recipients list returns 400."""
    resp = client.post(
        "/api/outreach/send",
        json={
            "recipients": [],
            "subject": "Test",
            "body": "Hi,\n\nTest.",
        },
    )
    assert resp.status_code == 400


def test_send_outreach_too_many_recipients(client: TestClient):
    """More than 50 recipients returns 400."""
    recipients = [{"name": f"R{i}", "email": f"r{i}@test.com", "company": ""} for i in range(51)]
    resp = client.post(
        "/api/outreach/send",
        json={
            "recipients": recipients,
            "subject": "Test",
            "body": "Hi,\n\nTest.",
        },
    )
    assert resp.status_code == 400


def test_send_outreach_graph_error_response(client: TestClient):
    """Graph returns error dict — treated as failure."""
    with patch("app.routers.outreach.GraphClient") as MockGC:
        mock_gc = MockGC.return_value
        mock_gc.post_json = AsyncMock(return_value={"error": 403, "detail": "Forbidden"})

        resp = client.post(
            "/api/outreach/send",
            json={
                "recipients": [{"name": "Test", "email": "test@example.com", "company": "Co"}],
                "subject": "Test",
                "body": "Hi,\n\nTest.",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["failed"]) == 1
        assert "403" in data["failed"][0]["error"]


def test_send_outreach_requires_auth(outreach_payload):
    """Unauthenticated request returns 401."""
    from app.main import app

    unauth_client = TestClient(app)
    resp = unauth_client.post("/api/outreach/send", json=outreach_payload)
    assert resp.status_code == 401
