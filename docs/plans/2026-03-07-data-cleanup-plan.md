# Data Cleanup Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Dedup SiteContacts, normalize all phone fields to E.164, extract phone numbers from site_name into a new contact_phone_2 field, and add forward guards.

**Architecture:** Single Alembic migration (055) adds contact_phone_2 column and runs 3 data fixes inline. Schema validators and router-level checks prevent recurrence.

**Tech Stack:** SQLAlchemy 2.0, Alembic, Pydantic validators, regex

---

### Task 1: Add contact_phone_2 to CustomerSite Model

**Files:**
- Modify: `app/models/crm.py:102` (add column after contact_phone)
- Modify: `app/schemas/crm.py:132-161` (add to SiteCreate, SiteUpdate, SiteOut)

**Step 1: Add column to model**

In `app/models/crm.py`, after line 102 (`contact_phone`), add:

```python
    contact_phone_2 = Column(String(100))
```

**Step 2: Add field to SiteCreate schema**

In `app/schemas/crm.py`, class `SiteCreate`, add:

```python
    contact_phone_2: str | None = None
```

Also add the field to `SiteUpdate` and `SiteOut` schemas if they exist.

**Step 3: Commit**

```bash
git add app/models/crm.py app/schemas/crm.py
git commit -m "model: add contact_phone_2 to CustomerSite"
```

---

### Task 2: Write Migration 055 — Schema + Data Fixes

**Files:**
- Create: `alembic/versions/055_data_cleanup.py`

**Step 1: Generate migration skeleton**

```bash
docker compose exec app alembic revision --autogenerate -m "data_cleanup_contact_phone_2"
```

Verify it picks up only `contact_phone_2` addition. Rename file to `055_data_cleanup.py`.

**Step 2: Add data fix functions to migration**

After the `upgrade()` schema change (add_column), add three Python data fix functions that run via `op.execute()` or use `op.get_bind()` for Python-level logic:

**Fix A — SiteContact dedup:**
```python
def _dedup_site_contacts(conn):
    """Merge duplicate SiteContacts sharing (customer_site_id, lower(email))."""
    dupes = conn.execute(text("""
        SELECT customer_site_id, lower(email) as em, array_agg(id ORDER BY id) as ids
        FROM site_contacts
        WHERE email IS NOT NULL
        GROUP BY customer_site_id, lower(email)
        HAVING count(*) > 1
    """)).fetchall()
    for row in dupes:
        ids = row.ids
        # Keep richest record (most non-null fields), merge into it
        contacts = conn.execute(text(
            "SELECT * FROM site_contacts WHERE id = ANY(:ids)"
        ), {"ids": ids}).fetchall()
        # Score by non-null field count
        best = max(contacts, key=lambda c: sum(1 for v in c if v is not None))
        delete_ids = [c.id for c in contacts if c.id != best.id]
        # Merge: fill NULLs in best from others
        for other in contacts:
            if other.id == best.id:
                continue
            for col in ['full_name', 'title', 'phone', 'notes', 'linkedin_url']:
                if getattr(best, col, None) is None and getattr(other, col, None) is not None:
                    conn.execute(text(f"UPDATE site_contacts SET {col} = :val WHERE id = :id"),
                                 {"val": getattr(other, col), "id": best.id})
        # Delete dupes
        conn.execute(text("DELETE FROM site_contacts WHERE id = ANY(:ids)"), {"ids": delete_ids})
```

**Fix B — Phone normalization:**
```python
def _normalize_phones(conn):
    """Normalize all phone fields to E.164 format."""
    tables_cols = [
        ("companies", "phone"),
        ("customer_sites", "contact_phone"),
        ("customer_sites", "contact_phone_2"),
        ("site_contacts", "phone"),
        ("vendor_contacts", "phone"),
        ("vendor_contacts", "phone_mobile"),
    ]
    for table, col in tables_cols:
        rows = conn.execute(text(f"""
            SELECT id, {col} FROM {table}
            WHERE {col} IS NOT NULL AND {col} != ''
              AND {col} NOT LIKE '+%%'
        """)).fetchall()
        for row in rows:
            from app.utils.phone_utils import format_phone_e164
            normalized = format_phone_e164(getattr(row, col))
            if normalized and normalized != getattr(row, col):
                conn.execute(text(f"UPDATE {table} SET {col} = :val WHERE id = :id"),
                             {"val": normalized, "id": row.id})
```

**Fix C — Extract phones from site_name:**
```python
import re

PHONE_RE = re.compile(
    r'[\(+]?\d[\d\s\-\(\)\.]{8,}\d'
)

def _extract_phones_from_site_name(conn):
    """Extract phone numbers embedded in customer_sites.site_name."""
    rows = conn.execute(text("""
        SELECT id, site_name, contact_phone, contact_phone_2
        FROM customer_sites WHERE site_name IS NOT NULL
    """)).fetchall()
    for row in rows:
        match = PHONE_RE.search(row.site_name)
        if not match:
            continue
        raw_phone = match.group(0).strip()
        from app.utils.phone_utils import format_phone_e164
        e164 = format_phone_e164(raw_phone)
        if not e164:
            continue
        clean_name = row.site_name[:match.start()] + row.site_name[match.end():]
        clean_name = re.sub(r'\s+', ' ', clean_name).strip(' -–—,')
        target_col = "contact_phone" if not row.contact_phone else "contact_phone_2"
        conn.execute(text(f"""
            UPDATE customer_sites
            SET site_name = :name, {target_col} = :phone
            WHERE id = :id
        """), {"name": clean_name, "phone": e164, "id": row.id})
```

**Step 3: Wire into upgrade()**

```python
def upgrade():
    op.add_column("customer_sites", sa.Column("contact_phone_2", sa.String(100), nullable=True))
    conn = op.get_bind()
    _dedup_site_contacts(conn)
    _normalize_phones(conn)
    _extract_phones_from_site_name(conn)

def downgrade():
    op.drop_column("customer_sites", "contact_phone_2")
```

**Step 4: Test migration**

```bash
docker compose exec app alembic upgrade head
docker compose exec app alembic downgrade -1
docker compose exec app alembic upgrade head
```

**Step 5: Commit**

```bash
git add alembic/versions/055_data_cleanup.py
git commit -m "migration: 055 data cleanup — dedup, phone normalize, site_name extract"
```

---

### Task 3: Add Forward Guard — SiteContact Dedup on Create

**Files:**
- Modify: `app/routers/crm/sites.py:267-285` (create_site_contact endpoint)
- Test: `tests/test_data_cleanup.py`

**Step 1: Write failing test**

```python
# tests/test_data_cleanup.py
def test_create_site_contact_dedup_by_email(client, db):
    """Creating a contact with same email on same site returns existing."""
    from app.models.crm import Company, CustomerSite, SiteContact
    co = Company(name="TestCo")
    db.add(co)
    db.flush()
    site = CustomerSite(company_id=co.id, site_name="HQ")
    db.add(site)
    db.flush()
    existing = SiteContact(customer_site_id=site.id, full_name="John", email="john@test.com")
    db.add(existing)
    db.commit()

    resp = client.post(f"/api/sites/{site.id}/contacts", json={
        "full_name": "Johnny", "email": "john@test.com"
    })
    assert resp.status_code == 200
    assert resp.json()["id"] == existing.id  # returned existing, not new
    assert db.query(SiteContact).filter_by(customer_site_id=site.id).count() == 1
```

**Step 2: Run test to verify it fails**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_data_cleanup.py::test_create_site_contact_dedup_by_email -v
```

Expected: FAIL (currently creates duplicate)

**Step 3: Add dedup check to create_site_contact**

In `app/routers/crm/sites.py`, before line 282 (`contact = SiteContact(...)`), add:

```python
    if payload.email:
        existing = db.query(SiteContact).filter(
            SiteContact.customer_site_id == site_id,
            func.lower(SiteContact.email) == payload.email.lower(),
        ).first()
        if existing:
            return {"id": existing.id, "full_name": existing.full_name}
```

Add `from sqlalchemy import func` if not imported.

**Step 4: Run test to verify it passes**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_data_cleanup.py::test_create_site_contact_dedup_by_email -v
```

**Step 5: Commit**

```bash
git add app/routers/crm/sites.py tests/test_data_cleanup.py
git commit -m "guard: dedup SiteContact on create by email+site"
```

---

### Task 4: Add Forward Guard — Phone Extraction from site_name

**Files:**
- Modify: `app/schemas/crm.py:132-161` (SiteCreate validator)
- Test: `tests/test_data_cleanup.py`

**Step 1: Write failing test**

```python
def test_site_create_extracts_phone_from_name():
    """SiteCreate validator extracts phone from site_name."""
    from app.schemas.crm import SiteCreate
    site = SiteCreate(site_name="Main Office (415) 555-1234")
    assert site.site_name == "Main Office"
    assert site.contact_phone == "+14155551234"
```

**Step 2: Run test to verify it fails**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_data_cleanup.py::test_site_create_extracts_phone_from_name -v
```

**Step 3: Add model_validator to SiteCreate**

In `app/schemas/crm.py`, add to `SiteCreate` class after `site_name_not_blank`:

```python
    @model_validator(mode="after")
    def extract_phone_from_name(self) -> "SiteCreate":
        import re
        phone_re = re.compile(r'[\(+]?\d[\d\s\-\(\)\.]{8,}\d')
        match = phone_re.search(self.site_name)
        if not match:
            return self
        from app.utils.normalization_helpers import normalize_phone_e164
        raw = match.group(0).strip()
        e164 = normalize_phone_e164(raw)
        if not e164:
            return self
        self.site_name = re.sub(r'\s+', ' ', self.site_name[:match.start()] + self.site_name[match.end():]).strip(' -,')
        if not self.contact_phone:
            self.contact_phone = e164
        elif not self.contact_phone_2:
            self.contact_phone_2 = e164
        return self
```

Add `from pydantic import model_validator` to imports if needed.

**Step 4: Run test to verify it passes**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_data_cleanup.py::test_site_create_extracts_phone_from_name -v
```

**Step 5: Commit**

```bash
git add app/schemas/crm.py tests/test_data_cleanup.py
git commit -m "guard: extract phone from site_name on SiteCreate"
```

---

### Task 5: Add Forward Guard — E.164 Enforcement (Remove Fallback)

**Files:**
- Modify: `app/schemas/crm.py:74-77,118-121,253-256,277-280` (all normalize_phone validators)
- Modify: `tests/test_phone_utils.py` (add enforcement tests)
- Test: `tests/test_data_cleanup.py`

**Step 1: Write failing test**

```python
def test_schema_rejects_unparseable_phone():
    """Phone validators reject gibberish instead of storing raw."""
    from app.schemas.crm import SiteContactCreate
    import pytest
    with pytest.raises(Exception):  # ValidationError
        SiteContactCreate(full_name="Test", phone="not a phone number")
```

**Step 2: Run test to verify it fails**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_data_cleanup.py::test_schema_rejects_unparseable_phone -v
```

Expected: FAIL (currently stores "not a phone number" as-is)

**Step 3: Update all normalize_phone validators**

In `app/schemas/crm.py`, change all four `normalize_phone` methods from:

```python
    return normalize_phone_e164(v) or v
```

to:

```python
    result = normalize_phone_e164(v)
    if result is None:
        raise ValueError(f"Could not parse phone number: {v}")
    return result
```

Locations: lines 77, 121, 256, 280.

**Step 4: Run test to verify it passes**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_data_cleanup.py::test_schema_rejects_unparseable_phone -v
```

**Step 5: Run full test suite to catch breakage**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v --tb=short 2>&1 | tail -30
```

Fix any tests that relied on passing garbage phone strings through validators.

**Step 6: Commit**

```bash
git add app/schemas/crm.py tests/test_data_cleanup.py
git commit -m "guard: reject unparseable phone numbers in schema validators"
```

---

### Task 6: Full Test Suite + Coverage Check

**Files:**
- Test: `tests/test_data_cleanup.py` (add remaining edge case tests)

**Step 1: Add comprehensive tests**

```python
def test_dedup_merge_keeps_richest_record(db):
    """Dedup merge picks record with most non-null fields."""
    # Setup: two contacts same email, one has title, other has phone
    # After merge: winner has both title and phone

def test_phone_normalization_us(db):
    """US phone (415) 555-1234 normalizes to +14155551234."""

def test_phone_normalization_intl(db):
    """International +44 20 7946 0958 normalizes to +442079460958."""

def test_site_name_phone_to_contact_phone_2(db):
    """Phone goes to contact_phone_2 when contact_phone is already filled."""

def test_site_name_no_false_positive():
    """Site name like '12345 Industrial Blvd' is not treated as phone."""
```

**Step 2: Run full suite + coverage**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ --cov=app --cov-report=term-missing --tb=no -q
```

Target: no coverage regression.

**Step 3: Commit**

```bash
git add tests/test_data_cleanup.py
git commit -m "test: comprehensive data cleanup test coverage"
```

---

### Task 7: Deploy and Verify

**Step 1: Push**

```bash
git push
```

**Step 2: Rebuild and deploy**

```bash
docker compose up -d --build
```

**Step 3: Verify migration ran**

```bash
docker compose logs app --tail 20 | grep -i "alembic\|migration\|055"
```

**Step 4: Spot-check data**

```bash
docker compose exec db psql -U availai -c "
  SELECT count(*) as dupes FROM (
    SELECT customer_site_id, lower(email)
    FROM site_contacts WHERE email IS NOT NULL
    GROUP BY customer_site_id, lower(email) HAVING count(*) > 1
  ) t;
"
```

Expected: 0 duplicates remaining.

```bash
docker compose exec db psql -U availai -c "
  SELECT count(*) FROM customer_sites
  WHERE site_name ~ '[\(+]?[0-9][\d\s\-\(\)\.]{8,}[0-9]';
"
```

Expected: 0 phone-in-site_name remaining.

**Step 5: Final commit if any fixes needed**

```bash
git add -A && git commit -m "fix: post-deploy data cleanup adjustments"
```
