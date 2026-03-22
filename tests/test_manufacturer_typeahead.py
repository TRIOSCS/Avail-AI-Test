"""tests/test_manufacturer_typeahead.py — Tests for manufacturer typeahead endpoints.

Tests GET /v2/partials/manufacturers/search and POST /v2/partials/manufacturers/add.

Called by: pytest
Depends on: conftest.py (client, db_session fixtures), app.models.sourcing.Manufacturer
"""

from app.models.sourcing import Manufacturer


def test_search_by_canonical_name(client, db_session):
    db_session.add(Manufacturer(canonical_name="Texas Instruments", aliases=["TI"]))
    db_session.commit()
    resp = client.get("/v2/partials/manufacturers/search?q=texas")
    assert resp.status_code == 200
    assert "Texas Instruments" in resp.text


def test_search_by_alias(client, db_session):
    db_session.add(Manufacturer(canonical_name="Texas Instruments", aliases=["TI"]))
    db_session.commit()
    resp = client.get("/v2/partials/manufacturers/search?q=TI")
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
