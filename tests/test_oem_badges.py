"""Badge rendering for oem_sourced + not_catalogued in the materials list partial, and
the dual-brand result-row cell ("IBM · Seagate Technology")."""

from jinja2 import Environment, FileSystemLoader, select_autoescape


def _render(status, provenance=None, *, brand=None, manufacturer="Lenovo", show_maker_suffix=None):
    env = Environment(
        loader=FileSystemLoader("app/templates"),
        autoescape=select_autoescape(["html"]),
    )
    tmpl = env.get_template("htmx/partials/materials/list.html")
    if show_maker_suffix is None:
        # Default mirrors the view annotation (htmx_views materials_faceted_partial)
        # for the no-alias case; pass show_maker_suffix=False to simulate the view's
        # normalized-compare suppressing a tautological alias pair ("HPE" / "HP").
        show_maker_suffix = bool(brand and manufacturer and brand != manufacturer)
    card = type(
        "C",
        (),
        {
            "enrichment_status": status,
            "enrichment_provenance": provenance or {},
            "lifecycle_status": None,
            "display_mpn": "01HW917",
            "brand": brand,
            "manufacturer": manufacturer,
            "category": "Memory",
            "description": "x",
            "_vendor_count": 0,
            "_best_price": None,
            "_best_currency": "USD",
            "_show_maker_suffix": show_maker_suffix,
            "id": 1,
            "normalized_mpn": "01hw917",
            "_primary_specs": [],
            "last_searched_at": None,
        },
    )()
    return tmpl.render(materials=[card], lc_colors={}, total=1, limit=50, offset=0)


def test_oem_sourced_badge_renders():
    html = _render(
        "oem_sourced", {"source_urls": ["https://support.lenovo.com/x"], "source_domains": ["support.lenovo.com"]}
    )
    assert "OEM-SOURCED" in html


def test_not_catalogued_badge_renders():
    html = _render("not_catalogued")
    assert "NOT CATALOGUED" in html


# --- Dual-brand cell: brand (OEM label) · manufacturer (actual maker) ---


def test_dual_display_renders_both_when_distinct():
    html = _render("unenriched", brand="IBM", manufacturer="Seagate Technology")
    assert "IBM · Seagate Technology" in html


def test_dual_display_single_value_when_equal():
    html = _render("unenriched", brand="Lenovo", manufacturer="Lenovo")
    assert "Lenovo" in html
    assert "Lenovo · Lenovo" not in html


def test_dual_display_suppresses_tautological_alias_pair():
    # B1 writes the NORMALIZED OEM into brand while manufacturer keeps the raw alias —
    # the view's normalized compare sets _show_maker_suffix=False for the same company
    # in two forms, so "Hewlett Packard Enterprise · HP" must never render.
    html = _render("unenriched", brand="Hewlett Packard Enterprise", manufacturer="HP", show_maker_suffix=False)
    assert "Hewlett Packard Enterprise" in html
    assert "Hewlett Packard Enterprise · HP" not in html


def test_dual_display_brand_only():
    html = _render("unenriched", brand="IBM", manufacturer=None)
    assert "IBM" in html
    assert "·" not in html.split("01HW917")[1].split("Memory")[0]  # no stray delimiter in the cell


def test_dual_display_manufacturer_only():
    html = _render("unenriched", brand=None, manufacturer="Seagate Technology")
    assert "Seagate Technology" in html


def test_dual_display_dashes_when_neither():
    html = _render("unenriched", brand=None, manufacturer=None)
    assert "--" in html
