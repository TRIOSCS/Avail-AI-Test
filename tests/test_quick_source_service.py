"""Tests for the quick-source (scratch requisition) service.

What: verifies get_or_create_scratch_req idempotency + scratch flagging, and
      persist_rows_as_sightings turning client-posted market rows into Sightings.
Calls: app.services.quick_source_service
Depends on: conftest fixtures (db_session, test_user), models.sourcing.
"""

from app.constants import RequisitionStatus
from app.models.sourcing import Requisition, Sighting
from app.services.quick_source_service import (
    get_or_create_scratch_req,
    persist_rows_as_sightings,
)


def _row(vendor="Broker Bin LLC", **over):
    row = {
        "vendor_name": vendor,
        "mpn_matched": "LM317T",
        "manufacturer": "TI",
        "qty_available": 1200,
        "unit_price": 0.84,
        "currency": "USD",
        "source_type": "brokerbin",
        "confidence": 0.91,
        "score": 78.0,
        "evidence_tier": "T4",
    }
    row.update(over)
    return row


def test_creates_scratch_requisition(db_session, test_user):
    req, requirement = get_or_create_scratch_req(db_session, test_user, "lm317t")

    assert req.is_scratch is True
    assert req.created_by == test_user.id
    assert req.status == RequisitionStatus.ACTIVE
    assert req.customer_name is None
    assert req.name == "Quick-source: LM317T"
    assert requirement.requisition_id == req.id
    assert requirement.primary_mpn == "LM317T"
    assert requirement.normalized_mpn == "LM317T"


def test_get_or_create_is_idempotent_per_user_and_mpn(db_session, test_user):
    req1, _ = get_or_create_scratch_req(db_session, test_user, "LM317T")
    req2, _ = get_or_create_scratch_req(db_session, test_user, "lm317t")

    assert req1.id == req2.id
    scratch_count = (
        db_session.query(Requisition)
        .filter(Requisition.is_scratch.is_(True), Requisition.created_by == test_user.id)
        .count()
    )
    assert scratch_count == 1


def test_different_mpn_gets_its_own_scratch_req(db_session, test_user):
    req1, _ = get_or_create_scratch_req(db_session, test_user, "LM317T")
    req2, _ = get_or_create_scratch_req(db_session, test_user, "STM32F407VGT6")

    assert req1.id != req2.id


def test_persist_rows_creates_sightings_under_requirement(db_session, test_user):
    _, requirement = get_or_create_scratch_req(db_session, test_user, "LM317T")

    created = persist_rows_as_sightings(db_session, requirement, [_row("Vendor A"), _row("Vendor B")])

    assert len(created) == 2
    persisted = db_session.query(Sighting).filter(Sighting.requirement_id == requirement.id).all()
    assert len(persisted) == 2
    assert {s.vendor_name for s in persisted} == {"Vendor A", "Vendor B"}
    assert all(s.requirement_id == requirement.id for s in persisted)
    assert all(s.unit_price == 0.84 for s in persisted)


def test_persist_rows_skips_rows_without_vendor(db_session, test_user):
    _, requirement = get_or_create_scratch_req(db_session, test_user, "LM317T")

    created = persist_rows_as_sightings(db_session, requirement, [_row("Real Vendor"), _row(""), {"unit_price": 1.0}])

    assert len(created) == 1
    assert created[0].vendor_name == "Real Vendor"


def test_persist_empty_payload_creates_no_sightings(db_session, test_user):
    _, requirement = get_or_create_scratch_req(db_session, test_user, "LM317T")

    created = persist_rows_as_sightings(db_session, requirement, [])

    assert created == []
    assert db_session.query(Sighting).filter(Sighting.requirement_id == requirement.id).count() == 0


def test_scratch_reqs_excluded_from_requisitions_list(client, db_session, test_user):
    db_session.add(Requisition(name="REQ-NORMAL-9", status="active", created_by=test_user.id))
    db_session.commit()
    get_or_create_scratch_req(db_session, test_user, "SCRATCHONLYPN")
    db_session.commit()

    body = client.get("/v2/partials/requisitions").text

    assert "REQ-NORMAL-9" in body
    assert "Quick-source: SCRATCHONLYPN" not in body
    assert "SCRATCHONLYPN" not in body


def test_scratch_reqs_excluded_from_requisition_picker(client, db_session, test_user):
    db_session.add(Requisition(name="REQ-NORMAL-9", status="active", created_by=test_user.id))
    db_session.commit()
    get_or_create_scratch_req(db_session, test_user, "SCRATCHONLYPN")
    db_session.commit()

    body = client.get("/v2/partials/search/requisition-picker?mpn=ABC123").text

    assert "REQ-NORMAL-9" in body
    assert "SCRATCHONLYPN" not in body
