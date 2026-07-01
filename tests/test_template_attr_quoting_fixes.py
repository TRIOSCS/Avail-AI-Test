"""Regression tests for the Alpine `var`-handler and tojson-in-double-quoted-attr bugs.

These template bugs are silent no-ops at runtime (dead Alpine handlers / attributes that
close early), so we lock the fixes structurally: render where a quote-containing value can
break out, and source-assert the corrected leading keyword / attribute quoting.

Tests: app/templates/htmx/partials/{requisitions/tabs/offers.html, requisitions/tabs/
req_row.html, sightings/table.html, manufacturers/search_results.html}
Depends on: app/template_env.py (configured Jinja env with custom filters)
"""

from pathlib import Path
from types import SimpleNamespace

from app.template_env import templates

_TPL = Path("app/templates")


def _src(rel: str) -> str:
    return (_TPL / rel).read_text()


def test_offers_add_to_quote_handler_uses_let_not_var():
    # A `var`-leading Alpine @click compiles to a SyntaxError and no-ops (offers never POST).
    src = _src("htmx/partials/requisitions/tabs/offers.html")
    assert "let csrf = document.cookie" in src
    assert "var csrf = document.cookie" not in src


def test_sightings_select_all_handler_uses_let_not_var():
    src = _src("htmx/partials/sightings/table.html")
    assert "let ids = [" in src
    assert "var ids = [" not in src


def test_sightings_filter_xdata_is_single_quoted_tojson():
    # A user query with an apostrophe must not break the x-data attr — single-quoted + tojson.
    src = _src("htmx/partials/sightings/table.html")
    assert "x-data='{ sStatus: {{ status|tojson }}" in src


def test_req_row_data_subs_is_single_quoted():
    # tojson emits `"` — a double-quoted data-subs closes early and JSON.parse throws.
    src = _src("htmx/partials/requisitions/tabs/req_row.html")
    assert "data-subs='{{ r.substitutes | tojson" in src


def test_manufacturer_picker_name_survives_quotes_in_onclick():
    """A manufacturer name with ' and " renders a well-formed, non-truncated onclick.

    Old bug: {{ name|tojson }} inside a double-quoted onclick closed the attribute early,
    so clicking a typeahead result did nothing. The name now rides a single-quoted data-
    attr read via dataset in the handler.
    """
    mfr = SimpleNamespace(canonical_name='O\'Reilly "Semi" Ltd', aliases=[])
    html = templates.env.get_template("htmx/partials/manufacturers/search_results.html").render(results=[mfr])

    # Value carried on a single-quoted data- attr (tojson escapes ' → legal there).
    assert "data-mfr-name='" in html
    # Handler reads it via dataset instead of interpolating tojson into the double-quoted attr.
    assert "JSON.parse(this.dataset.mfrName)" in html
    # The raw name's double-quote must appear only inside the single-quoted data- attr as an
    # escaped JSON unicode, never as a bare " that would close the onclick early.
    assert 'onclick="(function(el, name){' in html
    assert "\\u0022" in html or "&#34;" in html  # the " in the name is escaped, not raw
