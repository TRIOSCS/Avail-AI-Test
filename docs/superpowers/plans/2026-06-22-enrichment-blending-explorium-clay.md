# Unified Enrichment Blending (Explorium + Clay) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Activate Explorium and Clay as working enrichment sources and replace the first-write-wins waterfall with a per-field source-authority "blending" layer across all company/contact providers.

**Architecture:** A pure-logic authority ladder (`firmo_tiers.py`, ported from `spec_tiers.py`) arbitrates each field by `(tier, confidence)`. An orchestration layer (`enrichment_router.py`) calls providers in a cost-tiered, gap-gated order (free → metered → AI) and feeds their results to the ladder. `enrich_entity` / `find_suggested_contacts` stay as the public façade; `apply_enrichment_to_*` become provenance-aware so a higher-tier source can correct a lower-tier value while never clobbering manual/legacy data. Explorium gets a correct connector; Clay moves from the dead webhook path to a backend MCP client.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.0, PostgreSQL 16, Alembic, httpx (shared `app/http_client.py`), pytest (`-n auto`, in-memory SQLite, `TESTING=1`).

## Global Constraints

- Run all pytest with `TESTING=1 PYTHONPATH=<worktree> /root/availai/.venv/bin/python -m pytest`.
- New providers integrate via `get_credential_cached(source, ENV_VAR)` (DB→env) and the `enrichment_credit_guard` circuit (`circuit_open`/`trip_circuit`/`ProviderQuotaError`). Never add a second credential mechanism.
- Connectors mirror `app/connectors/lusha.py`: use the shared `http` singleton; raise `ProviderQuotaError` on 402/429; degrade to `None`/`[]` on any other error (enrichment never raises to the caller).
- Every new file gets a header comment: what it does / what calls it / what it depends on (CLAUDE.md).
- All schema changes via Alembic only (no DDL in startup/services). Coordinate the migration number through `MIGRATION_NUMBERS_IN_FLIGHT.txt`; verify `alembic heads` is single before committing.
- Status/source strings: a provider's source label MUST be registered in the tier tables (unknown → tier 0 → loses every conflict).
- Build behind flags **off**; do not enable in code. Enabling is a deploy step.
- Keep responses/files focused; loguru for logging, never `print()`.

---

## File Structure

| File | Responsibility |
|---|---|
| `app/services/firmo_tiers.py` (new) | Per-field authority tables + `firmo_tier`/`contact_tier` + `blend_company`/`blend_contacts` (pure). |
| `app/services/enrichment_router.py` (new) | Cost-tiered, gap-gated provider orchestration: `gather_company`, `gather_contacts`. |
| `app/connectors/explorium.py` (new) | Correct Explorium client (match→firmographics, prospects→contacts). |
| `app/connectors/clay_mcp.py` (new) | Backend Clay MCP client: sync `enrich_company`, polled `find_contacts`. |
| `app/connectors/sam_gov_company.py` (new) | Thin domain/name→firmographics adapter over the SAM.gov entity API. |
| `alembic/versions/<rev>_enrichment_provenance.py` (new) | `ticker/naics/revenue_range/enrichment_provenance` on `companies`+`vendor_cards`. |
| `app/enrichment_service.py` (modify) | Façade delegates to router+ladder; provenance-aware apply; drop broken `_explorium_*`. |
| `app/config.py` (modify) | Add hunter/apollo/sam flags+cooldowns. |
| `app/connectors/apollo.py`, `app/connectors/hunter.py` (modify) | Raise `ProviderQuotaError` on 402/429. |
| `app/services/clay_service.py` (modify) | Keep contact-apply helpers; delete webhook outbound. |
| `app/routers/crm/enrichment.py`, `app/routers/v13_features/activity.py` (modify) | Remove webhook trigger + callback endpoint. |
| `app/models/crm.py`, `app/models/vendors.py` (modify) | New columns. |
| `app/templates/htmx/partials/settings/api_keys.html`, `app/routers/htmx_views.py` (modify) | Explorium/Apollo/Hunter cards + context. |
| `app/data/api_sources.json` (modify) | Clay env vars → `CLAY_API_KEY`. |

---

### Task 0: Clay MCP headless-auth spike (GATE)

This is a spike, not TDD. It must pass before Task 5. It answers: *can the AvailAI backend authenticate to `https://api.clay.com/v3/mcp` headlessly with `CLAY_API_KEY` (no claude.ai OAuth)?*

**Files:**
- Create (throwaway): `scripts/spike_clay_mcp.py`

- [ ] **Step 1: Decide the transport.** Check the `mcp` SDK is installable; if added, prefer the Streamable-HTTP client. Otherwise implement a minimal JSON-RPC POST. Add to `requirements.in` (NOT `.txt`) only if the SDK is used: `mcp` ; then `pip-compile --no-header --no-strip-extras requirements.in`.

- [ ] **Step 2: Write the spike.** POST a JSON-RPC `initialize` then a `tools/call` for `get-credits-available` to `https://api.clay.com/v3/mcp` with header `Authorization: Bearer <CLAY_API_KEY>` (key from env for the spike). Print the HTTP status, any auth challenge, and the response body.

```python
# scripts/spike_clay_mcp.py — throwaway; confirms headless API-key auth to Clay MCP.
import asyncio, json, os, httpx

URL = "https://api.clay.com/v3/mcp"
KEY = os.environ["CLAY_API_KEY"]

async def main():
    headers = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "availai", "version": "1.0"}}}
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(URL, headers=headers, json=init)
        print("initialize:", r.status_code, r.headers.get("mcp-session-id"), r.text[:500])
        sid = r.headers.get("mcp-session-id")
        call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "get-credits-available", "arguments": {}}}
        h2 = dict(headers); h2["mcp-session-id"] = sid if sid else ""
        r2 = await c.post(URL, headers=h2, json=call)
        print("call:", r2.status_code, r2.text[:800])

asyncio.run(main())
```

- [ ] **Step 3: Run it.** `CLAY_API_KEY=<key> /root/availai/.venv/bin/python scripts/spike_clay_mcp.py`
  Expected: a 200 (or 200 SSE) with `hasWorkspaceCredits` in the body → headless auth works.

- [ ] **Step 4: Record the verdict** in the plan PR description: the working header/handshake, session-id requirement, and response framing (JSON vs SSE). **If it returns 401/403/upgrade-required**, STOP — do not build a fake path; report the blocker (the user is on Launch; Clay may gate headless API-key MCP). Delete `scripts/spike_clay_mcp.py` before moving on (`git rm` if it was added).

---

### Task 1: Authority ladder — `firmo_tiers.py`

**Files:**
- Create: `app/services/firmo_tiers.py`
- Test: `tests/test_firmo_tiers.py`

**Interfaces:**
- Produces: `firmo_tier(field: str, source: str) -> int`; `contact_tier(field: str, source: str) -> int`; `blend_company(results: list[dict]) -> dict` (flat firmographics + `"source"` composite + `"_provenance": {field: {"source","tier","confidence"}}`); `blend_contacts(results: list[dict]) -> list[dict]` (deduped, per-field best, each contact carries `verified`).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_firmo_tiers.py
from app.services import firmo_tiers as ft

def test_per_field_authority_overrides_base():
    # SAM wins legal_name; Explorium wins ticker; Lusha wins phone.
    assert ft.firmo_tier("legal_name", "sam_gov") > ft.firmo_tier("legal_name", "explorium")
    assert ft.firmo_tier("ticker", "explorium") > ft.firmo_tier("ticker", "clay")
    assert ft.contact_tier("phone", "lusha") > ft.contact_tier("phone", "apollo")

def test_unknown_source_is_tier_zero():
    assert ft.firmo_tier("industry", "totally_unknown") == 0

def test_blend_company_highest_tier_wins_per_field():
    results = [
        {"source": "apollo", "industry": "Wholesale", "legal_name": "Arrow Inc"},
        {"source": "explorium", "industry": "Electronics Distribution", "ticker": "ARW"},
    ]
    blended = ft.blend_company(results)
    assert blended["industry"] == "Electronics Distribution"   # explorium > apollo
    assert blended["legal_name"] == "Arrow Inc"                # only apollo had it
    assert blended["ticker"] == "ARW"
    assert set(blended["source"].split("+")) == {"apollo", "explorium"}
    assert blended["_provenance"]["industry"]["source"] == "explorium"

def test_blend_company_skips_empty_values():
    results = [{"source": "ai", "industry": None, "website": ""}]
    assert ft.blend_company(results) == {}

def test_blend_contacts_dedups_and_prefers_verified_email():
    results = [
        {"source": "apollo", "full_name": "Jane Doe", "email": "j@x.com", "verified": False, "title": "Buyer"},
        {"source": "lusha", "full_name": "Jane Doe", "email": "j@x.com", "verified": True, "phone": "+1"},
    ]
    out = ft.blend_contacts(results)
    assert len(out) == 1
    assert out[0]["verified"] is True
    assert out[0]["phone"] == "+1"
    assert out[0]["title"] == "Buyer"
```

- [ ] **Step 2: Run to verify it fails**
  Run: `TESTING=1 PYTHONPATH=$PWD /root/availai/.venv/bin/python -m pytest tests/test_firmo_tiers.py -q`
  Expected: FAIL (module not found).

- [ ] **Step 3: Implement `app/services/firmo_tiers.py`**

```python
"""Per-field source-authority ladder for company/contact enrichment blending.

Ports the materials F1 tier mechanic (app/services/spec_tiers.py) to firmographics
and contacts: for each field the value from the highest-authority source wins, ties
broken by confidence. Unknown source → tier 0 (loses every conflict).

Called by: app/services/enrichment_router.py, app/enrichment_service.py (blend + apply).
Depends on: nothing (pure logic).
"""

from loguru import logger

FIRMO_BASE_TIER: dict[str, int] = {
    "manual": 100, "explorium": 85, "lusha": 75, "clay": 70,
    "apollo": 65, "sam_gov": 60, "hunter": 40, "ai": 30,
}
FIRMO_FIELD_TIER: dict[str, dict[str, int]] = {
    "legal_name": {"sam_gov": 95, "explorium": 85, "lusha": 75, "clay": 70, "apollo": 65, "ai": 30},
    "naics": {"sam_gov": 95, "explorium": 85, "ai": 30},
    "ticker": {"explorium": 90, "clay": 75, "ai": 30},
    "revenue_range": {"explorium": 90, "clay": 75, "ai": 30},
    "employee_size": {"explorium": 85, "apollo": 75, "lusha": 70, "clay": 70, "ai": 30},
    "industry": {"explorium": 85, "apollo": 70, "clay": 70, "lusha": 65, "ai": 30},
    "hq_city": {"explorium": 85, "sam_gov": 80, "apollo": 65, "lusha": 60, "clay": 60, "ai": 30},
    "hq_state": {"explorium": 85, "sam_gov": 80, "apollo": 65, "lusha": 60, "clay": 60, "ai": 30},
    "hq_country": {"explorium": 85, "sam_gov": 80, "apollo": 65, "lusha": 60, "clay": 60, "ai": 30},
    "website": {"explorium": 80, "clay": 70, "apollo": 65, "lusha": 60, "ai": 30},
    "domain": {"explorium": 80, "clay": 70, "apollo": 65, "lusha": 60, "ai": 30},
    "linkedin_url": {"explorium": 85, "lusha": 80, "apollo": 65, "clay": 60, "ai": 30},
}
CONTACT_BASE_TIER: dict[str, int] = {
    "lusha": 80, "explorium": 70, "apollo": 70, "clay": 65, "hunter": 50, "ai": 30,
}
CONTACT_FIELD_TIER: dict[str, dict[str, int]] = {
    "phone": {"lusha": 95, "apollo": 70, "explorium": 65, "hunter": 50, "ai": 30},
    "email": {"lusha": 95, "hunter": 85, "apollo": 70, "explorium": 65, "ai": 30},
    "title": {"explorium": 80, "apollo": 75, "lusha": 70, "clay": 65, "hunter": 50, "ai": 30},
    "full_name": {"lusha": 80, "apollo": 70, "explorium": 70, "clay": 65, "hunter": 50, "ai": 30},
    "linkedin_url": {"lusha": 80, "apollo": 70, "explorium": 70, "clay": 65, "hunter": 50, "ai": 30},
}
_warned: set[str] = set()


def firmo_tier(field: str, source: str) -> int:
    t = FIRMO_FIELD_TIER.get(field, {}).get(source)
    if t is None:
        t = FIRMO_BASE_TIER.get(source, 0)
    if t == 0 and source not in _warned:
        _warned.add(source)
        logger.warning("firmo_tier: unknown source {!r} → tier 0 (loses every conflict)", source)
    return t


def contact_tier(field: str, source: str) -> int:
    return CONTACT_FIELD_TIER.get(field, {}).get(source, CONTACT_BASE_TIER.get(source, 0))


_CONTACT_FIELDS = ("full_name", "email", "phone", "title", "linkedin_url", "location", "company")


def blend_company(results: list[dict]) -> dict:
    blended: dict = {}
    prov: dict = {}
    sources: list[str] = []
    for r in results:
        if not r:
            continue
        src = r.get("source") or "unknown"
        if src not in sources:
            sources.append(src)
        conf_map = r.get("_confidence") if isinstance(r.get("_confidence"), dict) else {}
        for field, value in r.items():
            if field in ("source", "_provenance", "_confidence") or not value:
                continue
            tier = firmo_tier(field, src)
            conf = float(conf_map.get(field, 1.0))
            cur = prov.get(field)
            if cur is None or (tier, conf) > (cur["tier"], cur["confidence"]):
                blended[field] = value
                prov[field] = {"source": src, "tier": tier, "confidence": conf}
    if blended:
        blended["source"] = "+".join(sources)
        blended["_provenance"] = prov
    return blended


def _contact_key(c: dict) -> str:
    return (c.get("email") or "").strip().lower() or c.get("linkedin_url") or (c.get("full_name") or "").strip().lower()


def blend_contacts(results: list[dict]) -> list[dict]:
    """Dedup by email→linkedin→name; per field keep the highest contact_tier value
    (a verified email/phone gets a confidence bump so it beats an unverified peer)."""
    merged: dict[str, dict] = {}
    field_prov: dict[str, dict] = {}
    for c in results:
        if not c:
            continue
        key = _contact_key(c)
        if not key:
            continue
        src = c.get("source") or "unknown"
        verified = bool(c.get("verified"))
        if key not in merged:
            merged[key] = {"source": src, "verified": verified}
            field_prov[key] = {}
        row, fp = merged[key], field_prov[key]
        row["verified"] = row.get("verified") or verified
        for field in _CONTACT_FIELDS:
            value = c.get(field)
            if not value:
                continue
            conf = 0.9 if (field in ("email", "phone") and verified) else 0.5
            tier = contact_tier(field, src)
            cur = fp.get(field)
            if cur is None or (tier, conf) > cur:
                row[field] = value
                fp[field] = (tier, conf)
    return list(merged.values())
```

- [ ] **Step 4: Run to verify it passes**
  Run: `TESTING=1 PYTHONPATH=$PWD /root/availai/.venv/bin/python -m pytest tests/test_firmo_tiers.py -q` — Expected: PASS.

- [ ] **Step 5: Commit**
```bash
git add app/services/firmo_tiers.py tests/test_firmo_tiers.py
git commit -m "feat(enrichment): per-field source-authority ladder (firmo_tiers)"
```

---

### Task 2: Tier-table drift guard

**Files:**
- Test: `tests/test_firmo_tiers_invariants.py`

- [ ] **Step 1: Write the failing test** (locks the policy so an accidental edit is caught)

```python
# tests/test_firmo_tiers_invariants.py
from app.services import firmo_tiers as ft

KNOWN = {"manual", "explorium", "lusha", "clay", "apollo", "sam_gov", "hunter", "ai"}

def test_every_field_source_is_known():
    for field, table in {**ft.FIRMO_FIELD_TIER, **ft.CONTACT_FIELD_TIER}.items():
        for src in table:
            assert src in KNOWN, f"{field}:{src} not in KNOWN sources"

def test_manual_outranks_everything_for_firmo():
    for field in ft.FIRMO_FIELD_TIER:
        assert ft.firmo_tier(field, "manual") >= max(ft.FIRMO_FIELD_TIER[field].values())

def test_ai_is_lowest_nonzero_everywhere():
    for field, table in ft.FIRMO_FIELD_TIER.items():
        if "ai" in table:
            assert table["ai"] == min(table.values())
```

- [ ] **Step 2: Run — expect FAIL** (manual missing from per-field tables → `test_manual_outranks_everything` fails).
- [ ] **Step 3: Fix** by relying on the base tier: change the assertion to compare against per-field max OR add `manual` to base only. The intended invariant is "manual via base (100) ≥ any per-field value." Adjust `test_manual_outranks_everything_for_firmo` to use `ft.firmo_tier(field, "manual")` (already reads base 100) — it passes since base 100 ≥ field max (≤95). No code change needed; the test documents the invariant.
- [ ] **Step 4: Run — expect PASS.**
- [ ] **Step 5: Commit**
```bash
git add tests/test_firmo_tiers_invariants.py
git commit -m "test(enrichment): guard firmo tier-table invariants"
```

---

### Task 3: Config flags + Apollo/Hunter circuit coverage

**Files:**
- Modify: `app/config.py` (after the lusha block, ~line 301)
- Modify: `app/connectors/apollo.py`, `app/connectors/hunter.py`
- Test: `tests/test_provider_quota_coverage.py`

**Interfaces:**
- Produces: settings `apollo_enrichment_enabled`, `apollo_cooldown_minutes`, `hunter_enrichment_enabled`, `hunter_cooldown_minutes`, `sam_gov_enrichment_enabled`; apollo/hunter connectors raise `ProviderQuotaError` on 402/429.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_provider_quota_coverage.py
import pytest, httpx
from app.config import settings
from app.services.enrichment_credit_guard import ProviderQuotaError

def test_new_provider_settings_exist():
    for attr in ("apollo_enrichment_enabled", "apollo_cooldown_minutes",
                 "hunter_enrichment_enabled", "hunter_cooldown_minutes",
                 "sam_gov_enrichment_enabled"):
        assert hasattr(settings, attr)

@pytest.mark.asyncio
async def test_apollo_raises_quota_on_429(monkeypatch):
    from app.connectors import apollo
    class R: status_code = 429
    async def fake_get(*a, **k): return R()
    monkeypatch.setattr(apollo.http, "get", fake_get, raising=False)
    with pytest.raises(ProviderQuotaError):
        await apollo.search_company("x.com", "key")
```

- [ ] **Step 2: Run — expect FAIL.**
- [ ] **Step 3: Implement.** In `app/config.py` after `prospect_enrich_contacts_per_account` (~:301):
```python
    apollo_enrichment_enabled: bool = False
    apollo_cooldown_minutes: int = 15
    hunter_enrichment_enabled: bool = False
    hunter_cooldown_minutes: int = 15
    sam_gov_enrichment_enabled: bool = False
```
In `app/connectors/apollo.py` and `app/connectors/hunter.py`, add at the top: `from app.services.enrichment_credit_guard import ProviderQuotaError` and `_QUOTA_STATUSES = (402, 429)`; in each request method, after getting `resp`, insert `if resp.status_code in _QUOTA_STATUSES: raise ProviderQuotaError(f"<provider> quota: {resp.status_code}")` *before* the generic non-200 handling. (Match the exact pattern in `lusha.py:91-92`.)

- [ ] **Step 4: Run — expect PASS.**
- [ ] **Step 5: Commit**
```bash
git add app/config.py app/connectors/apollo.py app/connectors/hunter.py tests/test_provider_quota_coverage.py
git commit -m "feat(enrichment): apollo/hunter/sam config flags + quota circuit coverage"
```

---

### Task 4: Explorium connector rewrite

**Files:**
- Create: `app/connectors/explorium.py`
- Test: `tests/test_explorium_connector.py`

**Interfaces:**
- Produces: `async enrich_company(domain: str, name: str, api_key: str) -> dict | None` (firmographic shape incl. `naics/ticker/revenue_range`); `async search_contacts(domain: str, name: str, api_key: str, title_filter: str, limit: int) -> list[dict]` (contact shape incl. `verified`).

- [ ] **Step 1: Write failing tests** (mock the shared `http`; assert the real two-call pipeline, `api_key` header, envelope parse, quota raise).

```python
# tests/test_explorium_connector.py
import pytest
from app.services.enrichment_credit_guard import ProviderQuotaError

class Resp:
    def __init__(self, status, payload): self.status_code, self._p = status, payload
    def json(self): return self._p

@pytest.mark.asyncio
async def test_enrich_company_match_then_firmographics(monkeypatch):
    from app.connectors import explorium
    calls = []
    async def fake_post(url, **k):
        calls.append((url, k.get("headers", {}), k.get("json")))
        if url.endswith("/businesses/match"):
            return Resp(200, {"data": {"matched_businesses": [{"business_id": "abc"}]}})
        return Resp(200, {"data": {"name": "Arrow", "website": "https://arrow.com",
            "linkedin_industry_category": "Electronics", "naics": "423690", "ticker": "ARW",
            "yearly_revenue_range": {"min": 10_000_000_000}, "number_of_employees_range": {"min": 10001},
            "city_name": "Centennial", "region_name": "Colorado", "country_name": "US",
            "linkedin_profile": "https://linkedin.com/company/arrow"}})
    monkeypatch.setattr(explorium.http, "post", fake_post, raising=False)
    out = await explorium.enrich_company("arrow.com", "Arrow", "K")
    assert calls[0][1]["api_key"] == "K"           # api_key header, not Bearer
    assert out["legal_name"] == "Arrow" and out["naics"] == "423690" and out["ticker"] == "ARW"
    assert out["hq_state"] == "Colorado" and out["industry"] == "Electronics"
    assert out["source"] == "explorium"

@pytest.mark.asyncio
async def test_enrich_company_no_match_returns_none(monkeypatch):
    from app.connectors import explorium
    async def fake_post(url, **k): return Resp(200, {"data": {"matched_businesses": [{"business_id": None}]}})
    monkeypatch.setattr(explorium.http, "post", fake_post, raising=False)
    assert await explorium.enrich_company("nope.com", "", "K") is None

@pytest.mark.asyncio
async def test_quota_raises(monkeypatch):
    from app.connectors import explorium
    async def fake_post(url, **k): return Resp(429, {})
    monkeypatch.setattr(explorium.http, "post", fake_post, raising=False)
    with pytest.raises(ProviderQuotaError):
        await explorium.enrich_company("x.com", "", "K")
```

- [ ] **Step 2: Run — expect FAIL.**
- [ ] **Step 3: Implement `app/connectors/explorium.py`** (mirror `lusha.py`; real endpoints/auth/envelope per the spec §4):

```python
"""Explorium API connector (real v1 pipeline: match → firmographics; prospects → contacts).

Auth is the `api_key:` header (NOT Authorization: Bearer). Company enrichment is a
2-call pipeline (match to a business_id, then firmographics/enrich). 402/403/429 →
ProviderQuotaError; other errors degrade to None/[].

Called by: app/services/enrichment_router.py. Depends on: app/http_client.py (http),
app/services/enrichment_credit_guard (ProviderQuotaError).
"""
import httpx
from loguru import logger
from app.http_client import http
from app.services.enrichment_credit_guard import ProviderQuotaError

BASE = "https://api.explorium.ai/v1"
_QUOTA_STATUSES = (402, 403, 429)


def _headers(api_key: str) -> dict:
    return {"api_key": api_key, "Content-Type": "application/json"}


def _data(resp) -> dict:
    body = resp.json() if resp is not None else {}
    return body.get("data") if isinstance(body.get("data"), dict) else body


def _fmt_band(obj) -> str | None:
    if isinstance(obj, dict):
        lo, hi = obj.get("min"), obj.get("max")
        if lo and hi:
            return f"{lo}-{hi}"
        return str(lo or hi) if (lo or hi) else None
    return str(obj) if obj else None


async def _post(path: str, api_key: str, body: dict):
    resp = await http.post(f"{BASE}{path}", headers=_headers(api_key), json=body, timeout=20)
    if resp.status_code in _QUOTA_STATUSES:
        raise ProviderQuotaError(f"Explorium {path} quota/limit: {resp.status_code}")
    if resp.status_code != 200:
        logger.warning("Explorium {} failed: {}", path, resp.status_code)
        return None
    return resp


async def _match_business_id(domain: str, name: str, api_key: str) -> str | None:
    resp = await _post("/businesses/match", api_key, {"businesses_to_match": [{"name": name, "domain": domain}]})
    matched = (_data(resp) or {}).get("matched_businesses") or []
    return (matched[0].get("business_id") if matched else None) or None


async def enrich_company(domain: str, name: str, api_key: str) -> dict | None:
    try:
        bid = await _match_business_id(domain, name, api_key)
        if not bid:
            return None
        resp = await _post("/businesses/firmographics/enrich", api_key, {"business_id": bid})
        f = _data(resp) or {}
        if not f:
            return None
        out = {
            "source": "explorium",
            "legal_name": f.get("name"),
            "domain": (f.get("website") or domain).replace("https://", "").replace("http://", "").split("/")[0],
            "website": f.get("website"),
            "industry": f.get("linkedin_industry_category"),
            "employee_size": _fmt_band(f.get("number_of_employees_range")),
            "hq_city": f.get("city_name"),
            "hq_state": f.get("region_name"),
            "hq_country": f.get("country_name"),
            "linkedin_url": f.get("linkedin_profile"),
            "naics": f.get("naics"),
            "ticker": f.get("ticker"),
            "revenue_range": _fmt_band(f.get("yearly_revenue_range")),
        }
        return out if any(v for k, v in out.items() if k not in ("source", "domain")) else None
    except (httpx.HTTPError, KeyError, ValueError) as e:
        logger.warning("Explorium company error: {}", e)
        return None


async def search_contacts(domain: str, name: str, api_key: str, title_filter: str, limit: int) -> list[dict]:
    try:
        bid = await _match_business_id(domain, name, api_key)
        if not bid:
            return []
        filters: dict = {"business_id": {"values": [bid]}, "has_email": True}
        if title_filter:
            filters["job_title"] = {"values": [title_filter]}
        resp = await _post("/prospects", api_key, {"filters": filters, "size": limit})
        rows = ((resp.json().get("data") if resp else None) or []) if resp else []
        contacts: list[dict] = []
        for p in rows[:limit]:
            pid = p.get("prospect_id")
            ci = _data(await _post("/prospects/contacts_information/enrich", api_key, {"prospect_id": pid})) if pid else {}
            ci = ci or {}
            contacts.append({
                "source": "explorium",
                "full_name": p.get("full_name"),
                "title": p.get("job_title"),
                "linkedin_url": p.get("linkedin"),
                "location": p.get("city") or p.get("region_name"),
                "company": p.get("company_name"),
                "email": ci.get("professional_email"),
                "phone": ci.get("mobile_phone") or ((ci.get("phone_numbers") or [None])[0]),
                "verified": (ci.get("professional_email_status") == "valid"),
            })
        return [c for c in contacts if c.get("full_name")]
    except (httpx.HTTPError, KeyError, ValueError) as e:
        logger.warning("Explorium contacts error: {}", e)
        return []
```

- [ ] **Step 4: Run — expect PASS.**
- [ ] **Step 5: Commit**
```bash
git add app/connectors/explorium.py tests/test_explorium_connector.py
git commit -m "feat(enrichment): correct Explorium connector (real match→firmographics→contacts API)"
```

---

### Task 5: Clay MCP connector

> Depends on Task 0. Use the transport the spike confirmed. Below assumes JSON-RPC over httpx with `Authorization: Bearer <CLAY_API_KEY>` + `mcp-session-id`; adjust `_mcp_call` to match the spike.

**Files:**
- Create: `app/connectors/clay_mcp.py`
- Test: `tests/test_clay_mcp_connector.py`

**Interfaces:**
- Produces: `async enrich_company(domain: str) -> dict | None`; `async find_contacts(domain: str, title_filter: str, limit: int, want_email: bool) -> list[dict]`. Both read `CLAY_API_KEY` via `get_credential_cached("clay_enrichment", "CLAY_API_KEY")`.

- [ ] **Step 1: Write failing tests** (mock `_mcp_call`/`_poll_task`; assert mapping from the real spike shape + domain filter + quota raise).

```python
# tests/test_clay_mcp_connector.py
import pytest
from app.services.enrichment_credit_guard import ProviderQuotaError

COMPANY = {"companies": {"arrow.com": {"name": "Arrow Electronics", "domain": "arrow.com",
    "website": "https://arrow.com", "industry": "Technology", "size": "10,001+ employees",
    "annual_revenue": "10B-100B", "locality": "Centennial, Colorado", "country": "US",
    "url": "https://www.linkedin.com/company/arrow-electronics",
    "description": "Arrow Electronics (NYSE:ARW) ..."}}}

@pytest.mark.asyncio
async def test_enrich_company_maps_base_fields(monkeypatch):
    from app.connectors import clay_mcp
    monkeypatch.setattr(clay_mcp, "_resolve_key", lambda: "K")
    async def fake_call(tool, args): return COMPANY
    monkeypatch.setattr(clay_mcp, "_mcp_call", fake_call)
    out = await clay_mcp.enrich_company("arrow.com")
    assert out["legal_name"] == "Arrow Electronics" and out["hq_state"] == "Colorado"
    assert out["revenue_range"] == "10B-100B" and out["ticker"] == "ARW" and out["source"] == "clay"

@pytest.mark.asyncio
async def test_find_contacts_filters_to_target_domain(monkeypatch):
    from app.connectors import clay_mcp
    monkeypatch.setattr(clay_mcp, "_resolve_key", lambda: "K")
    async def fake_call(tool, args):
        return {"taskId": "t1", "contacts": [
            {"name": "Jane", "latest_experience_title": "Buyer", "domain": "arrow.com",
             "url": "https://li/jane", "location_name": "US"},
            {"name": "Ex Employee", "latest_experience_title": "X", "domain": "other.com"}]}
    monkeypatch.setattr(clay_mcp, "_mcp_call", fake_call)
    async def fake_poll(task_id, n): return {}
    monkeypatch.setattr(clay_mcp, "_poll_emails", fake_poll)
    out = await clay_mcp.find_contacts("arrow.com", "", 10, want_email=False)
    assert [c["full_name"] for c in out] == ["Jane"]

@pytest.mark.asyncio
async def test_disabled_without_key(monkeypatch):
    from app.connectors import clay_mcp
    monkeypatch.setattr(clay_mcp, "_resolve_key", lambda: "")
    assert await clay_mcp.enrich_company("x.com") is None
```

- [ ] **Step 2: Run — expect FAIL.**
- [ ] **Step 3: Implement `app/connectors/clay_mcp.py`** (transport per spike; mapping per real spike response):

```python
"""Clay enrichment via the backend MCP client (no webhook).

Calls the hosted Clay MCP (https://api.clay.com/v3/mcp) with CLAY_API_KEY:
- enrich_company: find-and-enrich-company returns base firmographics synchronously.
- find_contacts: find-and-enrich-contacts-at-company returns a base contact list inline;
  emails (Email data point) are polled via get-task-context (bounded).

Called by: app/services/enrichment_router.py. Depends on: app/http_client.py,
app/services/credential_service (key), app/services/enrichment_credit_guard (circuit).
"""
import asyncio
import re
import httpx
from loguru import logger
from app.http_client import http
from app.services.credential_service import get_credential_cached
from app.services.enrichment_credit_guard import ProviderQuotaError

MCP_URL = "https://api.clay.com/v3/mcp"
_QUOTA_STATUSES = (402, 429)
_POLL_TRIES = 5
_POLL_DELAY = 3.0
_TICKER_RE = re.compile(r"\((?:NYSE|NASDAQ):\s*([A-Z]{1,6})\)")


def _resolve_key() -> str:
    return get_credential_cached("clay_enrichment", "CLAY_API_KEY") or ""


async def _mcp_call(tool: str, args: dict) -> dict:
    """JSON-RPC tools/call to the Clay MCP. Transport confirmed by the Task 0 spike."""
    key = _resolve_key()
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": tool, "arguments": args}}
    resp = await http.post(MCP_URL, headers=headers, json=body, timeout=40)
    if resp.status_code in _QUOTA_STATUSES:
        raise ProviderQuotaError(f"Clay MCP quota/rate-limit: {resp.status_code}")
    if resp.status_code != 200:
        logger.warning("Clay MCP {} failed: {}", tool, resp.status_code)
        return {}
    payload = resp.json()
    # tools/call result content → structured JSON (unwrap per spike).
    result = payload.get("result", payload)
    content = result.get("structuredContent") or result
    return content if isinstance(content, dict) else {}


def _parse_locality(locality: str) -> tuple[str | None, str | None]:
    if not locality:
        return None, None
    parts = [p.strip() for p in locality.split(",")]
    return (parts[0] or None, parts[1] if len(parts) > 1 else None)


def _map_company(domain: str, payload: dict) -> dict | None:
    comp = (payload.get("companies") or {}).get(domain) or {}
    if not comp:
        return None
    city, state = _parse_locality(comp.get("locality") or "")
    ticker_m = _TICKER_RE.search(comp.get("description") or "")
    out = {
        "source": "clay",
        "legal_name": comp.get("name"),
        "domain": comp.get("domain") or domain,
        "website": comp.get("website"),
        "industry": comp.get("industry"),
        "employee_size": comp.get("size"),
        "hq_city": city, "hq_state": state, "hq_country": comp.get("country"),
        "linkedin_url": comp.get("url"),
        "revenue_range": comp.get("annual_revenue"),
        "ticker": ticker_m.group(1) if ticker_m else None,
    }
    return out if any(v for k, v in out.items() if k not in ("source", "domain")) else None


async def enrich_company(domain: str) -> dict | None:
    if not _resolve_key():
        return None
    try:
        payload = await _mcp_call("find-and-enrich-company", {"companyIdentifier": domain})
        return _map_company(domain, payload)
    except (httpx.HTTPError, KeyError, ValueError) as e:
        logger.warning("Clay MCP company error: {}", e)
        return None


async def _poll_emails(task_id: str, n: int) -> dict:
    """Poll get-task-context until Email enrichments complete; return {entityId: email}."""
    emails: dict = {}
    for _ in range(n):
        await asyncio.sleep(_POLL_DELAY)
        ctx = await _mcp_call("get-task-context", {"taskId": task_id})
        done = True
        for c in ctx.get("contacts") or []:
            for enr in c.get("enrichments") or []:
                if (enr.get("name") or "").lower().startswith("email"):
                    if enr.get("state") == "completed" and enr.get("value"):
                        emails[c.get("entityId")] = enr["value"]
                    elif enr.get("state") == "in-progress":
                        done = False
        if done:
            break
    return emails


async def find_contacts(domain: str, title_filter: str, limit: int, want_email: bool) -> list[dict]:
    if not _resolve_key():
        return []
    try:
        args: dict = {"companyIdentifier": domain}
        if title_filter:
            args["contactFilters"] = {"job_title_keywords": [title_filter]}
        if want_email:
            args["dataPoints"] = {"contactDataPoints": [{"type": "Email"}]}
        payload = await _mcp_call("find-and-enrich-contacts-at-company", args)
        raw = [c for c in (payload.get("contacts") or []) if c.get("domain") == domain][:limit]
        emails = await _poll_emails(payload.get("taskId"), _POLL_TRIES) if (want_email and payload.get("taskId")) else {}
        out = []
        for c in raw:
            out.append({
                "source": "clay",
                "full_name": c.get("name"),
                "title": c.get("latest_experience_title"),
                "linkedin_url": c.get("url"),
                "location": c.get("location_name"),
                "company": c.get("latest_experience_company"),
                "email": emails.get(c.get("entityId")),
                "verified": False,
            })
        return [c for c in out if c.get("full_name")]
    except (httpx.HTTPError, KeyError, ValueError) as e:
        logger.warning("Clay MCP contacts error: {}", e)
        return []
```

- [ ] **Step 4: Run — expect PASS.**
- [ ] **Step 5: Commit**
```bash
git add app/connectors/clay_mcp.py tests/test_clay_mcp_connector.py
git commit -m "feat(enrichment): backend Clay MCP connector (sync firmographics + polled emails)"
```

---

### Task 6: Provenance migration + model columns

**Files:**
- Modify: `app/models/crm.py` (Company, after :45), `app/models/vendors.py` (VendorCard, after :50)
- Create: `alembic/versions/<rev>_enrichment_provenance.py`
- Test: `tests/test_migration_enrichment_provenance.py`

**Interfaces:**
- Produces: `Company.ticker/naics/revenue_range/enrichment_provenance` and the same on `VendorCard`.

- [ ] **Step 1: Add columns to models.** In `app/models/crm.py` after line 45 (`enrichment_source`):
```python
    ticker = Column(String(20))
    naics = Column(String(20))
    revenue_range = Column(String(50))
    enrichment_provenance = Column(JSONB, default=dict, server_default="{}")
```
Mirror in `app/models/vendors.py` after the VendorCard `enrichment_source` (:50). Ensure `JSONB` is imported in both (`from sqlalchemy.dialects.postgresql import JSONB`).

- [ ] **Step 2: Write the migration test**
```python
# tests/test_migration_enrichment_provenance.py
from app.models.crm import Company
from app.models.vendors import VendorCard

def test_models_have_provenance_columns():
    for m in (Company, VendorCard):
        cols = m.__table__.columns
        for c in ("ticker", "naics", "revenue_range", "enrichment_provenance"):
            assert c in cols, f"{m.__name__} missing {c}"
```

- [ ] **Step 3: Generate + review migration.** Reserve a number in `MIGRATION_NUMBERS_IN_FLIGHT.txt`, then:
```bash
alembic revision --autogenerate -m "enrichment_provenance + firmographic columns"
```
Review: it must only `add_column` the 4 columns on `companies` + `vendor_cards`; downgrade drops them. Confirm single head: `alembic heads`.

- [ ] **Step 4: Test up/down + run the model test**
```bash
alembic upgrade head && alembic downgrade -1 && alembic upgrade head
TESTING=1 PYTHONPATH=$PWD /root/availai/.venv/bin/python -m pytest tests/test_migration_enrichment_provenance.py -q
```
Expected: migrations clean, test PASS.

- [ ] **Step 5: Commit**
```bash
git add app/models/crm.py app/models/vendors.py alembic/versions/ MIGRATION_NUMBERS_IN_FLIGHT.txt tests/test_migration_enrichment_provenance.py
git commit -m "feat(enrichment): provenance + ticker/naics/revenue columns on Company+VendorCard"
```

---

### Task 7: Provenance-aware apply

**Files:**
- Modify: `app/enrichment_service.py:687-742`
- Test: `tests/test_apply_provenance.py`

**Interfaces:**
- Consumes: blended dict with optional `_provenance` (from Task 1).
- Produces: `apply_enrichment_to_company(company, data)` / `apply_enrichment_to_vendor(card, data)` write `ticker/naics/revenue_range`, overwrite a lower-tier provenanced value, and never clobber a value lacking provenance.

- [ ] **Step 1: Write failing tests**
```python
# tests/test_apply_provenance.py
from types import SimpleNamespace
from app.enrichment_service import apply_enrichment_to_company

def _co(**kw):
    base = dict(domain=None, linkedin_url=None, legal_name=None, industry=None,
               employee_size=None, hq_city=None, hq_state=None, hq_country=None,
               website=None, ticker=None, naics=None, revenue_range=None,
               enrichment_provenance={}, last_enriched_at=None, enrichment_source=None)
    base.update(kw); return SimpleNamespace(**base)

def test_writes_new_firmographic_fields_incl_ticker():
    c = _co()
    data = {"industry": "Electronics", "ticker": "ARW", "source": "explorium",
            "_provenance": {"industry": {"source": "explorium", "tier": 85, "confidence": 1.0},
                            "ticker": {"source": "explorium", "tier": 90, "confidence": 1.0}}}
    updated = apply_enrichment_to_company(c, data)
    assert c.ticker == "ARW" and c.industry == "Electronics" and "ticker" in updated

def test_higher_tier_overwrites_lower_tier():
    c = _co(industry="Wholesale", enrichment_provenance={"industry": {"source": "apollo", "tier": 70, "confidence": 1.0}})
    data = {"industry": "Electronics Distribution",
            "_provenance": {"industry": {"source": "explorium", "tier": 85, "confidence": 1.0}}}
    apply_enrichment_to_company(c, data)
    assert c.industry == "Electronics Distribution"

def test_never_clobbers_unprovenanced_value():
    c = _co(industry="Hand-typed", enrichment_provenance={})
    data = {"industry": "AI Guess",
            "_provenance": {"industry": {"source": "ai", "tier": 30, "confidence": 1.0}}}
    apply_enrichment_to_company(c, data)
    assert c.industry == "Hand-typed"
```

- [ ] **Step 2: Run — expect FAIL.**
- [ ] **Step 3: Implement.** Replace `apply_enrichment_to_company` (`:687-713`) and `apply_enrichment_to_vendor` (`:716-742`) with a shared helper:

```python
_ENRICH_FIELDS = (
    "domain", "linkedin_url", "legal_name", "industry", "employee_size",
    "hq_city", "hq_state", "hq_country", "website", "ticker", "naics", "revenue_range",
)


def _apply_enrichment(obj, data: dict) -> list[str]:
    updated: list[str] = []
    prov_in = data.get("_provenance") or {}
    store = dict(getattr(obj, "enrichment_provenance", None) or {})
    for field in _ENRICH_FIELDS:
        val = data.get(field)
        if not val:
            continue
        incoming = prov_in.get(field)
        current = getattr(obj, field, None)
        if not current:
            pass  # empty → write
        elif incoming is None:
            continue  # have a value, no incoming provenance → never clobber
        else:
            existing = store.get(field)
            if existing is None:
                continue  # existing value lacks provenance (manual/legacy) → protect
            inc_key = (incoming.get("tier", 0), incoming.get("confidence", 0.0))
            cur_key = (existing.get("tier", 0), existing.get("confidence", 0.0))
            if inc_key <= cur_key:
                continue
        setattr(obj, field, val)
        if incoming:
            store[field] = {"source": incoming.get("source"), "tier": incoming.get("tier", 0),
                            "confidence": incoming.get("confidence", 1.0)}
        updated.append(field)
    if updated:
        obj.enrichment_provenance = store
        obj.last_enriched_at = datetime.now(timezone.utc)
        obj.enrichment_source = data.get("source", "unknown")
    return updated


def apply_enrichment_to_company(company, data: dict) -> list[str]:
    """Apply blended enrichment to a Company (provenance-aware; protects manual values)."""
    return _apply_enrichment(company, data)


def apply_enrichment_to_vendor(card, data: dict) -> list[str]:
    """Apply blended enrichment to a VendorCard (provenance-aware; protects manual values)."""
    return _apply_enrichment(card, data)
```

- [ ] **Step 4: Run — expect PASS** (plus `tests/test_clay_service.py` apply tests still green).
- [ ] **Step 5: Commit**
```bash
git add app/enrichment_service.py tests/test_apply_provenance.py
git commit -m "feat(enrichment): provenance-aware apply (ladder overwrite, protect manual)"
```

---

### Task 8: SAM.gov company adapter + the router

**Files:**
- Create: `app/connectors/sam_gov_company.py`, `app/services/enrichment_router.py`
- Test: `tests/test_enrichment_router.py`

**Interfaces:**
- Consumes: `firmo_tiers.blend_company/blend_contacts`; connectors `explorium`, `clay_mcp`, `lusha`, `apollo`, `sam_gov_company`, `hunter`; `_ai_find_company`/`_ai_find_contacts` from `enrichment_service`.
- Produces: `async gather_company(domain, name) -> list[dict]`; `async gather_contacts(domain, name, title_filter, limit) -> list[dict]`.

- [ ] **Step 1: Write failing tests** (assert cost-tiered order + gap-gating + circuit skip; mock each provider).

```python
# tests/test_enrichment_router.py
import pytest
from app.services import enrichment_router as er

@pytest.mark.asyncio
async def test_company_order_free_then_metered_and_gap_gates(monkeypatch):
    calls = []
    async def sam(d, n): calls.append("sam"); return {"source": "sam_gov", "legal_name": "Arrow Inc"}
    async def apollo(d, n): calls.append("apollo"); return {"source": "apollo", "industry": "Wholesale",
        "employee_size": "10001+", "hq_city": "X", "hq_state": "Y", "hq_country": "US",
        "website": "arrow.com", "linkedin_url": "li", "domain": "arrow.com"}
    async def clay(d): calls.append("clay"); return None
    async def expl(d, n): calls.append("explorium"); return None
    monkeypatch.setattr(er, "_sam_company", sam)
    monkeypatch.setattr(er, "_apollo_company", apollo)
    monkeypatch.setattr(er, "_clay_company", clay)
    monkeypatch.setattr(er, "_explorium_company", expl)
    monkeypatch.setattr(er, "_lusha_company", lambda d, n: None)
    monkeypatch.setattr(er, "_ai_company", lambda d, n: None)
    results = await er.gather_company("arrow.com", "Arrow")
    assert calls[0] == "sam" and calls[1] == "apollo"
    # all enrichable fields filled by sam+apollo → metered providers gap-gated out
    assert "explorium" not in calls
```

- [ ] **Step 2: Run — expect FAIL.**
- [ ] **Step 3a: Implement `app/connectors/sam_gov_company.py`** — a thin name/domain→firmographic adapter reusing the SAM entity API used by `prospect_free_enrichment.enrich_from_sam_gov`:

```python
"""SAM.gov entity → firmographic adapter for the company enrichment chain.

Wraps the public SAM.gov entity-information API (the same source as
prospect_free_enrichment.enrich_from_sam_gov) but keyed by company name/domain and
returning the shared firmographic shape (authoritative legal_name / NAICS / HQ).

Called by: app/services/enrichment_router.py. Depends on: app/http_client.py.
"""
import httpx
from loguru import logger
from app.http_client import http
from app.services.credential_service import get_credential_cached

_URL = "https://api.sam.gov/entity-information/v3/entities"


async def enrich_company(domain: str, name: str) -> dict | None:
    key = get_credential_cached("sam_gov", "SAM_GOV_API_KEY") or "DEMO_KEY"
    if not name:
        return None
    try:
        resp = await http.get(_URL, params={"api_key": key, "legalBusinessName": name,
                                            "registrationStatus": "A"}, timeout=15)
        if resp.status_code != 200:
            return None
        ents = (resp.json().get("entityData") or [])
        if not ents:
            return None
        reg = (ents[0].get("entityRegistration") or {})
        core = (ents[0].get("coreData") or {})
        addr = (core.get("physicalAddress") or {})
        naics = (core.get("assertions", {}).get("goodsAndServices", {}).get("primaryNaics"))
        out = {"source": "sam_gov", "legal_name": reg.get("legalBusinessName"),
               "hq_city": addr.get("city"), "hq_state": addr.get("stateOrProvinceCode"),
               "hq_country": addr.get("countryCode"), "naics": naics}
        return out if any(v for k, v in out.items() if k != "source") else None
    except (httpx.HTTPError, KeyError, ValueError) as e:
        logger.warning("SAM.gov company error: {}", e)
        return None
```

- [ ] **Step 3b: Implement `app/services/enrichment_router.py`** — cost-tiered, gap-gated orchestration (each `_xxx_company` is a thin wrapper that resolves the key + guards the circuit; named so tests can monkeypatch):

```python
"""Cost-tiered, gap-gated enrichment orchestration.

Calls providers free→metered→AI, gap-gated by remaining empty firmographic fields and
guarded by the per-provider circuit. Returns the raw provider results; arbitration is
firmo_tiers.blend_company / blend_contacts.

Called by: app/enrichment_service.py (enrich_entity, find_suggested_contacts).
"""
import asyncio
from loguru import logger
from app.config import settings
from app.connectors import apollo, clay_mcp, explorium, hunter, lusha, sam_gov_company
from app.services.credential_service import get_credential_cached
from app.services.enrichment_credit_guard import ProviderQuotaError, circuit_open, trip_circuit

_GAP_FIELDS = ("legal_name", "industry", "employee_size", "hq_city", "hq_state",
               "hq_country", "website", "linkedin_url")


def _gaps_remain(results: list[dict]) -> bool:
    filled = {k for r in results if r for k, v in r.items() if v}
    return any(f not in filled for f in _GAP_FIELDS)


async def _sam_company(d, n): return await sam_gov_company.enrich_company(d, n)
async def _apollo_company(d, n):
    if not settings.apollo_api_key:
        return None
    from app.connectors.apollo import search_company
    return await search_company(d, settings.apollo_api_key)
async def _clay_company(d): return await clay_mcp.enrich_company(d)
async def _explorium_company(d, n):
    return await explorium.enrich_company(d, n, get_credential_cached("explorium_enrichment", "EXPLORIUM_API_KEY") or "")
async def _lusha_company(d, n):
    return await lusha.enrich_company(d, get_credential_cached("lusha_enrichment", "LUSHA_API_KEY") or "")
async def _ai_company(d, n):
    from app.enrichment_service import _ai_find_company
    return await _ai_find_company(d, n)


async def _guarded(provider: str, coro, cooldown: int, results: list):
    if circuit_open(provider):
        return
    try:
        r = await coro
        if r:
            results.append(r)
    except ProviderQuotaError:
        logger.warning("{} quota/rate-limit — tripping circuit", provider)
        trip_circuit(provider, cooldown)


async def gather_company(domain: str, name: str = "") -> list[dict]:
    results: list[dict] = []
    # FREE, always-run
    if settings.sam_gov_enrichment_enabled:
        await _guarded("sam_gov", _sam_company(domain, name), 15, results)
    await _guarded("apollo", _apollo_company(domain, name), settings.apollo_cooldown_minutes, results)
    # METERED, gap-gated, in cost order
    for provider, factory, cooldown, enabled in (
        ("clay", lambda: _clay_company(domain), settings.clay_cooldown_minutes, settings.clay_enrichment_enabled),
        ("explorium", lambda: _explorium_company(domain, name), settings.explorium_cooldown_minutes, settings.explorium_enrichment_enabled),
        ("lusha", lambda: _lusha_company(domain, name), settings.lusha_cooldown_minutes, settings.lusha_enrichment_enabled),
    ):
        if enabled and _gaps_remain(results):
            await _guarded(provider, factory(), cooldown, results)
    # AI last
    if _gaps_remain(results):
        await _guarded("ai", _ai_company(domain, name), 15, results)
    return results


async def gather_contacts(domain: str, name: str, title_filter: str, limit: int) -> list[dict]:
    results: list[dict] = []
    # free / cheap concurrently
    async def hunter_c():
        from app.enrichment_service import _hunter_find_contacts
        return await _hunter_find_contacts(domain)
    cheap = [hunter_c()]
    if settings.apollo_api_key:
        from app.connectors.apollo import search_contacts as apollo_contacts
        cheap.append(apollo_contacts(domain, settings.apollo_api_key, limit))
    if settings.clay_enrichment_enabled and not circuit_open("clay"):
        cheap.append(clay_mcp.find_contacts(domain, title_filter, limit, want_email=False))
    for r in await asyncio.gather(*cheap, return_exceptions=True):
        if isinstance(r, list):
            results.extend(r)
    verified_n = sum(1 for c in results if c.get("verified"))
    # escalate to paid/verified if not enough verified contacts
    if verified_n < limit:
        if settings.lusha_enrichment_enabled:
            await _guarded("lusha", lusha.search_contacts(domain, get_credential_cached("lusha_enrichment", "LUSHA_API_KEY") or "", limit), settings.lusha_cooldown_minutes, _ListSink(results))
        if settings.explorium_enrichment_enabled:
            await _guarded("explorium", explorium.search_contacts(domain, name, get_credential_cached("explorium_enrichment", "EXPLORIUM_API_KEY") or "", title_filter, limit), settings.explorium_cooldown_minutes, _ListSink(results))
    return results


class _ListSink(list):
    """Adapter so _guarded can append a provider's list result to a shared list."""
    def append(self, value):
        if isinstance(value, list):
            super().extend(value)
        elif value:
            super().append(value)
```

> Note for implementer: `gather_contacts` passes `_ListSink(results)` is wrong — `_ListSink` wraps a *new* list. Instead define `_guarded` to accept a callback, OR collect each escalation result directly. Simplify: replace the two escalation calls with direct try/except that `results.extend(...)`. Keep `_guarded` only for the company path. (Resolve in Step 3 before running tests.)

- [ ] **Step 4: Run — expect PASS.** Fix the `gather_contacts` escalation per the note (direct `try/except ProviderQuotaError: trip_circuit(...)` extending `results`).
- [ ] **Step 5: Commit**
```bash
git add app/connectors/sam_gov_company.py app/services/enrichment_router.py tests/test_enrichment_router.py
git commit -m "feat(enrichment): cost-tiered gap-gated router + SAM.gov company adapter"
```

---

### Task 9: Wire the façade to router + ladder; remove broken Explorium

**Files:**
- Modify: `app/enrichment_service.py` (`enrich_entity` :474-568, `find_suggested_contacts` :599-684; delete `_explorium_find_company` :270-313 + `_explorium_find_contacts` :316-356)
- Modify: `app/services/prospect_discovery_explorium.py` (use `app.connectors.explorium`)
- Test: `tests/test_enrich_entity_blend.py`

**Interfaces:**
- Consumes: `enrichment_router.gather_company/gather_contacts`, `firmo_tiers.blend_company/blend_contacts`.
- Produces: `enrich_entity` returns the blended flat dict (incl. `_provenance`); `find_suggested_contacts` returns deduped per-field-best contacts.

- [ ] **Step 1: Write failing test** (replaces the ordering assertions of `test_enrich_entity_lusha.py` with blend behavior)
```python
# tests/test_enrich_entity_blend.py
import pytest
import app.enrichment_service as es

@pytest.mark.asyncio
async def test_enrich_entity_blends_by_authority(monkeypatch):
    async def fake_gather(domain, name=""):
        return [{"source": "apollo", "industry": "Wholesale"},
                {"source": "explorium", "industry": "Electronics", "ticker": "ARW"}]
    monkeypatch.setattr(es.enrichment_router, "gather_company", fake_gather)
    monkeypatch.setattr(es, "get_cached", lambda k: None)
    monkeypatch.setattr(es, "set_cached", lambda *a, **k: None)
    out = await es.enrich_entity("arrow.com", "Arrow")
    assert out["industry"] == "Electronics" and out["ticker"] == "ARW"
    assert out["_provenance"]["industry"]["source"] == "explorium"
```

- [ ] **Step 2: Run — expect FAIL.**
- [ ] **Step 3: Implement.** Replace the body of `enrich_entity` (after cache check + `normalize_company_input`) with:
```python
    from app.services import enrichment_router
    from app.services.firmo_tiers import blend_company
    results = await enrichment_router.gather_company(domain, name)
    blended = blend_company(results)
    blended.setdefault("domain", domain)
    normalized = normalize_company_output(blended)
    if blended.get("_provenance"):
        normalized["_provenance"] = blended["_provenance"]
    if any(v for k, v in normalized.items() if k not in ("domain", "_provenance")):
        set_cached(cache_key, normalized, ttl_days=14)
    return normalized
```
Replace `find_suggested_contacts` body with:
```python
    from app.services import enrichment_router
    from app.services.firmo_tiers import blend_contacts
    raw = await enrichment_router.gather_contacts(domain, name, title_filter, limit)
    unique = blend_contacts(raw)
    filtered = [c for c in unique if _is_relevant(c)]
    return (filtered if filtered else unique)[:limit]
```
Delete `_explorium_find_company` and `_explorium_find_contacts` (now in the connector) and any now-unused `_merge`/inline-provider code those functions referenced (keep `_ai_find_company`, `_ai_find_contacts`, `_hunter_find_contacts`, `normalize_*`, `_is_relevant`, `_RELEVANT_KEYWORDS`). In `prospect_discovery_explorium.py`, replace its broken `/v1/businesses/search` call with `from app.connectors import explorium` and `await explorium.enrich_company(domain, name, key)` / `search_contacts(...)`.

- [ ] **Step 4: Run** the new test + the existing suite slice:
```bash
TESTING=1 PYTHONPATH=$PWD /root/availai/.venv/bin/python -m pytest tests/test_enrich_entity_blend.py tests/test_enrich_entity_lusha.py tests/test_prospect_real_enrichment.py -q
```
Expected: new PASS; update/replace any now-obsolete assertions in `test_enrich_entity_lusha.py` that asserted the old fixed order (the blend test supersedes them — rewrite those to assert blend behavior, do not delete coverage).

- [ ] **Step 5: Commit**
```bash
git add app/enrichment_service.py app/services/prospect_discovery_explorium.py tests/test_enrich_entity_blend.py tests/test_enrich_entity_lusha.py
git commit -m "feat(enrichment): route+blend in enrich_entity/find_suggested_contacts; drop broken Explorium"
```

---

### Task 10: Remove the Clay webhook path

**Files:**
- Modify: `app/services/clay_service.py` (delete `request_enrichment`, `_webhook_url`, `_secret`, `verify_secret`, `verify_signature`, correlation store; KEEP `handle_callback`? no — callback is gone too; KEEP `_add_vendor_contacts`, `_add_site_contacts`, `_confidence_from_marker`)
- Modify: `app/routers/v13_features/activity.py` (remove `POST /api/webhooks/clay` :116-159)
- Modify: `app/routers/crm/enrichment.py` (remove the webhook trigger calls ~:79-84,:113-117)
- Modify: `app/data/api_sources.json` (clay_enrichment env_vars → `["CLAY_API_KEY"]`)
- Test: update `tests/test_clay_service.py`

- [ ] **Step 1: Update the test first.** In `tests/test_clay_service.py`, delete the webhook-trigger and callback-verification tests; KEEP/retarget the contact-helper tests to import the helpers from `clay_service` (or move them to `tests/test_clay_contact_helpers.py`). Add an assertion that `clay_service` no longer exposes `request_enrichment`.
```python
def test_webhook_path_removed():
    import app.services.clay_service as cs
    assert not hasattr(cs, "request_enrichment")
```
- [ ] **Step 2: Run — expect FAIL** (attribute still present).
- [ ] **Step 3: Delete the webhook code** in the four files above, keeping the three contact helpers. Remove the `clay` import/trigger in `crm/enrichment.py`; company/vendor enrichment now flows entirely through `enrich_entity` (which calls Clay via the router). Update `api_sources.json`.
- [ ] **Step 4: Run** `tests/test_clay_service.py` (and the contact-helper tests) — expect PASS. Grep to confirm no dangling import: `grep -rn "request_enrichment\|webhooks/clay\|CLAY_WEBHOOK_URL\|CLAY_CALLBACK_SECRET" app/`.
- [ ] **Step 5: Commit**
```bash
git add app/services/clay_service.py app/routers/v13_features/activity.py app/routers/crm/enrichment.py app/data/api_sources.json tests/
git commit -m "refactor(enrichment): remove dead Clay webhook path (MCP supersedes it)"
```

---

### Task 11: Settings UI cards (Explorium / Apollo / Hunter)

**Files:**
- Modify: `app/templates/htmx/partials/settings/api_keys.html` (add cards after the Lusha card, before Clay)
- Modify: `app/routers/htmx_views.py:10424` (context dict in `settings_api_keys_tab`)
- Test: `tests/test_settings_api_keys_cards.py`

- [ ] **Step 1: Write failing test**
```python
# tests/test_settings_api_keys_cards.py
def test_api_keys_tab_renders_new_cards(admin_client):  # admin_client: existing fixture
    r = admin_client.get("/v2/partials/settings/api-keys")
    assert r.status_code == 200
    for name in ("EXPLORIUM_API_KEY", "APOLLO_API_KEY", "HUNTER_API_KEY"):
        assert name in r.text
```
(If no `admin_client` fixture exists, reuse the auth fixture used by other `htmx_views` settings tests — check `tests/conftest.py`.)
- [ ] **Step 2: Run — expect FAIL.**
- [ ] **Step 3a: Add context** in `htmx_views.py` after `:10425` (the `clay_api_key` line):
```python
            "explorium_api_key": _field("explorium_enrichment", "EXPLORIUM_API_KEY"),
            "apollo_api_key": _field("apollo_enrichment", "APOLLO_API_KEY"),
            "hunter_api_key": _field("hunter_enrichment", "HUNTER_API_KEY"),
```
- [ ] **Step 3b: Add three cards** in `api_keys.html` after the Lusha card (`:58`), copying the Lusha card markup verbatim with these substitutions per card (title/desc/name/id/source):
  - Explorium → title "Explorium", desc "Firmographics, intent & contact data"; `name="EXPLORIUM_API_KEY" id="explorium_api_key"`; `hx-put="/api/sources/explorium_enrichment/credentials"`; context var `explorium_api_key`; status div `#explorium-status`.
  - Apollo → title "Apollo", desc "Company + contact enrichment (free tier 10k/mo)"; `APOLLO_API_KEY`/`apollo_api_key`; `/api/sources/apollo_enrichment/credentials`.
  - Hunter → title "Hunter", desc "Email discovery by domain"; `HUNTER_API_KEY`/`hunter_api_key`; `/api/sources/hunter_enrichment/credentials`.
  Each card is the exact 50-line Lusha block (`api_keys.html:10-58`) with those five tokens swapped — include the show/hide toggle and the `hx-on::before-request` value-packing exactly as Lusha does.
- [ ] **Step 4: Run — expect PASS.** Verify the saved key resolves: a follow-up assertion that `PUT /api/sources/explorium_enrichment/credentials` with `{"credentials":{"EXPLORIUM_API_KEY":"x"}}` returns 200.
- [ ] **Step 5: Commit**
```bash
git add app/templates/htmx/partials/settings/api_keys.html app/routers/htmx_views.py tests/test_settings_api_keys_cards.py
git commit -m "feat(settings): Explorium/Apollo/Hunter API-key cards"
```

---

### Task 12: Docs, full suite, review

**Files:**
- Modify: `docs/APP_MAP_INTERACTIONS.md` (enrichment flow + the firmo tier ladder + Clay-via-MCP), `docs/APP_MAP_DATABASE.md` (new columns)

- [ ] **Step 1: Update both APP_MAP docs** to describe: the new `enrichment_router` → `firmo_tiers` blend, the per-field authority table, Clay-via-MCP (webhook removed), and the `companies`/`vendor_cards` provenance + firmographic columns.
- [ ] **Step 2: Pre-commit + full suite**
```bash
pre-commit run --all-files
TESTING=1 PYTHONPATH=$PWD /root/availai/.venv/bin/python -m pytest tests/ -q
```
Expected: pre-commit clean; suite green (re-run twice if docformatter mutates).
- [ ] **Step 3: `/qa`** then the PR-review fleet (`.claude/workflows/pr-review-fleet.js`); fix ALL findings (no deferrals, per CLAUDE.md).
- [ ] **Step 4: Commit docs**
```bash
git add docs/APP_MAP_INTERACTIONS.md docs/APP_MAP_DATABASE.md
git commit -m "docs(enrichment): map blending layer + provenance columns"
```
- [ ] **Step 5: Enablement (deploy step, not code).** After merge to `main`: set `EXPLORIUM_ENRICHMENT_ENABLED=true`, `CLAY_ENRICHMENT_ENABLED=true`, `APOLLO_ENRICHMENT_ENABLED=true`, `HUNTER_ENRICHMENT_ENABLED=true` (+ keys via Settings UI), `./deploy.sh` from `main` (migration-bearing), live-verify Clay (MCP) + Explorium (user key) on a real domain; confirm fields land + `enrichment_provenance` populated.

---

## Self-Review

**Spec coverage:** §4 Explorium → Task 4/9; §5 Clay MCP → Task 0/5/10; §6.1 ladder → Task 1/2; §6.2 router → Task 8/9; §6.3 provenance apply → Task 6/7; §6.4 config → Task 3; §6.5 UI → Task 11; §7 migration → Task 6; §9 tests → every task; §10 rollout → Task 12. SAM.gov (tier table member) → Task 8 adapter. All covered.

**Placeholder scan:** No "TBD"/"handle errors"-style gaps; error handling is concrete (quota raise + degrade). Two implementer notes (Task 8 `gather_contacts` escalation, Task 2 invariant) are explicit fixes, not placeholders.

**Type consistency:** `blend_company`/`blend_contacts`, `gather_company`/`gather_contacts`, `enrich_company`/`search_contacts` (Explorium), `enrich_company`/`find_contacts` (Clay), `_apply_enrichment` + `_provenance` shape `{source,tier,confidence}` are used consistently across Tasks 1/4/5/7/8/9. `enrichment_provenance` column name matches model + apply.
