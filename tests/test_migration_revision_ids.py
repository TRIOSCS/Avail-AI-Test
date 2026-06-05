"""Guard: every Alembic revision id must fit ``alembic_version.version_num`` (VARCHAR(32))
on PostgreSQL.

A longer revision id passes the SQLite test DB (SQLite does not enforce VARCHAR length) but
fails the final ``UPDATE alembic_version SET version_num=...`` on PostgreSQL with
``StringDataRightTruncation`` — a deploy-blocking, SQLite-masked footgun (this guard was
added after migration 089 hit exactly that at deploy time). Pure string check over the
migration files, so it runs in CI on every change regardless of DB backend.

Called by: pytest. Depends on: the alembic/versions/ directory only.
"""

from __future__ import annotations

import pathlib
import re

_VERSIONS = pathlib.Path(__file__).resolve().parent.parent / "alembic" / "versions"
_MAX = 32  # Alembic's default alembic_version.version_num length on PostgreSQL.

# No grandfathered ids: every revision id must be <= 32 chars. (The previously-grandfathered
# 40-char 016 id was renamed to 016_sightings_vendor_normalized.) Keep this set empty — fix
# the offending id instead of adding an exception.
_GRANDFATHERED: set[str] = set()

_REV = re.compile(r"""^revision\s*=\s*["']([^"']+)["']""", re.MULTILINE)


def _revision_ids():
    for f in sorted(_VERSIONS.glob("*.py")):
        match = _REV.search(f.read_text())
        if match:
            yield f.name, match.group(1)


def test_all_revision_ids_fit_alembic_version_column():
    too_long = [
        (fname, rid, len(rid)) for fname, rid in _revision_ids() if len(rid) > _MAX and rid not in _GRANDFATHERED
    ]
    assert not too_long, (
        "Alembic revision id(s) exceed VARCHAR(32) — these fail the version-write on "
        f"PostgreSQL (but pass SQLite): {too_long}"
    )
