"""Dead-control + density fixes from the UX audit (render-based guards).

Covers four independent template fixes:

1. materials/list.html — the in-row datasheet anchor and the WEB-SOURCED / OEM-SOURCED
   badge links carry ``@click.stop`` so opening a source in a new tab does NOT bubble to
   the row's htmx click handler (which would also swap #main-content to the part detail).
2. requisitions/detail.html — the tab bar renders reachable Tasks + Activity buttons that
   point at the existing ``/tab/tasks`` and ``/tab/activity`` endpoints.
3. materials/workspace.html — the "Materials" title + commodity breadcrumb are folded into
   the search-bar row (single bordered row); the old standalone title row is gone.
4. requisitions/list.html — exactly one reset control ("Clean & reset"); the redundant
   "Clear filters" link is removed.

Called by: pytest
Depends on: the four templates above, app.template_env.templates, the faceted +
requisitions list HTMX routes (via the ``client`` fixture), Jinja2.
"""

import os

os.environ["TESTING"] = "1"

import re
from types import SimpleNamespace

from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader, select_autoescape

from app.template_env import templates

# ── Fix 1: materials/list.html — new-tab anchors carry @click.stop ──────────────────


def _material_card(**overrides):
    base = dict(
        id=1,
        display_mpn="01HW917",
        normalized_mpn="01hw917",
        datasheet_url=None,
        cross_references=None,
        _primary_specs=[],
        description="a memory module",
        brand="Lenovo",
        manufacturer="Lenovo",
        _show_maker_suffix=False,
        category="Memory",
        enrichment_status="verified",
        enrichment_provenance={},
        lifecycle_status=None,
        condition=None,
        _vendor_count=0,
        _best_price=None,
        _best_currency="USD",
        last_searched_at=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _render_list(cards):
    tmpl = templates.env.get_template("htmx/partials/materials/list.html")
    return tmpl.render(materials=cards, q="", commodity="", commodity_display="", total=len(cards), limit=50, offset=0)


def _anchor_containing(html, needle):
    """Return the first ``<a ...>`` opening tag whose text contains ``needle``."""
    for tag in re.findall(r"<a\b[^>]*>", html, re.DOTALL):
        if needle in tag:
            return tag
    return None


def test_datasheet_anchor_carries_click_stop():
    card = _material_card(datasheet_url="https://example.com/ds.pdf")
    tag = _anchor_containing(_render_list([card]), "Open datasheet")
    assert tag is not None, "datasheet anchor not rendered"
    assert "@click.stop" in tag, "in-row datasheet anchor must carry @click.stop"


def test_web_sourced_badge_link_carries_click_stop():
    card = _material_card(
        enrichment_status="web_sourced",
        enrichment_provenance={"source_urls": ["https://ex.com/p"], "source_domains": ["ex.com"]},
    )
    html = _render_list([card])
    assert "WEB-SOURCED" in html
    tag = _anchor_containing(html, "bg-sky-50")  # the WEB-SOURCED badge anchor
    assert tag is not None, "WEB-SOURCED anchor not rendered"
    assert "@click.stop" in tag, "WEB-SOURCED badge link must carry @click.stop"


def test_oem_sourced_badge_link_carries_click_stop():
    card = _material_card(
        enrichment_status="oem_sourced",
        enrichment_provenance={"source_urls": ["https://oem.com/p"], "source_domains": ["oem.com"]},
    )
    html = _render_list([card])
    assert "OEM-SOURCED" in html
    tag = _anchor_containing(html, "bg-indigo-50")  # the OEM-SOURCED badge anchor
    assert tag is not None, "OEM-SOURCED anchor not rendered"
    assert "@click.stop" in tag, "OEM-SOURCED badge link must carry @click.stop"


# ── Fix 2: requisitions/detail.html — Tasks + Activity tabs are reachable ───────────


def _render_req_detail(req_id=42):
    """Render detail.html with its two {% include %}s stubbed out so the tab-bar loop
    can be asserted in isolation (the includes need heavy DB context we don't exercise
    here)."""
    stubs = DictLoader(
        {
            "htmx/partials/requisitions/detail_header.html": "",
            "htmx/partials/requisitions/tabs/parts.html": "",
        }
    )
    env = Environment(
        loader=ChoiceLoader([stubs, FileSystemLoader("app/templates")]),
        autoescape=select_autoescape(["html"]),
    )
    tmpl = env.get_template("htmx/partials/requisitions/detail.html")
    req = SimpleNamespace(id=req_id, name="Test Req")
    return tmpl.render(req=req, initial_tab=None)


def test_detail_renders_tasks_tab_button_pointing_at_tab_endpoint():
    html = _render_req_detail(42)
    assert "Tasks" in html, "Tasks tab button label missing"
    assert "/v2/partials/requisitions/42/tab/tasks" in html, "Tasks tab must hx-get the /tab/tasks endpoint"


def test_detail_renders_activity_tab_button_pointing_at_tab_endpoint():
    html = _render_req_detail(42)
    assert "Activity" in html, "Activity tab button label missing"
    assert "/v2/partials/requisitions/42/tab/activity" in html, "Activity tab must hx-get the /tab/activity endpoint"


# ── Fix 3: materials/workspace.html — title folded into the search-bar row ──────────


def test_workspace_title_folded_into_search_row(client):
    resp = client.get("/v2/partials/materials/workspace")
    assert resp.status_code == 200
    html = resp.text

    # Title is preserved.
    assert "Materials</h1>" in html, "Materials title must still render"
    # The old standalone title row (its distinctive padding signature) is gone.
    assert "px-3 pt-3 pb-1" not in html, "standalone title row must be removed"

    # Title and search input now live in the SAME bordered row: the title appears before
    # the search input, with no border-b row boundary separating them.
    idx_title = html.find("Materials</h1>")
    idx_search = html.find("Search by MPN")
    assert idx_title != -1 and idx_search != -1, "title/search input not found"
    assert idx_title < idx_search, "title must precede the search input in the folded row"
    between = html[idx_title:idx_search]
    assert "border-b border-line-subtle" not in between, (
        "no bordered-row boundary may sit between the title and the search input — they are folded into one row"
    )


# ── Fix 4: requisitions/list.html — a single reset control ──────────────────────────


def test_requisitions_list_has_single_reset_control(client):
    # A filter is active so the OLD template would have rendered the redundant "Clear
    # filters" link — proving its removal, not merely a hidden guard.
    resp = client.get("/v2/partials/requisitions?q=widget")
    assert resp.status_code == 200
    html = resp.text
    assert "Clean &amp; reset" in html, "the single 'Clean & reset' control must remain"
    assert "Clear filters" not in html, "the redundant 'Clear filters' link must be removed"
