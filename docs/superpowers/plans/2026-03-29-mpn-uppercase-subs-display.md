# MPN Auto-Capitalization + Substitutes Display Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Auto-uppercase all MPN-like fields on save, fix substitutes display with amber chips, and ensure consistent dict-format subs across all write paths.

**Architecture:** Model-level `@validates` catches every ORM write path for uppercase. A `sub_mpns` Jinja2 filter extracts clean MPNs from mixed-format subs. JSON API paths fixed to store dict subs. Backfill migration uppercases existing data via pure SQL.

**Tech Stack:** SQLAlchemy 2.0, Alembic, Jinja2, Tailwind CSS, PostgreSQL jsonb

**Spec:** `docs/superpowers/specs/2026-03-29-mpn-uppercase-subs-display-design.md`

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `app/models/sourcing.py` | Modify | Add `@validates` for MPN uppercase |
| `app/template_env.py` | Modify | Add `sub_mpns` filter |
| `app/templates/htmx/partials/requisitions/tabs/req_row.html` | Modify | Amber chips + uppercase CSS on MPN cell |
| `app/routers/requisitions/requirements.py` | Modify | Fix JSON API create/PATCH to store dict subs |
| `alembic/versions/[auto].py` | Create | Backfill migration |
| `tests/test_mpn_uppercase.py` | Create | Tests for validator, filter, and API paths |

---

### Task 1: Add `@validates` for MPN Uppercase on Requirement Model

**Files:**
- Modify: `app/models/sourcing.py:145` (after existing `_validate_priority_score`)
- Create: `tests/test_mpn_uppercase.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_mpn_uppercase.py`:

```python
"""Tests for MPN auto-capitalization and substitutes display.

Called by: pytest
Depends on: conftest.py fixtures, app models, app constants
"""

from app.constants import RequisitionStatus, SourcingStatus
from app.models.sourcing import Requirement, Requisition


class TestMPNUppercaseValidator:
    def test_primary_mpn_uppercased_on_create(self, db_session):
        req = Requisition(name="Test", status=RequisitionStatus.ACTIVE, customer_name="Acme")
        db_session.add(req)
        db_session.flush()
        r = Requirement(
            requisition_id=req.id,
            primary_mpn="ne5559",
            manufacturer="TestMfr",
            target_qty=100,
            sourcing_status=SourcingStatus.OPEN,
        )
        db_session.add(r)
        db_session.flush()
        assert r.primary_mpn == "NE5559"

    def test_primary_mpn_uppercased_on_update(self, db_session):
        req = Requisition(name="Test", status=RequisitionStatus.ACTIVE, customer_name="Acme")
        db_session.add(req)
        db_session.flush()
        r = Requirement(
            requisition_id=req.id,
            primary_mpn="ABC123",
            manufacturer="TestMfr",
            target_qty=100,
            sourcing_status=SourcingStatus.OPEN,
        )
        db_session.add(r)
        db_session.flush()
        r.primary_mpn = "xyz789"
        assert r.primary_mpn == "XYZ789"

    def test_customer_pn_uppercased(self, db_session):
        req = Requisition(name="Test", status=RequisitionStatus.ACTIVE, customer_name="Acme")
        db_session.add(req)
        db_session.flush()
        r = Requirement(
            requisition_id=req.id,
            primary_mpn="ABC123",
            manufacturer="TestMfr",
            target_qty=100,
            sourcing_status=SourcingStatus.OPEN,
            customer_pn="cust-part-01",
        )
        db_session.add(r)
        db_session.flush()
        assert r.customer_pn == "CUST-PART-01"

    def test_oem_pn_uppercased(self, db_session):
        req = Requisition(name="Test", status=RequisitionStatus.ACTIVE, customer_name="Acme")
        db_session.add(req)
        db_session.flush()
        r = Requirement(
            requisition_id=req.id,
            primary_mpn="ABC123",
            manufacturer="TestMfr",
            target_qty=100,
            sourcing_status=SourcingStatus.OPEN,
            oem_pn="oem-part-x",
        )
        db_session.add(r)
        db_session.flush()
        assert r.oem_pn == "OEM-PART-X"

    def test_none_passes_through(self, db_session):
        req = Requisition(name="Test", status=RequisitionStatus.ACTIVE, customer_name="Acme")
        db_session.add(req)
        db_session.flush()
        r = Requirement(
            requisition_id=req.id,
            primary_mpn="ABC123",
            manufacturer="TestMfr",
            target_qty=100,
            sourcing_status=SourcingStatus.OPEN,
            customer_pn=None,
        )
        db_session.add(r)
        db_session.flush()
        assert r.customer_pn is None

    def test_strips_whitespace(self, db_session):
        req = Requisition(name="Test", status=RequisitionStatus.ACTIVE, customer_name="Acme")
        db_session.add(req)
        db_session.flush()
        r = Requirement(
            requisition_id=req.id,
            primary_mpn="  abc123  ",
            manufacturer="TestMfr",
            target_qty=100,
            sourcing_status=SourcingStatus.OPEN,
        )
        db_session.add(r)
        db_session.flush()
        assert r.primary_mpn == "ABC123"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_mpn_uppercase.py::TestMPNUppercaseValidator -v --override-ini="addopts="`
Expected: FAIL — `assert r.primary_mpn == "NE5559"` fails because `r.primary_mpn` is `"ne5559"`

- [ ] **Step 3: Add the validator to the model**

In `app/models/sourcing.py`, after the `_validate_priority_score` validator (after line 145), add:

```python
    @validates("primary_mpn", "customer_pn", "oem_pn")
    def _uppercase_mpn_fields(self, _key, value):
        return value.upper().strip() if value else value
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_mpn_uppercase.py::TestMPNUppercaseValidator -v --override-ini="addopts="`
Expected: All 6 tests PASS

- [ ] **Step 5: Commit**

```bash
cd /root/availai
git add app/models/sourcing.py tests/test_mpn_uppercase.py
git commit -m "feat: auto-uppercase primary_mpn, customer_pn, oem_pn via @validates

Model-level validator catches every ORM write path including
inline edit, form POST, API, imports, and background jobs."
```

---

### Task 2: Add `sub_mpns` Jinja2 Filter

**Files:**
- Modify: `app/template_env.py` (append after line 150)
- Modify: `tests/test_mpn_uppercase.py` (add filter tests)

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_mpn_uppercase.py`:

```python
from app.template_env import _sub_mpns_filter


class TestSubMpnsFilter:
    def test_empty_input(self):
        assert _sub_mpns_filter(None) == []
        assert _sub_mpns_filter([]) == []

    def test_string_subs(self):
        result = _sub_mpns_filter(["ne5559", "esp32-wrover-e"])
        assert result == ["NE5559", "ESP32-WROVER-E"]

    def test_dict_subs(self):
        result = _sub_mpns_filter([
            {"mpn": "17p9905", "manufacturer": "TI"},
            {"mpn": "SL9bt", "manufacturer": ""},
        ])
        assert result == ["17P9905", "SL9BT"]

    def test_mixed_format(self):
        result = _sub_mpns_filter([
            "abc123",
            {"mpn": "def456", "manufacturer": "Analog"},
        ])
        assert result == ["ABC123", "DEF456"]

    def test_skips_empty_mpn(self):
        result = _sub_mpns_filter([
            {"mpn": "", "manufacturer": "TI"},
            {"mpn": None, "manufacturer": ""},
            "",
        ])
        assert result == []

    def test_skips_short_mpn(self):
        """normalize_mpn returns None for MPNs shorter than 3 chars."""
        result = _sub_mpns_filter(["AB", {"mpn": "XY", "manufacturer": ""}])
        assert result == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_mpn_uppercase.py::TestSubMpnsFilter -v --override-ini="addopts="`
Expected: FAIL — `ImportError: cannot import name '_sub_mpns_filter'`

- [ ] **Step 3: Add the filter to template_env.py**

Append to `app/template_env.py` after line 150 (`templates.env.filters["sanitize_html"] = _sanitize_html_filter`):

```python


def _sub_mpns_filter(subs):
    """Extract clean uppercase MPN strings from substitutes.

    Handles both string-format and dict-format subs.
    Delegates to normalize_mpn() for consistent normalization.
    """
    from .utils.normalization import normalize_mpn

    if not subs:
        return []
    result = []
    for s in subs:
        raw = s if isinstance(s, str) else (s.get("mpn") or "") if isinstance(s, dict) else ""
        mpn = normalize_mpn(raw)
        if mpn:
            result.append(mpn)
    return result


templates.env.filters["sub_mpns"] = _sub_mpns_filter
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_mpn_uppercase.py::TestSubMpnsFilter -v --override-ini="addopts="`
Expected: All 6 tests PASS

- [ ] **Step 5: Commit**

```bash
cd /root/availai
git add app/template_env.py tests/test_mpn_uppercase.py
git commit -m "feat: add sub_mpns Jinja2 filter for clean substitute display

Extracts uppercase MPNs from both string and dict format subs.
Delegates to normalize_mpn() for consistent normalization."
```

---

### Task 3: Replace Substitutes Badge with Amber Chips in `req_row.html`

**Files:**
- Modify: `app/templates/htmx/partials/requisitions/tabs/req_row.html:16-17,40-46`

- [ ] **Step 1: Add `uppercase` CSS class to MPN display cell**

In `app/templates/htmx/partials/requisitions/tabs/req_row.html`, line 16 currently reads:

```html
  <td data-col-key="mpn" class="px-4 py-2.5 text-sm font-mono font-medium text-gray-900" x-show="!editing" x-cloak>
```

Add `uppercase` to the class list:

```html
  <td data-col-key="mpn" class="px-4 py-2.5 text-sm font-mono font-medium text-gray-900 uppercase" x-show="!editing" x-cloak>
```

- [ ] **Step 2: Replace the substitutes cell**

Replace lines 40-46 (the buggy badge):

```html
  <td data-col-key="substitutes" class="px-4 py-2.5 text-sm text-gray-500" x-show="!editing" x-cloak>
    {% if r.substitutes %}
      <span class="px-1.5 py-0.5 text-[10px] font-medium rounded-full bg-amber-50 text-amber-600 border border-amber-200" title="{{ r.substitutes|join(', ') }}">+{{ r.substitutes|length }} sub{{ 's' if r.substitutes|length != 1 }}</span>
    {% else %}
      —
    {% endif %}
  </td>
```

With the amber chips:

```html
  <td data-col-key="substitutes" class="px-4 py-2.5 text-sm" x-show="!editing" x-cloak>
    {% set mpns = r.substitutes|sub_mpns %}
    {% if mpns %}
      <div class="flex flex-wrap gap-1">
        {% for mpn in mpns %}
        <span class="px-1.5 py-0.5 text-[10px] font-mono font-medium rounded bg-amber-50 text-amber-700 border border-amber-200">{{ mpn }}</span>
        {% endfor %}
      </div>
    {% else %}
      <span class="text-gray-400">—</span>
    {% endif %}
  </td>
```

- [ ] **Step 3: Verify template renders**

Run: `cd /root/availai && python3 -c "from app.template_env import templates; t = templates.get_template('htmx/partials/requisitions/tabs/req_row.html'); print('OK')"`
Expected: Prints `OK`

- [ ] **Step 4: Commit**

```bash
cd /root/availai
git add app/templates/htmx/partials/requisitions/tabs/req_row.html
git commit -m "feat: replace substitutes badge with amber MPN chips

Each substitute MPN displayed as its own chip with font-mono.
Added uppercase CSS class to MPN display cell as belt-and-suspenders."
```

---

### Task 4: Fix JSON API Create/PATCH to Store Dict Subs

**Files:**
- Modify: `app/routers/requisitions/requirements.py:378-383,714-723`
- Modify: `tests/test_mpn_uppercase.py` (add API tests)

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_mpn_uppercase.py`:

```python
class TestAPISubstituteFormat:
    def test_batch_create_stores_dict_subs(self, client, db_session):
        """POST /api/requisitions/{id}/requirements should store subs as dicts."""
        req = Requisition(name="API Test", status=RequisitionStatus.ACTIVE, customer_name="Acme")
        db_session.add(req)
        db_session.commit()
        resp = client.post(
            f"/api/requisitions/{req.id}/requirements",
            json={"primary_mpn": "TEST-001", "manufacturer": "TestMfr", "target_qty": 100, "substitutes": ["alt-001", "alt-002"]},
        )
        assert resp.status_code == 200
        # Fetch the created requirement
        r = db_session.query(Requirement).filter_by(requisition_id=req.id).first()
        assert r is not None
        assert r.substitutes is not None
        assert len(r.substitutes) > 0
        # Each sub should be a dict with 'mpn' key, not a plain string
        for sub in r.substitutes:
            assert isinstance(sub, dict), f"Expected dict, got {type(sub)}: {sub}"
            assert "mpn" in sub

    def test_patch_stores_dict_subs(self, client, db_session):
        """PATCH should store subs as dicts."""
        req = Requisition(name="API Test", status=RequisitionStatus.ACTIVE, customer_name="Acme")
        db_session.add(req)
        db_session.flush()
        r = Requirement(
            requisition_id=req.id,
            primary_mpn="PATCH-001",
            manufacturer="TestMfr",
            target_qty=100,
            sourcing_status=SourcingStatus.OPEN,
        )
        db_session.add(r)
        db_session.commit()
        resp = client.patch(
            f"/api/requisitions/{req.id}/requirements/{r.id}",
            json={"substitutes": ["sub-a", "sub-b"]},
        )
        assert resp.status_code == 200
        db_session.refresh(r)
        assert r.substitutes is not None
        for sub in r.substitutes:
            assert isinstance(sub, dict), f"Expected dict, got {type(sub)}: {sub}"
            assert "mpn" in sub
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_mpn_uppercase.py::TestAPISubstituteFormat -v --override-ini="addopts="`
Expected: FAIL — `assert isinstance(sub, dict)` fails because subs are stored as strings

- [ ] **Step 3: Fix the batch create path**

In `app/routers/requisitions/requirements.py`, find the dedup loop (approximately lines 378-383):

```python
        deduped_subs = []
        for s in parsed.substitutes:
            key = normalize_mpn_key(s)
            if key and key not in seen_keys:
                seen_keys.add(key)
                deduped_subs.append(s)
```

Replace with:

```python
        deduped_subs = []
        for s in parsed.substitutes:
            ns = normalize_mpn(s) or s.strip()
            key = normalize_mpn_key(ns)
            if key and key not in seen_keys:
                seen_keys.add(key)
                deduped_subs.append({"mpn": ns, "manufacturer": ""})
```

Make sure `normalize_mpn` is imported at the top of the file. Check existing imports — it likely already is via `from ..utils.normalization import normalize_mpn, normalize_mpn_key`.

- [ ] **Step 4: Fix the PATCH path**

In the same file, find the PATCH dedup loop (approximately lines 714-723):

```python
        deduped = []
        for s in data.substitutes:
            ns = normalize_mpn(s) or s.strip()
            key = normalize_mpn_key(ns)
            if key and key not in seen_keys:
                seen_keys.add(key)
                deduped.append(ns)
        r.substitutes = deduped[:20]
```

Replace with:

```python
        deduped = []
        for s in data.substitutes:
            ns = normalize_mpn(s) or s.strip()
            key = normalize_mpn_key(ns)
            if key and key not in seen_keys:
                seen_keys.add(key)
                deduped.append({"mpn": ns, "manufacturer": ""})
        r.substitutes = deduped[:20]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_mpn_uppercase.py -v --override-ini="addopts="`
Expected: All tests PASS

- [ ] **Step 6: Run existing requirement tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_routers.py -k requisition -v --override-ini="addopts=" --timeout=30`
Expected: All existing tests still pass (or identify any that assumed string subs)

- [ ] **Step 7: Commit**

```bash
cd /root/availai
git add app/routers/requisitions/requirements.py tests/test_mpn_uppercase.py
git commit -m "fix: JSON API create/PATCH stores subs as dicts, not strings

Prevents recurring format inconsistency after backfill.
Each sub stored as {\"mpn\": \"...\", \"manufacturer\": \"\"}."
```

---

### Task 5: Backfill Migration

**Files:**
- Create: `alembic/versions/[auto].py`

- [ ] **Step 1: Create the migration**

Run: `cd /root/availai && alembic revision -m "backfill_mpn_uppercase_and_normalize_subs"`

- [ ] **Step 2: Write the upgrade function**

Edit the generated migration file. Replace the empty `upgrade()` with:

```python
from alembic import op
from sqlalchemy import text


def upgrade() -> None:
    """Uppercase all MPN-like fields and normalize substitute JSON format."""
    conn = op.get_bind()

    # 1. Uppercase string columns
    conn.execute(text("""
        UPDATE requirements SET primary_mpn = UPPER(TRIM(primary_mpn))
        WHERE primary_mpn IS NOT NULL AND primary_mpn != UPPER(TRIM(primary_mpn))
    """))
    conn.execute(text("""
        UPDATE requirements SET customer_pn = UPPER(TRIM(customer_pn))
        WHERE customer_pn IS NOT NULL AND customer_pn != UPPER(TRIM(customer_pn))
    """))
    conn.execute(text("""
        UPDATE requirements SET oem_pn = UPPER(TRIM(oem_pn))
        WHERE oem_pn IS NOT NULL AND oem_pn != UPPER(TRIM(oem_pn))
    """))

    # 2. Uppercase MPNs inside JSON substitutes column (handles both string and dict formats)
    conn.execute(text("""
        UPDATE requirements
        SET substitutes = (
            SELECT jsonb_agg(
                CASE
                    WHEN jsonb_typeof(elem) = 'string'
                    THEN to_jsonb(UPPER(TRIM(elem #>> '{}')))
                    WHEN jsonb_typeof(elem) = 'object'
                    THEN jsonb_set(elem, '{mpn}', to_jsonb(UPPER(TRIM(elem ->> 'mpn'))))
                    ELSE elem
                END
            )
            FROM jsonb_array_elements(substitutes::jsonb) AS elem
        )
        WHERE substitutes IS NOT NULL
          AND substitutes::text != '[]'
    """))


def downgrade() -> None:
    """No-op — uppercasing is non-destructive and cannot be reversed."""
    pass
```

- [ ] **Step 3: Add file header comment**

Add this docstring at the top of the migration file (after the Alembic-generated header):

```python
"""Backfill: uppercase all MPN fields and normalize substitutes JSON.

Called by: alembic upgrade head
Depends on: requirements table (primary_mpn, customer_pn, oem_pn, substitutes columns)
"""
```

- [ ] **Step 4: Verify migration has a single head**

Run: `cd /root/availai && alembic heads`
Expected: Single head (if multiple heads, create a merge migration first)

- [ ] **Step 5: Commit**

```bash
cd /root/availai
git add alembic/versions/
git commit -m "feat: backfill migration — uppercase MPNs and normalize subs JSON

Pure SQL: UPPER(TRIM()) on primary_mpn, customer_pn, oem_pn.
jsonb_agg for substitutes handles both string and dict formats."
```

---

### Task 6: Full Test Suite + Lint + Final Verification

**Files:**
- All modified files from Tasks 1-5

- [ ] **Step 1: Run all feature tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_mpn_uppercase.py -v --override-ini="addopts="`
Expected: All tests pass

- [ ] **Step 2: Run full test suite**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v --override-ini="addopts=" --timeout=30`
Expected: All tests pass

- [ ] **Step 3: Run linter**

Run: `cd /root/availai && ruff check app/models/sourcing.py app/template_env.py app/routers/requisitions/requirements.py`
Expected: No lint errors

- [ ] **Step 4: Run type checker**

Run: `cd /root/availai && mypy app/models/sourcing.py app/template_env.py app/routers/requisitions/requirements.py`
Expected: No new type errors

- [ ] **Step 5: Format**

Run: `cd /root/availai && ruff format app/models/sourcing.py app/template_env.py app/routers/requisitions/requirements.py`

- [ ] **Step 6: Final commit if any formatting changes**

```bash
cd /root/availai
git add -u
git diff --cached --stat
git commit -m "style: format MPN uppercase + subs display code"
```
