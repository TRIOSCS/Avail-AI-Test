# Required Manufacturer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Require manufacturer name on all part entries (primary + substitutes), with typeahead lookup, to enable accurate enrichment from real data sources.

**Architecture:** New `Manufacturer` lookup model for typeahead. Add `manufacturer` column to `Requirement`. Restructure substitutes JSON from string array to array of objects. Update `parse_substitute_mpns()` and `resolve_material_card()` signatures. All entry forms get manufacturer typeahead fields.

**Tech Stack:** SQLAlchemy, Alembic, Jinja2, HTMX, Alpine.js, Tailwind CSS

**Spec:** `docs/superpowers/specs/2026-03-22-required-manufacturer-design.md`

---

## Task Overview

| Task | Description | Dependencies |
|------|-------------|--------------|
| 1 | Manufacturer model + seed data | None |
| 2 | Manufacturer typeahead endpoint | Task 1 |
| 3 | Requirement `manufacturer` column + migration | Task 1 (Alembic chain) |
| 4 | Substitutes JSON restructure + migration + utility updates | Task 3 |
| 5 | Update HTMX entry paths + validation | Tasks 2, 3, 4 |
| 5b | Update API entry paths + validation | Tasks 3, 4 |
| 6 | UI: part entry forms with manufacturer typeahead + structured sub input | Tasks 2, 5 |
| 7 | UI: display changes (header, left panel, sibling table) | Tasks 3, 4 |

**IMPORTANT: Alembic migration sequencing** — Tasks 1 and 3 both generate migrations. Task 3's migration MUST be generated AFTER Task 1's migration is committed, so the Alembic revision chain stays linear. Do NOT run these tasks in parallel.

---

### Task 1: Manufacturer Model + Seed Data

**Files:**
- Modify: `app/models/sourcing.py:112` — add `Manufacturer` class
- Modify: `app/models/__init__.py` — export `Manufacturer`
- Create: `alembic/versions/xxx_add_manufacturer_table.py` (autogenerate)
- Modify: `app/startup.py` — add `_seed_manufacturers()`
- Create: `tests/test_manufacturer_model.py`

- [ ] **Step 1: Write test**

```python
# tests/test_manufacturer_model.py
import pytest
from sqlalchemy.exc import IntegrityError
from app.models.sourcing import Manufacturer

def test_manufacturer_create(db):
    mfr = Manufacturer(canonical_name="Texas Instruments", aliases=["TI", "Texas Inst"])
    db.add(mfr)
    db.commit()
    assert mfr.id is not None
    assert mfr.canonical_name == "Texas Instruments"
    assert "TI" in mfr.aliases

def test_manufacturer_unique_name(db):
    db.add(Manufacturer(canonical_name="Texas Instruments"))
    db.commit()
    with pytest.raises(IntegrityError):
        db.add(Manufacturer(canonical_name="Texas Instruments"))
        db.commit()
```

- [ ] **Step 2: Run test — expected FAIL**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_manufacturer_model.py -v`

- [ ] **Step 3: Add Manufacturer model**

In `app/models/sourcing.py` after the Requirement class (line 112), add:

```python
class Manufacturer(Base):
    """Manufacturer lookup for typeahead normalization.
    Called by: typeahead endpoint, startup seed
    Depends on: Base
    """
    __tablename__ = "manufacturers"
    id = Column(Integer, primary_key=True)
    canonical_name = Column(String(255), nullable=False, unique=True, index=True)
    aliases = Column(JSON, default=list)
    website = Column(String(500))
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
```

Export in `app/models/__init__.py`: `from .sourcing import Manufacturer`

- [ ] **Step 4: Generate migration**

Run: `alembic revision --autogenerate -m "add manufacturers table"`

- [ ] **Step 5: Run test — expected PASS**

- [ ] **Step 6: Add seed data in `app/startup.py`**

Add `_seed_manufacturers()` using INSERT ON CONFLICT DO NOTHING. Seed ~50 manufacturers with aliases. Call from `run_startup_migrations()`.

- [ ] **Step 7: Commit**

```bash
git add app/models/ app/startup.py alembic/versions/ tests/test_manufacturer_model.py
git commit -m "feat: add Manufacturer lookup model with seed data

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Manufacturer Typeahead Endpoint

**Files:**
- Modify: `app/routers/htmx_views.py` — add search + add routes
- Create: `app/templates/htmx/partials/manufacturers/search_results.html`
- Create: `tests/test_manufacturer_typeahead.py`

- [ ] **Step 1: Write tests**

```python
# tests/test_manufacturer_typeahead.py
def test_search_by_canonical_name(client, db):
    from app.models.sourcing import Manufacturer
    db.add(Manufacturer(canonical_name="Texas Instruments", aliases=["TI"]))
    db.commit()
    resp = client.get("/v2/partials/manufacturers/search?q=texas")
    assert resp.status_code == 200
    assert "Texas Instruments" in resp.text

def test_search_by_alias(client, db):
    from app.models.sourcing import Manufacturer
    db.add(Manufacturer(canonical_name="Texas Instruments", aliases=["TI"]))
    db.commit()
    resp = client.get("/v2/partials/manufacturers/search?q=TI")
    assert resp.status_code == 200
    assert "Texas Instruments" in resp.text

def test_search_no_match_shows_add(client, db):
    resp = client.get("/v2/partials/manufacturers/search?q=UnknownCorp")
    assert resp.status_code == 200
    assert "Add" in resp.text

def test_add_new_manufacturer(client, db):
    resp = client.post("/v2/partials/manufacturers/add", data={"name": "NewCorp"})
    assert resp.status_code == 200
    from app.models.sourcing import Manufacturer
    assert db.query(Manufacturer).filter_by(canonical_name="NewCorp").first() is not None
```

- [ ] **Step 2: Run tests — expected FAIL**

- [ ] **Step 3: Add routes**

`GET /v2/partials/manufacturers/search?q=...` — searches canonical_name + aliases (cast JSON to text, ILIKE), returns HTML partial with results. If no match, shows "Add [q] as manufacturer" button.

`POST /v2/partials/manufacturers/add` — creates new Manufacturer, returns the canonical name as a selectable element.

- [ ] **Step 4: Create search results template**

`app/templates/htmx/partials/manufacturers/search_results.html` — list of clickable manufacturer names. Clicking fills the input field via Alpine.js `$refs`. "Add new" option at bottom when no match.

- [ ] **Step 5: Run tests — expected PASS**

- [ ] **Step 6: Commit**

```bash
git add app/routers/htmx_views.py app/templates/htmx/partials/manufacturers/ tests/test_manufacturer_typeahead.py
git commit -m "feat: add manufacturer typeahead search + add-on-the-fly

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Requirement `manufacturer` Column + Migration

**Files:**
- Modify: `app/models/sourcing.py:82` — add `manufacturer` column
- Create: `alembic/versions/xxx_add_requirement_manufacturer.py`
- Create: `tests/test_requirement_manufacturer.py`

- [ ] **Step 1: Write test**

```python
# tests/test_requirement_manufacturer.py
from app.models.sourcing import Requirement, Requisition

def test_requirement_has_manufacturer(db):
    req = Requisition(name="Test", status="active", created_by=1)
    db.add(req)
    db.flush()
    r = Requirement(requisition_id=req.id, primary_mpn="LM317T", manufacturer="Texas Instruments")
    db.add(r)
    db.commit()
    assert r.manufacturer == "Texas Instruments"

def test_requirement_manufacturer_defaults_empty(db):
    req = Requisition(name="Test", status="active", created_by=1)
    db.add(req)
    db.flush()
    r = Requirement(requisition_id=req.id, primary_mpn="LM317T")
    db.add(r)
    db.commit()
    assert r.manufacturer == ""
```

- [ ] **Step 2: Run test — expected FAIL**

- [ ] **Step 3: Add column**

In `app/models/sourcing.py` after `brand = Column(String(255))` (line 82), add:

```python
    manufacturer = Column(String(255), nullable=False, server_default="")
```

- [ ] **Step 4: Generate and edit migration**

Run: `alembic revision --autogenerate -m "add manufacturer to requirements"`

Edit migration to include two-step nullable pattern + backfill:
```python
def upgrade():
    op.add_column("requirements", sa.Column("manufacturer", sa.String(255), nullable=True))
    op.execute("UPDATE requirements SET manufacturer = COALESCE(brand, '') WHERE manufacturer IS NULL")
    op.alter_column("requirements", "manufacturer", nullable=False, server_default=sa.text("''"))
    op.create_index("ix_requirements_manufacturer", "requirements", ["manufacturer"])
```

- [ ] **Step 5: Run test — expected PASS**

- [ ] **Step 6: Commit**

```bash
git add app/models/sourcing.py alembic/versions/ tests/test_requirement_manufacturer.py
git commit -m "feat: add required manufacturer column to Requirement

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Substitutes JSON Restructure + Utility Updates

**Files:**
- Create: `alembic/versions/xxx_restructure_substitutes_json.py` (manual)
- Modify: `app/utils/normalization.py:408-427` — rewrite `parse_substitute_mpns`
- Modify: `app/search_service.py:1423` — add `manufacturer` param to `resolve_material_card`
- Create: `tests/test_substitutes_restructure.py`

- [ ] **Step 1: Write tests for new `parse_substitute_mpns`**

```python
# tests/test_substitutes_restructure.py
from app.utils.normalization import parse_substitute_mpns

def test_parse_subs_new_format():
    subs = [{"mpn": "LM338T", "manufacturer": "TI"}, {"mpn": "SG3525", "manufacturer": "ON Semi"}]
    result = parse_substitute_mpns(subs, "LM317T")
    assert len(result) == 2
    assert result[0]["mpn"] == "LM338T"
    assert result[0]["manufacturer"] == "TI"

def test_parse_subs_excludes_primary():
    subs = [{"mpn": "LM317T", "manufacturer": "TI"}, {"mpn": "LM338T", "manufacturer": "TI"}]
    result = parse_substitute_mpns(subs, "LM317T")
    assert len(result) == 1

def test_parse_subs_deduplicates():
    subs = [{"mpn": "LM338T", "manufacturer": "TI"}, {"mpn": "LM-338T", "manufacturer": "TI"}]
    result = parse_substitute_mpns(subs, "LM317T")
    assert len(result) == 1

def test_parse_subs_respects_limit():
    subs = [{"mpn": f"MPN{i}", "manufacturer": "Test"} for i in range(30)]
    result = parse_substitute_mpns(subs, "PRIMARY", limit=5)
    assert len(result) == 5

def test_parse_subs_empty():
    assert parse_substitute_mpns([], "LM317T") == []

def test_parse_subs_skips_empty_mpn():
    subs = [{"mpn": "", "manufacturer": "TI"}, {"mpn": "LM338T", "manufacturer": "TI"}]
    result = parse_substitute_mpns(subs, "LM317T")
    assert len(result) == 1
```

- [ ] **Step 2: Run tests — expected FAIL**

- [ ] **Step 3: Rewrite `parse_substitute_mpns`**

Replace function at `app/utils/normalization.py:408-427`:

```python
def parse_substitute_mpns(
    subs: list[dict], primary_mpn: str, *, limit: int = MAX_SUBSTITUTES
) -> list[dict]:
    """Parse structured substitute list, normalize MPNs, and deduplicate.

    Each sub is a dict with 'mpn' and 'manufacturer' keys.
    Returns normalized, deduped list capped at limit.

    Called by: htmx_views.py (add/update/header-save endpoints)
    Depends on: normalize_mpn, normalize_mpn_key
    """
    result: list[dict] = []
    if not subs:
        return result
    seen_keys = {normalize_mpn_key(primary_mpn)}
    for sub in subs:
        raw_mpn = sub.get("mpn", "").strip()
        if not raw_mpn:
            continue
        ns = normalize_mpn(raw_mpn) or raw_mpn
        key = normalize_mpn_key(ns)
        if key and key not in seen_keys:
            seen_keys.add(key)
            result.append({
                "mpn": ns,
                "manufacturer": sub.get("manufacturer", "").strip(),
            })
    return result[:limit]
```

- [ ] **Step 4: Run tests — expected PASS**

- [ ] **Step 5: Update `resolve_material_card` signature**

In `app/search_service.py:1423`, change:
```python
def resolve_material_card(mpn: str, db: Session) -> MaterialCard | None:
```
To:
```python
def resolve_material_card(mpn: str, db: Session, manufacturer: str = "") -> MaterialCard | None:
```

Add manufacturer update logic before each `return card`:
```python
    if card and manufacturer and not card.manufacturer:
        card.manufacturer = manufacturer
```

For new card creation in the `.values(...)` block, add `manufacturer=manufacturer`.

- [ ] **Step 6: Write and apply migration**

Create manual migration: `alembic revision -m "restructure substitutes json and substitutes_text"`

Upgrade: Convert string arrays to object arrays, drop/recreate `substitutes_text` generated column with MPN-only extraction.

Downgrade: Reverse the conversion, restore original `substitutes_text` definition.

See spec Section 3 for exact SQL.

- [ ] **Step 7: Run all related tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_substitutes_restructure.py tests/test_htmx_material_card_auto.py -v`

- [ ] **Step 8: Commit**

```bash
git add app/utils/normalization.py app/search_service.py alembic/versions/ tests/test_substitutes_restructure.py
git commit -m "feat: restructure substitutes JSON to objects with manufacturer

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Update HTMX Entry Paths + Validation

**Files:**
- Modify: `app/routers/htmx_views.py` — lines 738-762 (import-save), 1070-1122 (add_requirement), 2795-2850 (update_requirement), 9050-9064 (header save)
- Create: `tests/test_manufacturer_validation.py`

- [ ] **Step 1: Write validation tests**

Test that each entry path rejects empty manufacturer and accepts valid manufacturer.

- [ ] **Step 2: Update all entry paths**

For each path:
1. Add `manufacturer: str = Form("")` parameter
2. Validate non-empty: `if not manufacturer.strip(): raise HTTPException(422, "Manufacturer required")`
3. Set on Requirement: `manufacturer=manufacturer.strip()`
4. Zip sub form arrays into dicts: `[{"mpn": m, "manufacturer": mfr} for m, mfr in zip(form.getlist("sub_mpn"), form.getlist("sub_manufacturer")) if m.strip()]`
5. Call new `parse_substitute_mpns(subs_raw, primary_mpn)`
6. Update resolve loops: `resolve_material_card(sub["mpn"], db, manufacturer=sub.get("manufacturer", ""))`

Header save (line 9050): Add `elif field == "manufacturer"` block. Restructure `elif field == "substitutes"` to accept structured JSON input from the header's structured sub editor.

- [ ] **Step 3: Run tests — expected PASS**

- [ ] **Step 4: Run full test suite**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v --tb=short -q`

- [ ] **Step 5: Commit**

```bash
git add app/routers/htmx_views.py tests/test_manufacturer_validation.py
git commit -m "feat: require manufacturer on all HTMX part entry paths

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 5b: Update API Entry Paths + Validation

**Files:**
- Modify: `app/routers/requisitions/requirements.py` — add manufacturer validation
- Modify: `app/schemas/` — update Pydantic schemas to require manufacturer
- Create: `tests/test_api_manufacturer_validation.py`

- [ ] **Step 1: Write tests**

```python
# tests/test_api_manufacturer_validation.py
def test_api_create_requirement_requires_manufacturer(client, db):
    """API POST without manufacturer should be rejected."""
    from app.models.sourcing import Requisition
    req = Requisition(name="Test", status="active", created_by=1)
    db.add(req)
    db.commit()
    resp = client.post(f"/api/requisitions/{req.id}/requirements", json={
        "primary_mpn": "LM317T",
        "target_qty": 100,
    })
    assert resp.status_code in (400, 422)

def test_api_create_requirement_with_manufacturer(client, db):
    """API POST with manufacturer should succeed."""
    from app.models.sourcing import Requisition
    req = Requisition(name="Test", status="active", created_by=1)
    db.add(req)
    db.commit()
    resp = client.post(f"/api/requisitions/{req.id}/requirements", json={
        "primary_mpn": "LM317T",
        "manufacturer": "Texas Instruments",
        "target_qty": 100,
    })
    assert resp.status_code == 200
```

- [ ] **Step 2: Update API router and schemas**

In `app/routers/requisitions/requirements.py`, add `manufacturer` as a required field in the create/update endpoints. Update the corresponding Pydantic request schemas to include `manufacturer: str`.

Update `resolve_material_card` calls to pass manufacturer. Update `parse_substitute_mpns` calls to use new dict format.

- [ ] **Step 3: Run tests — expected PASS**

- [ ] **Step 4: Commit**

```bash
git add app/routers/requisitions/ app/schemas/ tests/test_api_manufacturer_validation.py
git commit -m "feat: require manufacturer on API requirement endpoints

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: UI — Part Entry Forms + Structured Sub Input

**Files:**
- Modify: `app/templates/htmx/partials/requisitions/tabs/parts.html` — add/edit forms
- Modify: import form template (if separate)
- Modify: `app/templates/htmx/partials/parts/header.html` — structured sub editor

- [ ] **Step 1: Add manufacturer typeahead to add-requirement form**

After MPN input, add manufacturer text input with HTMX typeahead to `/v2/partials/manufacturers/search`. Use Alpine.js `x-ref` for the dropdown positioning.

- [ ] **Step 2: Replace comma-separated sub input with structured rows**

Alpine.js `x-data="{ subs: [] }"` manages dynamic rows. Each row: MPN input (`name="sub_mpn[]"`) + manufacturer typeahead input (`name="sub_manufacturer[]"`) + remove button. "Add substitute" button appends a new row.

- [ ] **Step 3: Update header substitutes editor**

The click-to-edit for subs in `header.html` changes from a single text input to the structured row format. The `PATCH /v2/partials/parts/{id}/header` route receives the parallel arrays.

- [ ] **Step 4: Verify forms render and submit**

Run: `docker compose up -d --build`
Manual verification.

- [ ] **Step 5: Commit**

```bash
git add app/templates/
git commit -m "feat: manufacturer typeahead + structured sub input on all entry forms

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: UI — Display Changes

**Files:**
- Modify: `app/templates/htmx/partials/parts/header.html` — show manufacturer
- Modify: `app/templates/htmx/partials/parts/list.html` — sub chip tooltips
- Modify: `app/templates/htmx/partials/parts/tabs/req_details.html` — Mfr column
- Modify: `app/routers/htmx_views.py` — update `sub_card_map` for new format

- [ ] **Step 1: Show manufacturer in header**

After MPN in `header.html`, add `· {{ requirement.manufacturer }}` with click-to-edit (typeahead).

- [ ] **Step 2: Update sub chips for new dict format**

In `list.html` and `header.html`, subs are now dicts. Access `sub.mpn` and `sub.manufacturer` (with `sub is mapping` guard for transition safety).

Left panel sub chips: MPN in chip text, `title="{{ sub.mpn }} — {{ sub.manufacturer }}"` for tooltip.

Header sub chips: Show as `MPN (Mfr)` format.

- [ ] **Step 3: Update `sub_card_map` in htmx_views.py**

Change `all_sub_mpns.extend(r.substitutes)` to iterate dicts and extract `mpn` key.

- [ ] **Step 4: Add Mfr column to sibling table**

In `req_details.html`, add `<th>Mfr</th>` header and `<td>{{ part.manufacturer or '...' }}</td>` cell.

- [ ] **Step 5: Verify all displays**

Run: `docker compose up -d --build`

- [ ] **Step 6: Run full test suite**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v --tb=short -q`

- [ ] **Step 7: Commit**

```bash
git add app/templates/ app/routers/htmx_views.py
git commit -m "feat: display manufacturer in header, sub chips, and sibling table

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```
