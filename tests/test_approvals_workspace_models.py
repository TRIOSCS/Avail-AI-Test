"""tests/test_approvals_workspace_models.py — Phase 0.1 Approvals Workspace foundations.

Covers: the new constants (SalesOrderType, SOURCING_ORDER_TYPES, PaymentMethod ACH/COD,
PO_LINE_PAYMENT_METHODS / PREPAYMENT_METHODS, ActivityType additions, KanbanLane), the
new ORM columns on BuyPlan / BuyPlanLine / QualityPlan / ActivityLog, the
BuyPlanAttachment model with its exactly-one-subject validation, and the single-head
invariant of migration 196.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from app.constants import (
    PO_LINE_PAYMENT_METHODS,
    PREPAYMENT_METHODS,
    SOURCING_ORDER_TYPES,
    ActivityType,
    KanbanLane,
    PaymentMethod,
    SalesOrderType,
)
from app.models.auth import User
from app.models.buy_plan import BuyPlanAttachment
from app.models.intelligence import ActivityLog
from app.models.quality_plan import Prepayment, QualityPlan
from tests.conftest import _buyplan_line as _line
from tests.conftest import _buyplan_plan as _plan
from tests.conftest import _buyplan_req as _req

# ── Constants ──────────────────────────────────────────────────────────


class TestSalesOrderType:
    def test_members_and_values(self):
        assert {t.value for t in SalesOrderType} == {
            "new",
            "revision",
            "testing_service",
            "comps",
            "stock_sale",
        }

    def test_all_values_fit_string_20(self):
        for member in SalesOrderType:
            assert len(member.value) <= 20, member

    def test_sourcing_order_types(self):
        assert SOURCING_ORDER_TYPES == {SalesOrderType.NEW, SalesOrderType.REVISION}

    def test_distinct_from_qp_order_type(self):
        from app.constants import QPOrderType

        assert SalesOrderType.NEW is not QPOrderType.NEW  # separate vocabularies


class TestPaymentMethodLists:
    def test_ach_and_cod_added(self):
        assert PaymentMethod.ACH == "ach"
        assert PaymentMethod.COD == "cod"

    def test_po_line_methods_has_all_five(self):
        assert len(PO_LINE_PAYMENT_METHODS) == 5
        assert set(PO_LINE_PAYMENT_METHODS) == set(PaymentMethod)

    def test_prepayment_methods_excludes_cod(self):
        assert PaymentMethod.COD not in PREPAYMENT_METHODS
        assert set(PREPAYMENT_METHODS) == {
            PaymentMethod.CC,
            PaymentMethod.PAYPAL,
            PaymentMethod.WIRE,
            PaymentMethod.ACH,
        }


class TestActivityTypeAdditions:
    @pytest.mark.parametrize(
        "member",
        [
            ActivityType.FIELD_EDIT,
            ActivityType.LINE_RECEIVED,
            ActivityType.ATTACH_ADDED,
            ActivityType.ATTACH_REMOVED,
        ],
    )
    def test_new_members_fit_string_20(self, member):
        assert len(member.value) <= 20, member


class TestKanbanLane:
    def test_members(self):
        assert {lane.name for lane in KanbanLane} == {
            "AWAITING_PO",
            "PENDING_APPROVAL",
            "PAID_AWAITING_DELIVERY",
            "APPROVED",
            "RECEIVED",
            "RESOURCING",
        }


# ── BuyPlan.order_type ─────────────────────────────────────────────────


class TestBuyPlanOrderType:
    def test_defaults_to_new(self, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        plan = _plan(db_session, req)
        assert plan.order_type == SalesOrderType.NEW.value

    def test_accepts_every_sales_order_type(self, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        for order_type in SalesOrderType:
            plan = _plan(db_session, req, order_type=order_type.value)
            assert plan.order_type == order_type.value

    def test_rejects_invalid_order_type(self, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        with pytest.raises(ValueError, match="Invalid order type"):
            _plan(db_session, req, order_type="bogus")


# ── BuyPlanLine payment_method + receiving ─────────────────────────────


class TestBuyPlanLinePaymentAndReceiving:
    def test_payment_method_accepts_all_po_line_methods(self, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        plan = _plan(db_session, req)
        for method in PO_LINE_PAYMENT_METHODS:
            line = _line(db_session, plan, payment_method=method.value)
            assert line.payment_method == method.value

    def test_payment_method_rejects_invalid(self, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        plan = _plan(db_session, req)
        with pytest.raises(ValueError, match="Invalid payment method"):
            _line(db_session, plan, payment_method="check")

    def test_payment_method_nullable(self, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        plan = _plan(db_session, req)
        line = _line(db_session, plan)
        assert line.payment_method is None

    def test_is_received_property(self, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        plan = _plan(db_session, req)
        line = _line(db_session, plan)
        assert line.is_received is False
        line.received_at = datetime.now(UTC)
        line.received_by_id = test_user.id
        db_session.commit()
        db_session.refresh(line)
        assert line.is_received is True
        assert line.received_by.id == test_user.id


# ── QualityPlan AS9120B purchasing columns ─────────────────────────────


class TestQualityPlanAS9120BColumns:
    def test_columns_persist(self, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        plan = _plan(db_session, req)
        qp = QualityPlan(
            buy_plan_id=plan.id,
            purchasing_traceability_verified=True,
            purchasing_counterfeit_risk="low",
            purchasing_risk_level="low",
            purchasing_coc_available=False,
            purchasing_vendor_rating="A - preferred",
            purchasing_sn_previously_received=True,
            purchasing_serial_numbers="SN001\nSN002",
        )
        db_session.add(qp)
        db_session.commit()
        db_session.refresh(qp)
        assert qp.purchasing_traceability_verified is True
        assert qp.purchasing_counterfeit_risk == "low"
        assert qp.purchasing_risk_level == "low"
        assert qp.purchasing_coc_available is False
        assert qp.purchasing_vendor_rating == "A - preferred"
        assert qp.purchasing_sn_previously_received is True
        assert qp.purchasing_serial_numbers == "SN001\nSN002"

    def test_columns_default_null(self, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        plan = _plan(db_session, req)
        qp = QualityPlan(buy_plan_id=plan.id)
        db_session.add(qp)
        db_session.commit()
        db_session.refresh(qp)
        assert qp.purchasing_traceability_verified is None
        assert qp.purchasing_serial_numbers is None


# ── ActivityLog audit FKs ──────────────────────────────────────────────


class TestActivityLogAuditFKs:
    def test_accepts_buy_plan_line_and_prepayment_ids(self, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        plan = _plan(db_session, req)
        line = _line(db_session, plan)
        prepayment = Prepayment(buy_plan_id=plan.id, buy_plan_line_id=line.id, total_incl_fees=1000)
        db_session.add(prepayment)
        db_session.commit()

        row = ActivityLog(
            user_id=test_user.id,
            activity_type=ActivityType.FIELD_EDIT,
            channel="system",
            buy_plan_id=plan.id,
            buy_plan_line_id=line.id,
            prepayment_id=prepayment.id,
            summary="Edited quantity",
            details={"edits": [{"field": "quantity", "old": "100", "new": "200"}]},
        )
        db_session.add(row)
        db_session.commit()
        db_session.refresh(row)
        assert row.buy_plan_line_id == line.id
        assert row.prepayment_id == prepayment.id


# ── BuyPlanAttachment ──────────────────────────────────────────────────


class TestBuyPlanAttachment:
    def _subjects(self, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        plan = _plan(db_session, req)
        line = _line(db_session, plan)
        prepayment = Prepayment(buy_plan_id=plan.id, buy_plan_line_id=line.id, total_incl_fees=500)
        db_session.add(prepayment)
        db_session.commit()
        return plan, line, prepayment

    def test_attach_to_each_subject(self, db_session: Session, test_user: User):
        plan, line, prepayment = self._subjects(db_session, test_user)
        for kwargs in (
            {"buy_plan_id": plan.id},
            {"buy_plan_line_id": line.id},
            {"prepayment_id": prepayment.id},
        ):
            attachment = BuyPlanAttachment(file_name="po.pdf", uploaded_by_id=test_user.id, **kwargs)
            attachment.validate_subject()  # must not raise
            db_session.add(attachment)
        db_session.commit()
        assert db_session.query(BuyPlanAttachment).count() == 3

    def test_validate_subject_rejects_zero_subjects(self):
        attachment = BuyPlanAttachment(file_name="po.pdf")
        with pytest.raises(ValueError, match="exactly one subject"):
            attachment.validate_subject()

    def test_validate_subject_rejects_two_subjects(self, db_session: Session, test_user: User):
        plan, line, _ = self._subjects(db_session, test_user)
        attachment = BuyPlanAttachment(file_name="po.pdf", buy_plan_id=plan.id, buy_plan_line_id=line.id)
        with pytest.raises(ValueError, match="exactly one subject"):
            attachment.validate_subject()

    def test_relationships_resolve(self, db_session: Session, test_user: User):
        plan, _, _ = self._subjects(db_session, test_user)
        attachment = BuyPlanAttachment(file_name="quote.pdf", buy_plan_id=plan.id, uploaded_by_id=test_user.id)
        db_session.add(attachment)
        db_session.commit()
        db_session.refresh(attachment)
        assert attachment.buy_plan.id == plan.id
        assert attachment.uploaded_by.id == test_user.id


# ── Migration 192 invariants ───────────────────────────────────────────


class TestMigration192:
    def test_single_alembic_head_is_192(self):
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        cfg = Config("alembic.ini")
        heads = ScriptDirectory.from_config(cfg).get_heads()
        assert heads == ["196_approvals_workspace_foundations"]

    def test_downgrade_reverses_every_upgrade_object(self):
        """Every column/index/constraint/table added in upgrade() is dropped in
        downgrade()."""
        src = open("alembic/versions/196_approvals_workspace_foundations.py").read()
        upgrade_body = src.split("def upgrade()")[1].split("def downgrade()")[0]
        downgrade_body = src.split("def downgrade()")[1]
        import re

        added_columns = re.findall(r'add_column\(\s*"(\w+)",\s*sa\.Column\("(\w+)"', upgrade_body)
        for table, column in added_columns:
            assert f'op.drop_column("{table}", "{column}")' in downgrade_body, (table, column)
        for index in re.findall(r'create_index\(\s*"(\w+)"', upgrade_body):
            assert f'"{index}"' in downgrade_body, index
        for fk in re.findall(r'create_foreign_key\(\s*"(\w+)"', upgrade_body):
            assert f'op.drop_constraint("{fk}"' in downgrade_body, fk
        assert 'op.drop_table("buy_plan_attachments"' in downgrade_body
