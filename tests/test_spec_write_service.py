"""tests/test_spec_write_service.py -- Tests for spec write service.

Covers: app/services/spec_write_service.py
Depends on: conftest.py (db_session), faceted search models
"""

from datetime import datetime, timezone

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


# --- Basic write ---


def test_record_spec_creates_facet(db_session: Session):
    card = _make_card(db_session)
    _make_schema(db_session, enum_values=["DDR3", "DDR4", "DDR5"])

    record_spec(db_session, card.id, "ddr_type", "DDR4", source="digikey_api", confidence=0.99)

    facet = db_session.query(MaterialSpecFacet).filter_by(material_card_id=card.id, spec_key="ddr_type").first()
    assert facet is not None
    assert facet.value_text == "DDR4"
    assert facet.category == "dram"


def test_record_spec_writes_jsonb(db_session: Session):
    card = _make_card(db_session)
    _make_schema(db_session, enum_values=["DDR3", "DDR4", "DDR5"])

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


def test_record_spec_rejects_invalid_enum(db_session: Session):
    card = _make_card(db_session)
    _make_schema(db_session, enum_values=["DDR3", "DDR4", "DDR5"])

    record_spec(db_session, card.id, "ddr_type", "DDR99", source="ai", confidence=0.5)

    facet = db_session.query(MaterialSpecFacet).filter_by(material_card_id=card.id, spec_key="ddr_type").first()
    assert facet is None  # Rejected — not written


# --- No schema row → skip silently ---


def test_record_spec_no_schema_skips(db_session: Session):
    card = _make_card(db_session)

    record_spec(db_session, card.id, "unknown_key", "foo", source="ai", confidence=0.5)

    count = db_session.query(MaterialSpecFacet).filter_by(material_card_id=card.id).count()
    assert count == 0


# --- Conflict: non-vendor-API overwrites non-vendor-API (latest wins) ---


def test_conflict_non_api_overwrites_non_api(db_session: Session):
    card = _make_card(db_session)
    _make_schema(db_session, enum_values=["DDR3", "DDR4", "DDR5"])

    record_spec(db_session, card.id, "ddr_type", "DDR3", source="haiku_extraction", confidence=0.85)
    record_spec(db_session, card.id, "ddr_type", "DDR4", source="octopart_scrape", confidence=0.90)

    db_session.refresh(card)
    assert card.specs_structured["ddr_type"]["value"] == "DDR4"
    assert card.specs_structured["ddr_type"]["source"] == "octopart_scrape"


# --- Conflict: vendor API overwrites non-vendor-API ---


def test_conflict_vendor_api_overwrites_non_api(db_session: Session):
    card = _make_card(db_session)
    _make_schema(db_session, enum_values=["DDR3", "DDR4", "DDR5"])

    record_spec(db_session, card.id, "ddr_type", "DDR3", source="haiku_extraction", confidence=0.85)
    record_spec(db_session, card.id, "ddr_type", "DDR4", source="digikey_api", confidence=0.95)

    db_session.refresh(card)
    assert card.specs_structured["ddr_type"]["value"] == "DDR4"
    assert card.specs_structured["ddr_type"]["source"] == "digikey_api"


# --- Conflict: non-vendor-API cannot overwrite vendor API ---


def test_conflict_non_api_cannot_overwrite_vendor_api(db_session: Session):
    card = _make_card(db_session)
    _make_schema(db_session, enum_values=["DDR3", "DDR4", "DDR5"])

    record_spec(db_session, card.id, "ddr_type", "DDR4", source="digikey_api", confidence=0.95)
    record_spec(db_session, card.id, "ddr_type", "DDR3", source="haiku_extraction", confidence=0.99)

    db_session.refresh(card)
    assert card.specs_structured["ddr_type"]["value"] == "DDR4"
    assert card.specs_structured["ddr_type"]["source"] == "digikey_api"


# --- Conflict: vendor API overwrites vendor API (latest wins) ---


def test_conflict_vendor_api_overwrites_vendor_api(db_session: Session):
    card = _make_card(db_session)
    _make_schema(db_session, enum_values=["DDR3", "DDR4", "DDR5"])

    record_spec(db_session, card.id, "ddr_type", "DDR3", source="digikey_api", confidence=0.90)
    record_spec(db_session, card.id, "ddr_type", "DDR4", source="nexar_api", confidence=0.88)

    db_session.refresh(card)
    assert card.specs_structured["ddr_type"]["value"] == "DDR4"
    assert card.specs_structured["ddr_type"]["source"] == "nexar_api"


# --- Upsert: same source updates in place ---


def test_same_source_updates_in_place(db_session: Session):
    card = _make_card(db_session)
    _make_schema(db_session, enum_values=["DDR3", "DDR4", "DDR5"])

    record_spec(db_session, card.id, "ddr_type", "DDR3", source="digikey_api", confidence=0.90)
    record_spec(db_session, card.id, "ddr_type", "DDR4", source="digikey_api", confidence=0.95)

    db_session.refresh(card)
    assert card.specs_structured["ddr_type"]["value"] == "DDR4"


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


def test_record_spec_schema_cache_miss_skips(db_session: Session):
    """When schema_cache is provided but key is missing, record_spec skips."""
    card = _make_card(db_session)
    _make_schema(db_session, enum_values=["DDR3", "DDR4", "DDR5"])

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


# --- Same-source lower confidence is rejected ---


def test_same_source_lower_confidence_rejected(db_session: Session):
    """Same source with lower confidence should NOT overwrite existing value."""
    card = _make_card(db_session)
    _make_schema(db_session, enum_values=["DDR3", "DDR4", "DDR5"])

    record_spec(db_session, card.id, "ddr_type", "DDR4", source="digikey_api", confidence=0.95)
    record_spec(db_session, card.id, "ddr_type", "DDR3", source="digikey_api", confidence=0.80)

    db_session.refresh(card)
    assert card.specs_structured["ddr_type"]["value"] == "DDR4"
    assert card.specs_structured["ddr_type"]["confidence"] == 0.95


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
