"""tests/test_manufacturer_typeahead.py — Tests for manufacturer typeahead endpoints.

Tests GET /v2/partials/manufacturers/search and POST /v2/partials/manufacturers/add.

Called by: pytest
Depends on: conftest.py (client, db_session fixtures), app.models.sourcing.Manufacturer
"""

import pytest

from app.models.sourcing import Manufacturer


@pytest.mark.parametrize(
    "query",
    [
        pytest.param("texas", id="by_canonical_name"),
        pytest.param("TI", id="by_alias"),
    ],
)
def test_search_matches_manufacturer(client, db_session, query):
    db_session.add(Manufacturer(canonical_name="Texas Instruments", aliases=["TI"]))
    db_session.commit()
    resp = client.get(f"/v2/partials/manufacturers/search?q={query}")
    assert resp.status_code == 200
    assert "Texas Instruments" in resp.text


def test_search_no_match_shows_add(client, db_session):
    resp = client.get("/v2/partials/manufacturers/search?q=UnknownCorp")
    assert resp.status_code == 200
    assert "Add" in resp.text


def test_add_new_manufacturer(client, db_session):
    resp = client.post("/v2/partials/manufacturers/add", data={"name": "NewCorp"})
    assert resp.status_code == 200
    assert db_session.query(Manufacturer).filter_by(canonical_name="NewCorp").first() is not None


def test_search_empty_query_returns_empty(client, db_session):
    resp = client.get("/v2/partials/manufacturers/search?q=")
    assert resp.status_code == 200
    # Empty query returns empty results — no results and no "Add" prompt
    assert "Add" not in resp.text


def test_add_existing_manufacturer_no_duplicate(client, db_session):
    db_session.add(Manufacturer(canonical_name="Acme Corp"))
    db_session.commit()
    resp = client.post("/v2/partials/manufacturers/add", data={"name": "Acme Corp"})
    assert resp.status_code == 200
    count = db_session.query(Manufacturer).filter_by(canonical_name="Acme Corp").count()
    assert count == 1


def test_add_empty_name_returns_error(client, db_session):
    resp = client.post("/v2/partials/manufacturers/add", data={"name": "   "})
    assert resp.status_code == 200
    assert "required" in resp.text.lower()


def test_search_wildcard_percent_is_escaped(client, db_session):
    """HIGH-SEC-3: a bare '%' in the search term must be treated as a
    literal, not a SQL LIKE wildcard. Without escaping, q='%' would match
    every manufacturer."""
    db_session.add(Manufacturer(canonical_name="Texas Instruments"))
    db_session.add(Manufacturer(canonical_name="Analog Devices"))
    db_session.commit()
    resp = client.get("/v2/partials/manufacturers/search?q=%25")  # %25 == '%'
    assert resp.status_code == 200
    # '%' is now literal — it matches neither manufacturer name.
    assert "Texas Instruments" not in resp.text
    assert "Analog Devices" not in resp.text


def test_search_wildcard_underscore_is_escaped(client, db_session):
    """HIGH-SEC-3: a bare '_' must be a literal, not a single-char wildcard."""
    db_session.add(Manufacturer(canonical_name="ABC"))
    db_session.commit()
    resp = client.get("/v2/partials/manufacturers/search?q=A_C")
    assert resp.status_code == 200
    # '_' is literal — 'A_C' does not match 'ABC'.
    assert "ABC" not in resp.text


def test_search_literal_percent_in_name_matches(client, db_session):
    """A manufacturer whose name actually contains '%' is still found when the user
    searches for that literal '%'."""
    db_session.add(Manufacturer(canonical_name="Discount 50% Co"))
    db_session.commit()
    resp = client.get("/v2/partials/manufacturers/search?q=50%25")  # '50%'
    assert resp.status_code == 200
    assert "Discount 50% Co" in resp.text
