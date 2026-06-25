"""tests/test_partsurfer_negative_cache.py -- the PartSurfer description negative-cache
selector + writer (partsurfer_desc_negative table).

Covers app/services/enrichment_worker/partsurfer_negative_cache.py:
- record_negative upserts one miss with the reason-correct retry window (no_result -> 90d,
  ungrammatical -> 14d) and refreshes an existing row in place (one row per spare_norm).
- blocked_spare_norms returns ONLY norms whose negative row is still fresh (retry_after in
  the future); a stale row is NOT blocked (so the worker re-fetches and the writer refreshes).
- a blank spare_norm is a no-op in both functions.

Depends on: conftest.py (db_session), app.models.PartsurferDescNegative.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.models import PartsurferDescNegative
from app.services.enrichment_worker.partsurfer_negative_cache import (
    PARTSURFER_NO_RESULT_RETRY_DAYS,
    PARTSURFER_UNGRAMMATICAL_RETRY_DAYS,
    blocked_spare_norms,
    record_negative,
)


def _now() -> datetime:
    return datetime(2026, 6, 19, 12, 0, 0, tzinfo=timezone.utc)


def test_record_no_result_uses_long_window(db_session: Session):
    now = _now()
    row = record_negative(db_session, "875942-001", "875942001", "no_result", now=now)
    db_session.commit()

    assert row is not None
    assert row.spare_norm == "875942001"
    assert row.spare_raw == "875942-001"
    assert row.reason == "no_result"
    assert row.looked_up_at == now
    assert row.retry_after == now + timedelta(days=PARTSURFER_NO_RESULT_RETRY_DAYS)


def test_record_ungrammatical_uses_short_window(db_session: Session):
    now = _now()
    row = record_negative(db_session, "918042-601", "918042601", "ungrammatical", now=now)
    db_session.commit()

    assert row.reason == "ungrammatical"
    # SHORT window -- a parse miss is not a permanent verdict, retried far sooner.
    assert row.retry_after == now + timedelta(days=PARTSURFER_UNGRAMMATICAL_RETRY_DAYS)
    assert PARTSURFER_UNGRAMMATICAL_RETRY_DAYS < PARTSURFER_NO_RESULT_RETRY_DAYS


def test_record_is_upsert_on_spare_norm(db_session: Session):
    now = _now()
    record_negative(db_session, "726719-B21", "726719b21", "ungrammatical", now=now)
    db_session.commit()
    later = now + timedelta(days=20)  # past the 14d ungrammatical window
    record_negative(db_session, "726719-B21", "726719b21", "no_result", now=later)
    db_session.commit()

    rows = db_session.query(PartsurferDescNegative).filter_by(spare_norm="726719b21").all()
    assert len(rows) == 1  # refreshed in place, never duplicated
    assert rows[0].reason == "no_result"
    assert rows[0].looked_up_at == later
    assert rows[0].retry_after == later + timedelta(days=PARTSURFER_NO_RESULT_RETRY_DAYS)


def test_record_blank_norm_is_noop(db_session: Session):
    assert record_negative(db_session, "  ", "  ", "no_result") is None
    assert db_session.query(PartsurferDescNegative).count() == 0


def test_blocked_returns_only_fresh_norms(db_session: Session):
    now = _now()
    record_negative(db_session, "AAA-1", "aaa1", "no_result", now=now)  # fresh (90d)
    # A stale row: looked up 100 days ago with the 90d window -> retry_after in the past.
    stale_lookup = now - timedelta(days=100)
    record_negative(db_session, "BBB-2", "bbb2", "no_result", now=stale_lookup)
    db_session.commit()

    blocked = blocked_spare_norms(db_session, ["aaa1", "bbb2", "never-seen"], now=now)
    assert blocked == {"aaa1"}  # fresh blocks; stale + never-seen do not


def test_blocked_ungrammatical_expires_after_short_window(db_session: Session):
    now = _now()
    record_negative(db_session, "CCC-3", "ccc3", "ungrammatical", now=now)
    db_session.commit()

    # Inside the 14d window -> blocked.
    assert blocked_spare_norms(db_session, ["ccc3"], now=now + timedelta(days=13)) == {"ccc3"}
    # Past the 14d window -> NOT blocked (eligible for a fresh re-fetch).
    assert blocked_spare_norms(db_session, ["ccc3"], now=now + timedelta(days=15)) == set()


def test_blocked_empty_input_is_empty(db_session: Session):
    assert blocked_spare_norms(db_session, []) == set()
    assert blocked_spare_norms(db_session, ["", None]) == set()
