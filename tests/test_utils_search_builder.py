"""tests/test_utils_search_builder.py — Tests for app/utils/search_builder.py and sql_helpers.py."""

import os

os.environ["TESTING"] = "1"

from sqlalchemy import Column, String, create_engine
from sqlalchemy.orm import DeclarativeBase, Session

from app.utils.search_builder import SearchBuilder
from app.utils.sql_helpers import escape_like


class TestEscapeLike:
    def test_plain_string_unchanged(self):
        assert escape_like("hello") == "hello"

    def test_percent_escaped(self):
        assert escape_like("50%") == r"50\%"

    def test_underscore_escaped(self):
        assert escape_like("a_b") == r"a\_b"

    def test_backslash_escaped(self):
        assert escape_like("a\\b") == "a\\\\b"

    def test_empty_string(self):
        assert escape_like("") == ""

    def test_multiple_special_chars(self):
        result = escape_like("100% complete_done")
        assert r"\%" in result
        assert r"\_" in result


class TestSearchBuilder:
    def setup_method(self):
        """Create an in-memory SQLite DB for testing."""

        self.engine = create_engine("sqlite:///:memory:")

        class Base(DeclarativeBase):
            pass

        class Item(Base):
            __tablename__ = "items"
            name = Column(String, primary_key=True)
            description = Column(String)

        Base.metadata.create_all(self.engine)
        self.Item = Item
        self.Base = Base

        with Session(self.engine) as session:
            session.add_all(
                [
                    Item(name="Resistor 100k", description="passive component"),
                    Item(name="Capacitor 10uF", description="bypass cap"),
                    Item(name="LM317T", description="voltage regulator"),
                ]
            )
            session.commit()

    def _session(self):
        return Session(self.engine)

    def test_ilike_filter_finds_match(self):
        sb = SearchBuilder("resistor")
        with self._session() as session:
            results = session.query(self.Item).filter(sb.ilike_filter(self.Item.name)).all()
            assert any("Resistor" in r.name for r in results)

    def test_ilike_filter_case_insensitive(self):
        sb = SearchBuilder("RESISTOR")
        with self._session() as session:
            results = session.query(self.Item).filter(sb.ilike_filter(self.Item.name)).all()
            assert len(results) >= 1

    def test_ilike_filter_no_match(self):
        sb = SearchBuilder("zzz_no_match")
        with self._session() as session:
            results = session.query(self.Item).filter(sb.ilike_filter(self.Item.name)).all()
            assert len(results) == 0

    def test_empty_query_returns_all(self):
        sb = SearchBuilder("")
        with self._session() as session:
            results = session.query(self.Item).filter(sb.ilike_filter(self.Item.name)).all()
            assert len(results) == 3  # All items

    def test_prefix_mode(self):
        sb = SearchBuilder("Res")
        with self._session() as session:
            results = session.query(self.Item).filter(sb.ilike_filter(self.Item.name, prefix=True)).all()
            assert any("Resistor" in r.name for r in results)

    def test_prefix_mode_no_suffix_match(self):
        sb = SearchBuilder("100k")
        with self._session() as session:
            results = session.query(self.Item).filter(sb.ilike_filter(self.Item.name, prefix=True)).all()
            # In prefix mode, "100k" won't match "Resistor 100k" from the start
            assert len(results) == 0

    def test_multi_column_search(self):
        sb = SearchBuilder("passive")
        with self._session() as session:
            results = session.query(self.Item).filter(sb.ilike_filter(self.Item.name, self.Item.description)).all()
            assert any("Resistor" in r.name for r in results)

    def test_fts_or_fallback_uses_ilike_in_sqlite(self):
        """SearchBuilder falls back to ILIKE for SQLite (no search_vector)."""
        sb = SearchBuilder("lm317")
        with self._session() as session:
            query = session.query(self.Item)
            result_query = sb.fts_or_fallback(query, self.Item, [self.Item.name])
            results = result_query.all()
            assert any("LM317T" in r.name for r in results)

    def test_fts_or_fallback_short_query_uses_ilike(self):
        """Queries shorter than min_len=3 skip FTS and use ILIKE."""
        sb = SearchBuilder("LM")
        with self._session() as session:
            query = session.query(self.Item)
            result_query = sb.fts_or_fallback(query, self.Item, [self.Item.name])
            results = result_query.all()
            assert any("LM317T" in r.name for r in results)

    def test_special_chars_escaped(self):
        sb = SearchBuilder("100%")
        # Should not crash — % is escaped
        with self._session() as session:
            results = session.query(self.Item).filter(sb.ilike_filter(self.Item.name)).all()
            assert isinstance(results, list)

    def test_fts_or_fallback_handles_programming_error(self):
        """SQLite raises ProgrammingError for tsquery — SearchBuilder falls back to ILIKE."""

        sb = SearchBuilder("LM317")

        # Add a fake search_vector attribute to trigger the FTS path
        class FakeModel:
            search_vector = self.Item.name
            name = self.Item.name

        with self._session() as session:
            query = session.query(self.Item)
            # SQLite doesn't support plainto_tsquery, so it will fall through
            # to ILIKE — but we patch the model to have search_vector
            result_query = sb.fts_or_fallback(query, FakeModel, [self.Item.name])
            results = result_query.all()
            # Should return results via ILIKE fallback
            assert isinstance(results, list)

    def test_empty_query_fts_fallback(self):
        """Empty query string returns all via ILIKE."""
        sb = SearchBuilder("")
        with self._session() as session:
            query = session.query(self.Item)
            result_query = sb.fts_or_fallback(query, self.Item, [self.Item.name])
            results = result_query.all()
            assert len(results) == 3  # All items
