"""tests/scripts/test_reconstruct_001_baseline.py — Tests for the SQL→Python DDL
translator inside scripts/reconstruct_001_baseline.py.

Called by: pytest
Depends on: scripts.reconstruct_001_baseline
"""

import textwrap

from scripts.reconstruct_001_baseline import (
    parse_pg_dump,
    render_op_create_index,
    render_op_create_table,
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
    from scripts.reconstruct_001_baseline import Column, Table

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
