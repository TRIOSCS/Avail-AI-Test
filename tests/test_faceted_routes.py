"""tests/test_faceted_routes.py -- Tests for faceted search HTMX routes.

Covers: Faceted search routes in app/routers/htmx_views.py
Depends on: conftest.py, faceted search models, commodity_registry
"""

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import CommoditySpecSchema, MaterialCard, MaterialSpecFacet
from tests.conftest import engine  # noqa: F401


def test_materials_workspace_renders(client):
    resp = client.get("/v2/partials/materials/workspace")
    assert resp.status_code == 200
    assert "materialsFilter" in resp.text


def test_commodity_tree_renders(client):
    resp = client.get("/v2/partials/materials/filters/tree")
    assert resp.status_code == 200
    assert "Passives" in resp.text


def test_subfilters_renders_for_commodity(client, db_session: Session):
    card = MaterialCard(
        normalized_mpn="ddr4-001",
        display_mpn="DDR4-001",
        manufacturer="MemCo",
        category="dram",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(card)
    db_session.flush()
    db_session.add(
        CommoditySpecSchema(
            commodity="dram",
            spec_key="ddr_type",
            display_name="DDR Type",
            data_type="enum",
            enum_values=["DDR4", "DDR5"],
            sort_order=1,
            is_filterable=True,
            is_primary=False,
        )
    )
    db_session.add(
        MaterialSpecFacet(
            material_card_id=card.id,
            category="dram",
            spec_key="ddr_type",
            value_text="DDR4",
        )
    )
    db_session.commit()
    resp = client.get("/v2/partials/materials/filters/sub?commodity=dram")
    assert resp.status_code == 200
    assert "DDR Type" in resp.text


def test_subfilters_placeholder_for_no_commodity(client):
    # No commodity → server-rendered nudge toward the category tree (replaces the
    # old empty-string response), with no spec-filter sections or coverage line.
    resp = client.get("/v2/partials/materials/filters/sub")
    assert resp.status_code == 200
    assert "Select a category to unlock spec filters" in resp.text
    assert 'class="text-[11px] text-gray-400 italic px-2 mt-2"' in resp.text
    assert "clearSubFilters()" not in resp.text
    assert "have filterable specs" not in resp.text
    assert "No filters available for this category" not in resp.text


def test_subfilters_no_placeholder_with_commodity(client):
    # With a commodity scope the placeholder nudge must NOT render.
    resp = client.get("/v2/partials/materials/filters/sub?commodity=dram")
    assert resp.status_code == 200
    assert "Select a category to unlock spec filters" not in resp.text


def test_faceted_results_returns_materials(client, db_session: Session):
    card = MaterialCard(
        normalized_mpn="test-001",
        display_mpn="TEST-001",
        manufacturer="TestCo",
        category="DRAM",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(card)
    db_session.commit()
    resp = client.get("/v2/partials/materials/faceted?commodity=dram")
    assert resp.status_code == 200
    assert "TEST-001" in resp.text


def test_faceted_results_no_search_bar(client, db_session: Session):
    """When called in faceted mode, the search bar and pill filters are hidden."""
    resp = client.get("/v2/partials/materials/faceted")
    assert resp.status_code == 200
    # The search bar input with hx-get="/v2/partials/materials" should not appear
    assert 'hx-get="/v2/partials/materials"' not in resp.text


def test_faceted_results_bad_sub_filters_json(client):
    """Malformed sub_filters JSON should not crash — treated as empty."""
    resp = client.get("/v2/partials/materials/faceted?sub_filters=NOT_JSON")
    assert resp.status_code == 200


def test_faceted_results_sub_filters_actually_filter(client, db_session: Session):
    """sub_filters should filter results — only matching cards returned."""
    import json

    # Create schema
    db_session.add(
        CommoditySpecSchema(
            commodity="dram",
            spec_key="ddr_type",
            display_name="DDR Type",
            data_type="enum",
            enum_values=["DDR4", "DDR5"],
            sort_order=1,
            is_filterable=True,
            is_primary=False,
        )
    )
    db_session.flush()

    # Create two cards with different spec values
    card_ddr4 = MaterialCard(
        normalized_mpn="ddr4-chip",
        display_mpn="DDR4-CHIP",
        manufacturer="MemCo",
        category="dram",
        created_at=datetime.now(timezone.utc),
    )
    card_ddr5 = MaterialCard(
        normalized_mpn="ddr5-chip",
        display_mpn="DDR5-CHIP",
        manufacturer="MemCo",
        category="dram",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add_all([card_ddr4, card_ddr5])
    db_session.flush()

    db_session.add(
        MaterialSpecFacet(
            material_card_id=card_ddr4.id,
            category="dram",
            spec_key="ddr_type",
            value_text="DDR4",
        )
    )
    db_session.add(
        MaterialSpecFacet(
            material_card_id=card_ddr5.id,
            category="dram",
            spec_key="ddr_type",
            value_text="DDR5",
        )
    )
    db_session.commit()

    # Filter for DDR5 only
    filters_json = json.dumps({"ddr_type": ["DDR5"]})
    resp = client.get(f"/v2/partials/materials/faceted?commodity=dram&sub_filters={filters_json}")
    assert resp.status_code == 200
    assert "DDR5-CHIP" in resp.text
    assert "DDR4-CHIP" not in resp.text


def test_manufacturer_filter_partial_renders(client, db_session: Session):
    """Manufacturer names must appear in HTML (x-data JSON); avoid broken
    x-data=\"{name: \"...\"}\"."""
    db_session.add(
        MaterialCard(
            normalized_mpn="mfg-test-1",
            display_mpn="MFG-TEST-1",
            manufacturer="MemCo",
            category="dram",
            created_at=datetime.now(timezone.utc),
        )
    )
    db_session.commit()
    resp = client.get("/v2/partials/materials/filters/manufacturers")
    assert resp.status_code == 200
    assert "MemCo" in resp.text
    assert "mfrLabel" in resp.text
    assert 'x-data="{name:' not in resp.text


def test_filters_sub_ignores_manufacturers_key(client, db_session):
    """Selecting a manufacturer must not zero out the enum facet count badges.

    The bug: without `parsed_filters.pop("manufacturers", None)` in the route,
    the "manufacturers" key is passed into get_facet_counts as a spec_key, which
    finds no MaterialSpecFacet rows and returns an empty counts dict — so the
    count badge (tabular-nums span) is never rendered for any enum value.

    This test asserts that the count badge `<span ... tabular-nums">1<` for
    LQFP-48/package appears in BOTH requests:
      1. sub_filters={}         (baseline — counts must render)
      2. sub_filters={"manufacturers":["ST"]}  (bug trigger — must still render)
    """
    import json

    from app.models.faceted_search import MaterialSpecFacet
    from app.models.intelligence import MaterialCard
    from app.services.commodity_registry import seed_commodity_schemas

    seed_commodity_schemas(db_session)
    card = MaterialCard(
        normalized_mpn="mc1", display_mpn="MC1", manufacturer="ST", category="microcontrollers", description="mcu"
    )
    db_session.add(card)
    db_session.flush()
    db_session.add(
        MaterialSpecFacet(
            material_card_id=card.id, category="microcontrollers", spec_key="package", value_text="LQFP-48"
        )
    )
    db_session.commit()

    # The count badge rendered by the checkbox_group macro when counts[val] exists:
    #   <span class="text-[11px] text-gray-400 tabular-nums">1</span>
    # Both "LQFP-48" and the badge must be present; LQFP-48 alone is insufficient
    # because it renders unconditionally from get_subfilter_options regardless of counts.
    COUNT_BADGE = 'tabular-nums">1<'

    # 1. Baseline: empty sub_filters — counts must render
    resp_empty = client.get("/v2/partials/materials/filters/sub?commodity=microcontrollers&sub_filters=%7B%7D")
    assert resp_empty.status_code == 200
    assert "LQFP-48" in resp_empty.text, "LQFP-48 not rendered in baseline"
    assert COUNT_BADGE in resp_empty.text, "Count badge not rendered in baseline (sub_filters={})"

    # 2. Bug trigger: manufacturers key must be ignored so counts are not zeroed
    sub_filters_with_mfr = json.dumps({"manufacturers": ["ST"]})
    resp_mfr = client.get(
        f"/v2/partials/materials/filters/sub?commodity=microcontrollers&sub_filters={sub_filters_with_mfr}"
    )
    assert resp_mfr.status_code == 200
    assert "LQFP-48" in resp_mfr.text, "LQFP-48 not rendered when manufacturers filter active"
    assert COUNT_BADGE in resp_mfr.text, (
        "Count badge zeroed when manufacturers key present in sub_filters — "
        "parsed_filters.pop('manufacturers', None) must be applied before get_facet_counts"
    )


def test_global_filters_partial_renders(client, db_session: Session):
    """The global-facets partial renders lifecycle / RoHS / datasheet with counts."""
    db_session.add(
        MaterialCard(
            normalized_mpn="gf-1",
            display_mpn="GF-1",
            category="resistors",
            lifecycle_status="active",
            rohs_status="compliant",
            datasheet_url="https://example.com/ds.pdf",
            created_at=datetime.now(timezone.utc),
        )
    )
    db_session.commit()
    resp = client.get("/v2/partials/materials/filters/global")
    assert resp.status_code == 200
    assert "Lifecycle" in resp.text
    assert "RoHS" in resp.text
    assert "Has datasheet" in resp.text
    # Bound to Alpine state arrays, not the legacy toggles.
    assert "toggleGlobalFacet('lifecycle'" in resp.text
    assert "toggleDatasheet()" in resp.text


def test_faceted_lifecycle_param_filters(client, db_session: Session):
    """The faceted route parses ?lifecycle= and returns only matching cards."""
    db_session.add_all(
        [
            MaterialCard(
                normalized_mpn="lc-active",
                display_mpn="LC-ACTIVE",
                category="resistors",
                lifecycle_status="active",
                created_at=datetime.now(timezone.utc),
            ),
            MaterialCard(
                normalized_mpn="lc-eol",
                display_mpn="LC-EOL",
                category="resistors",
                lifecycle_status="eol",
                created_at=datetime.now(timezone.utc),
            ),
        ]
    )
    db_session.commit()
    resp = client.get("/v2/partials/materials/faceted?lifecycle=active")
    assert resp.status_code == 200
    assert "LC-ACTIVE" in resp.text
    assert "LC-EOL" not in resp.text


def test_faceted_rohs_and_datasheet_params_filter(client, db_session: Session):
    """?rohs= and ?has_datasheet=true narrow the result set correctly."""
    db_session.add_all(
        [
            MaterialCard(
                normalized_mpn="r-ok-ds",
                display_mpn="R-OK-DS",
                category="resistors",
                rohs_status="compliant",
                datasheet_url="https://example.com/a.pdf",
                created_at=datetime.now(timezone.utc),
            ),
            MaterialCard(
                normalized_mpn="r-ok-no-ds",
                display_mpn="R-OK-NO-DS",
                category="resistors",
                rohs_status="compliant",
                datasheet_url=None,
                created_at=datetime.now(timezone.utc),
            ),
            MaterialCard(
                normalized_mpn="r-bad-ds",
                display_mpn="R-BAD-DS",
                category="resistors",
                rohs_status="non-compliant",
                datasheet_url="https://example.com/b.pdf",
                created_at=datetime.now(timezone.utc),
            ),
        ]
    )
    db_session.commit()
    resp = client.get("/v2/partials/materials/faceted?rohs=compliant&has_datasheet=true")
    assert resp.status_code == 200
    assert "R-OK-DS" in resp.text  # compliant + has datasheet
    assert "R-OK-NO-DS" not in resp.text  # compliant but no datasheet
    assert "R-BAD-DS" not in resp.text  # has datasheet but non-compliant


def test_faceted_statuses_param_still_filters(client, db_session: Session):
    """The trust-ladder ?statuses= CSV restricts to listed enrichment tiers."""
    db_session.add_all(
        [
            MaterialCard(
                normalized_mpn="st-verified",
                display_mpn="ST-VERIFIED",
                category="resistors",
                enrichment_status="verified",
                created_at=datetime.now(timezone.utc),
            ),
            MaterialCard(
                normalized_mpn="st-ai",
                display_mpn="ST-AI",
                category="resistors",
                enrichment_status="ai_inferred",
                created_at=datetime.now(timezone.utc),
            ),
        ]
    )
    db_session.commit()
    resp = client.get("/v2/partials/materials/faceted?statuses=verified")
    assert resp.status_code == 200
    assert "ST-VERIFIED" in resp.text
    assert "ST-AI" not in resp.text


def test_workspace_renders_confidence_groups(client):
    """The Data-confidence section renders as 3 groups via the confidence-group
    handler."""
    resp = client.get("/v2/partials/materials/workspace")
    assert resp.status_code == 200
    assert "Data confidence" in resp.text
    assert "toggleConfidenceGroup(" in resp.text
    assert "CONFIDENCE_GROUPS" in resp.text


def test_list_renders_zero_price_and_currency():
    """A $0 best price renders (not '--'); a non-USD currency shows its ISO code."""
    from types import SimpleNamespace

    from app.template_env import templates

    tmpl = templates.env.get_template("htmx/partials/materials/list.html")

    def _card(price, currency):
        return SimpleNamespace(
            id=1,
            display_mpn="X",
            normalized_mpn="x",
            description=None,
            manufacturer=None,
            category="other",
            lifecycle_status=None,
            last_searched_at=None,
            specs_structured=None,
            _primary_specs=[],
            _vendor_count=1,
            _best_price=price,
            _best_currency=currency,
        )

    html0 = tmpl.render(materials=[_card(0, "USD")], q="", total=1, limit=50, offset=0, faceted=True)
    assert "$0.0000" in html0  # zero price is shown, not '--'

    html_eur = tmpl.render(materials=[_card(1.5, "EUR")], q="", total=1, limit=50, offset=0, faceted=True)
    assert "EUR 1.5000" in html_eur  # non-USD currency labelled with its code


def test_workspace_injects_display_names(client):
    resp = client.get("/v2/partials/materials/workspace")
    assert resp.status_code == 200
    assert "data-display-names=" in resp.text
    assert "Analog ICs" in resp.text  # canonical name from _DISPLAY_NAMES


def test_faceted_endpoint_currency_aggregate(client, db_session):
    """Route-level Task 8 currency regression test.

    Exercises the real materials_faceted_partial aggregate SQL:
      count(distinct last_currency), max(last_currency), _best_currency derivation.

    Single-currency card → currency label rendered (e.g. "EUR 1.2500").
    Mixed-currency card  → _best_currency is None → falls back to "$" prefix.
    """
    from app.models.intelligence import MaterialCard, MaterialVendorHistory

    # --- Card 1: two rows, BOTH EUR → single-currency → should render "EUR <price>" ---
    card_eur = MaterialCard(
        normalized_mpn="eur-test-001",
        display_mpn="EUR-TEST-001",
        manufacturer="EuroCo",
        category="passive",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(card_eur)
    db_session.flush()

    db_session.add(
        MaterialVendorHistory(
            material_card_id=card_eur.id,
            vendor_name="Vendor A",
            last_price=5.0000,
            last_currency="EUR",
        )
    )
    db_session.add(
        MaterialVendorHistory(
            material_card_id=card_eur.id,
            vendor_name="Vendor B",
            last_price=1.2500,  # min price → best price shown
            last_currency="EUR",
        )
    )

    # --- Card 2: one USD row + one EUR row → mixed → _best_currency None → "$" prefix ---
    card_mixed = MaterialCard(
        normalized_mpn="mixed-test-001",
        display_mpn="MIXED-TEST-001",
        manufacturer="MixedCo",
        category="passive",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(card_mixed)
    db_session.flush()

    db_session.add(
        MaterialVendorHistory(
            material_card_id=card_mixed.id,
            vendor_name="Vendor C",
            last_price=3.0000,
            last_currency="USD",
        )
    )
    db_session.add(
        MaterialVendorHistory(
            material_card_id=card_mixed.id,
            vendor_name="Vendor D",
            last_price=2.5000,  # min price → shown, but mixed currencies → "$"
            last_currency="EUR",
        )
    )

    db_session.commit()

    resp = client.get("/v2/partials/materials/faceted")
    assert resp.status_code == 200

    # Single-currency EUR card: best price is 1.2500, labelled "EUR <price>"
    assert "EUR 1.2500" in resp.text, (
        "EUR-labelled best price not rendered for single-currency card; "
        "_best_currency derivation (count distinct == 1 → max currency) may be broken"
    )

    # Mixed-currency card: best price is 2.5000 (min), prefix must be "$" not "EUR"
    assert "$2.5000" in resp.text, (
        "Dollar-prefixed price not rendered for mixed-currency card; "
        "_best_currency should be None for mixed currencies (falls back to '$')"
    )


# --- Operational (Layer-3) filter params on the faceted route ---


def _op_card(db, mpn, **kw):
    from app.models.intelligence import MaterialCard

    card = MaterialCard(
        normalized_mpn=mpn.lower(),
        display_mpn=mpn.upper(),
        category=kw.pop("category", "resistors"),
        created_at=datetime.now(timezone.utc),
        **kw,
    )
    db.add(card)
    db.flush()
    return card


def test_faceted_operational_params_filter(client, db_session: Session):
    """?has_crosses / ?internal / ?min_searches narrow the result set."""
    _op_card(
        db_session,
        "op-keep",
        cross_references=[{"mpn": "ALT-1"}],
        is_internal_part=False,
        search_count=5,
    )
    _op_card(db_session, "op-no-cross", cross_references=[], is_internal_part=False, search_count=5)
    _op_card(
        db_session,
        "op-internal",
        cross_references=[{"mpn": "ALT-2"}],
        is_internal_part=True,
        search_count=5,
    )
    _op_card(db_session, "op-cold", cross_references=[{"mpn": "ALT-3"}], is_internal_part=False, search_count=1)
    db_session.commit()

    resp = client.get("/v2/partials/materials/faceted?has_crosses=true&internal=standard&min_searches=5")
    assert resp.status_code == 200
    assert "OP-KEEP" in resp.text
    assert "OP-NO-CROSS" not in resp.text
    assert "OP-INTERNAL" not in resp.text
    assert "OP-COLD" not in resp.text


def test_faceted_has_stock_and_price_params(client, db_session: Session):
    from app.models.intelligence import MaterialVendorHistory

    with_price = _op_card(db_session, "vh-price")
    db_session.add(MaterialVendorHistory(material_card_id=with_price.id, vendor_name="V", last_price=2.5))
    no_price = _op_card(db_session, "vh-stock")
    db_session.add(MaterialVendorHistory(material_card_id=no_price.id, vendor_name="V", last_price=None))
    _op_card(db_session, "vh-none")
    db_session.commit()

    resp = client.get("/v2/partials/materials/faceted?has_stock=true")
    assert "VH-PRICE" in resp.text and "VH-STOCK" in resp.text and "VH-NONE" not in resp.text

    resp = client.get("/v2/partials/materials/faceted?has_price=true")
    assert "VH-PRICE" in resp.text and "VH-STOCK" not in resp.text and "VH-NONE" not in resp.text


def test_faceted_searched_within_param(client, db_session: Session):
    from datetime import timedelta

    now = datetime.now(timezone.utc)
    _op_card(db_session, "sw-fresh", last_searched_at=now - timedelta(days=2))
    _op_card(db_session, "sw-stale", last_searched_at=now - timedelta(days=60))
    db_session.commit()

    resp = client.get("/v2/partials/materials/faceted?searched_within=7d")
    assert "SW-FRESH" in resp.text and "SW-STALE" not in resp.text

    resp = client.get("/v2/partials/materials/faceted?searched_within=90d")
    assert "SW-FRESH" in resp.text and "SW-STALE" in resp.text


def test_faceted_bogus_operational_params_degrade(client, db_session: Session):
    """Unknown/invalid values on hand-edited URLs degrade to no-ops, never 500/422."""
    _op_card(db_session, "deg-1", is_internal_part=True)
    _op_card(db_session, "deg-2", is_internal_part=False)
    db_session.commit()

    resp = client.get("/v2/partials/materials/faceted?internal=bogus&searched_within=yesterday")
    assert resp.status_code == 200
    assert "DEG-1" in resp.text and "DEG-2" in resp.text

    # min_searches follows the same contract: non-numeric and negative values degrade
    # to the 0 no-op (NOT a FastAPI 422, which htmx would silently refuse to swap).
    for bad in ("-1", "abc"):
        resp = client.get(f"/v2/partials/materials/faceted?min_searches={bad}")
        assert resp.status_code == 200
        assert "DEG-1" in resp.text and "DEG-2" in resp.text


def test_workspace_renders_sourcing_signals_section(client):
    """The rail's Sourcing-signals section renders with all Layer-3 controls wired."""
    resp = client.get("/v2/partials/materials/workspace")
    assert resp.status_code == 200
    assert "Sourcing signals" in resp.text
    assert "toggleSourcingFlag('hasStock')" in resp.text
    assert "toggleSourcingFlag('hasPrice')" in resp.text
    assert "toggleSourcingFlag('hasCrosses')" in resp.text
    assert "setInternal(" in resp.text
    assert "setSearchedWithin(" in resp.text
    assert "setMinSearches(" in resp.text


# --- Coverage-aware empty states ---


def _dram_with_facet(db, mpn):
    card = MaterialCard(
        normalized_mpn=mpn.lower(),
        display_mpn=mpn.upper(),
        category="dram",
        created_at=datetime.now(timezone.utc),
    )
    db.add(card)
    db.flush()
    db.add(MaterialSpecFacet(material_card_id=card.id, category="dram", spec_key="ddr_type", value_text="DDR4"))
    return card


def _seed_ddr_schema(db):
    db.add(
        CommoditySpecSchema(
            commodity="dram",
            spec_key="ddr_type",
            display_name="DDR Type",
            data_type="enum",
            enum_values=["DDR4", "DDR5"],
            sort_order=1,
            is_filterable=True,
            is_primary=False,
        )
    )
    db.flush()


def test_subfilters_panel_shows_coverage_line(client, db_session: Session):
    """'N of M parts in <commodity> have filterable specs' renders in the panel."""
    _seed_ddr_schema(db_session)
    _dram_with_facet(db_session, "cov-a")
    _op_card(db_session, "cov-b", category="dram")  # no facet rows
    db_session.commit()

    resp = client.get("/v2/partials/materials/filters/sub?commodity=dram")
    assert resp.status_code == 200
    assert "1 of 2 parts in" in resp.text
    assert "have filterable specs" in resp.text


def test_faceted_empty_state_coverage_nudge(client, db_session: Session):
    """Parametric zero-result + partial coverage → the 'not yet spec-enriched' copy."""
    import json

    _seed_ddr_schema(db_session)
    _dram_with_facet(db_session, "nudge-a")  # DDR4 — filtered out below
    _op_card(db_session, "nudge-b", category="dram")  # not spec-enriched
    db_session.commit()

    filters_json = json.dumps({"ddr_type": ["DDR5"]})
    resp = client.get(f"/v2/partials/materials/faceted?commodity=dram&sub_filters={filters_json}")
    assert resp.status_code == 200
    assert (
        "No matches &mdash; but most parts here haven&#39;t been spec-enriched yet." in resp.text
        or "No matches — but most parts here haven't been spec-enriched yet." in resp.text
    )
    assert "Try clearing parametric filters." in resp.text


def test_faceted_empty_state_generic_when_coverage_full(client, db_session: Session):
    """Full coverage (every card has facets) → the generic empty state, not the
    nudge."""
    import json

    _seed_ddr_schema(db_session)
    _dram_with_facet(db_session, "full-a")
    db_session.commit()

    filters_json = json.dumps({"ddr_type": ["DDR5"]})
    resp = client.get(f"/v2/partials/materials/faceted?commodity=dram&sub_filters={filters_json}")
    assert resp.status_code == 200
    assert "No results match your filters" in resp.text
    assert "spec-enriched" not in resp.text


def test_faceted_empty_state_generic_without_parametric_filters(client, db_session: Session):
    """Zero results WITHOUT parametric filters → generic empty state (no nudge)."""
    resp = client.get("/v2/partials/materials/faceted?commodity=dram")
    assert resp.status_code == 200
    assert "No results match your filters" in resp.text
    assert "spec-enriched" not in resp.text


def test_faceted_empty_state_generic_when_commodity_has_no_cards(client, db_session: Session):
    """Parametric filters on a commodity with ZERO cards (coverage 0/0) → generic empty
    state; the nonsensical 'Only 0 of 0 parts' nudge must never render."""
    import json

    _seed_ddr_schema(db_session)  # schema exists, but no dram cards are seeded
    db_session.commit()

    filters_json = json.dumps({"ddr_type": ["DDR5"]})
    resp = client.get(f"/v2/partials/materials/faceted?commodity=dram&sub_filters={filters_json}")
    assert resp.status_code == 200
    assert "No results match your filters" in resp.text
    assert "spec-enriched" not in resp.text


# --- Material-card (result row) upgrades ---


def test_faceted_row_datasheet_and_condition_badges(client, db_session: Session):
    _op_card(
        db_session,
        "row-rich",
        datasheet_url="https://example.com/ds.pdf",
        condition="New",
        cross_references=[{"mpn": "ALT-1"}, {"mpn": "ALT-2"}],
    )
    _op_card(db_session, "row-bare", cross_references=[])
    db_session.commit()

    resp = client.get("/v2/partials/materials/faceted")
    assert resp.status_code == 200
    # Datasheet indicator: new-tab link with noopener.
    assert 'href="https://example.com/ds.pdf" target="_blank" rel="noopener"' in resp.text
    assert "Open datasheet" in resp.text
    # Crosses badge with count.
    assert "2 alternates" in resp.text
    # Condition badge styled like the lifecycle palette.
    assert ">NEW<" in resp.text.replace("\n", "").replace("  ", "")
    # Bare card renders without the datasheet indicator leaking onto it.
    assert resp.text.count('title="Open datasheet"') == 1


def test_faceted_row_crosses_badge_singular(client, db_session: Session):
    _op_card(db_session, "row-one-alt", cross_references=[{"mpn": "ALT-1"}])
    db_session.commit()
    resp = client.get("/v2/partials/materials/faceted")
    assert "1 alternate" in resp.text
    assert "1 alternates" not in resp.text


def test_faceted_row_second_life_conditions_render_violet(client, db_session: Session):
    # Refurbished/Used share the violet second-life family; amber stays exclusively
    # caution/reconfirm (lifecycle EOL/LTB, AI guess).
    _op_card(db_session, "row-refurb", condition="Refurbished")
    _op_card(db_session, "row-used", condition="Used")
    db_session.commit()
    resp = client.get("/v2/partials/materials/faceted")
    assert resp.status_code == 200
    assert resp.text.count("bg-violet-50 text-violet-700 border-violet-200") == 2
    assert "bg-amber-50" not in resp.text


def test_faceted_row_crosses_chip_neutral_not_indigo(client, db_session: Session):
    # The alternates chip is count metadata, not a status — neutral gray; indigo is
    # reserved for the OEM-SOURCED badge.
    _op_card(db_session, "row-cross-neutral", cross_references=[{"mpn": "ALT-1"}])
    db_session.commit()
    resp = client.get("/v2/partials/materials/faceted")
    assert 'title="Known cross-references / substitutes"' in resp.text
    assert "bg-gray-100 text-gray-600 border-gray-200" in resp.text
    assert "bg-indigo-50" not in resp.text


def test_faceted_spec_chip_title_keeps_label_in_commodity_context(client, db_session: Session):
    # Commodity-scoped chips render value-only; the title keeps "Label: value" on hover.
    db_session.add(
        CommoditySpecSchema(
            commodity="dram",
            spec_key="capacity_gb",
            display_name="Capacity (GB)",
            data_type="numeric",
            sort_order=1,
            is_filterable=True,
            is_primary=True,
        )
    )
    _op_card(db_session, "chip-hover", category="dram", specs_structured={"capacity_gb": {"value": 32}})
    db_session.commit()
    resp = client.get("/v2/partials/materials/faceted?commodity=dram")
    assert resp.status_code == 200
    assert 'title="Capacity (GB): 32"' in resp.text


def test_faceted_spec_chips_without_commodity_use_schema_primaries(client, db_session: Session):
    """No-commodity rows chip their own category's is_primary specs as 'label:

    value'.
    """
    db_session.add(
        CommoditySpecSchema(
            commodity="dram",
            spec_key="capacity_gb",
            display_name="Capacity (GB)",
            data_type="numeric",
            sort_order=1,
            is_filterable=True,
            is_primary=True,
        )
    )
    _op_card(db_session, "chip-known", category="dram", specs_structured={"capacity_gb": {"value": 32}})
    db_session.commit()

    resp = client.get("/v2/partials/materials/faceted")
    assert resp.status_code == 200
    assert "Capacity (GB): 32" in resp.text


def test_faceted_spec_chips_without_commodity_fallback_first_scalars(client, db_session: Session):
    """Cards in a schema-less category fall back to the first 3 scalar spec entries."""
    _op_card(
        db_session,
        "chip-fallback",
        category="widgets",
        specs_structured={
            "speed_mhz": {"value": 3200},
            "rank": "2R",
            "nested_junk": {"value": {"a": 1}},  # non-scalar — skipped
            "voltage": 1.2,
            "fourth_key": "chip4-overflow-value",  # 4th scalar — beyond the 3-chip cap
        },
    )
    db_session.commit()

    resp = client.get("/v2/partials/materials/faceted")
    assert resp.status_code == 200
    assert "speed mhz: 3200" in resp.text
    assert "rank: 2R" in resp.text
    assert "voltage: 1.2" in resp.text
    assert "nested junk:" not in resp.text
    assert "chip4-overflow-value" not in resp.text


def test_faceted_spec_chips_in_commodity_context_unchanged(client, db_session: Session):
    """With a commodity selected, chips stay value-only (no 'label:' prefix)."""
    db_session.add(
        CommoditySpecSchema(
            commodity="dram",
            spec_key="capacity_gb",
            display_name="Capacity (GB)",
            data_type="numeric",
            sort_order=1,
            is_filterable=True,
            is_primary=True,
        )
    )
    _op_card(db_session, "chip-ctx", category="dram", specs_structured={"capacity_gb": {"value": 64}})
    # Raw-scalar primary spec (no {"value": ...} wrapper) — the pre-_spec_scalar code
    # raised AttributeError (HTTP 500) on this shape in commodity context; it must now
    # render the chip gracefully.
    _op_card(db_session, "chip-ctx-raw", category="dram", specs_structured={"capacity_gb": 32})
    db_session.commit()

    resp = client.get("/v2/partials/materials/faceted?commodity=dram")
    assert resp.status_code == 200
    compact = resp.text.replace("\n", "").replace(" ", "")
    assert ">64<" in compact
    assert ">32<" in compact
    # Value-only in the chip body (no "label:" prefix rendered)…
    assert ">Capacity(GB):64<" not in compact
    # …but the label survives on hover via the title attribute.
    assert 'title="Capacity (GB): 64"' in resp.text
