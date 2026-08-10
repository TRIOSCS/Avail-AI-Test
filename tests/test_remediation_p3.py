"""tests/test_remediation_p3.py — QC 2026-08-10 P3 (mobile touch actions).

On a touch device (hover: none) there is no hover, so the ~20 row actions styled
`opacity-0 group-hover:opacity-100` were permanently invisible and untappable on a
phone. A `@media (hover: none)` rule in htmx_mobile.css now forces them visible. (CSS
can only be checked structurally here; the visual result needs a browser.)
"""

from pathlib import Path

CSS = Path(__file__).resolve().parents[1] / "app" / "static" / "htmx_mobile.css"


def test_touch_reveals_hover_only_actions():
    css = CSS.read_text()
    assert "@media (hover: none)" in css
    assert "group-hover:opacity-100" in css  # the utility it un-hides


def test_mobile_css_braces_balanced():
    css = CSS.read_text()
    assert css.count("{") == css.count("}")
