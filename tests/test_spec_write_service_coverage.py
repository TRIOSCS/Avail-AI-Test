"""tests/test_spec_write_service_coverage.py — Branch coverage for lines 87-100.

Called by: pytest
Depends on: app/services/spec_write_service.py, tests/conftest.py
"""

import os

os.environ["TESTING"] = "1"

from datetime import datetime, timezone
from unittest.mock import patch

from sqlalchemy.orm import Session

from app.models import CommoditySpecSchema, MaterialCard, MaterialSpecFacet
from app.services.spec_write_service import record_spec
from tests.conftest import engine  # noqa: F401


def _make_card(db: Session, mpn: str = "TEST-001", category: str = "capacitors") -> MaterialCard:
    card = MaterialCard(
        normalized_mpn=mpn,
        display_mpn=mpn,
        manufacturer="TestCo",
        category=category,
        created_at=datetime.now(timezone.utc),
    )
    db.add(card)
    db.flush()
    return card


def _make_schema(
    db: Session,
    commodity: str = "capacitors",
    spec_key: str = "capacitance",
    data_type: str = "numeric",
    **kwargs,
) -> CommoditySpecSchema:
    defaults = dict(
        commodity=commodity,
        spec_key=spec_key,
        display_name=spec_key.replace("_", " ").title(),
        data_type=data_type,
        sort_order=0,
        is_filterable=True,
        is_primary=False,
    )
    defaults.update(kwargs)
    schema = CommoditySpecSchema(**defaults)
    db.add(schema)
    db.flush()
    return schema


# --- Line 92-94: numeric string parsed successfully, unit extracted from value ---


def test_record_spec_numeric_string_parses_value_and_extracts_unit(db_session: Session):
    # "0.1µF" → canonical_value=0.1, unit extracted from match group 2
    card = _make_card(db_session)
    _make_schema(db_session, canonical_unit="pF")

    record_spec(db_session, card.id, "capacitance", "0.1uF", source="ai", confidence=0.9)

    facet = db_session.query(MaterialSpecFacet).filter_by(material_card_id=card.id).first()
    # uF → pF conversion: 0.1 uF = 100_000 pF
    assert facet is not None
    assert facet.value_numeric == 100_000.0


def test_record_spec_numeric_string_with_comma_decimal(db_session: Session):
    # European decimal format "1,5" → 1.5
    card = _make_card(db_session)
    _make_schema(db_session, canonical_unit=None)

    record_spec(db_session, card.id, "capacitance", "1,5", source="ai", confidence=0.8)

    facet = db_session.query(MaterialSpecFacet).filter_by(material_card_id=card.id).first()
    assert facet is not None
    assert facet.value_numeric == 1.5


def test_record_spec_numeric_string_no_unit_in_value_uses_provided_unit(db_session: Session):
    # String "100" parsed, explicit unit="nF" provided, canonical_unit="pF"
    card = _make_card(db_session)
    _make_schema(db_session, canonical_unit="pF")

    record_spec(db_session, card.id, "capacitance", "100", source="ai", confidence=0.85, unit="nF")

    facet = db_session.query(MaterialSpecFacet).filter_by(material_card_id=card.id).first()
    # 100 nF → pF = 100_000 pF
    assert facet is not None
    assert facet.value_numeric == 100_000.0


# --- Line 99-100: non-numeric string → debug log + return early ---


def test_record_spec_numeric_non_numeric_string_skips(db_session: Session):
    # "not-a-number" doesn't match the regex → returns early, no facet written
    card = _make_card(db_session)
    _make_schema(db_session)

    record_spec(db_session, card.id, "capacitance", "not-a-number", source="ai", confidence=0.5)

    facet = db_session.query(MaterialSpecFacet).filter_by(material_card_id=card.id).first()
    assert facet is None


def test_record_spec_numeric_bare_letters_skips(db_session: Session):
    # "uF" has no leading digit → no match → skip
    card = _make_card(db_session)
    _make_schema(db_session)

    record_spec(db_session, card.id, "capacitance", "uF", source="ai", confidence=0.5)

    count = db_session.query(MaterialSpecFacet).filter_by(material_card_id=card.id).count()
    assert count == 0


# --- Lines 101-102: unit + canonical_unit → normalize_value called ---


def test_record_spec_numeric_string_unit_and_canonical_unit_normalizes(db_session: Session):
    # "470" with unit="nF" and canonical_unit="pF" → normalize_value("470", "nF", "pF")
    card = _make_card(db_session)
    _make_schema(db_session, canonical_unit="pF")

    record_spec(db_session, card.id, "capacitance", "470", source="digikey_api", confidence=0.99, unit="nF")

    facet = db_session.query(MaterialSpecFacet).filter_by(material_card_id=card.id).first()
    assert facet is not None
    assert facet.value_numeric == 470_000.0
    assert facet.value_unit == "pF"


def test_record_spec_numeric_string_no_canonical_unit_skips_normalize(db_session: Session):
    # unit provided but no canonical_unit → normalize_value NOT called, raw float stored
    card = _make_card(db_session)
    _make_schema(db_session, canonical_unit=None)

    record_spec(db_session, card.id, "capacitance", "3.3", source="ai", confidence=0.9, unit="V")

    facet = db_session.query(MaterialSpecFacet).filter_by(material_card_id=card.id).first()
    assert facet is not None
    assert facet.value_numeric == 3.3


# --- Lines 95-97: regex matches but float() conversion raises ValueError → return early ---


def test_record_spec_numeric_string_float_conversion_fails_skips(db_session: Session):
    # Patch float() to raise ValueError after the regex matches so lines 95-97 execute
    card = _make_card(db_session)
    _make_schema(db_session, canonical_unit="pF")

    original_float = float

    def _bad_float(v):
        if (
            isinstance(v, str)
            and v.replace(",", ".")
            .replace("+", "")
            .replace("-", "")
            .replace("e", "")
            .replace("E", "")
            .replace(".", "")
            .isdigit()
        ):
            raise ValueError("simulated parse failure")
        return original_float(v)

    with patch("builtins.float", side_effect=_bad_float):
        record_spec(db_session, card.id, "capacitance", "100nF", source="ai", confidence=0.9)

    # No facet should be written because the ValueError path returned early
    facet = db_session.query(MaterialSpecFacet).filter_by(material_card_id=card.id).first()
    assert facet is None
