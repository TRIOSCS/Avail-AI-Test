"""test_ilike_wildcard_escaping.py — HIGH-SEC-3 regression tests.

A user-supplied search term that contains the SQL LIKE wildcards ``%`` / ``_``
(or the escape char ``\\``) must match those characters *literally*, never as
wildcards. That requires two things working together:
  1. the term is run through ``escape_like()`` before interpolation, and
  2. the ``.ilike(...)`` / ``.like(...)`` call passes ``escape="\\"`` so the
     backslash escaping is actually honoured by the database.

Both halves are exercised here at the DB level (in-memory SQLite, which — unlike
PostgreSQL — has *no* default LIKE escape char, so the missing ``escape=`` is a
real, observable bug). A control test pins the unescaped wildcard behaviour so
the value of the helper is documented.

Called by: pytest
Depends on: app/utils/sql_helpers.py, app/utils/search_builder.py, app/models/tags.py
"""

from pathlib import Path

from app.models.tags import Tag
from app.utils.search_builder import SearchBuilder


def _add_tags(db, names, tag_type="brand"):
    for n in names:
        db.add(Tag(name=n, tag_type=tag_type))
    db.commit()


def _search(db, term):
    """Run *term* through the shared SearchBuilder ILIKE path."""
    sb = SearchBuilder(term)
    return {t.name for t in db.query(Tag).filter(sb.ilike_filter(Tag.name)).all()}


# ── literal matching ────────────────────────────────────────────────────────


def test_percent_matches_literally_not_as_wildcard(db_session):
    # As a wildcard "100%" matches BOTH rows; escaped it matches only "100%".
    _add_tags(db_session, ["100% Cotton", "1000 Cotton"])
    assert _search(db_session, "100%") == {"100% Cotton"}


def test_underscore_matches_literally_not_as_wildcard(db_session):
    # "_" is a single-char wildcard; escaped it must match only the literal "_".
    _add_tags(db_session, ["A_B Widget", "AXB Widget"])
    assert _search(db_session, "A_B") == {"A_B Widget"}


def test_backslash_matches_literally(db_session):
    _add_tags(db_session, [r"path\to", "pathXto"])
    assert _search(db_session, r"path\to") == {r"path\to"}


# ── normal-term behaviour is unchanged ──────────────────────────────────────


def test_normal_term_still_matches(db_session):
    _add_tags(db_session, ["100% Cotton", "1000 Cotton"])
    assert _search(db_session, "Cotton") == {"100% Cotton", "1000 Cotton"}


def test_normal_term_no_false_positive(db_session):
    _add_tags(db_session, ["Resistor", "Capacitor"])
    assert _search(db_session, "Resist") == {"Resistor"}


# ── control: documents the unescaped (buggy) wildcard semantics ─────────────


def test_raw_unescaped_underscore_acts_as_wildcard(db_session):
    """Without escaping, ``_`` matches any single char — the bug we fix."""
    _add_tags(db_session, ["A_B Widget", "AXB Widget"])
    raw = {t.name for t in db_session.query(Tag).filter(Tag.name.ilike("%A_B%")).all()}
    assert raw == {"A_B Widget", "AXB Widget"}


# ── regression guard for the shared helper ──────────────────────────────────


def test_search_builder_passes_escape_char():
    src = Path("app/utils/search_builder.py").read_text()
    assert 'escape="\\\\"' in src, (
        'SearchBuilder.ilike_filter must call .ilike(..., escape="\\\\") so '
        "escape_like()'s backslash escaping is honoured by the database."
    )
