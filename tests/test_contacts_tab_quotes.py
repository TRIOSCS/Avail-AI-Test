"""test_contacts_tab_quotes.py — Phase-4 quick-win regression tests.

Covers:
  1. contacts_tab.html empty-state hint uses ASCII attribute delimiters (no
     U+201C/U+201D smart quotes that silently break the `hidden` class +
     x-text search-term binding).
  2. contacts_list.html renders a 4-swatch cadence-dot legend whose colors +
     labels mirror the existing `dot_colors` map and the cadence filter.

Called by: pytest
Depends on: app.template_env.templates (Jinja2 env)
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from app.template_env import templates

ENV = templates.env

CONTACTS_TAB = Path("app/templates/htmx/partials/customers/tabs/contacts_tab.html")

# Curly double-quote characters that must never serve as HTML attribute delimiters.
CURLY_LEFT = "“"  # "
CURLY_RIGHT = "”"  # "


# ── Fix 1: smart-quote attribute delimiters ──────────────────────────────────


def test_contacts_tab_empty_state_uses_ascii_quotes():
    """The empty-state hint's attributes must be ASCII-quoted so the `hidden` class +
    x-text binding are live (smart quotes break attribute parsing)."""
    src = CONTACTS_TAB.read_text(encoding="utf-8")
    affected = [ln for ln in src.splitlines() if "data-contacts-empty" in ln or "No people match" in ln]
    assert affected, "Could not locate the empty-state hint lines in contacts_tab.html"
    blob = "\n".join(affected)
    assert CURLY_RIGHT not in blob, 'U+201D smart quote found in empty-state attributes — use ASCII "'
    assert CURLY_LEFT not in blob, 'U+201C smart quote found in empty-state attributes — use ASCII "'


def test_contacts_tab_empty_state_attributes_well_formed():
    """The empty-state element's `hidden` class + x-text search-term binding are present
    and ASCII-quoted after the fix."""
    src = CONTACTS_TAB.read_text(encoding="utf-8")
    assert 'data-contacts-empty class="hidden mt-2 text-xs text-gray-600" x-cloak' in src
    assert 'x-text="q ? ' in src  # the search-term binding is ASCII-quoted and live


def test_contacts_tab_renders_well_formed_empty_state():
    """Rendering the tab produces the empty-state hint with a quoted `hidden` class and
    the x-text binding intact."""
    tpl = ENV.get_template("htmx/partials/customers/tabs/contacts_tab.html")
    company = type("C", (), {"id": 42, "name": "Acme"})()
    html = tpl.render(
        company=company,
        contact_rows=[],
        roles=("specifier", "buyer_po", "other"),
        now_utc=datetime.now(timezone.utc),
        active_sites=[],
        can_find_contacts=False,
    )
    assert 'data-contacts-empty class="hidden mt-2 text-xs text-gray-600"' in html
    assert 'x-text="q ?' in html


# ── Fix 2: cadence-dot legend ────────────────────────────────────────────────


def _render_contacts_list(contacts=None):
    tpl = ENV.get_template("htmx/partials/customers/contacts_list.html")
    return tpl.render(
        contacts=contacts or [],
        companies=[],
        contact_roles=[],
        search="",
        company_id=0,
        contact_role="",
        cadence_state="",
        total=0,
        limit=50,
        offset=0,
    )


def test_contacts_list_legend_labels_present():
    """All four cadence legend labels render (same wording as the filter)."""
    html = _render_contacts_list()
    for label in ("New", "On target", "Due soon", "Overdue"):
        assert label in html, f"Legend missing label {label!r}"


def test_contacts_list_legend_colors_present():
    """The legend reuses the dot_colors swatch classes for each cadence state."""
    html = _render_contacts_list()
    for color in ("bg-gray-300", "bg-emerald-400", "bg-amber-400", "bg-rose-500"):
        assert color in html, f"Legend missing swatch color {color!r}"


def test_contacts_list_legend_uses_readable_label_color():
    """Legend label text uses the higher-contrast text-gray-600 (not gray-500)."""
    html = _render_contacts_list()
    # The legend row carries text-gray-600; the swatches are rounded-full h-2 w-2.
    assert "text-gray-600" in html
    assert "inline-block h-2 w-2 rounded-full" in html
