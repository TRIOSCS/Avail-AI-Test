"""Tests for app/constants.py StrEnum status fields."""


def test_api_source_status_strenum():
    """ApiSource.status takes one of these StrEnum values, written by
    health_monitor.ping_source.

    The enum lets type-checkers and IDE autocomplete catch typos like
    `status='errored'`.
    """
    from app.constants import ApiSourceStatus

    assert ApiSourceStatus.PENDING == "pending"
    assert ApiSourceStatus.LIVE == "live"
    assert ApiSourceStatus.ERROR == "error"
    assert ApiSourceStatus.DEGRADED == "degraded"
    assert ApiSourceStatus.DISABLED == "disabled"
    assert isinstance(ApiSourceStatus.LIVE.value, str)


def test_source_run_status_strenum():
    """source_stats[i]['status'] takes one of these.

    error_skipped is the new value introduced in 1dfec5b2 — health_monitor flipped this
    source to status='error' on a prior ping, so search_service skips it.
    """
    from app.constants import SourceRunStatus

    assert SourceRunStatus.OK == "ok"
    assert SourceRunStatus.ERROR == "error"
    assert SourceRunStatus.ERROR_SKIPPED == "error_skipped"
    assert SourceRunStatus.SKIPPED == "skipped"
    assert SourceRunStatus.DISABLED == "disabled"
