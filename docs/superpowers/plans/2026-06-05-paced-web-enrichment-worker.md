# Paced Web-Search Enrichment Worker — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A dedicated, paced background worker that fills `material_cards` descriptions/specs for `not_found`/`unenriched` parts via Claude web-search restricted to authorized-distributor/manufacturer domains (a new `web_sourced` tier), without thrashing distributor APIs.

**Architecture:** Repurpose the disabled `enrichment-worker` container into a long-lived async loop (modeled on `ics_worker`) that selects small batches (anti-spin backoff), runs each through the existing `enrich_card` chain — now with a `web_sourced` tier slotted between distributor-`verified` and Opus-`ai_inferred` — paces via a daily web-call budget + per-source cooldowns, and heartbeats to a singleton status table. A web extractor enforces four trust gates in Python (domain allowlist, exact-MPN-on-page, ≥0.92 confidence, URL capture). Precede the worker with type-foundation hardening and the rate-limit/error fixes the worker depends on.

**Tech Stack:** FastAPI, SQLAlchemy 2.0 (sync), PostgreSQL (SQLite in tests), Alembic, Claude via `app/utils/claude_client.py` (`claude_json` + `web_search_20250305` tool), Redis/`intel_cache`, apscheduler-style worker pattern (`app/services/ics_worker`), pytest.

**Spec:** `docs/superpowers/specs/2026-06-05-paced-web-enrichment-worker-design.md`
**Run tests:** `TESTING=1 PYTHONPATH=/root/availai python3 -m pytest <args> --override-ini="addopts="`. Pre-commit each commit: `pre-commit run --files <files>`. Commit trailer: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.

---

## File Structure

**Create:**
- `app/services/enrichment_types.py` — `FieldProvenance` + `EnrichmentProvenance` TypedDicts.
- `app/services/enrichment_worker/__init__.py`, `__main__.py`, `config.py`, `trusted_domains.py`, `web_extractor.py`, `circuit_breaker.py`, `worker.py`.
- `app/models/enrichment_worker_status.py` — singleton status model + `update_enrichment_worker_status`.
- `alembic/versions/<head+1>_enrichment_worker_status.py`.
- tests: `test_enrichment_status_enum.py`, `test_enrichment_rate_limit.py`, `test_trusted_domains.py`, `test_web_extractor.py`, `test_web_sourced_tier.py`, `test_enrichment_worker.py`.

**Modify:**
- `app/constants.py` — add `MaterialEnrichmentStatus(StrEnum)`.
- `app/models/intelligence.py` — `@validates("enrichment_status")`.
- `app/services/authoritative_enrichment_service.py` — rate-limit cooldown (F1/F2), `not_found` provenance (F5), `web_sourced` tier (`apply_web_sourced` + chain), enum-derived usage.
- `app/connectors/element14.py` — tighten QPS-403 classification.
- `scripts/import_part_numbers.py` — `gather(return_exceptions=True)` + commit guard + report-in-finally (F3/F4).
- `app/templates/htmx/partials/materials/list.html`, `app/services/faceted_search_service.py`, `app/static/htmx_app.js` — `web_sourced` badge + filter.
- `docker-compose.yml` — re-enable `enrichment-worker`.

---

## Task 1: Tier vocabulary as an enforced enum + provenance TypedDicts

**Files:** Modify `app/constants.py`, `app/models/intelligence.py`; Create `app/services/enrichment_types.py`, `tests/test_enrichment_status_enum.py`.

- [ ] **Step 1: Failing test**

`tests/test_enrichment_status_enum.py`:
```python
import pytest
from datetime import datetime, timezone
from app.constants import MaterialEnrichmentStatus
from app.models import MaterialCard


def test_enum_values():
    assert MaterialEnrichmentStatus.WEB_SOURCED == "web_sourced"
    assert set(MaterialEnrichmentStatus) >= {
        MaterialEnrichmentStatus.UNENRICHED, MaterialEnrichmentStatus.VERIFIED,
        MaterialEnrichmentStatus.WEB_SOURCED, MaterialEnrichmentStatus.AI_INFERRED,
        MaterialEnrichmentStatus.NOT_FOUND,
    }


def test_validator_rejects_bad_status(db_session):
    card = MaterialCard(normalized_mpn="x1", display_mpn="X1", created_at=datetime.now(timezone.utc))
    with pytest.raises(ValueError):
        card.enrichment_status = "verifed"  # typo


def test_validator_accepts_enum_and_literal(db_session):
    card = MaterialCard(normalized_mpn="x2", display_mpn="X2", created_at=datetime.now(timezone.utc))
    card.enrichment_status = "web_sourced"
    assert card.enrichment_status == "web_sourced"
    card.enrichment_status = MaterialEnrichmentStatus.VERIFIED
    assert card.enrichment_status == "verified"
```

- [ ] **Step 2: Run → fail** (`ImportError`/no validator). `python3 -m pytest tests/test_enrichment_status_enum.py -v --override-ini="addopts="`

- [ ] **Step 3: Add the enum** to `app/constants.py` (match the existing `StrEnum` import + sibling enums like `DigestStatusSignal`):
```python
class MaterialEnrichmentStatus(StrEnum):
    UNENRICHED = "unenriched"
    VERIFIED = "verified"
    WEB_SOURCED = "web_sourced"
    AI_INFERRED = "ai_inferred"
    NOT_FOUND = "not_found"
```

- [ ] **Step 4: Add the validator** to `MaterialCard` in `app/models/intelligence.py` (mirror the existing `@validates` methods in this file; `validates` is already imported):
```python
    @validates("enrichment_status")
    def _validate_enrichment_status(self, _key, value):
        from ..constants import MaterialEnrichmentStatus
        if value is None:
            return MaterialEnrichmentStatus.UNENRICHED.value
        return MaterialEnrichmentStatus(value).value  # raises ValueError on unknown
```

- [ ] **Step 5: Create the provenance TypedDicts** `app/services/enrichment_types.py`:
```python
"""Typed shapes for MaterialCard.enrichment_provenance (the JSONB column stays
``dict`` at the ORM layer; these constrain the producer functions under mypy)."""
from __future__ import annotations
from typing import NotRequired, TypedDict


class FieldProvenance(TypedDict):
    source: str
    confidence: float
    fetched_at: str
    matched_mpn: NotRequired[str]


class EnrichmentProvenance(TypedDict, total=False):
    reconfirm_needed: bool
    web_sourced: bool
    confidence: float
    source_urls: list[str]
    source_domains: list[str]
    fetched_at: str
    # plus per-field FieldProvenance entries keyed by field name (description, etc.)
```

- [ ] **Step 6: Run → pass.** Then `pre-commit run --files app/constants.py app/models/intelligence.py app/services/enrichment_types.py tests/test_enrichment_status_enum.py`.

- [ ] **Step 7: Commit** `feat(materials): MaterialEnrichmentStatus enum + status validator + provenance TypedDicts`.

---

## Task 2: Rate-limit cooldown + not_found provenance (silent-failure fixes F1/F2/F5)

**Files:** Modify `app/services/authoritative_enrichment_service.py`, `app/connectors/element14.py`; Create `tests/test_enrichment_rate_limit.py`.

- [ ] **Step 1: Failing tests** `tests/test_enrichment_rate_limit.py`:
```python
import asyncio
from app.connectors.errors import ConnectorQuotaError, ConnectorRateLimitError
from app.services.authoritative_enrichment_service import fetch_authoritative


def _rl_conn(name="element14"):
    class _C:
        source_name = name
        calls = 0
        async def search(self, pn):
            type(self).calls += 1
            raise ConnectorRateLimitError("element14 rate limited (QPS)")
    return _C()


def test_rate_limit_cools_down_not_disabled():
    conn = _rl_conn()
    disabled: set[str] = set()
    cooldown: dict[str, float] = {}
    # First MPN: rate-limited -> cooldown set, NOT permanently disabled
    asyncio.run(fetch_authoritative("A", "a", [conn], disabled, cooldown))
    assert "element14" not in disabled
    assert "element14" in cooldown
    calls_after_first = type(conn).calls
    # Second MPN immediately: still in cooldown -> skipped (no new call)
    asyncio.run(fetch_authoritative("B", "b", [conn], disabled, cooldown))
    assert type(conn).calls == calls_after_first


def test_quota_still_disables():
    class _Q:
        source_name = "oemsecrets"
        async def search(self, pn):
            raise ConnectorQuotaError("out of api calls")
    disabled: set[str] = set()
    asyncio.run(fetch_authoritative("A", "a", [_Q()], disabled, {}))
    assert "oemsecrets" in disabled
```

- [ ] **Step 2: Run → fail** (`fetch_authoritative` takes no `cooldown` arg).

- [ ] **Step 3: Implement** — change `fetch_authoritative` signature + handling in `app/services/authoritative_enrichment_service.py`:
```python
import time as _time
_RATE_COOLDOWN_SECONDS = 300  # 5 min

async def fetch_authoritative(
    display_mpn, normalized_mpn, connectors, disabled=None, cooldown=None,
):
    results: dict[str, list[dict]] = {}
    now = _time.monotonic()
    for conn in connectors:
        name = _SOURCE_TYPE_ALIASES.get(conn.source_name, conn.source_name)
        if disabled is not None and name in disabled:
            continue
        if cooldown is not None and cooldown.get(name, 0) > now:
            continue  # rate-limit cooldown active
        if name == "nexar":
            merged, _, _ = merge_authoritative(normalized_mpn, results)
            if all(f in merged for f in _ADEQUATE):
                break
        try:
            results[name] = await conn.search(display_mpn)
        except (ConnectorQuotaError, ConnectorAuthError) as e:
            if disabled is not None:
                disabled.add(name)
            logger.error("AUTH_ENRICH: {} DISABLED for run ({}): {}", name, type(e).__name__, e)
            results[name] = []
        except ConnectorRateLimitError as e:
            if cooldown is not None:
                cooldown[name] = _time.monotonic() + _RATE_COOLDOWN_SECONDS
            logger.warning("AUTH_ENRICH: {} rate-limited for {} (cooldown {}s): {}",
                           name, normalized_mpn, _RATE_COOLDOWN_SECONDS, e)
            results[name] = []
        except Exception as e:
            logger.warning("AUTH_ENRICH: {} failed for {}: {}: {}", name, normalized_mpn, type(e).__name__, e)
            results[name] = []
    return results
```
Import `ConnectorRateLimitError` at the top (alongside `ConnectorAuthError, ConnectorQuotaError`). Update `enrich_card` to create + thread a `cooldown: dict[str, float]` (caller-supplied like `disabled`): add param `cooldown: dict[str, float] | None = None` and pass it to `fetch_authoritative`.

- [ ] **Step 4: F5 — fix the `not_found` branch** in `enrich_card` (the final fall-through):
```python
    card.enrichment_status = "not_found"
    card.enrichment_source = None
    card.enrichment_provenance = None
    return "not_found"
```
(Was: `enrichment_source = card.enrichment_source or "claude_opus_inferred"` — that mislabeled unresolved parts.)

- [ ] **Step 5: F1 — tighten element14 QPS classification** in `app/connectors/element14.py` (replace the QPS check added earlier):
```python
        body = r.text.lower()
        _AUTH_MARKERS = ("invalid", "unauthorized", "forbidden", "api key", "not accepted")
        if r.status_code == 403 and "queries per second" in body and not any(m in body for m in _AUTH_MARKERS):
            raise ConnectorRateLimitError(f"element14 rate limited (QPS): {r.text[:200]}")
        if r.status_code in (401, 403):
            raise ConnectorAuthError(f"element14 auth error: HTTP {r.status_code} {r.text[:200]}")
```
(Remove the now-inaccurate "retried with backoff" comment.)

- [ ] **Step 6: Run → pass.** Also re-run `tests/test_authoritative_enrichment.py` (existing) to confirm no regression. Pre-commit the changed files.

- [ ] **Step 7: Commit** `fix(materials): rate-limit cooldown (not disable) + clean not_found provenance + tighten element14 QPS`.

---

## Task 3: Import-script batch robustness (F3/F4)

**Files:** Modify `scripts/import_part_numbers.py`; Test: `tests/test_part_number_import.py` (add).

- [ ] **Step 1: Failing test** — add to `tests/test_part_number_import.py`:
```python
def test_poison_mpn_does_not_sink_chunk(monkeypatch, db_session, tmp_path):
    # one MPN raises inside enrich_card; the rest of the chunk must still be reported
    import scripts.import_part_numbers as imp

    async def fake_enrich(card, db, **kw):
        if card.display_mpn == "BOOM":
            raise RuntimeError("poison")
        card.enrichment_status = "not_found"
        return "not_found"

    monkeypatch.setattr(imp, "enrich_card", fake_enrich)
    monkeypatch.setattr(imp, "_connectors_in_order", lambda db: [])
    f = tmp_path / "s.csv"
    f.write_text("mpn\nOK1\nBOOM\nOK2\n")
    rep = tmp_path / "out.csv"
    import asyncio
    asyncio.run(imp._run(str(f), commit=False, report_path=str(rep), refresh=False, concurrency=4))
    rows = rep.read_text()
    assert "OK1" in rows and "OK2" in rows and "error" in rows  # poison -> status=error, chunk survives
```

- [ ] **Step 2: Run → fail** (currently `gather` propagates, aborting the chunk).

- [ ] **Step 3: Implement** in `scripts/import_part_numbers.py` `_run`:
  - `statuses = await asyncio.gather(*(_enrich(item.card) for item in chunk), return_exceptions=True)`
  - In the zip loop, when `status` is an `Exception`: set the report row `status="error"`, log `logger.error("enrich failed for {}: {}", item.raw, status)`, and do not count it as a tier.
  - Wrap the per-chunk `db.commit()` in `try/except Exception` → `logger.error(...)`, `db.rollback()`, `continue`.
  - Move the report write into a `finally:` (or write incrementally) so a mid-run failure still yields a partial report.

```python
            statuses = await asyncio.gather(*(_enrich(item.card) for item in chunk), return_exceptions=True)
            for item, status in zip(chunk, statuses):
                if isinstance(status, Exception):
                    logger.error("enrich failed for {}: {}", item.raw, status)
                    rows.append({"input_mpn": item.raw, "normalized_mpn": item.norm,
                                 "status": "error", "notes": f"{type(status).__name__}: {status}"})
                    continue
                counts[status] = counts.get(status, 0) + 1
                rows.append(_report_row(item.raw, item.norm, status, item.card, item.transient))
                if commit and item.transient:
                    db.add(item.card)
            if commit:
                try:
                    db.commit()
                except Exception as e:
                    logger.error("commit failed for chunk {}-{}: {}", start, start + len(chunk), e)
                    db.rollback()
```
And wrap the report write in `finally`.

- [ ] **Step 4: Run → pass.** Pre-commit. **Commit** `fix(import): survive poison MPNs (gather return_exceptions) + guard commit + report in finally`.

---

## Task 4: Trusted-domain gate

**Files:** Create `app/services/enrichment_worker/__init__.py`, `app/services/enrichment_worker/trusted_domains.py`, `tests/test_trusted_domains.py`.

- [ ] **Step 1: Failing test** `tests/test_trusted_domains.py`:
```python
from app.services.enrichment_worker.trusted_domains import is_trusted_domain


def test_authorized_distributor():
    assert is_trusted_domain("https://www.digikey.com/en/products/detail/x")
    assert is_trusted_domain("https://www.mouser.com/ProductDetail/x")


def test_manufacturer_suffix():
    assert is_trusted_domain("https://www.ti.com/product/LM317")
    assert is_trusted_domain("https://st.com/foo")


def test_rejects_lookalike_and_untrusted():
    assert not is_trusted_domain("https://evil-st.com/foo")     # suffix spoof
    assert not is_trusted_domain("https://www.ebay.com/itm/123")
    assert not is_trusted_domain("ftp://www.ti.com/x")          # non-http
    assert not is_trusted_domain("not a url")
```

- [ ] **Step 2: Run → fail.** **Step 3: Implement** `__init__.py` (one-line docstring) + `trusted_domains.py`:
```python
"""Security allowlist for web_sourced enrichment: only authorized-distributor or
manufacturer-official domains may produce web_sourced data. Validated in code."""
from __future__ import annotations
from urllib.parse import urlparse

AUTHORIZED_DISTRIBUTORS: frozenset[str] = frozenset({
    "www.digikey.com", "www.mouser.com", "www.newark.com", "www.element14.com",
    "www.farnell.com", "www.arrow.com", "www.avnet.com", "www.ttiinc.com",
    "uk.rs-online.com", "us.rs-online.com", "www.rs-online.com", "www.futureelectronics.com",
})
MANUFACTURER_DOMAINS: dict[str, str] = {
    "st.com": "STMicroelectronics", "ti.com": "Texas Instruments", "analog.com": "Analog Devices",
    "infineon.com": "Infineon", "samsung.com": "Samsung", "bourns.com": "Bourns",
    "nxp.com": "NXP", "microchip.com": "Microchip", "onsemi.com": "onsemi", "vishay.com": "Vishay",
    "murata.com": "Murata", "tdk.com": "TDK", "te.com": "TE Connectivity", "molex.com": "Molex",
    "amphenol.com": "Amphenol", "rohm.com": "ROHM", "renesas.com": "Renesas",
}


def is_trusted_domain(url: str) -> bool:
    try:
        p = urlparse(url)
    except Exception:
        return False
    if p.scheme not in ("http", "https") or not p.hostname:
        return False
    host = p.hostname.lower()
    if host in AUTHORIZED_DISTRIBUTORS:
        return True
    return any(host == k or host.endswith("." + k) for k in MANUFACTURER_DOMAINS)
```

- [ ] **Step 4: Run → pass.** Pre-commit. **Commit** `feat(enrichment-worker): trusted-domain allowlist gate`.

---

## Task 5: Web extractor + four gates

**Files:** Create `app/services/enrichment_worker/web_extractor.py`, `tests/test_web_extractor.py`.

- [ ] **Step 1: Failing tests** `tests/test_web_extractor.py` (mock `claude_json`):
```python
from unittest.mock import AsyncMock, patch
import pytest
from app.services.enrichment_worker.web_extractor import extract_part_from_web

_GOOD = {"description": "Adjustable linear voltage regulator", "manufacturer": "Texas Instruments",
         "category": "Voltage Regulator", "datasheet_url": "https://www.ti.com/lit/ds/x.pdf",
         "confidence": 0.97, "exact_mpn_found": "LM317T",
         "source_urls": ["https://www.ti.com/product/LM317"]}


@pytest.mark.asyncio
@patch("app.services.enrichment_worker.web_extractor.claude_json", new_callable=AsyncMock)
async def test_all_gates_pass(mock_cj):
    mock_cj.return_value = dict(_GOOD)
    r = await extract_part_from_web("LM317T", "lm317t")
    assert r.status == "web_sourced"
    assert r.source_urls == ["https://www.ti.com/product/LM317"]
    assert mock_cj.call_args.kwargs["tools"][0]["type"] == "web_search_20250305"


@pytest.mark.asyncio
@patch("app.services.enrichment_worker.web_extractor.claude_json", new_callable=AsyncMock)
async def test_untrusted_domain_rejected(mock_cj):
    mock_cj.return_value = {**_GOOD, "source_urls": ["https://www.ebay.com/itm/1"]}
    assert (await extract_part_from_web("LM317T", "lm317t")).status == "failed"


@pytest.mark.asyncio
@patch("app.services.enrichment_worker.web_extractor.claude_json", new_callable=AsyncMock)
async def test_mpn_mismatch_rejected(mock_cj):
    mock_cj.return_value = {**_GOOD, "exact_mpn_found": "LM317MT"}
    assert (await extract_part_from_web("LM317T", "lm317t")).status == "failed"


@pytest.mark.asyncio
@patch("app.services.enrichment_worker.web_extractor.claude_json", new_callable=AsyncMock)
async def test_low_confidence_rejected(mock_cj):
    mock_cj.return_value = {**_GOOD, "confidence": 0.80}
    assert (await extract_part_from_web("LM317T", "lm317t")).status == "failed"


@pytest.mark.asyncio
@patch("app.services.enrichment_worker.web_extractor.claude_json", new_callable=AsyncMock)
async def test_claude_error_returns_failed(mock_cj):
    mock_cj.side_effect = RuntimeError("claude down")
    assert (await extract_part_from_web("LM317T", "lm317t")).status == "failed"
```

- [ ] **Step 2: Run → fail.** **Step 3: Implement** `web_extractor.py`:
```python
"""Grounded web-search enrichment: Claude reads authoritative pages and extracts
description/manufacturer/category/datasheet. Four gates enforced in Python."""
from __future__ import annotations
from dataclasses import dataclass, field
from urllib.parse import urlparse
from loguru import logger
from app.utils.normalization import normalize_mpn_key
from app.utils.claude_client import claude_json
from .trusted_domains import is_trusted_domain

_MIN_WEB_CONFIDENCE = 0.92

_SYSTEM = ("You are an electronic component data extraction assistant. Use web search to find "
           "AUTHORITATIVE manufacturer or authorized-distributor pages for the given MPN. "
           "Return ONLY valid JSON. Never invent data; use null when unknown.")
_PROMPT = ("Find the exact electronic component MPN {mpn} on a manufacturer or authorized distributor "
           "page. Return JSON: {{\"description\": str, \"manufacturer\": str, \"category\": str, "
           "\"datasheet_url\": str|null, \"confidence\": float, \"exact_mpn_found\": str, "
           "\"source_urls\": [str]}}. exact_mpn_found must be the MPN exactly as printed on the page.")


@dataclass
class WebExtractResult:
    status: str  # "web_sourced" | "failed"
    description: str | None = None
    manufacturer: str | None = None
    category: str | None = None
    datasheet_url: str | None = None
    confidence: float = 0.0
    source_urls: list[str] = field(default_factory=list)
    source_domains: list[str] = field(default_factory=list)


_FAILED = WebExtractResult(status="failed")


async def extract_part_from_web(display_mpn: str, normalized_mpn: str, *, timeout: int = 90) -> WebExtractResult:
    try:
        data = await claude_json(
            _PROMPT.format(mpn=display_mpn), system=_SYSTEM, model_tier="smart",
            max_tokens=1200, tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 4}],
            timeout=timeout,
        )
    except Exception as e:
        logger.warning("WEB_ENRICH: claude error for {}: {}", display_mpn, type(e).__name__)
        return _FAILED
    if not isinstance(data, dict):
        return _FAILED
    # Gate 1: trusted domains
    urls = [u for u in (data.get("source_urls") or []) if isinstance(u, str) and is_trusted_domain(u)]
    if not urls:
        logger.info("WEB_ENRICH: {} rejected — no trusted source ({})", display_mpn, data.get("source_urls"))
        return _FAILED
    # Gate 2: exact MPN verbatim
    if normalize_mpn_key(data.get("exact_mpn_found")) != normalized_mpn:
        return _FAILED
    # Gate 3: confidence
    try:
        conf = float(data.get("confidence") or 0.0)
    except (TypeError, ValueError):
        conf = 0.0
    if conf < _MIN_WEB_CONFIDENCE:
        return _FAILED
    desc = (data.get("description") or "").strip()
    mfr = (data.get("manufacturer") or "").strip()
    if len(desc) < 10 or not mfr:
        return _FAILED
    return WebExtractResult(
        status="web_sourced", description=desc, manufacturer=mfr,
        category=(data.get("category") or "").strip() or None,
        datasheet_url=(data.get("datasheet_url") or "").strip() or None,
        confidence=conf, source_urls=urls,
        source_domains=sorted({urlparse(u).hostname or "" for u in urls}),
    )
```

- [ ] **Step 4: Run → pass.** Pre-commit. **Commit** `feat(enrichment-worker): grounded web extractor with 4 trust gates`.

---

## Task 6: web_sourced tier in enrich_card

**Files:** Modify `app/services/authoritative_enrichment_service.py`; Create `tests/test_web_sourced_tier.py`.

- [ ] **Step 1: Failing tests** `tests/test_web_sourced_tier.py` (patch the web extractor + connectors):
```python
import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from app.models import MaterialCard
from app.services.enrichment_worker.web_extractor import WebExtractResult
from app.services.authoritative_enrichment_service import enrich_card


def _card(db, mpn="LM317T"):
    from app.utils.normalization import normalize_mpn_key
    c = MaterialCard(normalized_mpn=normalize_mpn_key(mpn), display_mpn=mpn, created_at=datetime.now(timezone.utc))
    db.add(c); db.flush(); return c


@patch("app.services.authoritative_enrichment_service.extract_part_from_web", new_callable=AsyncMock)
@patch("app.services.authoritative_enrichment_service._connectors_in_order")
def test_web_sourced_when_no_api_hit(mock_conns, mock_web, db_session):
    from tests.test_authoritative_enrichment import _FakeConn
    mock_conns.return_value = [_FakeConn("digikey", [])]
    mock_web.return_value = WebExtractResult(status="web_sourced", description="Adj regulator",
        manufacturer="TI", category="Voltage Regulator", confidence=0.97,
        source_urls=["https://www.ti.com/product/LM317"], source_domains=["www.ti.com"])
    card = _card(db_session)
    asyncio.run(enrich_card(card, db_session))
    assert card.enrichment_status == "web_sourced"
    assert card.enrichment_source == "web_search"
    assert card.enrichment_provenance["source_urls"] == ["https://www.ti.com/product/LM317"]


@patch("app.services.ai_inference_fallback.claude_structured", new_callable=AsyncMock)
@patch("app.services.authoritative_enrichment_service.extract_part_from_web", new_callable=AsyncMock)
@patch("app.services.authoritative_enrichment_service._connectors_in_order")
def test_falls_through_to_ai_when_web_fails(mock_conns, mock_web, mock_claude, db_session):
    from tests.test_authoritative_enrichment import _FakeConn
    mock_conns.return_value = [_FakeConn("digikey", [])]
    mock_web.return_value = WebExtractResult(status="failed")
    mock_claude.return_value = {"description": "guess", "category": "x", "confidence": 0.97}
    card = _card(db_session, "04M3HJ")
    asyncio.run(enrich_card(card, db_session))
    assert card.enrichment_status == "ai_inferred"
```

- [ ] **Step 2: Run → fail.** **Step 3: Implement** — in `authoritative_enrichment_service.py`:
  - Import: `from app.services.enrichment_worker.web_extractor import WebExtractResult, extract_part_from_web` (top-level is fine; no circular import — web_extractor imports only normalization/claude_client/trusted_domains).
  - Add `apply_web_sourced`:
```python
def apply_web_sourced(card: MaterialCard, result) -> None:
    now = datetime.now(timezone.utc)
    fields = {"description": result.description, "manufacturer": result.manufacturer,
              "category": result.category, "datasheet_url": result.datasheet_url}
    prov = {"web_sourced": True, "confidence": result.confidence,
            "source_urls": result.source_urls, "source_domains": result.source_domains,
            "fetched_at": now.isoformat()}
    for f, v in fields.items():
        if v:
            setattr(card, f, v)
            prov[f] = {"source": "web_search", "confidence": result.confidence, "fetched_at": now.isoformat()}
    card.enrichment_source = "web_search"
    card.enrichment_status = "web_sourced"
    card.enrichment_provenance = prov
    card.enriched_at = now
```
  - In `enrich_card`, between the `if merged:` block and the `infer_part` call, insert (gated by `disabled`/cooldown/budget — see Task 8 for the worker passing a "web_search" disable):
```python
    if not (disabled and "web_search" in disabled):
        web = await extract_part_from_web(card.display_mpn, card.normalized_mpn)
        if web.status == "web_sourced":
            apply_web_sourced(card, web)
            return "web_sourced"
```

- [ ] **Step 4: Run → pass** (+ re-run `tests/test_authoritative_enrichment.py`). Pre-commit. **Commit** `feat(materials): web_sourced enrichment tier in enrich_card`.

---

## Task 7: EnrichmentWorkerStatus model + migration

**Files:** Create `app/models/enrichment_worker_status.py`, `alembic/versions/<head+1>_enrichment_worker_status.py`, `tests/` assertion; register in `app/models/__init__.py`.

- [ ] **Step 1:** Determine the current head: `PYTHONPATH=/root/availai alembic heads` (expected `a1f7c2d9e4b8` unless a newer migration landed) — use it as `down_revision`. Name the file `088_enrichment_worker_status.py` (numeric prefix matches recent siblings like `087`).

- [ ] **Step 2: Failing test** (add to `tests/test_enrichment_worker.py`):
```python
def test_worker_status_singleton(db_session):
    from app.models.enrichment_worker_status import EnrichmentWorkerStatus
    row = db_session.query(EnrichmentWorkerStatus).get(1)
    # created by migration seed; in SQLite tests, create_all + a fixture seed may be needed
    assert row is None or row.id == 1
```

- [ ] **Step 3: Model** `app/models/enrichment_worker_status.py` (mirror `app/models/ics_worker_status.py`):
```python
from datetime import datetime, timezone
from sqlalchemy import Boolean, CheckConstraint, Column, Integer, JSON, Text
from app.models import Base
from app.utils.types import UTCDateTime  # match the import used by ics_worker_status


class EnrichmentWorkerStatus(Base):
    __tablename__ = "enrichment_worker_status"
    __table_args__ = (CheckConstraint("id = 1", name="ck_enrichment_worker_status_singleton"),)
    id = Column(Integer, primary_key=True, default=1)
    is_running = Column(Boolean, default=False, server_default="false", nullable=False)
    last_heartbeat = Column(UTCDateTime)
    last_enriched_at = Column(UTCDateTime)
    enriched_today = Column(Integer, default=0, server_default="0", nullable=False)
    web_sourced_today = Column(Integer, default=0, server_default="0", nullable=False)
    ai_inferred_today = Column(Integer, default=0, server_default="0", nullable=False)
    not_found_today = Column(Integer, default=0, server_default="0", nullable=False)
    circuit_breaker_open = Column(Boolean, default=False, server_default="false", nullable=False)
    circuit_breaker_reason = Column(Text)
    daily_stats_json = Column(JSON)
    updated_at = Column(UTCDateTime, default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))
```
Add `def update_enrichment_worker_status(db, **kw): ...` (upsert id=1, set fields + updated_at) mirroring `ics_worker_status`'s helper. Register the model in `app/models/__init__.py` (follow how `IcsWorkerStatus` is imported there).

- [ ] **Step 4: Migration** (verify exact `UTCDateTime`/column types against `031_ics_search_tables.py`):
```python
revision = "088_enrichment_worker_status"
down_revision = "a1f7c2d9e4b8"  # confirm via `alembic heads`
def upgrade():
    op.create_table("enrichment_worker_status", ...same columns...)
    op.execute("INSERT INTO enrichment_worker_status (id) VALUES (1)")
def downgrade():
    op.drop_table("enrichment_worker_status")
```

- [ ] **Step 5:** Run test (model importable; `create_all` builds it for SQLite). `upgrade/downgrade` round-trip on Postgres if reachable, else note. Pre-commit. **Commit** `feat(enrichment-worker): status singleton model + migration`.

---

## Task 8: Worker config + circuit breaker

**Files:** Create `app/services/enrichment_worker/config.py`, `app/services/enrichment_worker/circuit_breaker.py`; tests in `tests/test_enrichment_worker.py`.

- [ ] **Step 1–4 (config):** `config.py` — `EnrichmentWorkerConfig` reading env with the defaults from spec §5.5 (`ENRICHMENT_BATCH_SIZE=5`, `DAILY_CAP=200`, `WEB_DAILY_CAP=80`, `LOOP_SLEEP=30`, `IDLE_SLEEP=300`, `NOT_FOUND_RETRY_HOURS=22`, `CIRCUIT_BREAKER_ERRORS=5`). Test: env override changes the value. **Commit**.

- [ ] **Step 5–8 (breaker):** `circuit_breaker.py` — `EnrichmentCircuitBreaker(CircuitBreakerBase)` (from `app/services/search_worker_base/circuit_breaker.py`) with `record_claude_error()`/`record_claude_success()`; trips after N consecutive errors; `should_stop()`; 1h cooldown reset. Test: N errors → `should_stop()` True; a success resets the counter. **Commit** `feat(enrichment-worker): config + circuit breaker`.

---

## Task 9: Worker loop

**Files:** Create `app/services/enrichment_worker/worker.py`, `app/services/enrichment_worker/__main__.py`; tests in `tests/test_enrichment_worker.py`.

- [ ] **Step 1: Failing tests** — the pure, testable pieces (avoid testing the infinite loop directly):
  - `select_batch(db, config)` returns `unenriched` cards + `not_found` older than `NOT_FOUND_RETRY_HOURS`, excludes recent `not_found` + `is_internal_part` + `deleted_at`, ordered by `search_count` desc.
  - `run_one_batch(db, config, cooldown, breaker)` enriches a batch via `enrich_card` (mocked), stamps `enriched_at`, returns per-tier counts.
```python
def test_select_batch_anti_spin(db_session):
    from datetime import datetime, timezone, timedelta
    from app.models import MaterialCard
    from app.services.enrichment_worker.worker import select_batch
    from app.services.enrichment_worker.config import EnrichmentWorkerConfig
    now = datetime.now(timezone.utc)
    def mk(mpn, status, enriched=None, sc=0):
        c = MaterialCard(normalized_mpn=mpn, display_mpn=mpn.upper(), enrichment_status=status,
                         enriched_at=enriched, search_count=sc, created_at=now)
        db_session.add(c); return c
    mk("u1", "unenriched", sc=5)
    mk("nf_old", "not_found", enriched=now - timedelta(hours=30))
    mk("nf_recent", "not_found", enriched=now - timedelta(hours=1))
    mk("ver", "verified")
    db_session.flush()
    cfg = EnrichmentWorkerConfig(batch_size=10, not_found_retry_hours=22)
    picked = {c.normalized_mpn for c in select_batch(db_session, cfg)}
    assert picked == {"u1", "nf_old"}
```

- [ ] **Step 2: Run → fail.** **Step 3: Implement** `worker.py`:
  - `select_batch(db, config)` — the anti-spin query from spec §5.5.
  - `async def run_one_batch(db, config, cooldown, breaker) -> dict` — fetch batch; if empty return `{}`; `disabled=set()`; check the Redis web budget (`enrichment_worker:web_calls:{date}` via `intel_cache.get_cached`), and if `>= WEB_DAILY_CAP` add `"web_search"` to `disabled`; for each card `await enrich_card(card, db, disabled=disabled, cooldown=cooldown)`, stamp `enriched_at=now`, bump counts (keys from `MaterialEnrichmentStatus`), increment the web counter when a web call was made; `breaker.record_claude_success/error` based on whether `"web_search"` got disabled; `db.commit()` (guarded); return counts.
  - `async def main()` — SIGTERM/SIGINT handlers (mirror `ics_worker/worker.py`), startup `update_enrichment_worker_status(is_running=True)`, loop: shutdown check → daily reset at UTC-midnight → daily-cap/breaker long-sleep → `db=SessionLocal()` → `run_one_batch` → heartbeat + counters → `db.close()` → sleep (LOOP vs IDLE). Shutdown sets `is_running=False`.
  - `__main__.py`: `import asyncio; from .worker import main; asyncio.run(main())`.

- [ ] **Step 4: Run → pass.** Pre-commit. **Commit** `feat(enrichment-worker): paced worker loop with anti-spin batch selection`.

---

## Task 10: docker-compose re-enable + web_sourced UI

**Files:** Modify `docker-compose.yml`, `app/templates/htmx/partials/materials/list.html`, `app/services/faceted_search_service.py`, `app/static/htmx_app.js`.

- [ ] **Step 1: docker-compose** — replace the disabled `enrichment-worker` block per spec §5.6 (`command: ["python","-m","app.services.enrichment_worker"]`, `restart: always`, `ENRICHMENT_*` env, `depends_on db/redis/app healthy`, `healthcheck disable`, mem 512M).

- [ ] **Step 2: list.html** — add a `web_sourced` badge branch (blue) after the `verified` branch, with the source URL as a link:
```html
            {% elif es == "web_sourced" %}
            {% set _u = (m.enrichment_provenance or {}).get('source_urls', [None])[0] %}
            <a href="{{ _u or '#' }}" target="_blank" rel="noopener"
               class="inline-flex px-2 py-0.5 text-xs font-medium rounded-full border bg-sky-50 text-sky-700 border-sky-200"
               title="Web-sourced from {{ (m.enrichment_provenance or {}).get('source_domains', ['authoritative page'])|join(', ') }} — below API-verified">
              WEB-SOURCED
            </a>
```
(Reuse the `bg-sky-50/text-sky-700/border-sky-200` family — verify it's already in the CSS bundle; if not, add to safelist.)

- [ ] **Step 3: faceted_search_service.py** — the existing `verified_only` filters `enrichment_status == "verified"`. Add an optional `statuses: list[str] | None` filter (e.g. `query.filter(MaterialCard.enrichment_status.in_(statuses))`) so the UI can request `["verified","web_sourced"]`; keep `verified_only` as-is for API-only.

- [ ] **Step 4: htmx_app.js** — add a `web_sourced` checkbox/state to the `materialsFilter` Alpine component (mirror the `verifiedOnly` wiring: state, syncFromURL, pushURL, hx-vals) posting `statuses`.

- [ ] **Step 5:** `npm run build` (verify build + sky classes present). Pre-commit. **Commit** `feat(materials-ui): web_sourced badge + filter; re-enable enrichment worker`.

---

## Task 11: Integration, load, deploy

- [ ] **Step 1:** `pre-commit run --all-files`; full suite `TESTING=1 PYTHONPATH=/root/availai python3 -m pytest tests/ -q`. Fix regressions.
- [ ] **Step 2:** Load all 1,827 bare cards (no enrichment) so the worker has the full set: `POST /api/materials/import-part-numbers` with `/root/Material Items/report1780605266325.xls` (creates bare `unenriched` cards; idempotent on the ~932 already present).
- [ ] **Step 3:** Deploy `./deploy.sh` (it builds `--no-cache`, restarts; the `enrichment-worker` container starts). Confirm the worker container is up + heartbeating (`enrichment_worker_status.last_heartbeat` advancing) and enriching a few cards; watch logs for cooldown/breaker behavior.
- [ ] **Step 4:** Monitor over a day: per-tier growth in `enrichment_worker_status`. Update `docs/APP_MAP_ARCHITECTURE.md` (worker + web extractor), `APP_MAP_DATABASE.md` (status table + web_sourced tier), `APP_MAP_INTERACTIONS.md` (worker behavior + UI filter). **Commit** docs. Open PR; run PR-review agents.

---

## Self-Review (completed by plan author)

- **Spec coverage:** §2 decisions → Tasks 4–6,9,10; §3 type-foundation → Task 1; §4 silent-failure fixes → Tasks 2 (F1/F2/F5) + 3 (F3/F4); §5 components → Tasks 4 (trusted_domains),5 (web_extractor),6 (enrich_card),7 (status+migration),8 (config/breaker),9 (worker),10 (compose/UI); §6 migration → Task 7; §7 UI → Task 10; §10 testing → per task; §11 rollout → Task 11. ✅
- **Dropped (YAGNI):** the `String(20)→32` widen — `enrichment_status` values are ≤11 chars and the column is `String(20)`; the reviewer's 20-char concern was about `enrichment_source` (already `String(50)`). Noted, not implemented.
- **Type consistency:** `WebExtractResult`, `extract_part_from_web`, `is_trusted_domain`, `apply_web_sourced`, `select_batch`, `run_one_batch`, `EnrichmentWorkerStatus`, `MaterialEnrichmentStatus`, the `cooldown: dict[str,float]` param threaded through `fetch_authoritative`/`enrich_card` — all consistent across tasks. ✅
- **Concurrency invariant preserved:** the web extractor call in `enrich_card` is a pure `await` before attribute writes (Task 6) — no DB op added (matches the documented invariant).
