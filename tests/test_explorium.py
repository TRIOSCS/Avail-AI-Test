"""Tests for Explorium/Vibe discovery router and schemas.

Tests ICP segment listing, company discovery, status checks, error handling,
and schema validation with mocked Explorium API calls.

Called by: pytest
Depends on: app.routers.explorium, app.schemas.explorium,
            app.services.prospect_discovery_explorium
"""

from unittest.mock import AsyncMock, patch

from app.schemas.explorium import (
    DiscoveredCompany,
    DiscoverRequest,
    DiscoverResponse,
    ExploriumSegment,
    ExploriumStatus,
    SegmentsResponse,
)
from app.services.prospect_discovery_explorium import (
    REGIONS,
    SEGMENT_SEARCH_PARAMS,
)

# ── Fixtures ────────────────────────────────────────────────────────


SAMPLE_DISCOVERY_RESULT = {
    "name": "Acme Semiconductors",
    "domain": "acmesemi.com",
    "website": "https://acmesemi.com",
    "industry": "Semiconductor Manufacturing",
    "naics_code": "334418",
    "employee_count_range": "501-1000",
    "revenue_range": "$100M-$500M",
    "hq_location": "Austin, TX, US",
    "region": "US",
    "description": "Leading semiconductor manufacturer",
    "parent_company_domain": None,
    "discovery_source": "explorium",
    "segment_key": "ems_electronics",
    "intent": {
        "strength": "strong",
        "topics": ["electronic components", "semiconductors", "BOM sourcing"],
        "component_topics": ["electronic components", "semiconductors", "BOM sourcing"],
    },
    "hiring": {"type": "procurement", "detail": 15},
    "events": [{"type": "funding", "date": "2026-01-15", "description": "Series C raised $50M"}],
    "enrichment_raw": {"raw_field": "value"},
}


# ── Segments endpoint ───────────────────────────────────────────────


def test_list_segments(client):
    """GET /api/explorium/segments returns all ICP segments and regions."""
    resp = client.get("/api/explorium/segments")
    assert resp.status_code == 200
    data = resp.json()
    assert "segments" in data
    assert "regions" in data

    # All segments from SEGMENT_SEARCH_PARAMS should be listed
    segment_keys = {s["key"] for s in data["segments"]}
    for key in SEGMENT_SEARCH_PARAMS:
        assert key in segment_keys

    # Regions should match
    assert data["regions"] == REGIONS


def test_list_segments_structure(client):
    """Each segment has key, name, linkedin_categories, naics_codes, intent_keywords."""
    resp = client.get("/api/explorium/segments")
    data = resp.json()
    for seg in data["segments"]:
        assert "key" in seg
        assert "name" in seg
        assert "linkedin_categories" in seg
        assert "naics_codes" in seg
        assert "intent_keywords" in seg
        # Name should be human-readable title case
        assert seg["name"] == seg["key"].replace("_", " ").title()


def test_list_segments_count(client):
    """Segments count matches SEGMENT_SEARCH_PARAMS."""
    resp = client.get("/api/explorium/segments")
    data = resp.json()
    assert len(data["segments"]) == len(SEGMENT_SEARCH_PARAMS)


# ── Discover endpoint ───────────────────────────────────────────────


def test_discover_happy_path(client):
    """POST /api/explorium/discover returns companies for valid segment + region."""
    with patch(
        "app.routers.explorium.discover_companies_with_signals",
        new_callable=AsyncMock,
        return_value=[SAMPLE_DISCOVERY_RESULT],
    ):
        resp = client.post(
            "/api/explorium/discover",
            json={"segment": "ems_electronics", "region": "US"},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["segment"] == "ems_electronics"
    assert data["region"] == "US"
    assert data["total"] == 1
    assert len(data["companies"]) == 1

    co = data["companies"][0]
    assert co["name"] == "Acme Semiconductors"
    assert co["domain"] == "acmesemi.com"
    assert co["industry"] == "Semiconductor Manufacturing"
    assert co["discovery_source"] == "explorium"


def test_discover_multiple_companies(client):
    """Discovery returns multiple companies correctly."""
    results = [
        {**SAMPLE_DISCOVERY_RESULT, "name": "Company A", "domain": "a.com"},
        {**SAMPLE_DISCOVERY_RESULT, "name": "Company B", "domain": "b.com"},
        {**SAMPLE_DISCOVERY_RESULT, "name": "Company C", "domain": "c.com"},
    ]
    with patch(
        "app.routers.explorium.discover_companies_with_signals",
        new_callable=AsyncMock,
        return_value=results,
    ):
        resp = client.post(
            "/api/explorium/discover",
            json={"segment": "automotive", "region": "EU"},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 3
    assert len(data["companies"]) == 3
    names = {c["name"] for c in data["companies"]}
    assert names == {"Company A", "Company B", "Company C"}


def test_discover_empty_results(client):
    """Discovery with no matches returns empty list, not error."""
    with patch(
        "app.routers.explorium.discover_companies_with_signals",
        new_callable=AsyncMock,
        return_value=[],
    ):
        resp = client.post(
            "/api/explorium/discover",
            json={"segment": "ems_electronics", "region": "Asia"},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 0
    assert data["companies"] == []


def test_discover_invalid_segment(client):
    """Discovery with unknown segment returns 400 error."""
    resp = client.post(
        "/api/explorium/discover",
        json={"segment": "nonexistent_segment", "region": "US"},
    )
    assert resp.status_code == 400
    data = resp.json()
    assert "error" in data
    assert "nonexistent_segment" in data["error"]


def test_discover_invalid_region(client):
    """Discovery with unknown region returns 400 error."""
    resp = client.post(
        "/api/explorium/discover",
        json={"segment": "ems_electronics", "region": "Antarctica"},
    )
    assert resp.status_code == 400
    data = resp.json()
    assert "error" in data
    assert "Antarctica" in data["error"]


def test_discover_missing_segment_field(client):
    """Discovery without required segment field returns 422."""
    resp = client.post(
        "/api/explorium/discover",
        json={"region": "US"},
    )
    assert resp.status_code == 422


def test_discover_default_region(client):
    """Discovery defaults to US region when not specified."""
    with patch(
        "app.routers.explorium.discover_companies_with_signals",
        new_callable=AsyncMock,
        return_value=[],
    ) as mock_discover:
        resp = client.post(
            "/api/explorium/discover",
            json={"segment": "ems_electronics"},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["region"] == "US"
    mock_discover.assert_called_once_with("ems_electronics", "US")


def test_discover_preserves_signals(client):
    """Discovery preserves intent, hiring, and events signal data."""
    with patch(
        "app.routers.explorium.discover_companies_with_signals",
        new_callable=AsyncMock,
        return_value=[SAMPLE_DISCOVERY_RESULT],
    ):
        resp = client.post(
            "/api/explorium/discover",
            json={"segment": "ems_electronics", "region": "US"},
        )

    co = resp.json()["companies"][0]
    assert co["intent"]["strength"] == "strong"
    assert co["hiring"]["type"] == "procurement"
    assert len(co["events"]) == 1
    assert co["events"][0]["type"] == "funding"


def test_discover_all_segments(client):
    """Verify all defined segments can be used for discovery."""
    for seg_key in SEGMENT_SEARCH_PARAMS:
        with patch(
            "app.routers.explorium.discover_companies_with_signals",
            new_callable=AsyncMock,
            return_value=[],
        ):
            resp = client.post(
                "/api/explorium/discover",
                json={"segment": seg_key, "region": "US"},
            )
        assert resp.status_code == 200
        assert resp.json()["segment"] == seg_key


def test_discover_all_regions(client):
    """Verify all defined regions can be used for discovery."""
    for region_key in REGIONS:
        with patch(
            "app.routers.explorium.discover_companies_with_signals",
            new_callable=AsyncMock,
            return_value=[],
        ):
            resp = client.post(
                "/api/explorium/discover",
                json={"segment": "ems_electronics", "region": region_key},
            )
        assert resp.status_code == 200
        assert resp.json()["region"] == region_key


# ── Status endpoint ─────────────────────────────────────────────────


def test_status_no_api_key(client):
    """Status reports not configured when API key is missing."""
    with patch("app.routers.explorium._get_api_key", return_value=""):
        resp = client.get("/api/explorium/status")

    assert resp.status_code == 200
    data = resp.json()
    assert data["configured"] is False
    assert data["reachable"] is False
    assert "not configured" in data["message"]


def test_status_api_reachable(client):
    """Status reports reachable when API returns results."""
    with patch("app.routers.explorium._get_api_key", return_value="test-key"):
        with patch(
            "app.routers.explorium.discover_companies_with_signals",
            new_callable=AsyncMock,
            return_value=[SAMPLE_DISCOVERY_RESULT],
        ):
            resp = client.get("/api/explorium/status")

    assert resp.status_code == 200
    data = resp.json()
    assert data["configured"] is True
    assert data["reachable"] is True
    assert "OK" in data["message"]
    assert "1 test results" in data["message"]


def test_status_api_empty_results(client):
    """Status reports reachable even with empty results (API responded correctly)."""
    with patch("app.routers.explorium._get_api_key", return_value="test-key"):
        with patch(
            "app.routers.explorium.discover_companies_with_signals",
            new_callable=AsyncMock,
            return_value=[],
        ):
            resp = client.get("/api/explorium/status")

    assert resp.status_code == 200
    data = resp.json()
    assert data["configured"] is True
    assert data["reachable"] is True


def test_status_api_error(client):
    """Status reports unreachable when API call throws exception."""
    with patch("app.routers.explorium._get_api_key", return_value="test-key"):
        with patch(
            "app.routers.explorium.discover_companies_with_signals",
            new_callable=AsyncMock,
            side_effect=Exception("Connection timeout"),
        ):
            resp = client.get("/api/explorium/status")

    assert resp.status_code == 200
    data = resp.json()
    assert data["configured"] is True
    assert data["reachable"] is False
    assert "Connection timeout" in data["message"]


# ── Auth required ───────────────────────────────────────────────────


def test_segments_requires_auth():
    """Segments endpoint requires authentication (no override)."""
    from fastapi.testclient import TestClient

    from app.main import app

    # Use a fresh client without auth overrides
    with TestClient(app) as unauthed:
        resp = unauthed.get("/api/explorium/segments")
    assert resp.status_code in (401, 403)


def test_discover_requires_auth():
    """Discover endpoint requires authentication."""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as unauthed:
        resp = unauthed.post(
            "/api/explorium/discover",
            json={"segment": "ems_electronics"},
        )
    assert resp.status_code in (401, 403)


def test_status_requires_auth():
    """Status endpoint requires authentication."""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as unauthed:
        resp = unauthed.get("/api/explorium/status")
    assert resp.status_code in (401, 403)


# ── Schema validation ───────────────────────────────────────────────


def test_schema_explorium_segment():
    """ExploriumSegment schema validates correctly."""
    seg = ExploriumSegment(
        key="test_segment",
        name="Test Segment",
        linkedin_categories=["cat1"],
        naics_codes=["123456"],
        intent_keywords=["kw1"],
    )
    assert seg.key == "test_segment"
    assert seg.name == "Test Segment"
    assert seg.linkedin_categories == ["cat1"]


def test_schema_segments_response():
    """SegmentsResponse schema holds segments and regions."""
    resp = SegmentsResponse(
        segments=[
            ExploriumSegment(key="a", name="A"),
        ],
        regions={"US": ["US"]},
    )
    assert len(resp.segments) == 1
    assert resp.regions == {"US": ["US"]}


def test_schema_discover_request_defaults():
    """DiscoverRequest defaults region to US."""
    req = DiscoverRequest(segment="ems_electronics")
    assert req.segment == "ems_electronics"
    assert req.region == "US"


def test_schema_discover_request_custom_region():
    """DiscoverRequest accepts custom region."""
    req = DiscoverRequest(segment="automotive", region="EU")
    assert req.region == "EU"


def test_schema_discovered_company():
    """DiscoveredCompany schema maps all fields."""
    co = DiscoveredCompany(**SAMPLE_DISCOVERY_RESULT)
    assert co.name == "Acme Semiconductors"
    assert co.domain == "acmesemi.com"
    assert co.intent["strength"] == "strong"
    assert co.hiring["type"] == "procurement"
    assert len(co.events) == 1


def test_schema_discovered_company_minimal():
    """DiscoveredCompany works with minimal fields."""
    co = DiscoveredCompany(name="Minimal Co", domain="minimal.com")
    assert co.name == "Minimal Co"
    assert co.intent == {}
    assert co.hiring == {}
    assert co.events == []
    assert co.discovery_source == "explorium"


def test_schema_discover_response():
    """DiscoverResponse schema validates correctly."""
    resp = DiscoverResponse(
        segment="ems_electronics",
        region="US",
        companies=[DiscoveredCompany(name="Test", domain="test.com")],
        total=1,
    )
    assert resp.total == 1
    assert resp.companies[0].name == "Test"


def test_schema_explorium_status():
    """ExploriumStatus schema validates all fields."""
    status = ExploriumStatus(
        configured=True,
        reachable=True,
        message="OK — 5 test results",
    )
    assert status.configured is True
    assert status.reachable is True
    assert "OK" in status.message


def test_schema_explorium_status_defaults():
    """ExploriumStatus defaults to not configured/reachable."""
    status = ExploriumStatus()
    assert status.configured is False
    assert status.reachable is False
    assert status.message == ""
