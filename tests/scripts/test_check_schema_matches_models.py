"""tests/scripts/test_check_schema_matches_models.py — Tests for the schema diff
comparator used in CI and local smoke.

Called by: pytest
Depends on: scripts.check_schema_matches_models
"""

import pytest

from scripts.check_schema_matches_models import filter_allowlist, format_diffs


@pytest.mark.parametrize(
    "raw, expected",
    [
        # Numeric(10,2) vs NUMERIC(10,2) is semantic no-op drift the allowlist must drop.
        pytest.param(
            [("modify_type", None, "table_x", "col_y", {}, "NUMERIC(10, 2)", "Numeric(precision=10, scale=2)")],
            [],
            id="drops-numeric-precision-noise",
        ),
        # A genuinely added column must NOT be filtered.
        pytest.param(
            [("add_column", None, "table_x", "new_col")],
            [("add_column", None, "table_x", "new_col")],
            id="keeps-real-drift",
        ),
    ],
)
def test_filter_allowlist(raw, expected):
    assert filter_allowlist(raw) == expected


def test_unwraps_grouped_list_diffs():
    """compare_metadata wraps column-level diffs in a single-element list; the allowlist
    must unwrap to match a grandfathered modify_type entry."""
    # (oem_crosswalk, looked_up_at) is a grandfathered UTCDateTime reflection no-op.
    grandfathered = [[("modify_type", None, "oem_crosswalk", "looked_up_at", {}, "TIMESTAMP()", "UTCDateTime()")]]
    assert filter_allowlist(grandfathered) == []


def test_grandfather_is_name_scoped():
    """A modify_type on a NON-grandfathered (table, column) must still surface."""
    novel = [[("modify_type", None, "brand_new_table", "some_col", {}, "TIMESTAMP()", "UTCDateTime()")]]
    assert filter_allowlist(novel) == novel


def test_grandfathered_add_constraint_keyed_by_table_and_columns():
    """add_constraint is grandfathered by (table, sorted-columns); a new table fails."""

    class _Col:
        def __init__(self, name):
            self.name = name

    class _Table:
        def __init__(self, name):
            self.name = name

    class _UC:
        def __init__(self, table_name, col_names):
            self.table = _Table(table_name)
            self.columns = [_Col(c) for c in col_names]

    grandfathered = [("add_constraint", _UC("users", ["email"]))]
    novel = [("add_constraint", _UC("brand_new_table", ["email"]))]
    assert filter_allowlist(grandfathered) == []
    assert filter_allowlist(novel) == novel


def test_format_diffs_renders_human_readable():
    """Output must list the diff kind + table + column for each entry."""
    diffs = [
        ("add_column", None, "table_x", "new_col"),
        ("remove_table", None, "ghost_table"),
    ]
    out = format_diffs(diffs)
    assert "add_column" in out
    assert "table_x" in out
    assert "new_col" in out
    assert "remove_table" in out
    assert "ghost_table" in out
