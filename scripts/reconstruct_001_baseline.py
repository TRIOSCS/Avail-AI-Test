"""scripts/reconstruct_001_baseline.py — Generate a draft explicit-DDL body for
alembic/versions/001_initial_schema.py from today's live app.models.

Workflow:
  1. Launch an ephemeral postgres:16 container on a random port.
  2. In a subprocess (PYTHONPATH=repo root, throwaway env vars), import
     app.models and run Base.metadata.create_all() against the ephemeral DB.
  3. pg_dump --schema-only against the ephemeral DB.
  4. Parse the dump, emit op.create_table() / op.create_index() / FK calls.
  5. Tear down the container.

The live-models seed covers ~84 tables. Migrations 002+ reference ~10 more
tables/columns that have since been removed from today's models — those are
surfaced by scripts/validate_001_against_chain.py and added back via Task 6
gap triage.

Output: writes draft body to alembic/versions/001_initial_schema.py.draft

Called by: developer (one-shot, during reconstruction). Not invoked from CI.
Depends on: docker, postgres pg_dump, sqlalchemy, app.models (imported in
            a subprocess so import-time side effects don't leak).
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import textwrap
import time
import uuid
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


# ── Data classes for parsed schema ─────────────────────────────────────


@dataclass
class Column:
    name: str
    py_type: str  # e.g. "sa.String(length=255)" or "sa.Integer()"
    nullable: bool = True
    default: str | None = None  # raw expression as Python source, or None


@dataclass
class ForeignKey:
    local_columns: list[str]
    referenced_table: str
    referenced_columns: list[str]
    name: str | None = None
    ondelete: str | None = None  # 'CASCADE' | 'RESTRICT' | 'SET NULL' | 'SET DEFAULT' | 'NO ACTION' | None
    onupdate: str | None = None  # same set as ondelete


@dataclass
class Table:
    name: str
    columns: list[Column]
    primary_key: list[str] = field(default_factory=list)
    foreign_keys: list[ForeignKey] = field(default_factory=list)
    unique_constraints: list[tuple[str, list[str]]] = field(default_factory=list)


@dataclass
class Index:
    name: str
    table: str
    columns: list[str]
    unique: bool = False
    # Per-column ordering qualifier ('ASC' / 'DESC' / None). Same length as columns.
    column_orderings: list[str | None] = field(default_factory=list)
    # WHERE predicate for partial indexes (verbatim from pg_dump, including outer
    # parens), or None for non-partial indexes.
    where_clause: str | None = None


@dataclass
class ParseResult:
    tables: list[Table] = field(default_factory=list)
    indexes: list[Index] = field(default_factory=list)


# ── Postgres → SQLAlchemy type mapping ─────────────────────────────────

_PG_TO_SA = [
    (r"^integer$", "sa.Integer()"),
    (r"^bigint$", "sa.BigInteger()"),
    (r"^smallint$", "sa.SmallInteger()"),
    (r"^character varying\((\d+)\)$", lambda m: f"sa.String(length={m.group(1)})"),
    (r"^character varying$", "sa.String()"),
    (r"^text$", "sa.Text()"),
    (r"^boolean$", "sa.Boolean()"),
    (
        r"^numeric\((\d+),\s*(\d+)\)$",
        lambda m: f"sa.Numeric(precision={m.group(1)}, scale={m.group(2)})",
    ),
    (r"^numeric$", "sa.Numeric()"),
    (r"^double precision$", "sa.Float()"),
    (r"^real$", "sa.Float()"),
    (r"^date$", "sa.Date()"),
    (r"^timestamp without time zone$", "sa.DateTime()"),
    (r"^timestamp with time zone$", "sa.DateTime(timezone=True)"),
    (r"^json$", "sa.JSON()"),
    (r"^jsonb$", "postgresql.JSONB()"),
    (r"^uuid$", "postgresql.UUID()"),
    (r"^bytea$", "sa.LargeBinary()"),
    (r"^tsvector$", "postgresql.TSVECTOR()"),
    (
        r"^([a-z_]+)\[\]$",
        lambda m: f"postgresql.ARRAY(sa.{_simple_array_inner(m.group(1))})",
    ),
]


def _simple_array_inner(t: str) -> str:
    """Map an array-of-X postgres type to sa.X(); fall back to String."""
    bases = {
        "integer": "Integer()",
        "bigint": "BigInteger()",
        "text": "Text()",
        "character varying": "String()",
    }
    return bases.get(t, "String()")


def map_pg_type(pg_type: str) -> str:
    """Translate a Postgres column type to its SQLAlchemy py-source."""
    pg_type = pg_type.strip().lower()
    for pattern, replacement in _PG_TO_SA:
        m = re.match(pattern, pg_type)
        if m:
            return replacement(m) if callable(replacement) else replacement
    # Unknown type — return a comment-flagged literal so the engineer notices.
    return f"sa.Text()  # FIXME unmapped pg_type: {pg_type!r}"


# ── pg_dump SQL parser ─────────────────────────────────────────────────

_RE_CREATE_TABLE = re.compile(
    r"CREATE TABLE (?:public\.)?(\w+) \((.*?)\);",
    re.DOTALL | re.IGNORECASE,
)
_RE_PRIMARY_KEY = re.compile(
    r"ALTER TABLE ONLY (?:public\.)?(\w+)\s+"
    r"ADD CONSTRAINT \w+ PRIMARY KEY \(([^)]+)\);",
    re.IGNORECASE,
)
_RE_FOREIGN_KEY = re.compile(
    r"ALTER TABLE ONLY (?:public\.)?(\w+)\s+"
    r"ADD CONSTRAINT (\w+)\s+"
    r"FOREIGN KEY \(([^)]+)\) REFERENCES (?:public\.)?(\w+)\(([^)]+)\)"
    r"(?:\s+ON DELETE (CASCADE|RESTRICT|SET NULL|SET DEFAULT|NO ACTION))?"
    r"(?:\s+ON UPDATE (CASCADE|RESTRICT|SET NULL|SET DEFAULT|NO ACTION))?",
    re.IGNORECASE,
)
_RE_INDEX = re.compile(
    r"CREATE (UNIQUE )?INDEX (\w+) ON (?:public\.)?(\w+)(?:\s+USING \w+)?\s*\(([^)]+)\)"
    r"(?:\s+WHERE\s+(.+))?;",
    re.IGNORECASE,
)


def _parse_index_columns(col_str: str) -> tuple[list[str], list[str | None]]:
    """Split an index column list into (column_names, per_column_orderings).

    pg_dump emits index columns as ``a, b DESC, c`` — the trailing ``ASC``/``DESC``
    is an ordering qualifier on each column, not part of the column name. We split
    the qualifier off so render_op_create_index can decide whether to emit the
    simple op.create_index form (no orderings) or fall back to op.execute with
    verbatim CREATE INDEX SQL (any orderings).
    """
    cols: list[str] = []
    orderings: list[str | None] = []
    for raw in col_str.split(","):
        part = raw.strip()
        if not part:
            continue
        toks = part.rsplit(None, 1)
        if len(toks) == 2 and toks[1].upper() in ("ASC", "DESC"):
            cols.append(toks[0])
            orderings.append(toks[1].upper())
        else:
            cols.append(part)
            orderings.append(None)
    return cols, orderings


def _parse_columns(body: str) -> list[Column]:
    cols: list[Column] = []
    for line in body.splitlines():
        line = line.strip().rstrip(",")
        if not line or line.upper().startswith("CONSTRAINT"):
            continue
        # name TYPE [NOT NULL] [DEFAULT ...]
        m = re.match(
            r"(\w+)\s+([a-z][a-z0-9_ ()\[\],]*?)(\s+NOT NULL)?(\s+DEFAULT\s+(.+))?$",
            line,
            re.IGNORECASE,
        )
        if not m:
            continue
        name = m.group(1)
        pg_type = m.group(2).strip()
        not_null = bool(m.group(3))
        default_expr = m.group(5)
        cols.append(
            Column(
                name=name,
                py_type=map_pg_type(pg_type),
                nullable=not not_null,
                default=default_expr,
            )
        )
    return cols


def parse_pg_dump(sql: str) -> ParseResult:
    result = ParseResult()
    tables_by_name: dict[str, Table] = {}

    for m in _RE_CREATE_TABLE.finditer(sql):
        name = m.group(1)
        body = m.group(2)
        t = Table(name=name, columns=_parse_columns(body))
        result.tables.append(t)
        tables_by_name[name] = t

    for m in _RE_PRIMARY_KEY.finditer(sql):
        table = m.group(1)
        cols = [c.strip() for c in m.group(2).split(",")]
        if table in tables_by_name:
            tables_by_name[table].primary_key = cols

    for m in _RE_FOREIGN_KEY.finditer(sql):
        table = m.group(1)
        fk_name = m.group(2)
        local_cols = [c.strip() for c in m.group(3).split(",")]
        ref_table = m.group(4)
        ref_cols = [c.strip() for c in m.group(5).split(",")]
        ondelete = m.group(6).upper() if m.group(6) else None
        onupdate = m.group(7).upper() if m.group(7) else None
        if table in tables_by_name:
            tables_by_name[table].foreign_keys.append(
                ForeignKey(
                    local_columns=local_cols,
                    referenced_table=ref_table,
                    referenced_columns=ref_cols,
                    name=fk_name,
                    ondelete=ondelete,
                    onupdate=onupdate,
                )
            )

    for m in _RE_INDEX.finditer(sql):
        unique = bool(m.group(1))
        ix_name = m.group(2)
        table = m.group(3)
        cols, orderings = _parse_index_columns(m.group(4))
        where_clause = m.group(5).strip() if m.group(5) else None
        result.indexes.append(
            Index(
                name=ix_name,
                table=table,
                columns=cols,
                unique=unique,
                column_orderings=orderings,
                where_clause=where_clause,
            )
        )

    return result


# ── Python source emitters ─────────────────────────────────────────────


def render_op_create_table(t: Table) -> str:
    """Render op.create_table for a single table.

    Cross-table foreign keys are NOT inlined — they are emitted as separate
    op.create_foreign_key calls AFTER all create_table calls complete (see
    render_upgrade_body). This avoids "relation does not exist" errors when 001 creates
    many tables in one batch with cross-references between them.

    Self-reference foreign keys (parent_id → self.id) DO stay inline because the table
    exists by the time the FK constraint is checked.
    """
    lines = [f"op.create_table('{t.name}',"]
    for c in t.columns:
        attrs = [f"'{c.name}'", c.py_type]
        attrs.append(f"nullable={c.nullable}")
        if c.default is not None:
            attrs.append(f"server_default=sa.text({c.default!r})")
        lines.append(f"    sa.Column({', '.join(attrs)}),")
    if t.primary_key:
        pk_cols = ", ".join(f"'{c}'" for c in t.primary_key)
        lines.append(f"    sa.PrimaryKeyConstraint({pk_cols}),")
    for fk in t.foreign_keys:
        if fk.referenced_table != t.name:
            # Cross-table FK — emitted as a separate op.create_foreign_key
            # call after all create_tables finish.
            continue
        local = "[" + ", ".join(f"'{c}'" for c in fk.local_columns) + "]"
        ref = "[" + ", ".join(f"'{fk.referenced_table}.{c}'" for c in fk.referenced_columns) + "]"
        extras = f", name='{fk.name}'" if fk.name else ""
        if fk.ondelete:
            extras += f", ondelete='{fk.ondelete}'"
        if fk.onupdate:
            extras += f", onupdate='{fk.onupdate}'"
        lines.append(f"    sa.ForeignKeyConstraint({local}, {ref}{extras}),")
    lines.append(")")
    return "\n".join(lines)


def render_op_create_index(ix: Index) -> str:
    """Render an index creation as a single line.

    Two paths:

    - **Simple** (no per-column ordering, no WHERE predicate): use alembic's
      native ``op.create_index('name', 'table', [cols], unique=…)`` form. Same
      behavior as before this function gained ordering/where_clause awareness.

    - **Complex** (any column has ASC/DESC, or there's a WHERE predicate):
      fall back to ``op.execute('CREATE INDEX ... ;')`` with verbatim SQL
      reconstructed from the parsed parts. Alembic's create_index can't
      express partial indexes or per-column ordering, so we emit raw DDL.
    """
    has_orderings = any(o is not None for o in ix.column_orderings)
    has_where = ix.where_clause is not None
    if not has_orderings and not has_where:
        cols = "[" + ", ".join(f"'{c}'" for c in ix.columns) + "]"
        return f"op.create_index('{ix.name}', '{ix.table}', {cols}, unique={ix.unique})"

    # Complex path — verbatim SQL via op.execute.
    # column_orderings may be empty (legacy Index() construction); pad to len(columns).
    orderings = list(ix.column_orderings) + [None] * (len(ix.columns) - len(ix.column_orderings))
    col_parts: list[str] = []
    for col, ordering in zip(ix.columns, orderings):
        col_parts.append(f"{col} {ordering}" if ordering else col)
    cols_sql = ", ".join(col_parts)
    unique_kw = "UNIQUE " if ix.unique else ""
    sql = f"CREATE {unique_kw}INDEX {ix.name} ON {ix.table} ({cols_sql})"
    if ix.where_clause:
        sql += f" WHERE {ix.where_clause}"
    sql += ";"
    return f"op.execute({sql!r})"


def _is_complex_index(ix: Index) -> bool:
    """True if the index has per-column ordering or a WHERE predicate.

    Complex indexes are emitted as op.execute(...) by render_op_create_index
    and must use op.execute(DROP INDEX IF EXISTS …) on the downgrade side.
    Used by render_downgrade_body to mirror the upgrade emission.
    """
    return any(o is not None for o in ix.column_orderings) or ix.where_clause is not None


def render_op_create_foreign_key(src_table: str, fk: ForeignKey) -> str:
    """Render `op.create_foreign_key(name, src, ref, [src_cols], [ref_cols], ondelete=…,
    onupdate=…)` for a cross-table FK. Used by render_upgrade_body.

    Cascade kwargs are emitted only when the parsed FK has them; default `NO ACTION` is
    left implicit (no kwarg) to match alembic's convention.
    """
    local = "[" + ", ".join(f"'{c}'" for c in fk.local_columns) + "]"
    remote = "[" + ", ".join(f"'{c}'" for c in fk.referenced_columns) + "]"
    name = f"'{fk.name}'" if fk.name else "None"
    extras = ""
    if fk.ondelete:
        extras += f", ondelete='{fk.ondelete}'"
    if fk.onupdate:
        extras += f", onupdate='{fk.onupdate}'"
    return f"op.create_foreign_key({name}, '{src_table}', '{fk.referenced_table}', {local}, {remote}{extras})"


def render_upgrade_body(parsed: ParseResult) -> list[str]:
    """Render the body lines of upgrade() in three ordered passes:

      1. all op.create_table calls (FK constraints inline ONLY for self-refs)
      2. all op.create_index calls
      3. all op.create_foreign_key calls (cross-table FKs only)

    Pass 3 is what makes the FK separation safe — every referenced table
    exists by the time op.create_foreign_key runs.
    """
    lines: list[str] = []
    for t in parsed.tables:
        lines.append(textwrap.indent(render_op_create_table(t), "    "))
    for ix in parsed.indexes:
        lines.append("    " + render_op_create_index(ix))
    for t in parsed.tables:
        for fk in t.foreign_keys:
            if fk.referenced_table == t.name:
                continue  # self-reference — already inline in create_table
            lines.append("    " + render_op_create_foreign_key(t.name, fk))
    return lines


def render_downgrade_body(parsed: ParseResult) -> list[str]:
    """Render the body lines of downgrade() — symmetric inverse of upgrade.

    Order: drop_constraint (cross-table FKs) → drop_index → drop_table.
    Dropping the FK first prevents PostgreSQL from refusing to drop a table
    whose columns are referenced by another table's FK.
    """
    lines: list[str] = []
    for t in reversed(parsed.tables):
        for fk in reversed(t.foreign_keys):
            if fk.referenced_table == t.name:
                continue
            lines.append(f"    op.drop_constraint('{fk.name}', '{t.name}', type_='foreignkey')")
    for ix in reversed(parsed.indexes):
        if _is_complex_index(ix):
            # Complex indexes were created via op.execute("CREATE INDEX ...;")
            # Mirror with op.execute("DROP INDEX IF EXISTS ...;") — IF EXISTS
            # tolerates partial-index quirks where the index might already be
            # gone (e.g., alembic_version-only states during downgrade-base).
            lines.append(f"    op.execute('DROP INDEX IF EXISTS public.{ix.name};')")
        else:
            lines.append(f"    op.drop_index('{ix.name}', table_name='{ix.table}')")
    for t in reversed(parsed.tables):
        lines.append(f"    op.drop_table('{t.name}')")
    return lines


# ── Orchestration ──────────────────────────────────────────────────────


def _run(cmd: list[str], **kwargs) -> str:
    proc = subprocess.run(cmd, check=True, capture_output=True, text=True, **kwargs)
    return proc.stdout


def _start_pg_container(name: str, port: int) -> None:
    _run(
        [
            "docker",
            "run",
            "--rm",
            "-d",
            "--name",
            name,
            "-e",
            "POSTGRES_USER=arch",
            "-e",
            "POSTGRES_PASSWORD=arch",
            "-e",
            "POSTGRES_DB=arch",
            "-p",
            f"{port}:5432",
            "postgres:16",
        ]
    )
    # Wait for readiness (up to 30s).
    for _ in range(30):
        try:
            _run(["docker", "exec", name, "pg_isready", "-U", "arch", "-d", "arch"])
            return
        except subprocess.CalledProcessError:
            time.sleep(1)
    raise RuntimeError("Postgres container did not become ready in 30s")


def _stop_pg_container(name: str) -> None:
    subprocess.run(["docker", "stop", name], check=False, capture_output=True)


def _create_all_against_live_models(dsn: str) -> None:
    """Run Base.metadata.create_all() from today's app.models against the ephemeral DB.

    Done in a subprocess so the temporary connection doesn't pollute test state. Uses
    today's models (not the Feb-2026 snapshot) because the migration chain 002+ has been
    built against today's create_all output — it is the de-facto contract that 002+
    depends on.
    """
    script = f"""
import os
os.environ.setdefault("DATABASE_URL", {dsn!r})
os.environ.setdefault("SECRET_KEY", "reconstruct")
os.environ.setdefault("SESSION_SECRET", "reconstruct")
from sqlalchemy import create_engine
from app.models import Base
engine = create_engine({dsn!r})
Base.metadata.create_all(bind=engine)
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT) + (os.pathsep + env.get("PYTHONPATH", "") if env.get("PYTHONPATH") else "")
    subprocess.run([sys.executable, "-c", script], check=True, env=env)


def _pg_dump(dsn: str, container: str) -> str:
    return _run(
        [
            "docker",
            "exec",
            "-e",
            "PGPASSWORD=arch",
            container,
            "pg_dump",
            "--schema-only",
            "--no-owner",
            "--no-privileges",
            "-h",
            "localhost",
            "-U",
            "arch",
            "-d",
            "arch",
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--port",
        type=int,
        default=55432,
        help="Local port for ephemeral PG (default 55432)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=REPO_ROOT / "alembic" / "versions" / "001_initial_schema.py.draft",
        help="Output path for the draft 001 body",
    )
    args = parser.parse_args()
    container = f"avail-arch-{uuid.uuid4().hex[:8]}"
    dsn_local = f"postgresql://arch:arch@localhost:{args.port}/arch"
    print(f"[reconstruct] starting ephemeral postgres container {container} on port {args.port}")
    _start_pg_container(container, args.port)
    try:
        print("[reconstruct] running Base.metadata.create_all() from live app.models against ephemeral DB")
        _create_all_against_live_models(dsn_local)
        print("[reconstruct] running pg_dump --schema-only")
        sql = _pg_dump(dsn_local, container)
        print(f"[reconstruct] parsing pg_dump output ({len(sql)} chars)")
        parsed = parse_pg_dump(sql)
        print(f"[reconstruct] parsed {len(parsed.tables)} tables, {len(parsed.indexes)} indexes")
        gen_date = date.today().isoformat()
        body_lines = [
            '"""Initial schema — explicit DDL baseline.',
            "",
            f"Generated {gen_date} from today's live app.models via scripts/reconstruct_001_baseline.py,",
            "then augmented with historical tables/columns referenced by migrations 002+",
            "(per scripts/validate_001_against_chain.py's gap report).",
            "",
            "For fresh DBs only. Production and any DB already stamped at any revision",
            "≥ 001_initial is unaffected — alembic's version table is not modified by",
            "this rewrite.",
            '"""',
            "from typing import Sequence, Union",
            "import sqlalchemy as sa",
            "from sqlalchemy.dialects import postgresql",
            "from alembic import op",
            "",
            'revision: str = "001_initial"',
            "down_revision: Union[str, None] = None",
            "branch_labels: Union[str, Sequence[str], None] = None",
            "depends_on: Union[str, Sequence[str], None] = None",
            "",
            "",
            "def upgrade() -> None:",
        ]
        body_lines.extend(render_upgrade_body(parsed))
        body_lines.append("")
        body_lines.append("")
        body_lines.append("def downgrade() -> None:")
        body_lines.extend(render_downgrade_body(parsed))
        args.out.write_text("\n".join(body_lines) + "\n")
        print(f"[reconstruct] wrote draft to {args.out}")
    finally:
        print(f"[reconstruct] stopping container {container}")
        _stop_pg_container(container)
    return 0


if __name__ == "__main__":
    sys.exit(main())
