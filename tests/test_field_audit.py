"""tests/test_field_audit.py — Tests for app/services/field_audit.py (Phase 0.2).

Covers: _stringify normalization (dates → ISO UTC, Decimal → str, bool → yes/no,
None → ""), diff_fields change detection, log_field_edits ONE-row batching +
empty no-op, edits_since flattening/filtering, manager_edited_line_ids role
filtering, and the additive buy_plan_line_id / prepayment_id kwargs on
log_activity.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from app.constants import ActivityType, UserRole
from app.models.auth import User
from app.models.intelligence import ActivityLog
from app.models.quality_plan import Prepayment
from app.services.activity_service import log_activity
from app.services.field_audit import (
    FieldEdit,
    _stringify,
    diff_fields,
    edits_since,
    log_field_edits,
    manager_edited_line_ids,
)
from tests.conftest import _buyplan_line as _line
from tests.conftest import _buyplan_plan as _plan
from tests.conftest import _buyplan_req as _req


class TestStringify:
    def test_none_is_empty_string(self):
        assert _stringify(None) == ""

    def test_bool_is_yes_no(self):
        assert _stringify(True) == "yes"
        assert _stringify(False) == "no"

    def test_aware_datetime_is_iso_utc(self):
        dt = datetime(2026, 7, 17, 12, 30, tzinfo=UTC)
        assert _stringify(dt) == "2026-07-17T12:30:00+00:00"

    def test_naive_datetime_assumed_utc_same_iso(self):
        # SQLite hands back naive datetimes, PG aware ones — both must serialize
        # identically (risk 8: same isoformat serializer both sides).
        naive = datetime(2026, 7, 17, 12, 30)
        aware = datetime(2026, 7, 17, 12, 30, tzinfo=UTC)
        assert _stringify(naive) == _stringify(aware)

    def test_date_is_iso(self):
        assert _stringify(date(2026, 7, 17)) == "2026-07-17"

    def test_decimal_is_str(self):
        assert _stringify(Decimal("12.4000")) == "12.4000"

    def test_plain_values(self):
        assert _stringify(100) == "100"
        assert _stringify("PO-1") == "PO-1"


class TestDiffFields:
    def test_detects_changes_only(self, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        plan = _plan(db_session, req)
        line = _line(db_session, plan, po_number="PO-1")
        edits = diff_fields(line, {"quantity": 200, "po_number": "PO-1", "sales_note": "rush"})
        by_field = {e.field: e for e in edits}
        assert set(by_field) == {"quantity", "sales_note"}  # po_number unchanged
        assert by_field["quantity"].old == "100"
        assert by_field["quantity"].new == "200"
        assert by_field["sales_note"].old == ""  # None → ""

    def test_no_changes_returns_empty(self, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        plan = _plan(db_session, req)
        line = _line(db_session, plan)
        assert diff_fields(line, {"quantity": 100}) == []

    def test_equal_decimals_are_not_a_change(self, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        plan = _plan(db_session, req)
        line = _line(db_session, plan, unit_cost=Decimal("1.0000"))
        assert diff_fields(line, {"unit_cost": Decimal("1.0000")}) == []


class TestLogFieldEdits:
    def test_writes_one_row_with_batched_edits(self, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        plan = _plan(db_session, req)
        line = _line(db_session, plan)
        edits = [
            FieldEdit(field="quantity", old="100", new="200"),
            FieldEdit(field="unit_cost", old="1.00", new="1.25"),
        ]
        record = log_field_edits(db_session, user=test_user, buy_plan_id=plan.id, buy_plan_line_id=line.id, edits=edits)
        assert record is not None
        rows = db_session.query(ActivityLog).filter(ActivityLog.activity_type == ActivityType.FIELD_EDIT.value).all()
        assert len(rows) == 1  # ONE row per save, not one per field
        row = rows[0]
        assert row.channel == "system"
        assert row.user_id == test_user.id
        assert row.buy_plan_id == plan.id
        assert row.buy_plan_line_id == line.id
        assert row.summary == "Edited quantity, unit_cost"
        assert row.details == {
            "edits": [
                {"field": "quantity", "old": "100", "new": "200"},
                {"field": "unit_cost", "old": "1.00", "new": "1.25"},
            ]
        }

    def test_empty_edits_is_noop(self, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        plan = _plan(db_session, req)
        assert log_field_edits(db_session, user=test_user, buy_plan_id=plan.id, edits=[]) is None
        count = db_session.query(ActivityLog).filter(ActivityLog.activity_type == ActivityType.FIELD_EDIT.value).count()
        assert count == 0

    def test_prepayment_subject(self, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        plan = _plan(db_session, req)
        prepayment = Prepayment(buy_plan_id=plan.id, total_incl_fees=500)
        db_session.add(prepayment)
        db_session.commit()
        record = log_field_edits(
            db_session,
            user=test_user,
            buy_plan_id=plan.id,
            prepayment_id=prepayment.id,
            edits=[FieldEdit(field="payment_method", old="wire", new="ach")],
        )
        assert record.prepayment_id == prepayment.id
        assert record.buy_plan_line_id is None


class TestEditsSince:
    def test_flattens_and_filters_by_since(self, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        plan = _plan(db_session, req)
        line = _line(db_session, plan)
        log_field_edits(
            db_session,
            user=test_user,
            buy_plan_id=plan.id,
            buy_plan_line_id=line.id,
            edits=[
                FieldEdit(field="quantity", old="100", new="200"),
                FieldEdit(field="po_number", old="", new="PO-9"),
            ],
        )
        db_session.commit()

        rows = edits_since(db_session, buy_plan_id=plan.id, since=None)
        assert [r.field for r in rows] == ["quantity", "po_number"]  # flattened, in order
        assert rows[0].user_id == test_user.id
        assert rows[0].user_name == test_user.name
        assert rows[0].buy_plan_line_id == line.id

        future = datetime.now(UTC) + timedelta(hours=1)
        assert edits_since(db_session, buy_plan_id=plan.id, since=future) == []

    def test_scoped_to_plan(self, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        plan_a = _plan(db_session, req)
        plan_b = _plan(db_session, req)
        log_field_edits(
            db_session,
            user=test_user,
            buy_plan_id=plan_a.id,
            edits=[FieldEdit(field="salesperson_notes", old="", new="hi")],
        )
        assert edits_since(db_session, buy_plan_id=plan_b.id, since=None) == []


class TestManagerEditedLineIds:
    def _user(self, db: Session, role: UserRole) -> User:
        user = User(email=f"{role.value}-audit@trio.com", name=f"{role.value} auditor", role=role.value)
        db.add(user)
        db.commit()
        return user

    def test_only_manager_and_admin_edits_count(self, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        plan = _plan(db_session, req)
        line_mgr = _line(db_session, plan)
        line_admin = _line(db_session, plan)
        line_buyer = _line(db_session, plan)
        manager = self._user(db_session, UserRole.MANAGER)
        admin = self._user(db_session, UserRole.ADMIN)

        edit = [FieldEdit(field="quantity", old="100", new="150")]
        log_field_edits(db_session, user=manager, buy_plan_id=plan.id, buy_plan_line_id=line_mgr.id, edits=edit)
        log_field_edits(db_session, user=admin, buy_plan_id=plan.id, buy_plan_line_id=line_admin.id, edits=edit)
        log_field_edits(db_session, user=test_user, buy_plan_id=plan.id, buy_plan_line_id=line_buyer.id, edits=edit)
        # Plan-level manager edit (no line) must not appear in the line-id set.
        log_field_edits(db_session, user=manager, buy_plan_id=plan.id, edits=edit)
        db_session.commit()

        assert manager_edited_line_ids(db_session, plan) == {line_mgr.id, line_admin.id}


class TestLogActivityAdditiveKwargs:
    def test_defaults_stay_null(self, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        plan = _plan(db_session, req)
        record = log_activity(
            db_session,
            activity_type=ActivityType.NOTE,
            user_id=test_user.id,
            buy_plan_id=plan.id,
            summary="plain note",
        )
        assert record.buy_plan_line_id is None
        assert record.prepayment_id is None

    def test_passthrough(self, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        plan = _plan(db_session, req)
        line = _line(db_session, plan)
        record = log_activity(
            db_session,
            activity_type=ActivityType.NOTE,
            user_id=test_user.id,
            buy_plan_id=plan.id,
            buy_plan_line_id=line.id,
            summary="line note",
        )
        assert record.buy_plan_line_id == line.id
