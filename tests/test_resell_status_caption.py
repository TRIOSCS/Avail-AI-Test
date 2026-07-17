"""test_resell_status_caption.py — the shared status_badge caption for bid_out (D5).

The Resell lifecycle adds ``bid_out`` and ``closed`` as DISTINCT states. The generic
``value|replace('_',' ')|capitalize`` renders ``bid_out`` as "Bid out", which reads
awkwardly and blurs it against CLOSED; D5 wants an explicit "Bids out" label, kept
distinct from CLOSED's "Closed". Statuses with no explicit override still fall back to the
generic caption.

Called by: pytest
Depends on: app.template_env, htmx/partials/shared/_macros.html
"""

from app.template_env import templates

ENV = templates.env


def _render_badge(value: str) -> str:
    tpl = ENV.from_string(
        '{% from "htmx/partials/shared/_macros.html" import status_badge %}{{ status_badge("' + value + '") }}'
    )
    return tpl.render().strip()


def test_bid_out_renders_bids_out():
    html = _render_badge("bid_out")
    assert "Bids out" in html
    assert "Bid out" not in html  # not the generic replace/capitalize caption


def test_closed_stays_closed_distinct_from_bid_out():
    """CLOSED keeps its own generic 'Closed' caption — D5 keeps the two states
    distinct."""
    html = _render_badge("closed")
    assert "Closed" in html
    assert "Bids out" not in html


def test_unmapped_status_uses_generic_caption():
    """A status with no explicit label override still renders via replace/capitalize."""
    assert "Collecting" in _render_badge("collecting")
