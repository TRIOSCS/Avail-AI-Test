"""test_sprint9_10_materials_admin.py — Tests for Sprints 9-10.

Sprint 9: Material enrichment, insights, knowledge CRUD.
Sprint 10: Admin API health, vendor CSV import, data ops.

Called by: pytest
Depends on: conftest.py fixtures, app.routers.htmx_views
"""

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import MaterialCard, User

# ── Material Enrichment ──────────────────────────────────────────────


class TestMaterialEnrich:
    def test_enrich_renders(self, client: TestClient, test_material_card: MaterialCard):
        resp = client.post(
            f"/v2/partials/materials/{test_material_card.id}/enrich",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        # Endpoint triggers enrichment and returns the material detail partial
        assert resp.text.strip() != ""

    def test_enrich_nonexistent(self, client: TestClient):
        resp = client.post(
            "/v2/partials/materials/99999/enrich",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 404


# ── Material Insights ────────────────────────────────────────────────


class TestMaterialInsights:
    def test_insights_renders(self, client: TestClient, test_material_card: MaterialCard):
        resp = client.get(
            f"/v2/partials/materials/{test_material_card.id}/insights",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "Insights" in resp.text

    def test_insights_nonexistent(self, client: TestClient):
        resp = client.get(
            "/v2/partials/materials/99999/insights",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 404


# ── Knowledge Base ───────────────────────────────────────────────────


class TestKnowledge:
    def test_list_empty(self, client: TestClient):
        resp = client.get(
            "/v2/partials/knowledge",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "Knowledge Base" in resp.text

    def test_create_entry(self, client: TestClient):
        resp = client.post(
            "/v2/partials/knowledge",
            data={"entry_type": "note", "content": "LM317T is commonly used in voltage regulators"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "LM317T" in resp.text

    def test_create_empty_rejected(self, client: TestClient):
        resp = client.post(
            "/v2/partials/knowledge",
            data={"content": ""},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 400

    def test_search_entries(self, client: TestClient, db_session: Session, test_user: User):
        from app.models.knowledge import KnowledgeEntry

        e = KnowledgeEntry(
            entry_type="fact",
            content="Texas Instruments makes the LM317T",
            source="manual",
            created_by=test_user.id,
        )
        db_session.add(e)
        db_session.commit()

        resp = client.get(
            "/v2/partials/knowledge?q=Texas",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "Texas Instruments" in resp.text


# ── Admin API Health ─────────────────────────────────────────────────


class TestAdminApiHealth:
    def test_health_renders(self, client: TestClient):
        resp = client.get(
            "/v2/partials/admin/api-health",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "Connector Health" in resp.text


# (TestAdminDataOps removed — GET /v2/partials/admin/data-ops was deleted as a superseded
# dupe of the settings Data Ops tab, GET /v2/partials/settings/data-ops.)
