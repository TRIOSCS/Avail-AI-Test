"""tests/test_htmx_emails.py — Tests for HTMX email integration endpoints.

Verifies all 6 email HTMX endpoints return correct HTML partials,
handle errors gracefully, and interact with mocked services properly.

Called by: pytest
Depends on: conftest (client, db_session, test_user, test_requisition, test_vendor_card)
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── Sample data returned by mocked services ─────────────────────────

SAMPLE_THREADS = [
    {
        "conversation_id": "conv-abc-123",
        "subject": "RFQ for LM317T",
        "participants": ["vendor@arrow.com"],
        "message_count": 3,
        "last_message_date": "2026-03-10T14:30:00Z",
        "snippet": "We can offer 1000 pcs at $0.45 each.",
        "needs_response": True,
        "matched_via": "subject_token",
    },
    {
        "conversation_id": "conv-def-456",
        "subject": "Stock availability update",
        "participants": ["sales@digikey.com"],
        "message_count": 1,
        "last_message_date": "2026-03-08T09:00:00Z",
        "snippet": "Please see attached stock list.",
        "needs_response": False,
        "matched_via": "vendor_domain",
    },
]

SAMPLE_MESSAGES = [
    {
        "id": "msg-001",
        "from_name": "John Sales",
        "from_email": "john@arrow.com",
        "to": ["buyer@trioscs.com"],
        "subject": "RFQ for LM317T",
        "body_preview": "Hi, we have 1000 pcs available at $0.45.",
        "received_date": "2026-03-10T14:00:00Z",
        "direction": "received",
    },
    {
        "id": "msg-002",
        "from_name": "Test Buyer",
        "from_email": "buyer@trioscs.com",
        "to": ["john@arrow.com"],
        "subject": "Re: RFQ for LM317T",
        "body_preview": "Thanks, can you do $0.40 at 2000 pcs?",
        "received_date": "2026-03-10T14:15:00Z",
        "direction": "sent",
    },
    {
        "id": "msg-003",
        "from_name": "John Sales",
        "from_email": "john@arrow.com",
        "to": ["buyer@trioscs.com"],
        "subject": "Re: RFQ for LM317T",
        "body_preview": "Let me check with my manager and get back to you.",
        "received_date": "2026-03-10T14:30:00Z",
        "direction": "received",
    },
]

SAMPLE_SUMMARY = {
    "summary": "Vendor offered 1000 pcs of LM317T at $0.45. Buyer counter-offered at $0.40 for 2000 pcs. Vendor is checking with management.",
    "action_items": ["Follow up on vendor counter-offer", "Prepare alternative sourcing"],
    "sentiment": "positive",
}

SAMPLE_DASHBOARD = {
    "emails_scanned_7d": 142,
    "offers_detected_7d": 23,
    "stock_lists_7d": 8,
    "ooo_vendors": 2,
    "avg_response_hours": 4.5,
    "response_rate": 0.72,
    "top_vendors": [
        {"vendor_name": "Arrow Electronics", "email_health_score": 95, "response_rate": 0.88},
        {"vendor_name": "DigiKey", "email_health_score": 90, "response_rate": 0.75},
    ],
    "recent_offers": [],
    "pending_review": 5,
}


# ═══════════════════════════════════════════════════════════════════════
#  1. Requirement emails tab
# ═══════════════════════════════════════════════════════════════════════


@patch("app.routers.htmx.emails.email_threads.fetch_threads_for_requirement", new_callable=AsyncMock)
def test_requirement_emails_tab_success(mock_fetch, client, test_requisition):
    """GET /partials/requisitions/{id}/tab/emails returns thread table HTML."""
    req_id = test_requisition.requirements[0].id
    mock_fetch.return_value = SAMPLE_THREADS

    resp = client.get(f"/partials/requisitions/{req_id}/tab/emails")

    assert resp.status_code == 200
    html = resp.text
    assert "RFQ for LM317T" in html
    assert "Stock availability update" in html
    assert "vendor@arrow.com" in html
    assert "<table" in html
    assert "Needs reply" in html


@patch("app.routers.htmx.emails.email_threads.fetch_threads_for_requirement", new_callable=AsyncMock)
def test_requirement_emails_tab_empty(mock_fetch, client, test_requisition):
    """Empty thread list shows friendly empty state."""
    req_id = test_requisition.requirements[0].id
    mock_fetch.return_value = []

    resp = client.get(f"/partials/requisitions/{req_id}/tab/emails")

    assert resp.status_code == 200
    assert "No email threads found" in resp.text


@patch("app.routers.htmx.emails.email_threads.fetch_threads_for_requirement", new_callable=AsyncMock)
def test_requirement_emails_tab_error(mock_fetch, client, test_requisition):
    """Service failure returns friendly error HTML, not a 500."""
    req_id = test_requisition.requirements[0].id
    mock_fetch.side_effect = RuntimeError("Graph API timeout")

    resp = client.get(f"/partials/requisitions/{req_id}/tab/emails")

    assert resp.status_code == 200
    assert "Could not load emails" in resp.text


# ═══════════════════════════════════════════════════════════════════════
#  2. Thread messages
# ═══════════════════════════════════════════════════════════════════════


@patch("app.routers.htmx.emails.email_threads.fetch_thread_messages", new_callable=AsyncMock)
def test_thread_messages_success(mock_fetch, client):
    """GET /partials/emails/thread/{id} returns chat bubble HTML."""
    mock_fetch.return_value = SAMPLE_MESSAGES

    resp = client.get("/partials/emails/thread/conv-abc-123")

    assert resp.status_code == 200
    html = resp.text
    assert "John Sales" in html
    assert "1000 pcs available" in html
    assert "Send Reply" in html
    assert "Summarize with AI" in html
    # Reply form targets the last received message
    assert 'value="john@arrow.com"' in html
    assert 'value="msg-003"' in html


@patch("app.routers.htmx.emails.email_threads.fetch_thread_messages", new_callable=AsyncMock)
def test_thread_messages_empty(mock_fetch, client):
    """Empty thread shows empty state."""
    mock_fetch.return_value = []

    resp = client.get("/partials/emails/thread/conv-empty")

    assert resp.status_code == 200
    assert "No messages in this thread" in resp.text


@patch("app.routers.htmx.emails.email_threads.fetch_thread_messages", new_callable=AsyncMock)
def test_thread_messages_error(mock_fetch, client):
    """Service failure returns error HTML."""
    mock_fetch.side_effect = ConnectionError("Network error")

    resp = client.get("/partials/emails/thread/conv-fail")

    assert resp.status_code == 200
    assert "Could not load thread messages" in resp.text


# ═══════════════════════════════════════════════════════════════════════
#  3. Reply
# ═══════════════════════════════════════════════════════════════════════


@patch("app.routers.htmx.emails.email_threads.clear_cache")
@patch("app.utils.graph_client.GraphClient")
@patch("app.email_service._build_html_body", return_value="<p>Thanks!</p>")
def test_send_reply_success(mock_build, mock_gc_cls, mock_clear, client):
    """POST /partials/emails/reply returns success toast."""
    mock_gc = MagicMock()
    mock_gc.post_json = AsyncMock(return_value={})
    mock_gc_cls.return_value = mock_gc

    resp = client.post(
        "/partials/emails/reply",
        data={
            "conversation_id": "conv-abc-123",
            "message_id": "msg-003",
            "body": "Thanks for the quote!",
            "to_email": "john@arrow.com",
        },
    )

    assert resp.status_code == 200
    assert "Reply sent to john@arrow.com" in resp.text
    mock_clear.assert_called_once()


@patch("app.routers.htmx.emails.email_threads.clear_cache")
@patch("app.utils.graph_client.GraphClient")
@patch("app.email_service._build_html_body", return_value="<p>test</p>")
def test_send_reply_graph_error(mock_build, mock_gc_cls, mock_clear, client):
    """Graph API error returns error toast, not a 500."""
    mock_gc = MagicMock()
    mock_gc.post_json = AsyncMock(return_value={"error": {"code": "InvalidToken"}})
    mock_gc_cls.return_value = mock_gc

    resp = client.post(
        "/partials/emails/reply",
        data={
            "conversation_id": "conv-abc",
            "message_id": "msg-001",
            "body": "test",
            "to_email": "john@arrow.com",
        },
    )

    assert resp.status_code == 200
    assert "Failed to send reply" in resp.text


@patch("app.utils.graph_client.GraphClient")
@patch("app.email_service._build_html_body", return_value="<p>test</p>")
def test_send_reply_network_error(mock_build, mock_gc_cls, client):
    """Network exception returns error toast."""
    mock_gc = MagicMock()
    mock_gc.post_json = AsyncMock(side_effect=TimeoutError("Connection timed out"))
    mock_gc_cls.return_value = mock_gc

    resp = client.post(
        "/partials/emails/reply",
        data={
            "conversation_id": "conv-abc",
            "message_id": "msg-001",
            "body": "test",
            "to_email": "john@arrow.com",
        },
    )

    assert resp.status_code == 200
    assert "Failed to send reply" in resp.text


# ═══════════════════════════════════════════════════════════════════════
#  4. Vendor emails
# ═══════════════════════════════════════════════════════════════════════


@patch("app.routers.htmx.emails.email_threads.fetch_threads_for_vendor", new_callable=AsyncMock)
def test_vendor_emails_success(mock_fetch, client, test_vendor_card):
    """GET /partials/vendors/{id}/emails returns thread table."""
    mock_fetch.return_value = SAMPLE_THREADS

    resp = client.get(f"/partials/vendors/{test_vendor_card.id}/emails")

    assert resp.status_code == 200
    assert "RFQ for LM317T" in resp.text
    assert "<table" in resp.text


@patch("app.routers.htmx.emails.email_threads.fetch_threads_for_vendor", new_callable=AsyncMock)
def test_vendor_emails_empty(mock_fetch, client, test_vendor_card):
    """Empty vendor threads show vendor-specific empty message."""
    mock_fetch.return_value = []

    resp = client.get(f"/partials/vendors/{test_vendor_card.id}/emails")

    assert resp.status_code == 200
    assert "No email threads with this vendor" in resp.text


@patch("app.routers.htmx.emails.email_threads.fetch_threads_for_vendor", new_callable=AsyncMock)
def test_vendor_emails_error(mock_fetch, client, test_vendor_card):
    """Vendor email service failure returns error HTML."""
    mock_fetch.side_effect = OSError("DNS resolution failed")

    resp = client.get(f"/partials/vendors/{test_vendor_card.id}/emails")

    assert resp.status_code == 200
    assert "Could not load vendor emails" in resp.text


# ═══════════════════════════════════════════════════════════════════════
#  5. Thread summary
# ═══════════════════════════════════════════════════════════════════════


@patch("app.services.email_intelligence_service.summarize_thread", new_callable=AsyncMock)
def test_thread_summary_success(mock_summarize, client):
    """GET /partials/emails/thread/{id}/summary returns AI summary card."""
    mock_summarize.return_value = SAMPLE_SUMMARY

    resp = client.get("/partials/emails/thread/conv-abc-123/summary")

    assert resp.status_code == 200
    html = resp.text
    assert "AI Summary" in html
    assert "Vendor offered 1000 pcs" in html
    assert "Follow up on vendor counter-offer" in html
    assert "positive" in html


@patch("app.services.email_intelligence_service.summarize_thread", new_callable=AsyncMock)
def test_thread_summary_none(mock_summarize, client):
    """Null summary returns fallback message."""
    mock_summarize.return_value = None

    resp = client.get("/partials/emails/thread/conv-abc-123/summary")

    assert resp.status_code == 200
    assert "Could not generate a summary" in resp.text


@patch("app.services.email_intelligence_service.summarize_thread", new_callable=AsyncMock)
def test_thread_summary_error(mock_summarize, client):
    """AI service failure returns error HTML."""
    mock_summarize.side_effect = RuntimeError("AI service unavailable")

    resp = client.get("/partials/emails/thread/conv-abc-123/summary")

    assert resp.status_code == 200
    assert "Could not generate summary" in resp.text


# ═══════════════════════════════════════════════════════════════════════
#  6. Email intelligence dashboard
# ═══════════════════════════════════════════════════════════════════════


@patch("app.services.response_analytics.get_email_intelligence_dashboard")
def test_email_intelligence_dashboard_success(mock_dashboard, client):
    """GET /partials/email-intelligence returns stats dashboard."""
    mock_dashboard.return_value = SAMPLE_DASHBOARD

    resp = client.get("/partials/email-intelligence")

    assert resp.status_code == 200
    html = resp.text
    assert "Email Intelligence" in html
    assert "142" in html  # emails scanned
    assert "23" in html   # offers detected
    assert "4.5h" in html  # avg response
    assert "72%" in html   # response rate
    assert "Arrow Electronics" in html
    assert "Pending Review" in html


@patch("app.services.response_analytics.get_email_intelligence_dashboard")
def test_email_intelligence_dashboard_error(mock_dashboard, client):
    """Dashboard service failure returns error HTML."""
    mock_dashboard.side_effect = Exception("DB connection lost")

    resp = client.get("/partials/email-intelligence")

    assert resp.status_code == 200
    assert "Could not load email intelligence" in resp.text


@patch("app.services.response_analytics.get_email_intelligence_dashboard")
def test_email_intelligence_dashboard_custom_days(mock_dashboard, client):
    """Custom days parameter is passed through to service."""
    mock_dashboard.return_value = SAMPLE_DASHBOARD

    resp = client.get("/partials/email-intelligence?days=30")

    assert resp.status_code == 200
    mock_dashboard.assert_called_once()
    call_args = mock_dashboard.call_args
    assert call_args[1]["days"] == 30 or call_args[0][2] == 30
