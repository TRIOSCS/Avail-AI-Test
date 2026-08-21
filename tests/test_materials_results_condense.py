"""Condensed materials results (list.html): hybrid layout — 8-column desktop table
(labelled Specs column, merged Status cell, category folded under Manufacturer, smart
price decimals) plus the stacked mobile card list — and the match-framed count.

Renders the partial directly via Jinja (mirrors tests/test_oem_badges.py) so the layout
contract is asserted without seeding vendor-sighting stats.
"""

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape


def _render(*, materials=None, total=1, q="", commodity="", commodity_display="", **card_overrides):
    env = Environment(
        loader=FileSystemLoader("app/templates"),
        autoescape=select_autoescape(["html"]),
    )
    tmpl = env.get_template("htmx/partials/materials/list.html")
    if materials is None:
        base = {
            "id": 1,
            "display_mpn": "M393A2K43DB2-CWE",
            "normalized_mpn": "m393a2k43db2-cwe",
            "datasheet_url": None,
            "cross_references": None,
            "description": "16GB DDR4 RDIMM",
            "brand": None,
            "manufacturer": "Samsung",
            "_show_maker_suffix": False,
            "category": "DRAM",
            "lifecycle_status": None,
            "condition": None,
            "enrichment_status": "unenriched",
            "enrichment_provenance": {},
            "_vendor_count": 0,
            "_best_price": None,
            "_best_currency": "USD",
            "_card_specs": [],
            "_specs_more": 0,
            "last_searched_at": None,
        }
        base.update(card_overrides)
        materials = [type("C", (), base)()]
    return tmpl.render(
        materials=materials,
        total=total,
        q=q,
        commodity=commodity,
        commodity_display=commodity_display,
        limit=50,
        offset=0,
    )


def test_eight_column_header_no_category_or_lifecycle_columns():
    html = _render()
    # Exactly 8 column headers: Category + Lifecycle stay merged away; Specs is new;
    # "Last Seen" is renamed "Searched" (the column shows last_searched_at).
    assert html.count("<th ") == 8  # trailing space: doesn't match <thead>
    assert ">Category</th>" not in html
    assert ">Lifecycle</th>" not in html
    assert ">Last Seen</th>" not in html
    for header in (
        ">MPN</th>",
        ">Description</th>",
        ">Manufacturer</th>",
        ">Specs</th>",
        ">Status</th>",
        ">Vendors</th>",
        ">Best Price</th>",
        ">Searched</th>",
    ):
        assert header in html, f"missing header {header}"


def test_hybrid_renders_desktop_table_and_mobile_cards():
    html = _render()
    # Desktop table hidden below lg; mobile card list hidden at lg and up.
    assert 'class="overflow-x-auto hidden lg:block"' in html
    assert 'class="lg:hidden divide-y divide-gray-100"' in html
    # The MPN appears in BOTH renderings.
    assert html.count("M393A2K43DB2-CWE") >= 2


def test_category_folds_under_manufacturer():
    html = _render(manufacturer="Samsung", category="DRAM")
    assert "Samsung" in html
    # Category still visible, now as a muted sub-line (not its own column).
    assert "DRAM" in html
    assert ">Category</th>" not in html


def test_status_cell_merges_trust_lifecycle_condition():
    html = _render(enrichment_status="verified", lifecycle_status="active", condition="Refurbished")
    # All three badge families render, now grouped in the single Status cell.
    assert "VERIFIED" in html
    assert "ACTIVE" in html
    assert "REFURBISHED" in html
    # Still only 8 columns — proves lifecycle merged into status rather than adding a col.
    assert html.count("<th ") == 8  # trailing space: doesn't match <thead>


@pytest.mark.parametrize(
    ("price", "expected", "forbidden"),
    [
        pytest.param(42.5, "$42.50", "$42.5000", id="two_decimals_at_or_above_one_dollar"),
        pytest.param(0.0123, "$0.0123", None, id="four_decimals_below_one_dollar"),
    ],
)
def test_best_price_decimal_precision(price, expected, forbidden):
    html = _render(_best_price=price, _best_currency="USD")
    assert expected in html
    if forbidden is not None:
        assert forbidden not in html


def test_count_is_match_framed_with_query():
    html = _render(total=3, q="ddr5 ecc", commodity_display="DRAM")
    assert "results" in html
    assert "matching" in html
    assert "ddr5 ecc" in html
    assert "DRAM" in html
