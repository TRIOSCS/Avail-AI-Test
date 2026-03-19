"""tests/test_faceted_search_service.py -- Tests for faceted search queries.

Covers: app/services/faceted_search_service.py
Depends on: conftest.py, faceted search models, commodity_registry
"""

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import CommoditySpecSchema, MaterialCard, MaterialSpecFacet
from app.services.faceted_search_service import (
    get_commodity_counts,
    get_facet_counts,
    get_subfilter_options,
    search_materials_faceted,
)
from tests.conftest import engine  # noqa: F401


def _seed_dram_schema(db: Session) -> None:
    """Insert DRAM spec schemas for testing."""
    for spec in [
        {
            "spec_key": "ddr_type",
            "display_name": "DDR Type",
            "data_type": "enum",
            "enum_values": ["DDR3", "DDR4", "DDR5"],
        },
        {"spec_key": "capacity_gb", "display_name": "Capacity (GB)", "data_type": "numeric", "canonical_unit": "GB"},
        {"spec_key": "ecc", "display_name": "ECC", "data_type": "boolean"},
    ]:
        db.add(CommoditySpecSchema(commodity="dram", sort_order=0, is_filterable=True, is_primary=False, **spec))
    db.flush()


def _make_dram_card(db: Session, mpn: str, ddr: str, capacity: float, ecc: bool = False) -> MaterialCard:
    """Create a DRAM card with facet rows."""
    card = MaterialCard(
        normalized_mpn=mpn.lower(),
        display_mpn=mpn,
        manufacturer="TestCo",
        category="DRAM",
        created_at=datetime.now(timezone.utc),
    )
    db.add(card)
    db.flush()
    for spec_key, val_text, val_num in [
        ("ddr_type", ddr, None),
        ("capacity_gb", None, capacity),
        ("ecc", "true" if ecc else "false", None),
    ]:
        db.add(
            MaterialSpecFacet(
                material_card_id=card.id,
                category="dram",
                spec_key=spec_key,
                value_text=val_text,
                value_numeric=val_num,
            )
        )
    db.flush()
    return card


# --- Commodity counts ---


def test_get_commodity_counts(db_session: Session):
    _seed_dram_schema(db_session)
    _make_dram_card(db_session, "MEM-001", "DDR4", 16)
    _make_dram_card(db_session, "MEM-002", "DDR5", 32)

    # Add a capacitor card (no facets, just category)
    cap = MaterialCard(
        normalized_mpn="cap-001",
        display_mpn="CAP-001",
        manufacturer="TestCo",
        category="Capacitors",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(cap)
    db_session.flush()

    counts = get_commodity_counts(db_session)
    assert counts["dram"] == 2
    assert counts["capacitors"] == 1


# --- Facet counts ---


def test_get_facet_counts_for_dram(db_session: Session):
    _seed_dram_schema(db_session)
    _make_dram_card(db_session, "MEM-001", "DDR4", 16)
    _make_dram_card(db_session, "MEM-002", "DDR4", 32)
    _make_dram_card(db_session, "MEM-003", "DDR5", 16)

    counts = get_facet_counts(db_session, "dram")
    assert counts["ddr_type"]["DDR4"] == 2
    assert counts["ddr_type"]["DDR5"] == 1


# --- Faceted search ---


def test_search_materials_faceted_by_commodity(db_session: Session):
    _seed_dram_schema(db_session)
    _make_dram_card(db_session, "MEM-001", "DDR4", 16)

    cap = MaterialCard(
        normalized_mpn="cap-001",
        display_mpn="CAP-001",
        manufacturer="TestCo",
        category="Capacitors",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(cap)
    db_session.flush()

    results, total = search_materials_faceted(db_session, commodity="dram")
    assert total == 1
    assert results[0].normalized_mpn == "mem-001"


def test_search_materials_faceted_with_subfilters(db_session: Session):
    _seed_dram_schema(db_session)
    _make_dram_card(db_session, "MEM-001", "DDR4", 16)
    _make_dram_card(db_session, "MEM-002", "DDR5", 32)
    _make_dram_card(db_session, "MEM-003", "DDR4", 32)

    results, total = search_materials_faceted(
        db_session,
        commodity="dram",
        sub_filters={"ddr_type": ["DDR4"]},
    )
    assert total == 2
    mpns = {r.normalized_mpn for r in results}
    assert mpns == {"mem-001", "mem-003"}


def test_search_materials_faceted_numeric_range(db_session: Session):
    _seed_dram_schema(db_session)
    _make_dram_card(db_session, "MEM-001", "DDR4", 16)
    _make_dram_card(db_session, "MEM-002", "DDR4", 32)

    results, total = search_materials_faceted(
        db_session,
        commodity="dram",
        sub_filters={"capacity_gb_min": 20},
    )
    assert total == 1
    assert results[0].normalized_mpn == "mem-002"


# --- Sub-filter options ---


def test_get_subfilter_options(db_session: Session):
    _seed_dram_schema(db_session)
    _make_dram_card(db_session, "MEM-001", "DDR4", 16)
    _make_dram_card(db_session, "MEM-002", "DDR5", 32)

    options = get_subfilter_options(db_session, "dram")
    assert len(options) == 3  # ddr_type, capacity_gb, ecc
    ddr_opt = next(o for o in options if o["spec_key"] == "ddr_type")
    assert "DDR4" in ddr_opt["values"]
    assert "DDR5" in ddr_opt["values"]
