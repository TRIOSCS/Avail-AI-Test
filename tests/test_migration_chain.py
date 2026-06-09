"""Guard: the Alembic revision graph must have exactly ONE head.

``alembic upgrade head`` fails at DEPLOY time (never in CI) with "Multiple head
revisions" the moment two branches each chain a new migration onto the same parent —
exactly the situation created by parallel-numbered migrations (e.g. 093 deliberately
skipping the 092 number reserved by a concurrent branch). This turns that deterministic
production deploy crash into a CI failure on whichever branch lands second; the fix is
``alembic merge heads -m "..."`` on the later branch. Same class of deploy-blocking,
test-invisible footgun as tests/test_migration_revision_ids.py.

Uses alembic's own ScriptDirectory so merge revisions (tuple down_revision), branch
labels, and duplicate-revision-id errors are all handled by the canonical graph walker
instead of a hand-rolled parser.

Called by: pytest. Depends on: the alembic/ script directory only (no DB).
"""

from __future__ import annotations

import pathlib

from alembic.script import ScriptDirectory

_ALEMBIC_DIR = pathlib.Path(__file__).resolve().parent.parent / "alembic"


def test_revision_graph_has_exactly_one_head():
    heads = ScriptDirectory(str(_ALEMBIC_DIR)).get_heads()
    assert len(heads) == 1, (
        f"Alembic revision graph has {len(heads)} heads: {sorted(heads)}. "
        "'alembic upgrade head' will fail at deploy time with 'Multiple head revisions'. "
        "Add a merge revision on this branch: alembic merge heads -m 'merge heads'"
    )
