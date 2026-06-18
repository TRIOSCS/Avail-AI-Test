"""Regression tests for the proactive Scorecard panel rendering.

Guards the data-contract between get_scorecard() (proactive_service) and the
scorecard.html template: conversion_rate is already a whole-number percent (must
NOT be multiplied by 100 again), and the revenue card reads converted_revenue
(the key the service actually returns) — not the never-set total_revenue.

Called by: pytest. Depends on: app.template_env.templates, app.services.proactive_service.
"""

from app.template_env import templates


def _render(stats: dict) -> str:
    return templates.get_template("htmx/partials/proactive/scorecard.html").render(stats=stats)


def test_conversion_rate_not_multiplied_by_100():
    """A 33.3% rate (already a percent) must render as '33%', never '3330%'."""
    html = _render({"total_sent": 6, "total_converted": 2, "conversion_rate": 33.3, "converted_revenue": 0})
    assert "33%" in html
    assert "3330%" not in html


def test_revenue_reads_converted_revenue_key():
    """Revenue card must read converted_revenue, not the never-set total_revenue."""
    # Provide all money keys so defaults don't produce a stray $0.
    html = _render(
        {
            "total_sent": 6,
            "total_converted": 2,
            "conversion_rate": 33.3,
            "converted_revenue": 139122.5,
            "gross_profit": 41972.5,
            "anticipated_revenue": 278580.0,
        }
    )
    assert "$139,122" in html  # rounded, thousands-separated
    # The old buggy key being absent must not silently render $0.
    assert "$0" not in html


def test_revenue_defaults_to_zero_when_no_converted_revenue():
    """With no converted revenue the card shows $0 (not a KeyError)."""
    html = _render({"total_sent": 3, "total_converted": 0, "conversion_rate": 0, "converted_revenue": 0})
    assert "$0" in html
    assert "0%" in html


def test_scorecard_matches_service_contract():
    """Render directly from a get_scorecard-shaped dict to keep the contract honest."""
    # Shape mirrors proactive_service.get_scorecard() return value.
    stats = {
        "total_sent": 6,
        "total_converted": 2,
        "total_quoted": 0,
        "total_po": 0,
        "conversion_rate": 33.3,
        "anticipated_revenue": 278580.0,
        "converted_revenue": 139122.5,
        "gross_profit": 41972.5,
    }
    html = _render(stats)
    assert "33%" in html and "3330%" not in html
    assert "$139,122" in html


def test_gross_profit_renders():
    """gross_profit=41972.5 → '$41,972' (Python round-half-even: .5 rounds to even →
    41972)."""
    html = _render(
        {
            "total_sent": 6,
            "total_converted": 2,
            "conversion_rate": 33.3,
            "converted_revenue": 0,
            "gross_profit": 41972.5,
            "anticipated_revenue": 0,
        }
    )
    assert "$41,972" in html


def test_pipeline_renders():
    """anticipated_revenue=278580.0 → '$278,580'."""
    html = _render(
        {
            "total_sent": 6,
            "total_converted": 2,
            "conversion_rate": 33.3,
            "converted_revenue": 0,
            "gross_profit": 0,
            "anticipated_revenue": 278580.0,
        }
    )
    assert "$278,580" in html


def test_conv_rate_no_double_100_with_money_fields():
    """Conv.

    Rate still renders correctly when the full service dict (incl. money fields) is
    used.
    """
    html = _render(
        {
            "total_sent": 6,
            "total_converted": 2,
            "conversion_rate": 33.3,
            "converted_revenue": 139122.5,
            "gross_profit": 41972.5,
            "anticipated_revenue": 278580.0,
        }
    )
    assert "33%" in html
    assert "3330%" not in html
