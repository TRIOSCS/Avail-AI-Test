"""Tests for the Part Dossier ("The Bench") GET routes.

Covers the landing/dossier branch on /v2/partials/search, the four section endpoints
(hero / specs / market / recent) for known + unknown PNs, the light-footprint
search_count bump (existing card only — unknown PNs never create a card), and the
v2_page ?mpn= deep-link passthrough.

Called by: pytest
Depends on: app/routers/part_dossier.py, app/routers/htmx_views.py, MaterialCard.
"""

import json
import re
from datetime import UTC, datetime
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
        last_searched_at=datetime(2026, 6, 1, tzinfo=UTC),
        enrichment_status="verified",
        created_at=datetime.now(UTC),
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
        # unconfigured: digikey (skipped); a non-market/disabled enrichment source is
        # ignored, and so is the worker-backed ebay row (its health is a heartbeat,
        # not a synchronous search — see _MARKET_SOURCE_DISPLAY).
        mouser = type("MouserConnector", (), {})()
        stats = {
            "brokerbin": {"source": "brokerbin", "status": "error_skipped", "error": "Auth error — rotate credentials"},
            "digikey": {"source": "digikey", "status": "skipped", "error": "No API key configured"},
            "ebay": {"source": "ebay", "status": "skipped", "error": "No API key configured"},
            "hunter_enrichment": {"source": "hunter_enrichment", "status": "disabled", "error": None},
        }
        with patch("app.search_service._build_connectors", return_value=([mouser], stats, set())):
            h = get_market_source_health(db_session)

        assert h["available"] == 1
        assert [d["name"] for d in h["down"]] == ["brokerbin"]
        assert h["down"][0]["display"] == "BrokerBin"
        assert h["down"][0]["reason"].startswith("Auth error")
        assert [u["name"] for u in h["unconfigured"]] == ["digikey"]
        assert "ebay" not in {u["name"] for u in h["unconfigured"]}
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
        "digikey": {"source": "digikey", "status": "skipped", "error": "No API key configured"},
    }
    with patch("app.search_service._build_connectors", return_value=([], stats, set())):
        h = get_market_source_health(db_session)

    assert h["available"] == 0
    assert {d["name"] for d in h["down"]} == {"brokerbin", "nexar"}
    assert [u["name"] for u in h["unconfigured"]] == ["digikey"]
    assert h["total"] == 2  # available(0) + down(2)


def test_specs_shows_stored_datasheet(client, db_session):
    from datetime import datetime

    from app.models.intelligence import MaterialCard, MaterialCardDatasheet

    card = MaterialCard(normalized_mpn="lm317t", display_mpn="LM317T", datasheet_captured_at=datetime.now(UTC))
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
        captured_at=datetime.now(UTC),
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


# ── Market-baseline strip — helper unit tests ──────────────────────────────


class TestComputeMarketBaseline:
    """Unit tests for compute_market_baseline — no DB, no HTTP, no SSE.

    The helper must be importable and work on plain dicts (same schema as cached_rows /
    vendor_card.html).
    """

    def _make_row(self, *, is_authorized: bool, unit_price, qty_available) -> dict:
        return {"is_authorized": is_authorized, "unit_price": unit_price, "qty_available": qty_available}

    def test_empty_input_returns_no_authorized(self):
        from app.search_service import compute_market_baseline

        result = compute_market_baseline([])
        assert result["has_authorized"] is False
        assert result["median_price"] is None
        assert result["total_stock"] is None
        assert result["sources"] == 0

    def test_all_non_authorized_returns_no_authorized(self):
        from app.search_service import compute_market_baseline

        rows = [
            self._make_row(is_authorized=False, unit_price=1.50, qty_available=500),
            self._make_row(is_authorized=False, unit_price=2.00, qty_available=200),
        ]
        result = compute_market_baseline(rows)
        assert result["has_authorized"] is False
        assert result["sources"] == 0

    def test_authorized_rows_median_price_odd_count(self):
        """With 3 authorized rows, median is the middle price when sorted."""
        from app.search_service import compute_market_baseline

        rows = [
            self._make_row(is_authorized=True, unit_price=1.00, qty_available=100),
            self._make_row(is_authorized=True, unit_price=3.00, qty_available=200),
            self._make_row(is_authorized=True, unit_price=2.00, qty_available=150),
            self._make_row(is_authorized=False, unit_price=0.50, qty_available=5000),
        ]
        result = compute_market_baseline(rows)
        assert result["has_authorized"] is True
        assert result["sources"] == 3
        # Sorted prices: [1.00, 2.00, 3.00] → index 1 → 2.00
        assert result["median_price"] == pytest.approx(2.00)
        assert result["total_stock"] == 450  # 100 + 200 + 150

    def test_authorized_rows_median_price_even_count(self):
        """With 2 authorized rows, median uses upper-middle (index len//2 == 1)."""
        from app.search_service import compute_market_baseline

        rows = [
            self._make_row(is_authorized=True, unit_price=1.00, qty_available=100),
            self._make_row(is_authorized=True, unit_price=3.00, qty_available=200),
        ]
        result = compute_market_baseline(rows)
        # Sorted [1.00, 3.00] → index 1 → 3.00 (same algorithm as _median)
        assert result["median_price"] == pytest.approx(3.00)
        assert result["total_stock"] == 300

    def test_authorized_none_price_excluded_from_median(self):
        """unit_price=None rows are excluded from the price list; stock still
        counted."""
        from app.search_service import compute_market_baseline

        rows = [
            self._make_row(is_authorized=True, unit_price=None, qty_available=500),
            self._make_row(is_authorized=True, unit_price=2.50, qty_available=100),
        ]
        result = compute_market_baseline(rows)
        assert result["has_authorized"] is True
        assert result["median_price"] == pytest.approx(2.50)
        assert result["total_stock"] == 600  # None row's qty still counted

    def test_authorized_none_qty_excluded_from_stock_sum(self):
        """qty_available=None means unknown — excluded from sum; median still
        computed."""
        from app.search_service import compute_market_baseline

        rows = [
            self._make_row(is_authorized=True, unit_price=1.00, qty_available=None),
            self._make_row(is_authorized=True, unit_price=2.00, qty_available=None),
        ]
        result = compute_market_baseline(rows)
        assert result["has_authorized"] is True
        assert result["total_stock"] is None  # all qtys unknown
        assert result["median_price"] == pytest.approx(2.00)

    def test_all_authorized_none_price_median_is_none(self):
        """If every authorized row has no price, median is None (not a crash)."""
        from app.search_service import compute_market_baseline

        rows = [self._make_row(is_authorized=True, unit_price=None, qty_available=100)]
        result = compute_market_baseline(rows)
        assert result["has_authorized"] is True
        assert result["median_price"] is None
        assert result["total_stock"] == 100

    def test_zero_price_excluded_from_median(self):
        """unit_price=0 is not a real price and must be excluded (same as _median)."""
        from app.search_service import compute_market_baseline

        rows = [
            self._make_row(is_authorized=True, unit_price=0, qty_available=100),
            self._make_row(is_authorized=True, unit_price=5.00, qty_available=50),
        ]
        result = compute_market_baseline(rows)
        # Only 5.00 qualifies → median is 5.00
        assert result["median_price"] == pytest.approx(5.00)

    def test_mix_authorized_and_non_uses_only_authorized(self):
        """Non-authorized rows must not pollute the median or stock sum."""
        from app.search_service import compute_market_baseline

        rows = [
            self._make_row(is_authorized=True, unit_price=10.00, qty_available=50),
            self._make_row(is_authorized=False, unit_price=0.01, qty_available=99999),
        ]
        result = compute_market_baseline(rows)
        assert result["sources"] == 1
        assert result["median_price"] == pytest.approx(10.00)
        assert result["total_stock"] == 50


# ── Market-baseline strip — render tests ──────────────────────────────────


class TestMarketBaselineStripRender:
    """Light render tests: the dossier_market template renders the strip (and the
    empty-state path) without Jinja errors when cached rows are provided via a
    mocked Redis pointer."""

    def _rows_with_baseline(self) -> list[dict]:
        return [
            {
                "vendor_name": "DigiKey",
                "mpn_matched": "LM317T",
                "manufacturer": "TI",
                "unit_price": 1.25,
                "qty_available": 500,
                "is_authorized": True,
                "confidence_color": "green",
                "confidence_pct": 95,
                "source_type": "digikey",
                "sources_found": ["digikey"],
            },
            {
                "vendor_name": "Mouser",
                "mpn_matched": "LM317T",
                "manufacturer": "TI",
                "unit_price": 1.50,
                "qty_available": 300,
                "is_authorized": True,
                "confidence_color": "green",
                "confidence_pct": 92,
                "source_type": "mouser",
                "sources_found": ["mouser"],
            },
            {
                "vendor_name": "GreyBroker",
                "mpn_matched": "LM317T",
                "manufacturer": "",
                "unit_price": 0.75,
                "qty_available": 2000,
                "is_authorized": False,
                "confidence_color": "amber",
                "confidence_pct": 60,
                "source_type": "brokerbin",
                "sources_found": ["brokerbin"],
            },
        ]

    def _patch_redis(self, rows):
        rc = MagicMock()
        rc.get.side_effect = lambda k: (
            "sid-baseline-test" if k.endswith(":latest") else (json.dumps(rows) if k.endswith(":results") else None)
        )
        return rc

    def test_baseline_strip_shows_franchise_fields(self, client):
        """Cache hit with 2 authorized rows → strip renders median price, auth stock,
        and auth source count without Jinja errors."""
        rows = self._rows_with_baseline()
        rc = self._patch_redis(rows)
        with patch("app.search_service._get_search_redis", return_value=rc):
            resp = client.get("/v2/partials/search/dossier/market", params={"mpn": "LM317T"})
        assert resp.status_code == 200
        body = resp.text
        assert "Franchise baseline" in body
        assert "Median price" in body
        assert "Auth stock" in body
        assert "Auth sources" in body
        # 2 authorized sources
        assert "800" in body  # total authorized stock = 500 + 300

    def test_baseline_strip_empty_state_no_authorized(self, client):
        """When all cached rows are non-authorized, the graceful empty-state renders."""
        rows = [
            {
                "vendor_name": "GreyBroker",
                "mpn_matched": "LM317T",
                "manufacturer": "",
                "unit_price": 0.75,
                "qty_available": 2000,
                "is_authorized": False,
                "confidence_color": "amber",
                "confidence_pct": 60,
                "source_type": "brokerbin",
                "sources_found": ["brokerbin"],
            }
        ]
        rc = self._patch_redis(rows)
        with patch("app.search_service._get_search_redis", return_value=rc):
            resp = client.get("/v2/partials/search/dossier/market", params={"mpn": "LM317T"})
        assert resp.status_code == 200
        assert "No franchise/authorized pricing for this part" in resp.text

    def test_baseline_strip_absent_on_cache_miss(self, client):
        """Cache miss → no baseline strip (it only renders when cached_rows exist)."""
        resp = client.get("/v2/partials/search/dossier/market", params={"mpn": "LM317T"})
        assert resp.status_code == 200
        assert "Franchise baseline" not in resp.text
        assert "/v2/partials/search/run" in resp.text  # SSE frame fires instead


# ── Market-summary KPI tile strip — helper unit tests + endpoint render ───


def _production_shaped_rows(raw_hits: list[dict]) -> list[dict]:
    """Build market rows exactly the way stream_search_mpn builds ``accumulated``.

    Each raw connector hit goes through the REAL ``_score_raw_hit`` (which stamps
    confidence_pct / confidence_color / score / lead_quality ...) and then the REAL
    ``_incremental_dedup`` (which adds sub_offers / offer_count / sources_found and
    merges a repeat vendor+MPN into one row). No hand-built keys, so a change to the
    real row shape surfaces here instead of hiding behind a fixture.
    """
    from app.search_service import _incremental_dedup, _score_raw_hit

    accumulated: list[dict] = []
    for hit in raw_hits:
        _incremental_dedup([_score_raw_hit(hit, {})], accumulated)
    return accumulated


def _tile_values(html: str) -> dict[str, str]:
    """Map each KPI tile label to its rendered value (``{"Vendors": "2", ...}``).

    market_summary.html emits, per tile, a ``<span class="block ...">value</span>``
    immediately followed by a ``<span class="block ...">label</span>``; matching on the
    six known labels keeps this robust to other ``block`` spans elsewhere in the body.
    """
    pairs = re.findall(
        r'<span class="block[^"]*">\s*([^<]*?)\s*</span>\s*'
        r'<span class="block[^"]*">\s*(Vendors|Offers|Authorized|High confidence|Best price|Total stock)\s*</span>',
        html,
    )
    return {label: value for value, label in pairs}


class TestComputeMarketSummary:
    """Unit tests for compute_market_summary — no DB, no HTTP, no SSE.

    Whole-result-set KPI counts for the tile strip (market_summary.html), on plain dicts
    (same schema as cached_rows / vendor_card.html) plus rows built by the real
    streaming pipeline (_score_raw_hit -> _incremental_dedup).
    """

    def test_empty_input_returns_zeros(self):
        from app.search_service import compute_market_summary

        result = compute_market_summary([])
        assert result == {
            "vendors": 0,
            "offers": 0,
            "authorized": 0,
            "high_confidence": 0,
            "best_price": None,
            "total_stock": None,
        }

    def test_counts_vendors_authorized_and_high_confidence(self):
        from app.search_service import compute_market_summary

        rows = [
            {"is_authorized": True, "confidence_color": "green", "unit_price": 2.0, "qty_available": 100},
            {"is_authorized": False, "confidence_color": "green", "unit_price": 1.5, "qty_available": 50},
            {"is_authorized": False, "confidence_color": "amber", "unit_price": None, "qty_available": None},
        ]
        result = compute_market_summary(rows)
        assert result["vendors"] == 3
        assert result["authorized"] == 1
        assert result["high_confidence"] == 2
        assert result["best_price"] == 1.5
        assert result["total_stock"] == 150

    def test_offers_use_offer_count_then_sub_offers_then_one(self):
        from app.search_service import compute_market_summary

        rows = [
            {"offer_count": 4},  # explicit count wins
            {"sub_offers": [{}, {}]},  # falls back to len(sub_offers)
            {},  # bare row still counts as one offer
        ]
        assert compute_market_summary(rows)["offers"] == 7

    def test_best_price_ignores_zero_and_none(self):
        from app.search_service import compute_market_summary

        rows = [{"unit_price": 0}, {"unit_price": None}, {"unit_price": 3.25}]
        assert compute_market_summary(rows)["best_price"] == 3.25

    def test_no_known_qty_total_stock_none(self):
        from app.search_service import compute_market_summary

        rows = [{"unit_price": 1.0}, {"unit_price": 2.0}]
        assert compute_market_summary(rows)["total_stock"] is None

    def test_best_price_found_inside_sub_offers_when_head_is_pricier(self):
        """Dedup keeps the best-SCORED offer at the row head, not the cheapest, so the
        lowest price can sit only in sub_offers."""
        from app.search_service import compute_market_summary

        rows = [
            {"unit_price": 2.0, "sub_offers": [{"unit_price": 1.25}, {"unit_price": 3.0}]},
            {"unit_price": 1.5},
        ]
        assert compute_market_summary(rows)["best_price"] == 1.25

    def test_best_price_found_in_sub_offers_when_head_has_no_price(self):
        from app.search_service import compute_market_summary

        rows = [{"unit_price": None, "sub_offers": [{"unit_price": 4.5}, {"unit_price": 2.75}]}]
        assert compute_market_summary(rows)["best_price"] == 2.75

    def test_sub_offer_prices_of_zero_none_or_negative_are_ignored(self):
        from app.search_service import compute_market_summary

        rows = [{"unit_price": 2.0, "sub_offers": [{"unit_price": 0}, {"unit_price": None}, {}, {"unit_price": -1.0}]}]
        assert compute_market_summary(rows)["best_price"] == 2.0

    def test_best_price_none_when_no_head_or_sub_offer_has_a_positive_price(self):
        from app.search_service import compute_market_summary

        rows = [{"unit_price": None, "sub_offers": [{"unit_price": 0}, {"unit_price": None}]}]
        assert compute_market_summary(rows)["best_price"] is None

    def test_production_shaped_rows_count_green_rows_as_high_confidence(self):
        """Rows built by the real _score_raw_hit -> _incremental_dedup carry
        confidence_pct (int) + confidence_color, so high_confidence counts exactly the
        green (>=75%) rows — the streaming path used to emit neither, pinning the KPI at
        0."""
        from app.search_service import compute_market_summary

        def hit(vendor, confidence, **extra):
            return {
                "vendor_name": vendor,
                "mpn_matched": "LM317T",
                "unit_price": 1.0,
                "qty_available": 100,
                "source_type": "brokerbin",
                "confidence": confidence,
                **extra,
            }

        rows = _production_shaped_rows(
            [
                hit("Green Auth Co", 4, is_authorized=True, source_type="digikey"),  # 4/5 -> 80% green
                hit("Green Edge Co", 0.75),  # exactly 75% -> green
                hit("Amber Top Co", 0.74),  # 74% -> amber
                hit("Amber Edge Co", 0.5),  # exactly 50% -> amber
                hit("Red Top Co", 0.49),  # 49% -> red
                hit("Red Blank Co", 0),  # no confidence -> red
                # A second offer from an already-seen green vendor merges into its row
                # (offers +1) without adding a vendor or a second high-confidence count.
                hit("Green Auth Co", 4, is_authorized=True, source_type="mouser", unit_price=2.0),
            ]
        )

        # Real row shape, not a hand-built one.
        for row in rows:
            assert isinstance(row["confidence_pct"], int)
            assert row["confidence_color"] in {"green", "amber", "red"}
        assert {r["vendor_name"]: r["confidence_color"] for r in rows} == {
            "Green Auth Co": "green",
            "Green Edge Co": "green",
            "Amber Top Co": "amber",
            "Amber Edge Co": "amber",
            "Red Top Co": "red",
            "Red Blank Co": "red",
        }

        result = compute_market_summary(rows)
        assert result["vendors"] == 6
        assert result["offers"] == 7
        assert result["authorized"] == 1
        assert result["high_confidence"] == 2
        assert result["best_price"] == 1.0
        assert result["total_stock"] == 700  # 6 vendors x 100 + the merged 100

    def test_production_shaped_merge_keeps_cheaper_offer_in_sub_offers_for_best_price(self):
        """An authorized (score 100) offer stays the row head over a cheaper
        unauthorized one, so the cheaper price lives only in sub_offers — best_price
        must still find it."""
        from app.search_service import compute_market_summary

        rows = _production_shaped_rows(
            [
                {
                    "vendor_name": "Split Offer Co",
                    "mpn_matched": "LM317T",
                    "unit_price": 2.0,
                    "qty_available": 500,
                    "is_authorized": True,
                    "confidence": 0.9,
                    "source_type": "digikey",
                },
                {
                    "vendor_name": "Split Offer Co",
                    "mpn_matched": "LM317T",
                    "unit_price": 1.1,
                    "qty_available": 300,
                    "is_authorized": False,
                    "confidence": 0.9,
                    "source_type": "brokerbin",
                },
            ]
        )

        assert len(rows) == 1
        assert rows[0]["unit_price"] == 2.0  # head = best-scored, not cheapest
        assert [o["unit_price"] for o in rows[0]["sub_offers"]] == [1.1]
        result = compute_market_summary(rows)
        assert result["best_price"] == 1.1
        assert result["offers"] == 2
        assert result["total_stock"] == 800


class TestMarketSummaryStrip:
    """The KPI tile strip renders on the dossier cache-hit path and via the SSE
    "summary" event renderer, and stays absent on a cache miss.

    Rows come from the real streaming pipeline (_production_shaped_rows), not hand-built
    dicts, so the confidence_color / sub_offers / offer_count keys are the production
    ones.
    """

    def _rows(self):
        return _production_shaped_rows(
            [
                {
                    "vendor_name": "Tile Vendor A",
                    "mpn_matched": "LM317T",
                    "unit_price": 0.84,
                    "qty_available": 1000,
                    "is_authorized": True,
                    "confidence": 0.91,  # 91% -> green
                    "source_type": "digikey",
                },
                {
                    "vendor_name": "Tile Vendor B",
                    "mpn_matched": "LM317T",
                    "unit_price": 1.10,
                    "qty_available": 250,
                    "is_authorized": False,
                    "confidence": 0.6,  # 60% -> amber
                    "source_type": "brokerbin",
                },
            ]
        )

    def _rows_without_price_or_stock(self):
        return _production_shaped_rows(
            [
                {
                    "vendor_name": "Quote Only A",
                    "mpn_matched": "LM317T",
                    "unit_price": None,
                    "qty_available": None,
                    "confidence": 0.9,
                    "source_type": "brokerbin",
                },
                {
                    "vendor_name": "Quote Only B",
                    "mpn_matched": "LM317T",
                    "unit_price": None,
                    "qty_available": None,
                    "confidence": 0.8,
                    "source_type": "nexar",
                },
            ]
        )

    def _patch_redis(self, rows):
        # Serialize exactly as stream_search_mpn's cache writer does (default=str):
        # production rows are JSON-native (sources_found is a sorted list), so the
        # cached payload must round-trip without any set-to-repr corruption.
        rc = MagicMock()
        rc.get.side_effect = lambda k: (
            "sid-summary-test"
            if k.endswith(":latest")
            else (json.dumps(rows, default=str) if k.endswith(":results") else None)
        )
        return rc

    def test_cache_hit_renders_kpi_tiles(self, client):
        """Cache hit → the six tiles render with whole-set counts (2 vendors, 1
        authorized, 1 high confidence, best price $0.8400, stock 1,250)."""
        rc = self._patch_redis(self._rows())
        with patch("app.search_service._get_search_redis", return_value=rc):
            resp = client.get("/v2/partials/search/dossier/market", params={"mpn": "LM317T"})
        assert resp.status_code == 200
        body = resp.text
        for label in ("Vendors", "Offers", "Authorized", "High confidence", "Best price", "Total stock"):
            assert label in body
        assert "$0.8400" in body
        assert "1,250" in body
        assert _tile_values(body) == {
            "Vendors": "2",
            "Offers": "2",
            "Authorized": "1",
            "High confidence": "1",
            "Best price": "$0.8400",
            "Total stock": "1,250",
        }

    def test_cache_hit_header_counts_vendors_not_offers(self, client):
        """The cached freshness header counts vendor rows ("2 vendors"); the Offers tile
        owns the offer count."""
        rc = self._patch_redis(self._rows())
        with patch("app.search_service._get_search_redis", return_value=rc):
            resp = client.get("/v2/partials/search/dossier/market", params={"mpn": "LM317T"})
        assert resp.status_code == 200
        assert "2 vendors" in resp.text
        assert "2 offers" not in resp.text

    def test_cache_hit_strip_renders_above_cards_container(self, client):
        """Structural invariant: the strip sits ABOVE #search-results-cards (the
        filter/sort swap target), so re-filtering the cards never wipes it, and nothing
        of it renders inside or after the cards container."""
        rc = self._patch_redis(self._rows())
        with patch("app.search_service._get_search_redis", return_value=rc):
            resp = client.get("/v2/partials/search/dossier/market", params={"mpn": "LM317T"})
        assert resp.status_code == 200
        body = resp.text
        cards_at = body.index('id="search-results-cards"')
        assert body.index('role="group"') < cards_at
        assert body.index("High confidence") < cards_at
        assert "High confidence" not in body[cards_at:]
        assert 'role="group"' not in body[cards_at:]

    def test_cache_miss_has_no_tiles_server_side(self, client):
        """Cache miss → no server-rendered tiles; the SSE frame fills #market-summary at
        stream end instead."""
        resp = client.get("/v2/partials/search/dossier/market", params={"mpn": "LM317T"})
        assert resp.status_code == 200
        assert "High confidence" not in resp.text

    def test_sse_renderer_produces_tile_strip(self):
        """_render_market_summary_html (the "summary" SSE event body) renders the same
        tile strip from raw rows, and renders empty for an empty set."""
        from app.search_service import _render_market_summary_html

        html = _render_market_summary_html(self._rows())
        assert "Vendors" in html
        assert "High confidence" in html
        assert "$0.8400" in html
        assert _tile_values(html)["High confidence"] == "1"
        assert _render_market_summary_html([]).strip() == ""

    def test_sse_renderer_fallback_tiles_when_no_price_or_stock(self):
        """Rows with unit_price None and qty_available None render the "RFQ" / "—"
        fallback tiles (still labelled "Best price" / "Total stock").

        Guards the ``is not none`` Jinja conditionals: losing them would format None
        with %.4f / {:,} and raise inside the SSE renderer, silently killing the strip
        (stream_search_mpn swallows the error) or 500ing the dossier.
        """
        from app.search_service import _render_market_summary_html

        html = _render_market_summary_html(self._rows_without_price_or_stock())
        assert "Best price" in html
        assert "Total stock" in html
        assert "RFQ" in html
        assert "—" in html
        assert "$None" not in html
        tiles = _tile_values(html)
        assert tiles["Best price"] == "RFQ"
        assert tiles["Total stock"] == "—"
        # The rest of the strip is unaffected by the missing price/stock.
        assert tiles["Vendors"] == "2"
        assert tiles["High confidence"] == "2"

    def test_sse_renderer_price_and_stock_fallbacks_are_independent(self):
        """Missing price does not blank the stock tile, and missing stock does not blank
        the price tile."""
        from app.search_service import _render_market_summary_html

        price_only = _tile_values(
            _render_market_summary_html(
                _production_shaped_rows(
                    [{"vendor_name": "P", "mpn_matched": "LM317T", "unit_price": 0.5, "qty_available": None}]
                )
            )
        )
        assert price_only["Best price"] == "$0.5000"
        assert price_only["Total stock"] == "—"

        stock_only = _tile_values(
            _render_market_summary_html(
                _production_shaped_rows(
                    [{"vendor_name": "S", "mpn_matched": "LM317T", "unit_price": None, "qty_available": 1200}]
                )
            )
        )
        assert stock_only["Best price"] == "RFQ"
        assert stock_only["Total stock"] == "1,200"

    def test_sse_renderer_known_zero_stock_renders_zero_not_dash(self):
        """A known total of 0 is data ("0"), not missing ("—") — the conditional is ``is
        not none``, not truthiness."""
        from app.search_service import _render_market_summary_html

        # _incremental_dedup drops qty == 0 offers, so feed the renderer a row directly.
        rows = [{"vendor_name": "Zero Co", "unit_price": 1.0, "qty_available": 0, "confidence_color": "green"}]
        assert _tile_values(_render_market_summary_html(rows))["Total stock"] == "0"

    def test_cache_hit_fallback_tiles_do_not_break_dossier(self, client):
        """Cached rows without price/qty still render the dossier market (200) with the
        fallback tiles."""
        rc = self._patch_redis(self._rows_without_price_or_stock())
        with patch("app.search_service._get_search_redis", return_value=rc):
            resp = client.get("/v2/partials/search/dossier/market", params={"mpn": "LM317T"})
        assert resp.status_code == 200
        body = resp.text
        assert "Best price" in body
        assert "Total stock" in body
        tiles = _tile_values(body)
        assert tiles["Best price"] == "RFQ"
        assert tiles["Total stock"] == "—"
        assert "Quote Only A" in body


# ── Cache round-trip — sources_found must survive json.dumps(default=str) ──


class TestCachedRowsJsonRoundTrip:
    """Real pipeline rows must survive the Redis cache write verbatim.

    stream_search_mpn caches ``accumulated`` with ``json.dumps(..., default=str)``.
    A set-valued ``sources_found`` would serialize to its repr string, which 500s
    dossier_market.html's ``sum(start=[])`` source collector and corrupts the
    vendor-card source badges on read-back — so the dedup paths must keep it a
    JSON-native (sorted list) value end to end.
    """

    def test_pipeline_rows_are_json_native_and_render_after_round_trip(self, client):
        rows = _production_shaped_rows(
            [
                {
                    "vendor_name": "Multi Source",
                    "mpn": "LM317T",
                    "source_type": "digikey",
                    "unit_price": 1.0,
                    "qty_available": 10,
                    "confidence": 4,
                },
                {
                    "vendor_name": "Multi Source",
                    "mpn": "LM317T",
                    "source_type": "nexar",
                    "unit_price": 0.9,
                    "qty_available": 5,
                    "confidence": 4,
                },
                {
                    "vendor_name": "Solo Source",
                    "mpn": "LM317T",
                    "source_type": "brokerbin",
                    "unit_price": 2.0,
                    "qty_available": 7,
                    "confidence": 2,
                },
            ]
        )
        # Merged and solo rows both carry a sorted-list sources_found
        by_vendor = {r["vendor_name"]: r for r in rows}
        assert by_vendor["Multi Source"]["sources_found"] == ["digikey", "nexar"]
        assert by_vendor["Solo Source"]["sources_found"] == ["brokerbin"]

        # Plain json round-trip (no default needed) — the writer's default=str must
        # have nothing left to mangle
        restored = json.loads(json.dumps(rows, default=str))
        assert restored == json.loads(json.dumps(rows))

        # And the dossier cache-hit endpoint renders the round-tripped payload whole:
        # source <select> options, no set-repr leakage, KPI strip present
        rc = MagicMock()
        rc.get.side_effect = lambda k: (
            "sid-roundtrip"
            if k.endswith(":latest")
            else (json.dumps(rows, default=str) if k.endswith(":results") else None)
        )
        with patch("app.search_service._get_search_redis", return_value=rc):
            resp = client.get("/v2/partials/search/dossier/market", params={"mpn": "LM317T"})
        assert resp.status_code == 200
        body = resp.text
        assert "{'" not in body  # no set-repr string anywhere
        assert '<option value="digikey">' in body
        assert '<option value="nexar">' in body
        assert '<option value="brokerbin">' in body
        assert "High confidence" in body
