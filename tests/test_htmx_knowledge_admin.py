"""Tests for Sprint 6 HTMX knowledge and admin endpoints.

Covers knowledge CRUD (list, create, update, delete), AI insights,
admin dedup/merge, health dashboard, CSV imports, and source testing.

Called by: pytest
Depends on: conftest (client, db_session, test_user fixtures), app.models
"""

import io
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import ApiSource, Company, CustomerSite, KnowledgeEntry, User, VendorCard


# -- Fixtures ----------------------------------------------------------------


@pytest.fixture()
def knowledge_entries(db_session: Session, test_user: User):
    """Create sample knowledge entries for testing."""
    entries = []
    for i in range(3):
        entry = KnowledgeEntry(
            entry_type="note",
            content=f"Test knowledge entry {i}",
            source="manual",
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db_session.add(entry)
        entries.append(entry)
    db_session.commit()
    for e in entries:
        db_session.refresh(e)
    return entries


@pytest.fixture()
def vendor_pair(db_session: Session):
    """Two vendor cards for dedup/merge testing."""
    v1 = VendorCard(
        display_name="Arrow Electronics",
        normalized_name="arrow electronics",
        sighting_count=10,
        created_at=datetime.now(timezone.utc),
    )
    v2 = VendorCard(
        display_name="Arrow Electonics",
        normalized_name="arrow electonics",
        sighting_count=3,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add_all([v1, v2])
    db_session.commit()
    db_session.refresh(v1)
    db_session.refresh(v2)
    return v1, v2


@pytest.fixture()
def company_pair(db_session: Session):
    """Two companies for dedup/merge testing."""
    c1 = Company(name="Acme Corp", is_active=True, created_at=datetime.now(timezone.utc))
    c2 = Company(name="ACME Corporation", is_active=True, created_at=datetime.now(timezone.utc))
    db_session.add_all([c1, c2])
    db_session.commit()
    db_session.refresh(c1)
    db_session.refresh(c2)
    return c1, c2


@pytest.fixture()
def api_source(db_session: Session):
    """An API source for testing."""
    src = ApiSource(
        name="test_source",
        display_name="Test Source",
        category="distributor",
        source_type="api",
        status="active",
        is_active=True,
    )
    db_session.add(src)
    db_session.commit()
    db_session.refresh(src)
    return src


# -- Knowledge CRUD Tests ---------------------------------------------------


def test_knowledge_list_empty(client: TestClient):
    """GET /v2/partials/knowledge returns table with no rows when empty."""
    resp = client.get("/v2/partials/knowledge")
    assert resp.status_code == 200
    assert "<table" in resp.text
    assert "<thead>" in resp.text


def test_knowledge_list_with_entries(client: TestClient, knowledge_entries):
    """GET /v2/partials/knowledge returns rows for existing entries."""
    resp = client.get("/v2/partials/knowledge")
    assert resp.status_code == 200
    for entry in knowledge_entries:
        assert f"data-entry-id='{entry.id}'" in resp.text


def test_knowledge_list_filter_by_entity(
    client: TestClient, db_session: Session, test_user: User, test_vendor_card: VendorCard
):
    """GET /v2/partials/knowledge with entity_type/entity_id filters results."""
    entry = KnowledgeEntry(
        entry_type="note",
        content="Vendor-specific note",
        source="manual",
        vendor_card_id=test_vendor_card.id,
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    db_session.add(entry)
    db_session.commit()

    resp = client.get(f"/v2/partials/knowledge?entity_type=vendors&entity_id={test_vendor_card.id}")
    assert resp.status_code == 200
    assert "Vendor-specific note" in resp.text


def test_knowledge_create(client: TestClient):
    """POST /v2/partials/knowledge creates an entry and returns success."""
    resp = client.post(
        "/v2/partials/knowledge",
        data={"title": "Test Note", "content": "Some body text"},
    )
    assert resp.status_code == 200
    assert "created successfully" in resp.text
    assert resp.headers.get("HX-Trigger") == "knowledgeChanged"


def test_knowledge_create_with_entity(client: TestClient, test_company):
    """POST /v2/partials/knowledge links entry to an entity."""
    resp = client.post(
        "/v2/partials/knowledge",
        data={
            "title": "Company Note",
            "content": "Body",
            "entity_type": "companies",
            "entity_id": str(test_company.id),
        },
    )
    assert resp.status_code == 200
    assert "created successfully" in resp.text


def test_knowledge_update(client: TestClient, knowledge_entries):
    """PUT /v2/partials/knowledge/{id} updates the entry content."""
    entry = knowledge_entries[0]
    resp = client.put(
        f"/v2/partials/knowledge/{entry.id}",
        data={"title": "Updated Title", "content": "Updated body"},
    )
    assert resp.status_code == 200
    assert f"data-entry-id='{entry.id}'" in resp.text


def test_knowledge_update_not_found(client: TestClient):
    """PUT /v2/partials/knowledge/99999 returns 404."""
    resp = client.put(
        "/v2/partials/knowledge/99999",
        data={"title": "X", "content": "Y"},
    )
    assert resp.status_code == 404


def test_knowledge_delete(client: TestClient, knowledge_entries):
    """DELETE /v2/partials/knowledge/{id} removes the entry."""
    entry = knowledge_entries[0]
    resp = client.delete(f"/v2/partials/knowledge/{entry.id}")
    assert resp.status_code == 200
    assert resp.headers.get("HX-Trigger") == "knowledgeChanged"
    assert resp.text == ""


def test_knowledge_delete_not_found(client: TestClient):
    """DELETE /v2/partials/knowledge/99999 returns 404."""
    resp = client.delete("/v2/partials/knowledge/99999")
    assert resp.status_code == 404


# -- Insights Tests ----------------------------------------------------------


def test_insights_get_empty(client: TestClient):
    """GET /v2/partials/requisitions/1/insights returns placeholder when no insights."""
    resp = client.get("/v2/partials/requisitions/1/insights")
    assert resp.status_code == 200
    assert "No insights yet" in resp.text


def test_insights_get_with_data(client: TestClient, db_session: Session, test_user: User, test_requisition):
    """GET /v2/partials/requisitions/{id}/insights returns insight cards."""
    entry = KnowledgeEntry(
        entry_type="ai_insight",
        content="Price trending down for LM317T",
        source="ai_generated",
        confidence=0.85,
        requisition_id=test_requisition.id,
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    db_session.add(entry)
    db_session.commit()

    resp = client.get(f"/v2/partials/requisitions/{test_requisition.id}/insights")
    assert resp.status_code == 200
    assert "Price trending down" in resp.text
    assert "85%" in resp.text


def test_insights_invalid_entity_type(client: TestClient):
    """GET /v2/partials/invalid/1/insights returns 400."""
    resp = client.get("/v2/partials/invalid/1/insights")
    assert resp.status_code == 400


def test_insights_refresh(client: TestClient):
    """POST /v2/partials/requisitions/1/insights/refresh handles gracefully."""
    with patch("app.services.knowledge_service.generate_insights", return_value=[]):
        resp = client.post("/v2/partials/requisitions/1/insights/refresh")
        assert resp.status_code == 200
        assert "No insights could be generated" in resp.text


# -- Admin Dedup/Merge Tests -------------------------------------------------


def test_vendor_dedup_partial(client: TestClient):
    """GET /v2/partials/admin/vendor-dedup returns HTML."""
    with patch(
        "app.vendor_utils.find_vendor_dedup_candidates",
        return_value=[
            {"id_a": 1, "id_b": 2, "name_a": "Arrow", "name_b": "Arow", "score": 90},
        ],
    ):
        resp = client.get("/v2/partials/admin/vendor-dedup")
        assert resp.status_code == 200
        assert "Arrow" in resp.text
        assert "Merge" in resp.text


def test_vendor_dedup_empty(client: TestClient):
    """GET /v2/partials/admin/vendor-dedup with no dupes shows info message."""
    with patch("app.vendor_utils.find_vendor_dedup_candidates", return_value=[]):
        resp = client.get("/v2/partials/admin/vendor-dedup")
        assert resp.status_code == 200
        assert "No duplicate vendors" in resp.text


def test_vendor_merge_partial(client: TestClient, vendor_pair):
    """POST /v2/partials/admin/vendor-merge merges and returns success."""
    v1, v2 = vendor_pair
    with patch(
        "app.services.vendor_merge_service.merge_vendor_cards",
        return_value={"kept": v1.id, "removed": v2.id},
    ):
        resp = client.post(
            "/v2/partials/admin/vendor-merge",
            data={"keep_id": str(v1.id), "remove_id": str(v2.id)},
        )
        assert resp.status_code == 200
        assert "successfully" in resp.text


def test_vendor_merge_error(client: TestClient):
    """POST /v2/partials/admin/vendor-merge with bad IDs returns error."""
    with patch(
        "app.services.vendor_merge_service.merge_vendor_cards",
        side_effect=ValueError("Vendor not found"),
    ):
        resp = client.post(
            "/v2/partials/admin/vendor-merge",
            data={"keep_id": "999", "remove_id": "998"},
        )
        assert resp.status_code == 400
        assert "Merge failed" in resp.text


def test_company_dedup_partial(client: TestClient):
    """GET /v2/partials/admin/company-dedup returns HTML table."""
    with patch(
        "app.company_utils.find_company_dedup_candidates",
        return_value=[
            {"id_a": 1, "id_b": 2, "name_a": "Acme", "name_b": "ACME Corp", "score": 92},
        ],
    ):
        resp = client.get("/v2/partials/admin/company-dedup")
        assert resp.status_code == 200
        assert "Acme" in resp.text


def test_company_merge_partial(client: TestClient, company_pair):
    """POST /v2/partials/admin/company-merge merges and returns success."""
    c1, c2 = company_pair
    with patch(
        "app.services.company_merge_service.merge_companies",
        return_value={"kept": c1.id, "removed": c2.id},
    ):
        resp = client.post(
            "/v2/partials/admin/company-merge",
            data={"keep_id": str(c1.id), "remove_id": str(c2.id)},
        )
        assert resp.status_code == 200
        assert "successfully" in resp.text


# -- Health Tests ------------------------------------------------------------


def test_health_dashboard(client: TestClient):
    """GET /v2/partials/admin/health returns row counts."""
    resp = client.get("/v2/partials/admin/health")
    assert resp.status_code == 200
    assert "System Health" in resp.text
    assert "Users" in resp.text
    assert "Companies" in resp.text
    assert "Vendors" in resp.text


# -- Import Tests ------------------------------------------------------------


def test_import_customers_csv(client: TestClient, db_session: Session):
    """POST /v2/partials/admin/import/customers parses CSV and creates companies."""
    csv_content = "name,website,industry\nNewCo Inc,https://newco.com,Electronics\n,,\n"
    resp = client.post(
        "/v2/partials/admin/import/customers",
        files={"file": ("customers.csv", io.BytesIO(csv_content.encode()), "text/csv")},
    )
    assert resp.status_code == 200
    assert "1 created" in resp.text

    company = db_session.query(Company).filter(Company.name == "NewCo Inc").first()
    assert company is not None
    assert company.website == "https://newco.com"


def test_import_customers_dedup(client: TestClient, db_session: Session):
    """Import skips companies that already exist (case-insensitive)."""
    existing = Company(name="Existing Co", is_active=True, created_at=datetime.now(timezone.utc))
    db_session.add(existing)
    db_session.commit()

    csv_content = "name\nexisting co\nBrand New Co\n"
    resp = client.post(
        "/v2/partials/admin/import/customers",
        files={"file": ("customers.csv", io.BytesIO(csv_content.encode()), "text/csv")},
    )
    assert resp.status_code == 200
    assert "1 created" in resp.text
    assert "1 skipped" in resp.text


def test_import_vendors_csv(client: TestClient, db_session: Session):
    """POST /v2/partials/admin/import/vendors parses CSV and creates vendor cards."""
    csv_content = "name,domain,country\nNew Vendor,newvendor.com,US\n"
    resp = client.post(
        "/v2/partials/admin/import/vendors",
        files={"file": ("vendors.csv", io.BytesIO(csv_content.encode()), "text/csv")},
    )
    assert resp.status_code == 200
    assert "1 created" in resp.text

    card = db_session.query(VendorCard).filter(VendorCard.normalized_name == "new vendor").first()
    assert card is not None


def test_import_vendors_dedup(client: TestClient, db_session: Session):
    """Import skips vendors that already exist by normalized name."""
    existing = VendorCard(
        display_name="Old Vendor",
        normalized_name="old vendor",
        sighting_count=1,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(existing)
    db_session.commit()

    csv_content = "name\nOld Vendor\nFresh Vendor\n"
    resp = client.post(
        "/v2/partials/admin/import/vendors",
        files={"file": ("vendors.csv", io.BytesIO(csv_content.encode()), "text/csv")},
    )
    assert resp.status_code == 200
    assert "1 created" in resp.text
    assert "1 skipped" in resp.text


# -- Source Test Endpoint ----------------------------------------------------


def test_source_test_healthy(client: TestClient, api_source):
    """GET /v2/partials/settings/sources/{id}/test returns healthy badge."""
    resp = client.get(f"/v2/partials/settings/sources/{api_source.id}/test")
    assert resp.status_code == 200
    assert "healthy" in resp.text
    assert "bg-success" in resp.text


def test_source_test_not_found(client: TestClient):
    """GET /v2/partials/settings/sources/99999/test returns 404."""
    resp = client.get("/v2/partials/settings/sources/99999/test")
    assert resp.status_code == 404
