"""tests/test_spec_write_service.py -- Tests for spec write service.

Covers: app/services/spec_write_service.py
Depends on: conftest.py (db_session), faceted search models
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session

from app.models import CommoditySpecSchema, MaterialCard, MaterialSpecFacet
from app.services.spec_write_service import load_schema_cache, record_spec
from tests.conftest import engine  # noqa: F401


def _make_card(db: Session, mpn: str = "TEST-001", category: str = "dram") -> MaterialCard:
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
    commodity: str = "dram",
    spec_key: str = "ddr_type",
    data_type: str = "enum",
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


@pytest.fixture
def ddr_card(db_session: Session) -> MaterialCard:
    """A default dram MaterialCard with the standard ddr_type enum schema (DDR3/4/5)."""
    card = _make_card(db_session)
    _make_schema(db_session, enum_values=["DDR3", "DDR4", "DDR5"])
    return card


# --- Basic write ---


def test_record_spec_creates_facet(db_session: Session, ddr_card: MaterialCard):
    card = ddr_card

    record_spec(db_session, card.id, "ddr_type", "DDR4", source="digikey_api", confidence=0.99)

    facet = db_session.query(MaterialSpecFacet).filter_by(material_card_id=card.id, spec_key="ddr_type").first()
    assert facet is not None
    assert facet.value_text == "DDR4"
    assert facet.category == "dram"


def test_record_spec_writes_jsonb(db_session: Session, ddr_card: MaterialCard):
    card = ddr_card

    record_spec(db_session, card.id, "ddr_type", "DDR4", source="digikey_api", confidence=0.99)

    db_session.refresh(card)
    assert card.specs_structured is not None
    assert card.specs_structured["ddr_type"]["value"] == "DDR4"
    assert card.specs_structured["ddr_type"]["source"] == "digikey_api"
    assert card.specs_structured["ddr_type"]["confidence"] == 0.99


# --- Numeric with normalization ---


def test_record_spec_numeric_normalized(db_session: Session):
    card = _make_card(db_session, mpn="CAP-001", category="capacitors")
    _make_schema(
        db_session,
        commodity="capacitors",
        spec_key="capacitance",
        data_type="numeric",
        canonical_unit="pF",
    )

    record_spec(
        db_session,
        card.id,
        "capacitance",
        100,
        source="digikey_api",
        confidence=0.95,
        unit="uF",
    )

    facet = db_session.query(MaterialSpecFacet).filter_by(material_card_id=card.id, spec_key="capacitance").first()
    assert facet is not None
    assert facet.value_numeric == 100_000_000  # 100 uF → pF
    assert facet.value_unit == "pF"


# --- Enum validation ---


def test_record_spec_rejects_invalid_enum(db_session: Session, ddr_card: MaterialCard):
    card = ddr_card

    record_spec(db_session, card.id, "ddr_type", "DDR99", source="ai", confidence=0.5)

    facet = db_session.query(MaterialSpecFacet).filter_by(material_card_id=card.id, spec_key="ddr_type").first()
    assert facet is None  # Rejected — not written


# --- No schema row → skip silently ---


def test_record_spec_no_schema_skips(db_session: Session):
    card = _make_card(db_session)

    record_spec(db_session, card.id, "unknown_key", "foo", source="ai", confidence=0.5)

    count = db_session.query(MaterialSpecFacet).filter_by(material_card_id=card.id).count()
    assert count == 0


# --- Conflict arbitration: two sequential writes, the ladder picks the winner ---
# Each case: (value, source, confidence) for write 1 then write 2 → the expected subset
# of the surviving specs_structured["ddr_type"] entry (only the keys the case cares about).
@pytest.mark.parametrize(
    ("write1", "write2", "expected"),
    [
        pytest.param(
            ("DDR3", "spec_extraction", 0.99),
            ("DDR4", "web_search", 0.50),
            {"value": "DDR4", "source": "web_search", "tier": 70},
            id="higher_tier_overwrites_lower",
        ),
        pytest.param(
            ("DDR3", "spec_extraction", 0.85),
            ("DDR4", "digikey_api", 0.95),
            {"value": "DDR4", "source": "digikey_api", "tier": 90},
            id="vendor_api_overwrites_lower_tier",
        ),
        pytest.param(
            ("DDR4", "digikey_api", 0.95),
            ("DDR3", "spec_extraction", 0.99),
            {"value": "DDR4", "source": "digikey_api"},
            id="lower_tier_cannot_overwrite_vendor_api",
        ),
        pytest.param(
            ("DDR3", "digikey_api", 0.90),
            ("DDR4", "nexar_api", 0.95),
            {"value": "DDR4", "source": "nexar_api"},
            id="equal_tier_higher_confidence_wins",
        ),
        pytest.param(
            ("DDR3", "digikey_api", 0.95),
            ("DDR4", "nexar_api", 0.88),
            {"value": "DDR3", "source": "digikey_api"},
            id="equal_tier_lower_confidence_loses",
        ),
        pytest.param(
            ("DDR3", "digikey_api", 0.90),
            ("DDR4", "digikey_api", 0.95),
            {"value": "DDR4"},
            id="same_source_updates_in_place",
        ),
        pytest.param(
            ("DDR4", "digikey_api", 0.95),
            ("DDR3", "digikey_api", 0.80),
            {"value": "DDR4", "confidence": 0.95},
            id="same_source_lower_confidence_rejected",
        ),
    ],
)
def test_conflict_ladder_arbitration(db_session: Session, ddr_card: MaterialCard, write1, write2, expected):
    card = ddr_card
    for value, source, confidence in (write1, write2):
        record_spec(db_session, card.id, "ddr_type", value, source=source, confidence=confidence)

    db_session.refresh(card)
    entry = card.specs_structured["ddr_type"]
    for key, want in expected.items():
        assert entry[key] == want


# --- Headline regression: decode (85) beats higher-confidence extraction (60) ---


def test_decode_then_extraction_rejected(db_session: Session, ddr_card: MaterialCard):
    # decode writes tier 85; spec_extraction (tier 60) must NOT overwrite it even though
    # it runs LATER. The JSONB + facet keep the decode value/tier (kills the ordering band-aid).
    card = ddr_card

    assert record_spec(db_session, card.id, "ddr_type", "DDR4", source="mpn_decode", confidence=0.95) is True
    assert record_spec(db_session, card.id, "ddr_type", "DDR3", source="spec_extraction", confidence=0.85) is False

    db_session.refresh(card)
    assert card.specs_structured["ddr_type"]["value"] == "DDR4"
    assert card.specs_structured["ddr_type"]["tier"] == 85
    facet = db_session.query(MaterialSpecFacet).filter_by(material_card_id=card.id, spec_key="ddr_type").first()
    assert facet.value_text == "DDR4"
    assert facet.source == "mpn_decode"
    assert facet.tier == 85


def test_extraction_then_decode_upgrades(db_session: Session, ddr_card: MaterialCard):
    # Reverse order: extraction first, then decode → decode wins. Proves order-independence.
    card = ddr_card

    assert record_spec(db_session, card.id, "ddr_type", "DDR3", source="spec_extraction", confidence=0.85) is True
    assert record_spec(db_session, card.id, "ddr_type", "DDR4", source="mpn_decode", confidence=0.95) is True

    db_session.refresh(card)
    assert card.specs_structured["ddr_type"]["value"] == "DDR4"
    assert card.specs_structured["ddr_type"]["tier"] == 85
    facet = db_session.query(MaterialSpecFacet).filter_by(material_card_id=card.id, spec_key="ddr_type").first()
    assert facet.value_text == "DDR4"
    assert facet.source == "mpn_decode"
    assert facet.tier == 85


# --- Legacy entry missing "tier" key is backfilled from its source before compare ---


def test_legacy_entry_without_tier_backfilled(db_session: Session, ddr_card: MaterialCard):
    card = ddr_card

    # Simulate a legacy JSONB entry written before the ladder (no "tier" key), source=spec_extraction.
    card.specs_structured = {
        "ddr_type": {
            "value": "DDR3",
            "source": "spec_extraction",
            "confidence": 0.85,
            "updated_at": "2026-01-01T00:00:00+00:00",
        }
    }
    db_session.flush()

    # An incoming higher-tier decode still wins against the backfilled-tier-60 legacy entry.
    assert record_spec(db_session, card.id, "ddr_type", "DDR4", source="mpn_decode", confidence=0.95) is True
    db_session.refresh(card)
    assert card.specs_structured["ddr_type"]["value"] == "DDR4"
    assert card.specs_structured["ddr_type"]["tier"] == 85


# --- Facet provenance: untouched after a losing write ---


def test_facet_untouched_after_losing_write(db_session: Session, ddr_card: MaterialCard):
    card = ddr_card

    record_spec(db_session, card.id, "ddr_type", "DDR4", source="mpn_decode", confidence=0.95)
    record_spec(db_session, card.id, "ddr_type", "DDR3", source="spec_extraction", confidence=0.85)

    facet = db_session.query(MaterialSpecFacet).filter_by(material_card_id=card.id, spec_key="ddr_type").first()
    assert facet.value_text == "DDR4"  # losing write never mutated the facet
    assert facet.source == "mpn_decode"
    assert facet.confidence == 0.95
    assert facet.tier == 85


# --- Boolean type ---


def test_record_spec_boolean(db_session: Session):
    card = _make_card(db_session)
    _make_schema(db_session, spec_key="ecc", data_type="boolean")

    record_spec(db_session, card.id, "ecc", True, source="digikey_api", confidence=0.99)

    facet = db_session.query(MaterialSpecFacet).filter_by(material_card_id=card.id, spec_key="ecc").first()
    assert facet is not None
    assert facet.value_text == "true"


# --- schema_cache parameter ---


def test_record_spec_uses_schema_cache(db_session: Session):
    """When schema_cache is provided, record_spec uses it instead of querying."""
    card = _make_card(db_session)
    schema = _make_schema(db_session, enum_values=["DDR3", "DDR4", "DDR5"])

    cache = {("dram", "ddr_type"): schema}
    record_spec(
        db_session,
        card.id,
        "ddr_type",
        "DDR4",
        source="digikey_api",
        confidence=0.99,
        schema_cache=cache,
    )

    db_session.refresh(card)
    assert card.specs_structured["ddr_type"]["value"] == "DDR4"


def test_record_spec_schema_cache_miss_skips(db_session: Session, ddr_card: MaterialCard):
    """When schema_cache is provided but key is missing, record_spec skips."""
    card = ddr_card

    # Empty cache — schema exists in DB but won't be queried
    cache: dict = {}
    record_spec(
        db_session,
        card.id,
        "ddr_type",
        "DDR4",
        source="digikey_api",
        confidence=0.99,
        schema_cache=cache,
    )

    facet = db_session.query(MaterialSpecFacet).filter_by(material_card_id=card.id, spec_key="ddr_type").first()
    assert facet is None  # Not written because cache had no entry


# --- load_schema_cache ---


def test_load_schema_cache(db_session: Session):
    """load_schema_cache returns a dict keyed by (commodity, spec_key)."""
    _make_schema(db_session, commodity="dram", spec_key="ddr_type", data_type="enum")
    _make_schema(db_session, commodity="dram", spec_key="ecc", data_type="boolean")
    _make_schema(db_session, commodity="capacitors", spec_key="capacitance", data_type="numeric")

    cache = load_schema_cache(db_session, "dram")
    assert ("dram", "ddr_type") in cache
    assert ("dram", "ecc") in cache
    assert ("capacitors", "capacitance") not in cache
    assert len(cache) == 2


# --- Non-existent card_id ---


def test_record_spec_nonexistent_card_skips(db_session: Session):
    """record_spec with a card_id that doesn't exist should skip gracefully."""
    _make_schema(db_session, enum_values=["DDR3", "DDR4", "DDR5"])

    # card_id 99999 does not exist
    record_spec(db_session, 99999, "ddr_type", "DDR4", source="ai", confidence=0.9)

    # No facet should be created
    count = db_session.query(MaterialSpecFacet).count()
    assert count == 0


# --- Card with no category ---


def test_record_spec_card_no_category_skips(db_session: Session):
    """record_spec with a card that has category=None should skip gracefully."""
    card = MaterialCard(
        normalized_mpn="no-cat-001",
        display_mpn="NO-CAT-001",
        manufacturer="TestCo",
        category=None,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(card)
    db_session.flush()

    _make_schema(db_session, enum_values=["DDR3", "DDR4", "DDR5"])

    record_spec(db_session, card.id, "ddr_type", "DDR4", source="ai", confidence=0.9)

    # No facet should be created
    count = db_session.query(MaterialSpecFacet).filter_by(material_card_id=card.id).count()
    assert count == 0

    # No specs_structured should be set
    db_session.refresh(card)
    assert not card.specs_structured


# --- Losing writes never mutate the existing (ORM-aliased) JSONB entry ---


def test_losing_write_never_mutates_existing_jsonb_entry(db_session: Session, ddr_card: MaterialCard):
    # The legacy-tier backfill for the ladder comparison happens on a COPY: a losing
    # write must leave the in-memory specs_structured byte-identical to the DB (no
    # hidden "tier" key materializing through the shallow-copy alias).
    card = ddr_card
    legacy = {
        "value": "DDR4",
        "source": "mpn_decode",
        "confidence": 0.95,
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    card.specs_structured = {"ddr_type": dict(legacy)}
    db_session.flush()

    wrote = record_spec(db_session, card.id, "ddr_type", "DDR3", source="spec_extraction", confidence=0.99)
    assert wrote is False
    assert card.specs_structured["ddr_type"] == legacy  # no side-effect 'tier' key


# --- spec_would_write: the read-only dry-run twin ---


def test_spec_would_write_mirrors_record_spec_gates(db_session: Session, ddr_card: MaterialCard):
    from app.services.spec_write_service import spec_would_write

    card = ddr_card
    record_spec(db_session, card.id, "ddr_type", "DDR4", source="mpn_decode", confidence=0.95)
    existing = card.specs_structured

    # No category → False (record_spec would skip).
    assert (
        spec_would_write(
            db_session,
            category=None,
            existing_specs={},
            spec_key="ddr_type",
            value="DDR5",
            source="trio_source",
            confidence=1.0,
        )
        is False
    )
    # No schema for the key → False.
    assert (
        spec_would_write(
            db_session,
            category="dram",
            existing_specs={},
            spec_key="no_such_key",
            value=1,
            source="trio_source",
            confidence=1.0,
        )
        is False
    )
    # Enum mismatch → False.
    assert (
        spec_would_write(
            db_session,
            category="dram",
            existing_specs={},
            spec_key="ddr_type",
            value="DDR9",
            source="trio_source",
            confidence=1.0,
        )
        is False
    )
    # Ladder loss (60 vs existing 85) → False; ladder win (95) → True.
    assert (
        spec_would_write(
            db_session,
            category="dram",
            existing_specs=existing,
            spec_key="ddr_type",
            value="DDR3",
            source="spec_extraction",
            confidence=0.99,
        )
        is False
    )
    assert (
        spec_would_write(
            db_session,
            category="dram",
            existing_specs=existing,
            spec_key="ddr_type",
            value="DDR5",
            source="trio_source",
            confidence=1.0,
        )
        is True
    )
    # Read-only: nothing was written by any of the calls above.
    assert card.specs_structured["ddr_type"]["value"] == "DDR4"


def test_record_spec_clamps_out_of_range_confidence(db_session: Session, ddr_card: MaterialCard):
    # A percent-style confidence (95) must never be persisted: it would dominate every
    # same-tier comparison forever. Clamped to 1.0 at the boundary.
    card = ddr_card
    assert record_spec(db_session, card.id, "ddr_type", "DDR4", source="mpn_decode", confidence=95) is True
    assert card.specs_structured["ddr_type"]["confidence"] == 1.0
    facet = db_session.query(MaterialSpecFacet).filter_by(material_card_id=card.id, spec_key="ddr_type").first()
    assert facet.confidence == 1.0
