"""tests/test_search_worker_monitoring.py — Tests for
app/services/search_worker_base/monitoring.py."""

import os

os.environ["TESTING"] = "1"

from unittest.mock import patch

from app.services.search_worker_base.monitoring import (
    _get_hash_set,
    _known_html_hashes,
    capture_sentry_error,
    capture_sentry_message,
    check_html_structure_hash,
    log_daily_report,
)


class TestGetHashSet:
    def test_creates_new_set(self):
        _known_html_hashes.pop("TEST_COMPONENT_NEW", None)
        result = _get_hash_set("TEST_COMPONENT_NEW")
        assert isinstance(result, set)

    def test_returns_same_set_each_call(self):
        _known_html_hashes.pop("TEST_COMPONENT_SAME", None)
        s1 = _get_hash_set("TEST_COMPONENT_SAME")
        s2 = _get_hash_set("TEST_COMPONENT_SAME")
        assert s1 is s2

    def test_different_components_get_different_sets(self):
        _known_html_hashes.pop("COMP_A", None)
        _known_html_hashes.pop("COMP_B", None)
        s1 = _get_hash_set("COMP_A")
        s2 = _get_hash_set("COMP_B")
        s1.add("hash1")
        assert "hash1" not in s2


class TestLogDailyReport:
    def test_logs_without_error(self):
        with patch("app.services.search_worker_base.monitoring.logger") as mock_log:
            log_daily_report(
                searches_completed=100,
                sightings_created=50,
                parts_gated_out=10,
                parts_deduped=5,
                failed_searches=2,
                queue_remaining=20,
                circuit_breaker_status="HEALTHY",
                component_name="TEST",
            )
            mock_log.info.assert_called_once()
            msg = mock_log.info.call_args[0][0]
            assert "100" in msg
            assert "50" in msg
            assert "TEST" in msg


class TestCaptureSentryError:
    def test_logs_when_sentry_unavailable(self):
        with patch("app.services.search_worker_base.monitoring.logger") as mock_log:
            with patch("app.services.search_worker_base.monitoring._sentry_scope") as mock_scope:
                mock_scope.side_effect = ImportError("no sentry")
                error = ValueError("test error")
                capture_sentry_error(error, component_name="TEST")
                mock_log.warning.assert_called_once()


class TestCaptureSentryMessage:
    def test_logs_when_sentry_unavailable(self):
        with patch("app.services.search_worker_base.monitoring.logger") as mock_log:
            with patch("app.services.search_worker_base.monitoring._sentry_scope") as mock_scope:
                mock_scope.side_effect = ImportError("no sentry")
                capture_sentry_message("test message", component_name="TEST")
                mock_log.warning.assert_called_once()


class TestCheckHtmlStructureHash:
    def setup_method(self):
        # Reset the hash set for our test component
        _known_html_hashes.pop("HASH_TEST", None)

    def test_empty_html_returns_empty(self):
        result = check_html_structure_hash("", "LM317T", component_name="HASH_TEST")
        assert result == ""

    def test_none_html_returns_empty(self):
        result = check_html_structure_hash(None, "LM317T", component_name="HASH_TEST")
        assert result == ""

    def test_returns_hash_string(self):
        html = "<table><tr><td>data</td></tr></table>"
        result = check_html_structure_hash(html, "LM317T", component_name="HASH_TEST")
        assert isinstance(result, str)
        assert len(result) == 16

    def test_same_structure_same_hash(self):
        html1 = "<table><tr><td>value1</td></tr></table>"
        html2 = "<table><tr><td>value2</td></tr></table>"
        # Clear before
        _known_html_hashes.pop("HASH_TEST_SAME", None)
        h1 = check_html_structure_hash(html1, "PART1", component_name="HASH_TEST_SAME")
        h2 = check_html_structure_hash(html2, "PART2", component_name="HASH_TEST_SAME")
        assert h1 == h2

    def test_different_structure_different_hash(self):
        html1 = "<table><tr><td>val</td></tr></table>"
        html2 = "<div><span>val</span></div>"
        _known_html_hashes.pop("HASH_TEST_DIFF", None)
        h1 = check_html_structure_hash(html1, "PART1", component_name="HASH_TEST_DIFF")
        h2 = check_html_structure_hash(html2, "PART2", component_name="HASH_TEST_DIFF")
        assert h1 != h2

    def test_hash_added_to_set(self):
        _known_html_hashes.pop("HASH_TEST_SET", None)
        html = "<div>content</div>"
        h = check_html_structure_hash(html, "PART1", component_name="HASH_TEST_SET")
        assert h in _known_html_hashes["HASH_TEST_SET"]

    def test_attributes_ignored_same_hash(self):
        """Same tag structure with DIFFERENT attribute values hashes identically.

        This is the false-"layout changed" churn the strip fixes: per-row class/id/
        data-* values must not register as a structure change.
        """
        _known_html_hashes.pop("HASH_TEST_ATTR", None)
        html1 = '<table><tr class="row-1" data-mpn="A"><td style="width:10px">x</td></tr></table>'
        html2 = '<table><tr class="row-2" data-mpn="B"><td style="width:99px">y</td></tr></table>'
        h1 = check_html_structure_hash(html1, "PART1", component_name="HASH_TEST_ATTR")
        h2 = check_html_structure_hash(html2, "PART2", component_name="HASH_TEST_ATTR")
        assert h1 == h2

    def test_real_layout_change_still_detected(self):
        """A genuine tag-sequence change (table → list) still produces a new hash."""
        _known_html_hashes.pop("HASH_TEST_REAL", None)
        h1 = check_html_structure_hash('<table><tr><td class="a">x</td></tr></table>', "P1", "HASH_TEST_REAL")
        h2 = check_html_structure_hash('<ul><li class="a">x</li></ul>', "P2", "HASH_TEST_REAL")
        assert h1 != h2

    def test_hash_set_is_bounded(self):
        """The stored hash set never exceeds the cap, even under many distinct
        structures."""
        import app.services.search_worker_base.monitoring as mon

        _known_html_hashes.pop("HASH_TEST_CAP", None)
        # Feed far more distinct tag structures than the cap by growing a nesting chain.
        for i in range(mon._MAX_STRUCTURE_HASHES * 3):
            html = "<div>" * i + "content" + "</div>" * i
            check_html_structure_hash(html, f"PART{i}", component_name="HASH_TEST_CAP")
        assert len(_known_html_hashes["HASH_TEST_CAP"]) <= mon._MAX_STRUCTURE_HASHES
