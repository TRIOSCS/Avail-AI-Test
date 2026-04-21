"""Tests for Opportunity Table v2 Jinja macros.

Called by: pytest
Depends on: templates.env (app.template_env), _macros.html
"""

import pytest

from app.template_env import templates

ENV = templates.env


def render_macro(call_expr: str, **ctx) -> str:
    tpl = ENV.from_string(
        '{% from "htmx/partials/shared/_macros.html" import '
        "status_dot, deal_value, coverage_meter, urgency_accent_class, time_text %}" + call_expr,
    )
    return tpl.render(**ctx).strip()


# ── status_dot ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value,bucket,label",
    [
        ("active", "open", "Open"),
        ("sourcing", "sourcing", "Sourcing"),
        ("offers", "offered", "Offered"),
        ("quoting", "quoted", "Quoting"),
        ("quoted", "quoted", "Quoted"),
        ("won", "neutral", "Won"),
    ],
)
def test_status_dot_buckets(value, bucket, label):
    html = render_macro(f'{{{{ status_dot("{value}") }}}}')
    assert f"opp-status-dot--{bucket}" in html
    assert f">{label}<" in html


# ── deal_value ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "amount,source,expect_class,expect_text",
    [
        (150000, "entered", "opp-deal--tier-primary-500", "$150,000"),
        (5000, "entered", "opp-deal--tier-primary-400", "$5,000"),
        (500, "entered", "opp-deal--tier-tertiary", "$500"),
        (25000, "computed", "opp-deal--computed", "$25,000"),
        (None, "none", "opp-deal--tier-tertiary", "—"),
        (0, "none", "opp-deal--tier-tertiary", "—"),
    ],
)
def test_deal_value_tiers(amount, source, expect_class, expect_text):
    html = render_macro(f"{{{{ deal_value({amount!r}, {source!r}) }}}}")
    assert expect_class in html
    assert expect_text in html


def test_deal_value_partial_has_tilde_and_italic_and_tooltip():
    html = render_macro('{{ deal_value(30000, "partial", priced_count=3, requirement_count=5) }}')
    assert "~$30,000" in html
    assert "opp-deal--computed" in html
    assert "opp-deal--partial" in html
    assert "3 of 5 parts priced" in html


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


# ── urgency_accent_class ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "hours,urgency,expected",
    [
        (6, "normal", "opp-row--urgent-24h"),
        (48, "normal", "opp-row--urgent-72h"),
        (120, "normal", ""),
        (None, "normal", ""),
        (None, "critical", "opp-row--urgent-24h"),
        (120, "critical", "opp-row--urgent-24h"),
    ],
)
def test_urgency_accent_class(hours, urgency, expected):
    html = render_macro(f"{{{{ urgency_accent_class({hours!r}, {urgency!r}) }}}}")
    assert html == expected


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
