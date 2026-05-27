"""Model invariants for the IBM spec code resolver tables.

Called by: pytest collection
Depends on: app.models.sourcing (OemSpecCode, OemSpecCodePending, OemSpecCodeBlacklist,
            Requirement, Requisition), app.models.offers (Offer), app.schemas.spec_codes,
            tests.conftest (db_session fixture)
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.sourcing import (
    OemSpecCode,
    OemSpecCodeBlacklist,
    OemSpecCodePending,
    Requirement,
    Requisition,
)


def _new_requisition(db) -> Requisition:
    req_set = Requisition(name="test")
    db.add(req_set)
    db.commit()
    db.refresh(req_set)
    return req_set


def test_oem_spec_code_unique_constraint(db_session):
    db_session.add(
        OemSpecCode(
            oem="IBM",
            spec_code="SPREJ",
            avl=[{"mpn": "X", "manufacturer": "M", "rank": 1, "notes": None}],
            source="manual",
            approved_at=datetime.now(timezone.utc),
        )
    )
    db_session.commit()

    db_session.add(
        OemSpecCode(
            oem="IBM",
            spec_code="SPREJ",
            avl=[{"mpn": "Y", "manufacturer": "M", "rank": 1, "notes": None}],
            source="manual",
            approved_at=datetime.now(timezone.utc),
        )
    )
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_oem_spec_code_pending_unique_constraint(db_session):
    db_session.add(
        OemSpecCodePending(
            oem="IBM",
            spec_code="SPREJ",
            proposed_avl=[{"mpn": "X", "manufacturer": "M", "rank": 1, "notes": None}],
            llm_confidence=0.8,
        )
    )
    db_session.commit()

    db_session.add(
        OemSpecCodePending(
            oem="IBM",
            spec_code="SPREJ",
            proposed_avl=[{"mpn": "Y", "manufacturer": "M", "rank": 1, "notes": None}],
            llm_confidence=0.6,
        )
    )
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_blacklist_no_unique_constraint(db_session):
    """Multiple blacklist entries for the same spec code are allowed (each entry
    represents one rejection event)."""
    for mpn in ["A", "B"]:
        db_session.add(
            OemSpecCodeBlacklist(
                oem="IBM",
                spec_code="SPREJ",
                rejected_mpns=[mpn],
                reason="incorrect",
            )
        )
    db_session.commit()
    rows = db_session.query(OemSpecCodeBlacklist).filter_by(spec_code="SPREJ").all()
    assert len(rows) == 2


def test_requirement_oem_hint_defaults_to_none(db_session):
    rset = _new_requisition(db_session)
    req = Requirement(requisition_id=rset.id, primary_mpn="ABC123", manufacturer="TI")
    db_session.add(req)
    db_session.commit()
    db_session.refresh(req)
    assert req.oem_hint is None


def test_sighting_lineage_columns_nullable(db_session):
    from app.models.sourcing import Requirement, Sighting

    rset = _new_requisition(db_session)
    req = Requirement(requisition_id=rset.id, primary_mpn="ABC123", manufacturer="TI")
    db_session.add(req)
    db_session.commit()

    s = Sighting(
        requirement_id=req.id,
        vendor_name="Mouser",
        manufacturer="TI",
        normalized_mpn="ABC123",
        # lineage columns left null
    )
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    assert s.resolved_via_spec_code is None
    assert s.source_mpn is None


def test_sighting_lineage_columns_populated(db_session):
    from app.models.sourcing import Requirement, Sighting

    rset = _new_requisition(db_session)
    req = Requirement(requisition_id=rset.id, primary_mpn="SPREJ", manufacturer="")
    db_session.add(req)
    db_session.commit()

    s = Sighting(
        requirement_id=req.id,
        vendor_name="Broker",
        manufacturer="Murata",
        normalized_mpn="GRM188R71H103KA01D",
        resolved_via_spec_code="SPREJ",
        source_mpn="GRM188R71H103KA01D",
    )
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    assert s.resolved_via_spec_code == "SPREJ"
    assert s.source_mpn == "GRM188R71H103KA01D"


def test_resolver_llm_response_rejects_extra_fields():
    from pydantic import ValidationError

    from app.schemas.spec_codes import ResolverLlmResponse

    with pytest.raises(ValidationError):
        ResolverLlmResponse.model_validate(
            {
                "avl": [],
                "confidence": 0.0,
                "citations": [],
                "reasoning": "",
                "extra_field": "should fail",
            }
        )


def test_resolver_llm_response_rejects_invalid_confidence():
    from pydantic import ValidationError

    from app.schemas.spec_codes import ResolverLlmResponse

    with pytest.raises(ValidationError):
        ResolverLlmResponse.model_validate({"avl": [], "confidence": 1.5, "citations": [], "reasoning": ""})
