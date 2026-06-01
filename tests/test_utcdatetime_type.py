"""tests/test_utcdatetime_type.py -- Pin UTCDateTime's symmetric UTC behavior.

Covers: app/database.py ``UTCDateTime`` (``process_bind_param`` +
``process_result_value`` + ``load_dialect_impl``). Ensures naive writes become
aware UTC, non-UTC aware writes are converted to UTC, reads are always aware,
arithmetic against ``datetime.now(timezone.utc)`` never raises, and ``isoformat``
carries the ``+00:00`` offset (the user-facing serialization contract).
Depends on: tests/conftest.py (in-memory SQLite engine).
"""

from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

from app.database import UTCDateTime
from tests.conftest import engine  # noqa: F401

_md = sa.MetaData()
_probe = sa.Table(
    "_utc_probe",
    _md,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("ts", UTCDateTime),
)


@pytest.fixture()
def probe_conn():
    _md.create_all(engine)
    conn = engine.connect()
    try:
        yield conn
    finally:
        conn.close()
        _md.drop_all(engine)


def _store_fetch(conn, value):
    conn.execute(_probe.delete())
    conn.execute(_probe.insert().values(id=1, ts=value))
    conn.commit()
    return conn.execute(sa.select(_probe.c.ts)).scalar_one()


def test_naive_write_reads_back_aware_utc(probe_conn):
    """A naive value is assumed UTC and read back tz-aware with the same wall clock."""
    got = _store_fetch(probe_conn, datetime(2026, 3, 1, 12, 0, 0))
    assert got.tzinfo is not None
    assert got.utcoffset() == timedelta(0)
    assert (got.year, got.month, got.day, got.hour) == (2026, 3, 1, 12)


def test_non_utc_aware_write_is_converted_to_utc(probe_conn):
    """An aware value in another zone is normalized to UTC on write (12:00+05:00 ->
    07:00Z)."""
    tz_plus5 = timezone(timedelta(hours=5))
    got = _store_fetch(probe_conn, datetime(2026, 3, 1, 12, 0, 0, tzinfo=tz_plus5))
    assert got.utcoffset() == timedelta(0)
    assert got.hour == 7


def test_aware_roundtrip_allows_arithmetic_with_now(probe_conn):
    """Read values support subtraction with ``datetime.now(timezone.utc)`` (no
    naive/aware TypeError)."""
    stored = datetime.now(timezone.utc) - timedelta(hours=2)
    got = _store_fetch(probe_conn, stored)
    assert got.tzinfo is not None
    delta = datetime.now(timezone.utc) - got  # must not raise
    assert timedelta(hours=1) < delta < timedelta(hours=3)


def test_isoformat_carries_utc_offset(probe_conn):
    """Serialization includes the +00:00 offset (frontend/JSON contract)."""
    got = _store_fetch(probe_conn, datetime(2026, 3, 1, 12, 0, 0))
    assert got.isoformat().endswith("+00:00")


def test_none_passes_through(probe_conn):
    """NULL datetimes round-trip as None without error."""
    assert _store_fetch(probe_conn, None) is None
