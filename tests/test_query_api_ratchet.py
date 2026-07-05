"""tests/test_query_api_ratchet.py — DOWN-only ratchet on the legacy Query API.

Guards against NEW legacy SQLAlchemy 1.x ``<session>.query(`` introductions
without touching the ~1,562 existing call sites. Recomputes the count with the
SAME function that produced the committed baseline
(``scripts.query_api_baseline.count_legacy_query_calls``) so existing sites
always pass (current == baseline). A single new ``db.query(`` anywhere in
``app/`` makes current > baseline and fails.

Called by: pytest / CI (and mirrored by the ``query-api-ratchet`` pre-commit
local hook). Depends on: scripts.query_api_baseline.
"""

from __future__ import annotations

from scripts.query_api_baseline import (
    BASELINE_PATH,
    count_legacy_query_calls,
    load_baseline,
)


def test_baseline_file_is_well_formed() -> None:
    baseline = load_baseline()
    assert "total" in baseline, f"{BASELINE_PATH} must contain a 'total' key"
    assert isinstance(baseline["total"], int)
    assert baseline["total"] >= 0


def test_legacy_query_api_does_not_grow() -> None:
    baseline = load_baseline()["total"]
    current = count_legacy_query_calls()

    assert current <= baseline, (
        f"Legacy Query API count rose from {baseline} to {current} — new code "
        f"must use SQLAlchemy 2.0 style (`db.get()`, `db.scalars(select(...))`, "
        f"`db.execute(select(...))`). If you intentionally REMOVED sites, lower "
        f"tests/query_api_baseline.json to match (run: "
        f"`python -m scripts.query_api_baseline --write`)."
    )
