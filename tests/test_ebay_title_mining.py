"""tests/test_ebay_title_mining.py — eBay listing TITLES are free-text part
descriptions; mining them feeds the description-grammar enrichment (the same F1 ladder
path distributor descriptions use, see connector_desc harvest).

Verifies: the ``ebay_title`` source is registered at tier 83 (and the migration-096 CASE
stays in sync); a fixture eBay Browse response harvests its titles into the desc grammar,
landing category + facets via the ladder at ``ebay_title``/83; absent creds (or the flag
off) is a clean no-op; a harvest failure never aborts the caller.

Depends on: conftest.py (db_session), seed_commodity_schemas, MaterialCard +
MaterialSpecFacet, spec_tiers.SOURCE_TIER (ebay_title=83), app.services.enrichment.
"""

import asyncio
from unittest.mock import patch

from sqlalchemy.orm import Session

from app.config import settings
from app.models import MaterialCard, MaterialSpecFacet
from app.services import enrichment
from app.services.commodity_registry import seed_commodity_schemas
from app.services.desc_extractor._common import EBAY_TITLE_SOURCE
from app.services.spec_tiers import SOURCE_TIER, tier_for
from app.services.spec_write_service import record_spec


# ── A fixture eBay Browse item_summary/search response ────────────────────────────────
def _ebay_browse_payload(*titles: str) -> dict:
    """Shape one eBay Browse ``item_summary/search`` JSON body with the given titles."""
    return {
        "itemSummaries": [
            {
                "itemId": f"v1|{i}|0",
                "title": title,
                "seller": {"username": f"seller{i}"},
                "price": {"value": "42.00", "currency": "USD"},
                "condition": "New",
                "itemWebUrl": f"https://www.ebay.com/itm/{i}",
            }
            for i, title in enumerate(titles)
        ]
    }


class _FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        pass


def _async_return(value):
    async def _coro(*a, **k):
        return value

    return _coro


# ── Tier registration ─────────────────────────────────────────────────────────────────
def test_ebay_title_registered_at_tier_83():
    assert SOURCE_TIER["ebay_title"] == 83
    assert tier_for("ebay_title") == 83
    assert EBAY_TITLE_SOURCE == "ebay_title"
    # External marketplace free-text title — same evidence class as the card's own
    # desc_parse; below the curated distributor description (connector_desc 84) and the
    # deterministic decoders (mpn_decode 85).
    assert SOURCE_TIER["ebay_title"] == SOURCE_TIER["desc_parse"]
    assert SOURCE_TIER["ebay_title"] < SOURCE_TIER["connector_desc"]
    assert SOURCE_TIER["ebay_title"] < SOURCE_TIER["mpn_decode"]


def test_migration_096_case_in_sync():
    """The migration's literal SQL CASE must carry the new source too (its sync test
    asserts EXACT equality with SOURCE_TIER)."""
    import importlib.util
    import os

    path = os.path.join(os.path.dirname(__file__), "..", "alembic", "versions", "096_spec_provenance.py")
    spec = importlib.util.spec_from_file_location("migration_096_ebay", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert "WHEN 'ebay_title' THEN 83" in mod._SOURCE_TIER_SQL_CASE


# ── The harvest itself ─────────────────────────────────────────────────────────────────
def _facets(db: Session, card_id: int) -> dict:
    rows = db.query(MaterialSpecFacet).filter_by(material_card_id=card_id).all()
    return {r.spec_key: r for r in rows}


def _run_harvest(db: Session, card: MaterialCard, *titles: str) -> None:
    """Call the harvest with a mocked eBay Browse fetch + present creds."""
    payload = _ebay_browse_payload(*titles)
    with (
        patch.object(enrichment, "get_credential_cached", return_value="cred"),
        patch("app.connectors.ebay.http") as http,
    ):
        http.post = _async_return(_FakeResponse({"access_token": "tok", "expires_in": 7200}))
        http.get = _async_return(_FakeResponse(payload))
        asyncio.run(enrichment.harvest_ebay_titles(card.normalized_mpn, card, db))


def test_ebay_title_categorizes_uncategorized_card(db_session: Session):
    seed_commodity_schemas(db_session)
    card = MaterialCard(normalized_mpn="mem123", display_mpn="MEM123", category=None)
    db_session.add(card)
    db_session.flush()

    _run_harvest(db_session, card, "Samsung 16GB (1x16GB) DUAL RANK X4 DDR4-2666 REGISTERED ECC MEMORY")
    db_session.commit()

    # Title flowed through the desc grammar → categorized + faceted via the F1 ladder.
    assert card.category == "dram"
    assert card.category_source == "ebay_title"
    assert card.category_tier == 83
    facets = _facets(db_session, card.id)
    assert "capacity_gb" in facets
    assert facets["capacity_gb"].source == "ebay_title"
    assert facets["capacity_gb"].tier == 83


def test_ebay_title_loses_to_mpn_decode(db_session: Session):
    seed_commodity_schemas(db_session)
    card = MaterialCard(normalized_mpn="mem5", display_mpn="MEM5", category="dram")
    db_session.add(card)
    db_session.flush()
    # A higher-tier decoder value lands first.
    record_spec(db_session, int(card.id), "ddr_type", "DDR4", source="mpn_decode", confidence=0.95, schema_cache=None)
    db_session.commit()

    # eBay title says DDR3 — must not clobber the mpn_decode DDR4.
    _run_harvest(db_session, card, "Samsung 8GB DDR3-1600 REGISTERED ECC MEMORY")
    db_session.commit()

    facets = _facets(db_session, card.id)
    assert facets["ddr_type"].value_text == "DDR4"
    assert facets["ddr_type"].source == "mpn_decode"


def test_absent_creds_is_noop(db_session: Session):
    seed_commodity_schemas(db_session)
    card = MaterialCard(normalized_mpn="mem9", display_mpn="MEM9", category=None)
    db_session.add(card)
    db_session.flush()

    # No creds → connector must never be constructed / fetched.
    calls = {"post": 0, "get": 0}

    def _count(key):
        async def _coro(*a, **k):
            calls[key] += 1
            return _FakeResponse({})

        return _coro

    with (
        patch.object(enrichment, "get_credential_cached", return_value=None),
        patch("app.connectors.ebay.http") as http,
    ):
        http.post = _count("post")
        http.get = _count("get")
        asyncio.run(enrichment.harvest_ebay_titles(card.normalized_mpn, card, db_session))
    db_session.commit()
    assert calls == {"post": 0, "get": 0}  # no network when creds absent
    assert card.category is None


def test_flag_off_is_noop(db_session: Session, monkeypatch):
    seed_commodity_schemas(db_session)
    monkeypatch.setattr(settings, "ebay_title_mining_enabled", False)
    card = MaterialCard(normalized_mpn="mem8", display_mpn="MEM8", category=None)
    db_session.add(card)
    db_session.flush()

    called = {"n": 0}

    def _cred(*a, **k):
        called["n"] += 1
        return "cred"

    with patch.object(enrichment, "get_credential_cached", _cred):
        asyncio.run(enrichment.harvest_ebay_titles(card.normalized_mpn, card, db_session))
    db_session.commit()
    assert card.category is None
    assert called["n"] == 0  # flag short-circuits before any credential lookup


def test_harvest_failure_does_not_propagate(db_session: Session):
    """A harvest failure is best-effort — caught + logged, never re-raised to the
    caller."""
    seed_commodity_schemas(db_session)
    card = MaterialCard(normalized_mpn="boom1", display_mpn="BOOM1", category=None)
    db_session.add(card)
    db_session.flush()

    async def _boom(*a, **k):
        raise RuntimeError("ebay blew up")

    with (
        patch.object(enrichment, "get_credential_cached", return_value="cred"),
        patch("app.connectors.ebay.EbayConnector.search", _boom),
    ):
        # Must NOT raise.
        asyncio.run(enrichment.harvest_ebay_titles(card.normalized_mpn, card, db_session))
    db_session.commit()
    assert card.category is None
