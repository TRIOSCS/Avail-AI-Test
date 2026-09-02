"""Company-timezone day-boundary tests for tasks (Decision O: ONE company zone).

Task due dates are calendar-date sentinels — UTC midnight of the picked date, with
``due.date()`` read back as the calendar day. Deciding "which calendar day is it
right now" must therefore run in the company operating zone
(``settings.company_timezone``), never raw UTC: at 8:30pm Eastern (01:30 UTC the
next day) "tomorrow" still means the next EASTERN day, and a task due today is not
overdue. Covers:

  - timezones.company_zoneinfo — reads settings.company_timezone at call time,
    invalid names fall back to the hardcoded business default.
  - timezones.company_day_sentinel — evening-Eastern instants resolve to the
    company-local date, DST-correct via zoneinfo (never a fixed offset).
  - task_service.snooze_task on an undated task — +1 day at 8:30pm Eastern lands
    on the company tomorrow, not the day after.
  - task_service.get_my_tasks_summary — "overdue" counts only tasks whose due
    DATE fell on a prior company-local day (due-today / due-tomorrow never count).
  - task_service.on_bid_due_soon — the auto task's due_at is the company-local
    tomorrow's sentinel.
  - template_env._task_due_state — the no-viewer-zone fallback bucket zone comes
    from settings.company_timezone (config-driven, not hardcoded).

Called by: pytest
Depends on: conftest.py (db_session, test_user, test_requisition), freezegun,
    app.utils.timezones, app.services.task_service, app.template_env, app.config.
"""

from __future__ import annotations

import os

os.environ["TESTING"] = "1"

from datetime import UTC, datetime

from freezegun import freeze_time
from sqlalchemy.orm import Session

from app.config import settings
from app.constants import TaskStatus
from app.models.task import RequisitionTask
from app.request_context import current_user_display_tz_var
from app.services.task_service import get_my_tasks_summary, on_bid_due_soon, snooze_task
from app.utils.timezones import as_utc

# 2026-01-15 01:30 UTC = Jan 14 20:30 EST — the evening-Eastern boundary window
# where the UTC calendar day has already rolled over but the company day has not.
_EVENING_EASTERN = "2026-01-15 01:30:00"


def _add_task(
    db: Session,
    *,
    user_id: int,
    req,
    title: str = "tz task",
    due_at: datetime | None = None,
) -> RequisitionTask:
    t = RequisitionTask(
        requisition_id=req.id,
        title=title,
        status=TaskStatus.TODO.value,
        priority=2,
        assigned_to_id=user_id,
        due_at=due_at,
        created_at=datetime.now(UTC),
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


# ── company_zoneinfo / company_day_sentinel helpers ─────────────────────


class TestCompanyDayHelpers:
    def test_company_zoneinfo_reads_setting_at_call_time(self, monkeypatch):
        from app.utils.timezones import company_zoneinfo

        monkeypatch.setattr(settings, "company_timezone", "Asia/Tokyo")
        assert company_zoneinfo().key == "Asia/Tokyo"

    def test_company_zoneinfo_invalid_name_falls_back_to_business_default(self, monkeypatch):
        from app.utils.timezones import company_zoneinfo

        monkeypatch.setattr(settings, "company_timezone", "Not/AZone")
        assert company_zoneinfo().key == "America/New_York"

    @freeze_time(_EVENING_EASTERN)
    def test_sentinel_evening_eastern_is_still_the_eastern_day(self):
        from app.utils.timezones import company_day_sentinel

        # 01:30 UTC = Jan 14 20:30 EST → company today = Jan 14, tomorrow = Jan 15.
        assert company_day_sentinel(0) == datetime(2026, 1, 14, tzinfo=UTC)
        assert company_day_sentinel(1) == datetime(2026, 1, 15, tzinfo=UTC)

    def test_sentinel_accepts_explicit_now(self):
        from app.utils.timezones import company_day_sentinel

        # Same boundary instant passed explicitly (naive = stored-UTC convention too).
        instant = datetime(2026, 1, 15, 1, 30, tzinfo=UTC)
        assert company_day_sentinel(0, now=instant) == datetime(2026, 1, 14, tzinfo=UTC)
        assert company_day_sentinel(3, now=instant.replace(tzinfo=None)) == datetime(2026, 1, 17, tzinfo=UTC)

    @freeze_time("2026-07-15 04:30:00")
    def test_sentinel_dst_uses_zoneinfo_not_fixed_offset(self):
        from app.utils.timezones import company_day_sentinel

        # July: Eastern is EDT (UTC-4), so 04:30 UTC = Jul 15 00:30 EDT → today is
        # Jul 15. A hardcoded winter offset (UTC-5) would say Jul 14 23:30 → Jul 14.
        assert company_day_sentinel(0) == datetime(2026, 7, 15, tzinfo=UTC)


# ── snooze_task: undated → company-local tomorrow sentinel ──────────────


class TestSnoozeCompanyDay:
    @freeze_time(_EVENING_EASTERN)
    def test_snooze_plus_one_day_lands_on_company_tomorrow(self, db_session, test_user, test_requisition):
        task = _add_task(db_session, user_id=test_user.id, req=test_requisition)
        snoozed = snooze_task(db_session, task.id, days=1)
        assert snoozed is not None
        # Company today = Jan 14 → tomorrow = Jan 15 sentinel. The UTC-day math this
        # replaces produced Jan 16 (day AFTER tomorrow Eastern).
        assert as_utc(snoozed.due_at) == datetime(2026, 1, 15, tzinfo=UTC)

    @freeze_time(_EVENING_EASTERN)
    def test_snooze_default_undated_lands_on_company_tomorrow(self, db_session, test_user, test_requisition):
        task = _add_task(db_session, user_id=test_user.id, req=test_requisition)
        snoozed = snooze_task(db_session, task.id)  # days=None default branch
        assert snoozed is not None
        assert as_utc(snoozed.due_at) == datetime(2026, 1, 15, tzinfo=UTC)

    @freeze_time(_EVENING_EASTERN)
    def test_snooze_dated_task_still_advances_by_exact_days(self, db_session, test_user, test_requisition):
        # Regression guard: a task that HAS a sentinel advances by pure day
        # arithmetic — tz-agnostic, unchanged by the company-day rework.
        task = _add_task(
            db_session,
            user_id=test_user.id,
            req=test_requisition,
            due_at=datetime(2026, 1, 20, tzinfo=UTC),
        )
        snoozed = snooze_task(db_session, task.id, days=3)
        assert snoozed is not None
        assert as_utc(snoozed.due_at) == datetime(2026, 1, 23, tzinfo=UTC)


# ── get_my_tasks_summary: overdue = prior company-local day ─────────────


class TestSummaryOverdueCompanyDay:
    @freeze_time(_EVENING_EASTERN)
    def test_overdue_counts_only_prior_company_days(self, db_session, test_user, test_requisition):
        # Company today = Jan 14 (Eastern). Jan 13 → overdue; Jan 14 → due today
        # (NOT overdue); Jan 15 → due tomorrow (NOT overdue — the raw
        # ``due_at < now(UTC)`` badge counted BOTH Jan 14 and Jan 15 here).
        _add_task(
            db_session,
            user_id=test_user.id,
            req=test_requisition,
            title="prior day",
            due_at=datetime(2026, 1, 13, tzinfo=UTC),
        )
        _add_task(
            db_session,
            user_id=test_user.id,
            req=test_requisition,
            title="due today",
            due_at=datetime(2026, 1, 14, tzinfo=UTC),
        )
        _add_task(
            db_session,
            user_id=test_user.id,
            req=test_requisition,
            title="due tomorrow",
            due_at=datetime(2026, 1, 15, tzinfo=UTC),
        )
        summary = get_my_tasks_summary(db_session, test_user.id)
        assert summary["overdue"] == 1
        assert summary["assigned_to_me"] == 3


# ── on_bid_due_soon: due the company-local tomorrow ─────────────────────


class TestBidDueSoonCompanyDay:
    @freeze_time(_EVENING_EASTERN)
    def test_bid_due_task_sentinel_is_company_tomorrow(self, db_session, test_requisition):
        task = on_bid_due_soon(db_session, test_requisition.id, "2026-01-16", "REQ-TEST-001")
        assert task is not None
        # Company tomorrow = Jan 15 sentinel — not the raw now+24h instant
        # (Jan 16 01:30 UTC), which rendered as due the day AFTER tomorrow.
        assert as_utc(task.due_at) == datetime(2026, 1, 15, tzinfo=UTC)


# ── _task_due_state fallback zone is config-driven ──────────────────────


class _FakeTask:
    def __init__(self, due_at):
        self.due_at = due_at


class TestDueStateFallbackZone:
    def test_fallback_bucket_zone_comes_from_company_setting(self, monkeypatch):
        from app.template_env import _task_due_state

        # No viewer zone in the contextvar; company zone = Tokyo. At 01:30 UTC it is
        # Jan 15 10:30 in Tokyo, so a Jan-15 sentinel is due TODAY — a hardcoded
        # America/New_York fallback would bucket it as a future day (False, False).
        monkeypatch.setattr(settings, "company_timezone", "Asia/Tokyo")
        token = current_user_display_tz_var.set(None)
        try:
            now = datetime(2026, 1, 15, 1, 30, tzinfo=UTC)
            state = _task_due_state(_FakeTask(datetime(2026, 1, 15, tzinfo=UTC)), now)
        finally:
            current_user_display_tz_var.reset(token)
        assert state == (False, True)
