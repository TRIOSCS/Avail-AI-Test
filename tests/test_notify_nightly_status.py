"""Tests for app.management.notify_nightly_status + the shared in-app notify seam.

Covers: non-PASS status posts ONE Teams Adaptive Card (http.post mocked at its
source, app.http_client) and writes in-app Notification rows for ACTIVE admins
only; PASS is a no-op; a missing webhook logs-and-skips without raising (in-app
rows still written); write_in_app stays importable from the approvals module
(back-compat re-export of app.services.in_app_notifications).

Called by: pytest
Depends on: app.management.notify_nightly_status, conftest db_session
"""

import os

os.environ["TESTING"] = "1"

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.models.auth import User
from app.models.notification import Notification


def _seed_users(db):
    """One active admin, one inactive admin, one active buyer."""
    admin = User(
        email="nightly-admin@trioscs.com",
        name="Nightly Admin",
        role="admin",
        azure_id="nightly-adm-1",
        is_active=True,
        created_at=datetime.now(UTC),
    )
    inactive_admin = User(
        email="nightly-exadmin@trioscs.com",
        name="Ex Admin",
        role="admin",
        azure_id="nightly-adm-2",
        is_active=False,
        created_at=datetime.now(UTC),
    )
    buyer = User(
        email="nightly-buyer@trioscs.com",
        name="Nightly Buyer",
        role="buyer",
        azure_id="nightly-buy-1",
        is_active=True,
        created_at=datetime.now(UTC),
    )
    db.add_all([admin, inactive_admin, buyer])
    db.commit()
    return admin, inactive_admin, buyer


def test_non_pass_posts_teams_card_and_writes_admin_rows(db_session):
    from app.management.notify_nightly_status import notify_nightly_status

    admin, _inactive_admin, _buyer = _seed_users(db_session)
    mock_post = AsyncMock(return_value=SimpleNamespace(status_code=200, text="ok"))
    status_line = "2026-07-24: FAIL (exit=1, coverage=93%)"

    with (
        patch(
            "app.services.teams_notifications.get_credential_cached",
            return_value="https://outlook.office.com/webhook/test",
        ),
        patch("app.http_client.http.post", new=mock_post),
    ):
        dispatched = notify_nightly_status(status_line, db=db_session)

    assert dispatched is True
    mock_post.assert_called_once()
    payload = mock_post.call_args.kwargs["json"]
    assert payload["type"] == "message"
    card = payload["attachments"][0]["content"]
    assert card["type"] == "AdaptiveCard"
    assert status_line in card["body"][0]["text"]

    rows = db_session.query(Notification).all()
    assert {row.user_id for row in rows} == {admin.id}  # active admin ONLY
    assert rows[0].event_type == "nightly_tests_failed"
    assert status_line in (rows[0].body or "")
    assert rows[0].is_read is False


def test_pass_status_is_noop(db_session):
    from app.management.notify_nightly_status import notify_nightly_status

    _seed_users(db_session)
    mock_post = AsyncMock()

    with patch("app.http_client.http.post", new=mock_post):
        dispatched = notify_nightly_status("2026-07-24: PASS (coverage=97%)", db=db_session)

    assert dispatched is False
    mock_post.assert_not_called()
    assert db_session.query(Notification).count() == 0


def test_missing_webhook_logs_and_skips_but_still_writes_in_app(db_session):
    """No webhook configured → Teams path skips silently (no raise, no post); the in-app
    admin rows are still written."""
    from app.management.notify_nightly_status import notify_nightly_status

    admin, _inactive_admin, _buyer = _seed_users(db_session)
    mock_post = AsyncMock()

    with (
        patch("app.services.teams_notifications.get_credential_cached", return_value=None),
        patch("app.http_client.http.post", new=mock_post),
    ):
        dispatched = notify_nightly_status("2026-07-24: CRASH (xdist internal error — see log; exit=3)", db=db_session)

    assert dispatched is True
    mock_post.assert_not_called()
    rows = db_session.query(Notification).all()
    assert {row.user_id for row in rows} == {admin.id}


def test_crash_status_without_date_prefix_still_alerts(db_session):
    """The PASS guard parses the STATUS part; a bare status line (no date prefix) is
    handled too."""
    from app.management.notify_nightly_status import notify_nightly_status

    admin, _inactive_admin, _buyer = _seed_users(db_session)
    with (
        patch("app.services.teams_notifications.get_credential_cached", return_value=None),
        patch("app.http_client.http.post", new=AsyncMock()),
    ):
        assert notify_nightly_status("FAIL (coverage dropped to 42%)", db=db_session) is True
    assert db_session.query(Notification).count() == 1
    assert db_session.query(Notification).first().user_id == admin.id


def test_write_in_app_reexport_backcompat():
    """approvals.notifications.write_in_app must BE the shared seam's function so
    existing imports/patches (buy_plans router, approval_outbox._ns) keep working."""
    from app.services import in_app_notifications
    from app.services.approvals import notifications as approvals_notifications

    assert approvals_notifications.write_in_app is in_app_notifications.write_in_app
