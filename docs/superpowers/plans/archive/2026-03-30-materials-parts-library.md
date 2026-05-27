# Materials Parts Library Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Materials page functional as a searchable parts library by enabling enrichment, upgrading search to full-text, and fixing data pipeline gaps.

**Architecture:** Enable the existing (disabled) enrichment pipeline, replace ILIKE search with PostgreSQL FTS using the existing `search_vector` column, link `Requirement.material_card_id` during search, and wire the placeholder Enrich button to the real service.

**Tech Stack:** PostgreSQL FTS + pg_trgm, Claude Haiku (enrichment), SQLAlchemy, Alembic

---

### Task 1: Enable Enrichment & Run Initial Batch

**Files:**
- Modify: `.env` (add `MATERIAL_ENRICHMENT_ENABLED=true`)
- Modify: `app/routers/htmx_views.py:8582-8600` (wire Enrich button)
- Test: `tests/test_sightings_router.py` (not needed — existing enrichment service has tests)

- [ ] **Step 1: Enable the enrichment config flag**

Add to `.env`:
```
MATERIAL_ENRICHMENT_ENABLED=true
```

- [ ] **Step 2: Wire the Enrich button to real service**

In `app/routers/htmx_views.py`, replace the placeholder `enrich_material` endpoint (line 8582-8600):

```python
@router.post("/v2/partials/materials/{material_id}/enrich", response_class=HTMLResponse)
async def enrich_material(
    request: Request,
    material_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Trigger AI enrichment for a material card."""
    from ..models.intelligence import MaterialCard
    from ..services.material_enrichment_service import enrich_material_cards

    mc = db.query(MaterialCard).filter(MaterialCard.id == material_id).first()
    if not mc:
        raise HTTPException(404, "Material not found")

    try:
        result = await enrich_material_cards([material_id], db)
        db.refresh(mc)
    except Exception as e:
        logger.warning("Enrichment failed for material %d: %s", material_id, e)

    return await material_detail_partial(request, material_id, user, db)
```

- [ ] **Step 3: Run one-time enrichment of all existing cards**

```bash
docker compose exec app python -c "
import asyncio
from app.database import SessionLocal
from app.models.intelligence import MaterialCard
from app.services.material_enrichment_service import enrich_material_cards

db = SessionLocal()
card_ids = [c.id for c in db.query(MaterialCard.id).filter(MaterialCard.deleted_at.is_(None)).all()]
print(f'Enriching {len(card_ids)} cards...')
result = asyncio.run(enrich_material_cards(card_ids, db))
print(f'Result: {result}')
db.close()
"
```

- [ ] **Step 4: Verify enrichment populated categories**

```bash
docker compose exec app python -c "
from app.database import SessionLocal
from app.models.intelligence import MaterialCard
db = SessionLocal()
for c in db.query(MaterialCard).filter(MaterialCard.deleted_at.is_(None)).all():
    print(f'{c.display_mpn}: cat={c.category}, lifecycle={c.lifecycle_status}, desc={c.description and c.description[:60]}')
db.close()
"
```

Expected: All 16 cards now have category + lifecycle_status + description.

- [ ] **Step 5: Deploy**

```bash
./deploy.sh --no-commit
```

---

### Task 2: Add FTS Trigger + pg_trgm Index (Alembic Migration)

**Files:**
- Create: `alembic/versions/XXX_fts_trigger_and_trgm.py`

- [ ] **Step 1: Merge existing alembic heads**

```bash
cd /root/availai && alembic merge heads -m "merge_heads_before_fts"
```

- [ ] **Step 2: Create the migration**

```bash
alembic revision -m "add_fts_trigger_and_trgm_index"
```

Then edit the generated file:

```python
"""add_fts_trigger_and_trgm_index

Revision ID: auto-generated
"""
from alembic import op


def upgrade():
    # 1. Enable pg_trgm extension (idempotent)
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    # 2. Create trigger function for search_vector maintenance
    op.execute("""
        CREATE OR REPLACE FUNCTION material_cards_search_vector_update() RETURNS trigger AS $$
        BEGIN
            NEW.search_vector :=
                setweight(to_tsvector('english', coalesce(NEW.display_mpn, '')), 'A') ||
                setweight(to_tsvector('english', coalesce(NEW.normalized_mpn, '')), 'A') ||
                setweight(to_tsvector('english', coalesce(NEW.manufacturer, '')), 'B') ||
                setweight(to_tsvector('english', coalesce(NEW.description, '')), 'C') ||
                setweight(to_tsvector('english', coalesce(NEW.category, '')), 'C');
            RETURN NEW;
        END
        $$ LANGUAGE plpgsql;
    """)

    # 3. Create trigger
    op.execute("""
        CREATE TRIGGER trig_material_cards_search_vector
        BEFORE INSERT OR UPDATE OF display_mpn, normalized_mpn, manufacturer, description, category
        ON material_cards
        FOR EACH ROW
        EXECUTE FUNCTION material_cards_search_vector_update();
    """)

    # 4. Backfill search_vector for all existing rows
    op.execute("""
        UPDATE material_cards SET
            search_vector =
                setweight(to_tsvector('english', coalesce(display_mpn, '')), 'A') ||
                setweight(to_tsvector('english', coalesce(normalized_mpn, '')), 'A') ||
                setweight(to_tsvector('english', coalesce(manufacturer, '')), 'B') ||
                setweight(to_tsvector('english', coalesce(description, '')), 'C') ||
                setweight(to_tsvector('english', coalesce(category, '')), 'C');
    """)

    # 5. GIN index on search_vector (for FTS queries)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_material_cards_search_vector
        ON material_cards USING gin(search_vector)
    """)

    # 6. pg_trgm index on display_mpn (for typo-tolerant search)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_material_cards_trgm_mpn
        ON material_cards USING gin(display_mpn gin_trgm_ops)
    """)


def downgrade():
    op.execute("DROP INDEX IF EXISTS ix_material_cards_trgm_mpn")
    op.execute("DROP INDEX IF EXISTS ix_material_cards_search_vector")
    op.execute("DROP TRIGGER IF EXISTS trig_material_cards_search_vector ON material_cards")
    op.execute("DROP FUNCTION IF EXISTS material_cards_search_vector_update()")
```

- [ ] **Step 3: Run the migration**

```bash
alembic upgrade head
```

- [ ] **Step 4: Verify trigger works**

```bash
docker compose exec app python -c "
from app.database import SessionLocal
from sqlalchemy import text
db = SessionLocal()
# Check trigger exists
r = db.execute(text(\"SELECT tgname FROM pg_trigger WHERE tgname LIKE '%material%'\")).fetchall()
print(f'Triggers: {r}')
# Check search_vector populated
r2 = db.execute(text(\"SELECT display_mpn, search_vector::text FROM material_cards LIMIT 3\")).fetchall()
for row in r2:
    print(f'{row[0]}: {row[1][:80]}...')
db.close()
"
```

- [ ] **Step 5: Commit**

```bash
git add alembic/versions/
git commit -m "feat: add FTS trigger and pg_trgm index for material_cards"
```

---

### Task 3: Upgrade Faceted Search to Use FTS

**Files:**
- Modify: `app/services/faceted_search_service.py:160-169`
- Test: `tests/test_faceted_search.py` (new)

- [ ] **Step 1: Write failing test**

Create `tests/test_faceted_search.py`:

```python
"""Tests for faceted search service — FTS upgrade.

Called by: pytest
Depends on: conftest.py, MaterialCard model
"""

from app.models.intelligence import MaterialCard
from app.services.faceted_search_service import search_materials_faceted


class TestFacetedSearchFTS:
    def test_search_by_description_keyword(self, db_session):
        """FTS finds cards by description content, not just MPN substring."""
        card = MaterialCard(
            normalized_mpn="stm32f407vgt6",
            display_mpn="STM32F407VGT6",
            manufacturer="STMicroelectronics",
            description="32-bit ARM Cortex-M4 microcontroller with FPU",
            category="microcontroller",
        )
        db_session.add(card)
        db_session.commit()

        results, total = search_materials_faceted(db_session, q="microcontroller ARM")
        assert total >= 1
        assert any(r.normalized_mpn == "stm32f407vgt6" for r in results)

    def test_search_by_partial_mpn(self, db_session):
        """Short MPN prefix still works (ILIKE fallback for < 3 chars)."""
        card = MaterialCard(
            normalized_mpn="lm7805ct",
            display_mpn="LM7805CT",
            manufacturer="Texas Instruments",
            category="voltage_regulator",
        )
        db_session.add(card)
        db_session.commit()

        results, total = search_materials_faceted(db_session, q="LM78")
        assert total >= 1

    def test_search_no_results(self, db_session):
        """Search for nonexistent term returns empty."""
        results, total = search_materials_faceted(db_session, q="ZZZZNOTFOUND")
        assert total == 0
        assert results == []
```

- [ ] **Step 2: Run test to verify it fails**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_faceted_search.py -v --override-ini="addopts="
```

Expected: `test_search_by_description_keyword` may fail because ILIKE on "microcontroller ARM" won't match (searches for "%microcontroller ARM%" as substring).

- [ ] **Step 3: Implement FTS in faceted search**

In `app/services/faceted_search_service.py`, replace the search block (lines 160-169):

```python
    if q:
        sb = SearchBuilder(q)
        # Use PostgreSQL FTS when available, fall back to ILIKE for SQLite (tests)
        if db.bind and db.bind.dialect.name == "postgresql":
            from sqlalchemy import func as sqlfunc, text

            ts_query = sqlfunc.plainto_tsquery("english", q)
            query = query.filter(MaterialCard.search_vector.op("@@")(ts_query))
            # Rank by FTS relevance instead of default ordering
            query = query.order_by(
                sqlfunc.ts_rank(MaterialCard.search_vector, ts_query).desc(),
                MaterialCard.search_count.desc(),
            )
        else:
            # SQLite fallback for test environment
            query = query.filter(
                sb.ilike_filter(
                    MaterialCard.normalized_mpn,
                    MaterialCard.display_mpn,
                    MaterialCard.manufacturer,
                    MaterialCard.description,
                )
            )
```

Also update the ordering at the bottom (lines 211-213) — only apply default order if FTS didn't set one:

```python
    total = db.query(func.count()).select_from(query.subquery()).scalar()

    # If FTS already applied relevance ordering (PostgreSQL + q), don't override
    if not (q and db.bind and db.bind.dialect.name == "postgresql"):
        query = query.order_by(MaterialCard.search_count.desc(), MaterialCard.created_at.desc())

    materials = query.offset(offset).limit(limit).all()
    return materials, total
```

- [ ] **Step 4: Run tests**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_faceted_search.py -v --override-ini="addopts="
```

Expected: All 3 pass (SQLite fallback uses ILIKE).

- [ ] **Step 5: Commit**

```bash
git add app/services/faceted_search_service.py tests/test_faceted_search.py
git commit -m "feat: upgrade materials search to PostgreSQL FTS with ILIKE fallback"
```

---

### Task 4: Link Requirement.material_card_id in Search Pipeline

**Files:**
- Modify: `app/search_service.py:224-236`
- Test: existing tests (verify no regression)

- [ ] **Step 1: Add material_card_id linkage after upsert**

In `app/search_service.py`, after the material card upsert loop (after line 233), add linkage for the primary MPN:

```python
        # 3. Material card upsert (errors won't break search)
        card_ids = set()
        primary_card_id = None
        for pn in pns:
            try:
                card = _upsert_material_card(pn, sightings, write_db, now)
                if card:
                    card_ids.add(card.id)
                    # Link primary MPN's card to the requirement
                    if pn == pns[0] and not primary_card_id:
                        primary_card_id = card.id
            except Exception as e:
                logger.error("MATERIAL_CARD_UPSERT_FAIL: mpn=%s error=%s", pn, e)
                write_db.rollback()

        # Link requirement to its primary material card
        if primary_card_id and not write_req.material_card_id:
            write_req.material_card_id = primary_card_id
```

- [ ] **Step 2: Run existing tests**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py -v --override-ini="addopts=" -x
```

Expected: All 101 pass.

- [ ] **Step 3: Commit**

```bash
git add app/search_service.py
git commit -m "feat: link Requirement.material_card_id during search pipeline"
```

---

### Task 5: Cache Cross-Reference Lookups

**Files:**
- Modify: `app/routers/htmx_views.py:8603-8676`

- [ ] **Step 1: Add cache check at top of find_crosses**

In `app/routers/htmx_views.py`, at the start of `find_crosses()` (after the mc lookup, around line 8621), add:

```python
    # Return cached results if available
    if mc.cross_references:
        return templates.TemplateResponse(
            "htmx/partials/materials/crosses_section.html",
            {"request": request, "card": mc},
        )
```

- [ ] **Step 2: Add force-refresh parameter**

Update the function signature to accept a `refresh` query param:

```python
@router.post("/v2/partials/materials/{material_id}/find-crosses", response_class=HTMLResponse)
async def find_crosses(
    request: Request,
    material_id: int,
    refresh: bool = Form(False),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
```

And update the cache check:

```python
    if mc.cross_references and not refresh:
        return templates.TemplateResponse(
            "htmx/partials/materials/crosses_section.html",
            {"request": request, "card": mc},
        )
```

- [ ] **Step 3: Commit**

```bash
git add app/routers/htmx_views.py
git commit -m "feat: cache cross-reference lookups, add refresh param"
```

---

### Task 6: Deploy & Verify

**Files:** None (deployment + verification)

- [ ] **Step 1: Run full test suite**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v --override-ini="addopts=" -x --timeout=60
```

Expected: All tests pass.

- [ ] **Step 2: Deploy**

```bash
./deploy.sh --no-commit
```

- [ ] **Step 3: Run the Alembic migration on production DB**

```bash
docker compose exec app alembic upgrade head
```

- [ ] **Step 4: Verify Materials page works**

```bash
docker compose exec app python -c "
from app.database import SessionLocal
from app.models.intelligence import MaterialCard
db = SessionLocal()
# Check enrichment
enriched = db.query(MaterialCard).filter(MaterialCard.enriched_at.isnot(None)).count()
total = db.query(MaterialCard).filter(MaterialCard.deleted_at.is_(None)).count()
print(f'Enriched: {enriched}/{total}')
# Check categories populated
cats = db.query(MaterialCard.category).filter(MaterialCard.category.isnot(None)).distinct().all()
print(f'Categories: {[c[0] for c in cats]}')
db.close()
"
```

- [ ] **Step 5: Update APP_MAP docs**

Update `docs/APP_MAP_DATABASE.md` and `docs/APP_MAP_INTERACTIONS.md` with:
- FTS trigger on material_cards
- pg_trgm index
- Enrichment pipeline now enabled
- Materials nav tab in bottom nav
- Requirement.material_card_id linkage in search pipeline
