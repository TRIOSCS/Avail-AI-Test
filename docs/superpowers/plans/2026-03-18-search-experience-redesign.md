# Search Experience Redesign — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild the search tab with SSE streaming, aggressive vendor dedup, shortlist + batch actions, and a polished industrial-precision design.

**Architecture:** The search form POST returns a results shell with SSE connection. A background task fires connectors via `asyncio.wait(FIRST_COMPLETED)` and publishes results through the existing SSE broker. Cards stream in as vendor-grouped, aggressively deduped partials. Users shortlist results and batch-add to requisitions or create RFQs.

**Tech Stack:** FastAPI, SSE (sse-starlette, existing broker), HTMX 2.x SSE extension, Alpine.js 3.x stores, Jinja2 partials, Tailwind CSS, DM Sans + JetBrains Mono fonts.

**Spec:** `docs/superpowers/specs/2026-03-18-search-experience-redesign.md`

**Notes:**
- Redis caching uses `_get_search_redis()` from `search_service.py` (lazy-init pattern), NOT `app/cache/redis_client`
- `asyncio` is not imported at module level in `htmx_views.py` — use `import asyncio` locally where needed
- AI web search connector has special conditional trigger logic and is intentionally excluded from `_build_connectors()` — streaming search does not use AI fallback (it can be added in Sub-project #2)
- Sighting model stores `vendor_url`, `click_url`, `octopart_url`, `vendor_sku` in `raw_data` JSON, not as direct columns (intentional deviation from spec to avoid migration)
- `TestClient` must be created per-test or use a fixture that overrides `get_db` to return the test `db_session` — never use module-level `TestClient`

---

## File Map

### Modified Files
| File | Responsibility | Changes |
|------|---------------|---------|
| `app/search_service.py` | Search orchestration | Extract `_build_connectors()`, add `_deduplicate_sightings_aggressive()`, add `_incremental_dedup()`, add `stream_search_mpn()` |
| `app/routers/htmx_views.py` | Search routes | Modify `search_run` to return shell, add `search_stream` SSE route, add `add_to_requisition` route, add `search_filter` route |
| `app/connectors/sources.py` | Base connector | Add `source_name` property to `BaseConnector` |
| `app/static/htmx_app.js` | Alpine.js setup | Add `shortlist` store |
| `app/templates/htmx/partials/search/form.html` | Search input | Design polish |
| `app/templates/htmx/partials/search/results.html` | Full rewrite → vendor cards | Replaced by new card-based layout |
| `app/templates/htmx/partials/search/lead_detail.html` | Detail drawer | Tighten CSS, add shortlist button, show all vendor offers |

### New Files
| File | Responsibility |
|------|---------------|
| `app/templates/htmx/partials/search/results_shell.html` | Streaming container: progress chips + SSE connect + results div |
| `app/templates/htmx/partials/search/vendor_card.html` | Single vendor card partial (rendered per SSE event) |
| `app/templates/htmx/partials/search/shortlist_bar.html` | Sticky bottom action bar |
| `app/templates/htmx/partials/search/requisition_picker_modal.html` | Modal for selecting/creating requisition |
| `tests/test_search_streaming.py` | All new tests for this feature |

---

## Task 1: Add `source_name` to BaseConnector

**Files:**
- Modify: `app/connectors/sources.py:78-150` (BaseConnector)
- Test: `tests/test_search_streaming.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_search_streaming.py
"""Tests for search streaming, aggressive dedup, and shortlist features.

Called by: pytest
Depends on: app/search_service.py, app/connectors/sources.py
"""
import pytest
from app.connectors.sources import NexarConnector, BrokerBinConnector

def test_base_connector_has_source_name():
    """Each connector exposes a source_name property matching its source_type."""
    # NexarConnector uses source_type "octopart" in results
    nexar = NexarConnector.__new__(NexarConnector)
    assert hasattr(nexar, 'source_name')
    assert isinstance(nexar.source_name, str)
    assert len(nexar.source_name) > 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_search_streaming.py::test_base_connector_has_source_name -v`
Expected: FAIL — `AttributeError: 'NexarConnector' object has no attribute 'source_name'`

- [ ] **Step 3: Add source_name property to BaseConnector and each subclass**

In `app/connectors/sources.py`, add a class-level `source_name` attribute to `BaseConnector`:

```python
class BaseConnector(ABC):
    source_name: str = "unknown"  # Override in subclasses
    # ... rest unchanged
```

Then set it on each subclass (same file + other connector files):
- `NexarConnector.source_name = "nexar"` (sources.py)
- `BrokerBinConnector.source_name = "brokerbin"` (sources.py)
- `DigiKeyConnector.source_name = "digikey"` (digikey.py)
- `MouserConnector.source_name = "mouser"` (mouser.py)
- `OEMSecretsConnector.source_name = "oemsecrets"` (oemsecrets.py)
- `Element14Connector.source_name = "element14"` (element14.py)
- `EbayConnector.source_name = "ebay"` (ebay.py)
- `SourcengineConnector.source_name = "sourcengine"` (sourcengine.py)
- `AIWebSearchConnector.source_name = "ai_live_web"` (ai_live_web.py)

- [ ] **Step 4: Run test to verify it passes**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_search_streaming.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/connectors/sources.py app/connectors/digikey.py app/connectors/mouser.py app/connectors/oemsecrets.py app/connectors/element14.py app/connectors/ebay.py app/connectors/sourcengine.py app/connectors/ai_live_web.py tests/test_search_streaming.py
git commit -m "feat(search): add source_name to all connectors"
```

---

## Task 2: Extract `_build_connectors()` from `_fetch_fresh()`

**Files:**
- Modify: `app/search_service.py:568-637`
- Test: `tests/test_search_streaming.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_search_streaming.py — add to existing file
from unittest.mock import patch, MagicMock
from app.search_service import _build_connectors

def test_build_connectors_returns_connectors_and_stats(db_session):
    """_build_connectors returns (connectors_list, source_stats_map) with disabled sources skipped."""
    # Mock all credentials to return None (all sources skipped)
    with patch("app.search_service.get_credential", return_value=None):
        connectors, stats = _build_connectors(db_session)

    assert isinstance(connectors, list)
    assert isinstance(stats, dict)
    # With no credentials, all should be skipped
    assert len(connectors) == 0
    # Stats should have entries for skipped sources
    assert any(s["status"] in ("skipped", "disabled") for s in stats.values())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_search_streaming.py::test_build_connectors_returns_connectors_and_stats -v`
Expected: FAIL — `ImportError: cannot import name '_build_connectors'`

- [ ] **Step 3: Extract `_build_connectors()` from `_fetch_fresh()`**

In `app/search_service.py`, extract lines 574-637 into a new function:

```python
def _build_connectors(db: Session) -> tuple[list, dict[str, dict]]:
    """Build enabled connectors with credentials, returning (connectors, source_stats_map).

    Checks disabled sources in DB, loads credentials per-connector.
    Shared by _fetch_fresh() (batch) and stream_search_mpn() (streaming).

    Called by: _fetch_fresh, stream_search_mpn
    Depends on: services/credential_service, connectors/*, models.ApiSource
    """
    from .services.credential_service import get_credential

    disabled_sources = set()
    for src in db.query(ApiSource).filter_by(status="disabled").all():
        disabled_sources.add(src.name)

    connectors = []
    source_stats_map: dict[str, dict] = {}

    def _cred(source_name, var_name):
        return get_credential(db, source_name, var_name)

    def _add_or_skip(source_name, has_creds, connector_factory):
        if source_name in disabled_sources:
            source_stats_map[source_name] = {
                "source": source_name, "results": 0, "ms": 0,
                "error": None, "status": "disabled",
            }
        elif not has_creds:
            source_stats_map[source_name] = {
                "source": source_name, "results": 0, "ms": 0,
                "error": "No API key configured", "status": "skipped",
            }
        else:
            connectors.append(connector_factory())

    # All 8 connector setups — same as current lines 608-637
    nexar_id = _cred("nexar", "NEXAR_CLIENT_ID")
    nexar_sec = _cred("nexar", "NEXAR_CLIENT_SECRET")
    octopart_key = _cred("nexar", "OCTOPART_API_KEY")
    _add_or_skip("nexar", nexar_id and nexar_sec or octopart_key,
                 lambda: NexarConnector(nexar_id, nexar_sec, octopart_key))

    bb_key = _cred("brokerbin", "BROKERBIN_API_KEY")
    bb_sec = _cred("brokerbin", "BROKERBIN_API_SECRET")
    _add_or_skip("brokerbin", bb_key, lambda: BrokerBinConnector(bb_key, bb_sec))

    ebay_id = _cred("ebay", "EBAY_CLIENT_ID")
    ebay_sec = _cred("ebay", "EBAY_CLIENT_SECRET")
    _add_or_skip("ebay", ebay_id and ebay_sec, lambda: EbayConnector(ebay_id, ebay_sec))

    dk_id = _cred("digikey", "DIGIKEY_CLIENT_ID")
    dk_sec = _cred("digikey", "DIGIKEY_CLIENT_SECRET")
    _add_or_skip("digikey", dk_id and dk_sec, lambda: DigiKeyConnector(dk_id, dk_sec))

    mouser_key = _cred("mouser", "MOUSER_API_KEY")
    _add_or_skip("mouser", mouser_key, lambda: MouserConnector(mouser_key))

    oem_key = _cred("oemsecrets", "OEMSECRETS_API_KEY")
    _add_or_skip("oemsecrets", oem_key, lambda: OEMSecretsConnector(oem_key))

    src_key = _cred("sourcengine", "SOURCENGINE_API_KEY")
    _add_or_skip("sourcengine", src_key, lambda: SourcengineConnector(src_key))

    e14_key = _cred("element14", "ELEMENT14_API_KEY")
    _add_or_skip("element14", e14_key, lambda: Element14Connector(e14_key))

    return connectors, source_stats_map
```

Then update `_fetch_fresh()` to call it. Note: `_build_connectors` also returns `disabled_sources` so `_fetch_fresh` can still handle the AI connector:

```python
def _build_connectors(db: Session) -> tuple[list, dict[str, dict], set[str]]:
    # ... returns (connectors, source_stats_map, disabled_sources)
```

```python
async def _fetch_fresh(pns, db):
    connectors, source_stats_map, disabled_sources = _build_connectors(db)
    # AI connector setup (lines 639-660) stays in _fetch_fresh — uses disabled_sources
    # ... rest of _fetch_fresh unchanged (the gather + per-connector execution)
```

- [ ] **Step 4: Run full test suite to verify no regressions**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v --tb=short -q`
Expected: All existing tests PASS, new test PASS

- [ ] **Step 5: Commit**

```bash
git add app/search_service.py tests/test_search_streaming.py
git commit -m "refactor(search): extract _build_connectors from _fetch_fresh"
```

---

## Task 3: Aggressive Deduplication + Incremental Dedup

**Files:**
- Modify: `app/search_service.py`
- Test: `tests/test_search_streaming.py`

- [ ] **Step 1: Write failing tests for aggressive dedup**

```python
# tests/test_search_streaming.py — add to existing file
from app.search_service import _deduplicate_sightings_aggressive, _incremental_dedup

def test_aggressive_dedup_groups_by_vendor():
    """Same vendor with different prices should merge into one entry with sub_offers."""
    sightings = [
        {"vendor_name": "Arrow", "mpn_matched": "LM317T", "unit_price": 0.45,
         "qty_available": 1000, "score": 80, "confidence": 0.8, "source_type": "nexar",
         "is_authorized": True, "moq": 1},
        {"vendor_name": "Arrow", "mpn_matched": "LM317T", "unit_price": 0.48,
         "qty_available": 500, "score": 70, "confidence": 0.7, "source_type": "digikey",
         "is_authorized": True, "moq": 10},
        {"vendor_name": "Mouser", "mpn_matched": "LM317T", "unit_price": 0.50,
         "qty_available": 2000, "score": 75, "confidence": 0.75, "source_type": "mouser",
         "is_authorized": True, "moq": 1},
    ]
    result = _deduplicate_sightings_aggressive(sightings)

    # Should produce 2 entries: Arrow (merged) and Mouser
    assert len(result) == 2
    arrow = next(r for r in result if "arrow" in r["vendor_name"].lower())
    assert arrow["unit_price"] == 0.45  # best offer (highest score)
    assert arrow["qty_available"] == 1500  # summed
    assert len(arrow["sub_offers"]) == 1  # the other Arrow offer
    assert arrow["offer_count"] == 2
    assert "nexar" in arrow["sources_found"]
    assert "digikey" in arrow["sources_found"]


def test_aggressive_dedup_filters_zero_qty():
    """Sightings with qty_available=0 are excluded."""
    sightings = [
        {"vendor_name": "Arrow", "mpn_matched": "LM317T", "unit_price": 0.45,
         "qty_available": 0, "score": 80, "confidence": 0.8, "source_type": "nexar",
         "is_authorized": True},
    ]
    result = _deduplicate_sightings_aggressive(sightings)
    assert len(result) == 0


def test_incremental_dedup_new_vendor():
    """New vendor results in new_cards list."""
    existing = []
    incoming = [
        {"vendor_name": "Arrow", "mpn_matched": "LM317T", "unit_price": 0.45,
         "qty_available": 1000, "score": 80, "source_type": "nexar"},
    ]
    new_cards, updated_cards = _incremental_dedup(incoming, existing)
    assert len(new_cards) == 1
    assert len(updated_cards) == 0


def test_incremental_dedup_existing_vendor():
    """Existing vendor results in updated_cards list with merged sub_offers."""
    existing = [
        {"vendor_name": "Arrow", "mpn_matched": "LM317T", "unit_price": 0.45,
         "qty_available": 1000, "score": 80, "source_type": "nexar",
         "sub_offers": [], "offer_count": 1, "sources_found": {"nexar"}},
    ]
    incoming = [
        {"vendor_name": "Arrow", "mpn_matched": "LM317T", "unit_price": 0.48,
         "qty_available": 500, "score": 70, "source_type": "digikey"},
    ]
    new_cards, updated_cards = _incremental_dedup(incoming, existing)
    assert len(new_cards) == 0
    assert len(updated_cards) == 1
    assert updated_cards[0]["offer_count"] == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_search_streaming.py -k "dedup" -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Implement `_deduplicate_sightings_aggressive()` and `_incremental_dedup()`**

Add to `app/search_service.py` after the existing `_deduplicate_sightings()`:

```python
def _deduplicate_sightings_aggressive(sighting_dicts: list[dict]) -> list[dict]:
    """Aggressive dedup: one entry per vendor+MPN. All price variants become sub_offers.

    Used by the search tab (not requisition search which uses _deduplicate_sightings).

    Called by: stream_search_mpn, search_run (search tab only)
    Depends on: vendor_utils.normalize_vendor_name
    """
    groups: dict[tuple, list[dict]] = {}

    for d in sighting_dicts:
        qty = d.get("qty_available")
        if qty is not None and qty == 0:
            continue

        vendor = normalize_vendor_name((d.get("vendor_name") or "").strip())
        mpn = (d.get("mpn_matched") or "").strip().lower()
        key = (vendor, mpn)
        groups.setdefault(key, []).append(d)

    results = []
    for group in groups.values():
        group.sort(key=lambda x: (x.get("score", 0), x.get("confidence", 0)), reverse=True)
        best = dict(group[0])

        # Sum quantities
        known_qtys = [g["qty_available"] for g in group if g.get("qty_available") is not None]
        best["qty_available"] = sum(known_qtys) if known_qtys else None

        # Best confidence
        best["confidence"] = max((g.get("confidence") or 0) for g in group)

        # Lowest MOQ
        moqs = [g["moq"] for g in group if g.get("moq")]
        if moqs:
            best["moq"] = min(moqs)

        # Collect sources
        best["sources_found"] = {g.get("source_type", "") for g in group}
        best["sources_found"].discard("")

        # Sub-offers (everything except the best)
        best["sub_offers"] = group[1:] if len(group) > 1 else []
        best["offer_count"] = len(group)

        results.append(best)

    results.sort(key=lambda x: (x.get("score", 0), x.get("confidence", 0)), reverse=True)
    return results


def _incremental_dedup(
    incoming: list[dict], existing: list[dict]
) -> tuple[list[dict], list[dict]]:
    """Dedup incoming results against already-sent cards.

    Returns (new_cards, updated_cards) where:
    - new_cards: vendors not yet seen — append to DOM
    - updated_cards: vendors already sent — OOB swap to update card

    Mutates existing list in-place (adds new entries, updates existing ones).

    Called by: _run_streaming_search
    Depends on: vendor_utils.normalize_vendor_name
    """
    # Build lookup of existing cards by (vendor, mpn)
    existing_map: dict[tuple, dict] = {}
    for card in existing:
        vendor = normalize_vendor_name((card.get("vendor_name") or "").strip())
        mpn = (card.get("mpn_matched") or "").strip().lower()
        existing_map[(vendor, mpn)] = card

    new_cards = []
    updated_cards = []

    for item in incoming:
        qty = item.get("qty_available")
        if qty is not None and qty == 0:
            continue

        vendor = normalize_vendor_name((item.get("vendor_name") or "").strip())
        mpn = (item.get("mpn_matched") or "").strip().lower()
        key = (vendor, mpn)

        if key in existing_map:
            # Merge into existing card
            card = existing_map[key]
            card.setdefault("sub_offers", []).append(item)
            card["offer_count"] = card.get("offer_count", 1) + 1
            card.setdefault("sources_found", set()).add(item.get("source_type", ""))

            # Update best offer if incoming is better
            if (item.get("score", 0) > card.get("score", 0)):
                # Swap: current best becomes sub_offer, incoming becomes primary
                old_best = {k: v for k, v in card.items()
                           if k not in ("sub_offers", "offer_count", "sources_found")}
                card["sub_offers"].append(old_best)
                card["sub_offers"].remove(item)
                for k, v in item.items():
                    if k not in ("sub_offers", "offer_count", "sources_found"):
                        card[k] = v

            # Re-sum quantities
            all_offers = [card] + card.get("sub_offers", [])
            known_qtys = [o["qty_available"] for o in all_offers if o.get("qty_available") is not None]
            card["qty_available"] = sum(known_qtys) if known_qtys else None

            updated_cards.append(card)
        else:
            # New vendor card
            new_card = dict(item)
            new_card["sub_offers"] = []
            new_card["offer_count"] = 1
            new_card["sources_found"] = {item.get("source_type", "")}
            new_card["sources_found"].discard("")
            existing.append(new_card)
            existing_map[key] = new_card
            new_cards.append(new_card)

    return new_cards, updated_cards
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_search_streaming.py -k "dedup" -v`
Expected: All 4 dedup tests PASS

- [ ] **Step 5: Commit**

```bash
git add app/search_service.py tests/test_search_streaming.py
git commit -m "feat(search): add aggressive vendor dedup and incremental dedup"
```

---

## Task 4: Streaming Search Generator + SSE Route

**Files:**
- Modify: `app/search_service.py`
- Modify: `app/routers/htmx_views.py:2095-2210`
- Create: `app/templates/htmx/partials/search/results_shell.html`
- Create: `app/templates/htmx/partials/search/vendor_card.html`
- Test: `tests/test_search_streaming.py`

- [ ] **Step 1: Write failing test for the streaming search generator**

```python
# tests/test_search_streaming.py — add
import asyncio
from unittest.mock import patch, AsyncMock, MagicMock

@pytest.mark.asyncio
async def test_stream_search_publishes_events(db_session):
    """stream_search_mpn publishes source-status and results events to the SSE broker."""
    from app.search_service import stream_search_mpn

    published_events = []

    async def mock_publish(channel, event, data=""):
        published_events.append({"channel": channel, "event": event, "data": data})

    # Mock broker and connectors
    with patch("app.search_service.broker") as mock_broker, \
         patch("app.search_service._build_connectors") as mock_build:
        mock_broker.publish = mock_publish

        # One fake connector that returns one result
        fake_connector = MagicMock()
        fake_connector.source_name = "nexar"
        fake_connector.search = AsyncMock(return_value=[{
            "vendor_name": "Arrow", "mpn_matched": "LM317T",
            "unit_price": 0.45, "qty_available": 1000,
            "source_type": "nexar", "is_authorized": True,
        }])
        mock_build.return_value = ([fake_connector], {})

        await stream_search_mpn("test-search-id", "LM317T", db_session)

    # Should have published source-status + results + done events
    event_types = [e["event"] for e in published_events]
    assert "source-status" in event_types
    assert "results" in event_types
    assert "done" in event_types
    assert all(e["channel"] == "search:test-search-id" for e in published_events)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_search_streaming.py::test_stream_search_publishes_events -v`
Expected: FAIL — `ImportError: cannot import name 'stream_search_mpn'`

- [ ] **Step 3: Implement `stream_search_mpn()` in `app/search_service.py`**

Add after `quick_search_mpn()`:

```python
async def stream_search_mpn(search_id: str, mpn: str, db: Session):
    """Stream search results via SSE broker as each connector completes.

    Publishes events to channel 'search:{search_id}':
    - source-status: OOB chip update per connector
    - results: new vendor card HTML to append
    - done: final stats

    Called by: htmx_views.search_run (as background task)
    Depends on: _build_connectors, _incremental_dedup, _score_raw_results, broker
    """
    from .services.sse_broker import broker
    from .evidence_tiers import tier_for_sighting

    channel = f"search:{search_id}"
    clean_mpn = normalize_mpn(mpn) or mpn.strip().upper()
    if not clean_mpn:
        await broker.publish(channel, "done", "")
        return

    connectors, source_stats_map, _disabled = _build_connectors(db)

    if not connectors:
        # No connectors available — publish done immediately so SSE client doesn't hang
        for name, stat in source_stats_map.items():
            await broker.publish(channel, "source-status",
                f'<span id="source-chip-{name}" hx-swap-oob="outerHTML:#source-chip-{name}" '
                f'class="source-chip source-chip--{stat["status"]}">{name}: {stat["status"]}</span>')
        await broker.publish(channel, "done", json.dumps({"total_results": 0, "elapsed_seconds": 0}))
        return

    # Build vendor score lookup
    from .models import VendorCard as VC
    vendor_cards = db.query(VC.normalized_name, VC.vendor_score).all()
    vendor_score_map = {vc.normalized_name: vc.vendor_score for vc in vendor_cards}

    # Publish initial status for skipped/disabled sources
    for name, stat in source_stats_map.items():
        status_label = stat["status"]  # "disabled" or "skipped"
        await broker.publish(channel, "source-status",
            f'<span id="source-chip-{name}" hx-swap-oob="outerHTML:#source-chip-{name}" '
            f'class="source-chip source-chip--{status_label}">{name}: {status_label}</span>')

    async def _run_one(connector):
        start = time.time()
        try:
            results = await connector.search(clean_mpn)
            return connector.source_name, results, None, (time.time() - start) * 1000
        except Exception as exc:
            logger.error("Connector {} failed: {}", connector.source_name, exc)
            return connector.source_name, [], str(exc), (time.time() - start) * 1000

    pending = {}
    for conn in connectors:
        task = asyncio.create_task(_run_one(conn))
        pending[task] = conn.source_name

    all_results: list[dict] = []
    card_index = 0
    start_time = time.time()

    while pending:
        done, _ = await asyncio.wait(pending.keys(), return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            name = pending.pop(task)
            source_name, raw_results, error, elapsed_ms = task.result()

            # Score raw results
            scored = []
            for r in raw_results:
                scored.append(_score_single_result(r, vendor_score_map, db))

            # Incremental dedup
            new_cards, updated_cards = _incremental_dedup(scored, all_results)

            # Publish source chip status
            count = len(raw_results)
            if error:
                chip_class = "source-chip--error"
                chip_text = f"{source_name}: error"
            elif count == 0:
                chip_class = "source-chip--empty"
                chip_text = f"{source_name}: 0"
            else:
                chip_class = f"source-chip--done source-chip--{source_name}"
                chip_text = f"{source_name}: {count}"

            await broker.publish(channel, "source-status",
                f'<span id="source-chip-{source_name}" '
                f'hx-swap-oob="outerHTML:#source-chip-{source_name}" '
                f'class="source-chip {chip_class}">{chip_text}</span>')

            # Publish new cards (appended via sse-swap="results")
            for card_data in new_cards:
                # Card HTML will be rendered by Jinja2 — for now publish JSON
                # The route handler wraps this in template rendering
                card_data["_card_index"] = card_index
                await broker.publish(channel, "results", json.dumps(card_data, default=str))
                card_index += 1

            # Publish updated cards (OOB swap)
            for card_data in updated_cards:
                vendor_key = normalize_vendor_name(card_data.get("vendor_name", ""))
                card_data["_card_index"] = -1  # existing card, no animation
                await broker.publish(channel, "card-update",
                    json.dumps({"vendor_key": vendor_key, "card": card_data}, default=str))

    elapsed_total = time.time() - start_time
    await broker.publish(channel, "done", json.dumps({
        "total_results": len(all_results),
        "elapsed_seconds": round(elapsed_total, 1),
    }))


def _score_single_result(r: dict, vendor_score_map: dict, db: Session) -> dict:
    """Score a single raw connector result into an enriched dict.

    Extracted from quick_search_mpn scoring loop (lines 339-394).

    Called by: stream_search_mpn
    Depends on: scoring.py functions, evidence_tiers, vendor_utils
    """
    from .evidence_tiers import tier_for_sighting

    raw_mpn = r.get("mpn_matched")
    clean_mpn_r = normalize_mpn(raw_mpn) or raw_mpn
    raw_vendor = r.get("vendor_name", "Unknown")
    clean_vendor = fix_encoding((raw_vendor or "").strip()) or raw_vendor

    clean_qty = normalize_quantity(r.get("qty_available"))
    if clean_qty is None and isinstance(r.get("qty_available"), (int, float)) and r["qty_available"] > 0:
        clean_qty = int(r["qty_available"])

    clean_price = normalize_price(r.get("unit_price"))
    if clean_price is None and isinstance(r.get("unit_price"), (int, float)) and r["unit_price"] > 0:
        clean_price = float(r["unit_price"])

    raw_currency = r.get("currency") or "USD"
    clean_currency = detect_currency(raw_currency) if raw_currency else "USD"
    is_auth = r.get("is_authorized", False)
    norm_name = normalize_vendor_name(clean_vendor)
    base_score = score_sighting(vendor_score_map.get(norm_name), is_auth)
    tier = tier_for_sighting(r.get("source_type"), is_auth)

    # Normalize confidence (same as quick_search_mpn line 355-356)
    raw_conf = r.get("confidence", 0) or 0
    norm_conf = raw_conf / 5.0 if raw_conf > 1 else raw_conf

    result = {
        "id": None,
        "requirement_id": None,
        "vendor_name": clean_vendor,
        "vendor_email": r.get("vendor_email"),
        "vendor_phone": r.get("vendor_phone"),
        "mpn_matched": clean_mpn_r,
        "manufacturer": r.get("manufacturer"),
        "qty_available": clean_qty,
        "unit_price": clean_price,
        "currency": clean_currency,
        "source_type": r.get("source_type"),
        "is_authorized": is_auth,
        "confidence": norm_conf,
        "score": base_score,
        "evidence_tier": tier,
        "octopart_url": r.get("octopart_url"),
        "click_url": r.get("click_url"),
        "vendor_url": r.get("vendor_url"),
        "vendor_sku": r.get("vendor_sku"),
        "condition": normalize_condition(r.get("condition")),
        "moq": r.get("moq") if r.get("moq") and r.get("moq") > 0 else None,
        "date_code": normalize_date_code(r.get("date_code")),
        "packaging": normalize_packaging(r.get("packaging")),
        "lead_time_days": normalize_lead_time(r.get("lead_time")),
        "lead_time": r.get("lead_time"),
        "country": r.get("country"),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "is_historical": False,
        "is_material_history": False,
        "raw_data": r.get("raw_data"),
        "age_hours": r.get("age_hours"),
        "vendor_score": vendor_score_map.get(norm_name),
    }

    # Enrich with unified scoring
    unified = score_unified(
        source_type=result["source_type"] or "",
        vendor_score=result.get("vendor_score"),
        is_authorized=is_auth,
        unit_price=clean_price,
        qty_available=clean_qty,
        age_hours=result.get("age_hours"),
        has_price=bool(clean_price),
        has_qty=bool(clean_qty),
        has_lead_time=bool(result.get("lead_time")),
        has_condition=bool(result.get("condition")),
    )
    result["confidence_pct"] = unified["confidence_pct"]
    result["confidence_color"] = unified["confidence_color"]
    result["source_badge"] = unified["source_badge"]
    result["score_components"] = unified.get("components")

    result["lead_quality"] = classify_lead(
        score=unified["score"], is_authorized=is_auth,
        has_price=bool(clean_price), has_qty=bool(clean_qty),
        has_contact=bool(result.get("vendor_email") or result.get("vendor_phone")),
        evidence_tier=tier,
    )
    result["reason"] = explain_lead(
        vendor_name=clean_vendor, is_authorized=is_auth,
        vendor_score=result.get("vendor_score"),
        unit_price=clean_price, qty_available=clean_qty,
        has_contact=bool(result.get("vendor_email") or result.get("vendor_phone")),
        evidence_tier=tier, source_type=result["source_type"],
    )

    return result
```

- [ ] **Step 4: Run test to verify it passes**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_search_streaming.py::test_stream_search_publishes_events -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/search_service.py tests/test_search_streaming.py
git commit -m "feat(search): add streaming search generator with SSE broker"
```

---

## Task 5: SSE Stream Route + Results Shell Template

**Files:**
- Modify: `app/routers/htmx_views.py`
- Create: `app/templates/htmx/partials/search/results_shell.html`
- Test: `tests/test_search_streaming.py`

- [ ] **Step 1: Write failing test for the SSE stream route**

```python
# tests/test_search_streaming.py — add
from app.main import app
from app.dependencies import get_db

def test_search_run_returns_shell_html(db_session):
    """POST /v2/partials/search/run should return results shell with SSE connection."""
    from fastapi.testclient import TestClient

    app.dependency_overrides[get_db] = lambda: db_session
    try:
        client = TestClient(app)
        resp = client.post("/v2/partials/search/run",
                           data={"mpn": "LM317T"},
                           headers={"HX-Request": "true"})
        assert resp.status_code == 200
        html = resp.text
        assert "sse-connect" in html
        assert "source-chip" in html
    finally:
        app.dependency_overrides.pop(get_db, None)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_search_streaming.py::test_search_run_returns_shell_html -v`
Expected: FAIL — shell template doesn't exist yet or route still returns old results

- [ ] **Step 3: Create results_shell.html template**

```html
{# Results shell — returned by search_run POST, contains SSE connection for streaming.
   Loads progress chips and empty results container. SSE events populate both.
   Called by: htmx_views.search_run
   Depends on: SSE stream at /v2/partials/search/stream
#}

<div id="search-results-wrapper" class="space-y-4">

  {# Source progress chips #}
  <div id="source-progress" class="flex flex-wrap gap-2 items-center">
    <span class="text-xs font-medium text-gray-500 mr-1">Sources:</span>
    {% for source in enabled_sources %}
    <span id="source-chip-{{ source.name }}"
          class="source-chip source-chip--searching">
      <span class="inline-block w-1.5 h-1.5 rounded-full bg-current animate-pulse mr-1"></span>
      {{ source.name }}
    </span>
    {% endfor %}
  </div>

  {# SSE connection — streams results into #search-results-cards #}
  <div hx-ext="sse"
       sse-connect="/v2/partials/search/stream?search_id={{ search_id }}"
       sse-close="done">

    {# New cards appended here #}
    <div id="search-results-cards"
         sse-swap="results"
         hx-swap="beforeend settle:100ms"
         class="space-y-3">
    </div>

    {# OOB swaps for source chips and card updates come via source-status event #}
    <div sse-swap="source-status" hx-swap="none"></div>

    {# Card updates (existing vendor gets more offers) #}
    <div sse-swap="card-update" hx-swap="none"
         hx-on::after-settle="handleCardUpdate(event)"></div>

    {# Final stats #}
    <div sse-swap="done" hx-swap="none"
         hx-on::after-settle="handleSearchDone(event)"></div>
  </div>

  {# Stats bar (updated by done event) #}
  <div id="search-stats" class="hidden text-xs text-gray-500 pt-2">
  </div>
</div>
```

- [ ] **Step 4: Modify search_run route to return shell**

In `app/routers/htmx_views.py`, update the `search_run` function (line 2110):

```python
@router.post("/v2/partials/search/run", response_class=HTMLResponse)
async def search_run(
    request: Request,
    mpn: str = Form(default=""),
    requirement_id: int = Query(default=0),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Launch streaming search and return results shell with SSE connection."""
    import asyncio
    from uuid import uuid4

    search_mpn = mpn.strip()
    if not search_mpn and requirement_id:
        req = db.query(Requirement).filter(Requirement.id == requirement_id).first()
        if req:
            search_mpn = req.primary_mpn or ""
    if not search_mpn:
        search_mpn = request.query_params.get("mpn", "").strip()
    if not search_mpn:
        return HTMLResponse('<div class="p-4 text-sm text-red-600">Please enter a part number.</div>')

    search_id = str(uuid4())

    # Get enabled sources for progress chips
    enabled_sources = _get_enabled_sources(db)

    # Launch streaming search as background task
    from ..search_service import stream_search_mpn
    asyncio.create_task(stream_search_mpn(search_id, search_mpn, db))

    ctx = _base_ctx(request, user, "search")
    ctx.update({
        "search_id": search_id,
        "mpn": search_mpn,
        "enabled_sources": enabled_sources,
    })
    return templates.TemplateResponse("htmx/partials/search/results_shell.html", ctx)
```

Add the helper:

```python
def _get_enabled_sources(db: Session) -> list[dict]:
    """Return list of enabled API sources for progress chip display."""
    from ..models import ApiSource
    sources = db.query(ApiSource).filter(ApiSource.status != "disabled").all()
    return [{"name": s.name, "status": s.status} for s in sources]
```

- [ ] **Step 5: Add the SSE stream route**

```python
@router.get("/v2/partials/search/stream")
async def search_stream(
    request: Request,
    search_id: str = Query(...),
    user: User = Depends(require_user),
):
    """SSE endpoint for streaming search results.

    Subscribes to SSE broker channel 'search:{search_id}' and yields events
    as connectors complete.
    """
    from sse_starlette.sse import EventSourceResponse
    from ..services.sse_broker import broker

    async def event_generator():
        async for msg in broker.listen(f"search:{search_id}"):
            if await request.is_disconnected():
                break
            yield {"event": msg["event"], "data": msg["data"]}
            if msg["event"] == "done":
                break

    return EventSourceResponse(event_generator())
```

- [ ] **Step 6: Run test to verify it passes**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_search_streaming.py::test_search_run_returns_shell_html -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add app/routers/htmx_views.py app/templates/htmx/partials/search/results_shell.html tests/test_search_streaming.py
git commit -m "feat(search): add SSE stream route and results shell template"
```

---

## Task 6: Vendor Card Template + Visual Design

**Files:**
- Create: `app/templates/htmx/partials/search/vendor_card.html`
- Modify: `app/templates/htmx/partials/search/form.html`
- Modify: `app/templates/htmx/partials/search/results.html` (full rewrite for non-streaming fallback)

- [ ] **Step 1: Create the vendor card partial**

Write `app/templates/htmx/partials/search/vendor_card.html` — a single vendor card rendered server-side. This is the core visual component. Follow the industrial precision aesthetic: dark card surface, JetBrains Mono for data, DM Sans for labels, source-specific color badges.

Card must include:
- Checkbox for shortlist (`x-data` with `$store.shortlist`)
- Vendor name (bold)
- MPN + manufacturer subtitle
- Best price, total qty, MOQ
- Confidence badge (green/amber/red)
- Source badges (colored per source)
- Authorization badge
- Offer count + "See all offers" expand toggle
- Sub-offers table (Alpine.js `x-show`)
- "View Details" button → existing drawer
- CSS animation with `style="--i: {{ card_index }}"`

- [ ] **Step 2: Polish the search form**

Update `app/templates/htmx/partials/search/form.html`:
- Tighten spacing
- Larger monospace input
- Industrial precision aesthetic

- [ ] **Step 3: Add source chip CSS**

Add to a `<style>` block in the results shell or in the main CSS:

```css
.source-chip {
    @apply px-2.5 py-1 text-xs font-medium rounded-full border transition-all duration-300;
}
.source-chip--searching {
    @apply bg-gray-100 text-gray-500 border-gray-200 animate-pulse;
}
.source-chip--done { @apply border-transparent text-white; }
.source-chip--nexar { @apply bg-violet-600; }
.source-chip--brokerbin { @apply bg-sky-600; }
.source-chip--digikey { @apply bg-orange-600; }
.source-chip--mouser { @apply bg-teal-600; }
.source-chip--ebay { @apply bg-yellow-600; }
.source-chip--oemsecrets { @apply bg-fuchsia-600; }
.source-chip--element14 { @apply bg-lime-600; }
.source-chip--sourcengine { @apply bg-emerald-600; }
.source-chip--empty { @apply bg-gray-200 text-gray-400 border-gray-200; }
.source-chip--error { @apply bg-rose-100 text-rose-600 border-rose-200; }
.source-chip--disabled { @apply bg-gray-100 text-gray-400 border-gray-200 opacity-50; }
.source-chip--skipped { @apply bg-gray-100 text-gray-400 border-gray-200 opacity-50; }

@keyframes slideUp {
    from { opacity: 0; transform: translateY(12px); }
    to { opacity: 1; transform: translateY(0); }
}
.vendor-card {
    animation: slideUp 0.3s ease-out both;
    animation-delay: calc(var(--i, 0) * 50ms);
}
```

- [ ] **Step 4: Write template rendering test**

```python
# tests/test_search_streaming.py — add
from jinja2 import Environment, FileSystemLoader

def test_vendor_card_template_renders():
    """vendor_card.html renders without errors with sample data."""
    env = Environment(loader=FileSystemLoader("app/templates"))
    tpl = env.get_template("htmx/partials/search/vendor_card.html")
    html = tpl.render(card={
        "vendor_name": "Arrow Electronics", "mpn_matched": "LM317T",
        "manufacturer": "Texas Instruments", "unit_price": 0.45,
        "qty_available": 12450, "moq": 1, "confidence_color": "green",
        "confidence_pct": 85, "lead_quality": "strong", "source_badge": "Live Stock",
        "is_authorized": True, "source_type": "nexar",
        "sub_offers": [], "offer_count": 1, "sources_found": {"nexar"},
        "reason": "Authorized distributor with confirmed stock",
    }, card_index=0)
    assert "Arrow Electronics" in html
    assert "LM317T" in html
    assert "$0.45" in html or "0.45" in html
```

- [ ] **Step 5: Run test to verify it passes**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_search_streaming.py::test_vendor_card_template_renders -v`
Expected: PASS

- [ ] **Step 6: Test the full flow manually**

Run the app in Docker. Navigate to Search tab, enter a part number, verify:
- Shell appears with progress chips
- Cards stream in as connectors complete
- Cards have correct visual design

- [ ] **Step 7: Commit**

```bash
git add app/templates/htmx/partials/search/vendor_card.html app/templates/htmx/partials/search/form.html app/templates/htmx/partials/search/results.html app/templates/htmx/partials/search/results_shell.html tests/test_search_streaming.py
git commit -m "feat(search): add vendor card template and visual design polish"
```

---

## Task 7: Shortlist Store + Sticky Action Bar

**Files:**
- Modify: `app/static/htmx_app.js`
- Create: `app/templates/htmx/partials/search/shortlist_bar.html`
- Test: `tests/test_search_streaming.py`

- [ ] **Step 1: Add shortlist Alpine store**

In `app/static/htmx_app.js`, after the existing store definitions (line ~101):

```javascript
Alpine.store('shortlist', {
    items: [],
    toggle(item) {
        const key = item.vendor_name + ':' + item.mpn;
        const idx = this.items.findIndex(i => (i.vendor_name + ':' + i.mpn) === key);
        if (idx >= 0) {
            this.items.splice(idx, 1);
        } else {
            this.items.push(item);
        }
    },
    has(vendorName, mpn) {
        const key = vendorName + ':' + mpn;
        return this.items.some(i => (i.vendor_name + ':' + i.mpn) === key);
    },
    clear() { this.items = []; },
    get count() { return this.items.length; },
});
```

- [ ] **Step 2: Create shortlist bar template**

Write `app/templates/htmx/partials/search/shortlist_bar.html`:

```html
{# Sticky shortlist action bar — appears when items selected.
   Uses Alpine.js $store.shortlist for state.
   Called by: included in results_shell.html
   Depends on: Alpine store 'shortlist', requisition picker modal
#}
<div x-show="$store.shortlist.count > 0"
     x-transition:enter="transition ease-out duration-200 transform"
     x-transition:enter-start="translate-y-full"
     x-transition:enter-end="translate-y-0"
     x-transition:leave="transition ease-in duration-150 transform"
     x-transition:leave-start="translate-y-0"
     x-transition:leave-end="translate-y-full"
     class="fixed bottom-16 left-0 right-0 z-30 bg-gray-900 border-t border-gray-700 px-4 py-3 shadow-2xl"
     x-cloak>
  <div class="max-w-7xl mx-auto flex items-center justify-between">
    <span class="text-sm font-medium text-white">
      <span x-text="$store.shortlist.count" class="font-mono text-brand-400"></span>
      vendor<span x-show="$store.shortlist.count !== 1">s</span> selected
    </span>
    <div class="flex items-center gap-2">
      <button @click="$dispatch('open-modal', {url: '/v2/partials/search/requisition-picker?items=' + encodeURIComponent(JSON.stringify($store.shortlist.items))})"
              class="px-4 py-2 text-sm font-semibold bg-brand-500 text-white rounded-lg hover:bg-brand-600 transition-colors">
        Add to Requisition
      </button>
      <button @click="$dispatch('open-modal', {url: '/v2/partials/search/requisition-picker?action=rfq&items=' + encodeURIComponent(JSON.stringify($store.shortlist.items))})"
              class="px-4 py-2 text-sm font-semibold bg-gray-700 text-white rounded-lg hover:bg-gray-600 transition-colors border border-gray-600">
        Create RFQ
      </button>
      <button @click="$store.shortlist.clear()"
              class="px-3 py-2 text-sm text-gray-400 hover:text-white transition-colors">
        Clear
      </button>
    </div>
  </div>
</div>
```

- [ ] **Step 3: Include shortlist bar in results shell**

Add at the bottom of `results_shell.html`:
```html
{% include "htmx/partials/search/shortlist_bar.html" %}
```

- [ ] **Step 4: Write template rendering test**

```python
# tests/test_search_streaming.py — add
def test_shortlist_bar_template_renders():
    """shortlist_bar.html renders with Alpine.js directives."""
    env = Environment(loader=FileSystemLoader("app/templates"))
    tpl = env.get_template("htmx/partials/search/shortlist_bar.html")
    html = tpl.render()
    assert "$store.shortlist" in html
    assert "Add to Requisition" in html
```

- [ ] **Step 5: Run test**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_search_streaming.py::test_shortlist_bar_template_renders -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add app/static/htmx_app.js app/templates/htmx/partials/search/shortlist_bar.html app/templates/htmx/partials/search/results_shell.html
git commit -m "feat(search): add shortlist Alpine store and sticky action bar"
```

---

## Task 8: Add to Requisition Route + Modal

**Files:**
- Modify: `app/routers/htmx_views.py`
- Create: `app/templates/htmx/partials/search/requisition_picker_modal.html`
- Test: `tests/test_search_streaming.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_search_streaming.py — add
def test_add_to_requisition_creates_sightings(db_session, test_user):
    """POST /v2/partials/search/add-to-requisition creates Requirement + Sighting rows."""
    from fastapi.testclient import TestClient
    from app.models.sourcing import Requisition, Requirement, Sighting

    app.dependency_overrides[get_db] = lambda: db_session
    try:
        client = TestClient(app)

        # Create a requisition
        req = Requisition(name="Test Req", customer_name="Test Co", created_by=test_user.id)
        db_session.add(req)
        db_session.commit()

        resp = client.post("/v2/partials/search/add-to-requisition",
            headers={"HX-Request": "true", "Content-Type": "application/json"},
            json={
                "requisition_id": req.id,
                "mpn": "LM317T",
                "items": [{
                    "vendor_name": "Arrow", "mpn_matched": "LM317T",
                    "unit_price": 0.45, "qty_available": 1000,
                    "source_type": "nexar", "is_authorized": True,
                    "confidence": 0.8, "score": 80,
                    "evidence_tier": "T1",
                }],
            })
        assert resp.status_code == 200

        # Verify Requirement was created
        requirement = db_session.query(Requirement).filter_by(
            requisition_id=req.id, primary_mpn="LM317T"
        ).first()
        assert requirement is not None

        # Verify Sighting was created
        sighting = db_session.query(Sighting).filter_by(
            requirement_id=requirement.id, vendor_name="Arrow"
        ).first()
        assert sighting is not None
        assert sighting.unit_price == 0.45
    finally:
        app.dependency_overrides.pop(get_db, None)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_search_streaming.py::test_add_to_requisition_creates_sightings -v`
Expected: FAIL — route doesn't exist

- [ ] **Step 3: Implement add-to-requisition route**

In `app/routers/htmx_views.py`:

```python
@router.post("/v2/partials/search/add-to-requisition", response_class=HTMLResponse)
async def add_to_requisition(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Add shortlisted search results to a requisition as Sightings.

    Creates a Requirement for the MPN if one doesn't exist on the requisition.
    Persists each selected result as a Sighting row.
    """
    body = await request.json()
    requisition_id = body.get("requisition_id")
    mpn = body.get("mpn", "").strip()
    items = body.get("items", [])

    if not requisition_id or not mpn or not items:
        return HTMLResponse('<div class="text-red-600 text-sm p-2">Missing required fields.</div>', status_code=400)

    req = db.get(Requisition, requisition_id)
    if not req:
        return HTMLResponse('<div class="text-red-600 text-sm p-2">Requisition not found.</div>', status_code=404)

    # Find or create Requirement for this MPN
    requirement = db.query(Requirement).filter_by(
        requisition_id=requisition_id,
        primary_mpn=mpn,
    ).first()

    if not requirement:
        from ..vendor_utils import normalize_vendor_name as _nvn
        requirement = Requirement(
            requisition_id=requisition_id,
            primary_mpn=mpn,
            normalized_mpn=mpn.strip().upper(),
            target_qty=None,
            sourcing_status="open",
        )
        db.add(requirement)
        db.flush()

    # Create Sighting rows
    for item in items:
        sighting = Sighting(
            requirement_id=requirement.id,
            vendor_name=item.get("vendor_name", "Unknown"),
            mpn_matched=item.get("mpn_matched"),
            manufacturer=item.get("manufacturer"),
            qty_available=item.get("qty_available"),
            unit_price=item.get("unit_price"),
            currency=item.get("currency", "USD"),
            source_type=item.get("source_type"),
            is_authorized=item.get("is_authorized", False),
            confidence=item.get("confidence", 0),
            score=item.get("score", 0),
            evidence_tier=item.get("evidence_tier"),
            moq=item.get("moq"),
            lead_time=item.get("lead_time"),
            condition=item.get("condition"),
            date_code=item.get("date_code"),
            packaging=item.get("packaging"),
            vendor_email=item.get("vendor_email"),
            vendor_phone=item.get("vendor_phone"),
            raw_data={
                "vendor_url": item.get("vendor_url"),
                "click_url": item.get("click_url"),
                "octopart_url": item.get("octopart_url"),
                "vendor_sku": item.get("vendor_sku"),
            },
        )
        db.add(sighting)

    db.commit()

    return HTMLResponse(f'''
        <div class="text-sm text-emerald-600 p-2">
            Added {len(items)} result{"s" if len(items) != 1 else ""} to requisition "{req.name}"
        </div>
    ''')
```

- [ ] **Step 4: Create requisition picker modal template**

Write `app/templates/htmx/partials/search/requisition_picker_modal.html` — a modal that lists recent requisitions with a search input. User clicks one to select it, then confirms.

- [ ] **Step 5: Run test to verify it passes**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_search_streaming.py::test_add_to_requisition_creates_sightings -v`
Expected: PASS

- [ ] **Step 6: Run full test suite**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v --tb=short -q`
Expected: All PASS

- [ ] **Step 7: Commit**

```bash
git add app/routers/htmx_views.py app/templates/htmx/partials/search/requisition_picker_modal.html tests/test_search_streaming.py
git commit -m "feat(search): add-to-requisition route with sighting persistence"
```

---

## Task 9: Filter Route (Cache-Backed)

**Files:**
- Modify: `app/routers/htmx_views.py`
- Modify: `app/search_service.py` (cache results in stream_search_mpn)
- Test: `tests/test_search_streaming.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_search_streaming.py — add
def test_search_filter_reads_from_cache(db_session):
    """GET /v2/partials/search/filter returns re-rendered cards from cached results."""
    import json
    from unittest.mock import patch
    from fastapi.testclient import TestClient

    cached_results = [
        {"vendor_name": "Arrow", "mpn_matched": "LM317T", "unit_price": 0.45,
         "confidence_color": "green", "confidence_pct": 85, "lead_quality": "strong",
         "source_type": "nexar", "sub_offers": [], "offer_count": 1, "sources_found": ["nexar"]},
    ]

    app.dependency_overrides[get_db] = lambda: db_session
    try:
        with patch("app.routers.htmx_views._get_cached_search_results", return_value=cached_results):
            client = TestClient(app)
            resp = client.get("/v2/partials/search/filter?search_id=test-123&confidence=high",
                             headers={"HX-Request": "true"})
        assert resp.status_code == 200
        assert "Arrow" in resp.text
    finally:
        app.dependency_overrides.pop(get_db, None)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_search_streaming.py::test_search_filter_reads_from_cache -v`
Expected: FAIL

- [ ] **Step 3: Add Redis caching in stream_search_mpn**

At the end of `stream_search_mpn()` in `app/search_service.py`, before publishing `done`:

```python
    # Cache results for filter endpoint (15-min TTL)
    try:
        rc = _get_search_redis()
        if rc:
            cache_key = f"search:{search_id}:results"
            rc.setex(cache_key, 900, json.dumps(all_results, default=str))
    except Exception:
        logger.warning("Failed to cache search results for filtering")
```

- [ ] **Step 4: Add filter route**

In `app/routers/htmx_views.py`:

```python
@router.get("/v2/partials/search/filter", response_class=HTMLResponse)
async def search_filter(
    request: Request,
    search_id: str = Query(...),
    confidence: str = Query("all"),
    source: str = Query("all"),
    sort: str = Query("best"),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Re-render search results with filters applied, reading from Redis cache."""
    results = _get_cached_search_results(search_id)
    if results is None:
        return HTMLResponse('<div class="text-sm text-gray-500 p-4">Search results expired. Please search again.</div>')

    # Apply filters
    if confidence != "all":
        color_map = {"high": "green", "medium": "amber", "low": "red"}
        results = [r for r in results if r.get("confidence_color") == color_map.get(confidence)]

    if source != "all":
        results = [r for r in results if source in r.get("sources_found", [])]

    # Apply sort
    if sort == "cheapest":
        results.sort(key=lambda r: r.get("unit_price") or float("inf"))
    elif sort == "stock":
        results.sort(key=lambda r: r.get("qty_available") or 0, reverse=True)
    else:
        results.sort(key=lambda r: (r.get("score", 0), r.get("confidence_pct", 0)), reverse=True)

    ctx = _base_ctx(request, user, "search")
    ctx["results"] = results
    return templates.TemplateResponse("htmx/partials/search/results.html", ctx)


def _get_cached_search_results(search_id: str) -> list[dict] | None:
    """Read cached search results from Redis."""
    try:
        from ..search_service import _get_search_redis
        rc = _get_search_redis()
        if rc:
            data = rc.get(f"search:{search_id}:results")
            if data:
                return json.loads(data)
    except Exception:
        pass
    return None
```

- [ ] **Step 5: Run test to verify it passes**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_search_streaming.py::test_search_filter_reads_from_cache -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add app/search_service.py app/routers/htmx_views.py tests/test_search_streaming.py
git commit -m "feat(search): add cache-backed filter route"
```

---

## Task 10: Detail Drawer Updates

**Files:**
- Modify: `app/routers/htmx_views.py` (search_lead_detail route, ~line 2209)
- Modify: `app/templates/htmx/partials/search/lead_detail.html`
- Test: `tests/test_search_streaming.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_search_streaming.py — add
def test_lead_detail_reads_from_cache(db_session):
    """Lead detail route reads vendor data from Redis cache including sub_offers."""
    from unittest.mock import patch

    cached_results = [
        {"vendor_name": "Arrow", "mpn_matched": "LM317T", "unit_price": 0.45,
         "confidence_color": "green", "confidence_pct": 85, "lead_quality": "strong",
         "source_type": "nexar", "reason": "Authorized distributor",
         "sub_offers": [{"unit_price": 0.48, "source_type": "digikey", "qty_available": 500}],
         "offer_count": 2, "sources_found": ["nexar", "digikey"]},
    ]

    with patch("app.routers.htmx_views._get_cached_search_results", return_value=cached_results):
        from fastapi.testclient import TestClient
        app.dependency_overrides[get_db] = lambda: db_session
        try:
            client = TestClient(app)
            resp = client.get("/v2/partials/search/lead-detail?search_id=test-123&vendor_key=arrow",
                             headers={"HX-Request": "true"})
            assert resp.status_code == 200
            assert "Arrow" in resp.text
        finally:
            app.dependency_overrides.pop(get_db, None)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_search_streaming.py::test_lead_detail_reads_from_cache -v`
Expected: FAIL — route doesn't accept `search_id`/`vendor_key` params yet

- [ ] **Step 3: Update search_lead_detail route**

Modify the existing `search_lead_detail` route in `htmx_views.py` to accept `search_id` and `vendor_key` params. Read from Redis cache (via `_get_cached_search_results`) and find the matching vendor card by normalized vendor name. Pass the full card data (including `sub_offers`) to the template.

```python
@router.get("/v2/partials/search/lead-detail", response_class=HTMLResponse)
async def search_lead_detail(
    request: Request,
    search_id: str = Query(""),
    vendor_key: str = Query(""),
    # Keep old params for backwards compat
    idx: int = Query(0, ge=0),
    mpn: str = Query(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Lead detail drawer — reads from Redis cache for vendor-grouped data."""
    if search_id and vendor_key:
        results = _get_cached_search_results(search_id)
        if results:
            from ..vendor_utils import normalize_vendor_name
            lead = next(
                (r for r in results
                 if normalize_vendor_name(r.get("vendor_name", "")) == vendor_key),
                None
            )
            if lead:
                ctx = _base_ctx(request, user, "search")
                ctx.update({"lead": lead, "mpn": lead.get("mpn_matched", mpn)})
                return templates.TemplateResponse("htmx/partials/search/lead_detail.html", ctx)

    # Fallback to old idx-based lookup (backwards compat)
    # ... existing code
```

- [ ] **Step 4: Update lead_detail.html**

Changes:
- Tighten padding to match new card design
- Add "Add to Shortlist" button using `$store.shortlist.toggle()`
- Show all offers from this vendor (render `lead.sub_offers` as a table)
- Use JetBrains Mono for data fields, DM Sans for labels
- Match dark/industrial color scheme of new cards

- [ ] **Step 5: Update vendor_card.html "View Details" button**

The button should pass `search_id` and `vendor_key` instead of `idx`:
```html
<button hx-get="/v2/partials/search/lead-detail?search_id={{ search_id }}&vendor_key={{ card.vendor_name|lower|trim }}"
        hx-target="#lead-drawer-content"
        hx-on::after-request="document.getElementById('lead-drawer').dataset.open = 'true'"
        class="...">View Details</button>
```

- [ ] **Step 6: Run test to verify it passes**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_search_streaming.py::test_lead_detail_reads_from_cache -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add app/routers/htmx_views.py app/templates/htmx/partials/search/lead_detail.html app/templates/htmx/partials/search/vendor_card.html tests/test_search_streaming.py
git commit -m "feat(search): update detail drawer with cache-backed vendor data and sub-offers"
```

---

## Task 11: Integration Test + Full Suite Verification

**Files:**
- Test: `tests/test_search_streaming.py`
- Test: all existing tests

- [ ] **Step 1: Write integration test**

```python
# tests/test_search_streaming.py — add
def test_full_search_flow_smoke(db_session):
    """Smoke test: search form → shell → filter → add-to-req."""
    from fastapi.testclient import TestClient

    app.dependency_overrides[get_db] = lambda: db_session
    try:
        client = TestClient(app)
        # 1. Load search form
        resp = client.get("/v2/partials/search", headers={"HX-Request": "true"})
        assert resp.status_code == 200
        assert "Search All Sources" in resp.text

        # 2. Submit search (returns shell)
        resp = client.post("/v2/partials/search/run",
                           data={"mpn": "LM317T"},
                           headers={"HX-Request": "true"})
        assert resp.status_code == 200
        assert "sse-connect" in resp.text
    finally:
        app.dependency_overrides.pop(get_db, None)
```

- [ ] **Step 2: Run full test suite**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v --tb=short`
Expected: ALL PASS

- [ ] **Step 3: Run coverage check**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ --cov=app --cov-report=term-missing --tb=no -q`
Expected: No coverage reduction

- [ ] **Step 4: Final commit**

```bash
git add tests/test_search_streaming.py
git commit -m "test(search): add integration smoke test for search flow"
```

---

## Task 12: Deploy + Manual Verification

- [ ] **Step 1: Merge and push**

```bash
cd /root/availai && git push origin main
```

- [ ] **Step 2: Deploy**

```bash
cd /root/availai && docker compose up -d --build
```

- [ ] **Step 3: Check logs**

```bash
docker compose logs -f app 2>&1 | head -50
```

- [ ] **Step 4: Manual verification checklist**

1. Navigate to Search tab
2. Enter a part number (e.g., LM317T)
3. Verify source chips appear and animate as connectors complete
4. Verify vendor cards stream in with correct data
5. Verify card expand shows sub-offers
6. Verify shortlist checkbox works
7. Verify sticky action bar appears with selections
8. Verify "Add to Requisition" modal works
9. Verify filters work
10. Verify detail drawer works
