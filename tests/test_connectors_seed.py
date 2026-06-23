"""Tests for Task 2: catalog additions + prune of dead providers.
Task 4b: expands prune to all 9 dead names (aliexpress/arrow/avnet/partfuse/rs_components/
         siliconexpert/winsource/rocketreach_enrichment/clearbit_enrichment).
         Also adds 7 planned roadmap connectors to the catalog (durable + testable).

Verifies:
- api_sources.json contains ai_live_web + sam_gov_enrichment and NOT rocketreach/clearbit
- api_sources.json contains all 7 planned roadmap connectors
- seed_api_sources() prunes all 9 dead rows and leaves planned + real rows intact
- planned connectors seed into DB (NOT pruned)

Called by: pytest
Depends on: app/data/api_sources.json, app.startup.seed_api_sources, app.models.ApiSource
"""

import json
from unittest.mock import patch

from app.models import ApiSource

_ALL_PRUNE_NAMES = [
    "aliexpress",
    "arrow",
    "avnet",
    "partfuse",
    "rs_components",
    "siliconexpert",
    "winsource",
    "rocketreach_enrichment",
    "clearbit_enrichment",
]

# The 7 planned roadmap connectors that must exist in the catalog JSON.
_PLANNED_NAMES = [
    "findchips",
    "future",
    "heilind",
    "lcsc",
    "rochester",
    "thebrokersite",
    "verical",
]


def test_catalog_has_new_sources():
    """JSON catalog must include the two new providers and exclude the two dead ones."""
    import os

    cat_path = os.path.join(os.path.dirname(__file__), "..", "app", "data", "api_sources.json")
    cat = json.load(open(cat_path))
    names = {s["name"] for s in cat}
    assert "ai_live_web" in names, "ai_live_web missing from catalog"
    assert "sam_gov_enrichment" in names, "sam_gov_enrichment missing from catalog"
    assert "rocketreach_enrichment" not in names, "rocketreach_enrichment must NOT be in catalog"
    assert "clearbit_enrichment" not in names, "clearbit_enrichment must NOT be in catalog"


def test_catalog_has_all_7_planned_connectors():
    """JSON catalog must include all 7 planned roadmap connectors."""
    import os

    cat_path = os.path.join(os.path.dirname(__file__), "..", "app", "data", "api_sources.json")
    cat = json.load(open(cat_path))
    names = {s["name"] for s in cat}
    for planned in _PLANNED_NAMES:
        assert planned in names, f"Planned connector '{planned}' is missing from api_sources.json"


def test_planned_connectors_have_empty_env_vars():
    """Planned connectors must declare env_vars: [] (no credentials to configure)."""
    import os

    cat_path = os.path.join(os.path.dirname(__file__), "..", "app", "data", "api_sources.json")
    cat = json.load(open(cat_path))
    by_name = {s["name"]: s for s in cat}
    for planned in _PLANNED_NAMES:
        if planned not in by_name:
            continue  # covered by test_catalog_has_all_7_planned_connectors
        assert by_name[planned]["env_vars"] == [], (
            f"Planned connector '{planned}' must have env_vars: [] (no credentials)"
        )


def test_seed_prunes_dead_and_keeps_workers(db_session):
    """seed_api_sources() deletes rocketreach+clearbit rows, leaves icsource intact."""
    # Pre-seed a dead row and a browser-worker row
    db_session.add(
        ApiSource(
            name="rocketreach_enrichment",
            display_name="RocketReach",
            category="enrichment",
            source_type="enrichment",
            credentials={},
        )
    )
    db_session.add(
        ApiSource(
            name="icsource",
            display_name="IC Source",
            category="scraper",
            source_type="broker",
            credentials={},
        )
    )
    db_session.commit()

    # Patch SessionLocal so seed_api_sources uses the test session (not real PG)
    with (
        patch("app.startup.SessionLocal", return_value=db_session),
        patch.object(db_session, "close"),  # prevent the finally-close from killing the session
    ):
        from app.startup import seed_api_sources

        seed_api_sources()

    names = {s.name for s in db_session.query(ApiSource).all()}
    assert "rocketreach_enrichment" not in names, "rocketreach_enrichment should be pruned"
    assert "clearbit_enrichment" not in names, "clearbit_enrichment should be pruned"
    assert "icsource" in names, "icsource (browser worker) must survive"
    assert "ai_live_web" in names, "ai_live_web must be seeded"
    assert "sam_gov_enrichment" in names, "sam_gov_enrichment must be seeded"


def test_seed_prunes_all_9_dead_names(db_session):
    """seed_api_sources() must delete all 9 dead rows; planned+real rows survive."""
    # Pre-seed all 9 dead rows
    for dead_name in _ALL_PRUNE_NAMES:
        db_session.add(
            ApiSource(
                name=dead_name,
                display_name=dead_name.replace("_", " ").title(),
                category="enrichment",
                source_type="enrichment",
                credentials={},
            )
        )
    # Also pre-seed a planned row and a real row that must survive
    db_session.add(
        ApiSource(
            name="future",
            display_name="Future Electronics",
            category="api",
            source_type="broker",
            credentials={},
        )
    )
    db_session.add(
        ApiSource(
            name="icsource",
            display_name="IC Source",
            category="scraper",
            source_type="broker",
            credentials={},
        )
    )
    db_session.commit()

    with (
        patch("app.startup.SessionLocal", return_value=db_session),
        patch.object(db_session, "close"),
    ):
        from app.startup import seed_api_sources

        seed_api_sources()

    names = {s.name for s in db_session.query(ApiSource).all()}
    for dead in _ALL_PRUNE_NAMES:
        assert dead not in names, f"Dead source {dead!r} should have been pruned"
    assert "future" in names, "planned source 'future' must survive"
    assert "icsource" in names, "browser-worker 'icsource' must survive"


def test_seed_seeds_all_7_planned_connectors(db_session):
    """seed_api_sources() must seed all 7 planned connectors and NOT prune them."""
    with (
        patch("app.startup.SessionLocal", return_value=db_session),
        patch.object(db_session, "close"),
    ):
        from app.startup import seed_api_sources

        seed_api_sources()

    names = {s.name for s in db_session.query(ApiSource).all()}
    for planned in _PLANNED_NAMES:
        assert planned in names, f"Planned connector '{planned}' was not seeded into the DB"
