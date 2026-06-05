# No-Hallucination OEM/FRU Enrichment — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Resolve OEM/FRU/spare part numbers (Lenovo/IBM, HP/HPE, Dell, Acer, ASUS) with zero fabrication by adding a strict double-verified cross-reference tier and a single-source OEM-description tier to the enrichment pipeline, plus honest UI surfacing and a dry-run-first backfill.

**Architecture:** Two new Claude grounded-web-search tiers slot into `enrich_card` between the distributor web tier and the AI fallback, gated by a pure regex OEM-vendor classifier so non-OEM parts never incur OEM web calls. Cross-ref → `verified` only when the resolved MPN's linkage is sourced on an allowlisted page AND the resolved MPN independently clears the distributor pipeline. OEM description → new `oem_sourced` status from a single official OEM page. Unresolvable OEM parts → new terminal `not_catalogued`. All trust gates enforced in Python, never trusting LLM claims.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.0, PostgreSQL (varchar status column — no migration), Jinja2 + HTMX + Alpine.js + Tailwind, pytest (`-n auto`, in-memory SQLite, `TESTING=1`).

**Spec:** `docs/superpowers/specs/2026-06-05-no-hallucination-oem-enrichment-design.md`

**Worktree:** `/root/availai/.claude/worktrees/oem-enrichment` (branch `worktree-oem-enrichment`, base `origin/main` 116e3e6d). All commands assume this CWD and `TESTING=1 PYTHONPATH=$(pwd)`.

---

## File Structure

**Create**
- `app/services/enrichment_worker/oem_classifier.py` — pure regex vendor classifier
- `app/services/enrichment_worker/oem_domains.py` — OEM/cross-ref domain allowlists + predicates
- `app/services/enrichment_worker/oem_extractor.py` — `cross_reference_mpn` + `extract_oem_description`
- `scripts/backfill_oem_enrichment.py` — dry-run-first backfill over not_found/not_catalogued
- `tests/test_oem_classifier.py`, `tests/test_oem_domains.py`, `tests/test_oem_extractor.py`,
  `tests/test_backfill_oem_enrichment.py`

**Modify**
- `app/constants.py` — add `OEM_SOURCED`, `NOT_CATALOGUED`
- `app/models/intelligence.py` — update status comment (validator is data-driven, no logic change)
- `app/services/authoritative_enrichment_service.py` — OEM tiers, `web_meter`, 2 appliers, terminal
- `app/services/enrichment_worker/worker.py` — meter accounting, breaker reset, `not_catalogued` eligibility
- `app/services/enrichment_worker/config.py` — `not_catalogued_retry_days`
- `app/templates/htmx/partials/materials/list.html` — `oem_sourced` + `not_catalogued` badges
- `app/templates/htmx/partials/materials/workspace.html` — two filter toggles
- `app/static/htmx_app.js` — Alpine state + URL sync for two toggles
- `docs/APP_MAP_ARCHITECTURE.md`, `docs/APP_MAP_DATABASE.md`, `docs/APP_MAP_INTERACTIONS.md`

**Test (extend existing)**
- `tests/test_constants.py` / `tests/test_enrichment_status_enum.py`
- `tests/test_authoritative_enrichment.py`
- `tests/test_enrichment_worker.py`

**Dependency order:** Task 1 (constants) → Task 2 (classifier) ∥ Task 3 (domains) → Task 4 (extractor) → Task 5 (enrich_card) → Task 6 (worker) → Task 7 (badges) ∥ Task 8 (filter) → Task 9 (backfill) → Task 10 (docs). Tasks 2 & 3 are independent; 7 & 8 are independent.

---

## Task 1: Add `oem_sourced` + `not_catalogued` statuses

**Files:**
- Modify: `app/constants.py:452-463`
- Modify: `app/models/intelligence.py:54-56`
- Test: `tests/test_constants.py`, `tests/test_enrichment_status_enum.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_constants.py`:
```python
def test_material_enrichment_status_has_oem_tiers():
    from app.constants import MaterialEnrichmentStatus

    assert MaterialEnrichmentStatus.OEM_SOURCED == "oem_sourced"
    assert MaterialEnrichmentStatus.NOT_CATALOGUED == "not_catalogued"
    # All values fit the String(20) column.
    assert all(len(s.value) <= 20 for s in MaterialEnrichmentStatus)
```

Append to `tests/test_enrichment_status_enum.py`:
```python
def test_validator_accepts_oem_tiers():
    from app.constants import MaterialEnrichmentStatus
    from app.models import MaterialCard

    c = MaterialCard(display_mpn="01HW917", normalized_mpn="01hw917")
    c.enrichment_status = MaterialEnrichmentStatus.OEM_SOURCED
    assert c.enrichment_status == "oem_sourced"
    c.enrichment_status = "not_catalogued"
    assert c.enrichment_status == "not_catalogued"


def test_validator_still_rejects_junk():
    import pytest

    from app.models import MaterialCard

    c = MaterialCard(display_mpn="X", normalized_mpn="x")
    with pytest.raises(ValueError):
        c.enrichment_status = "bogus_status"
```

- [ ] **Step 2: Run — verify fail**

Run: `TESTING=1 PYTHONPATH=$(pwd) pytest tests/test_constants.py::test_material_enrichment_status_has_oem_tiers tests/test_enrichment_status_enum.py::test_validator_accepts_oem_tiers -v`
Expected: FAIL (`AttributeError: OEM_SOURCED`).

- [ ] **Step 3: Implement**

In `app/constants.py`, in `class MaterialEnrichmentStatus(StrEnum)`, update the docstring line `Single source of truth for the five valid enrichment tiers.` → `...the seven valid enrichment tiers.` and add after `NOT_FOUND = "not_found"`:
```python
    OEM_SOURCED = "oem_sourced"
    NOT_CATALOGUED = "not_catalogued"
```

In `app/models/intelligence.py`, update the comment block above the column (currently `# unenriched | verified | web_sourced | ai_inferred | not_found`) to:
```python
    # enrichment_status: see constants.MaterialEnrichmentStatus (validated on write):
    # unenriched | verified | web_sourced | oem_sourced | ai_inferred | not_found | not_catalogued
```

- [ ] **Step 4: Run — verify pass**

Run: `TESTING=1 PYTHONPATH=$(pwd) pytest tests/test_constants.py tests/test_enrichment_status_enum.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/constants.py app/models/intelligence.py tests/test_constants.py tests/test_enrichment_status_enum.py
git commit -m "feat(enrichment): add oem_sourced + not_catalogued status tiers"
```

---

## Task 2: OEM vendor classifier

**Files:**
- Create: `app/services/enrichment_worker/oem_classifier.py`
- Test: `tests/test_oem_classifier.py`

- [ ] **Step 1: Write failing test**

Create `tests/test_oem_classifier.py`:
```python
"""Truth-table tests for the OEM/FRU vendor classifier (real not_found samples)."""

import pytest

from app.services.enrichment_worker.oem_classifier import classify_oem_vendor


@pytest.mark.parametrize(
    "mpn,vendor",
    [
        ("01HW917", "lenovo"), ("00E2891", "lenovo"), ("01LV731", "lenovo"),
        ("00HW132", "lenovo"),
        ("38L7669", "lenovo"), ("46C9040", "lenovo"),
        ("5B20L64949", "lenovo"), ("5C10Q59981", "lenovo"), ("5T10Q96500", "lenovo"),
        ("918042-601", "hpe"), ("619559-001", "hpe"), ("486301-001", "hpe"),
        ("628668-001", "hpe"), ("902499-856", "hpe"),
        ("NB.MBC11.003", "acer"), ("KT.00403.025", "acer"), ("33.G55N7.002", "acer"),
        ("NB.GKH11.002", "acer"),
        ("60NB0690-MB1820", "asus"), ("0B200-00930000", "asus"),
        ("HV52W", "dell"), ("66YYK", "dell"),
    ],
)
def test_classifies_known_oem_codes(mpn, vendor):
    assert classify_oem_vendor(mpn) == vendor


@pytest.mark.parametrize(
    "mpn",
    [
        "M393A2K40EB3-CWEB/C",  # real Samsung DDR4 RDIMM
        "LM2596S", "ATMEGA328P-PU", "STM32F407VGT6",
        "", "  ", None, "AB",  # too short / empty
    ],
)
def test_rejects_non_oem(mpn):
    assert classify_oem_vendor(mpn) is None


def test_case_insensitive_and_stripped():
    assert classify_oem_vendor("  0b200-00930000  ") == "asus"
    assert classify_oem_vendor("nb.mbc11.003") == "acer"
```

- [ ] **Step 2: Run — verify fail**

Run: `TESTING=1 PYTHONPATH=$(pwd) pytest tests/test_oem_classifier.py -v`
Expected: FAIL (module not found).

- [ ] **Step 3: Implement**

Create `app/services/enrichment_worker/oem_classifier.py`:
```python
"""Classify an MPN as an OEM/system-vendor FRU/spare/service part number.

Pure, regex-based vendor detection used to gate the OEM enrichment tiers (cross-ref +
OEM official description) in ``enrich_card``. Returns the likely OEM vendor or ``None``.
The label is advisory only — it seeds the search prompt; correctness is enforced
downstream by the Python gates in ``oem_extractor`` / ``enrich_card``, never here.

Called by: app.services.authoritative_enrichment_service.enrich_card,
scripts.backfill_oem_enrichment. Depends on: stdlib ``re`` only.
"""

from __future__ import annotations

import re

# Ordered (priority) (vendor, pattern). First match wins. Anchored, matched against the
# UPPERCASED stripped display_mpn. Each pattern is justified by a real not_found sample
# (see spec §1).
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # Lenovo modern FRU/option: 5x + 2 digits + letter + 5 digits (5B20L64949, 5T10Q96500)
    ("lenovo", re.compile(r"^5[A-Z]\d{2}[A-Z]\d{5}$")),
    # Lenovo/IBM classic FRU: 2 digits + letter + 4 alnum (38L7669, 46C9040)
    ("lenovo", re.compile(r"^\d{2}[A-Z][A-Z0-9]{4}$")),
    # Lenovo/IBM 7-char FRU: 00/01 + 5 alnum (01HW917, 00E2891, 01LV731)
    ("lenovo", re.compile(r"^0[01][A-Z0-9]{5}$")),
    # Acer dotted part code: 2 alnum . 5 alnum . 3 alnum (NB.MBC11.003, 33.G55N7.002)
    ("acer", re.compile(r"^[A-Z0-9]{2}\.[A-Z0-9]{5}\.[A-Z0-9]{3}$")),
    # ASUS module code: 2 digits NB + 4 alnum - tail (60NB0690-MB1820)
    ("asus", re.compile(r"^\d{2}NB[A-Z0-9]{4}-[A-Z0-9]+$")),
    # ASUS 0X###-######## (0B200-00930000)
    ("asus", re.compile(r"^0[A-Z]\d{3}-\d{8}$")),
    # HP/HPE spare: 6 digits - 3 digits (918042-601, 619559-001)
    ("hpe", re.compile(r"^\d{6}-\d{3}$")),
    # Dell 5-char spare with >=1 letter (HV52W, 66YYK). Broad/low-priority; a false
    # positive costs only a wasted web call (genuine MPNs resolve at earlier tiers first).
    ("dell", re.compile(r"^(?=[A-Z0-9]{5}$)[A-Z0-9]*[A-Z][A-Z0-9]*$")),
]


def classify_oem_vendor(display_mpn: str | None) -> str | None:
    """Return the likely OEM vendor for an OEM/FRU/spare code, or ``None``.

    Never raises on empty/malformed input (returns ``None``). The vendor label only seeds
    the cross-ref / description search prompt; the Python trust gates downstream enforce
    correctness.
    """
    if not isinstance(display_mpn, str):
        return None
    mpn = display_mpn.strip().upper()
    if not mpn:
        return None
    for vendor, pat in _PATTERNS:
        if pat.match(mpn):
            return vendor
    return None
```

- [ ] **Step 4: Run — verify pass**

Run: `TESTING=1 PYTHONPATH=$(pwd) pytest tests/test_oem_classifier.py -v`
Expected: PASS (all params). If `STM32F407VGT6` or similar unexpectedly matches, tighten the offending pattern — but with the anchors above it should not.

- [ ] **Step 5: Commit**

```bash
git add app/services/enrichment_worker/oem_classifier.py tests/test_oem_classifier.py
git commit -m "feat(enrichment): OEM/FRU vendor classifier (gates OEM tiers)"
```

---

## Task 3: OEM domain allowlists

**Files:**
- Create: `app/services/enrichment_worker/oem_domains.py`
- Test: `tests/test_oem_domains.py`

- [ ] **Step 1: Write failing test**

Create `tests/test_oem_domains.py`:
```python
from app.services.enrichment_worker.oem_domains import is_crossref_domain, is_oem_domain


def test_official_oem_hosts_accepted():
    assert is_oem_domain("https://support.lenovo.com/parts/01HW917")
    assert is_oem_domain("https://partsurfer.hpe.com/Search.aspx?SearchText=918042-601")
    assert is_oem_domain("http://www.dell.com/support/HV52W")
    assert is_oem_domain("https://parts.hp.com/x")  # dot-suffix of hp.com root


def test_lookalike_and_bad_schemes_rejected():
    assert not is_oem_domain("https://evil-lenovo.com/x")
    assert not is_oem_domain("https://lenovo.com.evil.com/x")
    assert not is_oem_domain("ftp://support.lenovo.com/x")
    assert not is_oem_domain("not a url")
    assert not is_oem_domain("")


def test_crossref_superset_includes_distributors_and_oem():
    # OEM official
    assert is_crossref_domain("https://support.lenovo.com/x")
    # distributor (from trusted_domains)
    assert is_crossref_domain("https://www.mouser.com/ProductDetail/x")
    # manufacturer (from trusted_domains)
    assert is_crossref_domain("https://www.ti.com/product/x")
    # junk
    assert not is_crossref_domain("https://reddit.com/r/x")
```

- [ ] **Step 2: Run — verify fail**

Run: `TESTING=1 PYTHONPATH=$(pwd) pytest tests/test_oem_domains.py -v`
Expected: FAIL (module not found).

- [ ] **Step 3: Implement**

Create `app/services/enrichment_worker/oem_domains.py`:
```python
"""Security allowlist for OEM-sourced enrichment.

``is_oem_domain``: official system-vendor parts/support hosts (Lenovo, HPE, HP, Dell,
Acer, ASUS, IBM). A page on one of these may produce an ``oem_sourced`` description.

``is_crossref_domain``: the OEM-official set UNION the existing distributor + manufacturer
allowlist — a distributor/manufacturer page that lists an OEM FRU next to the commodity
MPN is acceptable evidence for the *linkage* (the resolved MPN is independently
re-verified against distributors regardless). Validated in code; the LLM's domain claims
are never trusted.

Called by: app.services.enrichment_worker.oem_extractor.
Depends on: app.services.enrichment_worker.trusted_domains.
"""

from __future__ import annotations

from urllib.parse import urlparse

from .trusted_domains import is_trusted_domain

# Exact-host official OEM/system-vendor parts & support domains.
OEM_OFFICIAL_HOSTS: frozenset[str] = frozenset(
    {
        "support.lenovo.com",
        "pcsupport.lenovo.com",
        "partsurfer.hpe.com",
        "partsurfer.com",
        "support.hpe.com",
        "support.hp.com",
        "parts.hp.com",
        "www.dell.com",
        "dell.com",
        "www.acer.com",
        "us.acer.com",
        "www.asus.com",
    }
)

# Vendor root domains matched by dot-suffix (foo.lenovo.com matches lenovo.com).
OEM_VENDOR_ROOTS: frozenset[str] = frozenset(
    {"lenovo.com", "hpe.com", "hp.com", "dell.com", "acer.com", "asus.com", "ibm.com"}
)


def is_oem_domain(url: str) -> bool:
    """Return True if *url* is an official OEM/system-vendor parts/support domain."""
    try:
        p = urlparse(url)
    except Exception:
        return False
    if p.scheme not in ("http", "https") or not p.hostname:
        return False
    host = p.hostname.lower()
    if host in OEM_OFFICIAL_HOSTS:
        return True
    return any(host == r or host.endswith("." + r) for r in OEM_VENDOR_ROOTS)


def is_crossref_domain(url: str) -> bool:
    """Return True if *url* is authoritative for asserting a FRU<->MPN linkage.

    OEM-official set plus the existing distributor / manufacturer allowlist.
    """
    return is_oem_domain(url) or is_trusted_domain(url)
```

- [ ] **Step 4: Run — verify pass**

Run: `TESTING=1 PYTHONPATH=$(pwd) pytest tests/test_oem_domains.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/enrichment_worker/oem_domains.py tests/test_oem_domains.py
git commit -m "feat(enrichment): OEM + cross-ref domain allowlists"
```

---

## Task 4: OEM extractor (cross-ref + description), all gates in Python

**Files:**
- Create: `app/services/enrichment_worker/oem_extractor.py`
- Test: `tests/test_oem_extractor.py`

- [ ] **Step 1: Write failing test**

Create `tests/test_oem_extractor.py`:
```python
"""Gate tests for OEM cross-ref + description extractors (mocked Claude)."""

from unittest.mock import AsyncMock, patch

import pytest

from app.services.enrichment_worker import oem_extractor
from app.services.enrichment_worker.oem_extractor import (
    cross_reference_mpn,
    extract_oem_description,
)
from app.utils.claude_errors import ClaudeError

XR_OK = {
    "resolved_mpn": "M393A2K40EB3-CWE",
    "manufacturer": "Samsung",
    "linkage_quote": "Lenovo FRU 01HW917 = Samsung M393A2K40EB3-CWE 16GB DDR4 RDIMM",
    "confidence": 0.95,
    "source_urls": ["https://support.lenovo.com/parts/01HW917"],
}


async def _xr(data):
    with patch.object(oem_extractor, "claude_json", new=AsyncMock(return_value=data)):
        return await cross_reference_mpn("01HW917", "01hw917", "lenovo")


@pytest.mark.asyncio
async def test_crossref_accept():
    r = await _xr(dict(XR_OK))
    assert r.status == "resolved"
    assert r.resolved_mpn == "M393A2K40EB3-CWE"
    assert r.linkage_source_domain == "support.lenovo.com"


@pytest.mark.asyncio
async def test_crossref_reject_untrusted_domain():
    r = await _xr({**XR_OK, "source_urls": ["https://reddit.com/r/homelab"]})
    assert r.status == "failed"


@pytest.mark.asyncio
async def test_crossref_reject_linkage_missing_resolved():
    # quote lacks the resolved MPN → linkage not sourced
    r = await _xr({**XR_OK, "linkage_quote": "Lenovo FRU 01HW917 memory module"})
    assert r.status == "failed"


@pytest.mark.asyncio
async def test_crossref_reject_linkage_missing_oem_code():
    r = await _xr({**XR_OK, "linkage_quote": "Samsung M393A2K40EB3-CWE 16GB DDR4 RDIMM"})
    assert r.status == "failed"


@pytest.mark.asyncio
async def test_crossref_reject_echo_mpn():
    r = await _xr(
        {
            **XR_OK,
            "resolved_mpn": "01HW917",
            "linkage_quote": "01HW917 01HW917",
        }
    )
    assert r.status == "failed"


@pytest.mark.asyncio
async def test_crossref_reject_low_confidence():
    r = await _xr({**XR_OK, "confidence": 0.5})
    assert r.status == "failed"


@pytest.mark.asyncio
async def test_crossref_claude_error_propagates():
    with patch.object(oem_extractor, "claude_json", new=AsyncMock(side_effect=ClaudeError("down"))):
        with pytest.raises(ClaudeError):
            await cross_reference_mpn("01HW917", "01hw917", "lenovo")


DESC_OK = {
    "description": "ThinkSystem 16GB TruDDR4 2666MHz RDIMM",
    "manufacturer": "Lenovo",
    "category": "Memory Module",
    "datasheet_url": None,
    "confidence": 0.95,
    "exact_mpn_found": "01HW917",
    "source_urls": ["https://support.lenovo.com/parts/01HW917"],
}


async def _desc(data):
    with patch.object(oem_extractor, "claude_json", new=AsyncMock(return_value=data)):
        return await extract_oem_description("01HW917", "01hw917", "lenovo")


@pytest.mark.asyncio
async def test_desc_accept():
    r = await _desc(dict(DESC_OK))
    assert r.status == "oem_sourced"
    assert r.description.startswith("ThinkSystem")
    assert r.source_domains == ["support.lenovo.com"]


@pytest.mark.asyncio
async def test_desc_reject_untrusted_domain():
    r = await _desc({**DESC_OK, "source_urls": ["https://www.ebay.com/itm/123"]})
    assert r.status == "failed"


@pytest.mark.asyncio
async def test_desc_reject_mpn_mismatch():
    r = await _desc({**DESC_OK, "exact_mpn_found": "01HW918"})
    assert r.status == "failed"


@pytest.mark.asyncio
async def test_desc_reject_short_description():
    r = await _desc({**DESC_OK, "description": "RAM"})
    assert r.status == "failed"


@pytest.mark.asyncio
async def test_desc_claude_error_propagates():
    with patch.object(oem_extractor, "claude_json", new=AsyncMock(side_effect=ClaudeError("down"))):
        with pytest.raises(ClaudeError):
            await extract_oem_description("01HW917", "01hw917", "lenovo")
```

- [ ] **Step 2: Run — verify fail**

Run: `TESTING=1 PYTHONPATH=$(pwd) pytest tests/test_oem_extractor.py -v`
Expected: FAIL (module not found).

- [ ] **Step 3: Implement**

Create `app/services/enrichment_worker/oem_extractor.py`:
```python
"""Grounded OEM enrichment: Claude reads authoritative OEM / cross-reference pages and
either (a) resolves an OEM/FRU code to the commodity MPN it relabels, or (b) extracts an
OEM-official description. All trust gates enforced in Python — the model's gate claims are
never trusted.

Called by: app.services.authoritative_enrichment_service.enrich_card.
Depends on: app.utils.claude_client, app.utils.normalization, .oem_domains.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlparse

from loguru import logger

from app.utils.claude_client import claude_json
from app.utils.claude_errors import ClaudeError
from app.utils.normalization import normalize_mpn_key

from .oem_domains import is_crossref_domain, is_oem_domain

_MIN_CROSSREF_CONFIDENCE = 0.90
_MIN_OEM_CONFIDENCE = 0.90


# --------------------------------- cross-reference ---------------------------------


@dataclass
class CrossRefResult:
    """Candidate OEM->commodity-MPN cross-reference (not yet distributor-confirmed)."""

    status: str  # "resolved" | "failed"
    resolved_mpn: str | None = None
    manufacturer: str | None = None
    linkage_source_url: str | None = None
    linkage_source_domain: str | None = None
    confidence: float = 0.0


_XR_FAILED = CrossRefResult(status="failed")

_XR_SYSTEM = (
    "You are an electronics cross-reference assistant. An OEM/system-vendor FRU, spare, or "
    "service part number relabels a commodity component. Use web search to find an "
    "AUTHORITATIVE page (the OEM's own site, or an authorized distributor/manufacturer page) "
    "that shows BOTH the OEM code AND the underlying manufacturer part number together. "
    "Return ONLY valid JSON. Never invent a part number; use null when unknown."
)
_XR_PROMPT = (
    "OEM/FRU part number: {mpn} (vendor: {vendor}). Find the commodity manufacturer part "
    'number it corresponds to. Return JSON: {{"resolved_mpn": str|null, "manufacturer": '
    'str|null, "linkage_quote": str, "confidence": float, "source_urls": [str]}}. '
    "linkage_quote must be the verbatim text from the page that shows the OEM code and the "
    "resolved_mpn together."
)


async def cross_reference_mpn(
    display_mpn: str,
    normalized_mpn: str,
    vendor: str,
    *,
    timeout: int = 90,
) -> CrossRefResult:
    """Resolve an OEM/FRU code to a CANDIDATE commodity MPN via grounded web search.

    Four Python gates: (1) >=1 source URL on a cross-ref allowlist domain; (2) both the OEM
    code and the resolved MPN appear verbatim (normalized) in the sourced ``linkage_quote``
    — this is what makes a real-but-wrong guess detectable; (3) resolved != original (no
    echo); (4) confidence threshold. Returns the candidate only — the caller independently
    re-verifies the MPN against distributors. Raises ClaudeError on backend failure.
    """
    if not normalized_mpn:
        return _XR_FAILED
    try:
        data = await claude_json(
            _XR_PROMPT.format(mpn=display_mpn, vendor=vendor),
            system=_XR_SYSTEM,
            model_tier="smart",
            max_tokens=1000,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 4}],
            timeout=timeout,
        )
    except ClaudeError:
        raise
    except Exception as e:
        logger.warning("OEM_XREF: unexpected error for {}: {}", display_mpn, type(e).__name__)
        return _XR_FAILED

    if not isinstance(data, dict):
        return _XR_FAILED

    urls = [u for u in (data.get("source_urls") or []) if isinstance(u, str) and is_crossref_domain(u)]
    if not urls:
        logger.info("OEM_XREF: {} rejected — no trusted source ({})", display_mpn, data.get("source_urls"))
        return _XR_FAILED

    resolved_raw = (data.get("resolved_mpn") or "").strip()
    resolved_key = normalize_mpn_key(resolved_raw)
    if not resolved_key:
        return _XR_FAILED

    linkage_key = normalize_mpn_key(data.get("linkage_quote"))
    if normalized_mpn not in linkage_key or resolved_key not in linkage_key:
        logger.info("OEM_XREF: {} rejected — linkage quote missing a code", display_mpn)
        return _XR_FAILED

    if resolved_key == normalized_mpn:
        logger.info("OEM_XREF: {} rejected — resolved == original", display_mpn)
        return _XR_FAILED

    try:
        conf = float(data.get("confidence") or 0.0)
    except (TypeError, ValueError):
        conf = 0.0
    if conf < _MIN_CROSSREF_CONFIDENCE:
        logger.info("OEM_XREF: {} rejected — confidence {:.2f} < {:.2f}", display_mpn, conf, _MIN_CROSSREF_CONFIDENCE)
        return _XR_FAILED

    return CrossRefResult(
        status="resolved",
        resolved_mpn=resolved_raw,
        manufacturer=(data.get("manufacturer") or "").strip() or None,
        linkage_source_url=urls[0],
        linkage_source_domain=urlparse(urls[0]).hostname or "",
        confidence=conf,
    )


# --------------------------------- OEM description ---------------------------------


@dataclass
class OemExtractResult:
    """Result of an OEM-official description extraction (description/category only)."""

    status: str  # "oem_sourced" | "failed"
    description: str | None = None
    manufacturer: str | None = None
    category: str | None = None
    datasheet_url: str | None = None
    confidence: float = 0.0
    source_urls: list[str] = field(default_factory=list)
    source_domains: list[str] = field(default_factory=list)


_OEM_FAILED = OemExtractResult(status="failed")

_OEM_SYSTEM = (
    "You are an electronic-component data extraction assistant. Use web search to find the "
    "OFFICIAL OEM/system-vendor page (Lenovo, HPE, HP, Dell, Acer, ASUS, IBM) for the given "
    "OEM/FRU/spare part number. Return ONLY valid JSON. Never invent data; use null when unknown."
)
_OEM_PROMPT = (
    "Find the OEM/FRU part number {mpn} (vendor: {vendor}) on the vendor's official parts or "
    'support page. Return JSON: {{"description": str, "manufacturer": str, "category": str, '
    '"datasheet_url": str|null, "confidence": float, "exact_mpn_found": str, "source_urls": '
    '[str]}}. exact_mpn_found must be the OEM code exactly as printed on the page.'
)


async def extract_oem_description(
    display_mpn: str,
    normalized_mpn: str,
    vendor: str,
    *,
    timeout: int = 90,
) -> OemExtractResult:
    """Extract an OEM-official description/category from the vendor's own page.

    Four Python gates (official OEM domain, exact code verbatim, confidence, non-trivial
    description + manufacturer). Writes description/category/datasheet only — never
    structured specs. Raises ClaudeError on backend failure.
    """
    if not normalized_mpn:
        return _OEM_FAILED
    try:
        data = await claude_json(
            _OEM_PROMPT.format(mpn=display_mpn, vendor=vendor),
            system=_OEM_SYSTEM,
            model_tier="smart",
            max_tokens=1000,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 4}],
            timeout=timeout,
        )
    except ClaudeError:
        raise
    except Exception as e:
        logger.warning("OEM_DESC: unexpected error for {}: {}", display_mpn, type(e).__name__)
        return _OEM_FAILED

    if not isinstance(data, dict):
        return _OEM_FAILED

    urls = [u for u in (data.get("source_urls") or []) if isinstance(u, str) and is_oem_domain(u)]
    if not urls:
        logger.info("OEM_DESC: {} rejected — no official OEM source ({})", display_mpn, data.get("source_urls"))
        return _OEM_FAILED

    if normalize_mpn_key(data.get("exact_mpn_found")) != normalized_mpn:
        logger.info("OEM_DESC: {} rejected — MPN mismatch (got {})", display_mpn, data.get("exact_mpn_found"))
        return _OEM_FAILED

    try:
        conf = float(data.get("confidence") or 0.0)
    except (TypeError, ValueError):
        conf = 0.0
    if conf < _MIN_OEM_CONFIDENCE:
        logger.info("OEM_DESC: {} rejected — confidence {:.2f} < {:.2f}", display_mpn, conf, _MIN_OEM_CONFIDENCE)
        return _OEM_FAILED

    desc = (data.get("description") or "").strip()
    mfr = (data.get("manufacturer") or "").strip()
    if len(desc) < 10 or not mfr:
        logger.info("OEM_DESC: {} rejected — description too short ({}) or missing manufacturer", display_mpn, len(desc))
        return _OEM_FAILED

    return OemExtractResult(
        status="oem_sourced",
        description=desc,
        manufacturer=mfr,
        category=(data.get("category") or "").strip() or None,
        datasheet_url=(data.get("datasheet_url") or "").strip() or None,
        confidence=conf,
        source_urls=urls,
        source_domains=sorted({urlparse(u).hostname or "" for u in urls}),
    )
```

- [ ] **Step 4: Run — verify pass**

Run: `TESTING=1 PYTHONPATH=$(pwd) pytest tests/test_oem_extractor.py -v`
Expected: PASS (all gate cases).

- [ ] **Step 5: Commit**

```bash
git add app/services/enrichment_worker/oem_extractor.py tests/test_oem_extractor.py
git commit -m "feat(enrichment): OEM cross-ref + description extractors (Python-gated)"
```

---

## Task 5: Wire OEM tiers into `enrich_card` + web metering

**Files:**
- Modify: `app/services/authoritative_enrichment_service.py` (imports; `enrich_card`; 2 new appliers)
- Test: `tests/test_authoritative_enrichment.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_authoritative_enrichment.py`:
```python
from unittest.mock import AsyncMock, patch

import pytest

from app.constants import MaterialEnrichmentStatus
from app.models import MaterialCard
from app.services import authoritative_enrichment_service as aes
from app.services.enrichment_worker.oem_extractor import CrossRefResult, OemExtractResult


def _card(mpn="01HW917"):
    return MaterialCard(display_mpn=mpn, normalized_mpn=mpn.lower().replace("-", ""))


@pytest.mark.asyncio
async def test_crossref_double_verify_to_verified(db_session):
    card = _card()
    xr = CrossRefResult(
        status="resolved", resolved_mpn="M393A2K40EB3-CWE", manufacturer="Samsung",
        linkage_source_url="https://support.lenovo.com/x", linkage_source_domain="support.lenovo.com",
        confidence=0.95,
    )
    # No distributor hit for the FRU; distributor DOES confirm the resolved MPN.
    async def fake_fetch(display, norm, conns, disabled, cooldown):
        if norm == "m393a2k40eb3cwe":
            return {"mouser": [{"mpn_matched": "M393A2K40EB3-CWE", "description": "16GB DDR4 RDIMM", "manufacturer": "Samsung"}]}
        return {}

    meter = {"web_calls": 0, "claude_ok": False}
    with patch.object(aes, "classify_oem_vendor", return_value="lenovo"), \
         patch.object(aes, "extract_part_from_web", new=AsyncMock(return_value=type("W", (), {"status": "failed"})())), \
         patch.object(aes, "cross_reference_mpn", new=AsyncMock(return_value=xr)), \
         patch.object(aes, "fetch_authoritative", new=AsyncMock(side_effect=fake_fetch)):
        status = await aes.enrich_card(card, db_session, connectors=[], web_meter=meter)

    assert status == MaterialEnrichmentStatus.VERIFIED
    assert card.description == "16GB DDR4 RDIMM"
    assert any(x.get("mpn") == "M393A2K40EB3-CWE" for x in (card.cross_references or []))
    assert card.enrichment_provenance["cross_ref"]["resolved_mpn"] == "M393A2K40EB3-CWE"
    assert meter["claude_ok"] is True and meter["web_calls"] >= 2


@pytest.mark.asyncio
async def test_crossref_unconfirmed_mpn_falls_through(db_session):
    card = _card()
    xr = CrossRefResult(status="resolved", resolved_mpn="BOGUS-NOPART", confidence=0.95,
                        linkage_source_domain="support.lenovo.com")
    with patch.object(aes, "classify_oem_vendor", return_value="lenovo"), \
         patch.object(aes, "extract_part_from_web", new=AsyncMock(return_value=type("W", (), {"status": "failed"})())), \
         patch.object(aes, "cross_reference_mpn", new=AsyncMock(return_value=xr)), \
         patch.object(aes, "fetch_authoritative", new=AsyncMock(return_value={})), \
         patch.object(aes, "extract_oem_description", new=AsyncMock(return_value=OemExtractResult(status="failed"))), \
         patch("app.services.ai_inference_fallback.infer_part",
               new=AsyncMock(return_value=type("I", (), {"status": "not_found"})())):
        status = await aes.enrich_card(card, db_session, connectors=[], web_meter={"web_calls": 0, "claude_ok": False})
    # Unconfirmed cross-ref discarded; OEM desc failed; AI declined → not_catalogued (OEM pattern matched).
    assert status == MaterialEnrichmentStatus.NOT_CATALOGUED
    assert card.cross_references in (None, [])


@pytest.mark.asyncio
async def test_oem_description_path(db_session):
    card = _card()
    oem = OemExtractResult(status="oem_sourced", description="ThinkSystem 16GB RDIMM",
                           manufacturer="Lenovo", category="Memory Module",
                           confidence=0.95, source_urls=["https://support.lenovo.com/x"],
                           source_domains=["support.lenovo.com"])
    with patch.object(aes, "classify_oem_vendor", return_value="lenovo"), \
         patch.object(aes, "extract_part_from_web", new=AsyncMock(return_value=type("W", (), {"status": "failed"})())), \
         patch.object(aes, "cross_reference_mpn", new=AsyncMock(return_value=CrossRefResult(status="failed"))), \
         patch.object(aes, "extract_oem_description", new=AsyncMock(return_value=oem)):
        status = await aes.enrich_card(card, db_session, connectors=[], web_meter={"web_calls": 0, "claude_ok": False})
    assert status == MaterialEnrichmentStatus.OEM_SOURCED
    assert card.description == "ThinkSystem 16GB RDIMM"
    assert card.enrichment_provenance["oem_sourced"] is True


@pytest.mark.asyncio
async def test_non_oem_failure_stays_not_found(db_session):
    card = _card("LM2596S")
    with patch.object(aes, "classify_oem_vendor", return_value=None), \
         patch.object(aes, "extract_part_from_web", new=AsyncMock(return_value=type("W", (), {"status": "failed"})())), \
         patch.object(aes, "fetch_authoritative", new=AsyncMock(return_value={})), \
         patch("app.services.ai_inference_fallback.infer_part",
               new=AsyncMock(return_value=type("I", (), {"status": "not_found"})())):
        status = await aes.enrich_card(card, db_session, connectors=[], web_meter={"web_calls": 0, "claude_ok": False})
    assert status == MaterialEnrichmentStatus.NOT_FOUND


@pytest.mark.asyncio
async def test_oem_tiers_skipped_when_web_disabled(db_session):
    card = _card()
    xref = AsyncMock()
    with patch.object(aes, "classify_oem_vendor", return_value="lenovo"), \
         patch.object(aes, "fetch_authoritative", new=AsyncMock(return_value={})), \
         patch.object(aes, "cross_reference_mpn", new=xref), \
         patch("app.services.ai_inference_fallback.infer_part",
               new=AsyncMock(return_value=type("I", (), {"status": "not_found"})())):
        status = await aes.enrich_card(card, db_session, connectors=[], disabled={"web_search"},
                                       web_meter={"web_calls": 0, "claude_ok": False})
    xref.assert_not_called()  # OEM tiers gated by web budget
    assert status == MaterialEnrichmentStatus.NOT_FOUND  # not_catalogued requires an actual attempt
```

> Note: `db_session` is the existing session fixture in `tests/conftest.py` (used by other `test_authoritative_enrichment.py` tests). If those tests use a different fixture name, match it.

- [ ] **Step 2: Run — verify fail**

Run: `TESTING=1 PYTHONPATH=$(pwd) pytest tests/test_authoritative_enrichment.py -k "crossref or oem_description or non_oem or web_disabled" -v`
Expected: FAIL (`enrich_card() got unexpected keyword 'web_meter'`).

- [ ] **Step 3: Implement**

In `app/services/authoritative_enrichment_service.py`, add imports after the existing `from app.services.enrichment_worker.web_extractor import ...` line:
```python
from app.services.enrichment_worker.oem_classifier import classify_oem_vendor
from app.services.enrichment_worker.oem_extractor import (
    CrossRefResult,
    OemExtractResult,
    cross_reference_mpn,
    extract_oem_description,
)
```

Add two appliers after `apply_web_sourced`:
```python
def apply_cross_ref_verified(
    card: MaterialCard,
    merged: dict,
    provenance: dict,
    contributors: list[str],
    xr: CrossRefResult,
) -> None:
    """Write the resolved commodity MPN's distributor data onto an OEM/FRU card.

    Status becomes ``verified`` (the resolved MPN was independently confirmed against a
    distributor). Records the FRU<->MPN linkage in ``cross_references`` and a top-level
    ``cross_ref`` provenance block so the whole chain is auditable.
    """
    now = datetime.now(timezone.utc)
    for field_name, value in merged.items():
        setattr(card, field_name, value)
    xrefs = list(card.cross_references or [])
    xrefs.append({"mpn": xr.resolved_mpn, "manufacturer": xr.manufacturer})
    card.cross_references = xrefs
    prov = dict(provenance)
    prov["cross_ref"] = {
        "oem_part": card.display_mpn,
        "resolved_mpn": xr.resolved_mpn,
        "linkage_source_url": xr.linkage_source_url,
        "linkage_source_domain": xr.linkage_source_domain,
        "confirmed_by": contributors[0] if contributors else None,
        "confidence": xr.confidence,
    }
    card.enrichment_provenance = prov
    card.enrichment_source = contributors[0] if contributors else "cross_ref"
    card.enrichment_status = MaterialEnrichmentStatus.VERIFIED
    card.enriched_at = now


def apply_oem_sourced(card: MaterialCard, result: OemExtractResult) -> None:
    """Write OEM-official description/category onto the card (status ``oem_sourced``).

    Description + category + datasheet only — never structured specs.
    """
    now = datetime.now(timezone.utc)
    iso = now.isoformat()
    prov: dict = {
        "oem_sourced": True,
        "confidence": result.confidence,
        "source_urls": result.source_urls,
        "source_domains": result.source_domains,
        "fetched_at": iso,
    }
    fields = {
        "description": result.description,
        "category": result.category,
        "datasheet_url": result.datasheet_url,
        "manufacturer": result.manufacturer,
    }
    for f, v in fields.items():
        if v:
            setattr(card, f, v)
            prov[f] = {"source": "oem_official", "confidence": result.confidence, "fetched_at": iso}
    card.enrichment_source = "oem_official"
    card.enrichment_status = MaterialEnrichmentStatus.OEM_SOURCED
    card.enrichment_provenance = prov
    card.enriched_at = now
```

Replace the body of `enrich_card` from the `web_enabled`/web-tier section through the terminal `return MaterialEnrichmentStatus.NOT_FOUND`. The new signature adds `web_meter`; update the docstring's CONCURRENCY INVARIANT note to add: "the cross-ref re-verification (`fetch_authoritative` on the resolved MPN) is pure async connector I/O — no DB query/flush — so the invariant holds." Body:

```python
async def enrich_card(
    card: MaterialCard,
    db: Session,
    *,
    connectors: list | None = None,
    refresh: bool = False,
    disabled: set[str] | None = None,
    cooldown: dict[str, float] | None = None,
    web_meter: dict | None = None,
) -> str:
    """...(keep existing docstring; append:)

    ``web_meter`` (optional ``{"web_calls": int, "claude_ok": bool}``) is updated in place:
    ``web_calls`` counts each billable web-search call made (distributor web, cross-ref, OEM
    description); ``claude_ok`` is set True after ANY Claude call (incl. infer_part) returns
    without raising. The worker uses ``web_calls`` for the daily budget and ``claude_ok`` to
    reset its circuit breaker. Default None = no metering.
    """
    if card.enrichment_status == MaterialEnrichmentStatus.VERIFIED and not refresh:
        return MaterialEnrichmentStatus.VERIFIED

    conns = connectors if connectors is not None else _connectors_in_order(db)
    results = await fetch_authoritative(card.display_mpn, card.normalized_mpn, conns, disabled, cooldown)
    merged, provenance, contributors = merge_authoritative(card.normalized_mpn, results)

    if merged:
        apply_authoritative(card, merged, provenance, contributors)
        return MaterialEnrichmentStatus.VERIFIED

    web_enabled = not (disabled and "web_search" in disabled)

    # Distributor / manufacturer web tier.
    if web_enabled:
        web = await extract_part_from_web(card.display_mpn, card.normalized_mpn)
        if web_meter is not None:
            web_meter["web_calls"] = web_meter.get("web_calls", 0) + 1
            web_meter["claude_ok"] = True
        if web.status == "web_sourced":
            apply_web_sourced(card, web)
            return MaterialEnrichmentStatus.WEB_SOURCED

    # OEM tiers — only for recognised OEM/FRU codes, only when the web budget is live.
    vendor = classify_oem_vendor(card.display_mpn)
    oem_attempted = False
    if vendor and web_enabled:
        oem_attempted = True
        # Tier 3: cross-reference, then INDEPENDENTLY re-verify against distributors.
        xr = await cross_reference_mpn(card.display_mpn, card.normalized_mpn, vendor)
        if web_meter is not None:
            web_meter["web_calls"] = web_meter.get("web_calls", 0) + 1
            web_meter["claude_ok"] = True
        if xr.status == "resolved" and xr.resolved_mpn:
            resolved_key = normalize_mpn_key(xr.resolved_mpn)
            xr_results = await fetch_authoritative(xr.resolved_mpn, resolved_key, conns, disabled, cooldown)
            xr_merged, xr_prov, xr_contrib = merge_authoritative(resolved_key, xr_results)
            if xr_merged:
                apply_cross_ref_verified(card, xr_merged, xr_prov, xr_contrib, xr)
                return MaterialEnrichmentStatus.VERIFIED
        # Tier 4: OEM-official description (single authoritative page).
        oem = await extract_oem_description(card.display_mpn, card.normalized_mpn, vendor)
        if web_meter is not None:
            web_meter["web_calls"] = web_meter.get("web_calls", 0) + 1
            web_meter["claude_ok"] = True
        if oem.status == "oem_sourced":
            apply_oem_sourced(card, oem)
            return MaterialEnrichmentStatus.OEM_SOURCED

    # No authoritative hit -> flagged inference.
    from app.services.ai_inference_fallback import infer_part

    inf = await infer_part(card.display_mpn)
    if web_meter is not None:
        web_meter["claude_ok"] = True
    now = datetime.now(timezone.utc)
    card.enriched_at = now
    if inf.status == "ai_inferred":
        card.description = inf.description
        card.category = inf.category
        card.enrichment_source = "claude_opus_inferred"
        card.enrichment_status = MaterialEnrichmentStatus.AI_INFERRED
        card.enrichment_provenance = {
            "reconfirm_needed": True,
            "description": {
                "source": "claude_opus_inferred",
                "confidence": inf.confidence,
                "fetched_at": now.isoformat(),
            },
        }
        return MaterialEnrichmentStatus.AI_INFERRED

    # Terminal: not_catalogued only when an OEM pattern matched AND the OEM tiers ran.
    card.enrichment_status = (
        MaterialEnrichmentStatus.NOT_CATALOGUED
        if (vendor and oem_attempted)
        else MaterialEnrichmentStatus.NOT_FOUND
    )
    card.enrichment_source = None
    card.enrichment_provenance = None
    return card.enrichment_status
```

> `normalize_mpn_key` is already imported at the top of this module. `extract_part_from_web` is already imported. The `import normalize_mpn_key` line is `from app.utils.normalization import normalize_mpn_key` (already present).

- [ ] **Step 4: Run — verify pass**

Run: `TESTING=1 PYTHONPATH=$(pwd) pytest tests/test_authoritative_enrichment.py -v`
Expected: PASS (new + existing tests).

- [ ] **Step 5: Commit**

```bash
git add app/services/authoritative_enrichment_service.py tests/test_authoritative_enrichment.py
git commit -m "feat(enrichment): OEM cross-ref + description tiers in enrich_card with web metering"
```

---

## Task 6: Worker — metered budget, breaker reset, `not_catalogued` retry

**Files:**
- Modify: `app/services/enrichment_worker/config.py:21-43`
- Modify: `app/services/enrichment_worker/worker.py` (`select_batch`, `run_one_batch`)
- Test: `tests/test_enrichment_worker.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_enrichment_worker.py`:
```python
from datetime import datetime, timedelta, timezone

from app.constants import MaterialEnrichmentStatus
from app.services.enrichment_worker.config import EnrichmentWorkerConfig


def test_config_has_not_catalogued_retry_days():
    c = EnrichmentWorkerConfig()
    assert c.not_catalogued_retry_days == 30


def test_select_batch_not_catalogued_eligibility(db_session):
    from app.models import MaterialCard
    from app.services.enrichment_worker.worker import select_batch

    now = datetime.now(timezone.utc)
    cfg = EnrichmentWorkerConfig(batch_size=10, not_catalogued_retry_days=30)
    fresh = MaterialCard(display_mpn="A1", normalized_mpn="a1",
                         enrichment_status=MaterialEnrichmentStatus.NOT_CATALOGUED,
                         enriched_at=now - timedelta(days=1))
    stale = MaterialCard(display_mpn="A2", normalized_mpn="a2",
                         enrichment_status=MaterialEnrichmentStatus.NOT_CATALOGUED,
                         enriched_at=now - timedelta(days=40))
    db_session.add_all([fresh, stale])
    db_session.commit()
    picked = {c.normalized_mpn for c in select_batch(db_session, cfg)}
    assert "a2" in picked          # past 30-day backoff → eligible
    assert "a1" not in picked      # within backoff → not yet
```

- [ ] **Step 2: Run — verify fail**

Run: `TESTING=1 PYTHONPATH=$(pwd) pytest tests/test_enrichment_worker.py -k "not_catalogued" -v`
Expected: FAIL (`AttributeError: not_catalogued_retry_days`).

- [ ] **Step 3: Implement**

In `app/services/enrichment_worker/config.py`, add the field after `not_found_retry_hours`:
```python
    not_found_retry_hours: int = 22
    not_catalogued_retry_days: int = 30
```
and in `from_env`, after the `not_found_retry_hours=...` line:
```python
            not_catalogued_retry_days=int(os.environ.get("ENRICHMENT_NOT_CATALOGUED_RETRY_DAYS", 30)),
```

In `app/services/enrichment_worker/worker.py`, in `select_batch`, after the `not_found_eligible = and_(...)` block add:
```python
    not_catalogued_cutoff = now - timedelta(days=config.not_catalogued_retry_days)
    not_catalogued_eligible = and_(
        MaterialCard.enrichment_status == MaterialEnrichmentStatus.NOT_CATALOGUED,
        or_(
            MaterialCard.enriched_at.is_(None),
            MaterialCard.enriched_at < not_catalogued_cutoff,
        ),
    )
```
and add `not_catalogued_eligible` to the `or_(...)` in the `.filter(...)`:
```python
            or_(
                MaterialCard.enrichment_status == MaterialEnrichmentStatus.UNENRICHED,
                not_found_eligible,
                not_catalogued_eligible,
            ),
```

In `run_one_batch`, replace the per-card try block (the `web_enabled = "web_search" not in disabled` line and the success accounting) with metered accounting. Specifically, delete the line `web_enabled = "web_search" not in disabled` and replace:
```python
        try:
            status = await enrich_card(
                card,
                db,
                connectors=conns,
                disabled=disabled,
                cooldown=cooldown,
            )
            card.enriched_at = now
            counts[status] = counts.get(status, 0) + 1

            if status != MaterialEnrichmentStatus.VERIFIED:
                breaker.record_claude_success()
                if web_enabled:
                    web_calls_today += 1
                    intel_cache.set_cached(web_cache_key, {"count": web_calls_today}, ttl_days=1.0)
```
with:
```python
        card_meter = {"web_calls": 0, "claude_ok": False}
        try:
            status = await enrich_card(
                card,
                db,
                connectors=conns,
                disabled=disabled,
                cooldown=cooldown,
                web_meter=card_meter,
            )
            card.enriched_at = now
            counts[status] = counts.get(status, 0) + 1

            # A Claude call (web/cross-ref/OEM/infer) returned without raising → backend healthy.
            if card_meter["claude_ok"]:
                breaker.record_claude_success()
            # Each card may fire 1–3 billable web-search calls; meter keeps the cap exact.
            if card_meter["web_calls"] > 0:
                web_calls_today += card_meter["web_calls"]
                intel_cache.set_cached(web_cache_key, {"count": web_calls_today}, ttl_days=1.0)
```

> Leave the budget-gate block above the loop (`if web_calls_today >= config.web_daily_cap ...: disabled.add("web_search")`) unchanged — it still trips the persistent `disabled` flag that `enrich_card` reads. Only the post-call accounting changes.

- [ ] **Step 4: Run — verify pass**

Run: `TESTING=1 PYTHONPATH=$(pwd) pytest tests/test_enrichment_worker.py -v`
Expected: PASS (new + existing).

- [ ] **Step 5: Commit**

```bash
git add app/services/enrichment_worker/config.py app/services/enrichment_worker/worker.py tests/test_enrichment_worker.py
git commit -m "feat(enrichment): metered web budget + breaker reset + not_catalogued retry backoff"
```

---

## Task 7: UI badges for `oem_sourced` + `not_catalogued`

**Files:**
- Modify: `app/templates/htmx/partials/materials/list.html:77-87`

- [ ] **Step 1: Add a render assertion test**

Append to `tests/test_materials_router.py` (or `tests/test_routers_materials.py` — whichever exists; pick the one already importing the materials list partial). If neither has a render helper, create `tests/test_oem_badges.py`:
```python
"""Badge rendering for oem_sourced + not_catalogued in the materials list partial."""

from jinja2 import Environment, FileSystemLoader, select_autoescape


def _render(status, provenance=None):
    env = Environment(
        loader=FileSystemLoader("app/templates"),
        autoescape=select_autoescape(["html"]),
    )
    tmpl = env.get_template("htmx/partials/materials/list.html")
    card = type("C", (), {
        "enrichment_status": status, "enrichment_provenance": provenance or {},
        "lifecycle_status": None, "display_mpn": "01HW917", "manufacturer": "Lenovo",
        "category": "Memory", "description": "x", "_vendor_count": 0, "_best_price": None,
        "_best_currency": "USD", "id": 1, "normalized_mpn": "01hw917",
    })()
    return tmpl.module if False else tmpl.render(materials=[card], lc_colors={})


def test_oem_sourced_badge_renders():
    html = _render("oem_sourced", {"source_urls": ["https://support.lenovo.com/x"],
                                    "source_domains": ["support.lenovo.com"]})
    assert "OEM-SOURCED" in html


def test_not_catalogued_badge_renders():
    html = _render("not_catalogued")
    assert "NOT CATALOGUED" in html
```
> If `list.html` requires more context vars to render than the stub provides, add the missing attributes to the stub `C` class until it renders — do not change the template to suit the test.

- [ ] **Step 2: Run — verify fail**

Run: `TESTING=1 PYTHONPATH=$(pwd) pytest tests/test_oem_badges.py -v`
Expected: FAIL (`OEM-SOURCED` not in output).

- [ ] **Step 3: Implement**

In `app/templates/htmx/partials/materials/list.html`, insert between the `web_sourced` `{% elif %}` block (ends at the `</a>` on line 77) and `{% elif es == "ai_inferred" %}` (line 78):
```jinja
            {% elif es == "oem_sourced" %}
            {% set _ou = (m.enrichment_provenance or {}).get('source_urls', [None])[0] %}
            <a href="{{ _ou or '#' }}" target="_blank" rel="noopener"
               class="inline-flex px-2 py-0.5 text-xs font-medium rounded-full border bg-indigo-50 text-indigo-700 border-indigo-200"
               title="OEM-official source: {{ (m.enrichment_provenance or {}).get('source_domains', ['vendor page'])|join(', ') }} — description from the OEM; specs not distributor-verified">
              OEM-SOURCED
            </a>
```
and insert between the `not_found` `{% elif %}` block (ends line 87) and `{% else %}` (line 88):
```jinja
            {% elif es == "not_catalogued" %}
            <span class="inline-flex px-2 py-0.5 text-xs font-medium rounded-full border bg-slate-100 text-slate-600 border-slate-300"
                  title="Recognised OEM service/FRU part; no public specs published">
              NOT CATALOGUED
            </span>
```

- [ ] **Step 4: Run — verify pass**

Run: `TESTING=1 PYTHONPATH=$(pwd) pytest tests/test_oem_badges.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/templates/htmx/partials/materials/list.html tests/test_oem_badges.py
git commit -m "feat(materials-ui): oem_sourced + not_catalogued status badges"
```

---

## Task 8: Filter toggles for the two new tiers

**Files:**
- Modify: `app/templates/htmx/partials/materials/workspace.html` (toggle markup `:56`; hx-vals JS `:194-204`)
- Modify: `app/static/htmx_app.js` (state `:558`; syncFromURL `:595`; reset `:625`; serialize `:636`)

- [ ] **Step 1: Implement state (no unit test — covered by E2E/manual; this is wiring)**

In `app/static/htmx_app.js`, after line 558 (`webSourced: false,`) add:
```javascript
  oemSourced: false,
  notCatalogued: false,
```
After the `this.webSourced = params.get('web_sourced') === 'true';` line (~595) add:
```javascript
      this.oemSourced = params.get('oem_sourced') === 'true';
      this.notCatalogued = params.get('not_catalogued') === 'true';
```
After the reset `this.webSourced = false;` line (~625) add:
```javascript
      this.oemSourced = false;
      this.notCatalogued = false;
```
After the serialize `if (this.webSourced) params.set('web_sourced', 'true'); else params.delete('web_sourced');` line (~636) add:
```javascript
    if (this.oemSourced) params.set('oem_sourced', 'true'); else params.delete('oem_sourced');
    if (this.notCatalogued) params.set('not_catalogued', 'true'); else params.delete('not_catalogued');
```

In `app/templates/htmx/partials/materials/workspace.html`: change the web-sourced wrapper `<div class="mb-3">` (line 56) to `<div class="mb-1">`, then insert after that toggle's closing `</div>` (line 64):
```jinja
      {# OEM-sourced toggle #}
      <div class="mb-1">
        <label class="flex items-center gap-2 px-2 py-1.5 rounded hover:bg-gray-50 cursor-pointer text-sm">
          <input type="checkbox"
                 :checked="oemSourced"
                 @change="oemSourced = !oemSourced; applyFilters()"
                 class="rounded border-gray-300 text-indigo-600 focus:ring-indigo-500 h-3.5 w-3.5">
          <span class="text-gray-700 font-medium">OEM-sourced</span>
        </label>
      </div>
      {# Not-catalogued toggle #}
      <div class="mb-3">
        <label class="flex items-center gap-2 px-2 py-1.5 rounded hover:bg-gray-50 cursor-pointer text-sm">
          <input type="checkbox"
                 :checked="notCatalogued"
                 @change="notCatalogued = !notCatalogued; applyFilters()"
                 class="rounded border-gray-300 text-slate-600 focus:ring-slate-500 h-3.5 w-3.5">
          <span class="text-gray-700 font-medium">Not catalogued</span>
        </label>
      </div>
```
In the `hx-vals` JS (lines 194-204), after `var webSourced = ...` add:
```javascript
           var oemSourced = Alpine.evaluate(ws, "oemSourced") || false;
           var notCatalogued = Alpine.evaluate(ws, "notCatalogued") || false;
```
and after `if (webSourced) statusList.push("web_sourced");` add:
```javascript
           if (oemSourced) statusList.push("oem_sourced");
           if (notCatalogued) statusList.push("not_catalogued");
```

- [ ] **Step 2: Build the frontend bundle to verify no JS syntax error**

Run: `npm run build 2>&1 | tail -5`
Expected: build succeeds (bundle smoke test passes).

- [ ] **Step 3: Commit**

```bash
git add app/templates/htmx/partials/materials/workspace.html app/static/htmx_app.js
git commit -m "feat(materials-ui): oem_sourced + not_catalogued filter toggles"
```

---

## Task 9: Dry-run-first backfill script

**Files:**
- Create: `scripts/backfill_oem_enrichment.py`
- Test: `tests/test_backfill_oem_enrichment.py`

- [ ] **Step 1: Write failing test**

Create `tests/test_backfill_oem_enrichment.py`:
```python
"""Backfill: dry-run writes a coverage report and commits nothing; budget cap halts."""

import csv
from unittest.mock import AsyncMock, patch

import pytest

from app.constants import MaterialEnrichmentStatus
from app.models import MaterialCard


@pytest.mark.asyncio
async def test_dry_run_writes_csv_and_no_commit(db_session, tmp_path):
    import scripts.backfill_oem_enrichment as bf

    c = MaterialCard(display_mpn="01HW917", normalized_mpn="01hw917",
                     enrichment_status=MaterialEnrichmentStatus.NOT_FOUND)
    db_session.add(c)
    db_session.commit()

    async def fake_enrich(card, db, **kw):
        card.enrichment_status = MaterialEnrichmentStatus.OEM_SOURCED
        return MaterialEnrichmentStatus.OEM_SOURCED

    out = tmp_path / "cov.csv"
    with patch.object(bf, "enrich_card", new=AsyncMock(side_effect=fake_enrich)), \
         patch.object(bf, "SessionLocal", return_value=db_session):
        counts = await bf.run(commit=False, limit=None, max_web_calls=100, csv_path=str(out))

    db_session.expire_all()
    assert db_session.get(MaterialCard, c.id).enrichment_status == MaterialEnrichmentStatus.NOT_FOUND  # rolled back
    assert counts["oem_sourced"] == 1
    rows = list(csv.DictReader(out.open()))
    assert rows[0]["projected_status"] == "oem_sourced"


@pytest.mark.asyncio
async def test_budget_cap_halts(db_session, tmp_path):
    import scripts.backfill_oem_enrichment as bf

    for i in range(5):
        db_session.add(MaterialCard(display_mpn=f"01HW{i:03d}", normalized_mpn=f"01hw{i:03d}",
                                    enrichment_status=MaterialEnrichmentStatus.NOT_FOUND))
    db_session.commit()

    async def fake_enrich(card, db, *, web_meter=None, **kw):
        if web_meter is not None:
            web_meter["web_calls"] = web_meter.get("web_calls", 0) + 2
        card.enrichment_status = MaterialEnrichmentStatus.NOT_CATALOGUED
        return MaterialEnrichmentStatus.NOT_CATALOGUED

    with patch.object(bf, "enrich_card", new=AsyncMock(side_effect=fake_enrich)), \
         patch.object(bf, "SessionLocal", return_value=db_session):
        counts = await bf.run(commit=False, limit=None, max_web_calls=3, csv_path=str(tmp_path / "c.csv"))

    # 3-call budget, 2 calls per card → stops after the 2nd card (4 calls would exceed).
    assert counts["processed"] <= 2
```

- [ ] **Step 2: Run — verify fail**

Run: `TESTING=1 PYTHONPATH=$(pwd) pytest tests/test_backfill_oem_enrichment.py -v`
Expected: FAIL (module not found).

- [ ] **Step 3: Implement**

Create `scripts/backfill_oem_enrichment.py`:
```python
"""One-time backfill: re-enrich not_found / not_catalogued MaterialCards through the OEM
cross-ref + description tiers.

Dry-run by default: runs ``enrich_card`` over the backlog, tallies projected outcomes,
writes a coverage CSV, and ROLLS BACK (writes nothing). ``--commit`` persists, with a
shared web-call budget cap so it cannot blow the API spend. The paced worker drains any
remainder afterward.

Usage:
  python3 scripts/backfill_oem_enrichment.py --dry-run
  python3 scripts/backfill_oem_enrichment.py --commit --max-web-calls 300 --limit 500

Called by: operators (manual, under explicit authorization). Depends on:
app.database.SessionLocal, app.services.authoritative_enrichment_service.
"""

import argparse
import asyncio
import csv
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from loguru import logger  # noqa: E402

from app.constants import MaterialEnrichmentStatus  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models import MaterialCard  # noqa: E402
from app.services.authoritative_enrichment_service import (  # noqa: E402
    _connectors_in_order,
    enrich_card,
)
from app.services.enrichment_worker.oem_classifier import classify_oem_vendor  # noqa: E402

_TARGET_STATUSES = (MaterialEnrichmentStatus.NOT_FOUND, MaterialEnrichmentStatus.NOT_CATALOGUED)


def _select(db, limit):
    q = (
        db.query(MaterialCard)
        .filter(
            MaterialCard.deleted_at.is_(None),
            MaterialCard.is_internal_part.is_(False),
            MaterialCard.enrichment_status.in_([s.value for s in _TARGET_STATUSES]),
        )
        .order_by(MaterialCard.search_count.desc(), MaterialCard.created_at.desc())
    )
    return q.limit(limit).all() if limit else q.all()


async def run(*, commit: bool, limit, max_web_calls: int, csv_path: str) -> dict:
    """Process the backlog. Returns a counts dict. Writes a coverage CSV. Rolls back unless commit."""
    db = SessionLocal()
    counts: dict[str, int] = {"processed": 0, "web_calls": 0}
    rows: list[dict] = []
    try:
        conns = _connectors_in_order(db)
        cards = _select(db, limit)
        logger.info("BACKFILL: {} candidate cards (commit={}, budget={})", len(cards), commit, max_web_calls)
        disabled: set[str] = set()
        cooldown: dict[str, float] = {}
        web_total = 0
        for i, card in enumerate(cards, 1):
            if web_total >= max_web_calls:
                logger.info("BACKFILL: web budget {} reached — stopping (remaining drains via worker)", max_web_calls)
                break
            meter = {"web_calls": 0, "claude_ok": False}
            try:
                status = await enrich_card(card, db, connectors=conns, disabled=disabled,
                                           cooldown=cooldown, web_meter=meter)
            except Exception as e:  # noqa: BLE001 — a single bad card must not abort the run
                logger.warning("BACKFILL: {} failed: {}", card.display_mpn, type(e).__name__)
                status = "error"
            web_total += meter["web_calls"]
            counts["processed"] += 1
            counts[status] = counts.get(status, 0) + 1
            resolved = None
            if card.enrichment_provenance and isinstance(card.enrichment_provenance, dict):
                resolved = (card.enrichment_provenance.get("cross_ref") or {}).get("resolved_mpn")
            rows.append({
                "display_mpn": card.display_mpn,
                "vendor": classify_oem_vendor(card.display_mpn) or "",
                "projected_status": status,
                "resolved_mpn": resolved or "",
                "source": card.enrichment_source or "",
            })
            if i % 25 == 0:
                logger.info("BACKFILL: {}/{} (web_calls={})", i, len(cards), web_total)

        counts["web_calls"] = web_total
        if commit:
            db.commit()
            logger.info("BACKFILL: committed.")
        else:
            db.rollback()
            logger.info("BACKFILL: DRY RUN — rolled back, no DB writes.")

        with open(csv_path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["display_mpn", "vendor", "projected_status", "resolved_mpn", "source"])
            w.writeheader()
            w.writerows(rows)
        logger.info("BACKFILL: coverage CSV → {}", csv_path)
        logger.info("BACKFILL SUMMARY: {}", {k: v for k, v in counts.items()})
        return counts
    finally:
        db.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="Preview only (default).")
    ap.add_argument("--commit", action="store_true", help="Persist results (default is dry-run).")
    ap.add_argument("--limit", type=int, default=None, help="Max cards to process.")
    ap.add_argument("--max-web-calls", type=int, default=300, help="Web-search call budget cap.")
    ap.add_argument("--csv", default="backfill_oem_coverage.csv", help="Coverage CSV output path.")
    args = ap.parse_args()
    if not args.commit:
        logger.info("DRY RUN — no DB writes. Use --commit to persist.")
    asyncio.run(run(commit=args.commit, limit=args.limit, max_web_calls=args.max_web_calls, csv_path=args.csv))
```

- [ ] **Step 4: Run — verify pass**

Run: `TESTING=1 PYTHONPATH=$(pwd) pytest tests/test_backfill_oem_enrichment.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/backfill_oem_enrichment.py tests/test_backfill_oem_enrichment.py
git commit -m "feat(scripts): dry-run-first OEM enrichment backfill with budget cap"
```

---

## Task 10: Update APP_MAP docs

**Files:**
- Modify: `docs/APP_MAP_ARCHITECTURE.md`, `docs/APP_MAP_DATABASE.md`, `docs/APP_MAP_INTERACTIONS.md`

- [ ] **Step 1: Edit docs (no test)**
  - `APP_MAP_DATABASE.md`: in the `material_cards.enrichment_status` description, add `oem_sourced` and `not_catalogued` to the value list; note `cross_references` now also records FRU→MPN linkages written by cross-ref enrichment.
  - `APP_MAP_ARCHITECTURE.md`: under the enrichment-worker module list, add `oem_classifier.py`, `oem_domains.py`, `oem_extractor.py`; add `scripts/backfill_oem_enrichment.py`.
  - `APP_MAP_INTERACTIONS.md`: in the enrichment-tier flow, document the new sequence (verified → web_sourced → OEM cross-ref [double-verify] → OEM description → ai_inferred → not_catalogued/not_found) and the `web_meter` budget/breaker contract.

- [ ] **Step 2: Commit**

```bash
git add docs/APP_MAP_ARCHITECTURE.md docs/APP_MAP_DATABASE.md docs/APP_MAP_INTERACTIONS.md
git commit -m "docs(app-map): document OEM enrichment tiers + new statuses"
```

---

## Final Verification (after all tasks)

- [ ] `TESTING=1 PYTHONPATH=$(pwd) pytest tests/ -q` — full suite green (no regressions vs the 57-test baseline + new tests).
- [ ] `ruff check app/ scripts/ && ruff format --check app/ scripts/` — clean.
- [ ] `mypy app/` — no new errors.
- [ ] `pre-commit run --all-files` — all hooks pass.
- [ ] `npm run build` — bundle smoke test passes (JS toggles).
- [ ] Backfill dry-run produces a sane coverage CSV (Task 11 in the session task list).

## Self-Review Notes (author check vs spec)
- Spec §3 tier order, §3.1 dual cross-ref gate, §4.1–4.8 components, §4.4.1 metering+breaker,
  §4.6 retry backoff, §4.7 UI, §4.8 backfill — each maps to Task 1–10. ✓
- Type consistency: `CrossRefResult` / `OemExtractResult` fields used in Task 5 match Task 4
  definitions; `web_meter` keys `{"web_calls","claude_ok"}` consistent across Tasks 5, 6, 9. ✓
- No migration (varchar status) — Task 1 changes enum only. ✓
- `not_catalogued` only when `vendor and oem_attempted` (Task 5) ↔ web-disabled stays
  `not_found` (Task 5 test `test_oem_tiers_skipped_when_web_disabled`). ✓
