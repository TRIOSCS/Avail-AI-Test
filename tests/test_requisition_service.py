"""Tests for requisition_service — date normalization, validation, error mapping."""

from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from app.services.requisition_service import (
    clone_requisition,
    parse_date_field,
    parse_positive_int,
    safe_commit,
    to_utc,
)

# ---------------------------------------------------------------------------
# to_utc()
# ---------------------------------------------------------------------------


class TestToUtc:
    def test_none_returns_none(self):
        assert to_utc(None) is None

    def test_naive_datetime_gets_utc(self):
        naive = datetime(2026, 3, 11, 12, 0, 0)
        result = to_utc(naive)
        assert result is not None
        assert result.tzinfo == UTC
        assert result.year == 2026
        assert result.hour == 12

    def test_utc_datetime_unchanged(self):
        aware = datetime(2026, 3, 11, 12, 0, 0, tzinfo=UTC)
        result = to_utc(aware)
        assert result == aware

    def test_non_utc_aware_converted(self):
        eastern = timezone(timedelta(hours=-5))
        aware = datetime(2026, 3, 11, 12, 0, 0, tzinfo=eastern)
        result = to_utc(aware)
        assert result is not None
        assert result.tzinfo == UTC
        assert result.hour == 17  # 12 EST = 17 UTC


# ---------------------------------------------------------------------------
# parse_date_field()
# ---------------------------------------------------------------------------


class TestParseDateField:
    def test_valid_iso_string(self):
        result = parse_date_field("2026-03-11T10:00:00")
        assert result.year == 2026
        assert result.tzinfo == UTC

    def test_valid_iso_with_tz(self):
        result = parse_date_field("2026-03-11T10:00:00+00:00")
        assert result.tzinfo == UTC

    def test_invalid_string_raises_400(self):
        with pytest.raises(HTTPException) as exc_info:
            parse_date_field("not-a-date", field_name="deadline")
        assert exc_info.value.status_code == 400
        assert "deadline" in exc_info.value.detail

    def test_empty_string_raises_400(self):
        with pytest.raises(HTTPException) as exc_info:
            parse_date_field("")
        assert exc_info.value.status_code == 400


# ---------------------------------------------------------------------------
# parse_positive_int()
# ---------------------------------------------------------------------------


class TestParsePositiveInt:
    def test_valid_int(self):
        assert parse_positive_int(5) == 5

    def test_valid_string(self):
        assert parse_positive_int("42") == 42

    @pytest.mark.parametrize(
        ("value", "field_name", "expected_detail"),
        [
            pytest.param(0, "qty", "qty", id="zero"),
            pytest.param(-1, "value", None, id="negative"),
            pytest.param("abc", "target_qty", "target_qty", id="non_numeric"),
            pytest.param(None, "value", None, id="none"),
        ],
    )
    def test_raises_400(self, value, field_name, expected_detail):
        with pytest.raises(HTTPException) as exc_info:
            parse_positive_int(value, field_name=field_name)  # type: ignore[arg-type]
        assert exc_info.value.status_code == 400
        if expected_detail is not None:
            assert expected_detail in exc_info.value.detail


# ---------------------------------------------------------------------------
# safe_commit()
# ---------------------------------------------------------------------------


class TestSafeCommit:
    def test_successful_commit(self):
        db = MagicMock()
        safe_commit(db, entity="test")
        db.commit.assert_called_once()

    def test_integrity_error_raises_409(self):
        from sqlalchemy.exc import IntegrityError

        db = MagicMock()
        db.commit.side_effect = IntegrityError("dup", {}, Exception("unique"))
        with pytest.raises(HTTPException) as exc_info:
            safe_commit(db, entity="requisition")
        assert exc_info.value.status_code == 409
        assert "requisition" in exc_info.value.detail
        db.rollback.assert_called_once()


def test_clone_requisition_duplicate_mpn_preserves_offer_mapping(db_session, test_user):
    """Clone keeps offers mapped to distinct cloned requirement rows even with duplicate
    MPNs."""
    from app.models import Offer, Requirement, Requisition

    src = Requisition(
        name="SRC-REQ",
        customer_name="Acme",
        status="open",
        created_by=test_user.id,
    )
    db_session.add(src)
    db_session.flush()

    r1 = Requirement(requisition_id=src.id, primary_mpn="LM317T", target_qty=10)
    r2 = Requirement(requisition_id=src.id, primary_mpn="LM317T", target_qty=20)
    db_session.add_all([r1, r2])
    db_session.flush()

    o1 = Offer(requisition_id=src.id, requirement_id=r1.id, vendor_name="V1", mpn="LM317T", status="active")
    o2 = Offer(requisition_id=src.id, requirement_id=r2.id, vendor_name="V2", mpn="LM317T", status="active")
    db_session.add_all([o1, o2])
    db_session.commit()

    cloned = clone_requisition(db_session, src, test_user.id)
    cloned_offers = db_session.query(Offer).filter(Offer.requisition_id == cloned.id).all()

    assert len(cloned_offers) == 2
    assert len({o.requirement_id for o in cloned_offers}) == 2


class TestCloneEdgeCases:
    """Edge cases for clone_requisition."""

    def test_clone_with_zero_requirements(self, db_session, test_user):
        from app.models import Requisition

        source = Requisition(name="empty", status="open", created_by=test_user.id)
        db_session.add(source)
        db_session.flush()
        clone = clone_requisition(db_session, source, test_user.id)
        assert clone.id != source.id
        assert clone.name.startswith("empty")

    def test_clone_preserves_name_prefix(self, db_session, test_user):
        from app.models import Requisition

        source = Requisition(name="Original RFQ", status="open", created_by=test_user.id)
        db_session.add(source)
        db_session.flush()
        clone = clone_requisition(db_session, source, test_user.id)
        assert "Original RFQ" in clone.name


class TestParseEdgeCases:
    """Boundary cases for parsing helpers."""

    def test_parse_date_field_whitespace_only_raises(self):
        with pytest.raises(HTTPException) as exc_info:
            parse_date_field("   ", "deadline")
        assert exc_info.value.status_code == 400

    def test_parse_positive_int_float_string_raises(self):
        with pytest.raises(HTTPException) as exc_info:
            parse_positive_int("3.14", "quantity")
        assert exc_info.value.status_code == 400

    def test_parse_positive_int_max_value(self):
        result = parse_positive_int(999999999, "quantity")
        assert result == 999999999

    def test_to_utc_with_far_future_date(self):
        from datetime import datetime

        dt = datetime(2099, 12, 31, 23, 59, 59, tzinfo=UTC)
        assert to_utc(dt) == dt

    def test_safe_commit_on_generic_exception(self, db_session):
        db_session.commit = MagicMock(side_effect=Exception("unexpected"))
        with pytest.raises(Exception, match="unexpected"):
            safe_commit(db_session, entity="test")


# ---------------------------------------------------------------------------
# clone_parts_to_active() — Clone-to-Active ("I need this part again")
# ---------------------------------------------------------------------------


def _make_part(db, req_id, mpn="LM317T", **kwargs):
    from app.models import Requirement

    part = Requirement(requisition_id=req_id, primary_mpn=mpn, **kwargs)
    db.add(part)
    db.commit()
    db.refresh(part)
    return part


def _make_req(db, user, name="SRC", **kwargs):
    from app.models import Requisition

    req = Requisition(name=name, customer_name="Acme", status="open", created_by=user.id, **kwargs)
    db.add(req)
    db.commit()
    db.refresh(req)
    return req


class TestClonePartsToActive:
    def test_copies_full_field_set_and_excludes_fresh_start_fields(self, db_session, test_user):
        from datetime import date

        from app.models import MaterialCard, Requirement
        from app.services.requisition_service import clone_parts_to_active

        card = MaterialCard(normalized_mpn="lm317t", display_mpn="LM317T")
        db_session.add(card)
        db_session.commit()

        src_req = _make_req(db_session, test_user)
        part = _make_part(
            db_session,
            src_req.id,
            mpn="LM317T",
            oem_pn="OEM-1",
            brand="TI",
            manufacturer="Texas Instruments",
            sku="SKU-9",
            target_qty=250,
            target_price=1.25,
            substitutes=["LM317AT"],
            condition="new",
            packaging="tube",
            date_codes="2023+",
            firmware="FW1.2",
            hardware_codes="HW-A",
            package_type="TO-220",
            revision="RevB",
            customer_pn="CUST-77",
            description="Adjustable regulator",
            oem_hint="IBM",
            notes="ask for COC",
            material_card_id=card.id,
            assigned_buyer_id=test_user.id,
            need_by_date=date(2026, 9, 1),
            sale_notes="internal margin note",
            sourcing_status="won",
            outcome_reason="Customer PO received",
            priority_score=88.0,
        )

        new_reqs = clone_parts_to_active(db_session, [part], test_user)

        assert len(new_reqs) == 1
        clone = db_session.query(Requirement).filter(Requirement.requisition_id == new_reqs[0].id).one()
        assert clone.primary_mpn == "LM317T"
        assert clone.oem_pn == "OEM-1"
        assert clone.brand == "TI"
        assert clone.manufacturer == "Texas Instruments"
        assert clone.sku == "SKU-9"
        assert clone.target_qty == 250
        assert float(clone.target_price) == 1.25
        assert clone.substitutes == ["LM317AT"]
        assert clone.condition == "new"
        assert clone.packaging == "tube"
        assert clone.date_codes == "2023+"
        assert clone.firmware == "FW1.2"
        assert clone.hardware_codes == "HW-A"
        assert clone.package_type == "TO-220"
        assert clone.revision == "RevB"
        assert clone.customer_pn == "CUST-77"
        assert clone.description == "Adjustable regulator"
        assert clone.oem_hint == "IBM"
        assert clone.notes == "ask for COC"
        assert clone.material_card_id == card.id
        assert clone.assigned_buyer_id == test_user.id
        # Fresh-start fields are NOT carried over.
        assert clone.need_by_date is None
        assert clone.sale_notes is None
        assert clone.outcome_reason is None
        assert clone.priority_score is None
        assert clone.last_searched_at is None
        # New part is active with provenance.
        assert clone.sourcing_status == "open"
        assert clone.cloned_from_id == part.id

    def test_new_requisition_open_with_customer_context(self, db_session, test_user, test_company, test_customer_site):
        from app.services.requisition_service import clone_parts_to_active

        src_req = _make_req(
            db_session,
            test_user,
            customer_site_id=test_customer_site.id,
            company_id=test_company.id,
        )
        part = _make_part(db_session, src_req.id, mpn="PART-CTX")

        new_reqs = clone_parts_to_active(db_session, [part], test_user)

        new_req = new_reqs[0]
        assert new_req.status == "open"
        assert new_req.customer_name == "Acme"
        assert new_req.customer_site_id == test_customer_site.id
        assert new_req.company_id == test_company.id
        assert new_req.created_by == test_user.id
        assert new_req.claimed_by_id == test_user.id
        assert new_req.cloned_from_id == src_req.id
        assert "Reorder" in new_req.name
        assert "Acme" in new_req.name

    def test_two_source_requisitions_give_two_new_requisitions(self, db_session, test_user):
        from app.models import Requirement
        from app.services.requisition_service import clone_parts_to_active

        req_a = _make_req(db_session, test_user, name="A")
        req_b = _make_req(db_session, test_user, name="B")
        p1 = _make_part(db_session, req_a.id, mpn="PART-A1")
        p2 = _make_part(db_session, req_b.id, mpn="PART-B1")
        p3 = _make_part(db_session, req_a.id, mpn="PART-A2")

        new_reqs = clone_parts_to_active(db_session, [p1, p2, p3], test_user)

        assert len(new_reqs) == 2
        assert new_reqs[0].cloned_from_id == req_a.id
        assert new_reqs[1].cloned_from_id == req_b.id
        a_mpns = {r.primary_mpn for r in db_session.query(Requirement).filter_by(requisition_id=new_reqs[0].id)}
        b_mpns = {r.primary_mpn for r in db_session.query(Requirement).filter_by(requisition_id=new_reqs[1].id)}
        assert a_mpns == {"PART-A1", "PART-A2"}
        assert b_mpns == {"PART-B1"}

    def test_offers_copied_as_reference_scoped_to_source_part(self, db_session, test_user):
        from app.models import Offer
        from app.services.requisition_service import clone_parts_to_active

        src_req = _make_req(db_session, test_user)
        part = _make_part(db_session, src_req.id, mpn="PART-OFF")
        other = _make_part(db_session, src_req.id, mpn="PART-OTHER")
        db_session.add_all(
            [
                Offer(
                    requisition_id=src_req.id, requirement_id=part.id, vendor_name="V1", mpn="PART-OFF", status="active"
                ),
                Offer(
                    requisition_id=src_req.id,
                    requirement_id=part.id,
                    vendor_name="V2",
                    mpn="PART-OFF",
                    status="selected",
                ),
                Offer(
                    requisition_id=src_req.id,
                    requirement_id=part.id,
                    vendor_name="V3",
                    mpn="PART-OFF",
                    status="expired",
                ),
                Offer(
                    requisition_id=src_req.id,
                    requirement_id=other.id,
                    vendor_name="V4",
                    mpn="PART-OTHER",
                    status="active",
                ),
            ]
        )
        db_session.commit()

        new_reqs = clone_parts_to_active(db_session, [part], test_user)

        copied = db_session.query(Offer).filter(Offer.requisition_id == new_reqs[0].id).all()
        assert {o.vendor_name for o in copied} == {"V1", "V2"}
        assert all(o.status == "reference" for o in copied)
        assert all(f"REQ-{src_req.id:03d}" in (o.notes or "") for o in copied)
        assert all(o.entered_by_id == test_user.id for o in copied)

    def test_source_part_untouched(self, db_session, test_user):
        from app.models import Requirement
        from app.services.requisition_service import clone_parts_to_active

        src_req = _make_req(db_session, test_user)
        part = _make_part(db_session, src_req.id, mpn="PART-SRC", sourcing_status="lost", outcome_reason="priced out")

        clone_parts_to_active(db_session, [part], test_user)

        refreshed = db_session.get(Requirement, part.id)
        assert refreshed.sourcing_status == "lost"
        assert refreshed.outcome_reason == "priced out"
        assert refreshed.requisition_id == src_req.id

    def test_part_cloned_activity_on_both_requisitions(self, db_session, test_user):
        from app.constants import ActivityType
        from app.models import ActivityLog
        from app.services.requisition_service import clone_parts_to_active

        src_req = _make_req(db_session, test_user)
        part = _make_part(db_session, src_req.id, mpn="PART-ACT")

        new_reqs = clone_parts_to_active(db_session, [part], test_user)

        rows = db_session.query(ActivityLog).filter(ActivityLog.activity_type == ActivityType.PART_CLONED).all()
        by_req = {r.requisition_id for r in rows}
        assert by_req == {src_req.id, new_reqs[0].id}
        src_row = next(r for r in rows if r.requisition_id == src_req.id)
        new_row = next(r for r in rows if r.requisition_id == new_reqs[0].id)
        assert src_row.requirement_id == part.id
        assert f"REQ-{new_reqs[0].id:03d}" in src_row.notes
        assert f"REQ-{src_req.id:03d}" in new_row.notes

    def test_source_task_created_for_assigned_buyer(self, db_session, test_user, sales_user):
        from app.models import RequisitionTask
        from app.services.requisition_service import clone_parts_to_active

        src_req = _make_req(db_session, test_user)
        part = _make_part(db_session, src_req.id, mpn="PART-TASK", assigned_buyer_id=sales_user.id)

        new_reqs = clone_parts_to_active(db_session, [part], test_user)

        tasks = db_session.query(RequisitionTask).filter(RequisitionTask.requisition_id == new_reqs[0].id).all()
        assert any("PART-TASK" in t.title for t in tasks)
        task = next(t for t in tasks if "PART-TASK" in t.title)
        assert task.assigned_to_id == sales_user.id  # part's buyer wins over the cloning user

    def test_source_task_falls_back_to_cloning_user(self, db_session, test_user):
        from app.models import RequisitionTask
        from app.services.requisition_service import clone_parts_to_active

        src_req = _make_req(db_session, test_user)
        part = _make_part(db_session, src_req.id, mpn="PART-TASK2")

        new_reqs = clone_parts_to_active(db_session, [part], test_user)

        task = (
            db_session.query(RequisitionTask)
            .filter(RequisitionTask.requisition_id == new_reqs[0].id)
            .filter(RequisitionTask.title.contains("PART-TASK2"))
            .one()
        )
        assert task.assigned_to_id == test_user.id


def test_clone_requisition_copies_full_spec_regression(db_session, test_user):
    """clone_requisition now shares _copy_requirement_fields — the previously dropped
    fields (material_card_id, manufacturer, description, ...) come along."""
    from app.models import MaterialCard, Requirement, Requisition

    card = MaterialCard(normalized_mpn="bc547", display_mpn="BC547")
    db_session.add(card)
    db_session.commit()

    src = Requisition(name="FULL-SPEC", customer_name="Acme", status="open", created_by=test_user.id)
    db_session.add(src)
    db_session.flush()
    r = Requirement(
        requisition_id=src.id,
        primary_mpn="BC547",
        manufacturer="ON Semi",
        description="NPN transistor",
        material_card_id=card.id,
        customer_pn="CUST-BC",
        package_type="TO-92",
    )
    db_session.add(r)
    db_session.commit()

    cloned = clone_requisition(db_session, src, test_user.id)

    clone_r = db_session.query(Requirement).filter(Requirement.requisition_id == cloned.id).one()
    assert clone_r.manufacturer == "ON Semi"
    assert clone_r.description == "NPN transistor"
    assert clone_r.material_card_id == card.id
    assert clone_r.customer_pn == "CUST-BC"
    assert clone_r.package_type == "TO-92"
