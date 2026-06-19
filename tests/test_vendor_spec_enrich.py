"""tests/test_vendor_spec_enrich.py — the vendor-API parametric-enrichment WRITER.

Drives ``app.services.vendor_spec_enrich.enrich_card_from_mouser``: a Mouser result
(rich, consistent DESCRIPTION + a category string but NO structured parametric fields)
is categorized + parsed into spec facets through the F1 ladder (connector_desc/tier 84).

Depends on: conftest.py (db_session), seed_commodity_schemas, MaterialCard +
MaterialSpecFacet, spec_tiers.SOURCE_TIER (connector_desc=84), the desc grammar
(capacitors + resistors registered).
"""

from sqlalchemy.orm import Session

from app.models import MaterialCard, MaterialSpecFacet
from app.services.commodity_registry import seed_commodity_schemas
from app.services.spec_tiers import SOURCE_TIER, set_category
from app.services.spec_write_service import load_schema_cache, record_spec
from app.services.vendor_spec_enrich import enrich_card_from_element14, enrich_card_from_mouser


def _facets(db: Session, card_id: int) -> dict:
    rows = db.query(MaterialSpecFacet).filter_by(material_card_id=card_id).all()
    return {r.spec_key: r for r in rows}


# A Mouser capacitor result shaped like the live API: a rich description, a category
# string, and NO structured parametric attributes (the design's central finding).
_CAP_RESULT = {
    "manufacturer": "Murata",
    "category": "Multilayer Ceramic Capacitors MLCC - SMD/SMT",
    "description": "Multilayer Ceramic Capacitors MLCC - SMD/SMT 16V 0.1uF X7R 0402 10%",
    "source_type": "mouser",
}

_RES_RESULT = {
    "manufacturer": "Yageo",
    "category": "Chip Resistor - Surface Mount",
    "description": "Thick Film Resistors - SMD 10 kOhms 1% 0.1W 0402",
    "source_type": "mouser",
}


def _card(db: Session, mpn: str) -> MaterialCard:
    card = MaterialCard(normalized_mpn=mpn.lower(), display_mpn=mpn, category=None)
    db.add(card)
    db.flush()
    return card


def test_capacitor_categorized_and_facets_written(db_session: Session):
    seed_commodity_schemas(db_session)
    card = _card(db_session, "GRM155R71C104KA88D")

    summary = enrich_card_from_mouser(db_session, card, [_CAP_RESULT])
    db_session.commit()

    assert card.category == "capacitors"
    assert card.category_source == "connector_desc"
    assert card.category_tier == SOURCE_TIER["connector_desc"] == 84

    facets = _facets(db_session, card.id)
    # The description grammar populated the parametric facets.
    assert "capacitance" in facets
    assert "voltage_rating" in facets
    assert "dielectric" in facets and facets["dielectric"].value_text == "X7R"
    assert "package" in facets and facets["package"].value_text == "0402"
    for f in facets.values():
        assert f.source == "connector_desc"
        assert f.tier == 84

    assert summary["categorized"] == 1
    assert summary["specs_written"] == len(facets)
    assert summary["specs_written"] >= 4


def test_resistor_categorized_and_facets_written(db_session: Session):
    seed_commodity_schemas(db_session)
    card = _card(db_session, "RC0402FR-0710KL")

    summary = enrich_card_from_mouser(db_session, card, [_RES_RESULT])
    db_session.commit()

    assert card.category == "resistors"
    assert card.category_source == "connector_desc"

    facets = _facets(db_session, card.id)
    assert "resistance" in facets
    assert "tolerance" in facets and facets["tolerance"].value_text == "1%"
    assert "package" in facets and facets["package"].value_text == "0402"

    assert summary["categorized"] == 1
    assert summary["specs_written"] == len(facets)


def test_empty_results_no_op(db_session: Session):
    seed_commodity_schemas(db_session)
    card = _card(db_session, "NOHIT")

    summary = enrich_card_from_mouser(db_session, card, [])
    db_session.commit()

    assert card.category is None
    assert _facets(db_session, card.id) == {}
    assert summary == {"categorized": 0, "specs_written": 0}


def test_first_non_empty_result_chosen(db_session: Session):
    # A leading empty/None-description result is skipped for the first usable one.
    seed_commodity_schemas(db_session)
    card = _card(db_session, "GRM155R71C104KA88D")

    summary = enrich_card_from_mouser(
        db_session,
        card,
        [{"category": None, "description": None}, _CAP_RESULT],
    )
    db_session.commit()

    assert card.category == "capacitors"
    assert summary["categorized"] == 1
    assert summary["specs_written"] >= 4


def test_higher_tier_category_wins_ladder_no_cap_facets(db_session: Session):
    # MEDIUM-7(a): a card already categorized 'dram' at trio_source (tier 95) must NOT be
    # reclassified to 'capacitors' by the connector_desc (tier 84) write, and — since the
    # commodity resolved from the result is 'capacitors' but the card stays 'dram' — the
    # facet extraction runs under the (lost-ladder) card category 'dram', so a CAPACITOR
    # description yields NO capacitor facets on a dram card.
    seed_commodity_schemas(db_session)
    card = _card(db_session, "GRM155R71C104KA88D")
    assert set_category(card, "dram", source="trio_source", confidence=0.99)
    db_session.flush()

    summary = enrich_card_from_mouser(db_session, card, [_CAP_RESULT])
    db_session.commit()

    assert card.category == "dram"  # higher-tier trio_source category held the ladder
    assert summary["categorized"] == 0
    facets = _facets(db_session, card.id)
    assert "capacitance" not in facets
    assert "dielectric" not in facets


def test_connector_desc_overwrites_lower_tier_desc_parse_facet(db_session: Session):
    # MEDIUM-7(b): a pre-recorded desc_parse (tier 83) dielectric=X5R on a capacitors card
    # must be overwritten by the connector_desc (tier 84) dielectric=X7R from the Mouser
    # description — the ladder lets the higher tier win.
    seed_commodity_schemas(db_session)
    card = _card(db_session, "GRM155R71C104KA88D")
    assert set_category(card, "capacitors", source="connector_desc", confidence=0.9)
    db_session.flush()

    cache = load_schema_cache(db_session, "capacitors")
    assert record_spec(
        db_session, int(card.id), "dielectric", "X5R", source="desc_parse", confidence=0.9, schema_cache=cache
    )
    db_session.flush()
    assert _facets(db_session, card.id)["dielectric"].value_text == "X5R"

    enrich_card_from_mouser(db_session, card, [_CAP_RESULT])  # description carries X7R
    db_session.commit()

    dielectric = _facets(db_session, card.id)["dielectric"]
    assert dielectric.value_text == "X7R"
    assert dielectric.source == "connector_desc"
    assert dielectric.tier == 84


def test_no_commodity_resolvable_is_a_no_op(db_session: Session):
    # MEDIUM-8: an off-vocab distributor category + a description with no commodity grammar
    # token resolves to NO commodity — the card stays uncategorized and nothing is written.
    seed_commodity_schemas(db_session)
    card = _card(db_session, "UNKNOWN123")

    result = {
        "manufacturer": "Acme",
        "category": "Sockets, Adapters",
        "description": "Generic Adapter Socket 2.54mm pitch",
        "source_type": "mouser",
    }
    summary = enrich_card_from_mouser(db_session, card, [result])
    db_session.commit()

    assert card.category is None
    assert _facets(db_session, card.id) == {}
    assert summary == {"categorized": 0, "specs_written": 0}


def test_no_commit_in_writer(db_session: Session):
    # The writer leaves the txn to the caller — a rollback after it must undo everything.
    seed_commodity_schemas(db_session)
    card = _card(db_session, "GRM155R71C104KA88D")

    enrich_card_from_mouser(db_session, card, [_CAP_RESULT])
    db_session.rollback()

    fresh = db_session.query(MaterialCard).filter_by(normalized_mpn="grm155r71c104ka88d").one_or_none()
    # The card itself was flushed-not-committed and is rolled back too.
    assert fresh is None or fresh.category is None


# ── Element14 structured-attribute writer (tier 90, element14_api) ────────────────────

# An Element14 capacitor result shaped like the live API: a category string + STRUCTURED
# parametrics in `specs` (already mapped to seeded keys by the connector / _vendor_spec_map).
_E14_CAP_RESULT = {
    "manufacturer": "Murata",
    "category": "Capacitors",
    "description": "SMD Multilayer Ceramic Capacitor, 0.1 µF, 16 V",
    "source_type": "element14",
    "specs": {
        "capacitance": "0.1µF",
        "voltage_rating": "16V",
        "dielectric": "X7R",
        "tolerance": "±10%",
        "package": "0402",
    },
    "dropped": {"Operating Temperature Min": "-55°C"},
}


def test_element14_categorizes_and_writes_specs_at_tier_90(db_session: Session):
    seed_commodity_schemas(db_session)
    card = _card(db_session, "GRM155R71C104KA88D")

    summary = enrich_card_from_element14(db_session, card, [_E14_CAP_RESULT])
    db_session.commit()

    assert card.category == "capacitors"
    assert card.category_source == "element14_api"
    assert card.category_tier == SOURCE_TIER["element14_api"] == 90

    facets = _facets(db_session, card.id)
    assert "capacitance" in facets
    assert "voltage_rating" in facets
    assert "dielectric" in facets and facets["dielectric"].value_text == "X7R"
    assert "tolerance" in facets and facets["tolerance"].value_text == "±10%"
    assert "package" in facets and facets["package"].value_text == "0402"
    for f in facets.values():
        assert f.source == "element14_api"
        assert f.tier == 90

    assert summary["categorized"] == 1
    assert summary["specs_written"] == len(facets)


def test_element14_logs_dropped_coverage_gap(db_session: Session):
    """The connector's ``dropped`` (attributes that mapped to no seeded key) must be
    SURFACED as an INFO coverage-gap log — not computed and silently discarded."""
    from loguru import logger

    seed_commodity_schemas(db_session)
    card = _card(db_session, "GRM155R71C104KA88D")

    messages: list[str] = []
    sink = logger.add(lambda m: messages.append(m.record["message"]), level="INFO")
    try:
        enrich_card_from_element14(db_session, card, [_E14_CAP_RESULT])  # carries a dropped attr
    finally:
        logger.remove(sink)

    assert any("unmapped attribute" in m for m in messages), messages


def test_element14_tier_90_beats_lower_tier_desc_parse(db_session: Session):
    # element14_api (90) is authoritative — it overrides a card already carrying a
    # connector_desc (84) dielectric and even sets category over a desc_parse (83) one.
    seed_commodity_schemas(db_session)
    card = _card(db_session, "GRM155R71C104KA88D")
    assert set_category(card, "capacitors", source="connector_desc", confidence=0.9)
    db_session.flush()
    cache = load_schema_cache(db_session, "capacitors")
    assert record_spec(
        db_session, int(card.id), "dielectric", "X5R", source="connector_desc", confidence=0.9, schema_cache=cache
    )
    db_session.flush()
    assert _facets(db_session, card.id)["dielectric"].value_text == "X5R"

    enrich_card_from_element14(db_session, card, [_E14_CAP_RESULT])  # specs carry X7R
    db_session.commit()

    dielectric = _facets(db_session, card.id)["dielectric"]
    assert dielectric.value_text == "X7R"
    assert dielectric.source == "element14_api"
    assert dielectric.tier == 90


def test_element14_does_not_clobber_higher_tier_trio_source(db_session: Session):
    # trio_source category (95) outranks element14_api (90): the card stays its trio
    # commodity and a capacitor result writes no capacitor facets on it.
    seed_commodity_schemas(db_session)
    card = _card(db_session, "GRM155R71C104KA88D")
    assert set_category(card, "dram", source="trio_source", confidence=0.99)
    db_session.flush()

    summary = enrich_card_from_element14(db_session, card, [_E14_CAP_RESULT])
    db_session.commit()

    assert card.category == "dram"
    assert summary["categorized"] == 0
    assert "capacitance" not in _facets(db_session, card.id)


def test_element14_bad_enum_value_dropped_by_schema_gate(db_session: Session):
    # A tolerance that does not match the seed enum after normalization is rejected by
    # record_spec's enum gate (never coerced); the valid specs still land.
    seed_commodity_schemas(db_session)
    card = _card(db_session, "BADTOL")
    result = {
        "category": "Capacitors",
        "description": "MLCC",
        "specs": {"capacitance": "0.1µF", "tolerance": "±3%"},  # ±3% not in the cap enum
    }
    summary = enrich_card_from_element14(db_session, card, [result])
    db_session.commit()

    facets = _facets(db_session, card.id)
    assert "capacitance" in facets
    assert "tolerance" not in facets  # off-enum value dropped by the schema gate
    assert summary["specs_written"] == 1


def test_element14_no_specs_result_is_categorize_only(db_session: Session):
    # A result with a category but no structured specs still categorizes the card.
    seed_commodity_schemas(db_session)
    card = _card(db_session, "CATONLY")
    result = {"category": "Capacitors", "description": "MLCC", "specs": {}}

    summary = enrich_card_from_element14(db_session, card, [result])
    db_session.commit()

    assert card.category == "capacitors"
    assert summary["categorized"] == 1
    assert summary["specs_written"] == 0


def test_element14_empty_results_no_op(db_session: Session):
    seed_commodity_schemas(db_session)
    card = _card(db_session, "NOHIT")
    summary = enrich_card_from_element14(db_session, card, [])
    db_session.commit()
    assert card.category is None
    assert summary == {"categorized": 0, "specs_written": 0}


def test_element14_no_commit_in_writer(db_session: Session):
    seed_commodity_schemas(db_session)
    card = _card(db_session, "GRM155R71C104KA88D")
    enrich_card_from_element14(db_session, card, [_E14_CAP_RESULT])
    db_session.rollback()
    fresh = db_session.query(MaterialCard).filter_by(normalized_mpn="grm155r71c104ka88d").one_or_none()
    assert fresh is None or fresh.category is None


def test_element14_resistor_tolerance_lands_end_to_end(db_session: Session):
    # End-to-end through the REAL connector _parse: a resistor whose Element14 tolerance
    # arrives as "± 1%" must land in the bare resistor enum ("1%") at tier 90 — the
    # commodity-aware sign normalization is what makes the headline facet populate.
    from app.connectors.element14 import Element14Connector

    data = {
        "manufacturerPartNumberSearchReturn": {
            "products": [
                {
                    "brandName": "Yageo",
                    "translatedManufacturerPartNumber": "RC0402FR-0710KL",
                    "displayName": "SMD Chip Resistor, 10 kohm",
                    "sku": "9",
                    "category": {"name": "Resistors"},
                    "attributes": [
                        {"attributeLabel": "Resistance", "attributeValue": "10kΩ"},
                        {"attributeLabel": "Power Rating", "attributeValue": "0.063W"},
                        {"attributeLabel": "Resistance Tolerance", "attributeValue": "± 1%"},
                        {"attributeLabel": "Case Style", "attributeValue": "0402 [1005 Metric]"},
                    ],
                }
            ]
        }
    }
    results = Element14Connector(api_key="k")._parse(data, "RC0402FR-0710KL")
    seed_commodity_schemas(db_session)
    card = _card(db_session, "RC0402FR-0710KL")

    enrich_card_from_element14(db_session, card, results)
    db_session.commit()

    assert card.category == "resistors"
    facets = _facets(db_session, card.id)
    assert "tolerance" in facets and facets["tolerance"].value_text == "1%"
    assert facets["tolerance"].source == "element14_api" and facets["tolerance"].tier == 90
    assert "package" in facets and facets["package"].value_text == "0402"
