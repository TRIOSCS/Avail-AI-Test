"""Tests for Opportunity Table v2 Jinja macros.

Called by: pytest
Depends on: templates.env (app.template_env), _macros.html
"""

import pytest

from app.template_env import templates

ENV = templates.env


def render_macro(call_expr: str, **ctx) -> str:
    tpl = ENV.from_string(
        '{% from "htmx/partials/shared/_macros.html" import status_dot, coverage_meter, time_text %}' + call_expr,
    )
    return tpl.render(**ctx).strip()


# ── status_dot ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value,bucket,label",
    [
        ("open", "open", "Open"),
        ("rfqs_sent", "sourcing", "RFQs Sent"),
        ("offers", "offered", "Offers"),
        ("quoted", "quoted", "Quoted"),
        ("hotlist", "neutral", "Hotlist"),
        ("won", "neutral", "Won"),
    ],
)
def test_status_dot_buckets(value, bucket, label):
    html = render_macro(f'{{{{ status_dot("{value}") }}}}')
    assert f"opp-status-dot--{bucket}" in html
    assert f">{label}<" in html


# ── coverage_meter ────────────────────────────────────────────────────


def test_coverage_meter_empty():
    html = render_macro("{{ coverage_meter(0, 0) }}")
    assert html.count("opp-coverage-seg") == 6
    assert "opp-coverage-seg--filled" not in html
    assert "no parts yet" in html


def test_coverage_meter_half():
    html = render_macro("{{ coverage_meter(3, 6) }}")
    assert html.count("opp-coverage-seg--filled") == 3


def test_coverage_meter_full():
    html = render_macro("{{ coverage_meter(6, 6) }}")
    assert html.count("opp-coverage-seg--filled") == 6


def test_coverage_meter_aria_label():
    html = render_macro("{{ coverage_meter(2, 5) }}")
    assert 'aria-label="Coverage: 2 of 5 parts sourced"' in html


# ── time_text ─────────────────────────────────────────────────────────


def test_time_text_none_is_empty():
    assert render_macro("{{ time_text(None) }}") == ""


def test_time_text_overdue():
    html = render_macro("{{ time_text(-2) }}")
    assert "Overdue" in html
    assert "opp-time--24h" in html


def test_time_text_under_24():
    html = render_macro("{{ time_text(6) }}")
    assert "6h" in html
    assert "opp-time--24h" in html


def test_time_text_between_24_and_72():
    html = render_macro("{{ time_text(48) }}")
    assert "48h" in html
    assert "opp-time--72h" in html


def test_time_text_days():
    html = render_macro("{{ time_text(120) }}")
    assert "5d" in html
    assert "opp-time--normal" in html


# ── mpn_chips_aggregated ──────────────────────────────────────────────


def render_aggregated(items_expr: str) -> str:
    tpl = ENV.from_string(
        '{% from "htmx/partials/shared/_mpn_chips.html" import mpn_chips_aggregated %}'
        + f"{{{{ mpn_chips_aggregated({items_expr}) }}}}"
    )
    return tpl.render().strip()


def test_mpn_chips_aggregated_renders_primaries_before_subs():
    items = [
        {"mpn": "LM317", "role": "primary"},
        {"mpn": "NE555", "role": "primary"},
        {"mpn": "LM337", "role": "sub"},
    ]
    html = render_aggregated(repr(items))
    pos_lm317 = html.index("LM317")
    pos_ne555 = html.index("NE555")
    pos_lm337 = html.index("LM337")
    assert pos_lm317 < pos_ne555 < pos_lm337


def test_mpn_chips_aggregated_includes_overflow_bucket_and_directive():
    items = [{"mpn": f"M{i}", "role": "primary"} for i in range(4)]
    html = render_aggregated(repr(items))
    assert "x-chip-overflow" in html
    assert "opp-chip-more" in html


def test_mpn_chips_aggregated_plus_n_button_carries_no_data_tip_content():
    # Security invariant (spec §Name cell / MPN chip row): the +N button
    # MUST NOT have a data-tip-content attribute. Hidden-chip content flows
    # at runtime via _tipNodes on the element, not as an HTML-string attr.
    # A regression here would re-open the innerHTML XSS class.
    items = [{"mpn": f"M{i}", "role": "primary"} for i in range(4)]
    html = render_aggregated(repr(items))
    assert "data-tip-content" not in html


def test_mpn_chips_aggregated_empty_renders_placeholder():
    html = render_aggregated("[]")
    assert "—" in html


# ── opp_status_cell ───────────────────────────────────────────────────


def render_status_cell(status, hours):
    tpl = ENV.from_string(
        '{% from "htmx/partials/shared/_macros.html" import opp_status_cell %}'
        f"{{{{ opp_status_cell({status!r}, {hours!r}) }}}}"
    )
    return tpl.render().strip()


def test_opp_status_cell_includes_dot_and_time_text():
    html = render_status_cell("rfqs_sent", 6)
    assert "opp-status-dot--sourcing" in html
    assert ">RFQs Sent<" in html
    assert "opp-time--24h" in html
    assert "6h" in html


def test_opp_status_cell_no_time_text_when_hours_none():
    html = render_status_cell("open", None)
    assert "opp-status-dot--open" in html
    assert "opp-time--" not in html


def test_opp_status_cell_aria_label_combines_status_and_time():
    html = render_status_cell("rfqs_sent", 6)
    assert 'aria-label="RFQs Sent, 6h"' in html


def test_opp_status_cell_overdue_in_html_and_aria_label():
    html = render_status_cell("rfqs_sent", -2)
    assert "Overdue" in html
    assert 'aria-label="RFQs Sent, Overdue"' in html


def test_opp_status_cell_days_formatting_in_html_and_aria_label():
    html = render_status_cell("open", 120)
    assert "5d" in html
    assert 'aria-label="Open, 5d"' in html


def test_opp_status_cell_aria_label_has_no_comma_when_hours_none():
    html = render_status_cell("open", None)
    assert 'aria-label="Open"' in html
    assert ", " not in (html.split('aria-label="')[1].split('"')[0])


@pytest.mark.parametrize(
    "status,bucket,label",
    [
        ("open", "open", "Open"),
        ("rfqs_sent", "sourcing", "RFQs Sent"),
        ("offers", "offered", "Offers"),
        ("quoted", "quoted", "Quoted"),
        ("hotlist", "neutral", "Hotlist"),
        ("won", "neutral", "Won"),
    ],
)
def test_opp_status_cell_all_buckets_render_correctly(status, bucket, label):
    html = render_status_cell(status, None)
    assert f"opp-status-dot--{bucket}" in html
    assert f">{label}<" in html
    assert f'aria-label="{label}"' in html
