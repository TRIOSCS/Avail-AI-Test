# Verified Material Enrichment + Part-Number Import — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Import a bare list of part numbers into availai `material_cards` and enrich each with core attributes from authoritative catalog sources (with per-field provenance), flagging anything we can't verify as AI-inferred or not-found — so nothing is ever a silent guess.

**Architecture:** A new `authoritative_enrichment_service` queries existing connectors (DigiKey → Mouser → Element14 → OEMSecrets → Nexar, gaps only) via `BaseConnector.search()`, accepts a source's data only on an exact normalized-MPN match, merges core fields first-non-null-by-priority while recording provenance, and falls back to a flagged Claude Opus 4.8 inference (`description`+`category` only) for parts no source resolves. Two new `material_cards` columns (`enrichment_status`, `enrichment_provenance`) make verified/inferred a first-class, filterable property. A new import endpoint + script (handling the HTML-table-as-`.xls` format) creates bare cards and runs the pipeline dry-run-first.

**Tech Stack:** FastAPI, SQLAlchemy 2.0 (sync), PostgreSQL (SQLite in tests), Alembic, Jinja2 + HTMX + Alpine.js + Tailwind, pytest, the house Anthropic wrapper `app/utils/claude_client.py`.

**Spec:** `docs/superpowers/specs/2026-06-04-verified-material-enrichment-design.md`

**Branch:** `feat/verified-material-enrichment` (already created off `main`; spec already committed as `91e54784`).

**Conventions for every task:**
- Run tests with `python3 -m pytest` (the bare `python` is not on PATH).
- Before each commit: `pre-commit run --files <changed files>` must pass (ruff, ruff-format, mypy, docformatter).
- Commit after each task with a `type(scope): summary` message + the `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>` trailer.

---

## File Structure

**Create:**
- `alembic/versions/a1f7c2d9e4b8_add_material_enrichment_status_provenance.py` — migration for the two new columns.
- `app/services/authoritative_enrichment_service.py` — source-priority merge + provenance + exact-match guard + orchestration.
- `app/services/ai_inference_fallback.py` — flagged Opus 4.8 inference (description+category only).
- `scripts/import_part_numbers.py` — one-time/ops importer with dry-run coverage report.
- `tests/test_authoritative_enrichment.py`, `tests/test_ai_inference_fallback.py`, `tests/test_part_number_import.py`, `tests/test_material_enrichment_status_filter.py`, `tests/test_html_table_parse.py`.

**Modify:**
- `app/models/intelligence.py` — add `enrichment_status`, `enrichment_provenance` to `MaterialCard`.
- `app/utils/claude_client.py:45` — bump `MODELS["opus"]` to `claude-opus-4-8`.
- `app/connectors/digikey.py`, `mouser.py`, `element14.py`, `oemsecrets.py`, `app/connectors/sources.py` (NexarConnector) — add optional core-attribute keys to result dicts.
- `app/file_utils.py` — add `_parse_html_table()` + HTML sniff in `parse_tabular_file()` + `extract_mpns()`.
- `app/routers/materials.py` — new `POST /api/materials/import-part-numbers` endpoint.
- `app/services/faceted_search_service.py:155` — add `verified_only` param.
- `app/routers/htmx_views.py:7197` — add `verified_only` query param to `materials_faceted_partial`.
- `app/templates/htmx/partials/materials/list.html`, `workspace.html`, `app/static/htmx_app.js` — status badge + "Verified only" toggle.
- `docs/APP_MAP_DATABASE.md`, `docs/APP_MAP_ARCHITECTURE.md`, `docs/APP_MAP_INTERACTIONS.md` — document the changes.

---

## Task 1: DB migration + model columns

**Files:**
- Modify: `app/models/intelligence.py` (after `enriched_at`, ~line 50)
- Create: `alembic/versions/a1f7c2d9e4b8_add_material_enrichment_status_provenance.py`
- Test: `tests/test_authoritative_enrichment.py` (first test only)

- [ ] **Step 1: Add columns to the model**

In `app/models/intelligence.py`, immediately after the `enriched_at = Column(UTCDateTime)` line, add:

```python
    # Verification provenance (added 2026-06-04 — verified-enrichment feature)
    # enrichment_status: "unenriched" | "verified" | "ai_inferred" | "not_found"
    enrichment_status = Column(String(20), nullable=False, server_default="unenriched", index=True)
    # Per-field provenance: {"<field>": {"source": "digikey", "confidence": 1.0,
    #                                    "fetched_at": "2026-06-04T..Z", "matched_mpn": "..."}}
    enrichment_provenance = Column(JSONB)
```

(`String`, `Column`, `JSONB` are already imported in this file.)

- [ ] **Step 2: Write the failing model test**

Create `tests/test_authoritative_enrichment.py`:

```python
from datetime import datetime, timezone

from app.models import MaterialCard


def test_new_card_defaults_to_unenriched(db_session):
    card = MaterialCard(
        normalized_mpn="teststatusdefault",
        display_mpn="TEST-STATUS-DEFAULT",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(card)
    db_session.flush()
    db_session.refresh(card)
    assert card.enrichment_status == "unenriched"
    assert card.enrichment_provenance is None
```

- [ ] **Step 3: Run it to verify it fails**

Run: `python3 -m pytest tests/test_authoritative_enrichment.py::test_new_card_defaults_to_unenriched -v`
Expected: FAIL — `AttributeError`/`no such column enrichment_status` (model/test DB not yet updated). (Test DB is created from models, so this passes only once Step 1 is in; if it already passes after Step 1, that confirms the model change.)

- [ ] **Step 4: Create the migration**

Create `alembic/versions/a1f7c2d9e4b8_add_material_enrichment_status_provenance.py`:

```python
"""Add material_cards.enrichment_status and enrichment_provenance.

enrichment_status (VARCHAR 20, NOT NULL, server_default 'unenriched', indexed)
marks a card as unenriched | verified | ai_inferred | not_found.
enrichment_provenance (JSONB, nullable) records per-field source attribution.

Revision ID: a1f7c2d9e4b8
Revises: 086_add_activity_digest
Create Date: 2026-06-04 22:30:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "a1f7c2d9e4b8"
down_revision: Union[str, None] = "086_add_activity_digest"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "material_cards",
        sa.Column(
            "enrichment_status",
            sa.String(length=20),
            nullable=False,
            server_default="unenriched",
        ),
    )
    op.add_column(
        "material_cards",
        sa.Column("enrichment_provenance", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.create_index(
        "ix_material_cards_enrichment_status",
        "material_cards",
        ["enrichment_status"],
    )


def downgrade() -> None:
    op.drop_index("ix_material_cards_enrichment_status", table_name="material_cards")
    op.execute("ALTER TABLE IF EXISTS material_cards DROP COLUMN IF EXISTS enrichment_provenance")
    op.execute("ALTER TABLE IF EXISTS material_cards DROP COLUMN IF EXISTS enrichment_status")
```

- [ ] **Step 5: Verify the model test passes + migration round-trips**

Run: `python3 -m pytest tests/test_authoritative_enrichment.py::test_new_card_defaults_to_unenriched -v`
Expected: PASS.

Run (migration round-trip against a Postgres dev DB if available; skip if tests use SQLite only):
`PYTHONPATH=/root/availai alembic upgrade head && PYTHONPATH=/root/availai alembic downgrade -1 && PYTHONPATH=/root/availai alembic upgrade head`
Expected: no errors; `alembic heads` now shows `a1f7c2d9e4b8`.

- [ ] **Step 6: Commit**

```bash
pre-commit run --files app/models/intelligence.py alembic/versions/a1f7c2d9e4b8_add_material_enrichment_status_provenance.py tests/test_authoritative_enrichment.py
git add app/models/intelligence.py alembic/versions/a1f7c2d9e4b8_add_material_enrichment_status_provenance.py tests/test_authoritative_enrichment.py
git commit -m "feat(materials): add enrichment_status + enrichment_provenance columns

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: Connector core-attribute extraction

Connectors today return only `manufacturer`/`description`/`datasheet_url` (+ price/stock). Extend each to *additionally* surface `category`, `lifecycle_status`, `package_type`, `pin_count`, `rohs_status` as **optional** keys (None when absent) — additive, so existing search behavior is untouched.

> ⚠️ The exact raw API field names below are from API docs, not a captured response. Step 1 verifies them against real responses before relying on them.

**Files:**
- Modify: `app/connectors/digikey.py`, `mouser.py`, `element14.py`, `oemsecrets.py`, `app/connectors/sources.py`
- Create: `app/connectors/_core_attrs.py` (shared normalization helpers)
- Test: `tests/test_connector_core_attrs.py`

- [ ] **Step 1: Probe real responses to confirm field names**

Run a one-off probe (delete after) for a known catalog MPN to capture the raw JSON keys for DigiKey and Nexar:

```bash
cd /root/availai && PYTHONPATH=/root/availai python3 - <<'PY'
import asyncio, json
from app.database import SessionLocal
from app.search_service import _build_connectors
db = SessionLocal()
conns, _, _ = _build_connectors(db)
async def main():
    for c in conns:
        if c.source_name in ("digikey", "nexar"):
            hits = await c.search("STM32F407VGT6")
            print(c.source_name, "->", len(hits), "hits")
            if hits:
                print(json.dumps(hits[0], indent=2, default=str)[:1500])
asyncio.run(main())
db.close()
PY
```

Record the actual key names (e.g. DigiKey `ProductStatus.Status`, `Category.Name`, `Parameters[].ParameterText/ValueText`). If they differ from the mapping in Step 3, adjust Step 3 accordingly. **Do not commit this probe.**

- [ ] **Step 2: Write the shared normalizer + failing test**

Create `app/connectors/_core_attrs.py`:

```python
"""Shared helpers to normalize connector core attributes into MaterialCard vocab.

Core attributes: category, lifecycle_status, package_type, pin_count, rohs_status.
All helpers return None when the input is missing/unmappable — never a guess.
"""

from __future__ import annotations

from typing import Any

# DigiKey ProductStatus / generic distributor lifecycle text -> MaterialCard lifecycle_status
_LIFECYCLE_MAP = {
    "active": "active",
    "obsolete": "obsolete",
    "discontinued": "obsolete",
    "not for new designs": "nrfnd",
    "nrnd": "nrfnd",
    "last time buy": "ltb",
    "end of life": "eol",
    "eol": "eol",
}

_ROHS_MAP = {
    "rohs compliant": "compliant",
    "compliant": "compliant",
    "rohs3 compliant": "compliant",
    "non-compliant": "non-compliant",
    "not compliant": "non-compliant",
    "rohs exempt": "exempt",
    "exempt": "exempt",
}


def map_lifecycle(raw: Any) -> str | None:
    if not raw:
        return None
    return _LIFECYCLE_MAP.get(str(raw).strip().lower())


def map_rohs(raw: Any) -> str | None:
    if not raw:
        return None
    return _ROHS_MAP.get(str(raw).strip().lower())


def clean_str(raw: Any, *, maxlen: int) -> str | None:
    if raw is None:
        return None
    s = str(raw).strip()
    return s[:maxlen] if s else None


def safe_pin_count(raw: Any) -> int | None:
    try:
        v = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def digikey_parameter(params: Any, names: tuple[str, ...]) -> str | None:
    """Extract a ValueText from DigiKey Parameters[] by ParameterText match."""
    if not isinstance(params, list):
        return None
    wanted = {n.lower() for n in names}
    for p in params:
        if isinstance(p, dict) and str(p.get("ParameterText", "")).strip().lower() in wanted:
            val = str(p.get("ValueText", "")).strip()
            if val and val != "-":
                return val
    return None
```

Create `tests/test_connector_core_attrs.py`:

```python
from app.connectors._core_attrs import (
    digikey_parameter,
    map_lifecycle,
    map_rohs,
    safe_pin_count,
)


def test_lifecycle_mapping():
    assert map_lifecycle("Active") == "active"
    assert map_lifecycle("Not For New Designs") == "nrfnd"
    assert map_lifecycle("Obsolete") == "obsolete"
    assert map_lifecycle("totally unknown status") is None
    assert map_lifecycle(None) is None


def test_rohs_mapping():
    assert map_rohs("ROHS Compliant") == "compliant"
    assert map_rohs("Non-Compliant") == "non-compliant"
    assert map_rohs("weird") is None


def test_pin_count():
    assert safe_pin_count("64") == 64
    assert safe_pin_count("0") is None
    assert safe_pin_count("abc") is None


def test_digikey_parameter():
    params = [
        {"ParameterText": "Package / Case", "ValueText": "100-LQFP"},
        {"ParameterText": "Number of I/O", "ValueText": "82"},
    ]
    assert digikey_parameter(params, ("Package / Case",)) == "100-LQFP"
    assert digikey_parameter(params, ("Mounting Type",)) is None
    assert digikey_parameter([], ("Package / Case",)) is None
```

- [ ] **Step 3: Run test to verify it fails, then it passes**

Run: `python3 -m pytest tests/test_connector_core_attrs.py -v`
Expected: PASS once `_core_attrs.py` exists (this module is pure logic; it should go green immediately).

- [ ] **Step 4: Wire DigiKey extraction**

In `app/connectors/digikey.py`, in the parse loop (where `results.append({...})` is built, ~line 147), before the append add:

```python
        from ._core_attrs import (
            clean_str,
            digikey_parameter,
            map_lifecycle,
            map_rohs,
            safe_pin_count,
        )

        cat = prod.get("Category") or {}
        category = clean_str(cat.get("Name") if isinstance(cat, dict) else cat, maxlen=255)
        status = prod.get("ProductStatus") or {}
        lifecycle = map_lifecycle(status.get("Status") if isinstance(status, dict) else status)
        params = prod.get("Parameters")
        package = clean_str(digikey_parameter(params, ("Package / Case", "Supplier Device Package")), maxlen=100)
        pin_count = safe_pin_count(digikey_parameter(params, ("Number of Terminations", "Number of I/O", "Number of Pins")))
        rohs = map_rohs((prod.get("Classifications") or {}).get("RohsStatus") if isinstance(prod.get("Classifications"), dict) else prod.get("RohsStatus"))
```

Then add these keys inside the result dict (after `"description": detail_desc,`):

```python
                    "category": category,
                    "lifecycle_status": lifecycle,
                    "package_type": package,
                    "pin_count": pin_count,
                    "rohs_status": rohs,
```

- [ ] **Step 5: Wire Mouser, Element14, OEMSecrets, Nexar**

Apply the same additive pattern using each connector's raw fields (confirm names via Step 1 where uncertain). Add to each result dict, defaulting to `None`:

- **`mouser.py`** (after `"description": desc,`): use `part.get("Category")`, `part.get("LifecycleStatus")`, `part.get("DataSheetUrl")` and `part.get("ProductAttributes")` (a list of `{AttributeName, AttributeValue}` — mirror `digikey_parameter` but for these keys).
- **`element14.py`** (after the description key): `prod.get("attributes")` (Newark returns `[{attributeLabel, attributeValue}]`); map `RoHS`/`Package` labels; `prod.get("datasheets")`.
- **`oemsecrets.py`** (after `"datasheet_url": datasheet,`): `item.get("category")`, `item.get("lifecycle_status")` (often absent — leave None).
- **`sources.py` `NexarConnector`**: add `category { name }` to `FULL_QUERY`'s `part { ... }` selection (it is already in `AGGREGATE_QUERY`), and extract `category` in the full-query parse. Lifecycle/package/pin are not reliably in the Nexar schema — leave None unless Step 1 shows otherwise.

Each new key is optional; downstream uses `.get()`, so missing keys are safe.

- [ ] **Step 6: Test connector extraction with recorded responses**

Add to `tests/test_connector_core_attrs.py` a test that feeds a recorded raw DigiKey `prod` dict (built from Step 1's capture) through the connector's parse path and asserts the new keys appear. Since `_do_search` performs HTTP, test the **pure extraction** by factoring the per-product mapping into a small module function if needed, or assert via a monkeypatched `httpx` response (see Task 3 mocking pattern). Minimum: assert `map_lifecycle`/`digikey_parameter` produce the expected values for the captured payload.

Run: `python3 -m pytest tests/test_connector_core_attrs.py -v` → PASS.

- [ ] **Step 7: Commit**

```bash
pre-commit run --files app/connectors/_core_attrs.py app/connectors/digikey.py app/connectors/mouser.py app/connectors/element14.py app/connectors/oemsecrets.py app/connectors/sources.py tests/test_connector_core_attrs.py
git add app/connectors/_core_attrs.py app/connectors/digikey.py app/connectors/mouser.py app/connectors/element14.py app/connectors/oemsecrets.py app/connectors/sources.py tests/test_connector_core_attrs.py
git commit -m "feat(connectors): capture core attributes (category/lifecycle/package/pins/rohs)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: Authoritative enrichment service (merge + provenance + exact-match guard)

**Files:**
- Create: `app/services/authoritative_enrichment_service.py`
- Test: `tests/test_authoritative_enrichment.py` (add tests)

- [ ] **Step 1: Write failing tests for the merge + guard**

Add to `tests/test_authoritative_enrichment.py`:

```python
import pytest

from app.services.authoritative_enrichment_service import (
    CORE_FIELDS,
    merge_authoritative,
)


def _hit(source, mpn="LM317T", **over):
    base = {
        "source_type": source,
        "mpn_matched": mpn,
        "manufacturer": "TI",
        "description": f"desc from {source}",
        "category": None,
        "lifecycle_status": None,
        "package_type": None,
        "pin_count": None,
        "rohs_status": None,
        "datasheet_url": None,
    }
    base.update(over)
    return base


def test_exact_match_guard_rejects_mismatch():
    # connector returned a DIFFERENT part — must be ignored
    results = {"digikey": [_hit("digikey", mpn="LM317MT")]}
    merged, prov, contributors = merge_authoritative("lm317t", results)
    assert merged == {}
    assert contributors == []


def test_first_non_null_by_priority():
    results = {
        "mouser": [_hit("mouser", description="mouser desc", category="Linear")],
        "digikey": [_hit("digikey", description="digikey desc", lifecycle_status="active")],
    }
    merged, prov, contributors = merge_authoritative("lm317t", results)
    # digikey has higher priority -> its description wins
    assert merged["description"] == "digikey desc"
    assert prov["description"]["source"] == "digikey"
    # category only present from mouser -> taken from mouser
    assert merged["category"] == "Linear"
    assert prov["category"]["source"] == "mouser"
    assert merged["lifecycle_status"] == "active"
    assert "digikey" in contributors and "mouser" in contributors
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_authoritative_enrichment.py -v`
Expected: FAIL — `ModuleNotFoundError: authoritative_enrichment_service`.

- [ ] **Step 3: Implement the service**

Create `app/services/authoritative_enrichment_service.py`:

```python
"""Verified, source-attributed enrichment for MaterialCards.

Queries existing connectors in cost-optimized priority order, accepts a source's
data ONLY on an exact normalized-MPN match, and merges core attributes
first-non-null-by-priority while recording per-field provenance. Parts with no
authoritative hit fall through to a flagged Opus 4.8 inference (see
ai_inference_fallback) — never a silent guess.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from loguru import logger
from sqlalchemy.orm import Session

from app.models import MaterialCard
from app.utils.normalization import normalize_mpn_key

# Cost-optimized: free distributor APIs first, paid Nexar last (gaps only).
SOURCE_ORDER = ["digikey", "mouser", "element14", "oemsecrets", "nexar"]
# Nexar source_type is reported as "octopart" by its connector.
_SOURCE_TYPE_ALIASES = {"octopart": "nexar"}

CORE_FIELDS = [
    "description",
    "manufacturer",
    "category",
    "lifecycle_status",
    "package_type",
    "rohs_status",
    "pin_count",
    "datasheet_url",
]
# A part is "adequately resolved" (skip paid Nexar) once these are present.
_ADEQUATE = ("description", "manufacturer", "category")


def _source_of(hit: dict) -> str:
    st = str(hit.get("source_type", "")).lower()
    return _SOURCE_TYPE_ALIASES.get(st, st)


def merge_authoritative(
    normalized_mpn: str, results_by_source: dict[str, list[dict]]
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """Merge connector results into (fields, provenance, contributors).

    Only exact normalized-MPN matches are considered. For each CORE_FIELD, the
    first source (in SOURCE_ORDER) with a non-null value wins.
    """
    merged: dict[str, Any] = {}
    provenance: dict[str, Any] = {}
    contributors: list[str] = []
    now = datetime.now(timezone.utc).isoformat()

    for source in SOURCE_ORDER:
        hits = results_by_source.get(source) or []
        exact = [h for h in hits if normalize_mpn_key(h.get("mpn_matched")) == normalized_mpn]
        if not exact:
            continue
        contributed = False
        for hit in exact:
            for field in CORE_FIELDS:
                if field in merged:
                    continue
                val = hit.get(field)
                if val is None or (isinstance(val, str) and not val.strip()):
                    continue
                merged[field] = val
                provenance[field] = {
                    "source": source,
                    "confidence": 1.0,
                    "fetched_at": now,
                    "matched_mpn": hit.get("mpn_matched"),
                }
                contributed = True
        if contributed and source not in contributors:
            contributors.append(source)
    return merged, provenance, contributors


def _connectors_in_order(db: Session) -> list:
    """Return enabled connectors filtered + ordered to SOURCE_ORDER."""
    from app.search_service import _build_connectors

    conns, _, _ = _build_connectors(db)
    by_name = {}
    for c in conns:
        name = _SOURCE_TYPE_ALIASES.get(c.source_name, c.source_name)
        by_name.setdefault(name, c)
    return [by_name[n] for n in SOURCE_ORDER if n in by_name]


async def fetch_authoritative(
    display_mpn: str, normalized_mpn: str, connectors: list
) -> dict[str, list[dict]]:
    """Query connectors in priority order; short-circuit before paid Nexar once adequate."""
    results: dict[str, list[dict]] = {}
    for conn in connectors:
        name = _SOURCE_TYPE_ALIASES.get(conn.source_name, conn.source_name)
        if name == "nexar":
            merged, _, _ = merge_authoritative(normalized_mpn, results)
            if all(f in merged for f in _ADEQUATE):
                logger.debug("AUTH_ENRICH: {} adequately resolved, skipping nexar", normalized_mpn)
                break
        try:
            results[name] = await conn.search(display_mpn)
        except Exception as e:  # connector-level failure is non-fatal for this MPN
            logger.warning("AUTH_ENRICH: {} failed for {}: {}", name, normalized_mpn, type(e).__name__)
            results[name] = []
    return results


def apply_authoritative(card: MaterialCard, merged: dict, provenance: dict, contributors: list[str]) -> None:
    """Write merged authoritative fields + provenance onto the card."""
    for field, value in merged.items():
        setattr(card, field, value)
    card.enrichment_provenance = provenance
    card.enrichment_source = contributors[0] if contributors else card.enrichment_source
    card.enrichment_status = "verified"
    card.enriched_at = datetime.now(timezone.utc)
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/test_authoritative_enrichment.py -v`
Expected: PASS (all tests).

- [ ] **Step 5: Commit**

```bash
pre-commit run --files app/services/authoritative_enrichment_service.py tests/test_authoritative_enrichment.py
git add app/services/authoritative_enrichment_service.py tests/test_authoritative_enrichment.py
git commit -m "feat(materials): authoritative enrichment merge with exact-match guard + provenance

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: AI inference fallback (Opus 4.8, flagged)

**Files:**
- Modify: `app/utils/claude_client.py:45`
- Create: `app/services/ai_inference_fallback.py`
- Test: `tests/test_ai_inference_fallback.py`

- [ ] **Step 1: Bump the opus model tier to the best version**

In `app/utils/claude_client.py`, change line 45:

```python
    "opus": "claude-opus-4-8",
```

- [ ] **Step 2: Write failing tests**

Create `tests/test_ai_inference_fallback.py`:

```python
from unittest.mock import AsyncMock, patch

import pytest

from app.services.ai_inference_fallback import infer_part


@pytest.mark.asyncio
@patch("app.services.ai_inference_fallback.claude_structured", new_callable=AsyncMock)
async def test_confident_inference_returns_ai_inferred(mock_claude):
    mock_claude.return_value = {
        "description": "Linear voltage regulator, adjustable, TO-220",
        "category": "Voltage Regulator",
        "confidence": 0.8,
    }
    result = await infer_part("LM317T")
    assert result.status == "ai_inferred"
    assert result.description.startswith("Linear voltage regulator")
    assert result.category == "Voltage Regulator"
    # Opus must be requested
    assert mock_claude.call_args.kwargs["model_tier"] == "opus"


@pytest.mark.asyncio
@patch("app.services.ai_inference_fallback.claude_structured", new_callable=AsyncMock)
async def test_declined_inference_returns_not_found(mock_claude):
    mock_claude.return_value = {"description": "", "category": "", "confidence": 0.0}
    result = await infer_part("04M3HJ")
    assert result.status == "not_found"
    assert result.description is None


@pytest.mark.asyncio
@patch("app.services.ai_inference_fallback.claude_structured", new_callable=AsyncMock)
async def test_null_response_returns_not_found(mock_claude):
    mock_claude.return_value = None
    result = await infer_part("ZZZ999")
    assert result.status == "not_found"
```

- [ ] **Step 3: Run to verify failure**

Run: `python3 -m pytest tests/test_ai_inference_fallback.py -v`
Expected: FAIL — module not found.

- [ ] **Step 4: Implement the fallback**

Create `app/services/ai_inference_fallback.py`:

```python
"""Flagged best-effort inference for parts no authoritative source resolves.

Uses Claude Opus 4.8 (model_tier="opus") with a strict refusal rule. Produces
ONLY description + category — never structured specs (lifecycle/package/pins/rohs),
because guessing those is the dangerous kind of hallucination. Confidence below
threshold or empty description => not_found (no guess kept).
"""

from __future__ import annotations

from dataclasses import dataclass

from loguru import logger

from app.utils.claude_client import claude_structured

_MIN_CONFIDENCE = 0.5

_SYSTEM = (
    "You are an expert electronic-component engineer. You are given a single "
    "manufacturer or OEM part number with NO other context. Identify the part ONLY "
    "if you genuinely recognize it. It is correct and expected to decline for "
    "obscure OEM/FRU/service part numbers you do not actually know.\n"
    "Rules:\n"
    "- description: 1 concise sentence of what the part is. Empty string if not confident.\n"
    "- category: a short commodity category (e.g. 'Capacitor', 'Connector', 'Memory Module'). "
    "Empty string if not confident.\n"
    "- confidence: 0.0-1.0, your honest probability that this description is correct.\n"
    "- NEVER invent a plausible-sounding description. When unsure, return empty strings and low confidence."
)

_SCHEMA = {
    "type": "object",
    "properties": {
        "description": {"type": "string"},
        "category": {"type": "string"},
        "confidence": {"type": "number"},
    },
    "required": ["description", "category", "confidence"],
}


@dataclass
class InferenceResult:
    status: str  # "ai_inferred" | "not_found"
    description: str | None
    category: str | None
    confidence: float


async def infer_part(display_mpn: str) -> InferenceResult:
    prompt = f"Part number: {display_mpn}"
    try:
        data = await claude_structured(
            prompt,
            _SCHEMA,
            system=_SYSTEM,
            model_tier="opus",
            max_tokens=300,
        )
    except Exception as e:
        logger.warning("AI_INFER: claude error for {}: {}", display_mpn, type(e).__name__)
        data = None

    if not data:
        return InferenceResult("not_found", None, None, 0.0)

    desc = (data.get("description") or "").strip()
    cat = (data.get("category") or "").strip()
    conf = float(data.get("confidence") or 0.0)

    if not desc or conf < _MIN_CONFIDENCE:
        return InferenceResult("not_found", None, None, conf)
    return InferenceResult("ai_inferred", desc, cat or None, conf)
```

- [ ] **Step 5: Run to verify pass**

Run: `python3 -m pytest tests/test_ai_inference_fallback.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
pre-commit run --files app/utils/claude_client.py app/services/ai_inference_fallback.py tests/test_ai_inference_fallback.py
git add app/utils/claude_client.py app/services/ai_inference_fallback.py tests/test_ai_inference_fallback.py
git commit -m "feat(materials): Opus 4.8 flagged AI inference fallback (description+category only)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: Orchestration — enrich one card end-to-end

**Files:**
- Modify: `app/services/authoritative_enrichment_service.py`
- Test: `tests/test_authoritative_enrichment.py` (add)

- [ ] **Step 1: Write failing orchestration tests**

Add to `tests/test_authoritative_enrichment.py`:

```python
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from app.models import MaterialCard
from app.services.authoritative_enrichment_service import enrich_card


def _card(db_session, mpn="LM317T"):
    from app.utils.normalization import normalize_mpn_key

    c = MaterialCard(
        normalized_mpn=normalize_mpn_key(mpn),
        display_mpn=mpn,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(c)
    db_session.flush()
    return c


class _FakeConn:
    def __init__(self, source_name, hits):
        self.source_name = source_name
        self._hits = hits

    async def search(self, pn):
        return self._hits


@patch("app.services.authoritative_enrichment_service._connectors_in_order")
def test_enrich_card_verified(mock_conns, db_session):
    card = _card(db_session)
    mock_conns.return_value = [
        _FakeConn("digikey", [{
            "source_type": "digikey", "mpn_matched": "LM317T",
            "manufacturer": "TI", "description": "Adjustable regulator",
            "category": "Voltage Regulator", "lifecycle_status": "active",
        }])
    ]
    import asyncio
    asyncio.run(enrich_card(card, db_session))
    assert card.enrichment_status == "verified"
    assert card.manufacturer == "TI"
    assert card.enrichment_provenance["description"]["source"] == "digikey"


@patch("app.services.ai_inference_fallback.claude_structured", new_callable=AsyncMock)
@patch("app.services.authoritative_enrichment_service._connectors_in_order")
def test_enrich_card_ai_inferred_when_no_authoritative(mock_conns, mock_claude, db_session):
    card = _card(db_session, "04M3HJ")
    mock_conns.return_value = [_FakeConn("digikey", [])]  # no hits anywhere
    mock_claude.return_value = {"description": "Dell laptop bezel", "category": "Mechanical", "confidence": 0.7}
    import asyncio
    asyncio.run(enrich_card(card, db_session))
    assert card.enrichment_status == "ai_inferred"
    assert card.description == "Dell laptop bezel"
    assert card.lifecycle_status is None  # never guessed


@patch("app.services.ai_inference_fallback.claude_structured", new_callable=AsyncMock)
@patch("app.services.authoritative_enrichment_service._connectors_in_order")
def test_enrich_card_not_found(mock_conns, mock_claude, db_session):
    card = _card(db_session, "ZZ9PLURAL")
    mock_conns.return_value = [_FakeConn("digikey", [])]
    mock_claude.return_value = {"description": "", "category": "", "confidence": 0.0}
    import asyncio
    asyncio.run(enrich_card(card, db_session))
    assert card.enrichment_status == "not_found"
    assert card.description is None
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_authoritative_enrichment.py -v`
Expected: FAIL — `enrich_card` not defined.

- [ ] **Step 3: Implement `enrich_card` + batch driver**

Append to `app/services/authoritative_enrichment_service.py`:

```python
async def enrich_card(card: MaterialCard, db: Session, *, connectors: list | None = None, refresh: bool = False) -> str:
    """Enrich one card: authoritative -> flagged AI inference -> not_found.

    Returns the resulting enrichment_status. Does not commit (caller controls txn).
    """
    if card.enrichment_status == "verified" and not refresh:
        return "verified"

    conns = connectors if connectors is not None else _connectors_in_order(db)
    results = await fetch_authoritative(card.display_mpn, card.normalized_mpn, conns)
    merged, provenance, contributors = merge_authoritative(card.normalized_mpn, results)

    if merged:
        apply_authoritative(card, merged, provenance, contributors)
        return "verified"

    # No authoritative hit -> flagged inference
    from app.services.ai_inference_fallback import infer_part

    inf = await infer_part(card.display_mpn)
    now = datetime.now(timezone.utc)
    card.enriched_at = now
    if inf.status == "ai_inferred":
        card.description = inf.description
        card.category = inf.category
        card.enrichment_source = "claude_opus_inferred"
        card.enrichment_status = "ai_inferred"
        card.enrichment_provenance = {
            "description": {"source": "claude_opus_inferred", "confidence": inf.confidence, "fetched_at": now.isoformat()}
        }
        return "ai_inferred"

    card.enrichment_status = "not_found"
    card.enrichment_source = card.enrichment_source or "claude_opus_inferred"
    return "not_found"


async def enrich_cards(card_ids: list[int], db: Session, *, concurrency: int = 5, refresh: bool = False) -> dict:
    """Enrich many cards with bounded concurrency. Commits in batches of 50."""
    conns = _connectors_in_order(db)
    sem = asyncio.Semaphore(concurrency)
    counts = {"verified": 0, "ai_inferred": 0, "not_found": 0}

    async def _one(cid: int) -> None:
        card = db.query(MaterialCard).get(cid)
        if card is None:
            return
        async with sem:
            status = await enrich_card(card, db, connectors=conns, refresh=refresh)
        counts[status] = counts.get(status, 0) + 1

    for i in range(0, len(card_ids), 50):
        batch = card_ids[i : i + 50]
        await asyncio.gather(*(_one(c) for c in batch))
        db.commit()
        logger.info("AUTH_ENRICH: committed {}/{} cards", min(i + 50, len(card_ids)), len(card_ids))
    return counts
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/test_authoritative_enrichment.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
pre-commit run --files app/services/authoritative_enrichment_service.py tests/test_authoritative_enrichment.py
git add app/services/authoritative_enrichment_service.py tests/test_authoritative_enrichment.py
git commit -m "feat(materials): orchestrate per-card enrich (verified -> ai_inferred -> not_found)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 6: HTML-table parser + `parse_tabular_file` sniff + MPN extraction

The input file is `text/html` with an `.xls` extension; `openpyxl` will fail on it. Add an HTML branch and a single-column MPN extractor.

**Files:**
- Modify: `app/file_utils.py`
- Test: `tests/test_html_table_parse.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_html_table_parse.py`:

```python
from app.file_utils import extract_mpns, parse_tabular_file

_HTML = (
    b"<head><META http-equiv=\"Content-Type\" content=\"text/html; charset=ISO-8859-1\"></head>"
    b"<table><tr><td>Material: Material Name</td></tr>"
    b"<tr><td>M393A2K43EB3-CWEB/C</td></tr>"
    b"<tr><td>04M3HJ</td></tr>"
    b"<tr><td></td></tr>"
    b"<tr><td>LTM8053IY#PBF</td></tr></table>"
)


def test_parse_html_disguised_as_xls():
    rows = parse_tabular_file(_HTML, "report1780605266325.xls")
    # header lowercased+stripped becomes the dict key
    assert len(rows) == 3  # blank row dropped
    assert rows[0]["material: material name"] == "M393A2K43EB3-CWEB/C"


def test_extract_mpns_single_column():
    rows = parse_tabular_file(_HTML, "report.xls")
    mpns = extract_mpns(rows)
    assert mpns == ["M393A2K43EB3-CWEB/C", "04M3HJ", "LTM8053IY#PBF"]


def test_extract_mpns_named_column():
    rows = [{"part number": "ABC123"}, {"part number": "DEF456"}]
    assert extract_mpns(rows) == ["ABC123", "DEF456"]
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_html_table_parse.py -v`
Expected: FAIL — HTML not handled / `extract_mpns` undefined.

- [ ] **Step 3: Implement HTML sniff + parser + extractor**

In `app/file_utils.py`, add an HTML parser and route to it. Add near the other parsers:

```python
def _looks_like_html(content: bytes) -> bool:
    head = content[:512].lstrip().lower()
    return head.startswith((b"<head", b"<html", b"<table", b"<!doctype", b"<meta"))


def _parse_html_table(content: bytes) -> list[dict]:
    """Parse an HTML <table> export (e.g. ERP 'Excel' that is really HTML)."""
    from html.parser import HTMLParser

    class _T(HTMLParser):
        def __init__(self):
            super().__init__()
            self.rows: list[list[str]] = []
            self._cur: list[str] | None = None
            self._cell: list[str] | None = None

        def handle_starttag(self, tag, attrs):
            if tag == "tr":
                self._cur = []
            elif tag in ("td", "th"):
                self._cell = []

        def handle_endtag(self, tag):
            if tag == "tr" and self._cur is not None:
                self.rows.append(self._cur)
                self._cur = None
            elif tag in ("td", "th") and self._cell is not None and self._cur is not None:
                self._cur.append("".join(self._cell).strip())
                self._cell = None

        def handle_data(self, data):
            if self._cell is not None:
                self._cell.append(data)

    try:
        text = content.decode("iso-8859-1")
    except Exception:
        text = content.decode("utf-8", errors="replace")
    p = _T()
    p.feed(text)
    table = [r for r in p.rows if any(c.strip() for c in r)]
    if not table:
        return []
    headers = [str(c or "").strip().lower() for c in table[0]]
    out = []
    for row in table[1:]:
        if not any(c.strip() for c in row):
            continue
        out.append(dict(zip(headers, [str(v or "").strip() for v in row])))
    return out
```

Then update `parse_tabular_file` so the `.xls`/`.xlsx` branch falls back to HTML when the bytes are HTML:

```python
    try:
        if fname.endswith((".xlsx", ".xls")):
            if _looks_like_html(content):
                rows = _parse_html_table(content)
            else:
                rows = _parse_excel(content)
        elif _looks_like_html(content):
            rows = _parse_html_table(content)
        else:
            delimiter = "\t" if fname.endswith(".tsv") else ","
            rows = _parse_csv(content, delimiter)
    except Exception as e:
        logger.warning(f"File parse error ({filename}): {e}")
```

Add the MPN extractor:

```python
_MPN_COLUMN_NAMES = (
    "material: material name",
    "material name",
    "mpn",
    "part number",
    "part_number",
    "partnumber",
    "pn",
    "part#",
)


def extract_mpns(rows: list[dict]) -> list[str]:
    """Pull part numbers from parsed rows.

    Prefers a recognized column name; otherwise uses the single column present.
    Preserves order, drops blanks.
    """
    if not rows:
        return []
    keys = list(rows[0].keys())
    col = next((k for k in keys if k in _MPN_COLUMN_NAMES), None)
    if col is None and len(keys) == 1:
        col = keys[0]
    if col is None:
        return []
    out = []
    for r in rows:
        v = (r.get(col) or "").strip()
        if v:
            out.append(v)
    return out
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/test_html_table_parse.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
pre-commit run --files app/file_utils.py tests/test_html_table_parse.py
git add app/file_utils.py tests/test_html_table_parse.py
git commit -m "feat(import): parse HTML-table-as-xls + extract_mpns helper

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 7: Import endpoint + script with dry-run coverage report

**Files:**
- Modify: `app/routers/materials.py`
- Create: `scripts/import_part_numbers.py`
- Test: `tests/test_part_number_import.py`

- [ ] **Step 1: Write failing endpoint test**

Create `tests/test_part_number_import.py`:

```python
import io


def test_import_part_numbers_creates_bare_cards(client, db_session):
    from app.models import MaterialCard

    html = (
        b"<table><tr><td>Material: Material Name</td></tr>"
        b"<tr><td>NEWPART-001</td></tr><tr><td>NEWPART-002</td></tr></table>"
    )
    resp = client.post(
        "/api/materials/import-part-numbers",
        files={"file": ("report.xls", io.BytesIO(html), "application/vnd.ms-excel")},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["created"] == 2
    cards = db_session.query(MaterialCard).filter(
        MaterialCard.normalized_mpn.in_(["newpart001", "newpart002"])
    ).all()
    assert len(cards) == 2
    assert all(c.enrichment_status == "unenriched" for c in cards)
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_part_number_import.py -v`
Expected: FAIL — 404 (endpoint not defined).

- [ ] **Step 3: Implement the endpoint**

In `app/routers/materials.py`, add (near the existing `import_stock_list_standalone`):

```python
@router.post("/api/materials/import-part-numbers")
async def import_part_numbers(
    request: Request, user: User = Depends(require_buyer), db: Session = Depends(get_db)
):
    """Import a bare list of part numbers (one MPN per row) as MaterialCards.

    Accepts CSV/XLSX/TSV and HTML-table-as-.xls. Creates bare cards
    (enrichment_status='unenriched'); enrichment runs separately.
    """
    import os as _os

    from ..file_utils import extract_mpns, parse_tabular_file
    from ..search_service import resolve_material_card

    form = await request.form()
    file = form.get("file")
    if not file:
        raise HTTPException(400, "No file uploaded")
    ext = _os.path.splitext(file.filename or "")[1].lower()
    if ext not in {".csv", ".xlsx", ".xls", ".tsv"}:
        raise HTTPException(400, f"Invalid file type '{ext}'")
    content = await file.read()
    if len(content) > 10_000_000:
        raise HTTPException(413, "File too large -- 10MB maximum")

    rows = parse_tabular_file(content, file.filename or "")
    mpns = extract_mpns(rows)
    if not mpns:
        raise HTTPException(400, "No part numbers found in file")

    created = existing = skipped = 0
    for mpn in mpns:
        card = resolve_material_card(mpn, db)
        if card is None:
            skipped += 1
            continue
        # resolve_material_card logs created vs resolved; detect new by enrichment_status default
        if card.enrichment_status == "unenriched" and card.enriched_at is None and card.search_count == 0:
            created += 1
        else:
            existing += 1
    db.commit()
    return {
        "created": created,
        "existing": existing,
        "skipped": skipped,
        "total_rows": len(mpns),
    }
```

> Note: `created` vs `existing` is approximate (relies on default state); the dry-run report in the script is the source of truth for coverage. If exact created-count matters, have `resolve_material_card` return a created flag — out of scope here.

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/test_part_number_import.py -v`
Expected: PASS.

- [ ] **Step 5: Write the importer/enricher script with dry-run report**

Create `scripts/import_part_numbers.py`:

```python
"""Import a bare part-number file and run verified enrichment.

Usage:
  python3 scripts/import_part_numbers.py --file "/path/report.xls" --dry-run
  python3 scripts/import_part_numbers.py --file "/path/report.xls" --commit
"""

import argparse
import asyncio
import csv
import sys
from datetime import datetime, timezone

sys.path.insert(0, "/root/availai")

from loguru import logger  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.file_utils import extract_mpns, parse_tabular_file  # noqa: E402
from app.models import MaterialCard  # noqa: E402
from app.services.authoritative_enrichment_service import (  # noqa: E402
    _connectors_in_order,
    enrich_card,
)
from app.utils.normalization import normalize_mpn_key  # noqa: E402

_REPORT_COLS = [
    "input_mpn", "normalized_mpn", "status", "source",
    "manufacturer", "category", "lifecycle_status", "package_type",
    "pin_count", "rohs_status", "description", "datasheet_url", "notes",
]


async def _run(file_path: str, commit: bool, report_path: str, refresh: bool) -> None:
    db = SessionLocal()
    try:
        content = open(file_path, "rb").read()
        mpns = extract_mpns(parse_tabular_file(content, file_path))
        logger.info("Parsed {} part numbers from {}", len(mpns), file_path)

        conns = _connectors_in_order(db)
        rows = []
        counts = {"verified": 0, "ai_inferred": 0, "not_found": 0}

        for i, raw in enumerate(mpns):
            norm = normalize_mpn_key(raw)
            if not norm:
                rows.append({"input_mpn": raw, "normalized_mpn": "", "status": "skipped", "notes": "unparseable mpn"})
                continue
            # In dry-run, use a transient card not added to the session.
            card = db.query(MaterialCard).filter_by(normalized_mpn=norm).first()
            transient = card is None
            if transient:
                card = MaterialCard(normalized_mpn=norm, display_mpn=raw.strip(),
                                    created_at=datetime.now(timezone.utc))
            status = await enrich_card(card, db, connectors=conns, refresh=refresh)
            counts[status] = counts.get(status, 0) + 1
            prov = card.enrichment_provenance or {}
            rows.append({
                "input_mpn": raw, "normalized_mpn": norm, "status": status,
                "source": (prov.get("description") or {}).get("source", ""),
                "manufacturer": card.manufacturer or "", "category": card.category or "",
                "lifecycle_status": card.lifecycle_status or "", "package_type": card.package_type or "",
                "pin_count": card.pin_count or "", "rohs_status": card.rohs_status or "",
                "description": card.description or "", "datasheet_url": card.datasheet_url or "",
                "notes": "" if not transient else "new card",
            })
            if commit:
                if transient:
                    db.add(card)
                if (i + 1) % 50 == 0:
                    db.commit()
                    logger.info("Committed {}/{}", i + 1, len(mpns))
            if (i + 1) % 25 == 0:
                logger.info("Processed {}/{} ({})", i + 1, len(mpns), counts)

        if commit:
            db.commit()
        else:
            db.rollback()

        with open(report_path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=_REPORT_COLS, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        logger.info("Wrote report -> {}", report_path)
        logger.info("SUMMARY: {} (committed={})", counts, commit)
    finally:
        db.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True)
    ap.add_argument("--commit", action="store_true", help="Write to DB (default is dry-run)")
    ap.add_argument("--refresh", action="store_true", help="Re-enrich already-verified cards")
    ap.add_argument("--report", default=None)
    args = ap.parse_args()
    report = args.report or f"reports/part_import_report_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.csv"
    import os
    os.makedirs(os.path.dirname(report) or ".", exist_ok=True)
    if not args.commit:
        logger.info("DRY RUN — no DB writes. Use --commit to persist.")
    asyncio.run(_run(args.file, args.commit, report, args.refresh))
```

Add `reports/` to `.gitignore` if not already ignored.

- [ ] **Step 6: Commit**

```bash
pre-commit run --files app/routers/materials.py scripts/import_part_numbers.py tests/test_part_number_import.py .gitignore
git add app/routers/materials.py scripts/import_part_numbers.py tests/test_part_number_import.py .gitignore
git commit -m "feat(import): part-number import endpoint + dry-run enrichment script

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 8: `verified_only` filter in faceted search

**Files:**
- Modify: `app/services/faceted_search_service.py:155`, `app/routers/htmx_views.py:7197`
- Test: `tests/test_material_enrichment_status_filter.py`

- [ ] **Step 1: Write failing test**

Create `tests/test_material_enrichment_status_filter.py`:

```python
from datetime import datetime, timezone

from app.models import MaterialCard
from app.services.faceted_search_service import search_materials_faceted


def _mk(db, mpn, status):
    c = MaterialCard(
        normalized_mpn=mpn, display_mpn=mpn.upper(), enrichment_status=status,
        created_at=datetime.now(timezone.utc),
    )
    db.add(c)
    return c


def test_verified_only_filter(db_session):
    _mk(db_session, "verifiedone", "verified")
    _mk(db_session, "guessedone", "ai_inferred")
    _mk(db_session, "missingone", "not_found")
    db_session.flush()

    all_cards, total_all = search_materials_faceted(db_session)
    assert total_all >= 3

    verified, total_v = search_materials_faceted(db_session, verified_only=True)
    assert {c.normalized_mpn for c in verified} == {"verifiedone"}
    assert total_v == 1
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_material_enrichment_status_filter.py -v`
Expected: FAIL — `verified_only` is an unexpected kwarg.

- [ ] **Step 3: Add the param + WHERE clause**

In `app/services/faceted_search_service.py`, add `verified_only: bool = False` to the `search_materials_faceted` keyword args, and after the `manufacturers` filter block add:

```python
    if verified_only:
        query = query.filter(MaterialCard.enrichment_status == "verified")
```

- [ ] **Step 4: Add the query param to the GET handler**

In `app/routers/htmx_views.py`, in `materials_faceted_partial` add the param:

```python
    verified_only: bool = Query(False),
```

and pass `verified_only=verified_only` into the `search_materials_faceted(...)` call.

- [ ] **Step 5: Run to verify pass**

Run: `python3 -m pytest tests/test_material_enrichment_status_filter.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
pre-commit run --files app/services/faceted_search_service.py app/routers/htmx_views.py tests/test_material_enrichment_status_filter.py
git add app/services/faceted_search_service.py app/routers/htmx_views.py tests/test_material_enrichment_status_filter.py
git commit -m "feat(materials): verified-only filter in faceted search

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 9: UI — status badge + "Verified only" toggle

**Files:**
- Modify: `app/templates/htmx/partials/materials/list.html`, `workspace.html`, `app/static/htmx_app.js`

- [ ] **Step 1: Add the status badge column**

In `app/templates/htmx/partials/materials/list.html`, after the lifecycle `<td>` (around line 61), insert a new cell keyed on `enrichment_status`:

```html
          <td>
            {% set es = m.enrichment_status %}
            {% if es == "verified" %}
            <span class="inline-flex px-2 py-0.5 text-xs font-medium rounded-full border bg-emerald-50 text-emerald-700 border-emerald-200"
                  title="Verified from {{ (m.enrichment_provenance or {}).get('description', {}).get('source', 'catalog') }}">
              VERIFIED
            </span>
            {% elif es == "ai_inferred" %}
            <span class="inline-flex px-2 py-0.5 text-xs font-medium rounded-full border bg-amber-50 text-amber-700 border-amber-200"
                  title="AI-inferred — unverified, best-effort guess">
              AI-INFERRED
            </span>
            {% elif es == "not_found" %}
            <span class="inline-flex px-2 py-0.5 text-xs font-medium rounded-full border bg-gray-100 text-gray-500 border-gray-200"
                  title="No authoritative source found this part">
              NOT FOUND
            </span>
            {% else %}
            <span class="text-xs text-gray-400">--</span>
            {% endif %}
          </td>
```

Also add a matching `<th>` header cell (e.g. `Status`) wherever the table header row is defined in this partial.

- [ ] **Step 2: Add the "Verified only" toggle**

In `app/templates/htmx/partials/materials/workspace.html`, after the manufacturer filter container (around line 42), insert:

```html
      {# Verified only toggle #}
      <div class="mb-3">
        <label class="flex items-center gap-2 px-2 py-1.5 rounded hover:bg-gray-50 cursor-pointer text-sm">
          <input type="checkbox"
                 :checked="verifiedOnly"
                 @change="verifiedOnly = !verifiedOnly; applyFilters()"
                 class="rounded border-gray-300 text-emerald-600 focus:ring-emerald-500 h-3.5 w-3.5">
          <span class="text-gray-700 font-medium">Verified only</span>
        </label>
      </div>
```

Add `"verified_only": verifiedOnly` to the `#materials-results` `hx-vals` object in this template (matching the existing `commodity`/`q`/`sub_filters` entries).

- [ ] **Step 3: Add Alpine state + URL sync**

In `app/static/htmx_app.js`, in the `materialsFilter` Alpine component (around lines 550–663):
- add state: `verifiedOnly: false,`
- in `syncFromURL()`: `this.verifiedOnly = params.get('verified_only') === 'true';`
- in `pushURL()`: `if (this.verifiedOnly) params.set('verified_only', 'true'); else params.delete('verified_only');`

- [ ] **Step 4: Build + smoke check**

Run: `npm run build`
Expected: build succeeds; new Tailwind classes (`bg-emerald-50`, `bg-amber-50`, etc. — all already used by `lc_colors`) are present. If any class is missing, check the Tailwind safelist (per project deploy notes).

Optional console-error check (authenticated) per the e2e harness in `tests/e2e/conftest.py`: load the materials page, toggle "Verified only", assert no console errors and the list refreshes.

- [ ] **Step 5: Commit**

```bash
pre-commit run --files app/templates/htmx/partials/materials/list.html app/templates/htmx/partials/materials/workspace.html app/static/htmx_app.js
git add app/templates/htmx/partials/materials/list.html app/templates/htmx/partials/materials/workspace.html app/static/htmx_app.js
git commit -m "feat(materials-ui): enrichment-status badge + verified-only toggle

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 10: Full verification, dry-run, docs, deploy

- [ ] **Step 1: Whole-suite + all-files gate**

Run: `pre-commit run --all-files`
Run: `python3 -m pytest tests/ -q`
Expected: all green. Fix any regressions before proceeding.

- [ ] **Step 2: Dry-run the real list and review coverage**

Run: `python3 scripts/import_part_numbers.py --file "/root/Material Items/report1780605266325.xls"`
Expected: a `reports/part_import_report_*.csv` with 1,827 data rows and a SUMMARY line of `verified / ai_inferred / not_found` counts. **Review with the user** (esp. the verified vs inferred split and the Nexar call volume) before committing anything to the DB.

- [ ] **Step 3: Commit to the DB (after user approval of the report)**

First create the cards via the endpoint or directly, then enrich:
Run: `python3 scripts/import_part_numbers.py --file "/root/Material Items/report1780605266325.xls" --commit`
Expected: cards created + enriched; SUMMARY logged.

- [ ] **Step 4: Update APP_MAP docs**

- `docs/APP_MAP_DATABASE.md`: document `material_cards.enrichment_status` + `enrichment_provenance`.
- `docs/APP_MAP_ARCHITECTURE.md`: document `authoritative_enrichment_service` + `ai_inference_fallback` + connector core-attribute extension.
- `docs/APP_MAP_INTERACTIONS.md`: document `POST /api/materials/import-part-numbers` + the verified-only filter.

Commit the docs.

- [ ] **Step 5: Deploy (UI changes need a fresh build)**

Run: `./deploy.sh --no-cache` (per the project deploy rule — Docker has cached stale templates before).
Verify the badge + "Verified only" toggle render on the live materials page; confirm no DNS `could not translate host name "db"` crash-loop (if it occurs: `docker compose down && up`).

- [ ] **Step 6: Open the PR**

```bash
git push -u origin feat/verified-material-enrichment
```
Open a PR summarizing the verified-enrichment capability + import; run the PR-review agents.

---

## Self-Review Checklist (completed by plan author)

- **Spec coverage:** §2 decisions → Tasks 1–9; §3 columns → Task 1; §4.1 connectors → Task 2; §4.2 service → Tasks 3/5; §4.3 fallback → Task 4; §4.4 import → Tasks 6/7; §4.5 filter/UI → Tasks 8/9; §6 report → Task 7; §7 errors → Task 3 (`fetch_authoritative` try/except) + §quota handled as empty results; §8 tests → each task; §9 rollout → Task 10. ✅
- **Exact-match guard** (spec's primary accuracy lever) → Task 3 `merge_authoritative` + test. ✅
- **Opus 4.8 "best version"** → Task 4 Step 1 bump + `model_tier="opus"`. ✅
- **AI never fabricates structured specs** → Task 4 (description+category only) + Task 5 `test_enrich_card_ai_inferred` asserts `lifecycle_status is None`. ✅
- **Known follow-up (not a blocker):** `import_part_numbers` created-count is approximate; the dry-run report is the authoritative coverage source (noted inline in Task 7).
- **Type consistency:** `enrich_card`/`enrich_cards`, `merge_authoritative`, `fetch_authoritative`, `apply_authoritative`, `infer_part`/`InferenceResult`, `extract_mpns`, `parse_tabular_file` used consistently across tasks. ✅
