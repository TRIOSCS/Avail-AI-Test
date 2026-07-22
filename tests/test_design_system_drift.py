"""test_design_system_drift.py — Guards against cosmetic drift in the AvailAI design
system so retired patterns (gray brand focus rings, dark-mode variants, ad-hoc component
markup) can't silently return.

What it guards:
  1. The shared `page_header` macro (the canonical page-title/subtitle block used across
     every partial) still exists in shared/_macros.html.
  2. Shared partials (app/templates/htmx/partials/shared/) never reintroduce the retired
     gray `focus:ring-brand-500` / `focus:border-brand-500` classes on form controls —
     these were converted to the accent color / the `.input-focus` mixin.
  3. The canonical component classes (.card, .btn-primary, .badge, .table-wrapper,
     .data-table, .h1, .input, .font-data) stay defined in app/static/styles.css — the
     macros and every page sweep depend on these; dropping one silently breaks styling
     across dozens of templates.
  4. AvailAI is intentionally light-only — no template may introduce a `dark:` Tailwind
     variant.

Why: mirrors the design-system-consistency guards in test_static_analysis.py (see the
"Design-system consistency guards" section there) — pure filesystem/regex checks, no DB,
no network, hermetic under pytest-xdist.

Called by: pytest
Depends on: app/templates/htmx/partials/shared/_macros.html, app/static/styles.css,
            app/templates/ (recursive scan)
"""

import re
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────────
# Enforced surface — edit this list to widen coverage in later PRs. Each
# entry is a directory (relative to repo root) that guard #2 (and any future
# file-walking guard reusing this constant) scans recursively for *.html.
# ─────────────────────────────────────────────────────────────────────────
_ENFORCED_SHARED_ROOTS: list[str] = [
    "app/templates/htmx/partials/shared",
]

_ALL_TEMPLATES_ROOT = "app/templates"


def _html_files(roots: list[str]) -> list[Path]:
    files: list[Path] = []
    for root in roots:
        files.extend(sorted(Path(root).rglob("*.html")))
    return files


def test_page_header_macro_exists():
    """The shared page_header macro must still be defined — every page-title/subtitle
    block across the app calls it; losing it silently breaks the header on every page
    that hasn't been hand-rewritten."""
    macros = Path("app/templates/htmx/partials/shared/_macros.html").read_text()
    assert "{% macro page_header(" in macros, (
        "page_header macro missing from shared/_macros.html — every page-title block depends on it"
    )


def test_shared_partials_no_gray_brand_focus_ring():
    """Shared form-control partials must use the accent focus ring / .input-focus mixin,
    not the retired gray-brand focus classes.

    Enforced surface is app/templates/htmx/partials/shared/ (see
    _ENFORCED_SHARED_ROOTS above) — widen that list to extend coverage to more
    directories in a later PR without touching this test body.
    """
    retired = ("focus:ring-brand-500", "focus:border-brand-500")
    offenders: list[str] = []
    for path in _html_files(_ENFORCED_SHARED_ROOTS):
        text = path.read_text()
        for needle in retired:
            if needle in text:
                offenders.append(f"{path}: contains retired class {needle!r}")
    assert not offenders, (
        "retired gray-brand focus-ring classes reappeared in shared partials — use the "
        "accent focus ring / .input-focus mixin instead:\n" + "\n".join(offenders)
    )


def test_canonical_component_classes_defined_in_styles_css():
    """The canonical component layer must define these classes — macros and every page
    sweep depend on them; a dropped definition silently breaks styling across the app."""
    css = Path("app/static/styles.css").read_text()
    required = [
        ".card",
        ".btn-primary",
        ".badge",
        ".table-wrapper",
        ".data-table",
        ".h1",
        ".input",
        ".font-data",
    ]
    missing = [sel for sel in required if not re.search(r"(?m)^\s*" + re.escape(sel) + r"\b", css)]
    assert not missing, f"canonical component classes missing from styles.css: {missing}"


def test_no_dark_mode_variants_in_templates():
    """AvailAI is intentionally light-only — no template may use a Tailwind `dark:`
    variant.

    Verified clean across the FULL app/templates/ tree today (zero hits), so the guard
    is scoped to the whole tree rather than narrowed to shared/ only — if a future sweep
    ever needs to narrow this, document why here and switch to _ENFORCED_SHARED_ROOTS.
    """
    dark_variant = re.compile(r'class="[^"]*\bdark:')
    offenders: list[str] = []
    for path in sorted(Path(_ALL_TEMPLATES_ROOT).rglob("*.html")):
        text = path.read_text()
        for m in dark_variant.finditer(text):
            line = text[: m.start()].count("\n") + 1
            offenders.append(f"{path}:{line}")
    assert not offenders, (
        "dark: Tailwind variant found — AvailAI is light-only, no dark-mode classes allowed:\n" + "\n".join(offenders)
    )
