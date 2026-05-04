# Alembic 001 Baseline Rewrite — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rewrite `alembic/versions/001_initial_schema.py` from `Base.metadata.create_all()` to an explicit `op.create_table()` snapshot covering every table the migration chain (002+) references, validated against the forward chain. Fix CI red on `main`, then walk the 6 stacked Phase 4 PRs through the merge gauntlet so the backlog clears and `main` CI is green end-to-end.

**Architecture:** Live-models + chain-validator strategy (revised 2026-05-04, see "Strategy revision" below): (a) `Base.metadata.create_all()` from today's `app.models` against an ephemeral Postgres → `pg_dump --schema-only` → transcribe to explicit `op.create_table()` calls; (b) the validator walks every migration after 001 and reports every `op.alter`/`op.drop`/`op.add` whose target isn't in the draft. Triage each gap by adding back the historical table/column the chain still references. Add a CI step that runs `alembic.autogenerate.compare_metadata` against `Base.metadata` to catch ongoing drift. Same `revision = "001_initial"` ID — prod is untouched.

## Strategy revision (2026-05-04)

The original plan called for git-archaeology of commit `d6ffe05d` to seed the first-draft DDL. That commit's `app/models.py` declared ~28 tables, but the migration chain (002–130) collectively touches ~94 tables (today's 84 + ~10 since-removed). A 28-table seed would have surfaced ~60 missing tables in the validator (a much heavier triage burden) than starting from today's models. The strategy was switched to live-models seeding before the script was first run; the script and validator at HEAD on `fix/ci-unblock-alembic-and-audit` implement the live-models path.

**One-shot validator outcome (2026-05-04):** 129 migrations walked, **203 gaps**. Concentrated in 5 since-removed tables (`buy_plans`, `error_reports`, `trouble_tickets`, `inventory_snapshots`, `material_card_audit`) and ~10 since-removed columns. These are the historical objects that need to be added back to 001 as part of Task 6 Step 2 (gap triage).

**Code listings below were written for the original git-archaeology approach.** Where they diverge from `scripts/reconstruct_001_baseline.py` and `scripts/validate_001_against_chain.py` at HEAD, **the committed scripts are authoritative.** Tasks 2, 3, 4 (script-creation tasks) are effectively complete — their committed outputs are in place. The active task is Task 6 Step 2 (gap triage).

**Tech Stack:** SQLAlchemy 2.0.48, Alembic 1.18.4, Postgres 16, Python 3.11+, pytest, ruff, mypy. Existing CI workflow at `.github/workflows/ci.yml` already provisions a Postgres service and runs the round trip — we extend that step.

**Spec source:** `docs/superpowers/specs/2026-04-30-alembic-001-baseline-rewrite-design.md`. Read it before starting.

**Spec deviation (single):** the spec mentions `tests/test_alembic_round_trip.py` as the test-file location for the metadata-diff. `tests/conftest.py` forcibly overrides `DATABASE_URL=sqlite://` and patches SQLite type compilers, so a Postgres-backed pytest in `tests/` would have to fight conftest. Instead, the metadata-diff lives in `scripts/check_schema_matches_models.py`, called from CI between `upgrade head` and `downgrade base`, and from the local smoke command. Rationale: simpler than a SQLite-skip dance; uses the existing CI Postgres service directly. Functionally identical guarantee.

---

## File Structure

| File | Status | Responsibility |
|---|---|---|
| `alembic/versions/001_initial_schema.py` | **Replace body** | Explicit `op.create_table()` / `op.create_index()` / `op.create_foreign_key()` calls covering every table the chain (002+) references — today's 84 + the 5 since-removed historical tables. Symmetric `downgrade()`. Same `revision = "001_initial"`. |
| `scripts/reconstruct_001_baseline.py` | **Done (committed)** | Dev tool: runs `Base.metadata.create_all()` from today's `app.models` against an ephemeral Postgres container, captures `pg_dump --schema-only`, emits a draft `op.create_table()` body. Runnable, idempotent (tears down its own container). |
| `scripts/validate_001_against_chain.py` | **Done (committed)** | Dev tool: builds an in-memory schema model from the draft 001, walks every later migration in revision order, asserts every `op.alter_table`/`op.drop_*`/`op.add_*`/`op.alter_column`/`op.rename_table` references something the model already contains. Emits structured gap report. Exit non-zero on gaps. |
| `scripts/validate_001_against_chain.last_run.txt` | **Generated, committed at the end of Task 6** | Validation evidence: the clean output of the validator's final run. The 2026-05-04 first-pass output (203 gaps) is the triage input for Task 6 Step 2 — the *clean* output gets committed once triage is done. |
| `scripts/check_schema_matches_models.py` | **Done (committed)** | Dev + CI tool: connects to `DATABASE_URL`, runs `alembic.autogenerate.compare_metadata(MigrationContext, Base.metadata)`, asserts the diff list is empty modulo a documented allowlist. Exit 0 on match, exit 1 on drift. |
| `tests/scripts/test_check_schema_matches_models.py` | **Done (committed)** | Unit tests for the diff comparator + allowlist filtering. |
| `tests/scripts/test_validate_001_against_chain.py` | **Done (committed)** | Unit tests for the schema-model class (add_table, drop_table, add_column, etc.) and integration tests against tiny synthetic migration directories. |
| `tests/scripts/test_reconstruct_001_baseline.py` | **Done (committed)** | Unit tests for the SQL→`op.create_table()` translator (the parsing portion is the only safely testable part without spinning Postgres). |
| `.github/workflows/ci.yml` | **Modify** | Extend the existing `Alembic upgrade/downgrade smoke test` step to invoke `scripts/check_schema_matches_models.py` between `upgrade head` and `downgrade base`. |
| `requirements.txt` | **Already modified on branch (uncommitted)** | CVE bumps in working tree — `cryptography 46.0.5→46.0.7`, `python-multipart 0.0.22→0.0.26`. Committed in its own commit per Task 12. |

---

## Tasks

### Task 1: Pre-flight — rebase, drop stash, verify clean state

**Files:**
- None modified — this task only touches git state.

- [ ] **Step 1: Confirm branch and inspect state**

```bash
cd /root/availai
git status --short
git rev-parse --abbrev-ref HEAD
git stash list
```
Expected:
```
 D .claude/worktrees/agent-a6911920
 M requirements.txt
fix/ci-unblock-alembic-and-audit
stash@{0}: On fix/ci-unblock-alembic-and-audit: wip-dev-tool-run-mods
stash@{1}: On main: pre-deploy dist artifacts
```
If branch isn't `fix/ci-unblock-alembic-and-audit`, switch: `git checkout fix/ci-unblock-alembic-and-audit`.

- [ ] **Step 2: Fetch and rebase onto `origin/main`**

```bash
git fetch origin
git rebase origin/main
```
Expected: clean rebase, no conflicts. Branch should be 0 behind, 1+ ahead after the spec-doc commit is rebased on top.

If a conflict appears in `requirements.txt`, resolve manually preserving both the CVE bumps (`cryptography 46.0.5→46.0.7`, `python-multipart 0.0.22→0.0.26`) and any new lines from main. `git rebase --continue`.

- [ ] **Step 3: Drop `stash@{0}` (band-aid + dist files)**

```bash
git stash show stash@{0} --stat
```
Expected: shows alembic/versions/003_perf_fk_indexes_dedup.py + tests + dist/ files. The 003 band-aid is what we're rejecting; dist files are deploy artifacts, not source.

```bash
git stash drop stash@{0}
git stash list
```
Expected: only `stash@{1}: On main: pre-deploy dist artifacts` remains.

- [ ] **Step 4: Verify prod is stamped at HEAD (compat sanity)**

```bash
ssh root@app.availai.net "cd /root/availai && docker compose exec -T app alembic current 2>&1 | tail -5"
```
Expected: a single revision ID (the head of the chain, e.g. `restructure_substitutes_json` or whichever the latest is). Capture the output to a local note for the post-merge gate later.

If the SSH command isn't available in your environment, mark this step skipped and add an explicit pre-merge check item to the PR description.

- [ ] **Step 5: Commit nothing — pre-flight is read-only state verification**

No commit. Move to Task 2.

---

### Task 2: Build `scripts/check_schema_matches_models.py` (TDD)

**Files:**
- Create: `scripts/check_schema_matches_models.py`
- Test: `tests/scripts/test_check_schema_matches_models.py`

This is the diff comparator used by CI and locally. Build first because it's also useful during reconstruction to verify the draft 001 produces the expected schema.

- [ ] **Step 1: Write the failing test (allowlist filtering)**

Create `tests/scripts/__init__.py` (empty) if it doesn't exist:

```bash
mkdir -p tests/scripts
touch tests/scripts/__init__.py
```

Write `tests/scripts/test_check_schema_matches_models.py`:

```python
"""tests/scripts/test_check_schema_matches_models.py — Tests for the schema diff
comparator used in CI and local smoke.

Called by: pytest
Depends on: scripts.check_schema_matches_models
"""
from scripts.check_schema_matches_models import filter_allowlist, format_diffs


def test_filter_allowlist_drops_numeric_precision_noise():
    """alembic.compare_metadata sometimes reports Numeric(10,2) vs NUMERIC(10,2) drift
    that is semantically a no-op. Allowlist must drop these."""
    raw = [
        ("modify_type", None, "table_x", "col_y", {}, "NUMERIC(10, 2)", "Numeric(precision=10, scale=2)"),
    ]
    assert filter_allowlist(raw) == []


def test_filter_allowlist_keeps_real_drift():
    """A genuinely added column must NOT be filtered."""
    raw = [
        ("add_column", None, "table_x", "new_col"),
    ]
    assert filter_allowlist(raw) == raw


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
```

- [ ] **Step 2: Run test to verify it fails**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/scripts/test_check_schema_matches_models.py -v
```
Expected: FAIL — `ModuleNotFoundError: scripts.check_schema_matches_models` (or `ImportError`).

- [ ] **Step 3: Write the minimal implementation**

Create `scripts/check_schema_matches_models.py`:

```python
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
from typing import Iterable

from sqlalchemy import create_engine

from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext

from app.models import Base


# Each entry is a (diff_kind, predicate) tuple. The predicate gets the raw diff
# tuple and returns True if the entry is a known false positive that should be
# dropped from the result. Keep this list short; every entry needs a comment
# explaining the underlying alembic/sqlalchemy quirk.
_ALLOWLIST: list[tuple[str, callable]] = [
    # Numeric(10, 2) reflected as NUMERIC(10, 2) — same type, different rendering.
    # alembic.autogenerate sometimes flags this as modify_type.
    (
        "modify_type",
        lambda d: (
            len(d) >= 7
            and isinstance(d[5], str)
            and isinstance(d[6], object)
            and "NUMERIC" in str(d[5]).upper()
            and "Numeric" in type(d[6]).__name__
        ),
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
```

- [ ] **Step 4: Run tests to verify pass**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/scripts/test_check_schema_matches_models.py -v
```
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add scripts/check_schema_matches_models.py tests/scripts/__init__.py tests/scripts/test_check_schema_matches_models.py
git commit -m "$(cat <<'EOF'
feat(alembic): add schema-vs-models drift check script

scripts/check_schema_matches_models.py runs alembic.autogenerate.compare_metadata
against app.models.Base.metadata and exits non-zero on drift. Used by the
upcoming CI smoke-test extension and locally by devs. Allowlist filters known
false positives (Numeric precision rendering) with comments explaining each.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Build `scripts/reconstruct_001_baseline.py` (TDD)

> **STALE — committed scripts are authoritative.** This task body was written for the original git-archaeology strategy (see "Strategy revision (2026-05-04)" at the top of this plan). The script now seeds from today's live `app.models`, not from `d6ffe05d`. Code listings, expected stdout, and the `BASELINE_COMMIT` constant in this task no longer match `scripts/reconstruct_001_baseline.py` at HEAD. Read for design intent only.

**Files:**
- Create: `scripts/reconstruct_001_baseline.py`
- Test: `tests/scripts/test_reconstruct_001_baseline.py`

This is the dev tool that emits the first-draft `001` body. It (a) writes `d6ffe05d`-era models to a temp dir, (b) launches an ephemeral Postgres container, (c) runs `Base.metadata.create_all()` from those models, (d) captures `pg_dump --schema-only`, (e) parses the SQL into Python `op.create_table()` calls.

The unit-testable portion is the SQL → Python translator. The orchestration (container management, git checkout, `create_all`) is integration-tested by running the full script against a real environment in Task 5.

- [ ] **Step 1: Write the failing test (SQL → op.create_table translator)**

Append to `tests/scripts/test_reconstruct_001_baseline.py`:

```python
"""tests/scripts/test_reconstruct_001_baseline.py — Tests for the SQL→Python
DDL translator inside scripts/reconstruct_001_baseline.py.

Called by: pytest
Depends on: scripts.reconstruct_001_baseline
"""
import textwrap

from scripts.reconstruct_001_baseline import (
    parse_pg_dump,
    render_op_create_table,
    render_op_create_index,
)


def test_parse_pg_dump_extracts_simple_table():
    """A bare CREATE TABLE block emerges as a structured Table object."""
    sql = textwrap.dedent("""
    CREATE TABLE public.users (
        id integer NOT NULL,
        email character varying(255) NOT NULL,
        created_at timestamp without time zone DEFAULT now()
    );
    """)
    result = parse_pg_dump(sql)
    assert len(result.tables) == 1
    t = result.tables[0]
    assert t.name == "users"
    assert len(t.columns) == 3
    assert t.columns[0].name == "id"
    assert t.columns[0].nullable is False
    assert t.columns[1].name == "email"
    assert t.columns[1].py_type == "sa.String(length=255)"


def test_parse_pg_dump_extracts_primary_key():
    sql = textwrap.dedent("""
    CREATE TABLE public.users (id integer NOT NULL);
    ALTER TABLE ONLY public.users ADD CONSTRAINT users_pkey PRIMARY KEY (id);
    """)
    result = parse_pg_dump(sql)
    t = result.tables[0]
    assert t.primary_key == ["id"]


def test_parse_pg_dump_extracts_foreign_key():
    sql = textwrap.dedent("""
    CREATE TABLE public.users (id integer NOT NULL);
    CREATE TABLE public.requisitions (id integer NOT NULL, creator_id integer);
    ALTER TABLE ONLY public.requisitions
        ADD CONSTRAINT requisitions_creator_id_fkey
        FOREIGN KEY (creator_id) REFERENCES public.users(id);
    """)
    result = parse_pg_dump(sql)
    req = next(t for t in result.tables if t.name == "requisitions")
    assert len(req.foreign_keys) == 1
    fk = req.foreign_keys[0]
    assert fk.local_columns == ["creator_id"]
    assert fk.referenced_table == "users"
    assert fk.referenced_columns == ["id"]


def test_parse_pg_dump_extracts_index():
    sql = textwrap.dedent("""
    CREATE TABLE public.users (id integer NOT NULL, email varchar(255));
    CREATE INDEX ix_users_email ON public.users USING btree (email);
    """)
    result = parse_pg_dump(sql)
    assert len(result.indexes) == 1
    ix = result.indexes[0]
    assert ix.name == "ix_users_email"
    assert ix.table == "users"
    assert ix.columns == ["email"]
    assert ix.unique is False


def test_render_op_create_table_emits_valid_python():
    from scripts.reconstruct_001_baseline import Table, Column
    t = Table(
        name="users",
        columns=[
            Column(name="id", py_type="sa.Integer()", nullable=False),
            Column(name="email", py_type="sa.String(length=255)", nullable=False),
        ],
        primary_key=["id"],
        foreign_keys=[],
    )
    out = render_op_create_table(t)
    assert "op.create_table(" in out
    assert "'users'" in out
    assert "sa.Column('id', sa.Integer(), nullable=False)" in out
    assert "sa.PrimaryKeyConstraint('id')" in out


def test_render_op_create_index_emits_valid_python():
    from scripts.reconstruct_001_baseline import Index
    ix = Index(name="ix_users_email", table="users", columns=["email"], unique=False)
    out = render_op_create_index(ix)
    assert out == "op.create_index('ix_users_email', 'users', ['email'], unique=False)"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/scripts/test_reconstruct_001_baseline.py -v
```
Expected: all FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write the minimal implementation (SQL parser + renderer; orchestration deferred to step 5 of Task 5)**

Create `scripts/reconstruct_001_baseline.py`:

```python
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
    py_type: str           # e.g. "sa.String(length=255)" or "sa.Integer()"
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
    (r"^numeric\((\d+),\s*(\d+)\)$", lambda m: f"sa.Numeric(precision={m.group(1)}, scale={m.group(2)})"),
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
    (r"^([a-z_]+)\[\]$", lambda m: f"postgresql.ARRAY(sa.{_simple_array_inner(m.group(1))})"),
]


def _simple_array_inner(t: str) -> str:
    """Map an array-of-X postgres type to sa.X(); fall back to String."""
    bases = {"integer": "Integer()", "bigint": "BigInteger()", "text": "Text()", "character varying": "String()"}
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


def _checkout_baseline_models(dest_dir: Path) -> tuple[Path, Path]:
    models = dest_dir / "models.py"
    database = dest_dir / "database.py"
    models.write_text(_run(["git", "show", f"{BASELINE_COMMIT}:app/models.py"], cwd=REPO_ROOT))
    database.write_text(_run(["git", "show", f"{BASELINE_COMMIT}:app/database.py"], cwd=REPO_ROOT))
    return models, database


def _start_pg_container(name: str, port: int) -> None:
    _run([
        "docker", "run", "--rm", "-d",
        "--name", name,
        "-e", "POSTGRES_USER=arch",
        "-e", "POSTGRES_PASSWORD=arch",
        "-e", "POSTGRES_DB=arch",
        "-p", f"{port}:5432",
        "postgres:16",
    ])
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


def _create_all_against(models_path: Path, database_path: Path, dsn: str) -> None:
    """Load Feb-2026 models in an isolated namespace and run create_all().

    Done in a subprocess so import-time side effects from app/* don't leak.
    """
    script = f"""
import importlib.util, sys
spec_db = importlib.util.spec_from_file_location("frozen_database", {str(database_path)!r})
mod_db = importlib.util.module_from_spec(spec_db); sys.modules["frozen_database"] = mod_db
spec_db.loader.exec_module(mod_db)
spec_m = importlib.util.spec_from_file_location("frozen_models", {str(models_path)!r})
mod_m = importlib.util.module_from_spec(spec_m); sys.modules["frozen_models"] = mod_m
spec_m.loader.exec_module(mod_m)
from sqlalchemy import create_engine
engine = create_engine({dsn!r})
mod_m.Base.metadata.create_all(bind=engine)
"""
    subprocess.run([sys.executable, "-c", script], check=True)


def _pg_dump(dsn: str, container: str) -> str:
    return _run([
        "docker", "exec", "-e", f"PGPASSWORD=arch", container,
        "pg_dump", "--schema-only", "--no-owner", "--no-privileges",
        "-h", "localhost", "-U", "arch", "-d", "arch",
    ])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=55432, help="Local port for ephemeral PG (default 55432)")
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
            print(f"[reconstruct] checking out d6ffe05d models to {tmp_path}")
            models, database = _checkout_baseline_models(tmp_path)
            print("[reconstruct] running Base.metadata.create_all() against ephemeral DB")
            _create_all_against(models, database, dsn_local)
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
```

- [ ] **Step 4: Run tests to verify pass**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/scripts/test_reconstruct_001_baseline.py -v
```
Expected: 6 passed.

- [ ] **Step 5: Run ruff to confirm style**

```bash
ruff check scripts/reconstruct_001_baseline.py
ruff format --check scripts/reconstruct_001_baseline.py
```
Expected: no errors. If formatting fails, run `ruff format scripts/reconstruct_001_baseline.py` and re-stage.

- [ ] **Step 6: Commit**

```bash
git add scripts/reconstruct_001_baseline.py tests/scripts/test_reconstruct_001_baseline.py
git commit -m "$(cat <<'EOF'
feat(alembic): add 001 baseline reconstruction script

scripts/reconstruct_001_baseline.py extracts d6ffe05d-era models, runs
Base.metadata.create_all() against an ephemeral postgres:16 container,
captures pg_dump --schema-only, and emits a draft 001 body of
op.create_table()/op.create_index() calls. Tested against synthetic
SQL fixtures.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Build `scripts/validate_001_against_chain.py` (TDD)

> **STALE — committed scripts are authoritative.** This task body predates the live-models pivot AND a subsequent fix to `walk_migration_ops` (see commit `7af6e7ac`: validator now walks only `def upgrade()` to avoid BFS-ordering false positives from idempotent-guarded upgrades vs. bare downgrade ops). Code listings here may not match `scripts/validate_001_against_chain.py` at HEAD. Read for design intent only.

**Files:**
- Create: `scripts/validate_001_against_chain.py`
- Test: `tests/scripts/test_validate_001_against_chain.py`

This walks every migration after 001 and asserts every operation references something the schema model already contains. Heart of the live-models + chain validator strategy.

- [ ] **Step 1: Write the failing tests (schema-model class + walker)**

Create `tests/scripts/test_validate_001_against_chain.py`:

```python
"""tests/scripts/test_validate_001_against_chain.py — Tests for the 001-vs-chain
validator's schema model and migration walker.

Called by: pytest
Depends on: scripts.validate_001_against_chain
"""
from scripts.validate_001_against_chain import SchemaModel, walk_migration_ops, Gap


def test_schema_model_add_then_drop():
    m = SchemaModel()
    m.add_table("users", ["id", "email"])
    assert m.has_table("users")
    assert m.has_column("users", "email")
    m.drop_table("users")
    assert not m.has_table("users")


def test_schema_model_add_drop_column():
    m = SchemaModel()
    m.add_table("users", ["id"])
    m.add_column("users", "email")
    assert m.has_column("users", "email")
    m.drop_column("users", "email")
    assert not m.has_column("users", "email")


def test_schema_model_rename_table():
    m = SchemaModel()
    m.add_table("users_v1", ["id"])
    m.rename_table("users_v1", "users")
    assert m.has_table("users")
    assert not m.has_table("users_v1")


def test_walk_migration_ops_detects_drop_of_unknown_table(tmp_path):
    """A migration that drops a table not in the model produces a Gap."""
    mig = tmp_path / "002_drop_ghosts.py"
    mig.write_text(
        "def upgrade():\n"
        "    op.drop_table('ghost_table')\n"
    )
    m = SchemaModel()
    m.add_table("real_table", ["id"])
    gaps = walk_migration_ops(m, [mig])
    assert len(gaps) == 1
    assert gaps[0].migration == "002_drop_ghosts.py"
    assert "ghost_table" in gaps[0].description


def test_walk_migration_ops_handles_add_column_on_known_table(tmp_path):
    mig = tmp_path / "002_add_email.py"
    mig.write_text(
        "def upgrade():\n"
        "    op.add_column('users', sa.Column('email', sa.String(255)))\n"
    )
    m = SchemaModel()
    m.add_table("users", ["id"])
    gaps = walk_migration_ops(m, [mig])
    assert gaps == []
    assert m.has_column("users", "email")


def test_walk_migration_ops_flags_alter_on_unknown_column(tmp_path):
    mig = tmp_path / "002_alter_missing.py"
    mig.write_text(
        "def upgrade():\n"
        "    op.alter_column('users', 'never_existed', nullable=False)\n"
    )
    m = SchemaModel()
    m.add_table("users", ["id"])
    gaps = walk_migration_ops(m, [mig])
    assert len(gaps) == 1
    assert "never_existed" in gaps[0].description


def test_walk_migration_ops_skips_data_only_migrations(tmp_path):
    """A migration with only data ops (op.execute, op.bulk_insert) produces no gaps."""
    mig = tmp_path / "002_seed_data.py"
    mig.write_text(
        "def upgrade():\n"
        "    op.execute('INSERT INTO users (id) VALUES (1)')\n"
    )
    m = SchemaModel()
    m.add_table("users", ["id"])
    gaps = walk_migration_ops(m, [mig])
    assert gaps == []
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/scripts/test_validate_001_against_chain.py -v
```
Expected: 7 FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write the minimal implementation**

Create `scripts/validate_001_against_chain.py`:

```python
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
from dataclasses import dataclass, field
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

REPO_ROOT = Path(__file__).resolve().parent.parent


# ── Schema model ───────────────────────────────────────────────────────

class SchemaModel:
    """Minimal in-memory schema state. Tracks tables and their column sets."""

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
            table = _string_arg(node, 0) or _kw_str(node, "table_name") or _kw_str(node, "source_table")
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
                col_name = (
                    _string_arg(col_node, 0) if isinstance(col_node, ast.Call) else None
                )
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
    """Parse the proposed 001 file and seed a SchemaModel from its create_table calls."""
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
```

- [ ] **Step 4: Run tests to verify pass**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/scripts/test_validate_001_against_chain.py -v
```
Expected: 7 passed.

- [ ] **Step 5: Lint + format**

```bash
ruff check scripts/validate_001_against_chain.py
ruff format --check scripts/validate_001_against_chain.py
```
Expected: clean. Auto-fix with `ruff format scripts/validate_001_against_chain.py` if needed and re-stage.

- [ ] **Step 6: Commit**

```bash
git add scripts/validate_001_against_chain.py tests/scripts/test_validate_001_against_chain.py
git commit -m "$(cat <<'EOF'
feat(alembic): add 001-vs-chain validator

scripts/validate_001_against_chain.py walks every migration after 001 in
revision order via alembic.ScriptDirectory, AST-parses each, and asserts
every op.alter/op.drop/op.add/op.rename references something the seeded
schema model contains. Non-zero exit on gap. Heart of the live-models
+ chain validator reconstruction strategy.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: Run reconstruction — produce draft 001

> **STALE — committed scripts are authoritative.** Expected stdout in this task ("checking out d6ffe05d models...") was written for the abandoned git-archaeology path. The current script prints "running Base.metadata.create_all() from live app.models against ephemeral DB" and produces an ~85-table draft. The reconstruction has already been run once on 2026-05-04; the draft sits at `alembic/versions/001_initial_schema.py.draft` awaiting Task 6 gap triage. Re-run only if the draft is regenerated from scratch.

**Files:**
- Create: `alembic/versions/001_initial_schema.py.draft` (uncommitted intermediate; deleted before final commit)

This is the integration step for `scripts/reconstruct_001_baseline.py`. It produces the first-draft body. The validator (Task 6) will then iterate against it.

- [ ] **Step 1: Verify Docker is available + chosen port is free**

```bash
docker version | head -3
ss -ltn | grep ':55432' && echo "PORT BUSY — pick a different one" || echo "port 55432 free"
```
Expected: docker is running; port 55432 free.

- [ ] **Step 2: Run the reconstruction script**

```bash
cd /root/availai
PYTHONPATH=/root/availai python scripts/reconstruct_001_baseline.py --port 55432
```
Expected output (excerpts):
```
[reconstruct] starting ephemeral postgres container avail-arch-XXXXXXXX on port 55432
[reconstruct] checking out d6ffe05d models to /tmp/...
[reconstruct] running Base.metadata.create_all() against ephemeral DB
[reconstruct] running pg_dump --schema-only
[reconstruct] parsing pg_dump output (NNNN chars)
[reconstruct] parsed N tables, N indexes
[reconstruct] wrote draft to .../alembic/versions/001_initial_schema.py.draft
[reconstruct] stopping container avail-arch-XXXXXXXX
```

- [ ] **Step 3: Inspect the draft for sanity**

```bash
wc -l alembic/versions/001_initial_schema.py.draft
head -30 alembic/versions/001_initial_schema.py.draft
grep -c "op.create_table" alembic/versions/001_initial_schema.py.draft
grep -c "op.create_index" alembic/versions/001_initial_schema.py.draft
grep "FIXME unmapped" alembic/versions/001_initial_schema.py.draft
```
Expected:
- Line count: ~1500–3000 lines.
- ~80+ `op.create_table` calls.
- Some `op.create_index` calls.
- ANY `FIXME unmapped` lines indicate type-mapping holes — investigate before proceeding (likely a Postgres type the mapper doesn't know yet; add a row to `_PG_TO_SA` in `scripts/reconstruct_001_baseline.py`, re-run, re-check).

- [ ] **Step 4: If FIXME-unmapped lines exist, patch the mapper**

For each unique unmapped `pg_type` shown:
- Add a row to `_PG_TO_SA` in `scripts/reconstruct_001_baseline.py`. Pattern: `(r"^<pg_type>$", "<sa_expression>")`.
- Re-run Step 2.
- Repeat until no `FIXME unmapped` lines remain.
- Commit the mapper additions:

```bash
git add scripts/reconstruct_001_baseline.py
git commit -m "fix(reconstruct): map additional pg_types — <list>"
```

- [ ] **Step 5: Move on to validation (Task 6) — do NOT commit the draft yet**

The draft is intentionally `.draft`-suffixed and untracked. Task 6 may require edits; Task 7 finalizes by renaming over the real `001_initial_schema.py`.

---

### Task 6: Run validation — iterate until clean

**Files:**
- Modify: `alembic/versions/001_initial_schema.py.draft` (in-place patches)
- Create: `scripts/validate_001_against_chain.last_run.txt`

- [ ] **Step 1: Run the validator against the draft**

```bash
cd /root/availai
PYTHONPATH=/root/availai python scripts/validate_001_against_chain.py \
  --baseline alembic/versions/001_initial_schema.py.draft \
  --report scripts/validate_001_against_chain.last_run.txt
```
Expected: exit code 0 (no gaps) OR exit code 1 with a gap report. Both are valid intermediate states — exit-1 is the expected starting state per the spec.

- [ ] **Step 2: For each gap in the report, triage manually**

Read every gap entry in `scripts/validate_001_against_chain.last_run.txt`. For each:
1. Open the offending migration file (`alembic/versions/<name>.py`).
2. Read its `upgrade()` to understand what schema state it expected.
3. Decide: does 001 need to add the missing table/column? (Almost always yes — the gap means 001 doesn't have something that existed in Feb 2026 and was already being modified by later migrations.)
4. **Hard rule (from spec):** never auto-add a column based purely on a later `op.add_column` "expecting" it. The later migration may be re-creating it under a different name, or the column may have been renamed before 002. Read context first.
5. Patch `alembic/versions/001_initial_schema.py.draft` directly — add the missing table or column to the relevant `op.create_table()` call. Match the column's apparent type by inspecting the corresponding `app/models/` definition at the time the migration was written (`git log --oneline -- alembic/versions/<that_migration>.py` to find the commit, then `git show <commit>:app/models/...`).

- [ ] **Step 3: Re-run the validator after each batch of patches**

```bash
PYTHONPATH=/root/availai python scripts/validate_001_against_chain.py \
  --baseline alembic/versions/001_initial_schema.py.draft \
  --report scripts/validate_001_against_chain.last_run.txt
```
Repeat Step 2 for any remaining gaps.

- [ ] **Step 4: Confirm a clean run**

```bash
cat scripts/validate_001_against_chain.last_run.txt
```
Expected: trailing line `found 0 gap(s)`. The walked count should match the number of revisions after 001 (130 - 1 = 129, give or take if the chain count drifts).

If gaps persist after multiple iterations and they appear semantic (e.g., a migration referencing a table that was *intentionally* removed by an earlier-in-chain migration), that is a finding — the chain itself has internal inconsistency. Document each such gap as a known-irreducible item in the report and proceed; the round-trip CI test will confirm the chain still applies cleanly.

- [ ] **Step 5: Commit the clean validator output**

```bash
git add scripts/validate_001_against_chain.last_run.txt
git commit -m "$(cat <<'EOF'
docs(alembic): commit 001 validator clean-run evidence

scripts/validate_001_against_chain.last_run.txt — output of the validator
against the draft 001 after iterative patching. Reviewers can re-run
scripts/validate_001_against_chain.py to reproduce.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: Replace the live 001 with the validated draft

**Files:**
- Replace: `alembic/versions/001_initial_schema.py`

- [ ] **Step 1: Move the draft over the real file**

```bash
mv alembic/versions/001_initial_schema.py.draft alembic/versions/001_initial_schema.py
```
Verify:
```bash
git status --short alembic/versions/001_initial_schema.py
head -30 alembic/versions/001_initial_schema.py
wc -l alembic/versions/001_initial_schema.py
```
Expected: file is modified (not new); revision header still says `revision: str = "001_initial"`; line count ~1500–3000.

- [ ] **Step 2: Sanity-check by re-running the validator against the live 001**

```bash
PYTHONPATH=/root/availai python scripts/validate_001_against_chain.py \
  --report scripts/validate_001_against_chain.last_run.txt
```
Expected: exit 0, `found 0 gap(s)`.

- [ ] **Step 3: Update the committed evidence file**

```bash
git diff scripts/validate_001_against_chain.last_run.txt
```
If it changed (because the run happened against `--baseline` default = live 001 instead of the draft), stage the update:
```bash
git add scripts/validate_001_against_chain.last_run.txt
```

- [ ] **Step 4: Lint + format the new 001**

```bash
ruff check alembic/versions/001_initial_schema.py
ruff format --check alembic/versions/001_initial_schema.py
```
Auto-fix as needed: `ruff format alembic/versions/001_initial_schema.py`. Re-stage.

- [ ] **Step 5: Commit the rewritten 001**

```bash
git add alembic/versions/001_initial_schema.py scripts/validate_001_against_chain.last_run.txt
git commit -m "$(cat <<'EOF'
fix(alembic): rewrite 001 baseline as explicit DDL snapshot

Replaces the 39-line Base.metadata.create_all() body with explicit
op.create_table() / op.create_index() / op.create_foreign_key() calls
covering today's app.models tables plus the historical tables/columns
that migrations 002+ still reference (buy_plans, error_reports,
trouble_tickets, inventory_snapshots, material_card_audit, plus a
handful of since-removed columns). Generated by
scripts/reconstruct_001_baseline.py (Base.metadata.create_all from
live models against ephemeral postgres) and validated against all
later migrations by scripts/validate_001_against_chain.py.

Same revision = "001_initial" — production stays stamped at HEAD,
the new upgrade() body never runs there. Fresh-DB-only semantics.
Resolves the create_all-baseline drift causing CI red on main
(DuplicateTable on ix_vr_scanned_by, UndefinedTable on buy_plans/error_reports).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 8: Wire the metadata-diff into CI

**Files:**
- Modify: `.github/workflows/ci.yml` (the existing `Alembic upgrade/downgrade smoke test` step around line 101)

- [ ] **Step 1: Read the current CI step**

```bash
sed -n '101,115p' /root/availai/.github/workflows/ci.yml
```
Expected: a step named `Alembic upgrade/downgrade smoke test` running `alembic upgrade head`, `alembic downgrade base`, `alembic upgrade head`.

- [ ] **Step 2: Edit the workflow to add the diff check between upgrade and downgrade**

Replace the existing step body with:

```yaml
      - name: Alembic upgrade/downgrade/diff smoke test
        env:
          DATABASE_URL: postgresql://availai:availai@localhost:5432/availai
          SECRET_KEY: ci-secret-key
          SESSION_SECRET: ci-session-secret
          TESTING: "0"
        run: |
          set -e
          alembic upgrade head
          python scripts/check_schema_matches_models.py
          alembic downgrade base
          alembic upgrade head
```

The `set -e` makes the step fail on the first non-zero exit. Renaming the step makes the gate visible in CI logs.

- [ ] **Step 3: Run the workflow file through yamllint to catch syntax issues**

```bash
python -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml'))"
```
Expected: no output (parse OK).

- [ ] **Step 4: Verify locally that `check_schema_matches_models.py` runs against a real DB**

This requires a live local Postgres with the chain applied. Skip if not feasible in your environment; the CI job will exercise it on push.

```bash
# In a worktree where docker compose is up:
docker compose exec -T app alembic upgrade head
docker compose exec -T -e DATABASE_URL='postgresql://availai:availai@db:5432/availai' app python scripts/check_schema_matches_models.py
```
Expected: `Schema matches Base.metadata.` and exit 0.

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "$(cat <<'EOF'
ci: extend alembic smoke test with Base.metadata diff check

Inserts scripts/check_schema_matches_models.py between alembic upgrade head
and alembic downgrade base. Catches model/migration drift in addition to
'chain doesn't apply' failures. Closes the regression-detector loop the
no-band-aids rule depends on.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 9: Local round-trip-plus-diff smoke test

**Files:**
- None modified — verification only.

- [ ] **Step 1: Bring db down clean, then back up**

```bash
docker compose down -v
docker compose up -d db
# wait for ready
for i in 1 2 3 4 5 6 7 8 9 10; do
  docker compose exec -T db pg_isready -U availai -d availai && break
  sleep 1
done
```
Expected: `db ... accepting connections`.

- [ ] **Step 2: Run the CI smoke sequence locally inside the app container**

```bash
docker compose run --rm \
  -e DATABASE_URL='postgresql://availai:availai@db:5432/availai' \
  -e SECRET_KEY=local-test \
  -e SESSION_SECRET=local-test \
  -e TESTING=0 \
  app bash -lc 'set -e
    alembic upgrade head
    python scripts/check_schema_matches_models.py
    alembic downgrade base
    alembic upgrade head'
```
Expected: each command exits 0, the diff script prints `Schema matches Base.metadata.`, and the final `upgrade head` completes without errors. Failure modes per the spec's Error handling table:
- Failure at `upgrade head` (step 1): 001 still missing a table or column → return to Task 6 with the new gap.
- Failure at the diff script: model/migration drift somewhere — likely fixable by adding/removing in 001, or in a follow-up PR if the drift is in 002+ (use the spec's `xfail` escape-hatch in that case).
- Failure at `downgrade base`: asymmetric drop somewhere → if in 001 fix here; if in 002+, see spec's xfail rule.
- Failure at re-`upgrade head`: non-idempotent leftover (orphan ENUM, sequence) → likely fix is explicit `DROP TYPE` in 001's downgrade.

- [ ] **Step 3: Tear down the local containers**

```bash
docker compose down -v
```

- [ ] **Step 4: No commit — verification only.**

---

### Task 10: Full pytest pass

**Files:**
- None modified — verification only.

- [ ] **Step 1: Run the full test suite**

```bash
cd /root/availai
TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v --tb=short
```
Expected: all tests pass except the 6 known pre-existing failures tracked in the pre-rollout-checklist tech-debt register. **No new failures.** If a new test fails, investigate before proceeding.

- [ ] **Step 2: If any new failure appears, diagnose**

The 001 rewrite is pure DDL — no Python application code paths should regress. A new failure here means either:
- The new pytest scripts (Tasks 2/3/4) have bugs → fix them.
- A test was implicitly relying on `Base.metadata.create_all()` running through 001 (unlikely; conftest uses SQLite directly via `Base.metadata.create_all(engine)`).

Fix and rerun until clean.

- [ ] **Step 3: No commit — verification only.**

---

### Task 11: Pre-commit pass

**Files:**
- None modified — verification only.

- [ ] **Step 1: Run pre-commit against all files (per `feedback_pre_commit_all_files`)**

```bash
cd /root/availai
pre-commit run --all-files
```
Expected: all hooks pass. The "mypy reporting Failed — files modified" cosmetic quirk from prior memory is acceptable iff mypy's actual stdout says `Success: no issues found`.

- [ ] **Step 2: Address any real failures**

For each failure:
- Read the hook output.
- Fix in the offending file.
- Re-run `pre-commit run --all-files`.

If the failure is unrelated to this PR (existing tech debt), note it in the PR description rather than fixing — keep PR scope tight.

- [ ] **Step 3: No commit — verification only. (Pre-commit fixes that produce file changes ARE committed; see Step 2.)**

---

### Task 12: Confirm CVE-bump commit is on the branch

**Files:**
- `requirements.txt` — modifications already in working tree from prior session, not yet committed.

- [ ] **Step 1: Inspect working tree**

```bash
git status --short
git diff requirements.txt
```
Expected: `M requirements.txt` with the diff:
```
-python-multipart==0.0.22
+python-multipart==0.0.26
-cryptography==46.0.5
+cryptography==46.0.7
```

- [ ] **Step 2: Commit the CVE bumps as their own commit**

```bash
git add requirements.txt
git commit -m "$(cat <<'EOF'
deps: bump cryptography 46.0.5→46.0.7, python-multipart 0.0.22→0.0.26

CVE-driven version bumps. Folded into this PR for cohesive 'unblock CI'
scope; behavior-neutral; covered by existing test suite.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 3: Confirm the working tree is clean of source changes (only the worktree gitlink remains)**

```bash
git status --short
```
Expected: only `D .claude/worktrees/agent-a6911920` remains (harmless; can be cleaned anytime).

---

### Task 13: Push branch and open PR

**Files:**
- None — git/GitHub operations only.

- [ ] **Step 1: Push the branch**

```bash
git push -u origin fix/ci-unblock-alembic-and-audit
```
Expected: push succeeds. If pre-push hooks fail, fix and re-push (do NOT use `--no-verify`).

- [ ] **Step 2: Open the PR**

```bash
gh pr create \
  --base main \
  --head fix/ci-unblock-alembic-and-audit \
  --title "fix(alembic): rewrite 001 as explicit DDL + add metadata-diff CI check" \
  --body "$(cat <<'EOF'
## Why

CI on `main` has been red for weeks. The visible symptom alternates between `DuplicateTable` on `ix_vr_scanned_by` (migration 003 on a fresh DB) and `UndefinedTable` on `buy_plans` / `error_reports` (migration 003 on other DB states).

Root cause: `alembic/versions/001_initial_schema.py` used `Base.metadata.create_all()` as its upgrade body. This violates CLAUDE.md absolute rule #2 and silently drifts with today's models — historical tables that 002+ migrations expect (`buy_plans` before its rename, `error_reports` before its restructure) vanish from the baseline.

## What changed

- `alembic/versions/001_initial_schema.py` — replaced 39 lines of `create_all()` with explicit `op.create_table()` / `op.create_index()` / `op.create_foreign_key()` calls. Symmetric `downgrade()`. Same `revision = "001_initial"` — **prod stays stamped at HEAD; the new upgrade body never runs there**.
- `scripts/reconstruct_001_baseline.py` — committed dev tool that regenerates the body from today's `app.models` via `Base.metadata.create_all()` against ephemeral Postgres + `pg_dump` (live-models strategy; see spec §"Reconstruction strategy revision").
- `scripts/validate_001_against_chain.py` — committed dev tool that walks every later migration and asserts every op.* references something the seeded model contains.
- `scripts/validate_001_against_chain.last_run.txt` — clean validator output (proof the chain references resolve).
- `scripts/check_schema_matches_models.py` — committed dev + CI tool that runs `alembic.autogenerate.compare_metadata` against `Base.metadata` and exits non-zero on drift.
- `.github/workflows/ci.yml` — extended the `Alembic upgrade/downgrade smoke test` step to invoke the diff check between `upgrade head` and `downgrade base`.
- `requirements.txt` — CVE bumps (`cryptography 46.0.5→46.0.7`, `python-multipart 0.0.22→0.0.26`) folded into this PR for cohesive scope.

## Reconstruction provenance

- Strategy: live-models seeding (`Base.metadata.create_all()` from today's `app.models`) + forward-chain validation of every later migration. Historical tables/columns no longer in live models are added back during gap triage (Task 6 Step 2). See spec §"Reconstruction strategy revision".
- See spec: `docs/superpowers/specs/2026-04-30-alembic-001-baseline-rewrite-design.md`.
- Validator output: `scripts/validate_001_against_chain.last_run.txt` (re-runnable via `python scripts/validate_001_against_chain.py`).

## Compatibility contract

- Prod is currently stamped at HEAD (verified pre-merge via `alembic current`). The new `upgrade()` body never executes there.
- Any DB stamped at any revision ≥ `001_initial` is unaffected — `alembic_version` is not modified.
- Only fresh DBs (CI, new dev sandboxes, future SFDC staging) execute the new explicit DDL.

## Out of scope (deferred follow-ups)

- Migration 049 audit (also contains `if_not_exists` patterns) — handled in a follow-up if the new metadata-diff CI step flags it.
- Sweep of other historical migrations for similar band-aid patterns.

## Test plan

- [ ] CI: round trip + diff smoke (gating)
- [ ] Local: `docker compose down -v && docker compose up -d db && docker compose run --rm app bash -lc 'alembic upgrade head && python scripts/check_schema_matches_models.py && alembic downgrade base && alembic upgrade head'`
- [ ] Pre-merge: `ssh prod 'docker compose exec app alembic current'` → capture output
- [ ] Post-merge: same command on prod → diff against pre-merge → must be empty.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```
Expected: `gh pr create` returns a PR URL. Save it.

- [ ] **Step 3: Verify CI starts and passes on the PR**

```bash
gh pr checks
```
Watch the `Alembic upgrade/downgrade/diff smoke test` step — it must turn green. If it fails, this is a real signal: either 001 is still wrong, or there's drift in 002+ that needs the spec's `xfail` escape-hatch (with a tracked follow-up issue). Do NOT band-aid 001 to mask the failure.

- [ ] **Step 4: Run the full pr-review-toolkit per CLAUDE.md "PR Reviews" section**

After CI is green, before requesting human review:

```bash
# In sequence (or invoke them one-by-one through the agents in your environment):
# - pr-review-toolkit:comment-analyzer
# - pr-review-toolkit:pr-test-analyzer
# - pr-review-toolkit:type-design-analyzer
# - pr-review-toolkit:silent-failure-hunter
# - pr-review-toolkit:code-simplifier
# - pr-review-toolkit:code-reviewer
# - feature-dev:code-reviewer
```
Per CLAUDE.md: "Fix ALL review findings immediately — never defer as 'lower priority' or 'MVP acceptable'."

- [ ] **Step 5: PR description points to the spec for design context**

Verify the PR body links the spec at `docs/superpowers/specs/2026-04-30-alembic-001-baseline-rewrite-design.md`. Edit the PR description (`gh pr edit`) if missing.

---

### Task 14: Verify CI green, merge, capture post-merge prod alembic current

**Files:**
- None modified — git/GitHub operations + a prod read-only command.

- [ ] **Step 1: Wait for CI green on the PR**

```bash
cd /root/availai
gh pr checks --watch
```
Expected: every required check passes — most importantly `Alembic upgrade/downgrade/diff smoke test` (the renamed step from Task 8). If anything fails, return to the appropriate earlier task. Do NOT merge red.

- [ ] **Step 2: Address all pr-review-toolkit findings (re-confirm Task 13 step 4 is done)**

Per CLAUDE.md "PR Reviews": every finding fixed before merge. If a finding is genuinely a false positive, document why in a PR comment.

- [ ] **Step 3: Squash-merge**

```bash
cd /root/availai
gh pr merge --squash --auto
```
Squash keeps `main`'s history clean — the per-iteration commits during the gap-fixing loop (Task 6) are noisy.

- [ ] **Step 4: Pull latest `main` locally**

```bash
git checkout main && git pull origin main
git log --oneline -3
```

- [ ] **Step 5: Capture post-merge prod `alembic current` and verify byte-identical to pre-merge**

After the deploy hook ships the merge to prod (typically <5 minutes after squash):

```bash
ssh root@app.availai.net "cd /root/availai && docker compose exec -T app alembic current 2>&1 | tail -5"
```
Diff against the pre-merge capture from Task 1 step 4. Must be byte-identical. The new 001's revision ID is unchanged, so the alembic_version table cannot have changed.

- [ ] **Step 6: Post the verification result as a PR comment**

```bash
gh pr comment <PR_NUMBER> --body "Pre/post-merge prod \`alembic current\` byte-identical. (pre: <pre>; post: <post>)"
```
Substitute the actual values. This is the audit trail the spec's post-merge gate requires.

If the two outputs differ — STOP. Do not merge anything else. Investigate before continuing. The new 001 should never have run on prod, so a different revision ID would mean alembic mis-applied something.

---

### Task 15: Phase 4 unblock — rebase and merge stacked PRs

**Files:**
- None modified in this repo (each PR's branch has its own changes; this task only does merge-queue work).

**Rationale:** With main CI green again, the 6 Phase 4 PRs (#92, #93, #94, #96, #97, #98) can finally land. Merge order: **#96 first** (it carries test-assertion updates other PRs depend on), then the rest in any order. Each rebase + push + wait-for-green + merge is a checkpoint.

- [ ] **Step 1: Confirm the open Phase 4 PRs and their states**

```bash
cd /root/availai
gh pr list --state open --json number,title,headRefName,mergeable --limit 30
```

Expected: at minimum #92, #93, #94, #96, #97, #98 OPEN. Note: #95 (`fix/requisitions2-shell-chrome`) is intentionally being left open — it gets closed when the shell-arch brainstorm resumes, NOT here. Skip it.

- [ ] **Step 2: Rebase + merge #96 first**

```bash
cd /root/availai
gh pr checkout 96
git rebase origin/main
```
If conflicts appear: most likely in `tests/test_alembic.py` (the 001 PR inverted assertions there). Take both changes — Task 11 of THIS plan added the inversion (in #96 already? confirm by reading), and #96's other test-assertion updates should be unaffected.

```bash
git push --force-with-lease
gh pr checks --watch
gh pr merge --squash --auto
```

- [ ] **Step 3: Pull main, then rebase+merge each remaining Phase 4 PR (#92, #93, #94, #97, #98)**

For each `<NUMBER>`:

```bash
cd /root/availai
git checkout main && git pull origin main
gh pr checkout <NUMBER>
git rebase origin/main
git push --force-with-lease
gh pr checks --watch
gh pr merge --squash --auto
```

If any PR's CI fails AFTER the rebase, halt — it likely surfaced a real interaction with the new 001 or with a previously-merged Phase 4 PR. Diagnose before continuing the queue. Do not band-aid.

- [ ] **Step 4: Verify all 6 Phase 4 PRs are merged**

```bash
cd /root/availai
gh pr list --state open --json number,title --limit 30
```

Expected: zero of #92/#93/#94/#96/#97/#98 in the list. Only #95 (and any new ones opened since this plan was written) remain.

- [ ] **Step 5: Confirm main CI green after the cascade**

```bash
cd /root/availai
gh run list --branch main --limit 5
```

Expected: most recent run is green. If not — diagnose; the Phase 4 PRs are mostly cleanup/docs/lint with no runtime impact, so a failure here is a regression worth investigating.

- [ ] **Step 6: No commit — this task is pure merge-queue work.**

---

## Self-Review

**Spec coverage:**
- ✅ "Rewrite 001 as explicit DDL" → Tasks 5, 6, 7
- ✅ "Live-models + chain validator reconstruction strategy" → Tasks 3, 4 (tooling) + Tasks 5, 6 (execution)
- ✅ "Symmetric reverse drops" → reconstruct script's `downgrade()` emitter (Task 3 step 3)
- ✅ "Round trip + Base.metadata diff smoke test" → Task 8 (CI wiring) + Task 2 (the diff script)
- ✅ "Fresh-DB only, prod stamped at HEAD untouched" → Task 1 step 4 (pre-merge check) + Task 13 step 2 (PR body) + post-merge verification noted in PR test plan
- ✅ "scripts/reconstruct_001_baseline.py committed" → Task 3
- ✅ "scripts/validate_001_against_chain.py committed" → Task 4
- ✅ "scripts/validate_001_against_chain.last_run.txt committed" → Task 6 step 5
- ✅ "CVE bumps folded into PR" → Task 12
- ✅ "049 deferred" → PR body "Out of scope"; Task 9 step 2 escape-hatch reference
- ✅ "xfail escape hatch" — referenced in Task 9 step 2 and Task 13 step 3

**Spec deviation called out at the top:** test-file path is `scripts/check_schema_matches_models.py`, not `tests/test_alembic_round_trip.py`. Reason: conftest.py overrides `DATABASE_URL=sqlite://`. Functionally identical CI guarantee.

**Sequencing & Phase 4 unblock:** Tasks 14–15 cover the post-merge prod-current verification and the rebase-and-merge cascade for the 6 stacked Phase 4 PRs (#92, #93, #94, #96, #97, #98 — #96 first per memory, then any order). The spec's "Hard gates / sequencing" rules (no merging Phase 4 until the 001 PR lands and main CI is green) map to Task 14 step 1 (CI green gate) → Task 14 step 3 (merge) → Task 15 (Phase 4 cascade).

**Placeholder scan:** none. No TBD/TODO/XXX/FIXME left in plan steps. (The reconstruction script's emitter does include literal `FIXME unmapped` for unmapped pg_types — that's a self-flagging mechanism, not a plan placeholder; Task 5 step 4 handles it.)

**Type consistency:**
- `SchemaModel` methods (`add_table`, `drop_table`, `add_column`, `drop_column`, `has_table`, `has_column`, `rename_table`) used consistently across Task 4 tests and `walk_migration_ops` body.
- `Gap` dataclass used consistently.
- `Table`, `Column`, `Index`, `ForeignKey` dataclasses defined and used consistently in Task 3.
- `parse_pg_dump`, `render_op_create_table`, `render_op_create_index` signatures match between tests and impl.
- `filter_allowlist`, `format_diffs` signatures match between tests and impl.

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-04-30-alembic-001-baseline-rewrite.md`.**

Two execution options:

1. **Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration. Good for this plan because Tasks 2/3/4 are well-isolated TDD blocks, and Tasks 5/6 are the iterative reconstruction loop where between-task review catches drift early.

2. **Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?
