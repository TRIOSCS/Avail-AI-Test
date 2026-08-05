"""test_resell_status_caption.py — the shared status_badge caption is fully generic.

W3 (migration 207) removed the ``bid_out`` status — and with it the macro's one
caption override ({'bid_out': 'Bids out'}). Every status now renders via the generic
``value|replace('_',' ')|capitalize`` path; this pins that the W3 ladder values and
an arbitrary unmapped value all caption generically (no override map left behind).

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


def test_ladder_statuses_render_generic_captions():
    assert "Posted" in _render_badge("posted")
    assert "Bidding" in _render_badge("bidding")
    assert "Awarded" in _render_badge("awarded")
    assert "Closed" in _render_badge("closed")


def test_no_bid_out_override_left_behind():
    """The retired status has no special caption — a stray raw value would render via
    the generic path ("Bid out"), never the deleted "Bids out" override."""
    html = _render_badge("bid_out")
    assert "Bid out" in html
    assert "Bids out" not in html


def test_underscore_status_uses_generic_caption():
    """A multi-word status still renders via replace/capitalize."""
    assert "No bids" in _render_badge("no_bids")
