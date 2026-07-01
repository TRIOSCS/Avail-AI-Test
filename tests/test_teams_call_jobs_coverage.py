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

import contextlib
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

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
# Helper: patch the job's lazy-imported dependencies at their source modules
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _patch_job_deps(mock_db, *, graph_client=None, log_call_activity=None, token=None):
    """Patch SessionLocal/SystemConfig/log_call_activity/GraphClient/get_valid_token.

    Mirrors the exact source-module targets the job lazily imports. Callers override
    only what varies; the rest default to inert mocks.
    """
    if graph_client is None:
        graph_client = MagicMock()
    if log_call_activity is None:
        log_call_activity = MagicMock(return_value=None)
    with (
        patch("app.database.SessionLocal", return_value=mock_db),
        patch("app.models.config.SystemConfig", MagicMock()),
        patch("app.services.activity_service.log_call_activity", log_call_activity),
        patch("app.utils.graph_client.GraphClient", graph_client),
        patch("app.utils.token_manager.get_valid_token", AsyncMock(return_value=token)),
    ):
        yield


# ---------------------------------------------------------------------------
# _job_sync_teams_calls — no users
# ---------------------------------------------------------------------------


async def test_job_sync_no_users():
    """When no eligible users exist, job commits watermark and exits cleanly."""
    from app.jobs.teams_call_jobs import _job_sync_teams_calls

    mock_db = _make_db(wm_row=None, users=[])

    with _patch_job_deps(mock_db):
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

    with _patch_job_deps(mock_db):
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

    with _patch_job_deps(mock_db):
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

    with _patch_job_deps(mock_db, graph_client=mock_graph_cls):
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

    with _patch_job_deps(mock_db, graph_client=mock_graph_cls, token="tok-abc"):
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

    with _patch_job_deps(mock_db, graph_client=mock_graph_cls, log_call_activity=mock_log, token="tok-xyz"):
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

    with _patch_job_deps(mock_db, graph_client=mock_graph_cls, log_call_activity=mock_log, token="tok-abc"):
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

    with _patch_job_deps(mock_db, graph_client=mock_graph_cls, log_call_activity=_log_call, token="tok"):
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

    with _patch_job_deps(mock_db):
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

    with _patch_job_deps(mock_db, graph_client=mock_graph_cls, token="tok"):
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

    with _patch_job_deps(mock_db, graph_client=mock_graph_cls, log_call_activity=_log_call, token="tok"):
        await _job_sync_teams_calls()

    assert logged_durations == [0]


# ---------------------------------------------------------------------------
# Real-DB watermark regression: a per-user fetch failure must NOT advance the
# single global watermark (else the failed user's window is lost forever), but a
# fully-successful run MUST advance it. Uses the in-memory SQLite DB (mocks hid this).
# ---------------------------------------------------------------------------

_OLD_WM = "2025-01-01T00:00:00+00:00"


def _seed_two_buyers_and_watermark(db: Session):
    """Insert two m365-connected buyers and a stale watermark row; return the users."""
    from app.models.auth import User
    from app.models.config import SystemConfig

    users = []
    for i in (1, 2):
        u = User(
            email=f"buyer{i}@trioscs.com",
            name=f"Buyer {i}",
            role="buyer",
            azure_id=f"az-wm-{i}",
            m365_connected=True,
            created_at=datetime.now(timezone.utc),
        )
        db.add(u)
        users.append(u)
    db.add(SystemConfig(key="teams_calls_last_poll", value=_OLD_WM, description="Teams call records last poll"))
    db.commit()
    for u in users:
        db.refresh(u)
    return users


@contextlib.contextmanager
def _patch_realdb_deps(db_session, *, fetch_by_email):
    """Run the job against the real test session.

    ``fetch_by_email`` maps a user's email to either a records list or an Exception
    instance to raise. get_valid_token returns the user's email as the token so the
    patched GraphClient can dispatch per user.
    """

    async def _get_token(user, _db):
        return user.email

    def _graph_ctor(token):
        gc = AsyncMock()
        outcome = fetch_by_email[token]

        async def _get_all_pages(*_a, **_kw):
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        gc.get_all_pages.side_effect = _get_all_pages
        return gc

    with (
        patch("app.database.SessionLocal", return_value=db_session),
        patch("app.utils.token_manager.get_valid_token", _get_token),
        patch("app.utils.graph_client.GraphClient", side_effect=_graph_ctor),
        # Prevent the job's finally: db.close() from closing our fixture session so we
        # can re-read the committed watermark afterwards.
        patch.object(db_session, "close", lambda: None),
    ):
        yield


def _read_watermark(db: Session) -> str:
    from app.models.config import SystemConfig

    db.expire_all()
    row = db.query(SystemConfig).filter(SystemConfig.key == "teams_calls_last_poll").first()
    return row.value


async def test_watermark_not_advanced_when_a_user_fetch_fails(db_session: Session):
    """One user's Graph fetch raises while another succeeds so watermark stays put."""
    from app.jobs.teams_call_jobs import _job_sync_teams_calls

    u1, u2 = _seed_two_buyers_and_watermark(db_session)
    fetch = {u1.email: RuntimeError("Graph timeout"), u2.email: []}

    with _patch_realdb_deps(db_session, fetch_by_email=fetch):
        await _job_sync_teams_calls()

    assert _read_watermark(db_session) == _OLD_WM, "failed user's window must be retried next run"


async def test_watermark_advanced_when_all_fetches_succeed(db_session: Session):
    """Fully-successful run advances the watermark past the prior value."""
    from app.jobs.teams_call_jobs import _job_sync_teams_calls

    u1, u2 = _seed_two_buyers_and_watermark(db_session)
    fetch = {u1.email: [], u2.email: []}

    with _patch_realdb_deps(db_session, fetch_by_email=fetch):
        await _job_sync_teams_calls()

    assert _read_watermark(db_session) != _OLD_WM, "clean run must advance the watermark"
