"""Badge rendering for oem_sourced + not_catalogued in the materials list partial."""

from jinja2 import Environment, FileSystemLoader, select_autoescape


def _render(status, provenance=None):
    env = Environment(
        loader=FileSystemLoader("app/templates"),
        autoescape=select_autoescape(["html"]),
    )
    tmpl = env.get_template("htmx/partials/materials/list.html")
    card = type(
        "C",
        (),
        {
            "enrichment_status": status,
            "enrichment_provenance": provenance or {},
            "lifecycle_status": None,
            "display_mpn": "01HW917",
            "manufacturer": "Lenovo",
            "category": "Memory",
            "description": "x",
            "_vendor_count": 0,
            "_best_price": None,
            "_best_currency": "USD",
            "id": 1,
            "normalized_mpn": "01hw917",
            "_primary_specs": [],
            "last_searched_at": None,
        },
    )()
    return tmpl.module if False else tmpl.render(materials=[card], lc_colors={}, total=1, limit=50, offset=0)


def test_oem_sourced_badge_renders():
    html = _render(
        "oem_sourced", {"source_urls": ["https://support.lenovo.com/x"], "source_domains": ["support.lenovo.com"]}
    )
    assert "OEM-SOURCED" in html


def test_not_catalogued_badge_renders():
    html = _render("not_catalogued")
    assert "NOT CATALOGUED" in html
