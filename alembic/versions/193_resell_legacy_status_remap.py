"""Remap the legacy pre-Resell ExcessList statuses onto the canonical lifecycle.

What: One-off DATA-ONLY migration (no DDL) that retires the two pre-Resell
      ``excess_lists.status`` members the Resell reshape left in place for backward-compat
      (``constants.ExcessListStatus`` still lists ACTIVE / BIDDING as "legacy, retired in
      the cutover chunk"). Order-coupled with the publish guard shipping in the same PR:
      once no row can sit in a legacy status, ``publish_list`` can safely reject any
      non-DRAFT publish.

      Three case-insensitive UPDATEs (``LOWER(TRIM(status))``, mirroring 189):
        (a) ``active``  -> ``open``       + stamp ``open_at`` where NULL (a posted window
            needs a start; an already-set ``open_at`` is preserved via COALESCE);
        (b) ``bidding`` -> ``collecting`` + stamp ``open_at`` where NULL (a collecting
            window is past-open, so it too must carry a posting-window start);
        (c) ``closed``  -> ``closed``     — casing normalize ONLY. Decision D5: legacy
            ``closed`` stays CLOSED, DISTINCT from ``bid_out`` — a manually-closed
            posting is not the same lifecycle state as one whose bid went out.

      The ``excess_lists.status`` column is a plain ``String(20)`` with NO check
      constraint, so this is a pure data UPDATE — no column/constraint change is needed.
      The SQL is factored into ``remap_legacy_statuses(connection)`` so
      tests/test_resell_legacy_status_remap.py drives the EXACT same code.

Downgrade: documented NO-OP — the remap is many-to-one and lossy (the original
``active``/``bidding`` strings are unrecoverable, and any ``open_at`` this migration
stamped cannot be distinguished from a genuine one), so the original state cannot be
restored by design (same contract as 093/100/189). The canonical statuses remain valid on
older code, so leaving them in place is safe.

Called by: alembic (upgrade/downgrade); remap_legacy_statuses also driven directly by
    tests/test_resell_legacy_status_remap.py.
Depends on: excess_lists table (status/open_at columns).

Revision ID: 193_resell_legacy_status_remap
Revises: 191_companies_account_type_index
Create Date: 2026-07-17
"""

from __future__ import annotations

from datetime import UTC, datetime

from loguru import logger
from sqlalchemy import text
from sqlalchemy.engine import Connection

from alembic import op

revision = "193_resell_legacy_status_remap"
down_revision = "191_companies_account_type_index"
branch_labels = None
depends_on = None


def remap_legacy_statuses(conn: Connection) -> dict[str, int]:
    """Remap legacy ExcessList statuses onto the canonical lifecycle (see module
    docstring).

    Runs the three case-insensitive UPDATEs and returns the per-pass rowcounts. Shared by
    ``upgrade()`` and the test so both exercise identical SQL. ``open_at`` is stamped with a
    Python-side ``datetime`` bound param (portable across PostgreSQL and the SQLite test
    dialect — no reliance on a DB ``now()``).
    """
    now = datetime.now(UTC)

    opened = (
        conn.execute(
            text(
                "UPDATE excess_lists SET status = 'open', open_at = COALESCE(open_at, :now) "
                "WHERE LOWER(TRIM(status)) = 'active'"
            ),
            {"now": now},
        ).rowcount
        or 0
    )
    collecting = (
        conn.execute(
            text(
                "UPDATE excess_lists SET status = 'collecting', open_at = COALESCE(open_at, :now) "
                "WHERE LOWER(TRIM(status)) = 'bidding'"
            ),
            {"now": now},
        ).rowcount
        or 0
    )
    # D5: keep CLOSED distinct from bid_out — normalize casing only, never remap the state.
    closed = (
        conn.execute(
            text(
                "UPDATE excess_lists SET status = 'closed' WHERE LOWER(TRIM(status)) = 'closed' AND status != 'closed'"
            )
        ).rowcount
        or 0
    )

    logger.info(
        "193: remapped {} active->open + {} bidding->collecting excess_lists; normalized {} closed-casing",
        opened,
        collecting,
        closed,
    )
    return {"opened": opened, "collecting": collecting, "closed": closed}


def upgrade() -> None:
    remap_legacy_statuses(op.get_bind())


def downgrade() -> None:
    # Intentionally a NO-OP: the remap is many-to-one and lossy (original active/bidding
    # strings and which open_at values this migration stamped are unrecoverable by design;
    # same contract as 093/100/189). Canonical statuses remain valid on older code.
    logger.info("193: downgrade is a documented no-op (legacy-status remap is irreversible)")
