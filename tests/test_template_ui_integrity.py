"""test_template_ui_integrity.py — Regression guards for two shipped UI bugs.

1. Sightings split-panel was dead (right panel never populated, slide-bar would
   not drag) because a JS `//` comment inside the root `x-data="..."` attribute
   contained a literal double-quote (`the "Search" button`). The double-quote
   prematurely closed the double-quoted attribute, so the browser truncated the
   `x-data` expression and Alpine threw a SyntaxError on init — leaving
   `selectReq` / `startDrag` / `selectedReqId` undefined. Fix: keep prose out of
   the attribute (sightings/list.html).

2. The global modal chrome (base.html) had no close (X) control, so any content
   template lacking its own close — e.g. materials/detail.html — opened a modal
   that could only be dismissed by Escape/backdrop. Fix: one persistent X in the
   chrome.

3. Same bug class as (1): the materials manufacturer filter embedded `|tojson`
   in a DOUBLE-quoted `x-data` attribute. `tojson` returns a Markup-safe string,
   so the `|e` that was meant to protect it is a no-op — the inner `"` truncated
   the attribute, breaking the filter. Fix: single-quoted attribute (tojson
   escapes `'`). The general guard below catches this class anywhere.

Called by: pytest
Depends on: app/routers/htmx_views.py, app/routers/sightings.py, conftest.py
"""

import glob
import os
import re

os.environ["TESTING"] = "1"

from fastapi.testclient import TestClient

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read_template(rel_path: str) -> str:
    with open(os.path.join(_REPO_ROOT, rel_path), encoding="utf-8") as f:
        return f.read()


def _extract_attr_value(html: str, attr_open: str, near_marker: str) -> str:
    """Return the value of the `attr_open` (e.g. ``x-data="``) attribute whose opening
    precedes ``near_marker``.

    The value ends at the first double-quote after the opening — which is exactly what a
    stray inner double-quote would (incorrectly) trigger, so a truncated attribute is
    observable here.
    """
    marker = html.find(near_marker)
    assert marker != -1, f"marker {near_marker!r} not found in rendered HTML"
    start = html.rfind(attr_open, 0, marker)
    assert start != -1, f"{attr_open!r} not found before {near_marker!r}"
    value_start = start + len(attr_open)
    end = html.find('"', value_start)
    assert end != -1, "unterminated attribute"
    return html[value_start:end]


class TestSightingsRootXDataIntegrity:
    """The root split-panel x-data must render intact (not truncated by a stray quote),
    or the whole sightings page goes dead."""

    def test_root_x_data_not_truncated(self, client: TestClient):
        resp = client.get("/v2/partials/sightings/workspace", headers={"HX-Request": "true"})
        assert resp.status_code == 200
        # The root x-data is the one declaring splitRatio (drag) + selectReq (panel).
        attr = _extract_attr_value(resp.text, 'x-data="', "splitRatio")

        # If a stray double-quote truncates the attribute, these members defined
        # AFTER the truncation point fall outside the parsed value.
        assert "selectReq" in attr, (
            "root x-data truncated before selectReq — a literal double-quote "
            "inside the attribute closed it early (regression of the //comment bug)"
        )
        assert "closeMobileDetail" in attr, "root x-data truncated before closeMobileDetail — attribute closed early"
        # The attribute value must be quote-balanced JS (no naked double-quote).
        assert '"' not in attr, "stray double-quote inside x-data attribute value"
        # Prose JS // comments must not live inside the attribute (fragile: any
        # quote in the prose re-breaks it).
        assert "//" not in attr, (
            "JS // comment inside x-data attribute — move prose to a Jinja {# #} comment outside the attribute"
        )


class TestGlobalModalCloseAffordance:
    """Every modal must be closable via a visible control supplied by the chrome,
    independent of whatever content template is loaded into #modal-content.

    The close button is static chrome (no Jinja conditionals), so we assert against the
    base template directly.
    """

    def test_modal_chrome_has_close_button(self):
        html = _read_template("app/templates/htmx/base.html")
        assert 'id="modal-content"' in html, "global modal mount missing from base.html"
        assert 'aria-label="Close"' in html, "global modal chrome has no labelled close button"
        assert "$dispatch('close-modal')" in html, "modal close button does not dispatch close-modal"
        # The close control is chrome: it precedes #modal-content, so it exists
        # regardless of which content template loads (which may supply no close).
        assert html.index('aria-label="Close"') < html.index('id="modal-content"'), (
            "close button should be part of the modal chrome, before #modal-content"
        )

    def test_material_card_relies_on_chrome_close(self):
        """The material card (loaded into #modal-content) historically shipped no close
        control of its own — it now relies on the chrome X.

        Guard that the chrome remains the close path (no regression to a dead-end
        modal).
        """
        card = _read_template("app/templates/htmx/partials/materials/detail.html")
        # It must not reintroduce its own redundant top-right close (would double
        # up with the chrome X); the only close path is the chrome.
        assert card.count("$dispatch('close-modal')") == 0, (
            "material card should not add its own close — the chrome X handles it"
        )


class TestNoQuoteTruncatedAlpineAttributes:
    """General guard for the attribute-truncation bug class (#1, #3): a literal double-
    quote inside a double-quoted Alpine attribute closes it early and breaks the
    component.

    Scans every template.
    """

    # `|tojson` inside a DOUBLE-quoted x-data: tojson keeps `"`, so the attribute
    # truncates (any `|e`/escape after it is a no-op on the Markup-safe result).
    _TOJSON_IN_DQ_XDATA = re.compile(r'x-data="\{\{[^"]*\|\s*tojson')

    def test_no_tojson_in_double_quoted_x_data(self):
        offenders = []
        for path in glob.glob(os.path.join(_REPO_ROOT, "app/templates/**/*.html"), recursive=True):
            with open(path, encoding="utf-8") as f:
                if self._TOJSON_IN_DQ_XDATA.search(f.read()):
                    offenders.append(os.path.relpath(path, _REPO_ROOT))
        assert not offenders, (
            "tojson embedded in a double-quoted x-data attribute truncates it "
            '(inner " closes the attribute). Use a single-quoted attribute. '
            f"Offending templates: {offenders}"
        )

    def test_manufacturer_filter_uses_single_quoted_xdata(self):
        html = _read_template("app/templates/htmx/partials/materials/filters/manufacturers.html")
        assert re.search(r"x-data='\{\{[^']*\|\s*tojson[^']*\}\}'", html), (
            "manufacturer label must embed tojson in a SINGLE-quoted x-data attribute"
        )


class TestHxTriggerEventFilterPlacement:
    """An htmx event filter `[condition]` must hug the event name (e.g. `keyup[cond]`).

    Trailing a modifier (`delay:800ms[cond]`) throws `htmx:syntax:error` and silently
    disables the trigger. Scans every template.
    """

    def test_event_filter_hugs_event_name(self):
        offenders = []
        for path in glob.glob(os.path.join(_REPO_ROOT, "app/templates/**/*.html"), recursive=True):
            with open(path, encoding="utf-8") as f:
                txt = f.read()
            for m in re.finditer(r'hx-trigger="([^"]*)"', txt):
                for spec in m.group(1).split(","):
                    spec = spec.strip()
                    if "[" not in spec or "{{" in spec:  # skip jinja-templated
                        continue
                    before_bracket = spec[: spec.index("[")]
                    # Valid: the bracket directly follows the event token, so nothing
                    # but the event name precedes it (no spaces / modifiers).
                    if " " in before_bracket:
                        rel = os.path.relpath(path, _REPO_ROOT)
                        offenders.append(f"{rel}: {spec}")
        assert not offenders, (
            "htmx [filter] must immediately follow the event name (keyup[cond]); "
            "trailing a modifier throws htmx:syntax:error. Offenders: " + str(offenders)
        )


class TestSplitPanelMobileStacking:
    """Wave 7: the desktop-first CDM account split-panel workspace must stack vertically
    on phones and keep the side-by-side split on md:+ . (The requisitions2 split-panel
    workspace was retired.)

    The mechanism (mirrored from sightings/list.html): the container is
    ``flex flex-col md:flex-row``; the inline ``:style`` width binding is guarded by
    ``window.innerWidth >= 768`` so it is empty on phones and the ``w-full`` class
    governs (no !important needed), while the exact desktop width is preserved
    unchanged at md:+ ; the drag divider is ``hidden md:block`` (desktop-only).

    Static assertions on the template source — desktop classes must remain literally
    present (byte-identical split) and the mobile-stacking classes must be added.
    """

    # (relative template path, split-container id, splitPanel panel key)
    # (The requisitions2 split-panel workspace was retired; the CDM workspace remains.)
    _SURFACES = [
        ("app/templates/htmx/partials/customers/list.html", "split-cdm", "cdm"),
    ]

    def test_container_stacks_on_mobile_splits_on_desktop(self):
        for rel, container_id, _ in self._SURFACES:
            html = _read_template(rel)
            assert f'id="{container_id}"' in html, f"{rel}: split container {container_id} missing"
            # Container: vertical stack on phones, horizontal split at md:+ .
            assert "flex flex-col md:flex-row" in html, (
                f"{rel}: split container must be 'flex flex-col md:flex-row' (stack on phones, side-by-side on desktop)"
            )

    def test_width_binding_guarded_so_w_full_wins_on_mobile(self):
        for rel, _, _ in self._SURFACES:
            html = _read_template(rel)
            # The inline width must only apply at >=768px, otherwise it would beat the
            # w-full class on phones (inline style > class) and the panel would not
            # go full-width.
            assert "window.innerWidth >= 768 ? ('width: ' + leftWidth + '%') : ''" in html, (
                f"{rel}: left-panel :style width must be guarded by window.innerWidth >= 768 "
                "so w-full governs on phones"
            )
            # The mobile full-width / desktop inline-width handoff class pair.
            assert "w-full md:w-auto" in html, f"{rel}: left panel must be 'w-full md:w-auto'"

    def test_divider_is_desktop_only(self):
        for rel, _, _ in self._SURFACES:
            html = _read_template(rel)
            assert "hidden md:block w-1" in html, (
                f"{rel}: the col-resize drag divider must be 'hidden md:block' "
                "(useless thin sliver when stacked on phones)"
            )

    def test_desktop_split_preserved(self):
        for rel, _, panel in self._SURFACES:
            html = _read_template(rel)
            # The resizable splitPanel component and its drag affordance survive — the
            # change is presentation-only at the small-screen breakpoint.
            assert f"splitPanel('{panel}'," in html, f"{rel}: splitPanel('{panel}', ...) removed"
            assert "cursor-col-resize" in html, f"{rel}: drag-resize affordance removed"
            # Quote-balanced :style binding (no stray double-quote breaking Alpine).
            for m in re.finditer(r':style="([^"]*)"', html):
                assert '"' not in m.group(1), f"{rel}: stray double-quote inside :style binding"
