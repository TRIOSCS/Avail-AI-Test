"""Per-user display-timezone mechanism tests.

Covers the four pieces of the foundation:
  - app.utils.timezones: IANA validation + UTC->viewer-zone conversion for two zones.
  - the |localtime / |localdate Jinja filters (contextvar-driven zone).
  - _task_due_state: a non-Eastern (Asia/Tokyo) viewer sees a task due on THEIR
    calendar day as "today" while the business-default viewer sees it as "later".
  - POST /v2/profile/timezone: valid stores, invalid 400s, unchanged is a no-op.

Called by: pytest
Depends on: app.utils.timezones, app.template_env, app.request_context, the settings router.
"""

from datetime import datetime, timezone

import pytest

from app.request_context import current_user_display_tz_var
from app.utils.timezones import (
    DEFAULT_DISPLAY_TZ,
    format_localdate,
    format_localtime,
    is_valid_timezone,
    to_display_tz,
)


@pytest.fixture()
def _reset_tz_contextvar():
    """Snapshot + restore the display-tz contextvar around a test (no cross-test
    leak)."""
    token = current_user_display_tz_var.set(None)
    try:
        yield
    finally:
        current_user_display_tz_var.reset(token)


# ── IANA validation ────────────────────────────────────────────────────


class TestIsValidTimezone:
    def test_real_iana_names(self):
        assert is_valid_timezone("America/New_York")
        assert is_valid_timezone("Asia/Tokyo")
        assert is_valid_timezone("UTC")

    def test_windows_and_junk_names_rejected(self):
        # Graph mailbox zone is a Windows name — must NOT validate as IANA.
        assert not is_valid_timezone("Pacific Standard Time")
        assert not is_valid_timezone("Not/AZone")
        assert not is_valid_timezone("")
        assert not is_valid_timezone(None)


# ── UTC -> viewer-zone conversion (two zones) ───────────────────────────


class TestConversion:
    # 23:30 UTC on Jul 4 → Eastern (UTC-4 summer) still Jul 4 19:30; Tokyo (UTC+9) Jul 5 08:30.
    UTC_DT = datetime(2026, 7, 4, 23, 30, tzinfo=timezone.utc)

    def test_format_localtime_two_zones(self):
        assert format_localtime(self.UTC_DT, "%b %d, %H:%M", tz="America/New_York") == "Jul 04, 19:30"
        assert format_localtime(self.UTC_DT, "%b %d, %H:%M", tz="Asia/Tokyo") == "Jul 05, 08:30"

    def test_naive_datetime_treated_as_utc(self):
        naive = datetime(2026, 7, 4, 23, 30)  # no tzinfo → assumed UTC
        assert format_localtime(naive, "%b %d, %H:%M", tz="Asia/Tokyo") == "Jul 05, 08:30"

    def test_format_localdate_crosses_day_boundary(self):
        assert format_localdate(self.UTC_DT, "%Y-%m-%d", tz="America/New_York") == "2026-07-04"
        assert format_localdate(self.UTC_DT, "%Y-%m-%d", tz="Asia/Tokyo") == "2026-07-05"

    def test_none_returns_default(self):
        assert format_localtime(None, default="—") == "—"
        assert to_display_tz(None) is None

    def test_invalid_zone_falls_back_to_default(self):
        # An unknown zone name resolves to DEFAULT_DISPLAY_TZ (America/New_York).
        assert DEFAULT_DISPLAY_TZ == "America/New_York"
        assert format_localtime(self.UTC_DT, "%b %d, %H:%M", tz="Bogus/Zone") == "Jul 04, 19:30"

    def test_contextvar_drives_default_zone(self, _reset_tz_contextvar):
        current_user_display_tz_var.set("Asia/Tokyo")
        assert format_localtime(self.UTC_DT, "%b %d, %H:%M") == "Jul 05, 08:30"
        current_user_display_tz_var.set("America/New_York")
        assert format_localtime(self.UTC_DT, "%b %d, %H:%M") == "Jul 04, 19:30"


# ── Jinja filters ───────────────────────────────────────────────────────


class TestJinjaFilters:
    def test_localtime_and_localdate_registered(self):
        from app.template_env import templates

        assert "localtime" in templates.env.filters
        assert "localdate" in templates.env.filters

    def test_localtime_filter_uses_contextvar(self, _reset_tz_contextvar):
        from app.template_env import templates

        current_user_display_tz_var.set("Asia/Tokyo")
        rendered = templates.env.from_string("{{ dt|localtime('%b %d, %H:%M') }}").render(
            dt=datetime(2026, 7, 4, 23, 30, tzinfo=timezone.utc)
        )
        assert rendered == "Jul 05, 08:30"

    def test_localtime_filter_passes_strings_through(self):
        from app.template_env import templates

        out = templates.env.from_string("{{ v|localtime }}").render(v="already a string")
        assert out == "already a string"


# ── _task_due_state per-user timezone ───────────────────────────────────


class _FakeTask:
    def __init__(self, due_at):
        self.due_at = due_at


class TestTaskDueStatePerUserTz:
    # now = Jul 4 22:00 UTC → Tokyo (UTC+9) is Jul 5 07:00; Eastern (UTC-4) is Jul 4 18:00.
    NOW_UTC = datetime(2026, 7, 4, 22, 0, tzinfo=timezone.utc)
    # Task due date sentinel = Jul 5 (UTC midnight), the calendar day the user picked.
    DUE_JUL5 = datetime(2026, 7, 5, 0, 0, tzinfo=timezone.utc)

    def test_tokyo_user_sees_due_today(self, _reset_tz_contextvar):
        from app.template_env import _task_due_state

        current_user_display_tz_var.set("Asia/Tokyo")
        is_overdue, is_due_today = _task_due_state(_FakeTask(self.DUE_JUL5), self.NOW_UTC)
        assert (is_overdue, is_due_today) == (False, True)

    def test_default_business_tz_sees_later_not_today(self, _reset_tz_contextvar):
        from app.template_env import _task_due_state

        # No viewer tz → business default (America/New_York): its "today" is still Jul 4,
        # so a Jul-5 due date is "later", not today — proving the zone actually matters.
        current_user_display_tz_var.set(None)
        is_overdue, is_due_today = _task_due_state(_FakeTask(self.DUE_JUL5), self.NOW_UTC)
        assert (is_overdue, is_due_today) == (False, False)

    def test_no_due_date_is_neither(self, _reset_tz_contextvar):
        from app.template_env import _task_due_state

        current_user_display_tz_var.set("Asia/Tokyo")
        assert _task_due_state(_FakeTask(None), self.NOW_UTC) == (False, False)


# ── POST /v2/profile/timezone endpoint ──────────────────────────────────


class TestTimezoneEndpoint:
    def test_valid_zone_stored_when_unset(self, client, db_session, test_user):
        assert test_user.display_timezone is None
        resp = client.post("/v2/profile/timezone", data={"timezone": "Asia/Tokyo"})
        assert resp.status_code == 200
        db_session.refresh(test_user)
        assert test_user.display_timezone == "Asia/Tokyo"

    def test_changed_zone_is_updated(self, client, db_session, test_user):
        test_user.display_timezone = "America/New_York"
        db_session.commit()
        resp = client.post("/v2/profile/timezone", data={"timezone": "Europe/London"})
        assert resp.status_code == 200
        db_session.refresh(test_user)
        assert test_user.display_timezone == "Europe/London"

    def test_unchanged_zone_is_noop(self, client, db_session, test_user):
        test_user.display_timezone = "Asia/Tokyo"
        db_session.commit()
        resp = client.post("/v2/profile/timezone", data={"timezone": "Asia/Tokyo"})
        assert resp.status_code == 200
        # No toast HX-Trigger on the unchanged no-op path.
        assert "HX-Trigger" not in resp.headers
        db_session.refresh(test_user)
        assert test_user.display_timezone == "Asia/Tokyo"

    def test_invalid_zone_rejected(self, client, db_session, test_user):
        resp = client.post("/v2/profile/timezone", data={"timezone": "Pacific Standard Time"})
        assert resp.status_code == 400
        assert resp.json()["error"]
        db_session.refresh(test_user)
        assert test_user.display_timezone is None

    def test_empty_zone_rejected(self, client, test_user):
        resp = client.post("/v2/profile/timezone", data={"timezone": ""})
        assert resp.status_code == 400
