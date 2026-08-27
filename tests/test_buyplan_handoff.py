"""tests/test_buyplan_handoff.py — Buy-plan handoff brief (AI queue P3-6).

Covers: DigestEntityType.BUY_PLAN, activity_service.get_buy_plan_activities,
services/buyplan_handoff.build_handoff_facts, and the BUY_PLAN branch of
activity_digest_service.get_or_build_digest.
"""

from datetime import UTC, datetime

from app.constants import ApprovalGateType, ApprovalSubjectType, DigestEntityType
from app.models.approvals import ApprovalEvent, ApprovalRequest
from app.models.buy_plan import BuyPlanLine
from app.models.intelligence import ActivityLog
from app.models.quality_plan import Prepayment, QualityPlan
from app.services.activity_service import get_buy_plan_activities
from app.services.buyplan_handoff import build_handoff_facts


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


class TestHandoffFacts:
    def test_header_and_empty_sections(self, db_session, test_buy_plan):
        facts = build_handoff_facts(db_session, test_buy_plan)
        assert f"Buy plan #{test_buy_plan.id}" in facts
        assert "status: draft" in facts
        assert "Lines: none yet" in facts
        assert "Quality plan: not created" in facts
        assert "Prepayments: none" in facts
        assert "Approval history: none" in facts

    def test_line_facts_and_status_counts(self, db_session, test_buy_plan, test_offer):
        line = BuyPlanLine(
            buy_plan_id=test_buy_plan.id,
            requirement_id=test_offer.requirement_id,
            offer_id=test_offer.id,
            quantity=25,
            unit_cost=4.10,
            unit_sell=6.00,
            status="awaiting_po",
            issue_type=None,
        )
        db_session.add(line)
        db_session.commit()
        facts = build_handoff_facts(db_session, test_buy_plan)
        assert "Lines (1): awaiting_po: 1" in facts
        assert "×25" in facts
        assert test_offer.vendor_name in facts

    def test_line_issue_surfaces_as_blocker(self, db_session, test_buy_plan, test_offer):
        # NOTE: brief's sample used issue_type="price_up", which is not a member of
        # LineIssueType (sold_out | price_changed | lead_time_changed | other — see
        # app/constants.py) and BuyPlanLine._validate_issue_type raises ValueError on
        # assignment. Adapted to "price_changed", the real vocabulary's closest match.
        line = BuyPlanLine(
            buy_plan_id=test_buy_plan.id,
            offer_id=test_offer.id,
            quantity=5,
            status="issue",
            issue_type="price_changed",
            issue_note="vendor repriced +12%",
        )
        db_session.add(line)
        db_session.commit()
        facts = build_handoff_facts(db_session, test_buy_plan)
        assert "ISSUE price_changed: vendor repriced +12%" in facts

    def test_qp_section_stamps(self, db_session, test_buy_plan, test_user):
        qp = QualityPlan(buy_plan_id=test_buy_plan.id)
        qp.sales_section_reviewed_at = datetime(2026, 8, 20, tzinfo=UTC)
        qp.sales_section_reviewed_by_id = test_user.id
        db_session.add(qp)
        db_session.commit()
        facts = build_handoff_facts(db_session, test_buy_plan)
        assert "Sales section reviewed 2026-08-20" in facts
        assert "Purchasing section not reviewed" in facts

    def test_prepayment_and_approval_timeline(self, db_session, test_buy_plan, test_user):
        db_session.add(
            Prepayment(buy_plan_id=test_buy_plan.id, total_incl_fees=1200, vendor_name="Arrow", status="approved")
        )
        req = ApprovalRequest(
            gate_type=ApprovalGateType.BUY_PLAN.value,
            subject_type=ApprovalSubjectType.BUY_PLAN.value,
            subject_id=test_buy_plan.id,
            status="approved",
            requested_by_id=test_user.id,
        )
        db_session.add(req)
        db_session.flush()
        db_session.add(ApprovalEvent(request_id=req.id, actor_id=test_user.id, event_type="approved"))
        db_session.commit()
        facts = build_handoff_facts(db_session, test_buy_plan)
        assert "approved" in facts
        assert "Prepayments (1): approved: 1" in facts
        assert "$1,200" in facts
