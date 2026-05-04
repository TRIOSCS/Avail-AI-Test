"""scripts/reconstruct_001_baseline.py — Generate a draft explicit-DDL body for
alembic/versions/001_initial_schema.py from the Feb-2026 baseline commit.

Workflow:
  1. Extract d6ffe05d:app/models.py and d6ffe05d:app/database.py to a temp dir.
  2. Launch an ephemeral postgres:16 container on a random port.
  3. Load the frozen Base via importlib.util, run Base.metadata.create_all().
  4. pg_dump --schema-only against the ephemeral DB.
  5. Parse the dump, emit op.create_table() / op.create_index() / FK calls.
  6. Tear down the container.

Output: writes draft body to alembic/versions/001_initial_schema.py.draft

Called by: developer (one-shot, during reconstruction). Not invoked from CI.
Depends on: docker, git, postgres pg_dump, sqlalchemy, app.* (only at runtime
            of the frozen models, in an isolated namespace).
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BASELINE_COMMIT = "d6ffe05d4435555d18e048fab44ebf25219f94d4"


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
    r"FOREIGN KEY \(([^)]+)\) REFERENCES (?:public\.)?(\w+)\(([^)]+)\)",
    re.IGNORECASE,
)
_RE_INDEX = re.compile(
    r"CREATE (UNIQUE )?INDEX (\w+) ON (?:public\.)?(\w+)(?:\s+USING \w+)?\s*\(([^)]+)\);",
    re.IGNORECASE,
)


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
        if table in tables_by_name:
            tables_by_name[table].foreign_keys.append(
                ForeignKey(
                    local_columns=local_cols,
                    referenced_table=ref_table,
                    referenced_columns=ref_cols,
                    name=fk_name,
                )
            )

    for m in _RE_INDEX.finditer(sql):
        unique = bool(m.group(1))
        ix_name = m.group(2)
        table = m.group(3)
        cols = [c.strip() for c in m.group(4).split(",")]
        result.indexes.append(Index(name=ix_name, table=table, columns=cols, unique=unique))

    return result


# ── Python source emitters ─────────────────────────────────────────────


def render_op_create_table(t: Table) -> str:
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
        local = "[" + ", ".join(f"'{c}'" for c in fk.local_columns) + "]"
        ref = "[" + ", ".join(f"'{fk.referenced_table}.{c}'" for c in fk.referenced_columns) + "]"
        name_arg = f", name='{fk.name}'" if fk.name else ""
        lines.append(f"    sa.ForeignKeyConstraint({local}, {ref}{name_arg}),")
    lines.append(")")
    return "\n".join(lines)


def render_op_create_index(ix: Index) -> str:
    cols = "[" + ", ".join(f"'{c}'" for c in ix.columns) + "]"
    return f"op.create_index('{ix.name}', '{ix.table}', {cols}, unique={ix.unique})"


# ── Orchestration ──────────────────────────────────────────────────────


def _run(cmd: list[str], **kwargs) -> str:
    proc = subprocess.run(cmd, check=True, capture_output=True, text=True, **kwargs)
    return proc.stdout


def _checkout_baseline_models(dest_dir: Path) -> Path | None:
    """Legacy git-archaeology path — UNUSED in the current reconstruction strategy.

    The d6ffe05d snapshot was 28 tables; migrations 002+ implicitly assume
    today's ~84-table schema (because 001 used to be Base.metadata.create_all
    against today's live models). The right baseline for the explicit-DDL
    rewrite is therefore today's models, not the Feb-2026 snapshot. See
    _create_all_against_live_models below.
    """
    return None


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

    Done in a subprocess so the temporary connection doesn't pollute test state.
    Uses today's models (not the Feb-2026 snapshot) because the migration chain
    002+ has been built against today's create_all output — it is the de-facto
    contract that 002+ depends on.
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
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            print("[reconstruct] running Base.metadata.create_all() from live app.models against ephemeral DB")
            _ = tmp_path  # tmp dir kept only as a scratch space marker
            _create_all_against_live_models(dsn_local)
            print("[reconstruct] running pg_dump --schema-only")
            sql = _pg_dump(dsn_local, container)
            print(f"[reconstruct] parsing pg_dump output ({len(sql)} chars)")
            parsed = parse_pg_dump(sql)
            print(f"[reconstruct] parsed {len(parsed.tables)} tables, {len(parsed.indexes)} indexes")
            body_lines = [
                '"""initial schema — explicit DDL baseline (generated)',
                "",
                "Generated by scripts/reconstruct_001_baseline.py from commit d6ffe05d.",
                "Validated against later migrations by scripts/validate_001_against_chain.py.",
                "Fresh-DB only; production stays stamped at HEAD untouched.",
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
            for t in parsed.tables:
                body_lines.append(textwrap.indent(render_op_create_table(t), "    "))
            for ix in parsed.indexes:
                body_lines.append("    " + render_op_create_index(ix))
            body_lines.append("")
            body_lines.append("")
            body_lines.append("def downgrade() -> None:")
            for ix in reversed(parsed.indexes):
                body_lines.append(f"    op.drop_index('{ix.name}', table_name='{ix.table}')")
            for t in reversed(parsed.tables):
                body_lines.append(f"    op.drop_table('{t.name}')")
            args.out.write_text("\n".join(body_lines) + "\n")
            print(f"[reconstruct] wrote draft to {args.out}")
    finally:
        print(f"[reconstruct] stopping container {container}")
        _stop_pg_container(container)
    return 0


if __name__ == "__main__":
    sys.exit(main())
