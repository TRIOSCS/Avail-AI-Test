"""
tests/test_routers_emails.py -- Tests for routers/emails.py

Covers: requirement emails, thread messages, vendor emails,
and reply sending with M365 token handling.

Called by: pytest
Depends on: app/routers/emails.py, conftest.py
"""

from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException

# ── Requirement emails ───────────────────────────────────────────────


@patch("app.routers.emails.require_fresh_token", new_callable=AsyncMock, return_value="mock-token")
@patch("app.routers.emails.fetch_threads_for_requirement", new_callable=AsyncMock,
       return_value=[{"conversation_id": "c1", "subject": "RFQ", "participants": [],
                      "message_count": 2, "last_message_date": None, "snippet": "",
                      "needs_response": False, "matched_via": "subject_token"}])
def test_requirement_emails_success(mock_fetch, mock_token, client):
    """Returns threads for requirement."""
    resp = client.get("/api/requirements/1/emails")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["threads"]) == 1
    assert data["error"] is None


@patch("app.routers.emails.require_fresh_token", new_callable=AsyncMock,
       side_effect=HTTPException(401, "expired"))
def test_requirement_emails_no_token(mock_token, client):
    """Missing M365 token -> error message in response."""
    resp = client.get("/api/requirements/1/emails")
    assert resp.status_code == 200
    data = resp.json()
    assert data["threads"] == []
    assert data["error"] is not None


@patch("app.routers.emails.require_fresh_token", new_callable=AsyncMock, return_value="mock-token")
@patch("app.routers.emails.fetch_threads_for_requirement", new_callable=AsyncMock,
       side_effect=Exception("Graph error"))
def test_requirement_emails_service_error(mock_fetch, mock_token, client):
    """Service throws -> error in response (not 500)."""
    resp = client.get("/api/requirements/1/emails")
    assert resp.status_code == 200
    data = resp.json()
    assert data["threads"] == []
    assert data["error"] is not None


# ── Thread messages ──────────────────────────────────────────────────


@patch("app.routers.emails.require_fresh_token", new_callable=AsyncMock, return_value="mock-token")
@patch("app.routers.emails.fetch_thread_messages", new_callable=AsyncMock,
       return_value=[{"id": "m1", "from_name": "Vendor", "from_email": "v@v.com",
                      "to": [], "subject": "RE: RFQ", "body_preview": "Hello",
                      "received_date": None, "direction": "received"}])
def test_thread_messages_success(mock_fetch, mock_token, client):
    """Returns messages in conversation."""
    resp = client.get("/api/emails/thread/conv-123")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["messages"]) == 1


@patch("app.routers.emails.require_fresh_token", new_callable=AsyncMock, return_value="mock-token")
@patch("app.routers.emails.fetch_thread_messages", new_callable=AsyncMock, return_value=[])
def test_thread_messages_empty(mock_fetch, mock_token, client):
    """No messages -> empty list."""
    resp = client.get("/api/emails/thread/conv-empty")
    assert resp.status_code == 200
    data = resp.json()
    assert data["messages"] == []


@patch("app.routers.emails.require_fresh_token", new_callable=AsyncMock,
       side_effect=HTTPException(401, "expired"))
def test_thread_messages_no_token(mock_token, client):
    """Missing M365 token -> error."""
    resp = client.get("/api/emails/thread/conv-123")
    assert resp.status_code == 200
    data = resp.json()
    assert data["messages"] == []
    assert data["error"] is not None


# ── Vendor emails ────────────────────────────────────────────────────


@patch("app.routers.emails.require_fresh_token", new_callable=AsyncMock, return_value="mock-token")
@patch("app.routers.emails.fetch_threads_for_vendor", new_callable=AsyncMock,
       return_value=[{"conversation_id": "c2", "subject": "Stock list",
                      "participants": [], "message_count": 3, "last_message_date": None,
                      "snippet": "", "needs_response": True, "matched_via": "vendor_domain"}])
def test_vendor_emails_success(mock_fetch, mock_token, client):
    """Returns threads for vendor."""
    resp = client.get("/api/vendors/1/emails")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["threads"]) == 1


@patch("app.routers.emails.require_fresh_token", new_callable=AsyncMock,
       side_effect=HTTPException(401, "expired"))
def test_vendor_emails_no_token(mock_token, client):
    """Missing M365 token -> error."""
    resp = client.get("/api/vendors/1/emails")
    assert resp.status_code == 200
    data = resp.json()
    assert data["threads"] == []
    assert data["error"] is not None


# ── Reply ────────────────────────────────────────────────────────────


@patch("app.services.email_threads.clear_cache")
@patch("app.utils.graph_client.GraphClient")
@patch("app.email_service._build_html_body", return_value="<p>Reply</p>")
@patch("app.routers.emails.require_fresh_token", new_callable=AsyncMock, return_value="mock-token")
def test_reply_success(mock_token, mock_html, mock_gc_cls, mock_cache, client):
    """Send reply -> 200."""
    mock_gc = MagicMock()
    mock_gc.post_json = AsyncMock(return_value={})
    mock_gc_cls.return_value = mock_gc

    resp = client.post("/api/emails/reply", json={
        "conversation_id": "conv-1",
        "to": "vendor@example.com",
        "subject": "RE: RFQ",
        "body": "Thanks for the quote",
    })
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


@patch("app.routers.emails.require_fresh_token", new_callable=AsyncMock,
       side_effect=HTTPException(401, "expired"))
def test_reply_no_m365(mock_token, client):
    """No M365 token -> 401."""
    resp = client.post("/api/emails/reply", json={
        "conversation_id": "conv-1",
        "to": "vendor@example.com",
        "subject": "RE: RFQ",
        "body": "Thanks",
    })
    assert resp.status_code == 401


@patch("app.services.email_threads.clear_cache")
@patch("app.utils.graph_client.GraphClient")
@patch("app.email_service._build_html_body", return_value="<p>Reply</p>")
@patch("app.routers.emails.require_fresh_token", new_callable=AsyncMock, return_value="mock-token")
def test_reply_graph_failure(mock_token, mock_html, mock_gc_cls, mock_cache, client):
    """Graph API error -> 502."""
    mock_gc = MagicMock()
    mock_gc.post_json = AsyncMock(return_value={"error": 500, "detail": "Internal"})
    mock_gc_cls.return_value = mock_gc

    resp = client.post("/api/emails/reply", json={
        "conversation_id": "conv-1",
        "to": "vendor@example.com",
        "subject": "RE: RFQ",
        "body": "Hello",
    })
    assert resp.status_code == 502


def test_reply_empty_body(client):
    """Empty body -> validation error (422)."""
    resp = client.post("/api/emails/reply", json={
        "conversation_id": "conv-1",
        "to": "vendor@example.com",
        "subject": "RE: RFQ",
    })
    assert resp.status_code == 422
