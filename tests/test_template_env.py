"""test_template_env.py — Tests for Jinja2 custom filters and helpers.

Covers _elapsed_seconds, _timesince_filter, _timeago_filter, _fmtdate_filter,
and _sanitize_html_filter.

Called by: pytest
Depends on: app/template_env.py, conftest.py
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.template_env import (
    _elapsed_seconds,
    _fmtdate_filter,
    _sanitize_html_filter,
    _task_due_state,
    _timeago_filter,
    _timesince_filter,
    template_response,
)


class TestTemplateResponse:
    """The helper's only enforced invariant: 'request' must be in context."""

    def test_raises_when_request_missing_from_context(self):
        with pytest.raises(ValueError, match="must be present"):
            template_response("any/template.html", {"user": "alice"})

    def test_raises_when_request_key_is_none(self):
        with pytest.raises(ValueError, match="must be present"):
            template_response("any/template.html", {"request": None})


# ═══════════════════════════════════════════════════════════════════════
#  _elapsed_seconds — helper function
# ═══════════════════════════════════════════════════════════════════════


class TestElapsedSeconds:
    def test_none_returns_none(self):
        assert _elapsed_seconds(None) is None

    def test_empty_string_returns_none(self):
        assert _elapsed_seconds("") is None

    def test_aware_datetime(self):
        past = datetime.now(timezone.utc) - timedelta(seconds=120)
        result = _elapsed_seconds(past)
        assert result is not None
        assert 119 <= result <= 122

    def test_naive_datetime_treated_as_utc(self):
        past = datetime.utcnow() - timedelta(seconds=60)
        result = _elapsed_seconds(past)
        assert result is not None
        assert 59 <= result <= 62

    def test_iso_string(self):
        past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        result = _elapsed_seconds(past)
        assert result is not None
        assert 298 <= result <= 302

    def test_invalid_string_returns_none(self):
        assert _elapsed_seconds("not-a-date") is None


# ═══════════════════════════════════════════════════════════════════════
#  _timesince_filter
# ═══════════════════════════════════════════════════════════════════════


class TestTimesinceFilter:
    def test_none_returns_empty(self):
        assert _timesince_filter(None) == ""

    def test_just_now(self):
        now = datetime.now(timezone.utc)
        assert _timesince_filter(now) == "just now"

    def test_minutes_ago(self):
        past = datetime.now(timezone.utc) - timedelta(minutes=15)
        result = _timesince_filter(past)
        assert "min ago" in result
        assert "15" in result

    def test_1_hour_ago(self):
        past = datetime.now(timezone.utc) - timedelta(hours=1, minutes=10)
        result = _timesince_filter(past)
        assert "hour" in result
        assert "s" not in result.split("hour")[0]  # singular

    def test_hours_ago_plural(self):
        past = datetime.now(timezone.utc) - timedelta(hours=5)
        result = _timesince_filter(past)
        assert "hours ago" in result

    def test_1_day_ago(self):
        past = datetime.now(timezone.utc) - timedelta(days=1)
        assert _timesince_filter(past) == "1 day ago"

    def test_days_ago(self):
        past = datetime.now(timezone.utc) - timedelta(days=7)
        result = _timesince_filter(past)
        assert "7 days ago" == result


# ═══════════════════════════════════════════════════════════════════════
#  _timeago_filter — compact format
# ═══════════════════════════════════════════════════════════════════════


class TestTimeagoFilter:
    def test_none_returns_dash(self):
        assert _timeago_filter(None) == "--"

    def test_just_now(self):
        now = datetime.now(timezone.utc)
        assert _timeago_filter(now) == "just now"

    def test_minutes(self):
        past = datetime.now(timezone.utc) - timedelta(minutes=30)
        result = _timeago_filter(past)
        assert "m ago" in result

    def test_hours(self):
        past = datetime.now(timezone.utc) - timedelta(hours=3)
        result = _timeago_filter(past)
        assert "h ago" in result

    def test_days(self):
        past = datetime.now(timezone.utc) - timedelta(days=4)
        result = _timeago_filter(past)
        assert "d ago" in result

    def test_weeks(self):
        past = datetime.now(timezone.utc) - timedelta(weeks=3)
        result = _timeago_filter(past)
        assert "w ago" in result

    def test_months(self):
        past = datetime.now(timezone.utc) - timedelta(days=60)
        result = _timeago_filter(past)
        assert "mo ago" in result


# ═══════════════════════════════════════════════════════════════════════
#  _fmtdate_filter
# ═══════════════════════════════════════════════════════════════════════


class TestFmtdateFilter:
    def test_none_returns_default(self):
        assert _fmtdate_filter(None) == "\u2014"

    def test_none_custom_default(self):
        assert _fmtdate_filter(None, default="N/A") == "N/A"

    def test_string_passthrough(self):
        assert _fmtdate_filter("March 2026") == "March 2026"

    def test_datetime_formatted(self):
        dt = datetime(2026, 3, 15, 14, 30)
        result = _fmtdate_filter(dt)
        assert "Mar 15" in result
        assert "14:30" in result

    def test_datetime_custom_format(self):
        dt = datetime(2026, 1, 5)
        result = _fmtdate_filter(dt, fmt="%Y-%m-%d")
        assert result == "2026-01-05"

    def test_non_datetime_returns_default(self):
        assert _fmtdate_filter(12345) == "\u2014"


# ═══════════════════════════════════════════════════════════════════════
#  _task_due_state — urgency bucketing (calendar-day, mutually exclusive)
# ═══════════════════════════════════════════════════════════════════════


class TestTaskDueState:
    """(is_overdue, is_due_today) is judged by *calendar day*, so a task due earlier
    today is 'due today', never 'overdue' (#4), and the two flags never both fire — the
    My Day filter and the results grouping consume this one helper and so can't
    disagree."""

    @staticmethod
    def _task(due_at):
        return SimpleNamespace(due_at=due_at)

    def test_none_due_is_neither(self):
        assert _task_due_state(self._task(None), datetime.now(timezone.utc)) == (False, False)

    def test_earlier_today_is_due_today_not_overdue(self):
        """The core #4 regression: due at 00:00 with the clock already at 18:00 is still
        'due today', not 'overdue'."""
        now = datetime(2026, 6, 25, 18, 0, tzinfo=timezone.utc)
        assert _task_due_state(self._task(datetime(2026, 6, 25, 0, 0, tzinfo=timezone.utc)), now) == (False, True)

    def test_prior_day_is_overdue(self):
        now = datetime(2026, 6, 25, 6, 0, tzinfo=timezone.utc)
        assert _task_due_state(self._task(datetime(2026, 6, 24, 0, 0, tzinfo=timezone.utc)), now) == (True, False)

    def test_future_day_is_neither(self):
        now = datetime(2026, 6, 25, 6, 0, tzinfo=timezone.utc)
        assert _task_due_state(self._task(datetime(2026, 6, 27, 0, 0, tzinfo=timezone.utc)), now) == (False, False)

    def test_naive_due_at_coerced_to_utc(self):
        """A naive due_at (legacy row / SQLite) is treated as UTC — no TypeError."""
        now = datetime(2026, 6, 25, 18, 0, tzinfo=timezone.utc)
        assert _task_due_state(self._task(datetime(2026, 6, 25, 0, 0)), now) == (False, True)


# ═══════════════════════════════════════════════════════════════════════
#  _sanitize_html_filter
# ═══════════════════════════════════════════════════════════════════════


class TestSanitizeHtmlFilter:
    def test_empty_string(self):
        assert _sanitize_html_filter("") == ""

    def test_none_returns_empty(self):
        assert _sanitize_html_filter(None) == ""

    def test_allows_safe_tags(self):
        html = "<p>Hello <strong>world</strong></p>"
        result = _sanitize_html_filter(html)
        assert "<p>" in result
        assert "<strong>" in result

    def test_strips_script_tags(self):
        html = "<p>Safe</p><script>alert('xss')</script>"
        result = _sanitize_html_filter(html)
        assert "<script>" not in result
        assert "alert" not in result

    def test_preserves_links_with_href(self):
        html = '<a href="https://example.com" title="Example">Link</a>'
        result = _sanitize_html_filter(html)
        assert "https://example.com" in result
        assert "Link" in result
