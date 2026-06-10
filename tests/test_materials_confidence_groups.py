"""Unit 7 — data-confidence 3-group filter.

Backend: statuses CSV still filters by enrichment_status (default = all tiers = no narrowing;
empty = no filter). Render: the workspace sidebar exposes the 3 group checkboxes in the
first filter fold (expanded by default) and no longer uses the old per-tier handler.
"""

from sqlalchemy.orm import Session

from app.models import MaterialCard
from app.services.faceted_search_service import search_materials_faceted

ALL_TIERS = ["verified", "web_sourced", "oem_sourced", "ai_inferred", "not_catalogued", "not_found", "unenriched"]


def _mk(db: Session, mpn: str, status: str) -> None:
    db.add(MaterialCard(normalized_mpn=mpn, display_mpn=mpn.upper(), category="dram", enrichment_status=status))
    db.flush()


def test_default_all_statuses_returns_everything(db_session: Session):
    for i, s in enumerate(["verified", "ai_inferred", "unenriched"]):
        _mk(db_session, f"m{i}", s)
    db_session.commit()
    _, total = search_materials_faceted(db_session, statuses=ALL_TIERS)
    assert total == 3  # all-on default does not hide anything


def test_empty_statuses_returns_everything(db_session: Session):
    for i, s in enumerate(["verified", "unenriched"]):
        _mk(db_session, f"m{i}", s)
    db_session.commit()
    _, total = search_materials_faceted(db_session, statuses=[])
    assert total == 2  # unchecking all groups falls through to no confidence filter


def test_statuses_subset_narrows(db_session: Session):
    _mk(db_session, "v", "verified")
    _mk(db_session, "u", "unenriched")
    db_session.commit()
    # "Trusted only" expands to verified/web_sourced/oem_sourced — excludes the unenriched card.
    results, total = search_materials_faceted(db_session, statuses=["verified", "web_sourced", "oem_sourced"])
    assert total == 1
    assert results[0].normalized_mpn == "v"


def test_workspace_renders_confidence_groups_first_fold(client):
    resp = client.get("/v2/partials/materials/workspace")
    assert resp.status_code == 200
    assert "Data confidence" in resp.text
    assert "toggleConfidenceGroup" in resp.text
    assert "CONFIDENCE_GROUPS" in resp.text
    # The old per-tier toggle handler is gone.
    assert "toggleStatus(" not in resp.text
