"""
test_schemas_requisitions_coverage.py — Coverage for app/schemas/requisitions.py

Covers uncovered lines:
- RequirementCreate: mpn_not_blank blank raises, substitutes string parsing
- RequirementUpdate: normalize_primary_mpn None, condition/packaging None
- SearchOptions defaults
"""

import pytest
from pydantic import ValidationError

from app.schemas.requisitions import (
    RequirementCreate,
    RequirementOut,
    RequirementUpdate,
    RequisitionArchiveOut,
    RequisitionCreate,
    RequisitionOut,
    RequisitionUpdate,
    SearchOptions,
    SightingUnavailableIn,
)


class TestRequirementCreate:
    def test_mpn_not_blank_raises(self):
        """Blank primary_mpn raises ValueError."""
        with pytest.raises(ValidationError, match="must not be blank"):
            RequirementCreate(primary_mpn="  ")

    def test_mpn_normalized(self):
        """primary_mpn is normalized to uppercase."""
        r = RequirementCreate(primary_mpn="lm317t")
        assert r.primary_mpn == "LM317T"

    def test_substitutes_from_string(self):
        """Substitutes can be a comma-separated string."""
        r = RequirementCreate(primary_mpn="LM317T", substitutes="NE555P,LM7805")
        assert len(r.substitutes) == 2
        assert "NE555P" in r.substitutes
        assert "LM7805" in r.substitutes

    def test_substitutes_from_newline_string(self):
        """Substitutes can be newline-separated string."""
        r = RequirementCreate(primary_mpn="LM317T", substitutes="NE555P\nLM7805")
        assert len(r.substitutes) == 2

    def test_substitutes_from_list(self):
        """Substitutes as list are normalized."""
        r = RequirementCreate(primary_mpn="LM317T", substitutes=["ne555p", "lm7805"])
        assert r.substitutes == ["NE555P", "LM7805"]

    def test_defaults(self):
        r = RequirementCreate(primary_mpn="LM317T")
        assert r.target_qty == 1
        assert r.target_price is None
        assert r.substitutes == []


class TestRequirementUpdate:
    def test_primary_mpn_none_passes(self):
        """None primary_mpn passes through."""
        r = RequirementUpdate(primary_mpn=None)
        assert r.primary_mpn is None

    def test_primary_mpn_normalized(self):
        r = RequirementUpdate(primary_mpn="lm317t")
        assert r.primary_mpn == "LM317T"

    def test_substitutes_list_normalized(self):
        r = RequirementUpdate(substitutes=["ne555p", "lm7805"])
        assert r.substitutes == ["NE555P", "LM7805"]

    def test_substitutes_none_passes(self):
        r = RequirementUpdate(substitutes=None)
        assert r.substitutes is None

    def test_condition_none_passes(self):
        r = RequirementUpdate(condition=None)
        assert r.condition is None

    def test_condition_normalized(self):
        r = RequirementUpdate(condition="Factory New")
        assert r.condition == "new"

    def test_packaging_none_passes(self):
        r = RequirementUpdate(packaging=None)
        assert r.packaging is None

    def test_packaging_normalized(self):
        r = RequirementUpdate(packaging="Tape and Reel")
        assert r.packaging == "reel"

    def test_all_optional(self):
        r = RequirementUpdate()
        assert r.primary_mpn is None
        assert r.condition is None


class TestRequisitionSchemas:
    def test_requisition_create_defaults(self):
        r = RequisitionCreate()
        assert r.name == "Untitled"
        assert r.customer_name is None

    def test_requisition_update_optional(self):
        r = RequisitionUpdate()
        assert r.name is None

    def test_requisition_out(self):
        r = RequisitionOut(id=1, name="Test")
        assert r.id == 1

    def test_archive_out(self):
        r = RequisitionArchiveOut(status="archived")
        assert r.ok is True

    def test_requirement_out(self):
        r = RequirementOut(id=1, primary_mpn="LM317T")
        assert r.target_qty == 1
        assert r.sighting_count == 0

    def test_sighting_unavailable(self):
        s = SightingUnavailableIn()
        assert s.unavailable is True

    def test_search_options_defaults(self):
        s = SearchOptions()
        assert s.requirement_ids is None

    def test_search_options_with_ids(self):
        s = SearchOptions(requirement_ids=[1, 2, 3])
        assert len(s.requirement_ids) == 3
