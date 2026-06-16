"""Condensed materials results table (list.html): 7-column layout, merged Status cell,
category folded under Manufacturer, smart price decimals, match-framed count.

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
            "_primary_specs": [],
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


def test_seven_column_header_no_category_or_lifecycle_columns():
    html = _render()
    # Exactly 7 column headers (was 9): Category + Lifecycle columns are gone.
    assert html.count("<th ") == 7  # trailing space: doesn't match <thead>
    assert ">Category</th>" not in html
    assert ">Lifecycle</th>" not in html
    # The survivors, including the merged Status column and the renamed Last Seen.
    for header in (
        ">MPN</th>",
        ">Description</th>",
        ">Manufacturer</th>",
        ">Status</th>",
        ">Vendors</th>",
        ">Best Price</th>",
        ">Last Seen</th>",
    ):
        assert header in html, f"missing header {header}"


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
    # Still only 7 columns — proves lifecycle merged into status rather than adding a col.
    assert html.count("<th ") == 7  # trailing space: doesn't match <thead>


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
