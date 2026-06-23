"""Tests for Task 2: catalog additions + prune of dead providers.

Verifies:
- api_sources.json contains ai_live_web + sam_gov_enrichment and NOT rocketreach/clearbit
- seed_api_sources() prunes dead rows and leaves browser-worker rows intact

Called by: pytest
Depends on: app/data/api_sources.json, app.startup.seed_api_sources, app.models.ApiSource
"""

import json
from unittest.mock import patch

from app.models import ApiSource


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
