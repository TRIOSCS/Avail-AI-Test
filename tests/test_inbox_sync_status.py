"""Tests for the inbox sync status helper."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.constants import InboxSyncHealth
from app.services.activity_service import get_inbox_sync_status


def _user(**kw):
    base = dict(
        m365_connected=True,
        last_inbox_scan=datetime.now(timezone.utc),
        token_expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        access_token="t",
        m365_error_reason=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_status_ok():
    s = get_inbox_sync_status(_user())
    assert s["health"] == InboxSyncHealth.OK
    assert s["connected"] is True


def test_status_error_when_disconnected():
    s = get_inbox_sync_status(_user(m365_connected=False))
    assert s["health"] == InboxSyncHealth.ERROR


def test_status_error_when_token_expired():
    s = get_inbox_sync_status(_user(token_expires_at=datetime.now(timezone.utc) - timedelta(minutes=1)))
    assert s["health"] == InboxSyncHealth.ERROR


def test_status_warning_when_stale():
    old = datetime.now(timezone.utc) - timedelta(hours=6)
    s = get_inbox_sync_status(_user(last_inbox_scan=old))
    assert s["health"] == InboxSyncHealth.WARNING
    assert s["is_stale"] is True


def test_status_stale_when_never_scanned():
    s = get_inbox_sync_status(_user(last_inbox_scan=None))
    assert s["is_stale"] is True
