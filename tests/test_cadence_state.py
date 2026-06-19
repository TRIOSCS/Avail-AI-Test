from datetime import datetime, timedelta, timezone

from app.services.crm_service import cadence_state

NOW = datetime(2026, 6, 17, 12, 0, tzinfo=timezone.utc)


def _ago(days):
    return NOW - timedelta(days=days)


def test_never_contacted_is_new():
    assert cadence_state("key", None, now=NOW) == "new"


def test_key_green_amber_red():
    assert cadence_state("key", _ago(3), now=NOW) == "on_target"  # <=7
    assert cadence_state("key", _ago(10), now=NOW) == "due"  # 8..30
    assert cadence_state("key", _ago(31), now=NOW) == "overdue"  # >30


def test_standard_has_no_amber_band_then_red():
    assert cadence_state("standard", _ago(20), now=NOW) == "on_target"  # <=30
    assert cadence_state("standard", _ago(31), now=NOW) == "overdue"  # >30


def test_null_tier_defaults_to_standard():
    assert cadence_state(None, _ago(20), now=NOW) == "on_target"
    assert cadence_state(None, _ago(31), now=NOW) == "overdue"


def test_naive_datetime_is_treated_as_utc():
    assert cadence_state("core", _ago(20).replace(tzinfo=None), now=NOW) == "due"  # >14, <=30
