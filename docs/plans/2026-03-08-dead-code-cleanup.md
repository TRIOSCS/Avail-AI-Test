# Dead Code Cleanup & Simplification Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Remove dead code, eliminate duplication, and simplify overly-repeated patterns across the codebase.

**Architecture:** No new features. Pure cleanup — remove unused imports, dead variables, duplicate functions, and consolidate repeated patterns. Extract inline data to JSON. Consolidate triple-duplicated MPN normalizer.

**Tech Stack:** Python, ruff (auto-fix), pytest

---

### Task 1: Fix all unused imports (48 files)

**Files:**
- Modify: All files flagged by `ruff check app/ --select F401`

**Step 1: Run ruff auto-fix**

Run: `cd /root/availai && ruff check app/ --select F401 --fix`

This auto-removes all 48 unused imports.

**Step 2: Verify no breakage**

Run: `cd /root/availai && TESTING=1 PYTHONPATH=/root/availai pytest tests/ -x -q --tb=short 2>&1 | tail -20`

Expected: All tests pass. If any fail, the import was actually used dynamically — restore it with a `# noqa: F401` comment.

**Step 3: Commit**

```bash
git add -A && git commit -m "chore: remove 48 unused imports (ruff F401 auto-fix)"
```

---

### Task 2: Remove dead variables and unused function parameters

**Files:**
- Modify: `app/search_service.py:318` — remove `_ALL_SOURCES`
- Modify: `app/services/ai_service.py:220` — remove unused `user_signature` param
- Modify: `app/services/proactive_matching.py:131` — remove unused `source_sighting` param

**Step 1: Remove `_ALL_SOURCES` from search_service.py**

Delete line 318:
```python
_ALL_SOURCES = list(_CONNECTOR_SOURCE_MAP.values())  # noqa: F841
```

**Step 2: Check if `user_signature` is referenced in ai_service.py**

Read `app/services/ai_service.py` around line 220. If the parameter is accepted but never used in the function body, add `_` prefix: `_user_signature`. Do NOT remove it from the signature if callers pass it.

**Step 3: Same for `source_sighting` in proactive_matching.py**

Read line 131. If the param is accepted but never used, prefix with `_`: `_source_sighting`.

**Step 4: Run tests**

Run: `cd /root/availai && TESTING=1 PYTHONPATH=/root/availai pytest tests/ -x -q --tb=short 2>&1 | tail -20`

**Step 5: Commit**

```bash
git add app/search_service.py app/services/ai_service.py app/services/proactive_matching.py
git commit -m "chore: remove dead variable _ALL_SOURCES, prefix unused params"
```

---

### Task 3: Replace print() with logger in eight_by_eight_jobs.py

**Files:**
- Modify: `app/jobs/eight_by_eight_jobs.py:175-236`

**Step 1: Replace all `print(...)` with `logger.info(...)`**

The file has 13 `print()` calls in a dry-run function. Replace each one. Make sure `logger` is imported from loguru at the top of the file:

```python
from loguru import logger
```

Replace patterns:
- `print("=" * 60)` → `logger.info("=" * 60)`
- `print(f"...")` → `logger.info("...")`
- `print()` → `logger.info("")`

**Step 2: Run tests**

Run: `cd /root/availai && TESTING=1 PYTHONPATH=/root/availai pytest tests/ -x -q --tb=short 2>&1 | tail -20`

**Step 3: Commit**

```bash
git add app/jobs/eight_by_eight_jobs.py
git commit -m "chore: replace 13 print() calls with logger.info() in 8x8 dry-run"
```

---

### Task 4: Deduplicate `_key()` in startup.py

**Files:**
- Modify: `app/startup.py`

**Step 1: Extract shared `_key()` to module level**

The function `_key(raw)` is defined identically at lines 275 and 340 inside two different backfill functions. Extract it once near the top of the file (after imports):

```python
import re as _re
_NONALNUM = _re.compile(r"[^a-z0-9]")

def _norm_key(raw):
    """Normalize a value to lowercase alphanumeric key for backfill comparison."""
    if not raw:
        return ""
    return _NONALNUM.sub("", str(raw).strip().lower())
```

Then in both `_backfill_normalized_mpn()` and `_backfill_sighting_offer_normalized_mpn()`, delete the local `import re`, `_nonalnum = ...`, and `def _key(...)` definitions. Replace all `_key(...)` calls with `_norm_key(...)`.

**Step 2: Run tests**

Run: `cd /root/availai && TESTING=1 PYTHONPATH=/root/availai pytest tests/ -x -q --tb=short 2>&1 | tail -20`

**Step 3: Commit**

```bash
git add app/startup.py
git commit -m "chore: deduplicate _key() helper in startup.py"
```

---

### Task 5: Consolidate triple-duplicated MPN normalizer

**Files:**
- Modify: `app/utils/normalization.py` — add suffix-stripping normalize function
- Modify: `app/services/ics_worker/ai_gate.py` — update import
- Modify: `app/services/nc_worker/ai_gate.py` — update import
- Modify: `app/services/ics_worker/queue_manager.py` — update import
- Modify: `app/services/nc_worker/queue_manager.py` — update import
- Modify: `scripts/nc_backfill_queue.py` — update import
- Modify: `tests/test_ics_worker_full.py` — update import
- Modify: `tests/test_nc_phase2.py` — update import
- Modify: `tests/test_nc_worker_full.py` — update import
- Delete: `app/services/ics_worker/mpn_normalizer.py`
- Delete: `app/services/nc_worker/mpn_normalizer.py`

**Step 1: Add `normalize_mpn_for_dedup()` to `app/utils/normalization.py`**

Copy the suffix-stripping logic from the worker mpn_normalizer.py into the central normalization module. Add it after the existing `normalize_mpn()` function:

```python
# Well-known ordering/packaging suffixes to strip for dedup comparison.
_STRIP_SUFFIXES = re.compile(
    r"("
    r"/TR|"
    r"-TR|"
    r"/CT|"
    r"-CT|"
    r"-ND|"
    r"-DKR|"
    r"#PBF|"
    r"-PBF|"
    r"/NOPB|"
    r"-NOPB"
    r")$",
    re.IGNORECASE,
)
_REEL_SUFFIX = re.compile(r"-RL\d*$", re.IGNORECASE)


def normalize_mpn_for_dedup(mpn: str) -> str:
    """Normalize MPN for dedup: uppercase, strip whitespace + packaging suffixes.

    Use for DEDUP comparison (queue dedup, classification cache).
    Strips -TR, /CT, #PBF, -NOPB, -RL etc. that are packaging, not part identity.
    """
    if not mpn:
        return ""
    result = mpn.strip().upper()
    result = re.sub(r"\\s+", "", result)
    result = _STRIP_SUFFIXES.sub("", result)
    result = _REEL_SUFFIX.sub("", result)
    return result
```

**Step 2: Update all imports**

In every file that imports from `ics_worker.mpn_normalizer` or `nc_worker.mpn_normalizer`, change to:

```python
from app.utils.normalization import normalize_mpn_for_dedup as normalize_mpn
```

Files to update:
- `app/services/ics_worker/ai_gate.py`
- `app/services/ics_worker/queue_manager.py`
- `app/services/nc_worker/ai_gate.py`
- `app/services/nc_worker/queue_manager.py`
- `scripts/nc_backfill_queue.py`
- `tests/test_ics_worker_full.py`
- `tests/test_nc_phase2.py`
- `tests/test_nc_worker_full.py`

**Step 3: Delete the duplicate files**

```bash
rm app/services/ics_worker/mpn_normalizer.py app/services/nc_worker/mpn_normalizer.py
```

**Step 4: Run tests**

Run: `cd /root/availai && TESTING=1 PYTHONPATH=/root/availai pytest tests/ -x -q --tb=short 2>&1 | tail -20`

**Step 5: Commit**

```bash
git add -A
git commit -m "refactor: consolidate triple-duplicated MPN normalizer into app/utils/normalization.py"
```

---

### Task 6: Extract `_seed_api_sources()` data to JSON

**Files:**
- Create: `app/data/api_sources.json`
- Modify: `app/main.py` — load from JSON instead of inline dicts

**Step 1: Create `app/data/api_sources.json`**

Extract the `SOURCES` list (lines 484-840 of main.py) into a JSON file at `app/data/api_sources.json`. This is a straight copy of the Python list-of-dicts to JSON format.

**Step 2: Simplify `_seed_api_sources()` in main.py**

Replace the 350+ lines of inline dicts with:

```python
def _seed_api_sources():
    """Seed the api_sources table with all known data sources."""
    import hashlib
    import json
    from pathlib import Path

    from .database import SessionLocal

    sources_path = Path(__file__).parent / "data" / "api_sources.json"
    SOURCES = json.loads(sources_path.read_text())

    db = SessionLocal()
    try:
        # ... rest of the function stays the same (hash check, upsert, quota backfill)
```

Keep lines 842-900 (the hash check, upsert loop, and quota backfill) exactly as they are — only replace the inline SOURCES list with the JSON load.

**Step 3: Run tests**

Run: `cd /root/availai && TESTING=1 PYTHONPATH=/root/availai pytest tests/ -x -q --tb=short 2>&1 | tail -20`

**Step 4: Commit**

```bash
git add app/data/api_sources.json app/main.py
git commit -m "refactor: extract API source seed data to app/data/api_sources.json (~350 lines removed from main.py)"
```

---

### Task 7: Generify knowledge_service.py insight pattern

**Files:**
- Modify: `app/services/knowledge_service.py`

The file has 4 nearly-identical `generate_*_insights()` functions and 4 nearly-identical `get_cached_*_insights()` functions. Consolidate into generic helpers.

**Step 1: Add generic `_generate_entity_insights()` helper**

Add after line 563 (after the existing `generate_insights` for requisitions):

```python
async def _generate_entity_insights(
    db: Session,
    *,
    context: str,
    system_prompt: str,
    entity_label: str,
    filter_kwargs: dict,
    create_kwargs: dict,
) -> list[KnowledgeEntry]:
    """Generic insight generator — used by all entity-scoped insight functions."""
    from app.utils.claude_client import claude_structured

    if not context:
        logger.debug("No context for {} — skipping insight generation", entity_label)
        return []

    # Delete old AI insights matching these filters
    q = db.query(KnowledgeEntry).filter(KnowledgeEntry.entry_type == "ai_insight")
    for attr, val in filter_kwargs.items():
        if val is None:
            q = q.filter(getattr(KnowledgeEntry, attr).is_(None))
        else:
            q = q.filter(getattr(KnowledgeEntry, attr) == val)
    for old in q.all():
        db.delete(old)
    db.flush()

    result = await claude_structured(
        prompt="Analyze this knowledge base and generate insights:\n\n{}".format(context),
        schema=INSIGHT_SCHEMA,
        system=system_prompt,
        model_tier="smart",
        max_tokens=2048,
        thinking_budget=5000,
    )

    if not result or "insights" not in result:
        logger.warning("AI insight generation returned no results for {}", entity_label)
        return []

    entries = []
    now = datetime.now(timezone.utc)
    for insight in result["insights"][:5]:
        entry = create_entry(
            db,
            user_id=0,
            entry_type="ai_insight",
            content=insight["content"],
            source="ai_generated",
            confidence=insight.get("confidence", 0.8),
            expires_at=now + timedelta(days=EXPIRY_AI_INSIGHT),
            **create_kwargs,
        )
        entries.append(entry)

    logger.info("Generated {} insights for {}", len(entries), entity_label)
    return entries


def _get_cached_entity_insights(db: Session, **filter_kwargs) -> list[KnowledgeEntry]:
    """Generic cached insight getter."""
    q = db.query(KnowledgeEntry).filter(KnowledgeEntry.entry_type == "ai_insight")
    for attr, val in filter_kwargs.items():
        if val is None:
            q = q.filter(getattr(KnowledgeEntry, attr).is_(None))
        else:
            q = q.filter(getattr(KnowledgeEntry, attr) == val)
    return q.order_by(KnowledgeEntry.created_at.desc()).all()
```

**Step 2: Replace all 4 `generate_*_insights()` with thin wrappers**

```python
async def generate_mpn_insights(db: Session, mpn: str) -> list[KnowledgeEntry]:
    return await _generate_entity_insights(
        db,
        context=build_mpn_context(db, mpn=mpn),
        system_prompt=MPN_INSIGHT_PROMPT,
        entity_label=f"MPN {mpn}",
        filter_kwargs={"mpn": mpn, "requisition_id": None},
        create_kwargs={"mpn": mpn},
    )

async def generate_vendor_insights(db: Session, vendor_card_id: int) -> list[KnowledgeEntry]:
    return await _generate_entity_insights(
        db,
        context=build_vendor_context(db, vendor_card_id=vendor_card_id),
        system_prompt=VENDOR_INSIGHT_PROMPT,
        entity_label=f"vendor {vendor_card_id}",
        filter_kwargs={"vendor_card_id": vendor_card_id},
        create_kwargs={"vendor_card_id": vendor_card_id},
    )

async def generate_pipeline_insights(db: Session) -> list[KnowledgeEntry]:
    return await _generate_entity_insights(
        db,
        context=build_pipeline_context(db),
        system_prompt=PIPELINE_INSIGHT_PROMPT,
        entity_label="pipeline",
        filter_kwargs={"mpn": "__pipeline__"},
        create_kwargs={"mpn": "__pipeline__"},
    )

async def generate_company_insights(db: Session, company_id: int) -> list[KnowledgeEntry]:
    return await _generate_entity_insights(
        db,
        context=build_company_context(db, company_id=company_id),
        system_prompt=COMPANY_INSIGHT_PROMPT,
        entity_label=f"company {company_id}",
        filter_kwargs={"company_id": company_id},
        create_kwargs={"company_id": company_id},
    )
```

**Step 3: Replace all 4 `get_cached_*_insights()` with thin wrappers**

```python
def get_cached_mpn_insights(db: Session, mpn: str) -> list[KnowledgeEntry]:
    return _get_cached_entity_insights(db, mpn=mpn, requisition_id=None)

def get_cached_vendor_insights(db: Session, vendor_card_id: int) -> list[KnowledgeEntry]:
    return _get_cached_entity_insights(db, vendor_card_id=vendor_card_id)

def get_cached_pipeline_insights(db: Session) -> list[KnowledgeEntry]:
    return _get_cached_entity_insights(db, mpn="__pipeline__")

def get_cached_company_insights(db: Session, company_id: int) -> list[KnowledgeEntry]:
    return _get_cached_entity_insights(db, company_id=company_id)
```

**Step 4: Delete the old full implementations** (lines ~889-1162)

The old `generate_mpn_insights`, `generate_vendor_insights`, `generate_pipeline_insights`, `generate_company_insights`, `get_cached_mpn_insights`, `get_cached_vendor_insights`, `get_cached_pipeline_insights`, `get_cached_company_insights` — all replaced by the thin wrappers above.

**Step 5: Run tests**

Run: `cd /root/availai && TESTING=1 PYTHONPATH=/root/availai pytest tests/ -x -q --tb=short 2>&1 | tail -20`

**Step 6: Commit**

```bash
git add app/services/knowledge_service.py
git commit -m "refactor: generify knowledge_service insight pattern — eliminate 4x code duplication (~400 lines saved)"
```

---

### Task 8: Consolidate ICS/NC worker shared code

**Files:**
- Create: `app/services/search_worker_base/` package with shared modules
- Modify: `app/services/ics_worker/` — import from shared base
- Modify: `app/services/nc_worker/` — import from shared base
- Delete: 6 duplicate files (human_behavior, monitoring, scheduler, ai_gate, queue_manager, config)

**Step 1: Create shared base package**

```bash
mkdir -p app/services/search_worker_base
```

Move these files (they are 95-100% identical between ICS and NC):

1. `human_behavior.py` — 100% identical, copy as-is
2. `monitoring.py` — parameterize the component name string
3. `scheduler.py` — parameterize the config prefix
4. `config.py` — make a factory function that reads from env with a prefix

For `monitoring.py`, change hardcoded `"ICS"` / `"NC"` strings to a `component_name` parameter passed at init.

For `scheduler.py`, change hardcoded config attribute lookups to accept a config prefix.

**Step 2: Update ICS worker imports**

In `app/services/ics_worker/worker.py`, `__main__.py`, and any other files that import the shared modules, change imports to:

```python
from app.services.search_worker_base.human_behavior import random_delay, ...
from app.services.search_worker_base.monitoring import WorkerMonitor
```

**Step 3: Update NC worker imports the same way**

**Step 4: Delete the now-redundant files from both ics_worker/ and nc_worker/**

Delete: `human_behavior.py`, `monitoring.py`, `scheduler.py`, `config.py` from both directories. Keep the legitimately different files: `worker.py`, `search_engine.py`, `session_manager.py`, `result_parser.py`, `sighting_writer.py`, `circuit_breaker.py`.

**Step 5: Run tests**

Run: `cd /root/availai && TESTING=1 PYTHONPATH=/root/availai pytest tests/ -x -q --tb=short 2>&1 | tail -20`

**Step 6: Commit**

```bash
git add -A
git commit -m "refactor: extract shared search worker base — eliminate ~800 lines of ICS/NC duplication"
```

---

### Task 9: Final verification — full test suite + coverage

**Step 1: Run full test suite**

Run: `cd /root/availai && TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v 2>&1 | tail -30`

**Step 2: Run coverage check**

Run: `cd /root/availai && TESTING=1 PYTHONPATH=/root/availai pytest tests/ --cov=app --cov-report=term-missing --tb=no -q 2>&1 | tail -30`

Expected: Coverage should remain at 97%+ (same or better than before — we're only removing dead code).

**Step 3: Run ruff one final time**

Run: `cd /root/availai && ruff check app/ --select F401,F841`

Expected: Clean (no unused imports or variables).

**Step 4: Commit any final fixes**

If any tests failed or coverage dropped, fix and commit.

---

## Summary

| Task | What | Lines Saved |
|------|------|-------------|
| 1 | Remove 48 unused imports | ~48 |
| 2 | Remove dead variables + prefix unused params | ~5 |
| 3 | Replace print() with logger | 0 (quality fix) |
| 4 | Deduplicate `_key()` in startup.py | ~15 |
| 5 | Consolidate MPN normalizer (3→1) | ~110 |
| 6 | Extract seed data to JSON | ~350 from main.py |
| 7 | Generify knowledge_service insights | ~400 |
| 8 | Consolidate ICS/NC worker shared code | ~800 |
| 9 | Final verification | 0 |
| **Total** | | **~1,728 lines** |
