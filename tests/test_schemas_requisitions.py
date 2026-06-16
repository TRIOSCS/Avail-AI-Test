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
    @pytest.mark.parametrize(
        "deadline",
        [
            pytest.param("2025-06-15", id="iso_date"),
            pytest.param("06/15/2025", id="us_date_format"),
            pytest.param("2025-06-15T00:00:00", id="iso_datetime"),
        ],
    )
    def test_valid_deadline_passthrough(self, deadline):
        req = RequisitionCreate(name="Test", deadline=deadline)
        assert req.deadline == deadline

    @pytest.mark.parametrize(
        "deadline",
        [
            pytest.param(None, id="none"),
            pytest.param("", id="empty_string"),
            pytest.param("   ", id="whitespace_only"),
        ],
    )
    def test_blank_deadline_returns_none(self, deadline):
        req = RequisitionCreate(name="Test", deadline=deadline)
        assert req.deadline is None

    @pytest.mark.parametrize(
        "deadline",
        [
            pytest.param("not-a-date", id="invalid"),
            pytest.param("2025-02-30", id="impossible"),
        ],
    )
    def test_bad_deadline_raises(self, deadline):
        with pytest.raises(ValidationError) as exc_info:
            RequisitionCreate(name="Test", deadline=deadline)
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

    @pytest.mark.parametrize(
        "field, value",
        [
            pytest.param("condition", "new", id="condition_normalized"),
            pytest.param("packaging", "tape and reel", id="packaging_normalized"),
        ],
    )
    def test_field_normalized(self, field, value):
        req = self._base(**{field: value})
        assert getattr(req, field) is not None

    @pytest.mark.parametrize("field", ["condition", "packaging"])
    def test_field_none_passthrough(self, field):
        req = self._base(**{field: None})
        assert getattr(req, field) is None


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

    @pytest.mark.parametrize(
        "field, value",
        [
            pytest.param("condition", "new", id="condition_normalized"),
            pytest.param("packaging", "tube", id="packaging_normalized"),
        ],
    )
    def test_field_normalized(self, field, value):
        req = RequirementUpdate(**{field: value})
        assert getattr(req, field) is not None

    @pytest.mark.parametrize("field", ["condition", "packaging"])
    def test_field_none_passthrough(self, field):
        req = RequirementUpdate(**{field: None})
        assert getattr(req, field) is None


# ── RequisitionOutcome ────────────────────────────────────────────────


class TestRequisitionOutcome:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            pytest.param("won", "won", id="won_valid"),
            pytest.param("lost", "lost", id="lost_valid"),
            pytest.param("WON", "won", id="case_insensitive"),
            pytest.param("  lost  ", "lost", id="whitespace_stripped"),
        ],
    )
    def test_valid_outcome_normalized(self, raw, expected):
        outcome = RequisitionOutcome(outcome=raw)
        assert outcome.outcome == expected

    def test_invalid_outcome_raises(self):
        with pytest.raises(ValidationError) as exc_info:
            RequisitionOutcome(outcome="pending")
        assert "outcome must be 'won' or 'lost'" in str(exc_info.value)


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
