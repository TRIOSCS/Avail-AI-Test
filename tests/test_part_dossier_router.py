"""Tests for the Part Dossier ("The Bench") GET routes.

Covers the landing/dossier branch on /v2/partials/search, the four section endpoints
(hero / specs / market / recent) for known + unknown PNs, the light-footprint
search_count bump (existing card only — unknown PNs never create a card), and the
v2_page ?mpn= deep-link passthrough.

Called by: pytest
Depends on: app/routers/part_dossier.py, app/routers/htmx_views.py, MaterialCard.
"""

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.models.intelligence import MaterialCard


@pytest.fixture()
def known_card(db_session):
    """A MaterialCard for LM317T with manufacturer + enrichment so the hero/specs
    render."""
    card = MaterialCard(
        normalized_mpn="lm317t",
        display_mpn="LM317T",
        manufacturer="Texas Instruments",
        lifecycle_status="active",
        package_type="TO-220",
        rohs_status="compliant",
        condition="New",
        datasheet_url="https://example.com/lm317t.pdf",
        specs_summary="Adjustable 1.2V–37V linear regulator, 1.5A.",
        specs_structured={"v_out": {"value": "1.2-37V", "source": "digikey", "confidence": 0.99}},
        search_count=4,
        last_searched_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        enrichment_status="verified",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(card)
    db_session.commit()
    db_session.refresh(card)
    return card


# ── Landing vs dossier branch on /v2/partials/search ──────────────────────


def test_search_no_mpn_renders_landing(client):
    """GET /v2/partials/search (no mpn) → 200, the landing search box + recent
    section."""
    resp = client.get("/v2/partials/search")
    assert resp.status_code == 200
    body = resp.text
    assert 'name="mpn"' in body
    # Recent-searches section lazy-loads from the recent endpoint.
    assert "/v2/partials/search/recent" in body


def test_search_with_mpn_renders_dossier_shell(client):
    """GET /v2/partials/search?mpn=LM317T → 200, the dossier shell with lazy
    sections."""
    resp = client.get("/v2/partials/search", params={"mpn": "LM317T"})
    assert resp.status_code == 200
    body = resp.text
    # Hero lazy-load + all four section endpoints wired into the shell.
    assert "/v2/partials/search/dossier/hero?mpn=LM317T" in body
    assert "/v2/partials/search/dossier/market?mpn=LM317T" in body
    assert "/v2/partials/search/dossier/specs?mpn=LM317T" in body
    assert "/v2/partials/search/history?mpn=LM317T" in body
    # MPN normalized to upper for display.
    assert "LM317T" in body


# ── Hero endpoint ──────────────────────────────────────────────────────────


def test_hero_known_card_shows_identity_and_bumps_search_count(client, db_session, known_card):
    """Hero for a known card → 200 with MPN + manufacturer + counts, and bumps
    search_count."""
    before = known_card.search_count
    resp = client.get("/v2/partials/search/dossier/hero", params={"mpn": "LM317T"})
    assert resp.status_code == 200
    body = resp.text
    assert "LM317T" in body
    assert "Texas Instruments" in body

    db_session.expire(known_card)
    refreshed = db_session.get(MaterialCard, known_card.id)
    assert refreshed.search_count == before + 1
    assert refreshed.last_searched_at is not None


def test_hero_unknown_mpn_is_new_to_us_and_creates_no_card(client, db_session):
    """Hero for an unknown PN → 200 'New to us' state and does NOT create a card."""
    resp = client.get("/v2/partials/search/dossier/hero", params={"mpn": "ZZ-NOPE-999"})
    assert resp.status_code == 200
    assert "New to us" in resp.text
    # No card was minted for the unknown PN.
    assert db_session.query(MaterialCard).filter(MaterialCard.normalized_mpn == "zznope999").first() is None


# ── Specs endpoint ─────────────────────────────────────────────────────────


def test_specs_known_card(client, known_card):
    """Specs for a known card → 200 rendering enrichment fields."""
    resp = client.get("/v2/partials/search/dossier/specs", params={"mpn": "LM317T"})
    assert resp.status_code == 200
    body = resp.text
    assert "TO-220" in body  # package_type
    assert "Datasheet" in body  # datasheet pill


def test_specs_unknown_mpn_graceful(client):
    """Specs for an unknown PN → 200 graceful 'New to us' empty state."""
    resp = client.get("/v2/partials/search/dossier/specs", params={"mpn": "ZZ-NOPE-999"})
    assert resp.status_code == 200
    assert "New to us" in resp.text


# ── Market endpoint ────────────────────────────────────────────────────────


def test_market_cache_miss_returns_terminal_frame(client):
    """Market with no Redis cache (TESTING → no Redis) → 200, the frame that fires the
    existing /v2/partials/search/run SSE flow."""
    resp = client.get("/v2/partials/search/dossier/market", params={"mpn": "LM317T"})
    assert resp.status_code == 200
    body = resp.text
    assert "/v2/partials/search/run" in body
    assert "load" in body  # hx-trigger="load"


def test_market_cache_hit_renders_cached_rows(client):
    """Market WITH a fresh Redis pointer (search:{key}:latest → id, :results → rows) →
    200, renders the cached vendor rows in the terminal frame + freshness stamp +
    Refresh, and does NOT auto-fire the SSE run flow.

    The load-bearing cache-hit path.
    """
    rows = [
        {
            "vendor_name": "Cached Vendor",
            "mpn_matched": "LM317T",
            "manufacturer": "TI",
            "unit_price": 0.84,
            "qty_available": 1000,
            "confidence_color": "green",
            "confidence_pct": 91,
            "source_type": "brokerbin",
            "sources_found": ["brokerbin"],
        }
    ]
    rc = MagicMock()
    rc.get.side_effect = lambda k: (
        "sid-cache-1" if k.endswith(":latest") else (json.dumps(rows) if k.endswith(":results") else None)
    )
    with patch("app.search_service._get_search_redis", return_value=rc):
        resp = client.get("/v2/partials/search/dossier/market", params={"mpn": "LM317T"})
    assert resp.status_code == 200
    body = resp.text
    assert "Cached Vendor" in body
    assert "cached" in body
    assert "refresh=1" in body  # the Refresh-market button
    assert "/v2/partials/search/run" not in body  # cache hit → no SSE re-fire


# ── Recent endpoint ────────────────────────────────────────────────────────


def test_recent_endpoint_lists_searched_cards(client, known_card):
    """Recent endpoint → 200 listing recently-searched cards as dossier deep links."""
    resp = client.get("/v2/partials/search/recent")
    assert resp.status_code == 200
    body = resp.text
    assert "LM317T" in body
    assert "/v2/search?mpn=LM317T" in body


def test_recent_endpoint_empty_state(client):
    """Recent endpoint with no searched cards → 200 clean empty state."""
    resp = client.get("/v2/partials/search/recent")
    assert resp.status_code == 200
    assert "No recent searches yet" in resp.text


# ── v2_page ?mpn= passthrough ──────────────────────────────────────────────


def test_v2_page_mpn_passthrough(client, test_user):
    """GET /v2/search?mpn=LM317T → 200 and the partial_url carries the mpn deep-link.

    v2_page reads the session via get_user (not require_user), so patch it like the
    other full-page tests (TestV2PagePathVariants).
    """
    with patch("app.routers.htmx_views.get_user", return_value=test_user):
        resp = client.get("/v2/search", params={"mpn": "LM317T"})
    assert resp.status_code == 200
    # base_page.html fires hx-get="{{ partial_url }}"; the mpn rides along.
    assert "/v2/partials/search?mpn=LM317T" in resp.text


# ── Degraded-market banner (market_health) ────────────────────────────────


class TestMarketSourceHealth:
    """get_market_source_health partitions live-market connectors into available / down
    (auth-quota errors) / unconfigured."""

    def test_classifies_down_unconfigured_and_ignores_non_market(self, db_session):
        from app.search_service import get_market_source_health

        # available: a built MouserConnector; down: brokerbin (error_skipped);
        # unconfigured: ebay (skipped); a non-market/disabled enrichment source is ignored.
        mouser = type("MouserConnector", (), {})()
        stats = {
            "brokerbin": {"source": "brokerbin", "status": "error_skipped", "error": "Auth error — rotate credentials"},
            "ebay": {"source": "ebay", "status": "skipped", "error": "No API key configured"},
            "hunter_enrichment": {"source": "hunter_enrichment", "status": "disabled", "error": None},
        }
        with patch("app.search_service._build_connectors", return_value=([mouser], stats, set())):
            h = get_market_source_health(db_session)

        assert h["available"] == 1
        assert [d["name"] for d in h["down"]] == ["brokerbin"]
        assert h["down"][0]["display"] == "BrokerBin"
        assert h["down"][0]["reason"].startswith("Auth error")
        assert [u["name"] for u in h["unconfigured"]] == ["ebay"]
        assert h["total"] == 2  # available(1) + down(1); unconfigured excluded


def test_market_banner_renders_when_sources_down(client):
    """When live-market sources are down, the market section shows the degraded banner
    with the source display name, its reason, and a Settings deep-link."""
    health = {
        "available": 2,
        "total": 6,
        "down": [{"name": "brokerbin", "display": "BrokerBin", "reason": "Auth error — rotate credentials"}],
        "unconfigured": [],
    }
    with patch("app.search_service.get_market_source_health", return_value=health):
        resp = client.get("/v2/partials/search/dossier/market", params={"mpn": "LM317T"})
    assert resp.status_code == 200
    body = resp.text
    assert "BrokerBin" in body
    assert "unavailable" in body
    assert "/v2/settings" in body
    assert "Auth error — rotate credentials" in body  # per-source tooltip reason


def test_market_no_banner_when_all_sources_healthy(client):
    """No down sources → no degraded banner."""
    health = {"available": 6, "total": 6, "down": [], "unconfigured": []}
    with patch("app.search_service.get_market_source_health", return_value=health):
        resp = client.get("/v2/partials/search/dossier/market", params={"mpn": "LM317T"})
    assert resp.status_code == 200
    assert "unavailable" not in resp.text


def test_market_section_survives_health_lookup_failure(client):
    """A health-check failure must never break the market section (best-effort
    banner)."""
    with patch("app.search_service.get_market_source_health", side_effect=RuntimeError("boom")):
        resp = client.get("/v2/partials/search/dossier/market", params={"mpn": "LM317T"})
    assert resp.status_code == 200
    # Cache-miss frame still renders (fires the SSE run flow).
    assert "/v2/partials/search/run" in resp.text


def test_market_health_all_down_no_available(db_session):
    """When no market connector is built (all errored), available=0 and
    total==len(down)."""
    from app.search_service import get_market_source_health

    stats = {
        "brokerbin": {"source": "brokerbin", "status": "error_skipped", "error": "Auth error"},
        "nexar": {"source": "nexar", "status": "error_skipped", "error": "Quota exhausted"},
        "ebay": {"source": "ebay", "status": "skipped", "error": "No API key configured"},
    }
    with patch("app.search_service._build_connectors", return_value=([], stats, set())):
        h = get_market_source_health(db_session)

    assert h["available"] == 0
    assert {d["name"] for d in h["down"]} == {"brokerbin", "nexar"}
    assert [u["name"] for u in h["unconfigured"]] == ["ebay"]
    assert h["total"] == 2  # available(0) + down(2)


def test_specs_shows_stored_datasheet(client, db_session):
    from datetime import datetime, timezone

    from app.models.intelligence import MaterialCard, MaterialCardDatasheet

    card = MaterialCard(normalized_mpn="lm317t", display_mpn="LM317T", datasheet_captured_at=datetime.now(timezone.utc))
    db_session.add(card)
    db_session.flush()
    ds = MaterialCardDatasheet(
        material_card_id=card.id,
        file_name="LM317T-datasheet.pdf",
        library_item_id="ITM",
        library_web_url="https://od/x",
        library_drive_id="DRV",
        source="connector",
        verified=True,
        captured_at=datetime.now(timezone.utc),
    )
    db_session.add(ds)
    db_session.commit()
    resp = client.get("/v2/partials/search/dossier/specs", params={"mpn": "LM317T"})
    assert resp.status_code == 200
    # Links to our in-app streaming download (NOT the raw OneDrive webUrl).
    assert f"/v2/partials/search/dossier/datasheet/{ds.id}/download" in resp.text
    assert "https://od/x" not in resp.text
    assert "Datasheet (saved" in resp.text


def test_datasheet_download_streams_pdf(client, db_session):
    from unittest.mock import AsyncMock, patch

    from app.models.intelligence import MaterialCard, MaterialCardDatasheet

    card = MaterialCard(normalized_mpn="lm317z", display_mpn="LM317Z")
    db_session.add(card)
    db_session.flush()
    ds = MaterialCardDatasheet(
        material_card_id=card.id,
        file_name="LM317Z-datasheet.pdf",
        library_item_id="ITM",
        library_drive_id="DRV",
        content_type="application/pdf",
    )
    db_session.add(ds)
    db_session.commit()
    with patch("app.routers.part_dossier.fetch_datasheet_bytes", AsyncMock(return_value=b"%PDF-1.4 hello")):
        resp = client.get(f"/v2/partials/search/dossier/datasheet/{ds.id}/download")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/pdf")
    assert resp.content == b"%PDF-1.4 hello"


def test_datasheet_download_404_missing(client):
    resp = client.get("/v2/partials/search/dossier/datasheet/99999999/download")
    assert resp.status_code == 404


def test_datasheet_download_sanitizes_content_disposition(client, db_session):
    from unittest.mock import AsyncMock, patch

    from app.models.intelligence import MaterialCard, MaterialCardDatasheet

    card = MaterialCard(normalized_mpn="evil1", display_mpn="EVIL1")
    db_session.add(card)
    db_session.flush()
    # file_name carries header-injection chars (CR/LF) + a quote.
    ds = MaterialCardDatasheet(
        material_card_id=card.id,
        file_name='x"\r\nSet-Cookie: pwned=1.pdf',
        library_item_id="ITM",
        library_drive_id="DRV",
        content_type="application/pdf",
    )
    db_session.add(ds)
    db_session.commit()
    with patch("app.routers.part_dossier.fetch_datasheet_bytes", AsyncMock(return_value=b"%PDF")):
        resp = client.get(f"/v2/partials/search/dossier/datasheet/{ds.id}/download")
    assert resp.status_code == 200
    cd = resp.headers["content-disposition"]
    assert "\r" not in cd and "\n" not in cd  # no header injection
    assert cd.count('"') == 2  # only the wrapping quotes; the payload's quote was stripped
    assert "Set-Cookie" not in resp.headers  # no injected header


def test_market_no_banner_when_only_unconfigured(client):
    """Sources merely unconfigured (never set up) do NOT trigger the degraded banner —
    only `down` (auth/quota errors) do.

    Confirms the asymmetry is intentional.
    """
    health = {
        "available": 5,
        "total": 5,
        "down": [],
        "unconfigured": [{"name": "ebay", "display": "eBay", "reason": "No API key configured"}],
    }
    with patch("app.search_service.get_market_source_health", return_value=health):
        resp = client.get("/v2/partials/search/dossier/market", params={"mpn": "LM317T"})
    assert resp.status_code == 200
    assert "unavailable" not in resp.text
