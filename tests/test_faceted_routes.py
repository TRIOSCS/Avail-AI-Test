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


def test_subfilters_empty_for_no_commodity(client):
    resp = client.get("/v2/partials/materials/filters/sub")
    assert resp.status_code == 200
    assert resp.text == ""


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
