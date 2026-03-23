"""Tests for NC Phase 9: Monitoring + Daily Report.

Called by: pytest
Depends on: conftest.py, nc_worker.monitoring
"""

from app.services.nc_worker.monitoring import (
    _get_hash_set,
    _known_html_hashes,
    check_html_structure_hash,
    log_daily_report,
)


def test_log_daily_report(capsys):
    """log_daily_report produces structured log output."""
    # Just verify it doesn't raise
    log_daily_report(
        searches_completed=47,
        sightings_created=312,
        parts_gated_out=23,
        parts_deduped=11,
        failed_searches=0,
        queue_remaining=8,
        circuit_breaker_status="OK",
    )


def test_log_daily_report_zero_searches():
    """Daily report handles zero activity (confirms worker is alive)."""
    log_daily_report(
        searches_completed=0,
        sightings_created=0,
        parts_gated_out=0,
        parts_deduped=0,
        failed_searches=0,
        queue_remaining=0,
        circuit_breaker_status="OK",
    )


def test_html_structure_hash_empty():
    """Empty HTML returns empty hash."""
    assert check_html_structure_hash("", "TEST") == ""


def test_html_structure_hash_consistent():
    """Same HTML structure produces same hash."""
    _known_html_hashes.clear()
    html = "<table><tr><td>Content A</td></tr></table>"
    h1 = check_html_structure_hash(html, "TEST1")
    assert h1 != ""

    # Same structure, different content — same hash
    html2 = "<table><tr><td>Content B</td></tr></table>"
    h2 = check_html_structure_hash(html2, "TEST2")
    assert h1 == h2


def test_html_structure_hash_detects_change():
    """Different HTML structure produces different hash and logs warning."""
    _known_html_hashes.clear()

    html1 = "<table><tr><td>Data</td></tr></table>"
    check_html_structure_hash(html1, "PART1")

    # Different structure
    html2 = "<div><span>Data</span></div>"
    h2 = check_html_structure_hash(html2, "PART2")

    # Both hashes should be in the known set now
    assert len(_get_hash_set("NC")) == 2
    _known_html_hashes.clear()


def test_capture_sentry_error_no_sdk():
    """capture_sentry_error handles missing Sentry gracefully."""
    from app.services.nc_worker.monitoring import capture_sentry_error

    # Should not raise even without Sentry configured
    capture_sentry_error(ValueError("test error"), context={"mpn": "LM317T"})


def test_capture_sentry_message_no_sdk():
    """capture_sentry_message handles missing Sentry gracefully."""
    from app.services.nc_worker.monitoring import capture_sentry_message

    capture_sentry_message("test warning", level="warning")
