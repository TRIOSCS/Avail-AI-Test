"""Tests for app.management.notify_nightly_status (Teams-only alerting).

Covers: non-PASS status posts ONE Teams Adaptive Card (http.post mocked at its
source, app.http_client); PASS is a no-op; a missing webhook logs-and-skips
without raising; a bare status line (no date prefix) still alerts. The former
per-admin in-app Notification rows were deleted with the write-only
notifications channel (W2.9/§5.5).

Called by: pytest
Depends on: app.management.notify_nightly_status
"""

import os

os.environ["TESTING"] = "1"

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


def test_non_pass_posts_teams_card():
    from app.management.notify_nightly_status import notify_nightly_status

    mock_post = AsyncMock(return_value=SimpleNamespace(status_code=200, text="ok"))
    status_line = "2026-07-24: FAIL (exit=1, coverage=93%)"

    with (
        patch(
            "app.services.teams_notifications.get_credential_cached",
            return_value="https://outlook.office.com/webhook/test",
        ),
        patch("app.http_client.http.post", new=mock_post),
    ):
        dispatched = notify_nightly_status(status_line)

    assert dispatched is True
    mock_post.assert_called_once()
    payload = mock_post.call_args.kwargs["json"]
    assert payload["type"] == "message"
    card = payload["attachments"][0]["content"]
    assert card["type"] == "AdaptiveCard"
    assert status_line in card["body"][0]["text"]


def test_pass_status_is_noop():
    from app.management.notify_nightly_status import notify_nightly_status

    mock_post = AsyncMock()

    with patch("app.http_client.http.post", new=mock_post):
        dispatched = notify_nightly_status("2026-07-24: PASS (coverage=97%)")

    assert dispatched is False
    mock_post.assert_not_called()


def test_missing_webhook_logs_and_skips_without_raising():
    """No webhook configured → Teams path skips silently (no raise, no post); the alert
    path still reports dispatched (the cron log carries the ALERT line)."""
    from app.management.notify_nightly_status import notify_nightly_status

    mock_post = AsyncMock()

    with (
        patch("app.services.teams_notifications.get_credential_cached", return_value=None),
        patch("app.http_client.http.post", new=mock_post),
    ):
        dispatched = notify_nightly_status("2026-07-24: CRASH (xdist internal error — see log; exit=3)")

    assert dispatched is True
    mock_post.assert_not_called()


def test_crash_status_without_date_prefix_still_alerts():
    """The PASS guard parses the STATUS part; a bare status line (no date prefix) is
    handled too."""
    from app.management.notify_nightly_status import notify_nightly_status

    with (
        patch("app.services.teams_notifications.get_credential_cached", return_value=None),
        patch("app.http_client.http.post", new=AsyncMock()),
    ):
        assert notify_nightly_status("FAIL (coverage dropped to 42%)") is True
