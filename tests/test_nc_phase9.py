"""Tests for NC Phase 9: Monitoring + Daily Report.

Called by: pytest
Depends on: conftest.py, nc_worker.monitoring
"""

import pytest

from app.services.nc_worker.monitoring import (
    _get_hash_set,
    _known_html_hashes,
    check_html_structure_hash,
    log_daily_report,
)


@pytest.mark.parametrize(
    ("searches_completed", "sightings_created", "parts_gated_out", "parts_deduped", "queue_remaining"),
    [
        pytest.param(47, 312, 23, 11, 8, id="active"),
        pytest.param(0, 0, 0, 0, 0, id="zero_searches"),
    ],
)
def test_log_daily_report(searches_completed, sightings_created, parts_gated_out, parts_deduped, queue_remaining):
    """log_daily_report produces structured log output without raising.

    The zero-activity case confirms the report still emits when the worker is alive but
    idle.
    """
    log_daily_report(
        searches_completed=searches_completed,
        sightings_created=sightings_created,
        parts_gated_out=parts_gated_out,
        parts_deduped=parts_deduped,
        failed_searches=0,
        queue_remaining=queue_remaining,
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
