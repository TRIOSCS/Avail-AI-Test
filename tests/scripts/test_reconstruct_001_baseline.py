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


# ── FK separation regression tests ──
#
# These cover the bug class where 001 fails to apply because cross-table FK
# constraints are inlined in op.create_table. With 87 tables created in one
# migration, an inline FK pointing at a table that comes later alphabetically
# blows up with 'relation "X" does not exist'. The fix: emit cross-table FKs
# as separate op.create_foreign_key calls AFTER all create_tables. Self-refs
# stay inline (the table exists by the time the FK is checked).


def test_render_op_create_table_strips_cross_table_fks():
    """Cross-table FKs must NOT appear inline — they're emitted separately by
    render_upgrade_body via op.create_foreign_key calls."""
    from scripts.reconstruct_001_baseline import Column, ForeignKey, Table

    t = Table(
        name="orders",
        columns=[
            Column(name="id", py_type="sa.Integer()", nullable=False),
            Column(name="user_id", py_type="sa.Integer()", nullable=True),
        ],
        primary_key=["id"],
        foreign_keys=[
            ForeignKey(
                local_columns=["user_id"],
                referenced_table="users",  # cross-table
                referenced_columns=["id"],
                name="orders_user_id_fkey",
            ),
        ],
    )
    out = render_op_create_table(t)
    assert "ForeignKeyConstraint" not in out, (
        "cross-table FK must NOT be inline; expected it to be emitted as a separate "
        "op.create_foreign_key call. Got:\n" + out
    )
    assert "orders_user_id_fkey" not in out


def test_render_op_create_table_keeps_self_reference_fks_inline():
    """Self-reference FKs (parent_id → self.id) DO stay inline — the table exists by the
    time the FK constraint is checked."""
    from scripts.reconstruct_001_baseline import Column, ForeignKey, Table

    t = Table(
        name="categories",
        columns=[
            Column(name="id", py_type="sa.Integer()", nullable=False),
            Column(name="parent_id", py_type="sa.Integer()", nullable=True),
        ],
        primary_key=["id"],
        foreign_keys=[
            ForeignKey(
                local_columns=["parent_id"],
                referenced_table="categories",  # self-reference
                referenced_columns=["id"],
                name="categories_parent_id_fkey",
            ),
        ],
    )
    out = render_op_create_table(t)
    assert "ForeignKeyConstraint" in out, "self-reference FK must stay inline; got:\n" + out
    assert "categories_parent_id_fkey" in out


def test_render_op_create_foreign_key_emits_alembic_signature():
    """Each cross-table FK becomes a single op.create_foreign_key call in alembic's
    positional signature: (name, src, ref, [src_cols], [ref_cols])."""
    from scripts.reconstruct_001_baseline import ForeignKey, render_op_create_foreign_key

    fk = ForeignKey(
        local_columns=["user_id"],
        referenced_table="users",
        referenced_columns=["id"],
        name="orders_user_id_fkey",
    )
    out = render_op_create_foreign_key("orders", fk)
    assert out == ("op.create_foreign_key('orders_user_id_fkey', 'orders', 'users', ['user_id'], ['id'])")


def test_render_upgrade_body_emits_create_table_then_index_then_fk():
    """The upgrade body must emit all create_table calls first, then all create_index
    calls, then all create_foreign_key calls.

    This ordering is what makes the FK separation safe — every referenced table exists
    by the time op.create_foreign_key runs.
    """
    from scripts.reconstruct_001_baseline import (
        Column,
        ForeignKey,
        Index,
        ParseResult,
        Table,
        render_upgrade_body,
    )

    parsed = ParseResult(
        tables=[
            Table(
                name="users",
                columns=[Column(name="id", py_type="sa.Integer()", nullable=False)],
                primary_key=["id"],
            ),
            Table(
                name="orders",
                columns=[
                    Column(name="id", py_type="sa.Integer()", nullable=False),
                    Column(name="user_id", py_type="sa.Integer()", nullable=True),
                ],
                primary_key=["id"],
                foreign_keys=[
                    ForeignKey(
                        local_columns=["user_id"],
                        referenced_table="users",
                        referenced_columns=["id"],
                        name="orders_user_id_fkey",
                    )
                ],
            ),
        ],
        indexes=[Index(name="ix_orders_user", table="orders", columns=["user_id"], unique=False)],
    )
    body = render_upgrade_body(parsed)
    create_table_idx = [i for i, line in enumerate(body) if "op.create_table(" in line]
    create_index_idx = [i for i, line in enumerate(body) if "op.create_index(" in line]
    create_fk_idx = [i for i, line in enumerate(body) if "op.create_foreign_key(" in line]
    assert len(create_table_idx) == 2
    assert len(create_index_idx) == 1
    assert len(create_fk_idx) == 1
    assert max(create_table_idx) < min(create_index_idx), "all create_table must precede any create_index"
    assert max(create_index_idx) < min(create_fk_idx), "all create_index must precede any create_foreign_key"


def test_render_downgrade_body_emits_drop_constraint_before_drop_table():
    """Downgrade must drop FK constraints BEFORE dropping either the source or the
    referenced table — otherwise PostgreSQL refuses to drop a table whose columns are
    referenced by an active FK."""
    from scripts.reconstruct_001_baseline import (
        Column,
        ForeignKey,
        ParseResult,
        Table,
        render_downgrade_body,
    )

    parsed = ParseResult(
        tables=[
            Table(
                name="users",
                columns=[Column(name="id", py_type="sa.Integer()", nullable=False)],
                primary_key=["id"],
            ),
            Table(
                name="orders",
                columns=[
                    Column(name="id", py_type="sa.Integer()", nullable=False),
                    Column(name="user_id", py_type="sa.Integer()", nullable=True),
                ],
                primary_key=["id"],
                foreign_keys=[
                    ForeignKey(
                        local_columns=["user_id"],
                        referenced_table="users",
                        referenced_columns=["id"],
                        name="orders_user_id_fkey",
                    )
                ],
            ),
        ],
        indexes=[],
    )
    body = render_downgrade_body(parsed)
    drop_fk_idx = [i for i, line in enumerate(body) if "op.drop_constraint(" in line and "orders_user_id_fkey" in line]
    drop_table_orders_idx = [i for i, line in enumerate(body) if "op.drop_table('orders')" in line]
    drop_table_users_idx = [i for i, line in enumerate(body) if "op.drop_table('users')" in line]
    assert len(drop_fk_idx) == 1
    assert len(drop_table_orders_idx) == 1
    assert len(drop_table_users_idx) == 1
    assert drop_fk_idx[0] < drop_table_orders_idx[0], (
        "drop_constraint(orders_user_id_fkey) must come before drop_table('orders')"
    )
    assert drop_fk_idx[0] < drop_table_users_idx[0], (
        "drop_constraint(orders_user_id_fkey) must come before drop_table('users') "
        "(the referenced table) since the FK pins both tables until dropped"
    )


def test_parse_pg_dump_extracts_on_delete_and_on_update():
    """Cascade clauses (ON DELETE / ON UPDATE) on real pg_dump-form FK statements must
    be captured into ForeignKey.ondelete / .onupdate. A FK with no cascade clause must
    produce ondelete=onupdate=None.

    Prod schema audit (2026-05-04): all 162 FKs in `Base.metadata.create_all()` output
    have explicit ON DELETE; none have ON UPDATE. Losing these clauses would silently
    downgrade prod referential semantics — see audit in the parser-fix turn for the
    breakdown.
    """
    sql = textwrap.dedent("""
    CREATE TABLE public.users (id integer NOT NULL);
    CREATE TABLE public.orders (id integer NOT NULL, user_id integer);
    CREATE TABLE public.audit (id integer NOT NULL, user_id integer);
    CREATE TABLE public.session (id integer NOT NULL, user_id integer);

    ALTER TABLE ONLY public.orders
        ADD CONSTRAINT orders_user_id_fkey
        FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;

    ALTER TABLE ONLY public.audit
        ADD CONSTRAINT audit_user_id_fkey
        FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE RESTRICT ON UPDATE CASCADE;

    ALTER TABLE ONLY public.session
        ADD CONSTRAINT session_user_id_fkey
        FOREIGN KEY (user_id) REFERENCES public.users(id);
    """)
    result = parse_pg_dump(sql)
    by_name = {t.name: t for t in result.tables}

    orders_fk = by_name["orders"].foreign_keys[0]
    assert orders_fk.ondelete == "CASCADE"
    assert orders_fk.onupdate is None

    audit_fk = by_name["audit"].foreign_keys[0]
    assert audit_fk.ondelete == "RESTRICT"
    assert audit_fk.onupdate == "CASCADE"

    session_fk = by_name["session"].foreign_keys[0]
    assert session_fk.ondelete is None, (
        "FK with no ON DELETE clause must produce None, not the literal "
        "'NO ACTION' — alembic's convention is to leave the kwarg unset for "
        "default behavior"
    )
    assert session_fk.onupdate is None


def test_render_op_create_foreign_key_emits_ondelete_and_onupdate_kwargs():
    """When a parsed FK has ondelete or onupdate set, the rendered op.create_foreign_key
    call must include them as kwargs in alembic's expected positional+kwarg form.

    When unset, no kwarg is emitted (so alembic keeps its default NO ACTION).
    """
    from scripts.reconstruct_001_baseline import ForeignKey, render_op_create_foreign_key

    fk_with_cascade = ForeignKey(
        local_columns=["user_id"],
        referenced_table="users",
        referenced_columns=["id"],
        name="orders_user_id_fkey",
        ondelete="CASCADE",
    )
    out = render_op_create_foreign_key("orders", fk_with_cascade)
    assert out == (
        "op.create_foreign_key('orders_user_id_fkey', 'orders', 'users', ['user_id'], ['id'], ondelete='CASCADE')"
    )

    fk_with_both = ForeignKey(
        local_columns=["user_id"],
        referenced_table="users",
        referenced_columns=["id"],
        name="audit_user_id_fkey",
        ondelete="RESTRICT",
        onupdate="CASCADE",
    )
    out = render_op_create_foreign_key("audit", fk_with_both)
    assert out == (
        "op.create_foreign_key('audit_user_id_fkey', 'audit', 'users', "
        "['user_id'], ['id'], ondelete='RESTRICT', onupdate='CASCADE')"
    )

    fk_default = ForeignKey(
        local_columns=["user_id"],
        referenced_table="users",
        referenced_columns=["id"],
        name="session_user_id_fkey",
    )
    out = render_op_create_foreign_key("session", fk_default)
    assert out == ("op.create_foreign_key('session_user_id_fkey', 'session', 'users', ['user_id'], ['id'])"), (
        "FK without cascade kwargs must produce a bare positional-only call"
    )


def test_render_op_create_table_self_reference_preserves_ondelete():
    """Self-reference FKs stay inline (covered by another test).

    When the
    self-ref carries ondelete/onupdate, those clauses must be preserved in
    the inline sa.ForeignKeyConstraint(...) — otherwise a categories tree
    that uses ON DELETE SET NULL on its parent_id would silently change to
    NO ACTION on regeneration.
    """
    from scripts.reconstruct_001_baseline import Column, ForeignKey, Table

    t = Table(
        name="categories",
        columns=[
            Column(name="id", py_type="sa.Integer()", nullable=False),
            Column(name="parent_id", py_type="sa.Integer()", nullable=True),
        ],
        primary_key=["id"],
        foreign_keys=[
            ForeignKey(
                local_columns=["parent_id"],
                referenced_table="categories",
                referenced_columns=["id"],
                name="categories_parent_id_fkey",
                ondelete="SET NULL",
            ),
        ],
    )
    out = render_op_create_table(t)
    assert "ForeignKeyConstraint" in out, "self-ref must stay inline"
    assert "ondelete='SET NULL'" in out, "self-ref ondelete must be preserved; got:\n" + out


def test_render_downgrade_body_omits_drop_constraint_for_self_reference():
    """Self-reference FKs are inline in create_table, so they get dropped automatically
    when their table is dropped — no separate drop_constraint needed."""
    from scripts.reconstruct_001_baseline import (
        Column,
        ForeignKey,
        ParseResult,
        Table,
        render_downgrade_body,
    )

    parsed = ParseResult(
        tables=[
            Table(
                name="categories",
                columns=[
                    Column(name="id", py_type="sa.Integer()", nullable=False),
                    Column(name="parent_id", py_type="sa.Integer()", nullable=True),
                ],
                primary_key=["id"],
                foreign_keys=[
                    ForeignKey(
                        local_columns=["parent_id"],
                        referenced_table="categories",
                        referenced_columns=["id"],
                        name="categories_parent_id_fkey",
                    )
                ],
            ),
        ],
        indexes=[],
    )
    body = render_downgrade_body(parsed)
    assert not any("op.drop_constraint(" in line and "categories_parent_id_fkey" in line for line in body), (
        "self-reference FK should not get a separate drop_constraint — it gets dropped with the table"
    )
