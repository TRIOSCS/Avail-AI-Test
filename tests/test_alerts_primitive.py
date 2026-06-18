"""Tests for the cross-app alert primitive (Phase 0).

Covers the alert_seen read-state model, record_seen idempotency, the seen-set helper
(scoped by user + kind), recency-floor logic (rolling window + launch epoch), and the
registry (per-tab sum + fail-quiet on a broken source).
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.constants import AlertKind
from app.models.alert_seen import AlertSeen
from app.models.auth import User
from app.services.alerts import (
    AlertItem,
    AlertSource,
    Temperament,
    count_for_tab,
    record_seen,
    register,
    source_for_kind,
    sources_for_tab,
)
from app.services.alerts.base import recency_floor

# --- fake sources used by the registry + count tests -----------------------


class _FyiSource(AlertSource):
    key = "fake_tab_fyi"
    kind = AlertKind.OFFER_CONFIRMED
    temperament = Temperament.FYI
    _candidates = {1, 2, 3}

    def count_for_user(self, db, user):
        return len(self._candidates - self.seen_ref_ids(db, user))

    def new_items_for_user(self, db, user):
        unseen = self._candidates - self.seen_ref_ids(db, user)
        return [AlertItem(ref_id=r) for r in sorted(unseen)]


class _ActionSource(AlertSource):
    key = "fake_tab_action"
    kind = AlertKind.BUYPLAN_ACTION
    temperament = Temperament.ACTION

    def count_for_user(self, db, user):
        # ACTION: count derives from "work-state" (here a constant) — ignores seen.
        return 2

    def new_items_for_user(self, db, user):
        return [AlertItem(ref_id=10), AlertItem(ref_id=11)]


class _BrokenSource(AlertSource):
    key = "fake_tab_broken"
    kind = AlertKind.INBOUND_VENDOR
    temperament = Temperament.FYI

    def count_for_user(self, db, user):
        raise RuntimeError("boom")

    def new_items_for_user(self, db, user):
        return []


# --- model + helpers -------------------------------------------------------


def test_alert_seen_unique_constraint(db_session: Session, test_user: User):
    db_session.add(AlertSeen(user_id=test_user.id, alert_kind="offer_confirmed", ref_id=1))
    db_session.commit()
    db_session.add(AlertSeen(user_id=test_user.id, alert_kind="offer_confirmed", ref_id=1))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_record_seen_idempotent(db_session: Session, test_user: User):
    record_seen(db_session, test_user, AlertKind.OFFER_CONFIRMED, 42)
    record_seen(db_session, test_user, AlertKind.OFFER_CONFIRMED, 42)
    n = db_session.query(AlertSeen).filter_by(user_id=test_user.id, ref_id=42).count()
    assert n == 1


def test_seen_ref_ids_scoped_by_kind_and_user(db_session: Session, test_user: User, sales_user: User):
    record_seen(db_session, test_user, AlertKind.OFFER_CONFIRMED, 1)
    record_seen(db_session, test_user, AlertKind.INBOUND_CUSTOMER, 2)
    record_seen(db_session, sales_user, AlertKind.OFFER_CONFIRMED, 99)

    assert _FyiSource().seen_ref_ids(db_session, test_user) == {1}  # this user, this kind only


def test_recency_floor_uses_rolling_window(monkeypatch):
    monkeypatch.setattr(settings, "alert_recency_days", 30)
    monkeypatch.setattr(settings, "alerts_epoch", "")
    now = datetime(2026, 6, 18, tzinfo=timezone.utc)
    assert recency_floor(now) == now - timedelta(days=30)


def test_recency_floor_epoch_overrides_window(monkeypatch):
    monkeypatch.setattr(settings, "alert_recency_days", 30)
    monkeypatch.setattr(settings, "alerts_epoch", "2026-06-10T00:00:00+00:00")
    now = datetime(2026, 6, 18, tzinfo=timezone.utc)
    # epoch (Jun 10) is later than now-30d (May 19), so the epoch wins.
    assert recency_floor(now) == datetime(2026, 6, 10, tzinfo=timezone.utc)


# --- temperaments ----------------------------------------------------------


def test_fyi_count_excludes_seen(db_session: Session, test_user: User):
    src = _FyiSource()
    assert src.count_for_user(db_session, test_user) == 3
    record_seen(db_session, test_user, AlertKind.OFFER_CONFIRMED, 2)
    assert src.count_for_user(db_session, test_user) == 2
    assert [i.ref_id for i in src.new_items_for_user(db_session, test_user)] == [1, 3]


def test_action_count_ignores_seen(db_session: Session, test_user: User):
    src = _ActionSource()
    assert src.count_for_user(db_session, test_user) == 2
    record_seen(db_session, test_user, AlertKind.BUYPLAN_ACTION, 10)
    record_seen(db_session, test_user, AlertKind.BUYPLAN_ACTION, 11)
    # seen rows recorded (they gate the pulse), but the ACTION count is unchanged.
    assert src.count_for_user(db_session, test_user) == 2


# --- registry --------------------------------------------------------------


def test_registry_sums_sources_and_is_fail_quiet(db_session: Session, test_user: User):
    register("demo_tab", _FyiSource())
    register("demo_tab", _ActionSource())
    register("demo_tab", _BrokenSource())  # raises in count → must be swallowed
    assert {s.kind for s in sources_for_tab("demo_tab")} == {
        AlertKind.OFFER_CONFIRMED,
        AlertKind.BUYPLAN_ACTION,
        AlertKind.INBOUND_VENDOR,
    }
    # 3 (fyi) + 2 (action) + 0 (broken, swallowed) = 5
    assert count_for_tab(db_session, test_user, "demo_tab") == 5
    assert source_for_kind(AlertKind.OFFER_CONFIRMED) is not None
