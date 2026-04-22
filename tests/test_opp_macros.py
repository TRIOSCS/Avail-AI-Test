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
    html = render_status_cell("sourcing", 6)
    assert "opp-status-dot--sourcing" in html
    assert ">Sourcing<" in html
    assert "opp-time--24h" in html
    assert "6h" in html


def test_opp_status_cell_no_time_text_when_hours_none():
    html = render_status_cell("active", None)
    assert "opp-status-dot--open" in html
    assert "opp-time--" not in html


def test_opp_status_cell_aria_label_combines_status_and_time():
    html = render_status_cell("sourcing", 6)
    assert 'aria-label="Sourcing, 6h"' in html


def test_opp_status_cell_overdue_in_html_and_aria_label():
    html = render_status_cell("sourcing", -2)
    assert "Overdue" in html
    assert 'aria-label="Sourcing, Overdue"' in html


def test_opp_status_cell_days_formatting_in_html_and_aria_label():
    html = render_status_cell("active", 120)
    assert "5d" in html
    assert 'aria-label="Open, 5d"' in html


def test_opp_status_cell_aria_label_has_no_comma_when_hours_none():
    html = render_status_cell("active", None)
    assert 'aria-label="Open"' in html
    assert ", " not in (html.split('aria-label="')[1].split('"')[0])


@pytest.mark.parametrize(
    "status,bucket,label",
    [
        ("active", "open", "Open"),
        ("sourcing", "sourcing", "Sourcing"),
        ("offers", "offered", "Offered"),
        ("quoting", "quoted", "Quoting"),
        ("quoted", "quoted", "Quoted"),
        ("won", "neutral", "Won"),
    ],
)
def test_opp_status_cell_all_buckets_render_correctly(status, bucket, label):
    html = render_status_cell(status, None)
    assert f"opp-status-dot--{bucket}" in html
    assert f">{label}<" in html
    assert f'aria-label="{label}"' in html


# ── opp_name_cell ─────────────────────────────────────────────────────


def render_name_cell(req):
    tpl = ENV.from_string('{% from "htmx/partials/shared/_macros.html" import opp_name_cell %}{{ opp_name_cell(req) }}')
    return tpl.render(req=req).strip()


def test_opp_name_cell_has_chip_row_and_name():
    req = {
        "id": 42,
        "name": "Acme Q3",
        "mpn_chip_items": [{"mpn": "LM317", "role": "primary"}],
        "match_reason": None,
        "matched_mpn": None,
    }
    html = render_name_cell(req)
    assert "opp-chip-row" in html
    assert "LM317" in html
    assert "Acme Q3" in html


def test_opp_name_cell_renders_match_badge_when_present():
    req = {
        "id": 7,
        "name": "Foo",
        "mpn_chip_items": [],
        "match_reason": "part",
        "matched_mpn": "XYZ123",
    }
    html = render_name_cell(req)
    assert "XYZ123" in html
    assert "match-badge" in html


def test_opp_name_cell_renders_customer_match_badge():
    # Spec: match_reason='customer' path must render sky-colored badge
    # with building-icon SVG and "customer match" label.
    req = {
        "id": 8,
        "name": "Bar",
        "mpn_chip_items": [],
        "match_reason": "customer",
        "matched_mpn": None,
    }
    html = render_name_cell(req)
    assert "match-badge" in html
    assert "bg-sky-50" in html
    assert "customer match" in html
    assert "<svg" in html  # building icon present


def test_opp_name_cell_part_match_badge_has_chip_icon():
    # Spec: match_reason='part' path must include the chip SVG icon
    # (not just the matched MPN text).
    req = {
        "id": 9,
        "name": "Baz",
        "mpn_chip_items": [],
        "match_reason": "part",
        "matched_mpn": "LM317",
    }
    html = render_name_cell(req)
    assert "<svg" in html  # chip icon present
    assert "LM317" in html


def test_opp_name_cell_name_has_truncate_tip():
    req = {
        "id": 1,
        "name": "VeryLong" * 10,
        "mpn_chip_items": [],
        "match_reason": None,
        "matched_mpn": None,
    }
    html = render_name_cell(req)
    assert "x-truncate-tip" in html


# ── opp_row_action_rail ───────────────────────────────────────────────


def _req(status="active", claimed_by_id=None, name="Acme", rid=1):
    return {"id": rid, "name": name, "status": status, "claimed_by_id": claimed_by_id}


def _user(uid=5, role="buyer"):
    class U:
        pass

    u = U()
    u.id = uid
    u.role = role
    return u


def render_rail(req, user):
    tpl = ENV.from_string(
        '{% from "htmx/partials/shared/_macros.html" import opp_row_action_rail %}{{ opp_row_action_rail(req, user) }}'
    )
    return tpl.render(req=req, user=user).strip()


def test_rail_shows_archive_when_not_archived():
    html = render_rail(_req(status="active"), _user())
    assert "action/archive" in html
    assert "action/activate" not in html


def test_rail_shows_activate_when_archived():
    html = render_rail(_req(status="archived"), _user())
    assert "action/activate" in html
    assert "action/archive" not in html


def test_rail_shows_claim_when_unclaimed_and_role_allowed():
    html = render_rail(_req(claimed_by_id=None), _user(role="buyer"))
    assert "action/claim" in html
    assert "action/unclaim" not in html


def test_rail_shows_unclaim_when_claimed_by_current_user():
    html = render_rail(_req(claimed_by_id=5), _user(uid=5))
    assert "action/unclaim" in html


def test_rail_has_toolbar_role_and_aria_label():
    html = render_rail(_req(), _user())
    assert 'role="toolbar"' in html
    assert 'aria-label="Row actions"' in html
