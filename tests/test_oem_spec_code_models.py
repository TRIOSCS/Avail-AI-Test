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


def test_resolver_llm_response_ignores_extra_top_level_fields():
    """A stray top-level key from the LLM (e.g. ``notes``) must NOT turn an otherwise-
    valid resolution into a rejection — the outer model uses ``extra="ignore"`` so we
    don't silently drop a good resolution."""
    from app.schemas.spec_codes import ResolverLlmResponse

    result = ResolverLlmResponse.model_validate(
        {
            "avl": [{"mpn": "X", "manufacturer": "M", "rank": 1, "notes": None}],
            "confidence": 0.9,
            "citations": [],
            "reasoning": "ok",
            "notes": "an unrequested extra field",
        }
    )
    assert result.confidence == 0.9
    assert result.avl[0].mpn == "X"
    # The ignored field is not retained on the model.
    assert not hasattr(result, "notes")


def test_resolver_llm_response_nested_avl_still_forbids_extra():
    """Nested AvlEntry keeps ``extra="forbid"`` — a stray key inside an AVL entry is
    safety-critical (we source against these MPNs) and must reject."""
    from pydantic import ValidationError

    from app.schemas.spec_codes import ResolverLlmResponse

    with pytest.raises(ValidationError):
        ResolverLlmResponse.model_validate(
            {
                "avl": [{"mpn": "X", "manufacturer": "M", "rank": 1, "notes": None, "sneaky": "no"}],
                "confidence": 0.9,
                "citations": [],
                "reasoning": "",
            }
        )


def test_resolver_llm_response_rejects_invalid_confidence():
    from pydantic import ValidationError

    from app.schemas.spec_codes import ResolverLlmResponse

    with pytest.raises(ValidationError):
        ResolverLlmResponse.model_validate({"avl": [], "confidence": 1.5, "citations": [], "reasoning": ""})


def test_citation_rejects_javascript_scheme():
    from pydantic import ValidationError

    from app.schemas.spec_codes import ResolverLlmResponse

    with pytest.raises(ValidationError):
        ResolverLlmResponse.model_validate(
            {
                "avl": [{"mpn": "X", "manufacturer": "M", "rank": 1, "notes": None}],
                "confidence": 0.9,
                "citations": [{"url": "javascript:alert(1)", "snippet": "evil"}],
                "reasoning": "test",
            }
        )


def test_citation_accepts_https_scheme():
    from app.schemas.spec_codes import ResolverLlmResponse

    result = ResolverLlmResponse.model_validate(
        {
            "avl": [{"mpn": "X", "manufacturer": "M", "rank": 1, "notes": None}],
            "confidence": 0.9,
            "citations": [{"url": "https://example.com", "snippet": "ok"}],
            "reasoning": "test",
        }
    )
    assert len(result.citations) == 1
    assert result.citations[0].url == "https://example.com"
    assert result.citations[0].snippet == "ok"


def test_citation_rejects_data_scheme():
    from pydantic import ValidationError

    from app.schemas.spec_codes import ResolverLlmResponse

    with pytest.raises(ValidationError):
        ResolverLlmResponse.model_validate(
            {
                "avl": [{"mpn": "X", "manufacturer": "M", "rank": 1, "notes": None}],
                "confidence": 0.9,
                "citations": [
                    {
                        "url": "data:text/html,<script>alert(1)</script>",
                        "snippet": "",
                    }
                ],
                "reasoning": "test",
            }
        )


def test_oem_spec_code_normalizes_oem_and_spec_code_case(db_session):
    """Unique constraint must be enforced regardless of input casing/whitespace."""
    db_session.add(
        OemSpecCode(
            oem="ibm",
            spec_code="sprej ",
            avl=[{"mpn": "X", "manufacturer": "M", "rank": 1, "notes": None}],
            source="manual",
            approved_at=datetime.now(timezone.utc),
        )
    )
    db_session.commit()
    row = db_session.query(OemSpecCode).one()
    assert row.oem == "IBM"
    assert row.spec_code == "SPREJ"

    # Second insert with different casing must collide on the unique constraint
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


def test_pending_normalizes_oem_and_spec_code_case(db_session):
    db_session.add(
        OemSpecCodePending(
            oem="ibm",
            spec_code=" sprej",
            proposed_avl=[{"mpn": "X", "manufacturer": "M", "rank": 1, "notes": None}],
            llm_confidence=0.7,
        )
    )
    db_session.commit()
    row = db_session.query(OemSpecCodePending).one()
    assert row.oem == "IBM"
    assert row.spec_code == "SPREJ"


def test_blacklist_normalizes_oem_and_spec_code_case(db_session):
    db_session.add(
        OemSpecCodeBlacklist(
            oem="ibm",
            spec_code="sprej",
            rejected_mpns=["X"],
            reason="test",
        )
    )
    db_session.commit()
    row = db_session.query(OemSpecCodeBlacklist).one()
    assert row.oem == "IBM"
    assert row.spec_code == "SPREJ"


def test_requirement_oem_hint_normalizes_case(db_session):
    rset = _new_requisition(db_session)
    req = Requirement(
        requisition_id=rset.id,
        primary_mpn="ABC123",
        manufacturer="TI",
        oem_hint=" ibm ",
    )
    db_session.add(req)
    db_session.commit()
    db_session.refresh(req)
    assert req.oem_hint == "IBM"


# ── Task 4.3: model-layer invariants (no cross-layer Pydantic import) ─────────


def test_oem_spec_code_source_accepts_valid_enum_values():
    """OemSpecCode.source accepts every SpecCodeSource value."""
    from app.constants import SpecCodeSource

    for value in SpecCodeSource:
        row = OemSpecCode(
            oem="IBM",
            spec_code="SPREJ",
            avl=[{"mpn": "X", "manufacturer": "M", "rank": 1, "notes": None}],
            source=value.value,
            approved_at=datetime.now(timezone.utc),
        )
        assert row.source == value.value


def test_oem_spec_code_source_rejects_unknown_value():
    with pytest.raises(ValueError):
        OemSpecCode(
            oem="IBM",
            spec_code="SPREJ",
            avl=[{"mpn": "X", "manufacturer": "M", "rank": 1, "notes": None}],
            source="not_a_real_source",
            approved_at=datetime.now(timezone.utc),
        )


def test_pending_llm_confidence_out_of_range_rejected():
    with pytest.raises(ValueError):
        OemSpecCodePending(
            oem="IBM",
            spec_code="SPREJ",
            proposed_avl=[{"mpn": "X", "manufacturer": "M", "rank": 1, "notes": None}],
            llm_confidence=42.0,
        )


def test_pending_citations_with_bad_scheme_rejected_at_model_layer():
    """A citation carrying a non-http(s) URL must be rejected by the model's structural
    @validates — without importing the Pydantic Citation schema."""
    with pytest.raises(ValueError):
        OemSpecCodePending(
            oem="IBM",
            spec_code="SPREJ",
            proposed_avl=[{"mpn": "X", "manufacturer": "M", "rank": 1, "notes": None}],
            llm_confidence=0.7,
            citations=[{"url": "javascript:alert(1)", "snippet": "evil"}],
        )


def test_pending_citations_with_leading_whitespace_scheme_rejected():
    """Leading-whitespace tricks must not slip a dangerous scheme past the structural
    check."""
    with pytest.raises(ValueError):
        OemSpecCodePending(
            oem="IBM",
            spec_code="SPREJ",
            proposed_avl=[{"mpn": "X", "manufacturer": "M", "rank": 1, "notes": None}],
            llm_confidence=0.7,
            citations=[{"url": "  javascript:alert(1)", "snippet": "evil"}],
        )


def test_pending_citations_with_http_scheme_accepted():
    row = OemSpecCodePending(
        oem="IBM",
        spec_code="SPREJ",
        proposed_avl=[{"mpn": "X", "manufacturer": "M", "rank": 1, "notes": None}],
        llm_confidence=0.7,
        citations=[{"url": "https://example.com", "snippet": "ok"}],
    )
    assert row.citations[0]["url"] == "https://example.com"


def test_citation_schema_rejects_leading_whitespace_scheme():
    """Schema-layer Citation must also reject leading-whitespace scheme tricks via the
    structural urlparse check (Task 4.3)."""
    from pydantic import ValidationError

    from app.schemas.spec_codes import ResolverLlmResponse

    with pytest.raises(ValidationError):
        ResolverLlmResponse.model_validate(
            {
                "avl": [{"mpn": "X", "manufacturer": "M", "rank": 1, "notes": None}],
                "confidence": 0.9,
                "citations": [{"url": "  javascript:alert(1)", "snippet": "evil"}],
                "reasoning": "test",
            }
        )
