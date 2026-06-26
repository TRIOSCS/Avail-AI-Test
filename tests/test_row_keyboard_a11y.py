"""test_row_keyboard_a11y.py — Lock the keyboard-accessibility bundle on clickable rows.

Clickable ``<tr ... hx-get=...>`` rows navigate on mouse click. Without the a11y bundle
they are invisible to keyboard users: no focus ring, no tab stop, and htmx's default
``click`` trigger never fires on Enter. The reference pattern lives in
``app/templates/htmx/partials/customers/_account_list.html`` (search it for ``role="button"``
+ ``tabindex`` + ``keyup``).

This static test reads each template that carries a clickable row and asserts every
``<tr>`` opening tag that has ``hx-get`` ALSO carries the full bundle in the SAME tag:

* ``role="button"``
* ``tabindex="0"``
* ``keyup[key=='Enter']`` (merged into ``hx-trigger`` so Enter activates the row)

A future row that adds ``hx-get`` without the bundle fails here before it ships.
Full interactive verification (Tab -> Enter -> visible focus ring) needs a headless
browser and is performed at deploy time.

Called by: pytest
Depends on: the 12 clickable-row templates listed in ``_A11Y_ROW_TEMPLATES``.
"""

import os

os.environ["TESTING"] = "1"

import re
from pathlib import Path

# The clickable-row templates whose ``<tr hx-get>`` rows must carry the a11y bundle.
# These are the heterogeneous rows hardened in the phase-3 keyboard-accessibility pass
# (some carry id=, x-show=, or wrap hx-get in a Jinja {% if %}). Add new clickable-row
# templates here as they appear.
_A11Y_ROW_TEMPLATES = (
    "app/templates/htmx/partials/customers/contacts_list.html",
    "app/templates/htmx/partials/vendors/contacts_list.html",
    "app/templates/htmx/partials/vendors/list.html",
    "app/templates/htmx/partials/materials/list.html",
    "app/templates/htmx/partials/materials/tabs/sourcing.html",
    "app/templates/htmx/partials/requisitions/req_row.html",
    "app/templates/htmx/partials/requisitions/tabs/quotes.html",
    "app/templates/htmx/partials/requisitions/tabs/buy_plans.html",
    "app/templates/htmx/partials/customers/tabs/quotes_tab.html",
    "app/templates/htmx/partials/customers/tabs/buy_plans_tab.html",
    "app/templates/htmx/partials/parts/tabs/quotes.html",
)

# Capture each <tr ...> opening tag, including multi-line tags. None of these rows embed a
# literal '>' inside an attribute value or Jinja block, so "from <tr to the first >" yields
# the whole opening tag.
_TR_OPEN_TAG = re.compile(r"<tr\b[^>]*>", re.DOTALL)


def test_clickable_rows_carry_keyboard_a11y_bundle():
    """Every ``<tr hx-get=...>`` opening tag must carry role="button", tabindex="0", and
    the ``keyup[key=='Enter']`` trigger so the row is keyboard-operable, mirroring the
    reference pattern in customers/_account_list.html."""
    offenders: list[str] = []
    rows_checked = 0

    for rel in _A11Y_ROW_TEMPLATES:
        path = Path(rel)
        assert path.exists(), f"expected clickable-row template missing: {rel}"
        text = path.read_text()
        tr_tags = _TR_OPEN_TAG.findall(text)
        assert tr_tags, f"{rel}: no <tr> opening tag found — template moved or renamed"

        for tag in tr_tags:
            if "hx-get" not in tag:
                continue  # non-clickable header / spacer rows are exempt
            rows_checked += 1
            line = text[: text.find(tag)].count("\n") + 1
            missing = [attr for attr in ('role="button"', 'tabindex="0"', "keyup[key=='Enter']") if attr not in tag]
            if missing:
                offenders.append(f"{rel}:{line} — <tr hx-get> missing {missing}")

    assert rows_checked, "no clickable <tr hx-get> rows were checked — the parse is broken"
    assert not offenders, (
        'clickable rows must be keyboard-accessible (role="button", tabindex="0", '
        "hx-trigger with keyup[key=='Enter']) — see customers/_account_list.html:\n" + "\n".join(offenders)
    )
