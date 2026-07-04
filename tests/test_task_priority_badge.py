"""Tests for the task_priority_badge Jinja macro (My Day priority badges).

L4 guard: all three priority tiers must render a visible badge so the My Day "Medium"
priority filter has an on-row counterpart. Medium (2) previously rendered NOTHING, so a
medium-priority task (the default) looked unlabelled and the filter was meaningless.

Called by: pytest
Depends on: templates.env (app.template_env), _macros.html
"""

import pytest

from app.template_env import templates

ENV = templates.env


def _render_badge(priority: int) -> str:
    tpl = ENV.from_string(
        '{% from "htmx/partials/shared/_macros.html" import task_priority_badge %}{{ task_priority_badge(priority) }}'
    )
    return tpl.render(priority=priority).strip()


@pytest.mark.parametrize(
    "priority,label",
    [
        (3, "High"),
        (2, "Medium"),
        (1, "Low"),
    ],
)
def test_every_priority_renders_a_visible_badge(priority, label):
    html = _render_badge(priority)
    assert "badge" in html, f"priority {priority} must render a .badge pill, got: {html!r}"
    assert label in html, f"priority {priority} badge must be labelled {label!r}, got: {html!r}"


def test_medium_is_no_longer_blank():
    """The specific regression: Medium (2) used to render an empty string."""
    assert _render_badge(2) != ""
