"""tests/scripts/test_check_schema_matches_models.py — Tests for the schema diff
comparator used in CI and local smoke.

Called by: pytest
Depends on: scripts.check_schema_matches_models
"""

from types import SimpleNamespace

import pytest

from scripts.check_schema_matches_models import (
    _diff_signature,
    filter_allowlist,
    format_diffs,
)


# --- helpers: minimal stand-ins matching the shape _diff_signature reads from real
#     alembic objects (.name / .table.name / .columns / the type's class name). ---
def _table_diff(kind, name):
    return (kind, SimpleNamespace(name=name))


def _index_diff(kind, table, name):
    return (kind, SimpleNamespace(name=name, table=SimpleNamespace(name=table)))


class UniqueConstraint:  # noqa: D401 — name matters: _diff_signature keys on type(con).__name__
    def __init__(self, table, cols):
        self.name = None
        self.table = SimpleNamespace(name=table)
        self.columns = [SimpleNamespace(name=c) for c in cols]


def _uq_diff(kind, table, cols):
    return (kind, UniqueConstraint(table, cols))


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


# --- Grandfathering: documented pre-existing drift is dropped, but only by exact name. ---


@pytest.mark.parametrize(
    "diff",
    [
        pytest.param(_table_diff("remove_table", "buy_plans"), id="grandfathered-table"),
        pytest.param(
            _index_diff("remove_index", "companies", "ix_companies_domain_trgm"), id="grandfathered-trgm-index"
        ),
        pytest.param(_uq_diff("add_constraint", "users", ["email"]), id="grandfathered-unique-constraint"),
        pytest.param(
            ("remove_column", None, "activity_log", SimpleNamespace(name="source_url")), id="grandfathered-dead-column"
        ),
    ],
)
def test_grandfathered_drift_is_dropped(diff):
    assert filter_allowlist([diff]) == []


@pytest.mark.parametrize(
    "diff",
    [
        # Same KIND as a grandfathered entry but a name that is NOT grandfathered must fail.
        pytest.param(_table_diff("remove_table", "some_brand_new_table"), id="new-table-still-fails"),
        pytest.param(_index_diff("remove_index", "companies", "ix_companies_brand_new"), id="new-index-still-fails"),
        pytest.param(_uq_diff("add_constraint", "users", ["phone_number"]), id="new-constraint-still-fails"),
        pytest.param(
            ("remove_column", None, "activity_log", SimpleNamespace(name="brand_new_col")), id="new-column-still-fails"
        ),
    ],
)
def test_new_drift_of_ungrandfathered_name_still_fails(diff):
    assert filter_allowlist([diff]) == [diff]


def test_unwraps_list_form_and_drops_utcdatetime_modify_type():
    """alembic yields column modify_* diffs as a list of tuples. The UTCDateTime/TIMESTAMP
    modify_type is a false positive that must be dropped even inside that list wrapper
    (regression for the dead-code allowlist predicate that never fired on list-form diffs).
    """
    raw = [[("modify_type", None, "fru_links", "created_at", {}, "TIMESTAMP()", "UTCDateTime()")]]
    assert filter_allowlist(raw) == []


def test_list_form_keeps_real_modification_drops_grandfathered():
    """A list-form diff with a grandfathered modify_comment AND a real modify_nullable keeps
    only the real one — the unwrap must be entry-by-entry, not all-or-nothing."""
    real = ("modify_nullable", None, "material_cards", "enrichment_status", {}, True, False)
    raw = [
        [
            ("modify_comment", None, "material_cards", "enrichment_status", {}, "x", None),
            real,
        ]
    ]
    assert filter_allowlist(raw) == [[real]]


def test_diff_signature_ignores_object_reprs():
    """Signatures must key on stable names, not repr() (which embeds memory addresses for
    functional indexes and would differ run-to-run)."""
    sig = _diff_signature(_index_diff("remove_index", "material_cards", "ix_mc_order_live"))
    assert sig == ("remove_index", "material_cards", "ix_mc_order_live")


def test_notification_model_is_registered_on_metadata():
    """Root-cause guard: the Notification model must be imported so `notifications` is on
    Base.metadata — otherwise the gate flags it as an unmodelled table (the original failure)."""
    from app.models import Base, Notification  # noqa: F401

    assert "notifications" in Base.metadata.tables
