"""tests/test_p3_mobile_complete.py — QC 2026-08-10 P3 (mobile), wave 2.

Desktop-safe, mobile-scoped fixes (CSS visual result still wants a browser pass):
- A just-loaded detail pane scrolls into view on narrow viewports (the master/
  detail workspaces stack on mobile, so a row tap swapped the detail below the
  fold and looked dead).
- Wide `compact-table`s (resell) scroll within their own box on mobile instead
  of pushing the whole page sideways.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "app" / "static" / "htmx_app.js"
CSS = ROOT / "app" / "static" / "htmx_mobile.css"


def test_detail_pane_scrolls_into_view_on_mobile():
    js = JS.read_text()
    assert "htmx:afterSettle" in js
    assert "scrollIntoView" in js
    assert "window.innerWidth >= 768" in js  # desktop is excluded by construction


def test_compact_tables_scroll_on_mobile():
    css = CSS.read_text()
    assert ".compact-table" in css
    # inside a max-width media query (mobile-only), never touching desktop
    idx = css.index(".compact-table")
    assert "@media (max-width: 768px)" in css[:idx]


def test_mobile_css_braces_balanced():
    assert CSS.read_text().count("{") == CSS.read_text().count("}")
