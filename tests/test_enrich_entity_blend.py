"""Task 9: enrich_entity + find_suggested_contacts use enrichment_router + blend layer.

Monkeypatches enrichment_router.gather_company / gather_contacts and cache helpers to
verify the new architecture — blend-by-authority, _provenance carried forward, contacts
deduped + relevance-filtered.
"""

import os

os.environ["TESTING"] = "1"

import pytest

import app.enrichment_service as es
from app.services import enrichment_router

# ── enrich_entity blend tests ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_enrich_entity_blends_by_authority(monkeypatch):
    """Higher-tier source wins per-field; _provenance is carried on the returned
    dict."""

    async def fake_gather(domain, name=""):
        return [
            {"source": "apollo", "industry": "Wholesale"},
            {"source": "explorium", "industry": "Electronics", "ticker": "ARW"},
        ]

    monkeypatch.setattr(enrichment_router, "gather_company", fake_gather)
    monkeypatch.setattr("app.cache.intel_cache.get_cached", lambda k: None)
    monkeypatch.setattr("app.cache.intel_cache.set_cached", lambda *a, **k: None)

    out = await es.enrich_entity("arrow.com", "Arrow")

    # Explorium tier 85 beats Apollo tier 70 for industry
    assert out["industry"] == "Electronics"
    # Explorium is the only ticker source
    assert out["ticker"] == "ARW"
    # _provenance must be present
    assert "_provenance" in out
    assert out["_provenance"]["industry"]["source"] == "explorium"


@pytest.mark.asyncio
async def test_enrich_entity_provenance_carried_through_normalize(monkeypatch):
    """_provenance survives normalize_company_output and lands on the final dict."""

    async def fake_gather(domain, name=""):
        return [
            {"source": "lusha", "legal_name": "Acme Corp", "industry": "Tech"},
        ]

    monkeypatch.setattr(enrichment_router, "gather_company", fake_gather)
    monkeypatch.setattr("app.cache.intel_cache.get_cached", lambda k: None)
    monkeypatch.setattr("app.cache.intel_cache.set_cached", lambda *a, **k: None)

    out = await es.enrich_entity("acme.com", "Acme")

    assert out.get("_provenance") is not None
    assert out["_provenance"]["legal_name"]["source"] == "lusha"


@pytest.mark.asyncio
async def test_enrich_entity_domain_always_set(monkeypatch):
    """Even when no provider returns a domain, the input domain is set."""

    async def fake_gather(domain, name=""):
        return [{"source": "ai", "industry": "Tech"}]

    monkeypatch.setattr(enrichment_router, "gather_company", fake_gather)
    monkeypatch.setattr("app.cache.intel_cache.get_cached", lambda k: None)
    monkeypatch.setattr("app.cache.intel_cache.set_cached", lambda *a, **k: None)

    out = await es.enrich_entity("myco.com", "MyCo")
    assert out["domain"] == "myco.com"


@pytest.mark.asyncio
async def test_enrich_entity_cache_hit_bypasses_router(monkeypatch):
    """Cached result returned directly without calling gather_company."""
    cached_val = {"domain": "cached.com", "industry": "Cached", "_provenance": {}}
    monkeypatch.setattr("app.cache.intel_cache.get_cached", lambda k: cached_val)

    # gather_company must NOT be called
    called = []

    async def should_not_call(domain, name=""):
        called.append(True)
        return []

    monkeypatch.setattr(enrichment_router, "gather_company", should_not_call)

    out = await es.enrich_entity("cached.com")
    assert out["industry"] == "Cached"
    assert not called


@pytest.mark.asyncio
async def test_enrich_entity_empty_results_not_cached(monkeypatch):
    """When all providers return nothing useful, result is not cached."""

    async def fake_gather(domain, name=""):
        return []

    monkeypatch.setattr(enrichment_router, "gather_company", fake_gather)
    monkeypatch.setattr("app.cache.intel_cache.get_cached", lambda k: None)
    cached_calls = []
    monkeypatch.setattr("app.cache.intel_cache.set_cached", lambda *a, **k: cached_calls.append(a))

    await es.enrich_entity("empty.com")
    assert not cached_calls, "Should not cache when no real data returned"


# ── find_suggested_contacts blend tests ─────────────────────────────────────


@pytest.mark.asyncio
async def test_find_contacts_blends_and_deduplicates(monkeypatch):
    """Contacts from multiple providers are deduped by email (highest-tier fields
    win)."""

    async def fake_gather(domain, name, title_filter, limit):
        return [
            {"source": "apollo", "full_name": "Jane Doe", "email": "jane@co.com", "title": "Buyer"},
            {"source": "lusha", "full_name": "Jane Doe", "email": "jane@co.com", "phone": "+1234", "verified": True},
        ]

    monkeypatch.setattr(enrichment_router, "gather_contacts", fake_gather)

    out = await es.find_suggested_contacts("co.com", "Co", title_filter="buyer", limit=5)
    # Deduped to one Jane (by email)
    janes = [c for c in out if c.get("email") == "jane@co.com"]
    assert len(janes) == 1
    # Lusha tier 95 wins for phone
    assert janes[0].get("phone") == "+1234"


@pytest.mark.asyncio
async def test_find_contacts_relevance_filter(monkeypatch):
    """_is_relevant filter keeps relevant titles; if all irrelevant, returns all."""

    async def fake_gather(domain, name, title_filter, limit):
        return [
            {"source": "ai", "full_name": "Bob", "title": "Senior Buyer", "email": "bob@co.com"},
            {"source": "ai", "full_name": "Alice", "title": "Janitor", "email": "alice@co.com"},
        ]

    monkeypatch.setattr(enrichment_router, "gather_contacts", fake_gather)

    out = await es.find_suggested_contacts("co.com", "Co")
    # Buyer is relevant; janitor is not
    names = [c["full_name"] for c in out]
    assert "Bob" in names
    assert "Alice" not in names


@pytest.mark.asyncio
async def test_find_contacts_all_irrelevant_returns_all(monkeypatch):
    """When nothing passes _is_relevant, the full unique list is returned."""

    async def fake_gather(domain, name, title_filter, limit):
        return [
            {"source": "ai", "full_name": "Carol", "title": "Janitor", "email": "carol@co.com"},
        ]

    monkeypatch.setattr(enrichment_router, "gather_contacts", fake_gather)

    out = await es.find_suggested_contacts("co.com", "Co")
    # Falls back to unfiltered
    assert any(c["full_name"] == "Carol" for c in out)


@pytest.mark.asyncio
async def test_find_contacts_respects_limit(monkeypatch):
    """Output is capped at limit."""

    async def fake_gather(domain, name, title_filter, limit):
        return [
            {"source": "ai", "full_name": f"Person{i}", "email": f"p{i}@co.com", "title": "Buyer"} for i in range(20)
        ]

    monkeypatch.setattr(enrichment_router, "gather_contacts", fake_gather)

    out = await es.find_suggested_contacts("co.com", "Co", limit=5)
    assert len(out) <= 5
