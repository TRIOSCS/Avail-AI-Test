"""tests/test_buyplan_handoff.py — Buy-plan handoff brief (AI queue P3-6).

Covers: DigestEntityType.BUY_PLAN, activity_service.get_buy_plan_activities,
services/buyplan_handoff.build_handoff_facts, and the BUY_PLAN branch of
activity_digest_service.get_or_build_digest.
"""

from datetime import UTC, datetime

from app.constants import DigestEntityType
from app.models.intelligence import ActivityLog
from app.services.activity_service import get_buy_plan_activities


def _mk_plan_activity(
    db, buy_plan_id, *, activity_type="sales_note", is_meaningful=True, buy_plan_line_id=None, subject=None, notes=None
):
    a = ActivityLog(
        activity_type=activity_type,
        channel="manual",
        buy_plan_id=buy_plan_id,
        buy_plan_line_id=buy_plan_line_id,
        subject=subject,
        notes=notes,
        is_meaningful=is_meaningful,
        occurred_at=datetime.now(UTC),
    )
    db.add(a)
    db.commit()
    return a


class TestBuyPlanActivities:
    def test_entity_type_value(self):
        assert DigestEntityType.BUY_PLAN.value == "buy_plan"

    def test_returns_plan_rows_newest_first(self, db_session, test_buy_plan):
        a1 = _mk_plan_activity(db_session, test_buy_plan.id, subject="first")
        a2 = _mk_plan_activity(db_session, test_buy_plan.id, subject="second")
        rows = get_buy_plan_activities(test_buy_plan.id, db_session)
        assert [r.id for r in rows][:2] == [a2.id, a1.id]

    def test_meaningful_only_hides_scored_false(self, db_session, test_buy_plan):
        _mk_plan_activity(db_session, test_buy_plan.id, subject="keep", is_meaningful=True)
        noisy = _mk_plan_activity(db_session, test_buy_plan.id, subject="noise", is_meaningful=False)
        rows = get_buy_plan_activities(test_buy_plan.id, db_session, meaningful_only=True)
        assert noisy.id not in [r.id for r in rows]
        rows_all = get_buy_plan_activities(test_buy_plan.id, db_session, meaningful_only=False)
        assert noisy.id in [r.id for r in rows_all]

    def test_other_plan_rows_excluded(self, db_session, test_buy_plan):
        _mk_plan_activity(db_session, test_buy_plan.id, subject="mine")
        rows = get_buy_plan_activities(test_buy_plan.id + 999, db_session)
        assert rows == []
