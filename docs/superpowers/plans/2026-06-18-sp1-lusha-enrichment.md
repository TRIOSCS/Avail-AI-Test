# SP1 — Lusha in the Enrichment Chain (Implementation Plan)

**For agentic workers — REQUIRED SUB-SKILL: `superpowers:subagent-driven-development`.**
Execute each task in its own subagent; one task = one independently testable, committable unit.
Run the full pipeline (TDD → execute → simplify → review → verify) on every task.

## Goal

Make the prospecting "Enrich" action pull **real** procurement contacts and firmographics
(not just SAM.gov + Google News) by adding **Lusha** into the two existing shared enrichment
functions (`enrich_entity`, `find_suggested_contacts`) and wiring the prospect adapter
(`run_enrichment_job`) to consume them — moving a prospect's fit score, readiness tier, and
buyer-ready rank. **No provider-router module** (rejected as speculative generality). No
schema change / no Alembic migration. Graceful degradation: Lusha off / no key → the chain
behaves exactly as today.

## Architecture

The existing `enrich_entity` *already is* an ordered, fill-only provider chain
(Explorium → Apollo → AI, inner `_merge`); `find_suggested_contacts` *already* runs providers
concurrently with dedup + relevance filtering. SP1 inserts Lusha into both, guarded by a
minimal credit "circuit" (cooldown) so a quota/rate-limit error isn't re-hit on every Enrich
click. The prospect adapter calls these two functions and maps their normalized output onto
`ProspectAccount` columns (fill-only) + JSONB, then recomputes fit + readiness.

```
enrich_entity(domain, name):
  Explorium → [Lusha if enabled & circuit closed & gaps] → [Apollo if key & gaps] → [AI if gaps]
find_suggested_contacts(domain, name, title_filter, limit):
  [Lusha first if enabled & circuit closed] → early-stop if ≥limit verified
  else existing asyncio.gather(Explorium, AI) → extend → dedup → relevance filter
run_enrichment_job(prospect_id):
  run_free_enrichment → [24h gate → enrich_entity + find_suggested_contacts → map fill-only]
  → warm-intro → recompute fit + readiness → enrich_status='done'
```

## Tech Stack

FastAPI + SQLAlchemy 2.0 (sync) + PostgreSQL 16 + Redis (intel_cache). Outbound HTTP via the
shared `app/http_client.py` `http` singleton. Loguru, Ruff, mypy. No React, no new UI elements.

## Global Constraints

(verbatim from `docs/superpowers/specs/2026-06-18-prospect-real-enrichment-design.md`)

- Stack: FastAPI + SQLAlchemy 2.0 (sync). No React. **No new UI elements** (only status copy).
- Outbound HTTP via the shared `app/http_client.py` `http` singleton (the new Lusha connector
  must use it — *not* a per-call `httpx.AsyncClient` like the legacy Apollo connector).
- Loguru, never `print()`. Ruff + mypy clean. Tests with every task. `db.get`, not
  `db.query(...).get`. New files get a header comment.
- **No schema change / no Alembic migration** — all new data lands in existing JSONB
  (`enrichment_data`, `readiness_signals`) + existing scalar columns (`industry`,
  `employee_count_range`, `naics_code`, `hq_location`, `revenue_range`, `fit_score`,
  `readiness_score`, `contacts_preview`).
- Firmographic writes are **fill-only** — never clobber an existing value (especially
  SAM.gov's `naics_code`); reuse the existing fill-only `_merge` pattern.
- Fire-and-forget safety: `run_enrichment_job` never raises; unexpected failure →
  `enrichment_data['enrich_status']='error'` (existing).
- **Graceful degradation:** Lusha disabled / no key → the chain behaves exactly as today.

**Canonical test command** (per task; substitute `<file>`):

```bash
TESTING=1 PYTHONPATH=/root/availai pytest <file> -q -p no:cacheprovider -o addopts=""
```

Project test idioms: mock at the **source module** (e.g. patch
`app.enrichment_service.lusha`, not the import site); use `AsyncMock` for async functions;
`monkeypatch.setattr(settings, "...", ...)` for config; `db_session` fixture (autouse,
in-memory SQLite, row-deleted after each test). `os.environ["TESTING"]="1"` is set by
conftest before app imports.

## File Structure

| File | New/Modify | Responsibility |
|---|---|---|
| `app/services/enrichment_credit_guard.py` | NEW | `ProviderQuotaError` + intel-cache-backed `circuit_open`/`trip_circuit` cooldown |
| `app/connectors/lusha.py` | NEW | `enrich_company` + `search_contacts` via shared `http`; 402/429 → `ProviderQuotaError` |
| `app/config.py` | Modify | 3 settings: `lusha_enrichment_enabled`, `lusha_cooldown_minutes`, `prospect_enrich_contacts_per_account` |
| `.env.example` | Modify | Document `LUSHA_API_KEY=` env fallback |
| `app/enrichment_service.py` | Modify | `_lusha_enabled()` helper; Lusha phase + gap-gate Apollo in `enrich_entity`; Lusha-first in `find_suggested_contacts` + `limit` kwarg |
| `app/services/prospect_free_enrichment.py` | Modify | Paid step in `run_enrichment_job` + `infer_seniority`/`_apply_company_to_prospect`/`_apply_contacts_to_prospect` |
| `app/templates/htmx/partials/prospecting/enrich_status.html` | Modify | Running copy → "Enriching… contacts + firmographics" |
| `docs/APP_MAP_INTERACTIONS.md` | Modify | Prospects/Enrichment row notes Lusha + real contacts/firmographics |
| `tests/test_enrichment_credit_guard.py` | NEW | Credit-guard unit tests |
| `tests/test_lusha_connector.py` | NEW | Lusha connector unit tests |
| `tests/test_enrich_entity_lusha.py` | NEW | `enrich_entity` + `find_suggested_contacts` Lusha behavior |
| `tests/test_prospect_real_enrichment.py` | NEW | Prospect adapter mapping + recompute tests |

---

## Task 1 — Config (3 settings + `.env.example`)

**Files**
- Modify `app/config.py` — add after line 282 (`apollo_api_key: str = ""`, the `# --- Apollo Enrichment ---` block).
- Modify `.env.example` — after the `EXPLORIUM_API_KEY=` line (line 38).

**Interfaces**
- Produces: `settings.lusha_enrichment_enabled: bool` (default `False`),
  `settings.lusha_cooldown_minutes: int` (default `15`),
  `settings.prospect_enrich_contacts_per_account: int` (default `5`).
- **No `lusha_api_key` field** — the key flows through
  `get_credential_cached("lusha_enrichment","LUSHA_API_KEY")` (DB-managed via Sources UI,
  env fallback), matching Explorium + the existing Lusha test connector.

**Steps**
- [ ] Write failing test `tests/test_config_lusha_settings.py`:

```python
"""SP1 config: Lusha enrichment settings exist with documented defaults.

Verifies the three new Settings fields and that no lusha_api_key field was added
(the key flows through get_credential_cached, matching Explorium).
"""

import os

os.environ["TESTING"] = "1"

from app.config import settings


def test_lusha_settings_defaults():
    assert settings.lusha_enrichment_enabled is False
    assert settings.lusha_cooldown_minutes == 15
    assert settings.prospect_enrich_contacts_per_account == 5


def test_no_lusha_api_key_field():
    assert not hasattr(settings, "lusha_api_key")
```

- [ ] Run (expect **FAIL** — `AttributeError`):
  `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_config_lusha_settings.py -q -p no:cacheprovider -o addopts=""`
- [ ] Add to `app/config.py` immediately after `apollo_api_key: str = ""` (line 282):

```python
    # --- Lusha Enrichment (key via get_credential_cached, NOT a Settings field) ---
    lusha_enrichment_enabled: bool = False  # feature gate; off → chain == today
    lusha_cooldown_minutes: int = 15  # quota/rate-limit (402/429) circuit cooldown
    prospect_enrich_contacts_per_account: int = 5  # cap for paid contact pulls
```

- [ ] Add to `.env.example` after `EXPLORIUM_API_KEY=`:

```
# Lusha contact + firmographic enrichment (DB-managed via Sources UI; this is the env fallback).
# Feature-gated by LUSHA_ENRICHMENT_ENABLED=true (default false).
LUSHA_API_KEY=
```

- [ ] Run (expect **PASS**): same command.
- [ ] `ruff check app/config.py && ruff format --check app/config.py`
- [ ] Commit: `feat(config): add Lusha enrichment settings (SP1 T1)`

---

## Task 2 — Credit guard (`enrichment_credit_guard.py`)

**Files**
- NEW `app/services/enrichment_credit_guard.py` (~15 lines + header).

**Interfaces**
- Produces:
  - `class ProviderQuotaError(Exception)` — raised by connectors on 402/429.
  - `circuit_open(provider: str) -> bool` — `get_cached("enrich:circuit:{provider}") is not None`.
  - `trip_circuit(provider: str, minutes: int) -> None` —
    `set_cached("enrich:circuit:{provider}", {"tripped": 1}, ttl_days=minutes / 1440)`.
- Consumes: `app.cache.intel_cache.get_cached`, `set_cached` (ttl in DAYS; `minutes/1440`
  converts minutes→days — `_ttl_seconds` already floors to whole seconds for Redis).

**Steps**
- [ ] Write failing test `tests/test_enrichment_credit_guard.py`:

```python
"""SP1 credit guard: circuit cooldown around paid-provider quota/rate-limit errors.

trip_circuit writes an intel-cache marker; circuit_open reads it. TTL is minutes→days.
Both intel-cache calls are patched at the credit-guard module (the import site here).
"""

import os

os.environ["TESTING"] = "1"

from app.services import enrichment_credit_guard as guard


def test_provider_quota_error_is_exception():
    assert issubclass(guard.ProviderQuotaError, Exception)


def test_circuit_open_false_when_no_marker(monkeypatch):
    monkeypatch.setattr(guard, "get_cached", lambda key: None)
    assert guard.circuit_open("lusha") is False


def test_circuit_open_true_when_marker_present(monkeypatch):
    monkeypatch.setattr(guard, "get_cached", lambda key: {"tripped": 1})
    assert guard.circuit_open("lusha") is True


def test_trip_circuit_writes_marker_with_minutes_ttl(monkeypatch):
    captured = {}

    def _fake_set(key, data, ttl_days):
        captured["key"] = key
        captured["data"] = data
        captured["ttl_days"] = ttl_days

    monkeypatch.setattr(guard, "set_cached", _fake_set)
    guard.trip_circuit("lusha", 15)
    assert captured["key"] == "enrich:circuit:lusha"
    assert captured["data"] == {"tripped": 1}
    assert captured["ttl_days"] == 15 / 1440
```

- [ ] Run (expect **FAIL** — module does not exist):
  `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_enrichment_credit_guard.py -q -p no:cacheprovider -o addopts=""`
- [ ] Create `app/services/enrichment_credit_guard.py`:

```python
"""Enrichment credit guard — quota error + per-provider cooldown ("circuit").

A paid provider (Lusha today) that returns 402/429 raises ``ProviderQuotaError``; the
caller trips a short cooldown so the same quota/rate-limit isn't re-hit on every Enrich
click. The cooldown marker lives in the shared intel cache (Redis → PG fallback), so it's
process-wide — graceful fall-through alone does NOT stop repeat spend across clicks.

Called by: app/enrichment_service.py (enrich_entity, find_suggested_contacts).
Depends on: app/cache/intel_cache.py (get_cached, set_cached; TTL in days).
"""

from app.cache.intel_cache import get_cached, set_cached


class ProviderQuotaError(Exception):
    """A paid provider returned a quota/rate-limit status (402/429)."""


def _circuit_key(provider: str) -> str:
    return f"enrich:circuit:{provider}"


def circuit_open(provider: str) -> bool:
    """True while *provider* is in cooldown (a trip marker exists and hasn't expired)."""
    return get_cached(_circuit_key(provider)) is not None


def trip_circuit(provider: str, minutes: int) -> None:
    """Open *provider*'s cooldown for *minutes* (intel-cache TTL is in days)."""
    set_cached(_circuit_key(provider), {"tripped": 1}, ttl_days=minutes / 1440)
```

- [ ] Run (expect **PASS**): same command.
- [ ] `ruff check app/services/enrichment_credit_guard.py && mypy app/services/enrichment_credit_guard.py`
- [ ] Commit: `feat(enrichment): add credit guard (ProviderQuotaError + circuit) (SP1 T2)`

---

## Task 3 — Lusha connector (`connectors/lusha.py`)

**Files**
- NEW `app/connectors/lusha.py` (mirror `app/connectors/apollo.py` structure, but use shared `http`).

**Interfaces**
- Consumes: `app.http_client.http` (shared singleton), `app.services.enrichment_credit_guard.ProviderQuotaError`.
- Produces:
  - `async def enrich_company(domain: str, api_key: str) -> dict | None`
    → `{"source":"lusha","legal_name","domain","industry","employee_size","hq_city",
       "hq_state","hq_country","linkedin_url"} | None`
  - `async def search_contacts(domain: str, api_key: str, limit: int) -> list[dict]`
    → `[{"source":"lusha","full_name","email","phone","title","verified"}]`
- Auth header per Lusha v2 (same as `_LushaTestConnector`): `{"api_key": api_key, "Content-Type": "application/json"}`,
  base `https://api.lusha.com/v2`. On HTTP **402/429** raise `ProviderQuotaError`; on other
  `httpx.HTTPError`/`KeyError`/`ValueError` → log warn + return `None`/`[]`.

**Steps**
- [ ] Write failing test `tests/test_lusha_connector.py`:

```python
"""SP1 Lusha connector: field mapping, empty handling, quota → ProviderQuotaError.

http is patched at the connector module (app.connectors.lusha.http) with an AsyncMock
returning a fake httpx.Response-like object.
"""

import os

os.environ["TESTING"] = "1"

from unittest.mock import AsyncMock

import httpx
import pytest

from app.connectors import lusha
from app.services.enrichment_credit_guard import ProviderQuotaError


class _Resp:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = str(self._payload)

    def json(self):
        return self._payload


async def test_enrich_company_maps_fields(monkeypatch):
    payload = {
        "data": {
            "name": "Acme Aerospace Inc",
            "industry": "Aerospace & Defense",
            "employees": "501-1000",
            "location": {"city": "Dallas", "state": "TX", "country": "United States"},
            "social": {"linkedin": "linkedin.com/company/acme"},
        }
    }
    monkeypatch.setattr(lusha, "http", AsyncMock())
    lusha.http.get.return_value = _Resp(200, payload)
    out = await lusha.enrich_company("acme.com", "key")
    assert out["source"] == "lusha"
    assert out["legal_name"] == "Acme Aerospace Inc"
    assert out["domain"] == "acme.com"
    assert out["industry"] == "Aerospace & Defense"
    assert out["employee_size"] == "501-1000"
    assert out["hq_city"] == "Dallas"
    assert out["hq_state"] == "TX"
    assert out["hq_country"] == "United States"
    assert out["linkedin_url"] == "linkedin.com/company/acme"


async def test_enrich_company_empty_returns_none(monkeypatch):
    monkeypatch.setattr(lusha, "http", AsyncMock())
    lusha.http.get.return_value = _Resp(200, {"data": {}})
    assert await lusha.enrich_company("acme.com", "key") is None


@pytest.mark.parametrize("code", [402, 429])
async def test_enrich_company_quota_raises(monkeypatch, code):
    monkeypatch.setattr(lusha, "http", AsyncMock())
    lusha.http.get.return_value = _Resp(code, {})
    with pytest.raises(ProviderQuotaError):
        await lusha.enrich_company("acme.com", "key")


async def test_enrich_company_http_error_returns_none(monkeypatch):
    monkeypatch.setattr(lusha, "http", AsyncMock())
    lusha.http.get.side_effect = httpx.HTTPError("boom")
    assert await lusha.enrich_company("acme.com", "key") is None


async def test_search_contacts_maps_and_filters(monkeypatch):
    payload = {
        "contacts": [
            {
                "fullName": "Jane Buyer",
                "emailAddresses": [{"email": "jane@acme.com"}],
                "phoneNumbers": [{"number": "+15551234567"}],
                "jobTitle": "Director of Procurement",
                "isEmailVerified": True,
            },
            {"fullName": None},  # dropped (no name)
        ]
    }
    monkeypatch.setattr(lusha, "http", AsyncMock())
    lusha.http.post.return_value = _Resp(200, payload)
    out = await lusha.search_contacts("acme.com", "key", limit=5)
    assert len(out) == 1
    c = out[0]
    assert c["source"] == "lusha"
    assert c["full_name"] == "Jane Buyer"
    assert c["email"] == "jane@acme.com"
    assert c["phone"] == "+15551234567"
    assert c["title"] == "Director of Procurement"
    assert c["verified"] is True


@pytest.mark.parametrize("code", [402, 429])
async def test_search_contacts_quota_raises(monkeypatch, code):
    monkeypatch.setattr(lusha, "http", AsyncMock())
    lusha.http.post.return_value = _Resp(code, {})
    with pytest.raises(ProviderQuotaError):
        await lusha.search_contacts("acme.com", "key", limit=5)


async def test_search_contacts_http_error_returns_empty(monkeypatch):
    monkeypatch.setattr(lusha, "http", AsyncMock())
    lusha.http.post.side_effect = httpx.HTTPError("boom")
    assert await lusha.search_contacts("acme.com", "key", limit=5) == []
```

- [ ] Run (expect **FAIL** — module does not exist):
  `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_lusha_connector.py -q -p no:cacheprovider -o addopts=""`
- [ ] Create `app/connectors/lusha.py`:

```python
"""Lusha API connector for company + contact enrichment (Lusha API v2).

Mirrors apollo.py but uses the shared app/http_client.py `http` singleton (connection
pooling) instead of a per-call httpx.AsyncClient. On HTTP 402/429 (quota/rate-limit)
raises ProviderQuotaError so the caller trips the cooldown circuit; other transport/parse
errors degrade to None / [].

Called by: app/enrichment_service.py (enrich_entity Phase 1a-Lusha, find_suggested_contacts).
Depends on: app/http_client.py (http), app/services/enrichment_credit_guard.py
            (ProviderQuotaError).
"""

import httpx
from loguru import logger

from app.http_client import http
from app.services.enrichment_credit_guard import ProviderQuotaError

LUSHA_BASE = "https://api.lusha.com/v2"
_QUOTA_STATUSES = (402, 429)


def _headers(api_key: str) -> dict:
    """Build Lusha v2 request headers (same scheme as the Lusha test connector)."""
    return {"api_key": api_key, "Content-Type": "application/json"}


def _parse_company(data: dict) -> dict | None:
    """Map a Lusha company payload to the shared firmographic shape, or None if empty."""
    org = data.get("data") or data.get("company") or {}
    if not org:
        return None
    location = org.get("location") or {}
    social = org.get("social") or {}
    out = {
        "source": "lusha",
        "legal_name": org.get("name") or org.get("legalName"),
        "domain": org.get("domain") or org.get("website"),
        "industry": org.get("industry"),
        "employee_size": org.get("employees") or org.get("employeeRange") or org.get("size"),
        "hq_city": location.get("city"),
        "hq_state": location.get("state"),
        "hq_country": location.get("country"),
        "linkedin_url": social.get("linkedin") or org.get("linkedinUrl"),
    }
    # Empty unless at least one informative field is present.
    if not any(v for k, v in out.items() if k not in ("source", "domain")):
        return None
    return out


def _parse_contacts(data: dict) -> list[dict]:
    """Map Lusha contact payloads to the shared contact shape (verified flag preserved)."""
    raw = data.get("contacts") or data.get("data") or []
    contacts = []
    for person in raw:
        name = person.get("fullName") or person.get("full_name") or person.get("name")
        if not name:
            continue
        emails = person.get("emailAddresses") or person.get("emails") or []
        phones = person.get("phoneNumbers") or person.get("phones") or []
        email = (emails[0].get("email") if emails and isinstance(emails[0], dict) else None) or person.get("email")
        phone = (phones[0].get("number") if phones and isinstance(phones[0], dict) else None) or person.get("phone")
        contacts.append(
            {
                "source": "lusha",
                "full_name": name,
                "email": email,
                "phone": phone,
                "title": person.get("jobTitle") or person.get("title"),
                "verified": bool(person.get("isEmailVerified") or person.get("verified")),
            }
        )
    return contacts


async def enrich_company(domain: str, api_key: str) -> dict | None:
    """Look up a company on Lusha by domain. 402/429 → ProviderQuotaError; else None on error."""
    try:
        resp = await http.get(
            f"{LUSHA_BASE}/company",
            headers=_headers(api_key),
            params={"domain": domain},
            timeout=15,
        )
        if resp.status_code in _QUOTA_STATUSES:
            raise ProviderQuotaError(f"Lusha company quota/rate-limit: {resp.status_code}")
        if resp.status_code != 200:
            logger.warning("Lusha company lookup failed: {}", resp.status_code)
            return None
        return _parse_company(resp.json())
    except (httpx.HTTPError, KeyError, ValueError) as e:
        logger.warning("Lusha company lookup error: {}", e)
        return None


async def search_contacts(domain: str, api_key: str, limit: int) -> list[dict]:
    """Search contacts at a company on Lusha. 402/429 → ProviderQuotaError; else [] on error."""
    try:
        resp = await http.post(
            f"{LUSHA_BASE}/contacts",
            headers=_headers(api_key),
            json={"domain": domain, "limit": limit},
            timeout=20,
        )
        if resp.status_code in _QUOTA_STATUSES:
            raise ProviderQuotaError(f"Lusha contacts quota/rate-limit: {resp.status_code}")
        if resp.status_code != 200:
            logger.warning("Lusha contacts search failed: {}", resp.status_code)
            return []
        return _parse_contacts(resp.json())
    except (httpx.HTTPError, KeyError, ValueError) as e:
        logger.warning("Lusha contacts search error: {}", e)
        return []
```

- [ ] Run (expect **PASS**): same command.
- [ ] `ruff check app/connectors/lusha.py && mypy app/connectors/lusha.py`
- [ ] Commit: `feat(connectors): add Lusha connector via shared http (SP1 T3)`

---

## Task 4 — `enrich_entity` + `find_suggested_contacts` (CRM regression stays green)

**Files**
- Modify `app/enrichment_service.py`:
  - add module-level `_lusha_enabled()` helper (near the top of the "Unified Enrichment" section, before `enrich_entity` at line 451).
  - `enrich_entity` (lines 506–519): insert Lusha phase after Explorium `_merge` (line 507),
    before Apollo (line 512); **gap-gate Apollo** by adding `and any(not result.get(f) for f in _enrichable)` to its `if`.
  - `find_suggested_contacts` (signature line 531; body 539–551): add `limit: int = 10`;
    call Lusha first; early-stop or fall through to the existing gather; **dedup + relevance filter unchanged**.

**Interfaces**
- Consumes: `app.connectors.lusha` (`enrich_company`, `search_contacts`),
  `app.services.enrichment_credit_guard` (`circuit_open`, `trip_circuit`, `ProviderQuotaError`),
  `app.services.credential_service.get_credential_cached`, `app.config.settings`.
- Produces (unchanged shapes): `enrich_entity(domain, name="") -> dict`;
  `find_suggested_contacts(domain, name="", title_filter="", limit=10) -> list[dict]`.
- `_lusha_enabled() -> bool` = `settings.lusha_enrichment_enabled and bool(get_credential_cached("lusha_enrichment","LUSHA_API_KEY"))`.

**Steps**
- [ ] Write failing test `tests/test_enrich_entity_lusha.py`:

```python
"""SP1: Lusha in enrich_entity (gap-fill + gap-gated Apollo) and find_suggested_contacts.

All providers are patched at the enrichment_service module. Lusha gated off by default →
CRM regression behavior is unchanged (covered by existing tests); these tests force it on.
"""

import os

os.environ["TESTING"] = "1"

from unittest.mock import AsyncMock

import app.enrichment_service as es
from app.config import settings
from app.services.enrichment_credit_guard import ProviderQuotaError


def _enable_lusha(monkeypatch):
    monkeypatch.setattr(settings, "lusha_enrichment_enabled", True)
    monkeypatch.setattr(es, "get_credential_cached", lambda src, var: "lusha-key")
    monkeypatch.setattr(es, "circuit_open", lambda provider: False)


async def test_lusha_fills_gaps_and_gap_gates_apollo(monkeypatch):
    _enable_lusha(monkeypatch)
    monkeypatch.setattr("app.cache.intel_cache.get_cached", lambda key: None)
    monkeypatch.setattr("app.cache.intel_cache.set_cached", lambda *a, **k: None)
    monkeypatch.setattr(es, "_explorium_find_company", AsyncMock(return_value={"source": "explorium", "legal_name": "Acme"}))
    full = {
        "source": "lusha", "legal_name": "AcmeL", "industry": "Aero", "employee_size": "100-200",
        "hq_city": "Dallas", "hq_state": "TX", "hq_country": "United States",
        "website": "acme.com", "linkedin_url": "li/acme",
    }
    monkeypatch.setattr(es.lusha, "enrich_company", AsyncMock(return_value=full))
    apollo_mock = AsyncMock(return_value={"source": "apollo", "legal_name": "AcmeA"})
    monkeypatch.setattr("app.connectors.apollo.search_company", apollo_mock)
    monkeypatch.setattr(settings, "apollo_api_key", "apollo-key")
    monkeypatch.setattr(es, "_ai_find_company", AsyncMock(return_value=None))

    out = await es.enrich_entity("acme.com", "Acme")
    assert out["industry"] == "Aero"  # filled by Lusha (not clobbered)
    assert out["legal_name"] == "Acme"  # Explorium won (fill-only)
    apollo_mock.assert_not_called()  # no gaps remain → Apollo skipped (gap-gate)


async def test_lusha_quota_trips_circuit(monkeypatch):
    _enable_lusha(monkeypatch)
    monkeypatch.setattr("app.cache.intel_cache.get_cached", lambda key: None)
    monkeypatch.setattr("app.cache.intel_cache.set_cached", lambda *a, **k: None)
    monkeypatch.setattr(es, "_explorium_find_company", AsyncMock(return_value=None))
    monkeypatch.setattr(es.lusha, "enrich_company", AsyncMock(side_effect=ProviderQuotaError("402")))
    tripped = {}
    monkeypatch.setattr(es, "trip_circuit", lambda p, m: tripped.update(provider=p, minutes=m))
    monkeypatch.setattr(settings, "apollo_api_key", "")
    monkeypatch.setattr(es, "_ai_find_company", AsyncMock(return_value=None))

    await es.enrich_entity("acme.com", "Acme")
    assert tripped["provider"] == "lusha"
    assert tripped["minutes"] == settings.lusha_cooldown_minutes


async def test_lusha_disabled_chain_unchanged(monkeypatch):
    monkeypatch.setattr(settings, "lusha_enrichment_enabled", False)
    monkeypatch.setattr("app.cache.intel_cache.get_cached", lambda key: None)
    monkeypatch.setattr("app.cache.intel_cache.set_cached", lambda *a, **k: None)
    monkeypatch.setattr(es, "_explorium_find_company", AsyncMock(return_value={"source": "explorium", "legal_name": "Acme"}))
    lusha_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(es.lusha, "enrich_company", lusha_mock)
    monkeypatch.setattr(settings, "apollo_api_key", "")
    monkeypatch.setattr(es, "_ai_find_company", AsyncMock(return_value=None))

    await es.enrich_entity("acme.com", "Acme")
    lusha_mock.assert_not_called()  # disabled → never called


async def test_find_contacts_lusha_first_early_stop(monkeypatch):
    _enable_lusha(monkeypatch)
    verified = [{"source": "lusha", "full_name": f"P{i}", "title": "Buyer", "email": f"p{i}@a.com", "verified": True} for i in range(3)]
    monkeypatch.setattr(es.lusha, "search_contacts", AsyncMock(return_value=verified))
    expl = AsyncMock(return_value=[])
    ai = AsyncMock(return_value=[])
    monkeypatch.setattr(es, "_explorium_find_contacts", expl)
    monkeypatch.setattr(es, "_ai_find_contacts", ai)

    out = await es.find_suggested_contacts("acme.com", "Acme", limit=3)
    assert len(out) == 3
    expl.assert_not_called()  # ≥limit verified → existing gather (providers) skipped
    ai.assert_not_called()


async def test_find_contacts_falls_through_when_lusha_thin(monkeypatch):
    _enable_lusha(monkeypatch)
    monkeypatch.setattr(es.lusha, "search_contacts", AsyncMock(return_value=[]))  # nothing from Lusha
    monkeypatch.setattr(es, "_explorium_find_contacts", AsyncMock(return_value=[{"source": "explorium", "full_name": "Jane", "title": "Procurement", "email": "jane@a.com"}]))
    monkeypatch.setattr(es, "_ai_find_contacts", AsyncMock(return_value=[]))

    out = await es.find_suggested_contacts("acme.com", "Acme", limit=5)
    assert any(c["full_name"] == "Jane" for c in out)  # fallback merged + relevance-filtered
```

- [ ] Run (expect **FAIL** — `_lusha_enabled`/`lusha`/`circuit_open` not yet imported):
  `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_enrich_entity_lusha.py -q -p no:cacheprovider -o addopts=""`
- [ ] Add imports to `app/enrichment_service.py` (after line 19, the `claude_client` import):

```python
from .config import settings
from .connectors import lusha
from .services.enrichment_credit_guard import ProviderQuotaError, circuit_open, trip_circuit
```

  **Leave** `enrich_entity`'s function-local `from .cache.intel_cache import get_cached,
  set_cached` (line 460) AS-IS — do NOT hoist it (avoids any import cycle and matches the
  CLAUDE.md "mock lazy imports at the source module" rule). Tests control caching by patching
  the **source** (`monkeypatch.setattr("app.cache.intel_cache.get_cached", ...)`), which the
  function-local import re-fetches at call time. If a module-level `from .config import
  settings` already exists, keep just one; delete the now-dead function-local `from .config
  import settings as _settings` (line 510) and use the module-level `settings`.

- [ ] Add the helper immediately before `enrich_entity` (line 451):

```python
def _lusha_enabled() -> bool:
    """True when Lusha is feature-gated on AND a key is resolvable (DB or env)."""
    return settings.lusha_enrichment_enabled and bool(get_credential_cached("lusha_enrichment", "LUSHA_API_KEY"))
```

- [ ] Replace the Phase-1/1b/2 block in `enrich_entity` (lines 506–519) with:

```python
    def _gaps_remain() -> bool:
        return any(not result.get(f) for f in _enrichable)

    # ── Phase 1: Explorium ──
    _merge(await _explorium_find_company(domain, name), "explorium")

    # ── Phase 1a: Lusha (verified contacts/firmographics) — gap-gated, circuit-guarded ──
    if _lusha_enabled() and not circuit_open("lusha") and _gaps_remain():
        try:
            _merge(await lusha.enrich_company(domain, get_credential_cached("lusha_enrichment", "LUSHA_API_KEY")), "lusha")
        except ProviderQuotaError:
            logger.warning("Lusha quota/rate-limit on company {} — tripping circuit", domain)
            trip_circuit("lusha", settings.lusha_cooldown_minutes)

    # ── Phase 1b: Apollo enrichment (fills gaps; gap-gated → spares credits) ──
    if settings.apollo_api_key and _gaps_remain():
        from .connectors.apollo import search_company as apollo_search

        _merge(await apollo_search(domain, settings.apollo_api_key), "apollo")

    # ── Phase 2: AI fills remaining gaps (conditional) ──
    if _gaps_remain():
        _merge(await _ai_find_company(domain, name), "ai")
```

  (Delete the now-dead local `from .config import settings as _settings` at line 510 and its
  `_settings` references — use the module-level `settings`.)

- [ ] Replace `find_suggested_contacts` (lines 531–551, signature + gather + collect) so the
  Lusha-first early-stop precedes the existing gather; **leave the dedup + `_RELEVANT_KEYWORDS`
  filter (lines 553–598) untouched**:

```python
async def find_suggested_contacts(domain: str, name: str = "", title_filter: str = "", limit: int = 10) -> list[dict]:
    """Find suggested contacts at a company from all configured providers.

    Lusha (verified) runs first; if it returns >= limit verified contacts the existing
    concurrent Explorium+AI gather is skipped, else they run and results are merged. Returns
    a deduplicated, relevance-filtered list. Each contact has: full_name, title, email, phone,
    linkedin_url, location, source (and verified for Lusha rows).
    """
    all_contacts: list[dict] = []

    # ── Lusha first (verified source) — circuit-guarded ──
    if _lusha_enabled() and not circuit_open("lusha"):
        try:
            all_contacts = await lusha.search_contacts(domain, get_credential_cached("lusha_enrichment", "LUSHA_API_KEY"), limit)
        except ProviderQuotaError:
            logger.warning("Lusha quota/rate-limit on contacts {} — tripping circuit", domain)
            trip_circuit("lusha", settings.lusha_cooldown_minutes)

    # Fall through to the existing concurrent providers unless Lusha already satisfied the need.
    if not (len(all_contacts) >= limit and any(c.get("verified") for c in all_contacts)):
        results = await asyncio.gather(
            _explorium_find_contacts(domain, title_filter),
            _ai_find_contacts(domain, name, title_filter),
            return_exceptions=True,
        )
        for r in results:
            if isinstance(r, Exception):
                logger.warning("Contact provider failed: {}", r)
                continue
            all_contacts.extend(r)
```

  (The next existing line — `# Deduplicate by email or linkedin_url or full_name` — and
  everything below it stays exactly as-is. Delete the old `all_contacts = []` /
  `for r in results:` collection block that the replacement supersedes.)

- [ ] Run (expect **PASS**): same command.
- [ ] **CRM regression** — run the existing enrichment tests to prove nothing broke:
  `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_enrichment_service.py tests/test_enrich_readiness_recompute.py -q -p no:cacheprovider -o addopts=""`
  (if `tests/test_enrichment_service.py` is absent, run `grep -rln "find_suggested_contacts\|enrich_entity" tests/` and run those files — all must stay green).
- [ ] `ruff check app/enrichment_service.py && mypy app/enrichment_service.py`
- [ ] Commit: `feat(enrichment): wire Lusha into enrich_entity + find_suggested_contacts (SP1 T4)`

---

## Task 5 — Prospect adapter (`run_enrichment_job` paid step)

**Files**
- Modify `app/services/prospect_free_enrichment.py`:
  - add module-level `infer_seniority`, `_apply_company_to_prospect`, `_apply_contacts_to_prospect`.
  - modify `run_enrichment_job` (lines 276–333): insert the 24h-gated paid step between
    `run_free_enrichment` (line 293) and warm-intro (line 300); recompute **fit** (new) +
    **readiness** (existing line 308); preserve fire-and-forget safety.

**Interfaces**
- Consumes: `app.enrichment_service.enrich_entity`, `find_suggested_contacts`;
  `app.services.prospect_scoring.calculate_fit_score`, `calculate_readiness_score`;
  `app.config.settings.prospect_enrich_contacts_per_account`;
  `sqlalchemy.orm.attributes.flag_modified` (already imported).
- Produces (fill-only) on `ProspectAccount`:
  - `_apply_company_to_prospect(prospect, company: dict | None) -> None` — fill-only field-name
    map: `industry→industry`, `employee_size→employee_count_range`,
    `hq_city`+`hq_state`→`hq_location` as `"City, ST"`, `naics→naics_code` **only if empty**
    (preserve SAM.gov), `revenue_range→revenue_range`.
  - `_apply_contacts_to_prospect(prospect, contacts: list[dict], limit: int) -> list[dict]` —
    map `full_name→name`, `infer_seniority(title)→seniority`,
    `verified=bool(c.get("verified"))` (default False), cap at `limit`, dedup by
    email/name; write `contacts_preview`; return the mapped list.
  - `infer_seniority(title: str | None) -> str` — `decision_maker` | `influencer` | `contributor`.

**Steps**
- [ ] Write failing test `tests/test_prospect_real_enrichment.py`:

```python
"""SP1 prospect adapter: real enrichment maps fill-only, infers seniority, recomputes scores.

enrich_entity / find_suggested_contacts are patched at the prospect_free_enrichment import
site (they're imported lazily inside run_enrichment_job, so patch the source module).
"""

import os

os.environ["TESTING"] = "1"

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from sqlalchemy.orm import Session

from app.config import settings
from app.models.prospect_account import ProspectAccount
from app.services.prospect_free_enrichment import (
    _apply_company_to_prospect,
    _apply_contacts_to_prospect,
    infer_seniority,
)


def _prospect(db: Session, **kw) -> ProspectAccount:
    p = ProspectAccount(
        name=f"P {uuid.uuid4().hex[:6]}",
        domain=f"p-{uuid.uuid4().hex[:6]}.com",
        status="suggested",
        discovery_source="clay",
        created_at=datetime.now(timezone.utc),
        **kw,
    )
    db.add(p)
    db.commit()
    return p


def test_infer_seniority():
    assert infer_seniority("VP of Supply Chain") == "decision_maker"
    assert infer_seniority("Director, Procurement") == "decision_maker"
    assert infer_seniority("Senior Buyer") == "influencer"
    assert infer_seniority("Procurement Specialist") == "influencer"
    assert infer_seniority("Warehouse Associate") == "contributor"
    assert infer_seniority(None) == "contributor"


def test_apply_company_fill_only_preserves_sam_naics(db_session):
    p = _prospect(db_session, naics_code="336412", industry=None)
    _apply_company_to_prospect(p, {
        "industry": "Aerospace", "employee_size": "501-1000", "naics": "111111",
        "hq_city": "Dallas", "hq_state": "TX", "revenue_range": "$50M-$100M",
    })
    assert p.naics_code == "336412"  # SAM.gov preserved (fill-only)
    assert p.industry == "Aerospace"  # was empty → filled
    assert p.employee_count_range == "501-1000"
    assert p.hq_location == "Dallas, TX"
    assert p.revenue_range == "$50M-$100M"


def test_apply_contacts_maps_and_counts(db_session):
    p = _prospect(db_session)
    mapped = _apply_contacts_to_prospect(p, [
        {"full_name": "Jane VP", "title": "VP Procurement", "email": "jane@a.com", "verified": True},
        {"full_name": "Joe Buyer", "title": "Buyer", "email": "joe@a.com"},  # verified default False
        {"full_name": "Jane VP", "title": "VP Procurement", "email": "jane@a.com", "verified": True},  # dup
    ], limit=5)
    assert len(mapped) == 2  # deduped
    assert mapped[0]["seniority"] == "decision_maker"
    assert mapped[1]["verified"] is False
    assert p.contacts_preview == mapped


async def test_run_enrichment_job_paid_step_recomputes(db_session, monkeypatch):
    from app.services import prospect_free_enrichment as pfe

    monkeypatch.setattr(settings, "prospect_enrich_contacts_per_account", 5)
    p = _prospect(db_session, fit_score=10, readiness_score=10, industry=None,
                  readiness_signals={"intent": {"strength": "strong"}, "events": [{"type": "funding"}],
                                     "hiring": {"type": "procurement"}})

    company = {"industry": "Aerospace & Defense", "naics": "336412", "employee_size": "501-1000",
               "hq_city": "Dallas", "hq_state": "TX", "revenue_range": "$100M+"}
    contacts = [{"full_name": "Jane VP", "title": "VP Procurement", "email": "jane@a.com", "verified": True}]

    with (
        patch.object(pfe, "run_free_enrichment", new_callable=AsyncMock, return_value={}),
        patch("app.enrichment_service.enrich_entity", new_callable=AsyncMock, return_value=company),
        patch("app.enrichment_service.find_suggested_contacts", new_callable=AsyncMock, return_value=contacts),
        patch("app.services.prospect_warm_intros.detect_warm_intros", return_value={}),
        patch("app.services.prospect_warm_intros.generate_one_liner", return_value=""),
    ):
        await pfe.run_enrichment_job(p.id, db=db_session)

    db_session.refresh(p)
    assert p.industry == "Aerospace & Defense"  # firmographic filled
    assert p.fit_score > 10  # recomputed (was 10)
    assert p.readiness_score >= 70  # strong signals + verified contact
    assert (p.readiness_signals or {}).get("contacts_verified_count") == 1
    assert (p.enrichment_data or {}).get("contact_provider")
    assert (p.enrichment_data or {}).get("contacts_enriched_at")
    assert (p.enrichment_data or {}).get("enrich_status") == "done"


async def test_run_enrichment_job_24h_skip(db_session, monkeypatch):
    from app.services import prospect_free_enrichment as pfe

    recent = datetime.now(timezone.utc).isoformat()
    p = _prospect(db_session, fit_score=10, readiness_score=10,
                  enrichment_data={"contacts_enriched_at": recent},
                  readiness_signals={"intent": {"strength": "strong"}})
    enrich_mock = AsyncMock()
    with (
        patch.object(pfe, "run_free_enrichment", new_callable=AsyncMock, return_value={}),
        patch("app.enrichment_service.enrich_entity", enrich_mock),
        patch("app.services.prospect_warm_intros.detect_warm_intros", return_value={}),
        patch("app.services.prospect_warm_intros.generate_one_liner", return_value=""),
    ):
        await pfe.run_enrichment_job(p.id, db=db_session)

    enrich_mock.assert_not_called()  # within 24h → paid step skipped
    db_session.refresh(p)
    assert (p.enrichment_data or {}).get("enrich_status") == "done"  # still completes + recomputes
```

- [ ] Run (expect **FAIL** — helpers not yet defined):
  `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_prospect_real_enrichment.py -q -p no:cacheprovider -o addopts=""`
- [ ] Add helpers to `app/services/prospect_free_enrichment.py` (after the imports, before
  `enrich_from_sam_gov`):

```python
_DECISION_MAKER_KEYWORDS = (
    "vp", "vice president", "director", "chief", "ceo", "coo", "cfo", "cto", "cpo",
    "head of", "owner", "president",
)
_INFLUENCER_KEYWORDS = (
    "manager", "lead", "senior", "principal", "buyer", "sourcing", "procurement",
    "purchasing", "commodity",
)


def infer_seniority(title: str | None) -> str:
    """Bucket a job title into decision_maker | influencer | contributor (keyword match)."""
    t = (title or "").lower()
    if any(kw in t for kw in _DECISION_MAKER_KEYWORDS):
        return "decision_maker"
    if any(kw in t for kw in _INFLUENCER_KEYWORDS):
        return "influencer"
    return "contributor"


def _apply_company_to_prospect(prospect: ProspectAccount, company: dict | None) -> None:
    """Fill-only firmographic write from enrich_entity output (never clobbers existing)."""
    if not company:
        return
    if company.get("industry") and not prospect.industry:
        prospect.industry = company["industry"]
    if company.get("employee_size") and not prospect.employee_count_range:
        prospect.employee_count_range = company["employee_size"]
    if company.get("revenue_range") and not prospect.revenue_range:
        prospect.revenue_range = company["revenue_range"]
    if company.get("naics") and not prospect.naics_code:  # preserve SAM.gov naics
        prospect.naics_code = company["naics"]
    if not prospect.hq_location and (company.get("hq_city") or company.get("hq_state")):
        city, state = company.get("hq_city"), company.get("hq_state")
        prospect.hq_location = ", ".join(part for part in (city, state) if part)


def _apply_contacts_to_prospect(prospect: ProspectAccount, contacts: list[dict], limit: int) -> list[dict]:
    """Map provider contacts → canonical preview rows (dedup, cap), write contacts_preview."""
    mapped: list[dict] = []
    seen: set[str] = set()
    for c in contacts:
        name = c.get("full_name")
        if not name:
            continue
        key = (c.get("email") or "").lower() or name.lower()
        if key in seen:
            continue
        seen.add(key)
        mapped.append(
            {
                "name": name,
                "title": c.get("title"),
                "seniority": infer_seniority(c.get("title")),
                "email": c.get("email"),
                "verified": bool(c.get("verified")),
            }
        )
        if len(mapped) >= limit:
            break
    prospect.contacts_preview = mapped
    return mapped
```

- [ ] Insert the paid step + fit recompute into `run_enrichment_job`. After
  `result = await run_free_enrichment(prospect_id, db=db)` and the `status = ...` line (lines
  293–294), and after re-fetching `prospect` (line 296), add the gated paid block; then add the
  fit recompute next to the existing readiness recompute (line 308). Concretely:

```python
        # ── Paid enrichment (Lusha chain) — 24h skip gate ──
        from app.config import settings as _settings
        from app.enrichment_service import enrich_entity, find_suggested_contacts

        ed = dict(prospect.enrichment_data or {})
        last = ed.get("contacts_enriched_at")
        recently = False
        if last:
            try:
                recently = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() < 86400
            except (TypeError, ValueError):
                recently = False

        if not recently:
            try:
                company = await enrich_entity(prospect.domain, prospect.name or "")
                contacts = await find_suggested_contacts(
                    prospect.domain, prospect.name or "",
                    limit=_settings.prospect_enrich_contacts_per_account,
                )
                _apply_company_to_prospect(prospect, company)
                mapped = _apply_contacts_to_prospect(prospect, contacts, _settings.prospect_enrich_contacts_per_account)

                signals = dict(prospect.readiness_signals or {})
                signals["contacts_verified_count"] = sum(1 for c in mapped if c["verified"])
                signals["contacts_unverified_count"] = sum(1 for c in mapped if not c["verified"])
                prospect.readiness_signals = signals

                ed["contact_provider"] = (company or {}).get("source") or "lusha"
                ed["contacts_enriched_at"] = datetime.now(timezone.utc).isoformat()
                prospect.enrichment_data = ed
                flag_modified(prospect, "contacts_preview")
                flag_modified(prospect, "readiness_signals")
                flag_modified(prospect, "enrichment_data")
            except Exception as exc:  # noqa: BLE001 — paid step is best-effort; free data already saved
                logger.warning("Paid enrichment step failed for prospect {}: {}", prospect_id, exc)
```

  Place this block immediately after the `if prospect is None: return` guard (line 297–298)
  and before the warm-intro `try`. Then, immediately after the existing readiness recompute
  (lines 308–309), add:

```python
        new_fit, fit_reasoning = calculate_fit_score(
            {
                "industry": prospect.industry,
                "naics_code": prospect.naics_code,
                "employee_count_range": prospect.employee_count_range,
                "region": prospect.region,
                "has_procurement_staff": None,
                "uses_brokers": None,
            }
        )
        prospect.fit_score = new_fit
        prospect.fit_reasoning = fit_reasoning
```

  Add `calculate_fit_score` to the existing import on line 285:
  `from app.services.prospect_scoring import calculate_fit_score, calculate_readiness_score`.
  (The `ed = dict(prospect.enrichment_data or {})` already re-read at line 311 must be kept
  consistent — reuse the `ed` built in the paid block; do not shadow it. Re-read once after
  the paid block if clearer, but preserve `contact_provider`/`contacts_enriched_at`.)

- [ ] Run (expect **PASS**): same command.
- [ ] **Regression** — the existing readiness-recompute test must stay green:
  `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_enrich_readiness_recompute.py -q -p no:cacheprovider -o addopts=""`
- [ ] `ruff check app/services/prospect_free_enrichment.py && mypy app/services/prospect_free_enrichment.py`
- [ ] Commit: `feat(prospecting): consume Lusha chain in run_enrichment_job (SP1 T5)`

---

## Task 6 — Status copy + APP_MAP_INTERACTIONS

**Files**
- Modify `app/templates/htmx/partials/prospecting/enrich_status.html` (line 17).
- Modify `docs/APP_MAP_INTERACTIONS.md` (Prospects/Enrichment row, line 3120; Enrichment-Pipeline diagram, line 1386).

**Interfaces** — none (copy + docs only).

**Steps**
- [ ] Write failing test `tests/test_enrich_status_copy.py`:

```python
"""SP1 status copy: the running fragment advertises contacts + firmographics."""

from pathlib import Path

_TPL = Path("app/templates/htmx/partials/prospecting/enrich_status.html").read_text()


def test_running_copy_mentions_contacts_and_firmographics():
    assert "Enriching… contacts + firmographics" in _TPL
    assert "SAM.gov + news" not in _TPL  # old copy removed
```

- [ ] Run (expect **FAIL**):
  `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_enrich_status_copy.py -q -p no:cacheprovider -o addopts=""`
- [ ] Edit `enrich_status.html` line 17: replace `Enriching… (SAM.gov + news)` with
  `Enriching… contacts + firmographics`.
- [ ] Run (expect **PASS**): same command.
- [ ] Update `docs/APP_MAP_INTERACTIONS.md`:
  - Prospects/Enrichment row (line 3120): in the `enrich (background …)` clause note that the
    background job now also pulls **real contacts + firmographics via the shared Lusha chain**
    (`enrich_entity` + `find_suggested_contacts`), fill-only onto prospect columns, and
    recomputes **both fit and readiness** (was readiness-only).
  - Enrichment-Pipeline diagram (around line 1386–1391): add `lusha.py --> Lusha API` under
    "Phase 1b: API enrichment" alongside `apollo.py` / `prospect_discovery_explorium.py`, and
    note the gap-gated ordering Explorium → Lusha → Apollo → AI + the credit-guard circuit.
- [ ] Commit: `docs(prospecting): Lusha status copy + APP_MAP enrichment row (SP1 T6)`

---

## Done criteria

- All six task files committed; each task independently green via the canonical command.
- Full suite green: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ -q`.
- `pre-commit run --all-files` clean (ruff, ruff-format, mypy, docformatter).
- Lusha gated **off** by default → CRM + existing prospecting behavior unchanged until
  `LUSHA_ENRICHMENT_ENABLED=true` + a key is set.
- No Alembic migration created (verify `alembic heads` is unchanged / single head).
