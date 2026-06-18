"""tests/test_faceted_search_service.py -- Tests for faceted search queries.

Covers: app/services/faceted_search_service.py
Depends on: conftest.py, faceted search models, commodity_registry
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session

from app.models import CommoditySpecSchema, MaterialCard, MaterialSpecFacet
from app.services.faceted_search_service import (
    _natural_sort_key,
    get_commodity_counts,
    get_facet_counts,
    get_global_facet_counts,
    get_manufacturer_options,
    get_subfilter_options,
    search_materials_faceted,
)
from tests.conftest import (
    engine,  # noqa: F401
    force_card_category,
)


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
        category="dram",
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
        category="capacitors",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(cap)
    db_session.flush()

    counts = get_commodity_counts(db_session)
    assert counts["dram"] == 2
    assert counts["capacitors"] == 1


def test_get_commodity_counts_expression_equivalence(db_session: Session):
    """Pin the semantics of the 098 query-shape rewrite (filter + GROUP BY on
    lower(trim(category)) with count(*), for an index-only scan over
    ix_mc_cat_order_live):

    - NULL, empty, and whitespace-only categories are all dropped (lower/trim are
      strict, so the expression filter selects the same rows as the raw column;
      whitespace-only collapses to '' and dies in the ``if cat`` comprehension);
    - case/padding variants merge into one key (' ddr4 ' + 'DDR4' -> 'ddr4');
    - soft-deleted rows are excluded;
    - count(*) over those groups equals the row counts (id is the non-null PK).
    """
    now = datetime.now(timezone.utc)
    rows = [
        (None, None),
        ("", None),
        ("   ", None),
        ("DDR4", None),
        (" ddr4 ", None),
        ("Capacitors", None),
        ("DDR4", now),  # soft-deleted — must not be counted
    ]
    # The whole point of this test is non-canonical case/padding residue (legacy
    # rows the @validates guard would now reject), so seed the raw values through
    # force_card_category exactly as a pre-guard writer left them in the DB.
    for i, (category, deleted_at) in enumerate(rows):
        card = MaterialCard(
            normalized_mpn=f"eq-{i:03d}",
            display_mpn=f"EQ-{i:03d}",
            manufacturer="TestCo",
            created_at=now,
            deleted_at=deleted_at,
        )
        db_session.add(card)
        db_session.flush()
        if category is not None:
            force_card_category(db_session, card, category)

    assert get_commodity_counts(db_session) == {"ddr4": 2, "capacitors": 1}


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
        category="capacitors",
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


def test_natural_sort_key_orders_numeric_runs():
    """Digit runs compare as ints so '205' sorts before '1210' (not lexically)."""
    assert sorted(["1210", "0805", "205"], key=_natural_sort_key) == ["205", "0805", "1210"]


def test_get_subfilter_options_natural_sorts_enum_overflow(db_session: Session):
    """Fixed-vocab enum: canonical values keep their curated order, then observed
    overflow values (not in the canonical list) append in NUMERIC order."""
    db_session.add(
        CommoditySpecSchema(
            commodity="capacitors",
            spec_key="package",
            display_name="Package",
            data_type="enum",
            enum_values=["0402", "0603"],
            sort_order=0,
            is_filterable=True,
            is_primary=False,
        )
    )
    db_session.flush()

    # Overflow values (none are in the canonical list); "0805" sorts as 805,
    # between 205 and 1210 — proving the numeric (not lexical) order. Each value
    # lives on its own card ((material_card_id, spec_key) is UNIQUE).
    for i, value in enumerate(["1210", "0805", "205"]):
        card = MaterialCard(
            normalized_mpn=f"cap-pkg-{i}",
            display_mpn=f"CAP-PKG-{i}",
            manufacturer="TestCo",
            category="capacitors",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(card)
        db_session.flush()
        db_session.add(
            MaterialSpecFacet(
                material_card_id=card.id,
                category="capacitors",
                spec_key="package",
                value_text=value,
            )
        )
    db_session.flush()

    options = get_subfilter_options(db_session, "capacitors")
    pkg_opt = next(o for o in options if o["spec_key"] == "package")
    assert pkg_opt["values"] == ["0402", "0603", "205", "0805", "1210"]


# --- Numeric common-value chips (P2 backend) ---


def test_search_materials_faceted_numeric_vals_in(db_session: Session):
    """``{spec_key}__vals`` filters via ``value_numeric IN (...)`` (OR-within-facet),
    returning exactly the selected discrete values — never the in-between ones."""
    _seed_dram_schema(db_session)
    _make_dram_card(db_session, "MEM-008", "DDR4", 8)
    _make_dram_card(db_session, "MEM-016", "DDR4", 16)
    _make_dram_card(db_session, "MEM-032", "DDR5", 32)

    results, total = search_materials_faceted(
        db_session,
        commodity="dram",
        sub_filters={"capacity_gb__vals": [8, 32]},
    )
    assert total == 2  # the 8 and 32 cards, NOT the in-between 16
    assert {r.normalized_mpn for r in results} == {"mem-008", "mem-032"}


def test_subfilter_numeric_chips_top_n_ordered_by_value(db_session: Session):
    """Numeric specs expose ``chips`` = top-N values by DISTINCT-card count, displayed
    ascending by value, alongside the existing min/max ``range``."""
    _seed_dram_schema(db_session)
    # 16GB on 3 cards, 8GB on 2, 32GB on 1 → all three are chips; display value-ascending.
    for cap, n in [(8.0, 2), (16.0, 3), (32.0, 1)]:
        for i in range(n):
            _make_dram_card(db_session, f"MEM-{int(cap)}-{i}", "DDR4", cap)

    opt = {o["spec_key"]: o for o in get_subfilter_options(db_session, "dram")}["capacity_gb"]
    assert [c["value"] for c in opt["chips"]] == [8.0, 16.0, 32.0]
    assert {c["value"]: c["count"] for c in opt["chips"]} == {8.0: 2, 16.0: 3, 32.0: 1}
    assert opt["range"]["min"] == 8.0 and opt["range"]["max"] == 32.0


def test_subfilter_numeric_chips_truncated_to_n(db_session: Session):
    """More than ``NUMERIC_CHIP_N`` distinct values → keep the N most common, then
    display the kept set ascending by value."""
    from app.services.faceted_search_service import NUMERIC_CHIP_N

    _seed_dram_schema(db_session)
    # Distinct counts 1..N+2 so the two least-common values (count 1 and 2) are dropped.
    caps = [(float(8 * (i + 1)), i + 1) for i in range(NUMERIC_CHIP_N + 2)]
    for cap, n in caps:
        for i in range(n):
            _make_dram_card(db_session, f"MEM-{int(cap)}-{i}", "DDR4", cap)

    opt = {o["spec_key"]: o for o in get_subfilter_options(db_session, "dram")}["capacity_gb"]
    kept = {c["value"] for c in opt["chips"]}
    assert len(opt["chips"]) == NUMERIC_CHIP_N
    # The two lowest-count values (the first two seeded) are excluded.
    assert caps[0][0] not in kept and caps[1][0] not in kept
    # Displayed ascending by value.
    assert [c["value"] for c in opt["chips"]] == sorted(kept)


def test_subfilter_numeric_chips_empty_without_rows(db_session: Session):
    """A numeric spec with no facet rows → no chips (empty list), range absent/None."""
    _seed_dram_schema(db_session)
    # ddr_type/ecc cards exist but carry NO capacity_gb numeric rows.
    card = MaterialCard(
        normalized_mpn="no-cap",
        display_mpn="NO-CAP",
        manufacturer="TestCo",
        category="dram",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(card)
    db_session.flush()
    db_session.add(MaterialSpecFacet(material_card_id=card.id, category="dram", spec_key="ddr_type", value_text="DDR4"))
    db_session.flush()

    opt = {o["spec_key"]: o for o in get_subfilter_options(db_session, "dram")}["capacity_gb"]
    assert opt.get("chips") == []


def test_facet_counts_numeric_chip_pass1(db_session: Session):
    """Numeric chip counts (pass 1) are string-keyed by value and reflect the active
    card scope."""
    _seed_dram_schema(db_session)
    _make_dram_card(db_session, "MEM-008", "DDR4", 8)
    _make_dram_card(db_session, "MEM-008b", "DDR4", 8)
    _make_dram_card(db_session, "MEM-016", "DDR4", 16)
    _make_dram_card(db_session, "MEM-016b", "DDR4", 16)
    _make_dram_card(db_session, "MEM-016c", "DDR4", 16)

    counts = get_facet_counts(db_session, "dram")
    assert counts["capacity_gb"]["8.0"] == 2
    assert counts["capacity_gb"]["16.0"] == 3


def test_facet_counts_numeric_chip_self_exclusion(db_session: Session):
    """With one chip actively selected, the facet's OWN counts still include the
    unselected sibling at its full count (OR-within-facet), keyed as ``str(value)``."""
    _seed_dram_schema(db_session)
    _make_dram_card(db_session, "MEM-008", "DDR4", 8)
    _make_dram_card(db_session, "MEM-008b", "DDR4", 8)
    _make_dram_card(db_session, "MEM-016", "DDR4", 16)
    _make_dram_card(db_session, "MEM-016b", "DDR4", 16)
    _make_dram_card(db_session, "MEM-016c", "DDR4", 16)

    counts = get_facet_counts(db_session, "dram", active_filters={"capacity_gb__vals": [8]})
    # Self-exclusion: selecting 8 must NOT collapse the sibling 16's count.
    assert counts["capacity_gb"].get("16.0") == 3
    assert counts["capacity_gb"].get("8.0") == 2


def test_facet_counts_numeric_chip_narrows_other_facets(db_session: Session):
    """A ``__vals`` selection narrows OTHER facets (AND-across-facets), pass 1."""
    _seed_dram_schema(db_session)
    _make_dram_card(db_session, "MEM-008", "DDR4", 8)
    _make_dram_card(db_session, "MEM-016", "DDR5", 16)

    counts = get_facet_counts(db_session, "dram", active_filters={"capacity_gb__vals": [8]})
    # Only the 8GB card (DDR4) is in scope, so the ddr_type facet shows DDR4 only.
    assert counts["ddr_type"].get("DDR4") == 1
    assert "DDR5" not in counts["ddr_type"]


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


# --- Dual-brand: combined facet ORs across brand + manufacturer (migration 097) ---


def _dual_card(db, mpn, *, brand=None, manufacturer=None, category="hdd"):
    card = MaterialCard(
        normalized_mpn=mpn,
        display_mpn=mpn.upper(),
        brand=brand,
        manufacturer=manufacturer,
        category=category,
    )
    db.add(card)
    return card


def test_filter_matches_brand_only_manufacturer_only_and_both(db_session: Session):
    # The headline: filtering "IBM" matches the IBM-labeled drive actually made by
    # Seagate, and filtering "Seagate Technology" matches the SAME card.
    _dual_card(db_session, "st300mp0016", brand="IBM", manufacturer="Seagate Technology")
    _dual_card(db_session, "ssd001", manufacturer="Seagate Technology")  # maker-only
    _dual_card(db_session, "fru001", brand="IBM")  # label-only
    _dual_card(db_session, "other1", manufacturer="Kingston Technology")
    db_session.flush()

    _, total_ibm = search_materials_faceted(db_session, manufacturers=["IBM"])
    assert total_ibm == 2  # the dual card + the label-only card

    _, total_seagate = search_materials_faceted(db_session, manufacturers=["Seagate Technology"])
    assert total_seagate == 2  # the dual card + the maker-only card

    # OR-within-facet: selecting both values is a union, the dual card counted once.
    results, total_both = search_materials_faceted(db_session, manufacturers=["IBM", "Seagate Technology"])
    assert total_both == 3
    assert {r.normalized_mpn for r in results} == {"st300mp0016", "ssd001", "fru001"}


def test_manufacturer_options_count_spans_both_columns(db_session: Session):
    _dual_card(db_session, "st300mp0016", brand="IBM", manufacturer="Seagate Technology")
    _dual_card(db_session, "fru001", brand="IBM")
    _dual_card(db_session, "ssd001", manufacturer="Seagate Technology")
    db_session.flush()

    options = {o["name"]: o["count"] for o in get_manufacturer_options(db_session)}
    assert options["IBM"] == 2  # brand column on two cards
    assert options["Seagate Technology"] == 2  # manufacturer column on two cards


def test_manufacturer_options_dedupe_card_when_brand_equals_manufacturer(db_session: Session):
    # A card carrying the same name in BOTH columns counts ONCE (COUNT(DISTINCT id)).
    _dual_card(db_session, "wd001", brand="Western Digital", manufacturer="Western Digital")
    db_session.flush()

    options = get_manufacturer_options(db_session)
    assert options == [{"name": "Western Digital", "count": 1}]


def test_manufacturer_options_commodity_scopes_both_union_branches(db_session: Session):
    _dual_card(db_session, "hdd1", brand="IBM", manufacturer="Seagate Technology", category="hdd")
    _dual_card(db_session, "dram1", brand="Lenovo", manufacturer="Samsung", category="dram")
    db_session.flush()

    options = {o["name"] for o in get_manufacturer_options(db_session, commodity="hdd")}
    assert options == {"IBM", "Seagate Technology"}  # brand AND manufacturer branches scoped
    assert "Lenovo" not in options
    assert "Samsung" not in options


def test_manufacturer_options_exclude_deleted_in_both_branches(db_session: Session):
    from datetime import datetime, timezone

    card = _dual_card(db_session, "gone1", brand="IBM", manufacturer="Seagate Technology")
    card.deleted_at = datetime.now(timezone.utc)
    db_session.flush()

    assert get_manufacturer_options(db_session) == []


def test_brand_filter_scopes_with_commodity(db_session: Session):
    # AND-across-facets: the brand OR-match still respects the commodity filter.
    _dual_card(db_session, "hdd1", brand="IBM", category="hdd")
    _dual_card(db_session, "dram1", brand="IBM", category="dram")
    db_session.flush()

    _, total = search_materials_faceted(db_session, commodity="hdd", manufacturers=["IBM"])
    assert total == 1


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


@pytest.mark.parametrize(
    ("facet", "seed", "filter_values", "expected"),
    [
        pytest.param(
            "lifecycle",
            [("lc-active", "active"), ("lc-eol", "eol"), ("lc-obs", "obsolete")],
            ["active", "eol"],
            {"lc-active", "lc-eol"},
            id="lifecycle",
        ),
        pytest.param(
            "rohs",
            [("rohs-ok", "compliant"), ("rohs-no", "non-compliant"), ("rohs-ex", "exempt")],
            ["compliant", "exempt"],
            {"rohs-ok", "rohs-ex"},
            id="rohs",
        ),
    ],
)
def test_search_faceted_single_value_global_facet(db_session: Session, facet, seed, filter_values, expected):
    for mpn, value in seed:
        _mk_global(db_session, mpn, **{facet: value})

    results, total = search_materials_faceted(db_session, **{facet: filter_values})
    assert {c.normalized_mpn for c in results} == expected
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
        category="dram",
        created_at=datetime.now(timezone.utc),
    )
    deleted = MaterialCard(
        normalized_mpn="cov-003",
        display_mpn="COV-003",
        category="dram",
        deleted_at=datetime.now(timezone.utc),
        created_at=datetime.now(timezone.utc),
    )
    other_cat = MaterialCard(
        normalized_mpn="cov-004",
        display_mpn="COV-004",
        category="capacitors",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add_all([no_specs, deleted, other_cat])
    db_session.flush()

    coverage = get_commodity_spec_coverage(db_session, "dram")
    assert coverage == SpecCoverage(with_specs=1, total=2)  # deleted + other-category excluded

    # Case-insensitive commodity key, and an unknown commodity yields zeros.
    assert get_commodity_spec_coverage(db_session, "  DRAM ") == SpecCoverage(with_specs=1, total=2)
    assert get_commodity_spec_coverage(db_session, "nonexistent_xyz") == SpecCoverage(with_specs=0, total=0)


# --- "Has alternates" (has_crosses): fru_links crosswalk OR manual cross_references ---


def _cross_card(db, mpn, **kw):
    card = MaterialCard(
        normalized_mpn=mpn,
        display_mpn=mpn.upper(),
        category=kw.pop("category", "hdd"),
        created_at=datetime.now(timezone.utc),
        **kw,
    )
    db.add(card)
    db.flush()
    return card


def _fru_edge(db, fru_norm, related_norm, kind=None):
    from app.constants import FruLinkKind
    from app.models import FruLink

    link = FruLink(
        fru_raw=fru_norm.upper(),
        fru_norm=fru_norm,
        related_raw=related_norm.upper(),
        related_norm=related_norm,
        rel_kind=(kind or FruLinkKind.MFG_MODEL).value,
        source_sheet="Main",
    )
    db.add(link)
    db.flush()
    return link


def test_has_crosses_matches_fru_links_both_directions(db_session: Session):
    """A card matches when its normalized_mpn sits on EITHER side of a fru_links
    edge."""
    _cross_card(db_session, "00aj001")  # FRU side (fru_norm)
    _cross_card(db_session, "st9300603ss")  # related side (related_norm)
    _cross_card(db_session, "lonely001")  # no edges, no cross_references
    _fru_edge(db_session, "00aj001", "st9300603ss")
    db_session.flush()

    results, total = search_materials_faceted(db_session, has_crosses=True)
    assert total == 2
    assert {r.normalized_mpn for r in results} == {"00aj001", "st9300603ss"}


def test_has_crosses_keeps_manual_cross_references_branch(db_session: Session):
    """A card with manual cross_references but NO fru_links edge still matches (OR
    branch)."""
    _cross_card(db_session, "manualx01", cross_references=[{"mpn": "ALT-1"}])
    _cross_card(db_session, "emptycross", cross_references=[])  # empty list is NOT an alternate
    _cross_card(db_session, "nullcross", cross_references=None)
    db_session.flush()

    results, total = search_materials_faceted(db_session, has_crosses=True)
    assert total == 1
    assert results[0].normalized_mpn == "manualx01"


def test_has_crosses_excludes_unrelated_and_is_noop_when_false(db_session: Session):
    """No fru_links edge + no cross_references -> excluded; has_crosses=False filters
    nothing."""
    _cross_card(db_session, "plain001")
    _cross_card(db_session, "plain002")
    db_session.flush()

    _, total_filtered = search_materials_faceted(db_session, has_crosses=True)
    assert total_filtered == 0

    _, total_all = search_materials_faceted(db_session, has_crosses=False)
    assert total_all == 2


def test_has_crosses_count_consistent_and_dedups_overlap(db_session: Session):
    """Total == len(results): a card matched by BOTH fru_links AND cross_references
    counts once."""
    _cross_card(db_session, "bothways1", cross_references=[{"mpn": "ALT-9"}])
    _fru_edge(db_session, "bothways1", "wd4000fyyz")
    # The edge's related side has no card row -- the EXISTS must not invent results.
    _cross_card(db_session, "plain003")
    db_session.flush()

    results, total = search_materials_faceted(db_session, has_crosses=True)
    assert total == len(results) == 1
    assert results[0].normalized_mpn == "bothways1"


def test_has_crosses_predicate_shared_by_list_and_count(db_session: Session):
    """The list rows and the total come from the same predicate under combined
    filters."""
    fru_card = _cross_card(db_session, "00fruhdd1", category="hdd")
    _cross_card(db_session, "00fruram1", category="dram")  # crossed, wrong commodity
    _cross_card(db_session, "nocross01", category="hdd")  # right commodity, no alternates
    _fru_edge(db_session, "00fruhdd1", "st4000nm0023")
    _fru_edge(db_session, "00fruram1", "m393a2g40db1")
    db_session.flush()

    results, total = search_materials_faceted(db_session, commodity="hdd", has_crosses=True)
    assert total == len(results) == 1
    assert results[0].id == fru_card.id
