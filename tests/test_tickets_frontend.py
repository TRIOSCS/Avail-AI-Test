"""Tests for tickets.js frontend correctness — validates filter pills, status labels, and column order.

Called by: pytest
Depends on: app/static/tickets.js
"""

import re

import pytest


@pytest.fixture
def tickets_js():
    """Read tickets.js source for static analysis."""
    with open("app/static/tickets.js") as f:
        return f.read()


def test_filter_pills_include_rejected(tickets_js):
    """TT-20260308-077: Filter pills must include 'rejected' status."""
    # Find the filters array
    match = re.search(r"var filters = \[([^\]]+)\]", tickets_js)
    assert match, "Could not find filter pills array"
    filters_str = match.group(1)
    assert "'rejected'" in filters_str, "Filter pills missing 'rejected' status"


def test_filter_pills_include_in_progress(tickets_js):
    """TT-20260308-077: Filter pills must include 'in_progress' status."""
    match = re.search(r"var filters = \[([^\]]+)\]", tickets_js)
    assert match, "Could not find filter pills array"
    filters_str = match.group(1)
    assert "'in_progress'" in filters_str, "Filter pills missing 'in_progress' status"


def test_status_labels_has_fix_queued(tickets_js):
    """TT-20260308-078: STATUS_LABELS must include fix_queued."""
    assert "fix_queued:" in tickets_js, "STATUS_LABELS missing fix_queued"


def test_status_colors_has_fix_queued(tickets_js):
    """TT-20260308-078: STATUS_COLORS must include fix_queued."""
    match = re.search(r"var STATUS_COLORS = \{([^}]+)\}", tickets_js)
    assert match, "Could not find STATUS_COLORS"
    assert "fix_queued:" in match.group(1), "STATUS_COLORS missing fix_queued"


def test_admin_table_created_before_linked(tickets_js):
    """TT-20260308-084: In data rows, Created cell must come before Linked cell."""
    # Find the position of "// Created" and "// Linked count badge" comments
    created_pos = tickets_js.find("// Created\n        row.appendChild")
    linked_pos = tickets_js.find("// Linked count badge")
    assert created_pos > 0, "Could not find Created cell in table builder"
    assert linked_pos > 0, "Could not find Linked cell in table builder"
    assert created_pos < linked_pos, (
        "Created cell must come before Linked cell to match header order"
    )


def test_source_labels_handle_agent_and_playwright(tickets_js):
    """TT-20260308-082: Source labels must handle agent and playwright sources."""
    assert "=== 'agent'" in tickets_js, "Missing agent source handler"
    assert "=== 'playwright'" in tickets_js, "Missing playwright source handler"
