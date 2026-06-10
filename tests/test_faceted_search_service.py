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
    get_global_facet_counts,
    get_manufacturer_options,
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


# --- Facet counts with active_filters ---


def test_get_facet_counts_with_active_filters(db_session: Session):
    """When active_filters narrow the card set, counts for other specs reflect filtered
    subset."""
    _seed_dram_schema(db_session)
    _make_dram_card(db_session, "MEM-001", "DDR4", 16, ecc=True)
    _make_dram_card(db_session, "MEM-002", "DDR4", 32, ecc=False)
    _make_dram_card(db_session, "MEM-003", "DDR5", 16, ecc=True)

    # Filter to DDR4 only. OTHER facets (ecc) reflect the DDR4 subset...
    counts = get_facet_counts(db_session, "dram", active_filters={"ddr_type": ["DDR4"]})
    # Only DDR4 cards: MEM-001 (ecc=true) and MEM-002 (ecc=false)
    assert counts["ecc"]["true"] == 1
    assert counts["ecc"]["false"] == 1
    # ...but ddr_type SELF-EXCLUDES — its own counts ignore the ddr_type selection, so the
    # sibling DDR5 is NOT collapsed (OR-within-facet correctness; see test_facet_counts_self_exclusion).
    assert counts["ddr_type"]["DDR4"] == 2
    assert counts["ddr_type"]["DDR5"] == 1


# --- Text search with q parameter ---


def test_search_materials_faceted_text_search_by_mpn(db_session: Session):
    """Search by partial MPN returns matching cards."""
    _seed_dram_schema(db_session)
    _make_dram_card(db_session, "MEM-001", "DDR4", 16)
    _make_dram_card(db_session, "XYZ-999", "DDR5", 32)

    results, total = search_materials_faceted(db_session, q="MEM")
    assert total == 1
    assert results[0].display_mpn == "MEM-001"


def test_search_materials_faceted_text_search_by_manufacturer(db_session: Session):
    """Search by manufacturer returns matching cards."""
    _seed_dram_schema(db_session)
    _make_dram_card(db_session, "MEM-001", "DDR4", 16)

    # All test cards have manufacturer "TestCo"
    results, total = search_materials_faceted(db_session, q="TestCo")
    assert total == 1
    assert results[0].manufacturer == "TestCo"


# --- Pagination with offset ---


def test_search_materials_faceted_pagination_offset(db_session: Session):
    """Offset skips the correct number of results."""
    _seed_dram_schema(db_session)
    for i in range(5):
        _make_dram_card(db_session, f"MEM-{i:03d}", "DDR4", 16)

    # Get all to confirm total
    all_results, total = search_materials_faceted(db_session, commodity="dram", limit=50)
    assert total == 5

    # Offset 3 should return 2 results
    page_results, page_total = search_materials_faceted(
        db_session,
        commodity="dram",
        limit=50,
        offset=3,
    )
    assert page_total == 5  # Total count unchanged
    assert len(page_results) == 2  # Only 2 remaining after offset


# --- Manufacturer options ---


def test_get_manufacturer_options_returns_sorted_list(db_session: Session):
    """get_manufacturer_options returns distinct manufacturers sorted by count."""
    db_session.add(
        MaterialCard(normalized_mpn="a", display_mpn="A", manufacturer="Texas Instruments", category="resistors")
    )
    db_session.add(
        MaterialCard(normalized_mpn="b", display_mpn="B", manufacturer="Texas Instruments", category="resistors")
    )
    db_session.add(MaterialCard(normalized_mpn="c", display_mpn="C", manufacturer="Murata", category="capacitors"))
    db_session.flush()

    result = get_manufacturer_options(db_session)
    assert len(result) == 2
    assert result[0]["name"] == "Texas Instruments"
    assert result[0]["count"] == 2
    assert result[1]["name"] == "Murata"


def test_get_manufacturer_options_scoped_to_commodity(db_session: Session):
    """When commodity is given, only return manufacturers in that commodity."""
    db_session.add(MaterialCard(normalized_mpn="a", display_mpn="A", manufacturer="TI", category="resistors"))
    db_session.add(MaterialCard(normalized_mpn="b", display_mpn="B", manufacturer="Murata", category="capacitors"))
    db_session.flush()

    result = get_manufacturer_options(db_session, commodity="resistors")
    assert len(result) == 1
    assert result[0]["name"] == "TI"


# --- Manufacturer filter in search ---


def test_search_faceted_filters_by_manufacturer(db_session: Session):
    """search_materials_faceted respects manufacturer filter."""
    db_session.add(MaterialCard(normalized_mpn="a", display_mpn="A", manufacturer="TI", category="resistors"))
    db_session.add(MaterialCard(normalized_mpn="b", display_mpn="B", manufacturer="Murata", category="resistors"))
    db_session.flush()

    results, total = search_materials_faceted(db_session, commodity="resistors", manufacturers=["TI"])
    assert total == 1
    assert results[0].manufacturer == "TI"


# --- Global facets: lifecycle / rohs / has_datasheet ---


def _mk_global(db, mpn, *, lifecycle=None, rohs=None, datasheet=None):
    card = MaterialCard(
        normalized_mpn=mpn,
        display_mpn=mpn.upper(),
        category="resistors",
        lifecycle_status=lifecycle,
        rohs_status=rohs,
        datasheet_url=datasheet,
        created_at=datetime.now(timezone.utc),
    )
    db.add(card)
    db.flush()
    return card


def test_search_faceted_lifecycle_filter(db_session: Session):
    _mk_global(db_session, "lc-active", lifecycle="active")
    _mk_global(db_session, "lc-eol", lifecycle="eol")
    _mk_global(db_session, "lc-obs", lifecycle="obsolete")

    results, total = search_materials_faceted(db_session, lifecycle=["active", "eol"])
    mpns = {c.normalized_mpn for c in results}
    assert mpns == {"lc-active", "lc-eol"}
    assert total == 2


def test_search_faceted_rohs_filter(db_session: Session):
    _mk_global(db_session, "rohs-ok", rohs="compliant")
    _mk_global(db_session, "rohs-no", rohs="non-compliant")
    _mk_global(db_session, "rohs-ex", rohs="exempt")

    results, total = search_materials_faceted(db_session, rohs=["compliant", "exempt"])
    mpns = {c.normalized_mpn for c in results}
    assert mpns == {"rohs-ok", "rohs-ex"}
    assert total == 2


def test_search_faceted_has_datasheet_filter(db_session: Session):
    _mk_global(db_session, "ds-yes", datasheet="https://example.com/ds.pdf")
    _mk_global(db_session, "ds-no", datasheet=None)

    results, total = search_materials_faceted(db_session, has_datasheet=True)
    mpns = {c.normalized_mpn for c in results}
    assert mpns == {"ds-yes"}
    assert total == 1

    # has_datasheet=False is a no-op → both rows returned.
    _, total_all = search_materials_faceted(db_session, has_datasheet=False)
    assert total_all == 2


def test_search_faceted_global_facets_combine_with_status(db_session: Session):
    """Global facets AND with the trust-ladder statuses filter."""
    c1 = _mk_global(db_session, "combo-1", lifecycle="active")
    c1.enrichment_status = "verified"
    c2 = _mk_global(db_session, "combo-2", lifecycle="active")
    c2.enrichment_status = "ai_inferred"
    db_session.flush()

    results, total = search_materials_faceted(db_session, lifecycle=["active"], statuses=["verified"])
    assert {c.normalized_mpn for c in results} == {"combo-1"}
    assert total == 1


def test_get_global_facet_counts(db_session: Session):
    _mk_global(db_session, "g-1", lifecycle="active", rohs="compliant", datasheet="https://x/1.pdf")
    _mk_global(db_session, "g-2", lifecycle="active", rohs="exempt", datasheet=None)
    _mk_global(db_session, "g-3", lifecycle="eol", rohs="compliant", datasheet="https://x/3.pdf")

    counts = get_global_facet_counts(db_session)
    assert counts["lifecycle"]["active"] == 2
    assert counts["lifecycle"]["eol"] == 1
    assert counts["rohs"]["compliant"] == 2
    assert counts["rohs"]["exempt"] == 1
    assert counts["has_datasheet"]["true"] == 2


class TestFacetedSearchEdgeCases:
    """Boundary and validation edge cases for faceted search."""

    def test_empty_commodity_returns_all(self, db_session):
        _seed_dram_schema(db_session)
        _make_dram_card(db_session, "MEM-001", "DDR4", 16)
        _make_dram_card(db_session, "MEM-002", "DDR5", 32)
        results, total = search_materials_faceted(db_session, commodity=None)
        assert total >= 2

    def test_nonexistent_commodity_returns_empty(self, db_session):
        _seed_dram_schema(db_session)
        _make_dram_card(db_session, "MEM-001", "DDR4", 16)
        results, total = search_materials_faceted(db_session, commodity="nonexistent_xyz")
        assert total == 0
        assert results == []

    def test_offset_beyond_total_returns_empty(self, db_session):
        _seed_dram_schema(db_session)
        _make_dram_card(db_session, "MEM-001", "DDR4", 16)
        results, total = search_materials_faceted(db_session, commodity="dram", offset=9999)
        assert results == []
        assert total == 1  # total still reflects full count

    def test_limit_zero_returns_empty_results(self, db_session):
        _seed_dram_schema(db_session)
        _make_dram_card(db_session, "MEM-001", "DDR4", 16)
        results, total = search_materials_faceted(db_session, commodity="dram", limit=0)
        assert results == []

    def test_special_chars_in_text_search(self, db_session):
        _seed_dram_schema(db_session)
        _make_dram_card(db_session, "MEM-001", "DDR4", 16)
        results, total = search_materials_faceted(db_session, q="'; DROP TABLE--")
        assert isinstance(results, list)  # no SQL injection crash

    def test_numeric_range_min_equals_max(self, db_session):
        _seed_dram_schema(db_session)
        _make_dram_card(db_session, "MEM-001", "DDR4", 16)
        _make_dram_card(db_session, "MEM-002", "DDR5", 32)
        results, total = search_materials_faceted(
            db_session,
            commodity="dram",
            sub_filters={"capacity_gb_min": 16, "capacity_gb_max": 16},
        )
        assert total == 1

    def test_unicode_manufacturer_filter(self, db_session):
        _seed_dram_schema(db_session)
        results, total = search_materials_faceted(
            db_session,
            manufacturers=["éèü"],
        )
        assert results == []

    def test_get_commodity_counts_empty_db(self, db_session):
        counts = get_commodity_counts(db_session)
        assert counts == {} or len(counts) == 0

    def test_get_manufacturer_options_empty_db(self, db_session):
        options = get_manufacturer_options(db_session)
        assert options == []

    def test_get_subfilter_options_nonexistent_commodity(self, db_session):
        options = get_subfilter_options(db_session, "nonexistent_xyz")
        assert options == []

    def test_search_with_empty_manufacturers_list(self, db_session):
        _seed_dram_schema(db_session)
        _make_dram_card(db_session, "MEM-001", "DDR4", 16)
        results, total = search_materials_faceted(db_session, manufacturers=[])
        assert total >= 1  # empty list should not filter

    def test_search_with_empty_sub_filters(self, db_session):
        _seed_dram_schema(db_session)
        _make_dram_card(db_session, "MEM-001", "DDR4", 16)
        results, total = search_materials_faceted(db_session, sub_filters={})
        assert total >= 1


def test_boolean_subfilter_always_exposes_yes_no(db_session):
    """Boolean specs always expose Yes/No (with counts incl. 0), independent of backing
    data.

    Filters are not gated on whether data currently exists — a (0) count is itself
    useful.
    """
    from app.models.faceted_search import MaterialSpecFacet
    from app.models.intelligence import MaterialCard
    from app.services.commodity_registry import seed_commodity_schemas
    from app.services.faceted_search_service import get_subfilter_options

    seed_commodity_schemas(db_session)
    card = MaterialCard(normalized_mpn="mc2", display_mpn="MC2", category="microcontrollers", description="mcu")
    db_session.add(card)
    db_session.commit()

    opts = {o["spec_key"]: o for o in get_subfilter_options(db_session, "microcontrollers")}
    # A boolean spec with NO facet rows still exposes both toggle values.
    assert opts["has_usb"]["values"] == ["true", "false"]

    # Adding a facet row does not change the offered values.
    db_session.add(
        MaterialSpecFacet(material_card_id=card.id, category="microcontrollers", spec_key="has_usb", value_text="true")
    )
    db_session.commit()
    opts2 = {o["spec_key"]: o for o in get_subfilter_options(db_session, "microcontrollers")}
    assert opts2["has_usb"]["values"] == ["true", "false"]


# --- Operational (Layer-3) sourcing filters ---


def _mk_op(
    db,
    mpn,
    *,
    crosses="unset",
    internal=None,
    last_searched=None,
    searches=0,
    vendor_price="none",
):
    """Card with operational fields.

    crosses: 'unset' keeps the column default ([]).
    vendor_price: 'none' = no vendor rows, None = row without price, number = row with price.
    """
    card = MaterialCard(
        normalized_mpn=mpn,
        display_mpn=mpn.upper(),
        category="resistors",
        is_internal_part=internal,
        last_searched_at=last_searched,
        search_count=searches,
        created_at=datetime.now(timezone.utc),
    )
    if crosses != "unset":
        card.cross_references = crosses
    db.add(card)
    db.flush()
    if vendor_price != "none":
        from app.models import MaterialVendorHistory

        db.add(MaterialVendorHistory(material_card_id=card.id, vendor_name="V1", last_price=vendor_price))
        db.flush()
    return card


def test_search_faceted_has_stock_filter(db_session: Session):
    _mk_op(db_session, "stock-yes", vendor_price=None)  # row exists, price unknown
    _mk_op(db_session, "stock-no")

    results, total = search_materials_faceted(db_session, has_stock=True)
    assert {c.normalized_mpn for c in results} == {"stock-yes"}
    assert total == 1

    _, total_all = search_materials_faceted(db_session, has_stock=False)
    assert total_all == 2  # False is a no-op


def test_search_faceted_has_price_filter(db_session: Session):
    _mk_op(db_session, "price-yes", vendor_price=1.25)
    _mk_op(db_session, "price-null", vendor_price=None)  # sighting without a price
    _mk_op(db_session, "price-none")  # no vendor rows at all

    results, total = search_materials_faceted(db_session, has_price=True)
    assert {c.normalized_mpn for c in results} == {"price-yes"}
    assert total == 1

    # has_stock is the looser predicate: any vendor row counts.
    _, total_stock = search_materials_faceted(db_session, has_stock=True)
    assert total_stock == 2


def test_search_faceted_has_crosses_filter_portable(db_session: Session):
    """has_crosses must hold on rows with SQL NULL, JSON null, [] and a real list.

    The predicate is text-cast based so it behaves identically on PostgreSQL JSONB and
    SQLite JSON-as-text (see feedback_sqlite_masks_postgres).
    """
    from sqlalchemy import null

    # Python None persists as the JSON 'null' encoding (JSON.none_as_null is False);
    # a true SQL NULL needs the explicit null() expression. Both shapes are seeded so
    # both halves of the predicate (isnot(None) + the text-cast notin_) are exercised.
    _mk_op(db_session, "x-json-null", crosses=None)  # JSON null
    _mk_op(db_session, "x-sql-null", crosses=null())  # SQL NULL
    _mk_op(db_session, "x-empty", crosses=[])  # empty list
    _mk_op(db_session, "x-default")  # column default (list)
    _mk_op(db_session, "x-real", crosses=[{"mpn": "ALT-1", "manufacturer": "TI"}])

    results, total = search_materials_faceted(db_session, has_crosses=True)
    assert {c.normalized_mpn for c in results} == {"x-real"}
    assert total == 1

    _, total_all = search_materials_faceted(db_session, has_crosses=False)
    assert total_all == 5  # False is a no-op


def test_search_faceted_internal_tristate(db_session: Session):
    _mk_op(db_session, "int-true", internal=True)
    _mk_op(db_session, "int-false", internal=False)
    _mk_op(db_session, "int-null", internal=None)  # legacy rows: NULL counts as standard

    _, total_all = search_materials_faceted(db_session, internal="all")
    assert total_all == 3

    results, total = search_materials_faceted(db_session, internal="standard")
    assert {c.normalized_mpn for c in results} == {"int-false", "int-null"}
    assert total == 2

    results, total = search_materials_faceted(db_session, internal="internal")
    assert {c.normalized_mpn for c in results} == {"int-true"}
    assert total == 1

    # Unknown value degrades to the "all" no-op (hand-edited URLs).
    _, total_bogus = search_materials_faceted(db_session, internal="bogus")
    assert total_bogus == 3


def test_search_faceted_searched_within_buckets(db_session: Session):
    from datetime import timedelta

    now = datetime.now(timezone.utc)
    _mk_op(db_session, "sw-1d", last_searched=now - timedelta(days=1))
    _mk_op(db_session, "sw-10d", last_searched=now - timedelta(days=10))
    _mk_op(db_session, "sw-45d", last_searched=now - timedelta(days=45))
    _mk_op(db_session, "sw-120d", last_searched=now - timedelta(days=120))
    _mk_op(db_session, "sw-never", last_searched=None)

    cases = {
        "7d": {"sw-1d"},
        "30d": {"sw-1d", "sw-10d"},
        "90d": {"sw-1d", "sw-10d", "sw-45d"},
    }
    for bucket, expected in cases.items():
        results, total = search_materials_faceted(db_session, searched_within=bucket)
        assert {c.normalized_mpn for c in results} == expected, bucket
        assert total == len(expected), bucket

    # "any" and unknown values are no-ops — never-searched rows included.
    for bucket in ("any", "bogus"):
        _, total_all = search_materials_faceted(db_session, searched_within=bucket)
        assert total_all == 5, bucket


def test_search_faceted_min_searches(db_session: Session):
    _mk_op(db_session, "ms-0", searches=0)
    _mk_op(db_session, "ms-3", searches=3)
    _mk_op(db_session, "ms-9", searches=9)

    results, total = search_materials_faceted(db_session, min_searches=3)
    assert {c.normalized_mpn for c in results} == {"ms-3", "ms-9"}  # boundary inclusive
    assert total == 2

    _, total_all = search_materials_faceted(db_session, min_searches=0)
    assert total_all == 3  # 0 is a no-op


def test_search_faceted_operational_filters_combine(db_session: Session):
    """Operational filters AND together and with global facets."""
    keep = _mk_op(
        db_session,
        "combo-keep",
        crosses=[{"mpn": "ALT-9"}],
        internal=False,
        searches=5,
        vendor_price=2.0,
    )
    keep.lifecycle_status = "active"
    drop = _mk_op(db_session, "combo-drop", crosses=[{"mpn": "ALT-8"}], internal=True, searches=5, vendor_price=2.0)
    drop.lifecycle_status = "active"
    db_session.flush()

    results, total = search_materials_faceted(
        db_session,
        lifecycle=["active"],
        has_stock=True,
        has_price=True,
        has_crosses=True,
        internal="standard",
        min_searches=5,
    )
    assert {c.normalized_mpn for c in results} == {"combo-keep"}
    assert total == 1


# --- Commodity spec coverage ---


def test_get_commodity_spec_coverage(db_session: Session):
    from app.services.faceted_search_service import SpecCoverage, get_commodity_spec_coverage

    _seed_dram_schema(db_session)
    _make_dram_card(db_session, "COV-001", "DDR4", 16)  # has facet rows
    no_specs = MaterialCard(
        normalized_mpn="cov-002",
        display_mpn="COV-002",
        category="DRAM",
        created_at=datetime.now(timezone.utc),
    )
    deleted = MaterialCard(
        normalized_mpn="cov-003",
        display_mpn="COV-003",
        category="DRAM",
        deleted_at=datetime.now(timezone.utc),
        created_at=datetime.now(timezone.utc),
    )
    other_cat = MaterialCard(
        normalized_mpn="cov-004",
        display_mpn="COV-004",
        category="Capacitors",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add_all([no_specs, deleted, other_cat])
    db_session.flush()

    coverage = get_commodity_spec_coverage(db_session, "dram")
    assert coverage == SpecCoverage(with_specs=1, total=2)  # deleted + other-category excluded

    # Case-insensitive commodity key, and an unknown commodity yields zeros.
    assert get_commodity_spec_coverage(db_session, "  DRAM ") == SpecCoverage(with_specs=1, total=2)
    assert get_commodity_spec_coverage(db_session, "nonexistent_xyz") == SpecCoverage(with_specs=0, total=0)
