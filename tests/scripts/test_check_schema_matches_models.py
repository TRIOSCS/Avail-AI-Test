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


def test_reconciled_add_constraints_now_surface():
    """#464 finish (migration 174) created every formerly-grandfathered unique
    constraint, so an ``add_constraint`` diff — even for a previously-listed key like
    (users, email) — must now SURFACE as real drift."""

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

    formerly_grandfathered = [("add_constraint", _UC("users", ["email"]))]
    novel = [("add_constraint", _UC("brand_new_table", ["email"]))]
    assert filter_allowlist(formerly_grandfathered) == formerly_grandfathered
    assert filter_allowlist(novel) == novel


def test_reconciled_indexes_surface_but_intentional_ones_stay_grandfathered():
    """#464 (migration 172) declared the raw-DDL pg_trgm/GIN/btree/partial indexes on
    the models, so a ``remove_index`` for a reconciled name must now SURFACE as real
    drift, while DANGER/orphan-table and PG-only expression indexes stay
    grandfathered."""

    class _Ix:
        def __init__(self, name):
            self.name = name

    reconciled = [("remove_index", _Ix("ix_companies_name_trgm"))]
    # ix_buyplans_token left with its table (dropped by migration 174, #464 finish),
    # so its remove_index must now surface too.
    dropped_with_table = [("remove_index", _Ix("ix_buyplans_token"))]
    orphan_table_index = [("remove_index", _Ix("ix_ecu_provider_month"))]
    pg_expression = [("remove_index", _Ix("ix_vendor_cards_domain_lower"))]

    # Reconciled (now model-declared) → no longer filtered → surfaces as drift.
    assert filter_allowlist(reconciled) == reconciled
    assert filter_allowlist(dropped_with_table) == dropped_with_table
    # Still intentional: enrichment_credit_usage's index + PG-only expression DDL.
    assert filter_allowlist(orphan_table_index) == []
    assert filter_allowlist(pg_expression) == []


def test_reports_to_phantom_add_index_no_longer_grandfathered():
    """The site_contacts reports_to index was renamed to the DB's ix_sc_reports_to, so
    the phantom ``add_index`` for ix_site_contacts_reports_to_id must now surface."""

    class _Ix:
        def __init__(self, name):
            self.name = name

    phantom = [("add_index", _Ix("ix_site_contacts_reports_to_id"))]
    assert filter_allowlist(phantom) == phantom


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
