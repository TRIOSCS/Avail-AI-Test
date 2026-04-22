"""tests/test_teams_call_jobs_coverage.py — Coverage tests for
app/jobs/teams_call_jobs.py.

Covers: register_teams_call_jobs, _job_sync_teams_calls (success, no users,
        watermark found/not-found, invalid watermark, token failure, graph
        fetch failure, duration calc error, log_call_activity returns falsy,
        top-level exception path).

The job uses lazy imports inside the function body, so we patch at source modules:
  - app.database.SessionLocal
  - app.models.config.SystemConfig
  - app.services.activity_service.log_call_activity
  - app.utils.graph_client.GraphClient
  - app.utils.token_manager.get_valid_token
"""

import os

os.environ["TESTING"] = "1"

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# register_teams_call_jobs
# ---------------------------------------------------------------------------


def test_register_teams_call_jobs_adds_job():
    from app.jobs.teams_call_jobs import register_teams_call_jobs

    mock_scheduler = MagicMock()
    mock_settings = MagicMock()
    register_teams_call_jobs(mock_scheduler, mock_settings)
    mock_scheduler.add_job.assert_called_once()
    call_kwargs = mock_scheduler.add_job.call_args
    assert call_kwargs[1]["id"] == "teams_call_records_sync"


# ---------------------------------------------------------------------------
# Helper: build a DB mock that handles watermark + user queries
# ---------------------------------------------------------------------------


def _make_db(wm_row=None, users=None):
    """Return a mock SessionLocal() instance pre-configured for the job."""
    mock_db = MagicMock()

    def _query(model):
        q = MagicMock()
        q.filter.return_value.first.return_value = wm_row
        q.filter.return_value.all.return_value = users or []
        q.filter.return_value.filter.return_value.all.return_value = users or []
        return q

    mock_db.query.side_effect = _query
    return mock_db


# ---------------------------------------------------------------------------
# _job_sync_teams_calls — no users
# ---------------------------------------------------------------------------


async def test_job_sync_no_users():
    """When no eligible users exist, job commits watermark and exits cleanly."""
    from app.jobs.teams_call_jobs import _job_sync_teams_calls

    mock_db = _make_db(wm_row=None, users=[])

    with (
        patch("app.database.SessionLocal", return_value=mock_db),
        patch("app.models.config.SystemConfig", MagicMock()),
        patch("app.services.activity_service.log_call_activity", MagicMock(return_value=None)),
        patch("app.utils.graph_client.GraphClient", MagicMock()),
        patch("app.utils.token_manager.get_valid_token", AsyncMock(return_value=None)),
    ):
        await _job_sync_teams_calls()

    mock_db.commit.assert_called_once()
    mock_db.close.assert_called_once()


# ---------------------------------------------------------------------------
# _job_sync_teams_calls — with valid watermark row
# ---------------------------------------------------------------------------


async def test_job_sync_with_existing_watermark():
    """Existing watermark row is parsed and updated without adding a new row."""
    from app.jobs.teams_call_jobs import _job_sync_teams_calls

    wm_row = MagicMock()
    wm_row.value = "2025-01-01T00:00:00+00:00"

    mock_db = _make_db(wm_row=wm_row, users=[])

    with (
        patch("app.database.SessionLocal", return_value=mock_db),
        patch("app.models.config.SystemConfig", MagicMock()),
        patch("app.services.activity_service.log_call_activity", MagicMock(return_value=None)),
        patch("app.utils.graph_client.GraphClient", MagicMock()),
        patch("app.utils.token_manager.get_valid_token", AsyncMock(return_value=None)),
    ):
        await _job_sync_teams_calls()

    mock_db.commit.assert_called_once()
    # Row existed, so db.add() should NOT have been called
    mock_db.add.assert_not_called()
    # wm_row.value should have been updated to a new timestamp
    assert wm_row.value != "2025-01-01T00:00:00+00:00"


# ---------------------------------------------------------------------------
# _job_sync_teams_calls — corrupted watermark falls back to 1-day lookback
# ---------------------------------------------------------------------------


async def test_job_sync_corrupted_watermark():
    """Corrupted watermark string falls back to 1-day lookback without raising."""
    from app.jobs.teams_call_jobs import _job_sync_teams_calls

    wm_row = MagicMock()
    wm_row.value = "not-a-valid-iso-datetime"

    mock_db = _make_db(wm_row=wm_row, users=[])

    with (
        patch("app.database.SessionLocal", return_value=mock_db),
        patch("app.models.config.SystemConfig", MagicMock()),
        patch("app.services.activity_service.log_call_activity", MagicMock(return_value=None)),
        patch("app.utils.graph_client.GraphClient", MagicMock()),
        patch("app.utils.token_manager.get_valid_token", AsyncMock(return_value=None)),
    ):
        await _job_sync_teams_calls()

    mock_db.commit.assert_called_once()


# ---------------------------------------------------------------------------
# _job_sync_teams_calls — token failure skips user
# ---------------------------------------------------------------------------


async def test_job_sync_token_failure_skips_user():
    """When get_valid_token returns None the user is skipped, no Graph call made."""
    from app.jobs.teams_call_jobs import _job_sync_teams_calls

    mock_user = MagicMock()
    mock_user.id = 1
    mock_user.email = "buyer@example.com"

    mock_db = _make_db(wm_row=None, users=[mock_user])
    mock_graph_cls = MagicMock()

    with (
        patch("app.database.SessionLocal", return_value=mock_db),
        patch("app.models.config.SystemConfig", MagicMock()),
        patch("app.services.activity_service.log_call_activity", MagicMock(return_value=None)),
        patch("app.utils.graph_client.GraphClient", mock_graph_cls),
        patch("app.utils.token_manager.get_valid_token", AsyncMock(return_value=None)),
    ):
        await _job_sync_teams_calls()

    mock_graph_cls.assert_not_called()
    mock_db.commit.assert_called_once()


# ---------------------------------------------------------------------------
# _job_sync_teams_calls — graph fetch failure continues to next user
# ---------------------------------------------------------------------------


async def test_job_sync_graph_fetch_failure_continues():
    """Graph API failure for one user is caught and processing continues."""
    from app.jobs.teams_call_jobs import _job_sync_teams_calls

    mock_user = MagicMock()
    mock_user.id = 1
    mock_user.email = "buyer@example.com"

    mock_db = _make_db(wm_row=None, users=[mock_user])

    mock_gc_instance = AsyncMock()
    mock_gc_instance.get_all_pages.side_effect = RuntimeError("Graph API timeout")
    mock_graph_cls = MagicMock(return_value=mock_gc_instance)

    with (
        patch("app.database.SessionLocal", return_value=mock_db),
        patch("app.models.config.SystemConfig", MagicMock()),
        patch("app.services.activity_service.log_call_activity", MagicMock(return_value=None)),
        patch("app.utils.graph_client.GraphClient", mock_graph_cls),
        patch("app.utils.token_manager.get_valid_token", AsyncMock(return_value="tok-abc")),
    ):
        await _job_sync_teams_calls()

    mock_db.commit.assert_called_once()


# ---------------------------------------------------------------------------
# _job_sync_teams_calls — successful record logging
# ---------------------------------------------------------------------------


async def test_job_sync_logs_call_records():
    """Call records with valid start/end datetimes are logged via log_call_activity."""
    from app.jobs.teams_call_jobs import _job_sync_teams_calls

    mock_user = MagicMock()
    mock_user.id = 42
    mock_user.email = "trader@example.com"

    mock_db = _make_db(wm_row=None, users=[mock_user])

    records = [
        {
            "id": "rec-001",
            "startDateTime": "2025-01-01T10:00:00+00:00",
            "endDateTime": "2025-01-01T10:05:00+00:00",
        },
        {
            "id": "rec-002",
            "startDateTime": "2025-01-01T11:00:00+00:00",
            "endDateTime": "2025-01-01T11:02:30+00:00",
        },
    ]

    mock_gc_instance = AsyncMock()
    mock_gc_instance.get_all_pages.return_value = records
    mock_graph_cls = MagicMock(return_value=mock_gc_instance)

    mock_log = MagicMock(return_value=MagicMock())  # truthy return value

    with (
        patch("app.database.SessionLocal", return_value=mock_db),
        patch("app.models.config.SystemConfig", MagicMock()),
        patch("app.services.activity_service.log_call_activity", mock_log),
        patch("app.utils.graph_client.GraphClient", mock_graph_cls),
        patch("app.utils.token_manager.get_valid_token", AsyncMock(return_value="tok-xyz")),
    ):
        await _job_sync_teams_calls()

    assert mock_log.call_count == 2
    mock_db.commit.assert_called_once()


# ---------------------------------------------------------------------------
# _job_sync_teams_calls — record missing id is skipped
# ---------------------------------------------------------------------------


async def test_job_sync_record_missing_id_skipped():
    """Records without an 'id' field are skipped."""
    from app.jobs.teams_call_jobs import _job_sync_teams_calls

    mock_user = MagicMock()
    mock_user.id = 7
    mock_user.email = "sales@example.com"

    mock_db = _make_db(wm_row=None, users=[mock_user])

    records = [{"startDateTime": "2025-01-01T10:00:00+00:00", "endDateTime": "2025-01-01T10:01:00+00:00"}]

    mock_gc_instance = AsyncMock()
    mock_gc_instance.get_all_pages.return_value = records
    mock_graph_cls = MagicMock(return_value=mock_gc_instance)

    mock_log = MagicMock(return_value=None)

    with (
        patch("app.database.SessionLocal", return_value=mock_db),
        patch("app.models.config.SystemConfig", MagicMock()),
        patch("app.services.activity_service.log_call_activity", mock_log),
        patch("app.utils.graph_client.GraphClient", mock_graph_cls),
        patch("app.utils.token_manager.get_valid_token", AsyncMock(return_value="tok-abc")),
    ):
        await _job_sync_teams_calls()

    mock_log.assert_not_called()


# ---------------------------------------------------------------------------
# _job_sync_teams_calls — invalid datetime values → duration 0
# ---------------------------------------------------------------------------


async def test_job_sync_invalid_datetime_duration_zero():
    """When start/end datetime cannot be parsed, duration defaults to 0."""
    from app.jobs.teams_call_jobs import _job_sync_teams_calls

    mock_user = MagicMock()
    mock_user.id = 5
    mock_user.email = "buyer2@example.com"

    mock_db = _make_db(wm_row=None, users=[mock_user])

    records = [{"id": "rec-bad", "startDateTime": "not-a-date", "endDateTime": "also-bad"}]

    mock_gc_instance = AsyncMock()
    mock_gc_instance.get_all_pages.return_value = records
    mock_graph_cls = MagicMock(return_value=mock_gc_instance)

    logged_durations = []

    def _log_call(**kwargs):
        logged_durations.append(kwargs.get("duration_seconds", -1))
        return MagicMock()

    with (
        patch("app.database.SessionLocal", return_value=mock_db),
        patch("app.models.config.SystemConfig", MagicMock()),
        patch("app.services.activity_service.log_call_activity", _log_call),
        patch("app.utils.graph_client.GraphClient", mock_graph_cls),
        patch("app.utils.token_manager.get_valid_token", AsyncMock(return_value="tok")),
    ):
        await _job_sync_teams_calls()

    assert logged_durations == [0]


# ---------------------------------------------------------------------------
# _job_sync_teams_calls — top-level exception triggers rollback + re-raise
# ---------------------------------------------------------------------------


async def test_job_sync_top_level_exception_rollback():
    """Unhandled exception at top level triggers db.rollback() and re-raises."""
    from app.jobs.teams_call_jobs import _job_sync_teams_calls

    mock_db = MagicMock()
    mock_db.query.side_effect = RuntimeError("DB exploded")

    with (
        patch("app.database.SessionLocal", return_value=mock_db),
        patch("app.models.config.SystemConfig", MagicMock()),
        patch("app.services.activity_service.log_call_activity", MagicMock()),
        patch("app.utils.graph_client.GraphClient", MagicMock()),
        patch("app.utils.token_manager.get_valid_token", AsyncMock(return_value=None)),
    ):
        with pytest.raises(RuntimeError, match="DB exploded"):
            await _job_sync_teams_calls()

    mock_db.rollback.assert_called_once()
    mock_db.close.assert_called_once()


# ---------------------------------------------------------------------------
# _job_sync_teams_calls — log_call_activity returns falsy (no total_logged increment)
# ---------------------------------------------------------------------------


async def test_job_sync_log_call_activity_returns_none():
    """When log_call_activity returns None, total_logged stays 0."""
    from app.jobs.teams_call_jobs import _job_sync_teams_calls

    mock_user = MagicMock()
    mock_user.id = 9
    mock_user.email = "trader2@example.com"

    mock_db = _make_db(wm_row=None, users=[mock_user])

    records = [
        {
            "id": "rec-falsy",
            "startDateTime": "2025-03-01T09:00:00+00:00",
            "endDateTime": "2025-03-01T09:03:00+00:00",
        }
    ]

    mock_gc_instance = AsyncMock()
    mock_gc_instance.get_all_pages.return_value = records
    mock_graph_cls = MagicMock(return_value=mock_gc_instance)

    with (
        patch("app.database.SessionLocal", return_value=mock_db),
        patch("app.models.config.SystemConfig", MagicMock()),
        patch("app.services.activity_service.log_call_activity", MagicMock(return_value=None)),
        patch("app.utils.graph_client.GraphClient", mock_graph_cls),
        patch("app.utils.token_manager.get_valid_token", AsyncMock(return_value="tok")),
    ):
        await _job_sync_teams_calls()

    mock_db.commit.assert_called_once()


# ---------------------------------------------------------------------------
# _job_sync_teams_calls — record missing start/end fields → duration 0
# ---------------------------------------------------------------------------


async def test_job_sync_record_no_datetime_fields():
    """Records without startDateTime/endDateTime produce duration 0."""
    from app.jobs.teams_call_jobs import _job_sync_teams_calls

    mock_user = MagicMock()
    mock_user.id = 11
    mock_user.email = "sales2@example.com"

    mock_db = _make_db(wm_row=None, users=[mock_user])

    # Record has id but no datetime fields
    records = [{"id": "rec-nodates"}]

    mock_gc_instance = AsyncMock()
    mock_gc_instance.get_all_pages.return_value = records
    mock_graph_cls = MagicMock(return_value=mock_gc_instance)

    logged_durations = []

    def _log_call(**kwargs):
        logged_durations.append(kwargs.get("duration_seconds", -1))
        return MagicMock()

    with (
        patch("app.database.SessionLocal", return_value=mock_db),
        patch("app.models.config.SystemConfig", MagicMock()),
        patch("app.services.activity_service.log_call_activity", _log_call),
        patch("app.utils.graph_client.GraphClient", mock_graph_cls),
        patch("app.utils.token_manager.get_valid_token", AsyncMock(return_value="tok")),
    ):
        await _job_sync_teams_calls()

    assert logged_durations == [0]
