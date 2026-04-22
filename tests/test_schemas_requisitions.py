"""test_schemas_requisitions.py — Tests for app/schemas/requisitions.py validators.

Covers: _validate_deadline, RequirementCreate validators, RequirementUpdate validators,
RequisitionCreate/Update deadline, RequisitionOutcome, RequirementNoteAdd.

Called by: pytest
Depends on: app/schemas/requisitions.py
"""

import os

os.environ["TESTING"] = "1"

import pytest
from pydantic import ValidationError

from app.schemas.requisitions import (
    RequirementCreate,
    RequirementNoteAdd,
    RequirementUpdate,
    RequisitionCreate,
    RequisitionOutcome,
    RequisitionUpdate,
)

# ── _validate_deadline / RequisitionCreate ───────────────────────────


class TestDeadlineValidator:
    def test_valid_iso_date(self):
        req = RequisitionCreate(name="Test", deadline="2025-06-15")
        assert req.deadline == "2025-06-15"

    def test_valid_us_date_format(self):
        req = RequisitionCreate(name="Test", deadline="06/15/2025")
        assert req.deadline == "06/15/2025"

    def test_valid_iso_datetime(self):
        req = RequisitionCreate(name="Test", deadline="2025-06-15T00:00:00")
        assert req.deadline == "2025-06-15T00:00:00"

    def test_none_deadline_is_valid(self):
        req = RequisitionCreate(name="Test", deadline=None)
        assert req.deadline is None

    def test_empty_string_deadline_returns_none(self):
        req = RequisitionCreate(name="Test", deadline="")
        assert req.deadline is None

    def test_whitespace_only_deadline_returns_none(self):
        req = RequisitionCreate(name="Test", deadline="   ")
        assert req.deadline is None

    def test_invalid_date_raises(self):
        with pytest.raises(ValidationError) as exc_info:
            RequisitionCreate(name="Test", deadline="not-a-date")
        assert "Invalid date" in str(exc_info.value)

    def test_impossible_date_raises(self):
        with pytest.raises(ValidationError) as exc_info:
            RequisitionCreate(name="Test", deadline="2025-02-30")
        assert "Invalid date" in str(exc_info.value)

    def test_update_deadline_valid(self):
        req = RequisitionUpdate(deadline="2025-12-31")
        assert req.deadline == "2025-12-31"

    def test_update_deadline_invalid(self):
        with pytest.raises(ValidationError):
            RequisitionUpdate(deadline="bad-date")

    def test_update_deadline_none(self):
        req = RequisitionUpdate(deadline=None)
        assert req.deadline is None


# ── RequirementCreate validators ─────────────────────────────────────


class TestRequirementCreateValidators:
    def _base(self, **kwargs):
        defaults = {"primary_mpn": "LM317T", "manufacturer": "Texas Instruments"}
        defaults.update(kwargs)
        return RequirementCreate(**defaults)

    def test_valid_create(self):
        req = self._base()
        assert req.primary_mpn == "LM317T"

    def test_manufacturer_blank_raises(self):
        with pytest.raises(ValidationError) as exc_info:
            self._base(manufacturer="   ")
        assert "manufacturer must not be blank" in str(exc_info.value)

    def test_manufacturer_stripped(self):
        req = self._base(manufacturer="  Texas Instruments  ")
        assert req.manufacturer == "Texas Instruments"

    def test_mpn_blank_raises(self):
        with pytest.raises(ValidationError) as exc_info:
            self._base(primary_mpn="   ")
        assert "primary_mpn must not be blank" in str(exc_info.value)

    def test_mpn_normalized_uppercase(self):
        req = self._base(primary_mpn="lm317t")
        assert req.primary_mpn == "LM317T"

    def test_substitutes_from_string(self):
        req = self._base(substitutes="ABC123, DEF456")
        assert "ABC123" in req.substitutes
        assert "DEF456" in req.substitutes

    def test_substitutes_from_newline_string(self):
        req = self._base(substitutes="ABC123\nDEF456")
        assert len(req.substitutes) == 2

    def test_substitutes_normalized(self):
        req = self._base(substitutes=["abc123", "def456"])
        assert all(s == s.upper() for s in req.substitutes)

    def test_substitutes_empty_list(self):
        req = self._base(substitutes=[])
        assert req.substitutes == []

    def test_substitutes_filters_falsy_values(self):
        req = self._base(substitutes=["ABC123", "", "DEF456"])
        assert "" not in req.substitutes
        assert len(req.substitutes) == 2

    def test_condition_normalized(self):
        req = self._base(condition="new")
        assert req.condition is not None

    def test_condition_none_passthrough(self):
        req = self._base(condition=None)
        assert req.condition is None

    def test_packaging_normalized(self):
        req = self._base(packaging="tape and reel")
        assert req.packaging is not None

    def test_packaging_none_passthrough(self):
        req = self._base(packaging=None)
        assert req.packaging is None


# ── RequirementUpdate validators ─────────────────────────────────────


class TestRequirementUpdateValidators:
    def test_mpn_none_passthrough(self):
        req = RequirementUpdate(primary_mpn=None)
        assert req.primary_mpn is None

    def test_mpn_normalized(self):
        req = RequirementUpdate(primary_mpn="lm317t")
        assert req.primary_mpn == "LM317T"

    def test_mpn_blank_raises(self):
        with pytest.raises(ValidationError) as exc_info:
            RequirementUpdate(primary_mpn="   ")
        assert "primary_mpn must not be blank" in str(exc_info.value)

    def test_substitutes_list_normalized(self):
        req = RequirementUpdate(substitutes=["abc123"])
        assert req.substitutes == ["ABC123"]

    def test_substitutes_none_passthrough(self):
        req = RequirementUpdate(substitutes=None)
        assert req.substitutes is None

    def test_condition_normalized(self):
        req = RequirementUpdate(condition="new")
        assert req.condition is not None

    def test_condition_none_passthrough(self):
        req = RequirementUpdate(condition=None)
        assert req.condition is None

    def test_packaging_normalized(self):
        req = RequirementUpdate(packaging="tube")
        assert req.packaging is not None

    def test_packaging_none_passthrough(self):
        req = RequirementUpdate(packaging=None)
        assert req.packaging is None


# ── RequisitionOutcome ────────────────────────────────────────────────


class TestRequisitionOutcome:
    def test_won_valid(self):
        outcome = RequisitionOutcome(outcome="won")
        assert outcome.outcome == "won"

    def test_lost_valid(self):
        outcome = RequisitionOutcome(outcome="lost")
        assert outcome.outcome == "lost"

    def test_case_insensitive(self):
        outcome = RequisitionOutcome(outcome="WON")
        assert outcome.outcome == "won"

    def test_invalid_outcome_raises(self):
        with pytest.raises(ValidationError) as exc_info:
            RequisitionOutcome(outcome="pending")
        assert "outcome must be 'won' or 'lost'" in str(exc_info.value)

    def test_whitespace_stripped(self):
        outcome = RequisitionOutcome(outcome="  lost  ")
        assert outcome.outcome == "lost"


# ── RequirementNoteAdd ────────────────────────────────────────────────


class TestRequirementNoteAdd:
    def test_valid_note(self):
        note = RequirementNoteAdd(text="This is a note")
        assert note.text == "This is a note"

    def test_whitespace_stripped(self):
        note = RequirementNoteAdd(text="  hello  ")
        assert note.text == "hello"

    def test_blank_after_strip_raises(self):
        with pytest.raises(ValidationError) as exc_info:
            RequirementNoteAdd(text="   ")
        assert "Note text is required" in str(exc_info.value)

    def test_too_short_raises(self):
        with pytest.raises(ValidationError):
            RequirementNoteAdd(text="")
