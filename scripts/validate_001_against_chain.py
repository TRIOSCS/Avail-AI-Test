"""scripts/validate_001_against_chain.py — Validate that the proposed 001 baseline
provides everything later migrations expect.

Walks every migration file in alembic/versions/ in revision order (resolved via
alembic.script.ScriptDirectory). For each, simulates op.create_table /
op.add_column / op.alter_column / op.drop_table / op.rename_table /
op.drop_column against an in-memory SchemaModel seeded from the proposed 001.
Any op that targets a table or column not present in the model is reported
as a Gap.

Called by: developer (during reconstruction iteration). Not invoked from CI.
Depends on: alembic, ast (stdlib), pathlib (stdlib).

Usage:
    python scripts/validate_001_against_chain.py                          # validate current 001
    python scripts/validate_001_against_chain.py --baseline path/to.draft # validate a draft

Exit:
    0  if no gaps found
    1  if gaps found (writes report to stdout)
    2  on usage error
"""

from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

REPO_ROOT = Path(__file__).resolve().parent.parent


# ── Schema model ───────────────────────────────────────────────────────


class SchemaModel:
    """Minimal in-memory schema state.

    Tracks tables and their column sets.
    """

    def __init__(self) -> None:
        self._tables: dict[str, set[str]] = {}

    def add_table(self, name: str, columns: list[str]) -> None:
        self._tables[name] = set(columns)

    def drop_table(self, name: str) -> None:
        self._tables.pop(name, None)

    def has_table(self, name: str) -> bool:
        return name in self._tables

    def add_column(self, table: str, column: str) -> None:
        self._tables.setdefault(table, set()).add(column)

    def drop_column(self, table: str, column: str) -> None:
        self._tables.get(table, set()).discard(column)

    def has_column(self, table: str, column: str) -> bool:
        return column in self._tables.get(table, set())

    def rename_table(self, old: str, new: str) -> None:
        if old in self._tables:
            self._tables[new] = self._tables.pop(old)


# ── Gap report ─────────────────────────────────────────────────────────


@dataclass
class Gap:
    migration: str
    op_name: str
    target: str
    description: str


# ── AST-based op walker ────────────────────────────────────────────────

_SCHEMA_OPS_NEEDING_TABLE = {
    "drop_table",
    "rename_table",
    "add_column",
    "drop_column",
    "alter_column",
    "create_index",
    "drop_index",
    "create_foreign_key",
    "drop_constraint",
    "create_unique_constraint",
    "create_check_constraint",
}
_DATA_OPS = {"execute", "bulk_insert", "bulk_update"}


def _string_arg(node: ast.Call, idx: int) -> str | None:
    """Return the str value of a positional arg if it's a literal, else None."""
    if idx < len(node.args) and isinstance(node.args[idx], ast.Constant) and isinstance(node.args[idx].value, str):
        return node.args[idx].value
    return None


def _kw_str(node: ast.Call, name: str) -> str | None:
    for kw in node.keywords:
        if kw.arg == name and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
            return kw.value.value
    return None


def _column_names_from_create_table(node: ast.Call) -> list[str]:
    """Pull column names out of op.create_table('name', sa.Column('a',...), ...)."""
    cols: list[str] = []
    for arg in node.args[1:]:
        if isinstance(arg, ast.Call) and getattr(arg.func, "attr", None) == "Column":
            n = _string_arg(arg, 0)
            if n:
                cols.append(n)
    return cols


_TABLE_AT_ARG_0 = {"drop_table", "rename_table", "add_column", "drop_column", "alter_column"}
_TABLE_AT_ARG_1 = {
    "create_index",
    "create_foreign_key",
    "create_unique_constraint",
    "create_check_constraint",
}
_TABLE_AT_KWARG = {"drop_index", "drop_constraint"}


def _resolve_table(node: ast.Call, op_name: str) -> str | None:
    """Locate the table name for an op call based on alembic's positional convention."""
    if op_name in _TABLE_AT_ARG_0:
        return _string_arg(node, 0) or _kw_str(node, "table_name")
    if op_name in _TABLE_AT_ARG_1:
        return (
            _string_arg(node, 1)
            or _kw_str(node, "table_name")
            or _kw_str(node, "source_table")
        )
    if op_name in _TABLE_AT_KWARG:
        return _kw_str(node, "table_name") or _string_arg(node, 1)
    return None


def walk_migration_ops(model: SchemaModel, migration_paths: list[Path]) -> list[Gap]:
    gaps: list[Gap] = []
    for path in migration_paths:
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Attribute):
                continue
            if not (isinstance(node.func.value, ast.Name) and node.func.value.id == "op"):
                continue
            op_name = node.func.attr
            if op_name in _DATA_OPS:
                continue
            if op_name == "create_table":
                table = _string_arg(node, 0)
                if table:
                    model.add_table(table, _column_names_from_create_table(node))
                continue
            if op_name not in _SCHEMA_OPS_NEEDING_TABLE:
                continue
            table = _resolve_table(node, op_name)
            if not table:
                continue
            if op_name == "rename_table":
                new_name = _string_arg(node, 1) or _kw_str(node, "new_table_name")
                if not model.has_table(table):
                    gaps.append(Gap(path.name, op_name, table, f"rename source table {table!r} not in model"))
                else:
                    model.rename_table(table, new_name or table)
                continue
            if not model.has_table(table):
                gaps.append(Gap(path.name, op_name, table, f"target table {table!r} not in model"))
                continue
            if op_name == "drop_table":
                model.drop_table(table)
            elif op_name == "add_column":
                col_node = node.args[1] if len(node.args) >= 2 else None
                col_name = _string_arg(col_node, 0) if isinstance(col_node, ast.Call) else None
                if col_name:
                    model.add_column(table, col_name)
            elif op_name == "drop_column":
                col = _string_arg(node, 1)
                if col and not model.has_column(table, col):
                    gaps.append(Gap(path.name, op_name, f"{table}.{col}", f"column {col!r} not in {table!r}"))
                elif col:
                    model.drop_column(table, col)
            elif op_name == "alter_column":
                col = _string_arg(node, 1)
                if col and not model.has_column(table, col):
                    gaps.append(Gap(path.name, op_name, f"{table}.{col}", f"column {col!r} not in {table!r}"))
            # create_index / drop_index / create_foreign_key etc. only checked for table existence
    return gaps


# ── Seed model from a baseline file ────────────────────────────────────


def seed_from_baseline(baseline_path: Path) -> SchemaModel:
    """Parse the proposed 001 file and seed a SchemaModel from its create_table
    calls."""
    model = SchemaModel()
    tree = ast.parse(baseline_path.read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not (isinstance(node.func, ast.Attribute) and node.func.attr == "create_table"):
            continue
        table = _string_arg(node, 0)
        if not table:
            continue
        model.add_table(table, _column_names_from_create_table(node))
    return model


# ── Migration ordering via alembic ─────────────────────────────────────


def list_migrations_after_001() -> list[Path]:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    scripts = ScriptDirectory.from_config(cfg)
    revs = list(scripts.walk_revisions())
    revs.reverse()  # walk_revisions yields newest-first; reverse for forward order
    paths: list[Path] = []
    for rev in revs:
        if rev.revision == "001_initial":
            continue
        paths.append(Path(rev.path))
    return paths


# ── Entrypoint ─────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--baseline",
        type=Path,
        default=REPO_ROOT / "alembic" / "versions" / "001_initial_schema.py",
        help="Baseline file to seed from (default: live 001)",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        help="Optional path to write the report (default: stdout only)",
    )
    args = parser.parse_args()
    if not args.baseline.exists():
        print(f"ERROR: baseline file not found: {args.baseline}", file=sys.stderr)
        return 2
    model = seed_from_baseline(args.baseline)
    migrations = list_migrations_after_001()
    gaps = walk_migration_ops(model, migrations)
    lines = [
        f"validate_001_against_chain — baseline: {args.baseline}",
        f"  walked {len(migrations)} migrations after 001",
        f"  found {len(gaps)} gap(s)",
        "",
    ]
    for g in gaps:
        lines.append(f"  [{g.migration}] {g.op_name}({g.target}) — {g.description}")
    report = "\n".join(lines)
    print(report)
    if args.report:
        args.report.write_text(report + "\n")
    return 1 if gaps else 0


if __name__ == "__main__":
    sys.exit(main())
