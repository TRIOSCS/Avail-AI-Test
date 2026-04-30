"""tests/scripts/test_check_schema_matches_models.py — Tests for the schema diff
comparator used in CI and local smoke.

Called by: pytest
Depends on: scripts.check_schema_matches_models
"""

from scripts.check_schema_matches_models import filter_allowlist, format_diffs


def test_filter_allowlist_drops_numeric_precision_noise():
    """alembic.compare_metadata sometimes reports Numeric(10,2) vs NUMERIC(10,2) drift
    that is semantically a no-op.

    Allowlist must drop these.
    """
    raw = [
        ("modify_type", None, "table_x", "col_y", {}, "NUMERIC(10, 2)", "Numeric(precision=10, scale=2)"),
    ]
    assert filter_allowlist(raw) == []


def test_filter_allowlist_keeps_real_drift():
    """A genuinely added column must NOT be filtered."""
    raw = [
        ("add_column", None, "table_x", "new_col"),
    ]
    assert filter_allowlist(raw) == raw


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
