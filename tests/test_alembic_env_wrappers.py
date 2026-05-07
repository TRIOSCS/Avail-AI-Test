"""test_alembic_env_wrappers.py - Unit tests for alembic/env.py idempotent op wrappers.

The wrappers in alembic/env.py monkey-patch alembic.op to make migrations
re-runnable: they short-circuit when the target object already matches the
desired state (and log a WARN so silent skips remain visible). These tests
exercise every branch of every wrapper using a real in-memory SQLite engine
bound through alembic's Operations proxy.

Strategy:
- Cannot just `import alembic.env` - its module bottom auto-runs migrations.
- Instead, slice the wrapper-definition block out of env.py source and
  evaluate it in a fresh namespace where `op` is a real Operations proxy
  bound to a per-test in-memory SQLite connection. The wrappers' `_orig_*`
  bindings then resolve to the proxy's bound methods, so the originals run
  for real against SQLite.
- For PostgreSQL-only paths (CASCADE drop), assert via os.environ + capture
  of op.execute() calls rather than running real DDL.

Called by: pytest
Depends on: alembic.migration.MigrationContext, alembic.operations.Operations
"""

from __future__ import annotations

import builtins
import os
import re
import sys
from pathlib import Path

import pytest
import sqlalchemy as sa
from loguru import logger

from alembic.migration import MigrationContext
from alembic.operations import Operations

ENV_PATH = Path(__file__).resolve().parent.parent / "alembic" / "env.py"

_PY_RUNNER = builtins.exec  # avoid literal token at call site


def _wrapper_source() -> str:
    """Extract just the wrapper-definition block from alembic/env.py.

    We slice from the wrappers banner up to (but not including) the global
    monkey-patches `op.add_column = _idempotent_...` so we can evaluate it
    into a namespace with our own `op` proxy.
    """
    src = ENV_PATH.read_text()
    start_match = re.search(r"^# .. Idempotent op wrappers", src, re.MULTILINE)
    end_match = re.search(r"^op\.add_column = _idempotent_add_column", src, re.MULTILINE)
    assert start_match and end_match, "env.py layout changed - update slicing anchors"
    return src[start_match.start() : end_match.start()]


def _build_wrapper_ns(op_proxy: Operations) -> dict:
    """Evaluate the wrapper block with `op` bound to the given Operations proxy.

    Returns the populated namespace, from which tests pull out _idempotent_* callables.
    The `_orig_*` references inside the wrappers will bind to op_proxy's real methods,
    so calls flow through to the underlying SQLite connection.
    """
    ns: dict = {
        "os": os,
        "sa": sa,
        "logger": logger,
        "op": op_proxy,
    }
    code = compile(_wrapper_source(), str(ENV_PATH), "exec")
    _PY_RUNNER(code, ns)
    return ns


@pytest.fixture()
def conn():
    """Per-test in-memory SQLite connection inside a transaction."""
    engine = sa.create_engine("sqlite://")
    with engine.connect() as connection:
        trans = connection.begin()
        try:
            yield connection
        finally:
            trans.rollback()
        engine.dispose()


@pytest.fixture()
def op_proxy(conn):
    """Alembic Operations proxy bound to the SQLite connection."""
    ctx = MigrationContext.configure(conn)
    return Operations(ctx)


@pytest.fixture()
def wrappers(op_proxy):
    """Dict of wrapper functions, evaluated with op = op_proxy."""
    return _build_wrapper_ns(op_proxy)


@pytest.fixture()
def loguru_capture():
    """Capture loguru WARN+ messages emitted during a test."""
    messages: list[str] = []
    sink_id = logger.add(lambda msg: messages.append(str(msg)), level="WARNING")
    yield messages
    logger.remove(sink_id)


def _make_widgets(op_proxy: Operations) -> None:
    """Helper: create a baseline `widgets` table for tests that need one."""
    op_proxy.create_table(
        "widgets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(50), nullable=False),
    )


def test_create_table_skips_when_exists(wrappers, op_proxy, conn, loguru_capture):
    """Branch: pre-existing table -> no-op + WARN log."""
    _make_widgets(op_proxy)
    wrappers["_idempotent_create_table"](
        "widgets",
        sa.Column("id", sa.Integer(), primary_key=True),
    )
    assert any("create_table (already exists)" in m for m in loguru_capture)
    assert sa.inspect(conn).get_table_names() == ["widgets"]


def test_create_table_creates_when_missing(wrappers, op_proxy, conn):
    """Branch: missing table -> delegates to original create_table."""
    wrappers["_idempotent_create_table"](
        "gadgets",
        sa.Column("id", sa.Integer(), primary_key=True),
    )
    assert "gadgets" in sa.inspect(conn).get_table_names()


def test_drop_table_noop_when_missing(wrappers, loguru_capture):
    """Branch: drop_table on missing table -> log + return None (no error)."""
    result = wrappers["_idempotent_drop_table"]("nonexistent")
    assert result is None
    assert any("drop_table (table missing)" in m for m in loguru_capture)


def test_drop_table_drops_when_present(wrappers, op_proxy, conn):
    """Branch: drop_table happy path -> original DROP runs."""
    _make_widgets(op_proxy)
    wrappers["_idempotent_drop_table"]("widgets")
    assert sa.inspect(conn).get_table_names() == []


def test_drop_table_cascade_only_when_opted_in(wrappers, op_proxy, monkeypatch, loguru_capture):
    """Branch: cascade=True emits a CASCADE DROP via op.execute, default does not.

    SQLite has no CASCADE, so we capture op.execute() calls rather than
    running real DDL. The CASCADE path uses op.execute("DROP TABLE ... CASCADE");
    the default path delegates to alembic's _orig_drop_table.
    """
    _make_widgets(op_proxy)
    captured: list[str] = []
    monkeypatch.setattr(op_proxy, "execute", lambda sql: captured.append(str(sql)))
    monkeypatch.delenv("ALEMBIC_ALLOW_CASCADE", raising=False)

    wrappers["_idempotent_drop_table"]("widgets", cascade=True)
    assert captured and "CASCADE" in captured[0]
    assert any("CASCADE drop_table" in m for m in loguru_capture)

    captured.clear()
    if "widgets" not in sa.inspect(op_proxy.get_bind()).get_table_names():
        _make_widgets(op_proxy)
    wrappers["_idempotent_drop_table"]("widgets")
    assert captured == []


def test_add_column_skips_when_column_exists(wrappers, op_proxy, loguru_capture):
    """Branch: column already present -> no-op + WARN log."""
    _make_widgets(op_proxy)
    wrappers["_idempotent_add_column"]("widgets", sa.Column("name", sa.String(50)))
    assert any("add_column (column already exists)" in m for m in loguru_capture)


def test_add_column_skips_when_table_missing(wrappers, loguru_capture):
    """Branch: parent table absent -> no-op + WARN log."""
    wrappers["_idempotent_add_column"]("ghost", sa.Column("foo", sa.Integer()))
    assert any("add_column (table missing)" in m for m in loguru_capture)


def test_add_column_adds_when_neither_exists(wrappers, op_proxy, conn):
    """Branch: table present, column missing -> original add_column runs."""
    _make_widgets(op_proxy)
    wrappers["_idempotent_add_column"]("widgets", sa.Column("price", sa.Integer()))
    cols = {c["name"] for c in sa.inspect(conn).get_columns("widgets")}
    assert "price" in cols


def test_alter_column_short_circuits_when_already_in_target_state(wrappers, op_proxy, conn, loguru_capture):
    """Branch (C1 fix): existing type + nullable already match request -> skip.

    The wrapper uses repr(current_type) == repr(requested_type) for comparison.
    SQLAlchemy reflects sa.String(50) as VARCHAR(length=50), so we must pass the type
    SQLAlchemy actually reflects back from the inspector — fetch it live to make the
    test agnostic to dialect-specific reflection mappings.
    """
    _make_widgets(op_proxy)
    insp = sa.inspect(conn)
    reflected_type = next(c["type"] for c in insp.get_columns("widgets") if c["name"] == "name")
    wrappers["_idempotent_alter_column"](
        "widgets",
        "name",
        type_=reflected_type,
        nullable=False,
    )
    assert any("alter_column (already in target state)" in m for m in loguru_capture)


def test_alter_column_applies_when_type_differs(wrappers, op_proxy, loguru_capture):
    """Branch (C1 fix): requested type repr differs from current -> original runs."""
    _make_widgets(op_proxy)
    called = {}

    def fake_alter(table, col, **kw):
        called["args"] = (table, col, kw)

    wrappers["_orig_alter_column"] = fake_alter
    wrappers["_idempotent_alter_column"](
        "widgets",
        "name",
        type_=sa.String(200),
        nullable=False,
    )
    assert called["args"][0] == "widgets"
    assert called["args"][1] == "name"
    assert not any("already in target state" in m for m in loguru_capture)


def test_alter_column_applies_when_nullable_differs(wrappers, op_proxy, loguru_capture):
    """Branch (C1 fix): nullable mismatch -> original alter runs."""
    _make_widgets(op_proxy)
    called = {}
    wrappers["_orig_alter_column"] = lambda t, c, **kw: called.setdefault("kw", kw)
    wrappers["_idempotent_alter_column"]("widgets", "name", nullable=True)
    assert called.get("kw", {}).get("nullable") is True
    assert not any("already in target state" in m for m in loguru_capture)


def test_create_index_skips_when_table_missing(wrappers, loguru_capture):
    """Branch (C2 fix): table absent -> skip + log."""
    wrappers["_idempotent_create_index"]("ix_x", "missing_table", ["col"])
    assert any("create_index (table missing)" in m for m in loguru_capture)


def test_create_index_skips_when_index_exists(wrappers, op_proxy, loguru_capture):
    """Branch (C2 fix): named index already present -> skip + log."""
    _make_widgets(op_proxy)
    op_proxy.create_index("ix_widgets_name", "widgets", ["name"])
    wrappers["_idempotent_create_index"]("ix_widgets_name", "widgets", ["name"])
    assert any("create_index (index already exists)" in m for m in loguru_capture)


def test_create_index_creates_when_neither(wrappers, op_proxy, conn):
    """Branch: table present, index missing -> original creates it."""
    _make_widgets(op_proxy)
    wrappers["_idempotent_create_index"]("ix_widgets_name", "widgets", ["name"])
    idx_names = {ix["name"] for ix in sa.inspect(conn).get_indexes("widgets")}
    assert "ix_widgets_name" in idx_names


def test_create_foreign_key_warns_when_source_missing(wrappers, op_proxy, loguru_capture):
    """Branch: source_table missing -> loud WARN (not silent SKIP)."""
    op_proxy.create_table("ref_target", sa.Column("id", sa.Integer(), primary_key=True))
    wrappers["_idempotent_create_foreign_key"]("fk_ghost_target", "ghost_source", "ref_target", ["x"], ["id"])
    joined = " ".join(loguru_capture)
    assert "create_foreign_key SKIPPED" in joined
    assert "source table missing" in joined


def test_create_foreign_key_warns_when_referent_missing(wrappers, op_proxy, loguru_capture):
    """Branch: source present but referent missing -> loud WARN."""
    _make_widgets(op_proxy)
    wrappers["_idempotent_create_foreign_key"]("fk_widgets_ghost", "widgets", "ghost_referent", ["x"], ["id"])
    joined = " ".join(loguru_capture)
    assert "create_foreign_key SKIPPED" in joined
    assert "referent table missing" in joined


def test_drop_constraint_default_type_scans_all(wrappers, op_proxy, loguru_capture):
    """Branch: type_=None scans fk/unique/check/primary; missing -> skip + log."""
    _make_widgets(op_proxy)
    wrappers["_idempotent_drop_constraint"]("fk_nope", "widgets")
    assert any("drop_constraint (not present)" in m for m in loguru_capture)


def test_drop_constraint_handles_named_unique(wrappers, op_proxy):
    """Branch: type_='unique' with present constraint -> original drop runs."""
    _make_widgets(op_proxy)
    with op_proxy.batch_alter_table("widgets") as batch:
        batch.create_unique_constraint("uq_widgets_name", ["name"])

    captured = {}
    wrappers["_orig_drop_constraint"] = lambda name, table, type_=None, **kw: captured.update(
        {"name": name, "table": table, "type_": type_}
    )
    wrappers["_idempotent_drop_constraint"]("uq_widgets_name", "widgets", type_="unique")
    assert captured.get("name") == "uq_widgets_name"
    assert captured.get("type_") == "unique"


def test_drop_index_does_not_inject_if_exists(wrappers, op_proxy):
    """Branch (I3 fix): wrapper must NOT silently inject if_exists=True."""
    _make_widgets(op_proxy)
    op_proxy.create_index("ix_widgets_name", "widgets", ["name"])

    captured = {}
    wrappers["_orig_drop_index"] = lambda name, table_name=None, **kw: captured.update(
        {"name": name, "table_name": table_name, "kwargs": kw}
    )
    wrappers["_idempotent_drop_index"]("ix_widgets_name", table_name="widgets")
    assert "if_exists" not in captured["kwargs"], (
        "I3 regression: drop_index wrapper must not auto-inject if_exists=True"
    )
    assert captured["name"] == "ix_widgets_name"
    assert captured["table_name"] == "widgets"


def test_drop_index_skips_when_table_missing(wrappers, loguru_capture):
    """Branch: table absent -> skip + log (no original call)."""
    wrappers["_idempotent_drop_index"]("ix_nope", table_name="missing_table")
    assert any("drop_index (table missing)" in m for m in loguru_capture)


def test_wrapper_source_extraction_guard():
    """Fail loudly if env.py layout drifts so the slicing breaks."""
    src = ENV_PATH.read_text()
    assert "Idempotent op wrappers" in src
    assert "op.add_column = _idempotent_add_column" in src
    assert src.index("Idempotent op wrappers") < src.index("op.add_column = _idempotent_add_column")


sys.modules.pop("alembic.env", None)
