"""scripts/check_schema_matches_models.py — Schema-equivalence check.

Connects to DATABASE_URL, reflects the live schema, runs
alembic.autogenerate.compare_metadata against app.models.Base.metadata,
filters known false positives, prints any remaining drift, exits non-zero
on drift.

Called by: .github/workflows/ci.yml (smoke test step) + local devs.
Depends on: app.models.Base, alembic, sqlalchemy.

Usage:
    DATABASE_URL=postgresql://... python scripts/check_schema_matches_models.py
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from typing import Iterable

from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine

from app.models import Base

# Each entry is a (diff_kind, predicate) tuple. The predicate gets the raw diff
# tuple and returns True if the entry is a known false positive that should be
# dropped from the result. Keep this list short; every entry needs a comment
# explaining the underlying alembic/sqlalchemy quirk.
_ALLOWLIST: list[tuple[str, Callable[..., bool]]] = [
    # Numeric(10, 2) reflected as NUMERIC(10, 2) — same type, different rendering.
    # alembic.autogenerate sometimes flags this as modify_type. The check uses
    # str() on both sides so it works whether the values are SQLAlchemy type
    # objects (real alembic output) or their string representations (tests).
    (
        "modify_type",
        lambda d: len(d) >= 7 and "NUMERIC" in str(d[5]).upper() and "numeric" in str(d[6]).lower(),
    ),
]


def filter_allowlist(diffs: Iterable[tuple]) -> list[tuple]:
    """Drop diff entries that match a documented false-positive pattern."""
    out: list[tuple] = []
    for d in diffs:
        kind = d[0] if d else None
        skip = False
        for allow_kind, predicate in _ALLOWLIST:
            if kind == allow_kind and predicate(d):
                skip = True
                break
        if not skip:
            out.append(d)
    return out


def format_diffs(diffs: Iterable[tuple]) -> str:
    """Human-readable rendering of remaining diffs, one per line."""
    lines = []
    for d in diffs:
        lines.append("  " + " | ".join(repr(part) for part in d))
    return "\n".join(lines) if lines else "(no diffs)"


def main() -> int:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL not set", file=sys.stderr)
        return 2
    engine = create_engine(db_url)
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn)
        raw_diffs = list(compare_metadata(ctx, Base.metadata))
    filtered = filter_allowlist(raw_diffs)
    if filtered:
        print("Schema drift detected vs app.models.Base.metadata:")
        print(format_diffs(filtered))
        print(f"\n{len(filtered)} drift entr{'y' if len(filtered) == 1 else 'ies'}.")
        return 1
    print(f"Schema matches Base.metadata. ({len(raw_diffs)} raw diff(s), all in allowlist.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
