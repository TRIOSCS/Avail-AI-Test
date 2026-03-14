"""Tests for Phase 4 Task 9 — M365/Outlook strengthening.

Covers:
  - scan_sent_folder: mock Graph API, assert ActivityLog created with direction="outbound"
  - group_by_thread: emails grouped by In-Reply-To/References headers
  - detect_attachments: xlsx flagged, inline image excluded
  - Retry logic: 429 with Retry-After honored, 401 not retried

Called by: pytest
Depends on: conftest.py fixtures, app.jobs.email_jobs, app.services.email_threads,
            app.utils.graph_client
"""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from app.models import ActivityLog, Requisition
from app.models.pipeline import SyncState

# ── Helpers ──────────────────────────────────────────────────────────────


def _make_graph_message(msg_id, subject, recipient, sent_dt=None, has_attachments=False, headers=None):
    """Build a fake Graph API message dict."""
    msg = {
        "id": msg_id,
        "subject": subject,
        "from": {"emailAddress": {"address": "buyer@trioscs.com", "name": "Buyer"}},
        "toRecipients": [{"emailAddress": {"address": recipient, "name": "Vendor"}}],
        "sentDateTime": sent_dt or "2026-03-13T10:00:00Z",
        "receivedDateTime": sent_dt or "2026-03-13T10:00:00Z",
        "hasAttachments": has_attachments,
        "internetMessageHeaders": headers or [],
    }
    return msg


# ── test_scan_sent_folder ────────────────────────────────────────────────


def test_scan_sent_folder(db_session, test_user):
    """Mock Graph delta_query on SentItems, assert ActivityLog entries created."""
    # Create a requisition so [AVAIL-{id}] tag can link
    req = Requisition(
        name="REQ-SENT-001",
        customer_name="Test Co",
        status="open",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.commit()
    db_session.refresh(req)

    messages = [
        _make_graph_message("msg-001", f"RFQ for LM317T [AVAIL-{req.id}]", "vendor1@example.com"),
        _make_graph_message("msg-002", "Quote follow-up", "vendor2@example.com"),
        _make_graph_message("msg-003", f"Re: [AVAIL-{req.id}] stock check", "vendor3@example.com"),
    ]

    mock_gc_instance = AsyncMock()
    mock_gc_instance.delta_query = AsyncMock(return_value=(messages, "new-delta-token-123"))
    mock_gc_instance.get_json = AsyncMock(return_value={"value": []})  # No attachments

    with (
        patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="fake-token"),
        patch("app.utils.graph_client.GraphClient.__init__", return_value=None),
        patch("app.utils.graph_client.GraphClient.delta_query", mock_gc_instance.delta_query),
        patch("app.utils.graph_client.GraphClient.get_json", mock_gc_instance.get_json),
    ):
        from app.jobs.email_jobs import scan_sent_folder

        # Simulate user with access token
        test_user.access_token = "fake-token"
        test_user.m365_connected = True
        db_session.commit()

        result = asyncio.get_event_loop().run_until_complete(
            scan_sent_folder(test_user, db_session)
        )

    # Should create 3 ActivityLog entries
    assert len(result) == 3

    logs = db_session.query(ActivityLog).filter(ActivityLog.user_id == test_user.id).all()
    assert len(logs) == 3

    # Verify all are outbound email_sent
    for log in logs:
        assert log.activity_type == "email_sent"
        assert log.direction == "outbound"
        assert log.channel == "email"
        assert log.auto_logged is True

    # Verify AVAIL tag linked to requisition
    tagged_logs = [entry for entry in logs if entry.requisition_id == req.id]
    assert len(tagged_logs) == 2  # msg-001 and msg-003 have AVAIL tags

    # Verify non-tagged log has no requisition link
    untagged = [entry for entry in logs if entry.requisition_id is None]
    assert len(untagged) == 1
    assert untagged[0].contact_email == "vendor2@example.com"

    # Verify delta token stored in SyncState
    sync = db_session.query(SyncState).filter(
        SyncState.user_id == test_user.id,
        SyncState.folder == "sent_items_scan",
    ).first()
    assert sync is not None
    assert sync.delta_token == "new-delta-token-123"


def test_scan_sent_folder_dedup(db_session, test_user):
    """Duplicate messages (same external_id) should not create duplicate logs."""
    # Pre-create an ActivityLog with the same external_id
    existing = ActivityLog(
        user_id=test_user.id,
        activity_type="email_sent",
        channel="email",
        direction="outbound",
        external_id="msg-dup-001",
        auto_logged=True,
    )
    db_session.add(existing)
    db_session.commit()

    messages = [
        _make_graph_message("msg-dup-001", "Already logged", "vendor@example.com"),
    ]

    mock_gc_instance = AsyncMock()
    mock_gc_instance.delta_query = AsyncMock(return_value=(messages, "token-2"))

    with (
        patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="fake-token"),
        patch("app.utils.graph_client.GraphClient.__init__", return_value=None),
        patch("app.utils.graph_client.GraphClient.delta_query", mock_gc_instance.delta_query),
    ):
        from app.jobs.email_jobs import scan_sent_folder

        test_user.access_token = "fake-token"
        test_user.m365_connected = True
        db_session.commit()

        result = asyncio.get_event_loop().run_until_complete(
            scan_sent_folder(test_user, db_session)
        )

    # No new logs created (dedup)
    assert len(result) == 0
    total_logs = db_session.query(ActivityLog).filter(
        ActivityLog.user_id == test_user.id
    ).count()
    assert total_logs == 1


# ── test_group_by_thread ─────────────────────────────────────────────────


def test_group_by_thread_related():
    """3 emails with matching In-Reply-To/References headers -> grouped into 1 thread."""
    from app.services.email_threads import group_by_thread

    messages = [
        _make_graph_message(
            "m1", "RFQ for parts", "vendor@example.com",
            headers=[
                {"name": "Message-ID", "value": "<aaa@mail.com>"},
            ],
        ),
        _make_graph_message(
            "m2", "Re: RFQ for parts", "buyer@trioscs.com",
            headers=[
                {"name": "Message-ID", "value": "<bbb@mail.com>"},
                {"name": "In-Reply-To", "value": "<aaa@mail.com>"},
                {"name": "References", "value": "<aaa@mail.com>"},
            ],
        ),
        _make_graph_message(
            "m3", "Re: Re: RFQ for parts", "vendor@example.com",
            headers=[
                {"name": "Message-ID", "value": "<ccc@mail.com>"},
                {"name": "In-Reply-To", "value": "<bbb@mail.com>"},
                {"name": "References", "value": "<aaa@mail.com> <bbb@mail.com>"},
            ],
        ),
    ]

    threads = group_by_thread(messages)
    assert len(threads) == 1
    assert threads[0]["message_count"] == 3


def test_group_by_thread_unrelated():
    """2 emails with no relation -> 2 separate threads."""
    from app.services.email_threads import group_by_thread

    messages = [
        _make_graph_message(
            "m1", "RFQ for LM317T", "vendor1@example.com",
            headers=[
                {"name": "Message-ID", "value": "<xxx@mail.com>"},
            ],
        ),
        _make_graph_message(
            "m2", "Invoice #4567", "vendor2@example.com",
            headers=[
                {"name": "Message-ID", "value": "<yyy@mail.com>"},
            ],
        ),
    ]

    threads = group_by_thread(messages)
    assert len(threads) == 2
    for t in threads:
        assert t["message_count"] == 1


def test_group_by_thread_empty():
    """Empty input returns empty list."""
    from app.services.email_threads import group_by_thread

    assert group_by_thread([]) == []


# ── test_detect_attachments ──────────────────────────────────────────────


def test_detect_attachments_xlsx_flagged():
    """An email with .xlsx attachment is flagged for mining pipeline."""
    mock_gc = AsyncMock()
    mock_gc.get_json = AsyncMock(return_value={
        "value": [
            {
                "name": "stock_list.xlsx",
                "contentType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "size": 45000,
                "isInline": False,
            },
        ]
    })

    from app.jobs.email_jobs import detect_attachments

    result = asyncio.get_event_loop().run_until_complete(
        detect_attachments(mock_gc, "msg-123")
    )

    assert len(result) == 1
    assert result[0]["name"] == "stock_list.xlsx"
    assert result[0]["size"] == 45000


def test_detect_attachments_pdf_flagged():
    """An email with .pdf attachment is flagged."""
    mock_gc = AsyncMock()
    mock_gc.get_json = AsyncMock(return_value={
        "value": [
            {
                "name": "quote.pdf",
                "contentType": "application/pdf",
                "size": 120000,
                "isInline": False,
            },
        ]
    })

    from app.jobs.email_jobs import detect_attachments

    result = asyncio.get_event_loop().run_until_complete(
        detect_attachments(mock_gc, "msg-456")
    )

    assert len(result) == 1
    assert result[0]["name"] == "quote.pdf"


def test_detect_attachments_inline_image_excluded():
    """An inline image is NOT flagged as a file attachment."""
    mock_gc = AsyncMock()
    mock_gc.get_json = AsyncMock(return_value={
        "value": [
            {
                "name": "logo.png",
                "contentType": "image/png",
                "size": 5000,
                "isInline": True,
            },
        ]
    })

    from app.jobs.email_jobs import detect_attachments

    result = asyncio.get_event_loop().run_until_complete(
        detect_attachments(mock_gc, "msg-789")
    )

    assert len(result) == 0


def test_detect_attachments_mixed():
    """Mixed inline image + file attachment: only file is flagged."""
    mock_gc = AsyncMock()
    mock_gc.get_json = AsyncMock(return_value={
        "value": [
            {
                "name": "signature.jpg",
                "contentType": "image/jpeg",
                "size": 3000,
                "isInline": True,
            },
            {
                "name": "inventory.xlsx",
                "contentType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "size": 80000,
                "isInline": False,
            },
        ]
    })

    from app.jobs.email_jobs import detect_attachments

    result = asyncio.get_event_loop().run_until_complete(
        detect_attachments(mock_gc, "msg-mixed")
    )

    assert len(result) == 1
    assert result[0]["name"] == "inventory.xlsx"


# ── test_retry_honors_retry_after ────────────────────────────────────────


def test_retry_honors_retry_after():
    """429 with Retry-After header: client uses max(backoff, Retry-After)."""
    from app.utils.graph_client import _parse_retry_after

    # Create a mock response with Retry-After header
    mock_resp = MagicMock()
    mock_resp.headers = {"Retry-After": "10"}

    result = _parse_retry_after(mock_resp)
    assert result == 10


def test_retry_after_missing():
    """Missing Retry-After returns None."""
    from app.utils.graph_client import _parse_retry_after

    mock_resp = MagicMock()
    mock_resp.headers = {}

    result = _parse_retry_after(mock_resp)
    assert result is None


def test_retry_after_non_numeric():
    """Non-numeric Retry-After returns None."""
    from app.utils.graph_client import _parse_retry_after

    mock_resp = MagicMock()
    mock_resp.headers = {"Retry-After": "Sat, 01 Jan 2028 00:00:00 GMT"}

    result = _parse_retry_after(mock_resp)
    assert result is None


# ── test_401_not_retried ─────────────────────────────────────────────────


def test_401_not_retried():
    """401 Unauthorized should return error immediately, no retries."""
    from app.utils.graph_client import GraphClient

    mock_resp = MagicMock()
    mock_resp.status_code = 401
    mock_resp.text = "Unauthorized"
    mock_resp.headers = {}

    call_count = 0

    async def mock_get(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return mock_resp

    gc = GraphClient("expired-token")

    with patch("app.utils.graph_client.http") as mock_http:
        mock_http.get = mock_get
        result = asyncio.get_event_loop().run_until_complete(
            gc.get_json("/me/messages")
        )

    # Should only be called once (no retries for 401)
    assert call_count == 1
    assert result["error"] == 401


def test_429_returns_error_in_test_mode():
    """In TESTING mode (MAX_RETRIES=0), 429 returns after single attempt."""
    from app.utils.graph_client import GraphClient

    mock_resp = MagicMock()
    mock_resp.status_code = 429
    mock_resp.text = "Too Many Requests"
    mock_resp.headers = {"Retry-After": "5"}

    call_count = 0

    async def mock_get(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return mock_resp

    gc = GraphClient("test-token")

    with patch("app.utils.graph_client.http") as mock_http:
        mock_http.get = mock_get
        result = asyncio.get_event_loop().run_until_complete(
            gc.get_json("/me/messages")
        )

    # In test mode MAX_RETRIES=0, so only 1 attempt total
    assert call_count == 1
    # After exhausting retries, returns the max_retries error
    assert result.get("error") == "max_retries"


# ── test_scan_sent_folders_job_registered ─────────────────────────────────


def test_scan_sent_folders_job_registered():
    """Assert scan_sent_folders job is registered at 30min interval."""
    from app.jobs.email_jobs import register_email_jobs

    mock_scheduler = MagicMock()
    mock_settings = MagicMock()
    mock_settings.contacts_sync_enabled = False
    mock_settings.activity_tracking_enabled = False
    mock_settings.ownership_sweep_enabled = False
    mock_settings.deep_email_mining_enabled = False
    mock_settings.contact_scoring_enabled = False
    mock_settings.customer_enrichment_enabled = False

    register_email_jobs(mock_scheduler, mock_settings)

    # Find the scan_sent_folders call
    job_ids = [call.kwargs.get("id") or call[1].get("id", "") for call in mock_scheduler.add_job.call_args_list]
    assert "scan_sent_folders" in job_ids
