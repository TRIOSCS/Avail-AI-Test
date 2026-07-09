"""test_template_env.py — Tests for Jinja2 custom filters and helpers.

Covers _elapsed_seconds, _timesince_filter, _timeago_filter, _fmtdate_filter,
and _sanitize_html_filter.

Called by: pytest
Depends on: app/template_env.py, conftest.py
"""

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.request_context import current_user_display_tz_var
from app.template_env import (
    _elapsed_seconds,
    _fmtdate_filter,
    _fru_alias_mpns_filter,
    _localdate_filter,
    _localday_filter,
    _localtime_filter,
    _now,
    _part_description,
    _pricefmt_filter,
    _safe_url_filter,
    _sanitize_html_filter,
    _sub_mpns_filter,
    _task_due_state,
    _timeago_filter,
    _timesince_filter,
    page_response,
    template_response,
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
        past = datetime.now(UTC) - timedelta(seconds=120)
        result = _elapsed_seconds(past)
        assert result is not None
        assert 119 <= result <= 122

    def test_naive_datetime_treated_as_utc(self):
        past = datetime.utcnow() - timedelta(seconds=60)
        result = _elapsed_seconds(past)
        assert result is not None
        assert 59 <= result <= 62

    def test_iso_string(self):
        past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
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
        now = datetime.now(UTC)
        assert _timesince_filter(now) == "just now"

    def test_minutes_ago(self):
        past = datetime.now(UTC) - timedelta(minutes=15)
        result = _timesince_filter(past)
        assert "min ago" in result
        assert "15" in result

    def test_1_hour_ago(self):
        past = datetime.now(UTC) - timedelta(hours=1, minutes=10)
        result = _timesince_filter(past)
        assert "hour" in result
        assert "s" not in result.split("hour")[0]  # singular

    def test_hours_ago_plural(self):
        past = datetime.now(UTC) - timedelta(hours=5)
        result = _timesince_filter(past)
        assert "hours ago" in result

    def test_1_day_ago(self):
        past = datetime.now(UTC) - timedelta(days=1)
        assert _timesince_filter(past) == "1 day ago"

    def test_days_ago(self):
        past = datetime.now(UTC) - timedelta(days=7)
        result = _timesince_filter(past)
        assert "7 days ago" == result


# ═══════════════════════════════════════════════════════════════════════
#  _timeago_filter — compact format
# ═══════════════════════════════════════════════════════════════════════


class TestTimeagoFilter:
    def test_none_returns_dash(self):
        assert _timeago_filter(None) == "--"

    def test_just_now(self):
        now = datetime.now(UTC)
        assert _timeago_filter(now) == "just now"

    def test_minutes(self):
        past = datetime.now(UTC) - timedelta(minutes=30)
        result = _timeago_filter(past)
        assert "m ago" in result

    def test_hours(self):
        past = datetime.now(UTC) - timedelta(hours=3)
        result = _timeago_filter(past)
        assert "h ago" in result

    def test_days(self):
        past = datetime.now(UTC) - timedelta(days=4)
        result = _timeago_filter(past)
        assert "d ago" in result

    def test_weeks(self):
        past = datetime.now(UTC) - timedelta(weeks=3)
        result = _timeago_filter(past)
        assert "w ago" in result

    def test_months(self):
        past = datetime.now(UTC) - timedelta(days=60)
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
        assert _task_due_state(self._task(None), datetime.now(UTC)) == (False, False)

    def test_earlier_today_is_due_today_not_overdue(self):
        """The core #4 regression: due at 00:00 with the clock already at 18:00 is still
        'due today', not 'overdue'."""
        now = datetime(2026, 6, 25, 18, 0, tzinfo=UTC)
        assert _task_due_state(self._task(datetime(2026, 6, 25, 0, 0, tzinfo=UTC)), now) == (False, True)

    def test_prior_day_is_overdue(self):
        now = datetime(2026, 6, 25, 6, 0, tzinfo=UTC)
        assert _task_due_state(self._task(datetime(2026, 6, 24, 0, 0, tzinfo=UTC)), now) == (True, False)

    def test_future_day_is_neither(self):
        now = datetime(2026, 6, 25, 6, 0, tzinfo=UTC)
        assert _task_due_state(self._task(datetime(2026, 6, 27, 0, 0, tzinfo=UTC)), now) == (False, False)

    def test_naive_due_at_coerced_to_utc(self):
        """A naive due_at (legacy row / SQLite) is treated as UTC — no TypeError."""
        now = datetime(2026, 6, 25, 18, 0, tzinfo=UTC)
        assert _task_due_state(self._task(datetime(2026, 6, 25, 0, 0)), now) == (False, True)

    def test_business_timezone_governs_today_near_utc_midnight(self):
        """#3 — near UTC midnight the business day (US/Eastern) still governs.

        At 02:00 UTC it is still the prior evening in Eastern, so a task due that
        Eastern day is 'due today', not 'overdue' — even though the UTC clock has
        already rolled to tomorrow.
        """
        now = datetime(2026, 6, 26, 2, 0, tzinfo=UTC)  # 2026-06-25 22:00 US/Eastern
        task = self._task(datetime(2026, 6, 25, 0, 0, tzinfo=UTC))  # due 2026-06-25
        assert _task_due_state(task, now) == (False, True)


# ═══════════════════════════════════════════════════════════════════════
#  _localday_filter — viewer-local day bucketing key
# ═══════════════════════════════════════════════════════════════════════


class TestLocalDayFilter:
    """|localday returns the datetime's calendar DATE in the CURRENT viewer's zone, so
    the activity-feed day-group headers ("Today"/"Yesterday"/date) bucket on the
    viewer's local day rather than the UTC calendar day."""

    # 23:30 UTC Jul 4 → Eastern (UTC-4 summer) still Jul 4 19:30; Tokyo (UTC+9) Jul 5 08:30.
    UTC_DT = datetime(2026, 7, 4, 23, 30, tzinfo=UTC)

    def test_registered_as_filter(self):
        from app.template_env import templates

        assert "localday" in templates.env.filters

    def test_none_and_strings_return_none(self):
        assert _localday_filter(None) is None
        assert _localday_filter("already a string") is None

    def test_returns_date_object(self, _reset_tz_contextvar):
        current_user_display_tz_var.set("America/New_York")
        assert _localday_filter(self.UTC_DT) == date(2026, 7, 4)

    def test_same_instant_buckets_differently_per_zone(self, _reset_tz_contextvar):
        """The core fix: one UTC instant near midnight → different local day per viewer."""
        current_user_display_tz_var.set("America/New_York")
        eastern_day = _localday_filter(self.UTC_DT)
        current_user_display_tz_var.set("Asia/Tokyo")
        tokyo_day = _localday_filter(self.UTC_DT)
        assert eastern_day == date(2026, 7, 4)
        assert tokyo_day == date(2026, 7, 5)
        assert eastern_day != tokyo_day

    def test_naive_datetime_treated_as_utc(self, _reset_tz_contextvar):
        current_user_display_tz_var.set("Asia/Tokyo")
        assert _localday_filter(datetime(2026, 7, 4, 23, 30)) == date(2026, 7, 5)


# ═══════════════════════════════════════════════════════════════════════
#  Activity-feed day bucketing — the real |localday + |localdate grouping block
# ═══════════════════════════════════════════════════════════════════════


class TestActivityDayBucketing:
    """Reproduces the exact day-group block from the activity/timeline templates (now|localday
    + ts|localday + ts|localdate) through the real registered filters, proving the header a
    viewer sees for a given instant follows THEIR local day — Tokyo and the business-default
    (Eastern) viewer disagree at the UTC midnight boundary for the same instant."""

    # Mirrors the bucketing block in the activity templates verbatim (now passed in for a
    # deterministic "today"; the templates use the now() global, same mechanism).
    TEMPLATE = (
        "{%- set today = now|localday -%}"
        "{%- set ts_day = ts|localday -%}"
        "{%- set day_delta = (today - ts_day).days -%}"
        "{%- if day_delta == 0 %}Today"
        "{%- elif day_delta == 1 %}Yesterday"
        "{%- else %}{{ ts|localdate('%b %d, %Y') }}{% endif -%}"
    )

    def _render(self, now_dt, ts_dt):
        from app.template_env import templates

        return templates.env.from_string(self.TEMPLATE).render(now=now_dt, ts=ts_dt)

    # now = Jul 5 12:00 UTC (a stable "today" for both zones); ts = Jul 4 23:30 UTC.
    NOW = datetime(2026, 7, 5, 12, 0, tzinfo=UTC)
    TS_NEAR_MIDNIGHT = datetime(2026, 7, 4, 23, 30, tzinfo=UTC)

    def test_boundary_instant_is_yesterday_for_eastern(self, _reset_tz_contextvar):
        # Eastern: ts → Jul 4 19:30 (yesterday relative to Jul 5).
        current_user_display_tz_var.set("America/New_York")
        assert self._render(self.NOW, self.TS_NEAR_MIDNIGHT) == "Yesterday"

    def test_boundary_instant_is_today_for_tokyo(self, _reset_tz_contextvar):
        # Same instant, Tokyo: ts → Jul 5 08:30 (today) — a different header from Eastern.
        current_user_display_tz_var.set("Asia/Tokyo")
        assert self._render(self.NOW, self.TS_NEAR_MIDNIGHT) == "Today"

    def test_date_header_reflects_viewer_local_day(self, _reset_tz_contextvar):
        # Far-past instant → the date branch; header date differs by viewer zone.
        far_now = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
        current_user_display_tz_var.set("America/New_York")
        assert self._render(far_now, self.TS_NEAR_MIDNIGHT) == "Jul 04, 2026"
        current_user_display_tz_var.set("Asia/Tokyo")
        assert self._render(far_now, self.TS_NEAR_MIDNIGHT) == "Jul 05, 2026"


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


class TestPageResponse:
    def test_missing_request_raises(self):
        with pytest.raises(ValueError, match="'request' must be present"):
            page_response({})

    def test_missing_request_key_raises(self):
        with pytest.raises(ValueError):
            page_response({"user": "alice"})


class TestTemplateResponseSuccessPath:
    def test_missing_request_raises(self):
        with pytest.raises(ValueError, match="'request' must be present"):
            template_response("htmx/base_page.html", {"title": "x"})


class TestLocalTimeFilter:
    def test_none_returns_default(self):
        assert _localtime_filter(None) == "—"

    def test_string_passthrough(self):
        assert _localtime_filter("2024-01-01") == "2024-01-01"

    def test_datetime_returns_string(self):
        dt = datetime(2024, 6, 15, 12, 0, tzinfo=UTC)
        result = _localtime_filter(dt, "%Y-%m-%d")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_custom_default_for_none(self):
        assert _localtime_filter(None, default="N/A") == "N/A"


class TestLocalDateFilter:
    def test_none_returns_default(self):
        assert _localdate_filter(None) == "—"

    def test_string_passthrough(self):
        assert _localdate_filter("2024-01-01") == "2024-01-01"

    def test_datetime_returns_string(self):
        dt = datetime(2024, 6, 15, 12, 0, tzinfo=UTC)
        result = _localdate_filter(dt)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_custom_default(self):
        assert _localdate_filter(None, default="TBD") == "TBD"


class TestPricefmtFilter:
    def test_none_returns_default(self):
        assert _pricefmt_filter(None) == "—"

    def test_empty_string_returns_default(self):
        assert _pricefmt_filter("") == "—"

    def test_strips_trailing_zeros(self):
        assert _pricefmt_filter(0.79) == "0.79"

    def test_integer_price(self):
        assert _pricefmt_filter(5) == "5"

    def test_four_decimal_places(self):
        assert _pricefmt_filter(0.0084) == "0.0084"

    def test_string_numeric(self):
        assert _pricefmt_filter("12.34") == "12.34"

    def test_invalid_string_returns_default(self):
        assert _pricefmt_filter("not-a-price") == "—"

    def test_custom_default(self):
        assert _pricefmt_filter(None, default="—") == "—"

    def test_zero(self):
        result = _pricefmt_filter(0.0)
        assert result == "0"


class TestSubMpnsFilter:
    def test_none_returns_empty(self):
        assert _sub_mpns_filter(None) == []

    def test_empty_list_returns_empty(self):
        assert _sub_mpns_filter([]) == []

    def test_list_of_strings(self):
        result = _sub_mpns_filter(["ABC123", "XYZ456"])
        assert "ABC123" in result
        assert "XYZ456" in result

    def test_list_of_dicts(self):
        result = _sub_mpns_filter([{"mpn": "LM317T"}, {"mpn": "NE555"}])
        assert "LM317T" in result
        assert "NE555" in result

    def test_mixed_str_and_dict(self):
        result = _sub_mpns_filter(["ABC123", {"mpn": "LM317T"}])
        assert "ABC123" in result
        assert "LM317T" in result

    def test_dict_without_mpn_key_skipped(self):
        result = _sub_mpns_filter([{"manufacturer": "TI"}])
        assert result == []

    def test_unknown_type_skipped(self):
        result = _sub_mpns_filter([42, None])
        assert result == []

    def test_short_mpn_filtered_out(self):
        # normalize_mpn returns None for < 3 chars
        result = _sub_mpns_filter(["AB"])
        assert result == []


class TestFruAliasMpnsFilter:
    def test_none_returns_empty_set(self):
        assert _fru_alias_mpns_filter(None) == set()

    def test_empty_list_returns_empty_set(self):
        assert _fru_alias_mpns_filter([]) == set()

    def test_fru_source_included(self):
        from app.constants import FRU_ALIAS_SOURCE

        subs = [{"mpn": "LM317T", "source": FRU_ALIAS_SOURCE}]
        result = _fru_alias_mpns_filter(subs)
        assert "LM317T" in result

    def test_non_fru_source_excluded(self):
        subs = [{"mpn": "LM317T", "source": "manual"}, {"mpn": "NE555", "source": "user"}]
        result = _fru_alias_mpns_filter(subs)
        assert result == set()

    def test_string_entries_skipped(self):
        result = _fru_alias_mpns_filter(["ABC123"])
        assert result == set()

    def test_mixed_fru_and_other(self):
        from app.constants import FRU_ALIAS_SOURCE

        subs = [
            {"mpn": "ABC123", "source": FRU_ALIAS_SOURCE},
            {"mpn": "XYZ", "source": "other"},
        ]
        result = _fru_alias_mpns_filter(subs)
        assert "ABC123" in result
        assert "XYZ" not in result


class TestPartDescription:
    def test_material_card_description_preferred(self):
        card = SimpleNamespace(description="  Resistor 10K  ")
        obj = SimpleNamespace(material_card=card, description="Old desc")
        assert _part_description(obj) == "Resistor 10K"

    def test_fallback_to_own_description(self):
        obj = SimpleNamespace(material_card=None, description="Capacitor 100uF")
        assert _part_description(obj) == "Capacitor 100uF"

    def test_no_description_returns_empty(self):
        obj = SimpleNamespace(material_card=None, description=None)
        assert _part_description(obj) == ""

    def test_short_card_description_falls_through(self):
        card = SimpleNamespace(description="AB")  # < 3 chars
        obj = SimpleNamespace(material_card=card, description="Real description")
        assert _part_description(obj) == "Real description"

    def test_short_own_description_returns_empty(self):
        obj = SimpleNamespace(material_card=None, description="AB")
        assert _part_description(obj) == ""

    def test_no_material_card_attr(self):
        obj = SimpleNamespace(description="Some desc here")
        assert _part_description(obj) == "Some desc here"

    def test_card_with_no_description_attr(self):
        card = SimpleNamespace()
        obj = SimpleNamespace(material_card=card, description="Fallback desc")
        assert _part_description(obj) == "Fallback desc"


class TestSafeUrlFilter:
    def test_https_url_allowed(self):
        url = "https://example.com/path"
        assert _safe_url_filter(url) == url

    def test_http_url_allowed(self):
        url = "http://example.com"
        assert _safe_url_filter(url) == url

    def test_none_returns_fallback(self):
        assert _safe_url_filter(None) == "#"

    def test_empty_string_returns_fallback(self):
        assert _safe_url_filter("") == "#"

    def test_javascript_returns_fallback(self):
        assert _safe_url_filter("javascript:alert(1)") == "#"

    def test_data_uri_returns_fallback(self):
        assert _safe_url_filter("data:text/html,<h1>XSS</h1>") == "#"

    def test_custom_fallback(self):
        assert _safe_url_filter(None, fallback="/safe") == "/safe"

    def test_ftp_returns_fallback(self):
        assert _safe_url_filter("ftp://files.example.com") == "#"


class TestNowGlobal:
    def test_returns_datetime(self):
        result = _now()
        assert isinstance(result, datetime)

    def test_returns_utc(self):
        result = _now()
        assert result.tzinfo is not None

    def test_approximately_current_time(self):
        from datetime import timedelta

        before = datetime.now(UTC)
        result = _now()
        after = datetime.now(UTC)
        assert before <= result <= after + timedelta(seconds=1)


class TestCrmCompleteness:
    def test_company_dispatches_to_company_compl(self):
        from unittest.mock import MagicMock, patch

        from app.models.crm import Company

        company = MagicMock(spec=Company)
        with patch("app.template_env._company_compl", return_value={"score": 80}) as mock_c:
            from app.template_env import _crm_completeness

            result = _crm_completeness(company)
        mock_c.assert_called_once_with(company)
        assert result == {"score": 80}

    def test_non_company_dispatches_to_contact_compl(self):
        from unittest.mock import patch

        obj = SimpleNamespace(full_name="Jane Doe")
        with patch("app.template_env._contact_compl", return_value={"score": 60}) as mock_c:
            from app.template_env import _crm_completeness

            result = _crm_completeness(obj)
        mock_c.assert_called_once_with(obj)
        assert result == {"score": 60}
