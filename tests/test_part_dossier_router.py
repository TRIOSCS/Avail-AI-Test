"""Tests for the Part Dossier ("The Bench") GET routes.

Covers the landing/dossier branch on /v2/partials/search, the four section endpoints
(hero / specs / market / recent) for known + unknown PNs, the light-footprint
search_count bump (existing card only — unknown PNs never create a card), and the
v2_page ?mpn= deep-link passthrough.

Called by: pytest
Depends on: app/routers/part_dossier.py, app/routers/htmx_views.py, MaterialCard.
"""

from datetime import datetime, timezone
from unittest.mock import patch

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
