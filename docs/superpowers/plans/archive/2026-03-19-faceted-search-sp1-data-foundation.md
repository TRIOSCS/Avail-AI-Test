# Faceted Search SP1: Data Foundation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create the database tables, models, migration, and spec-write service that power faceted materials search.

**Architecture:** Three new tables (`commodity_spec_schemas`, `material_spec_facets`, `material_spec_conflicts`) + one new JSONB column (`specs_structured`) on `material_cards`. A single `spec_write_service.py` handles normalization, validation, conflict resolution, and facet sync. All data flows through `record_spec()`.

**Tech Stack:** SQLAlchemy 2.0, Alembic, PostgreSQL 16, pytest (SQLite in-memory for tests)

**Spec:** `docs/superpowers/specs/2026-03-19-faceted-materials-search-design.md`

---

### Task 1: SQLAlchemy Models

**Files:**
- Create: `app/models/faceted_search.py`
- Modify: `app/models/__init__.py` (add re-exports)
- Modify: `app/models/intelligence.py` (add `specs_structured` column + relationship)

- [ ] **Step 1: Create `app/models/faceted_search.py` with three models**

```python
"""Faceted search data models.

What: CommoditySpecSchema, MaterialSpecFacet, MaterialSpecConflict tables.
Called by: spec_write_service, faceted search queries (SP3).
Depends on: Base from app.models.base, MaterialCard FK.
"""

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship

from app.models.base import Base


class CommoditySpecSchema(Base):
    """Metadata registry — defines what specs each commodity has."""

    __tablename__ = "commodity_spec_schemas"

    id = Column(Integer, primary_key=True)
    commodity = Column(String(100), nullable=False)
    spec_key = Column(String(100), nullable=False)
    display_name = Column(String(100), nullable=False)
    data_type = Column(String(20), nullable=False)  # enum, numeric, boolean
    unit = Column(String(20))  # Display unit: "GB", "pF"
    canonical_unit = Column(String(20))  # Storage unit after normalization
    enum_values = Column(JSONB)  # ["DDR3", "DDR4", "DDR5"] for enum types
    numeric_range = Column(JSONB)  # {"min": 0, "max": 1000000}
    sort_order = Column(Integer, default=0)
    is_filterable = Column(Boolean, default=True, server_default="true")
    is_primary = Column(Boolean, default=False, server_default="false")

    __table_args__ = (
        UniqueConstraint("commodity", "spec_key", name="uq_css_commodity_spec_key"),
    )


class MaterialSpecFacet(Base):
    """Denormalized, typed, indexed projection for fast faceted queries."""

    __tablename__ = "material_spec_facets"

    id = Column(Integer, primary_key=True)
    material_card_id = Column(
        Integer,
        ForeignKey("material_cards.id", ondelete="CASCADE"),
        nullable=False,
    )
    category = Column(String(100), nullable=False)
    spec_key = Column(String(100), nullable=False)
    value_text = Column(String(255))
    value_numeric = Column(Float)
    value_unit = Column(String(20))

    material_card = relationship("MaterialCard", back_populates="spec_facets")

    __table_args__ = (
        UniqueConstraint(
            "material_card_id", "spec_key", name="uq_msf_card_spec"
        ),
        Index("ix_msf_category_key", "category", "spec_key"),
        Index("ix_msf_category_key_text", "category", "spec_key", "value_text"),
        Index(
            "ix_msf_key_numeric",
            "spec_key",
            "value_numeric",
            postgresql_where="value_numeric IS NOT NULL",
        ),
        Index("ix_msf_key_text_card", "spec_key", "value_text", "material_card_id"),
        Index("ix_msf_card", "material_card_id"),
    )


class MaterialSpecConflict(Base):
    """Audit log for when sources disagree on a spec value."""

    __tablename__ = "material_spec_conflicts"

    id = Column(Integer, primary_key=True)
    material_card_id = Column(
        Integer,
        ForeignKey("material_cards.id", ondelete="CASCADE"),
        nullable=False,
    )
    spec_key = Column(String(100), nullable=False)
    existing_value = Column(String(255))
    existing_source = Column(String(50))
    existing_confidence = Column(Float)
    incoming_value = Column(String(255))
    incoming_source = Column(String(50))
    incoming_confidence = Column(Float)
    resolution = Column(String(20), nullable=False)  # kept_existing, overwrote, flagged
    resolved_by = Column(String(50), default="auto")
    created_at = Column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    material_card = relationship("MaterialCard")
```

- [ ] **Step 2: Add `specs_structured` column and relationship to MaterialCard**

In `app/models/intelligence.py`, add to `MaterialCard`:

```python
# After existing enrichment columns (around line with specs_summary):
specs_structured = Column(JSONB)  # Structured specs: {"ddr_type": {"value": "DDR4", "source": "...", "confidence": 0.99, "updated_at": "..."}}

# In relationships section:
spec_facets = relationship(
    "MaterialSpecFacet",
    back_populates="material_card",
    cascade="all, delete-orphan",
)
```

- [ ] **Step 3: Add re-exports to `app/models/__init__.py`**

Add these imports:
```python
from .faceted_search import CommoditySpecSchema, MaterialSpecConflict, MaterialSpecFacet
```

- [ ] **Step 4: Verify models load**

Run: `cd /root/availai && python -c "from app.models import CommoditySpecSchema, MaterialSpecFacet, MaterialSpecConflict; print('OK')"`
Expected: `OK`

- [ ] **Step 5: Commit**

```bash
git add app/models/faceted_search.py app/models/__init__.py app/models/intelligence.py
git commit -m "feat: add faceted search models (CommoditySpecSchema, MaterialSpecFacet, MaterialSpecConflict)"
```

---

### Task 2: Alembic Migration

**Files:**
- Create: `alembic/versions/XXX_faceted_search_tables.py` (autogenerated)

- [ ] **Step 1: Generate migration**

Run inside Docker:
```bash
docker compose exec app alembic revision --autogenerate -m "add faceted search tables and specs_structured column"
```

- [ ] **Step 2: Review the generated migration**

Verify it contains:
1. `create_table("commodity_spec_schemas", ...)` with unique constraint
2. `create_table("material_spec_facets", ...)` with 5 indexes + unique constraint
3. `create_table("material_spec_conflicts", ...)` with FK
4. `add_column("material_cards", Column("specs_structured", JSONB))`

Check the partial index `ix_msf_key_numeric` uses `postgresql_where`. If autogenerate doesn't handle it, add manually:
```python
op.create_index(
    "ix_msf_key_numeric",
    "material_spec_facets",
    ["spec_key", "value_numeric"],
    postgresql_where=sa.text("value_numeric IS NOT NULL"),
)
```

Verify downgrade drops in reverse order (indexes first, then tables, then column).

- [ ] **Step 3: Test migration forward and back**

```bash
docker compose exec app alembic upgrade head
docker compose exec app alembic downgrade -1
docker compose exec app alembic upgrade head
```
Expected: All three succeed without errors.

- [ ] **Step 4: Commit**

```bash
git add alembic/versions/
git commit -m "migration: add faceted search tables and specs_structured column"
```

---

### Task 3: Unit Normalization Module

**Files:**
- Create: `app/services/unit_normalizer.py`
- Create: `tests/test_unit_normalizer.py`

- [ ] **Step 1: Write failing tests**

```python
"""tests/test_unit_normalizer.py -- Tests for unit normalization.

Covers: app/services/unit_normalizer.py
Depends on: conftest.py
"""

from app.services.unit_normalizer import normalize_value


def test_capacitance_uf_to_pf():
    assert normalize_value(100, "uF", "pF") == 100_000_000


def test_capacitance_nf_to_pf():
    assert normalize_value(100, "nF", "pF") == 100_000


def test_capacitance_pf_to_pf():
    assert normalize_value(47, "pF", "pF") == 47


def test_resistance_kohm_to_ohm():
    assert normalize_value(4.7, "kOhm", "ohms") == 4700


def test_resistance_mohm_to_ohm():
    assert normalize_value(1.5, "MOhm", "ohms") == 1_500_000


def test_inductance_uh_to_nh():
    assert normalize_value(10, "uH", "nH") == 10_000


def test_inductance_mh_to_nh():
    assert normalize_value(1, "mH", "nH") == 1_000_000


def test_frequency_ghz_to_mhz():
    assert normalize_value(3.2, "GHz", "MHz") == 3200


def test_same_unit_passthrough():
    assert normalize_value(42, "V", "V") == 42


def test_unknown_conversion_returns_original():
    assert normalize_value(99, "widgets", "gadgets") == 99


def test_string_value_passthrough():
    """Non-numeric values pass through unchanged."""
    assert normalize_value("DDR4", None, None) == "DDR4"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_unit_normalizer.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write implementation**

```python
"""Unit normalization for structured specs.

What: Converts values between measurement units (uF→pF, kOhm→ohms, etc.).
Called by: spec_write_service.record_spec()
Depends on: nothing (pure functions)
"""

from loguru import logger

# Conversion table: (from_unit, to_unit) → multiplier
# All keys are lowercase for case-insensitive matching.
_CONVERSIONS: dict[tuple[str, str], float] = {
    # Capacitance → pF
    ("uf", "pf"): 1_000_000,
    ("nf", "pf"): 1_000,
    ("mf", "pf"): 1_000_000_000,
    # Resistance → ohms
    ("kohm", "ohms"): 1_000,
    ("mohm", "ohms"): 1_000_000,
    # Inductance → nH
    ("uh", "nh"): 1_000,
    ("mh", "nh"): 1_000_000,
    ("h", "nh"): 1_000_000_000,
    # Frequency → MHz
    ("ghz", "mhz"): 1_000,
    ("khz", "mhz"): 0.001,
    ("hz", "mhz"): 0.000001,
    # Power → W
    ("mw", "w"): 0.001,
    ("kw", "w"): 1_000,
    # Current → A
    ("ma", "a"): 0.001,
    ("ua", "a"): 0.000001,
}


def normalize_value(
    value: float | int | str,
    from_unit: str | None,
    canonical_unit: str | None,
) -> float | int | str:
    """Normalize a value to its canonical unit.

    Returns the original value unchanged if:
    - value is a string (enum/text values)
    - units are the same
    - no conversion rule exists
    """
    if isinstance(value, str):
        return value

    if not from_unit or not canonical_unit:
        return value

    from_lower = from_unit.lower()
    canonical_lower = canonical_unit.lower()

    if from_lower == canonical_lower:
        return value

    multiplier = _CONVERSIONS.get((from_lower, canonical_lower))
    if multiplier is None:
        logger.warning(
            "No conversion rule: {} → {}, returning original",
            from_unit,
            canonical_unit,
        )
        return value

    return value * multiplier
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_unit_normalizer.py -v`
Expected: All 11 tests PASS

- [ ] **Step 5: Commit**

```bash
git add app/services/unit_normalizer.py tests/test_unit_normalizer.py
git commit -m "feat: add unit normalizer for spec value conversion"
```

---

### Task 4: Spec Write Service

**Files:**
- Create: `app/services/spec_write_service.py`
- Create: `tests/test_spec_write_service.py`

- [ ] **Step 1: Write failing tests**

```python
"""tests/test_spec_write_service.py -- Tests for spec write service.

Covers: app/services/spec_write_service.py
Depends on: conftest.py (db_session), faceted search models
"""

import asyncio
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import MaterialCard, CommoditySpecSchema, MaterialSpecFacet, MaterialSpecConflict
from app.services.spec_write_service import record_spec
from tests.conftest import engine  # noqa: F401


def _make_card(db: Session, mpn: str = "TEST-001", category: str = "dram") -> MaterialCard:
    card = MaterialCard(
        normalized_mpn=mpn,
        display_mpn=mpn,
        manufacturer="TestCo",
        category=category,
        created_at=datetime.now(timezone.utc),
    )
    db.add(card)
    db.flush()
    return card


def _make_schema(
    db: Session,
    commodity: str = "dram",
    spec_key: str = "ddr_type",
    data_type: str = "enum",
    **kwargs,
) -> CommoditySpecSchema:
    defaults = dict(
        commodity=commodity,
        spec_key=spec_key,
        display_name=spec_key.replace("_", " ").title(),
        data_type=data_type,
        sort_order=0,
        is_filterable=True,
        is_primary=False,
    )
    defaults.update(kwargs)
    schema = CommoditySpecSchema(**defaults)
    db.add(schema)
    db.flush()
    return schema


# --- Basic write ---

def test_record_spec_creates_facet(db_session: Session):
    card = _make_card(db_session)
    _make_schema(db_session, enum_values=["DDR3", "DDR4", "DDR5"])

    record_spec(db_session, card.id, "ddr_type", "DDR4", source="digikey_api", confidence=0.99)

    facet = db_session.query(MaterialSpecFacet).filter_by(
        material_card_id=card.id, spec_key="ddr_type"
    ).first()
    assert facet is not None
    assert facet.value_text == "DDR4"
    assert facet.category == "dram"


def test_record_spec_writes_jsonb(db_session: Session):
    card = _make_card(db_session)
    _make_schema(db_session, enum_values=["DDR3", "DDR4", "DDR5"])

    record_spec(db_session, card.id, "ddr_type", "DDR4", source="digikey_api", confidence=0.99)

    db_session.refresh(card)
    assert card.specs_structured is not None
    assert card.specs_structured["ddr_type"]["value"] == "DDR4"
    assert card.specs_structured["ddr_type"]["source"] == "digikey_api"
    assert card.specs_structured["ddr_type"]["confidence"] == 0.99


# --- Numeric with normalization ---

def test_record_spec_numeric_normalized(db_session: Session):
    card = _make_card(db_session, mpn="CAP-001", category="capacitors")
    _make_schema(
        db_session,
        commodity="capacitors",
        spec_key="capacitance",
        data_type="numeric",
        canonical_unit="pF",
    )

    record_spec(
        db_session, card.id, "capacitance", 100,
        source="digikey_api", confidence=0.95, unit="uF",
    )

    facet = db_session.query(MaterialSpecFacet).filter_by(
        material_card_id=card.id, spec_key="capacitance"
    ).first()
    assert facet is not None
    assert facet.value_numeric == 100_000_000  # 100 uF → pF
    assert facet.value_unit == "pF"


# --- Enum validation ---

def test_record_spec_rejects_invalid_enum(db_session: Session):
    card = _make_card(db_session)
    _make_schema(db_session, enum_values=["DDR3", "DDR4", "DDR5"])

    record_spec(db_session, card.id, "ddr_type", "DDR99", source="ai", confidence=0.5)

    facet = db_session.query(MaterialSpecFacet).filter_by(
        material_card_id=card.id, spec_key="ddr_type"
    ).first()
    assert facet is None  # Rejected — not written


# --- No schema row → skip silently ---

def test_record_spec_no_schema_skips(db_session: Session):
    card = _make_card(db_session)
    # No schema row for "dram" + "unknown_key"

    record_spec(db_session, card.id, "unknown_key", "foo", source="ai", confidence=0.5)

    count = db_session.query(MaterialSpecFacet).filter_by(material_card_id=card.id).count()
    assert count == 0


# --- Conflict: higher priority overwrites ---

def test_conflict_higher_priority_overwrites(db_session: Session):
    card = _make_card(db_session)
    _make_schema(db_session, enum_values=["DDR3", "DDR4", "DDR5"])

    # First write: AI extraction (priority 3)
    record_spec(db_session, card.id, "ddr_type", "DDR3", source="haiku_extraction", confidence=0.85)
    # Second write: DigiKey API (priority 1 — higher)
    record_spec(db_session, card.id, "ddr_type", "DDR4", source="digikey_api", confidence=0.95)

    db_session.refresh(card)
    assert card.specs_structured["ddr_type"]["value"] == "DDR4"
    assert card.specs_structured["ddr_type"]["source"] == "digikey_api"

    conflict = db_session.query(MaterialSpecConflict).filter_by(
        material_card_id=card.id, spec_key="ddr_type"
    ).first()
    assert conflict is not None
    assert conflict.resolution == "overwrote"


# --- Conflict: lower priority keeps existing ---

def test_conflict_lower_priority_keeps_existing(db_session: Session):
    card = _make_card(db_session)
    _make_schema(db_session, enum_values=["DDR3", "DDR4", "DDR5"])

    # First write: DigiKey (priority 1)
    record_spec(db_session, card.id, "ddr_type", "DDR4", source="digikey_api", confidence=0.95)
    # Second write: AI (priority 3 — lower)
    record_spec(db_session, card.id, "ddr_type", "DDR3", source="haiku_extraction", confidence=0.90)

    db_session.refresh(card)
    assert card.specs_structured["ddr_type"]["value"] == "DDR4"  # Kept existing

    conflict = db_session.query(MaterialSpecConflict).filter_by(
        material_card_id=card.id, spec_key="ddr_type"
    ).first()
    assert conflict is not None
    assert conflict.resolution == "kept_existing"


# --- Conflict: high confidence override ---

def test_conflict_high_confidence_overrides_regardless(db_session: Session):
    card = _make_card(db_session)
    _make_schema(db_session, enum_values=["DDR3", "DDR4", "DDR5"])

    # First: DigiKey but low confidence
    record_spec(db_session, card.id, "ddr_type", "DDR3", source="digikey_api", confidence=0.70)
    # Second: AI but very high confidence (>=0.95 vs existing <0.80)
    record_spec(db_session, card.id, "ddr_type", "DDR4", source="haiku_extraction", confidence=0.96)

    db_session.refresh(card)
    assert card.specs_structured["ddr_type"]["value"] == "DDR4"  # Overridden


# --- Conflict: close confidence flags for review ---

def test_conflict_close_confidence_flags(db_session: Session):
    card = _make_card(db_session)
    _make_schema(db_session, enum_values=["DDR3", "DDR4", "DDR5"])

    # Same priority, close confidence
    record_spec(db_session, card.id, "ddr_type", "DDR3", source="digikey_api", confidence=0.90)
    record_spec(db_session, card.id, "ddr_type", "DDR4", source="nexar_api", confidence=0.88)

    conflict = db_session.query(MaterialSpecConflict).filter_by(
        material_card_id=card.id, spec_key="ddr_type"
    ).first()
    assert conflict is not None
    assert conflict.resolution == "flagged"


# --- Upsert: same source updates in place ---

def test_same_source_updates_in_place(db_session: Session):
    card = _make_card(db_session)
    _make_schema(db_session, enum_values=["DDR3", "DDR4", "DDR5"])

    record_spec(db_session, card.id, "ddr_type", "DDR3", source="digikey_api", confidence=0.90)
    record_spec(db_session, card.id, "ddr_type", "DDR4", source="digikey_api", confidence=0.95)

    db_session.refresh(card)
    assert card.specs_structured["ddr_type"]["value"] == "DDR4"

    # No conflict logged for same-source updates
    conflict_count = db_session.query(MaterialSpecConflict).filter_by(
        material_card_id=card.id, spec_key="ddr_type"
    ).count()
    assert conflict_count == 0


# --- Boolean type ---

def test_record_spec_boolean(db_session: Session):
    card = _make_card(db_session)
    _make_schema(db_session, spec_key="ecc", data_type="boolean")

    record_spec(db_session, card.id, "ecc", True, source="digikey_api", confidence=0.99)

    facet = db_session.query(MaterialSpecFacet).filter_by(
        material_card_id=card.id, spec_key="ecc"
    ).first()
    assert facet is not None
    assert facet.value_text == "true"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_spec_write_service.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write implementation**

```python
"""Spec write service — single entry point for recording structured specs.

What: Normalizes, validates, conflict-resolves, and writes spec data to
      both the JSONB column (source of truth) and the facet table (indexed projection).
Called by: Data population jobs (SP2), vendor API enrichment, AI extraction.
Depends on: CommoditySpecSchema, MaterialSpecFacet, MaterialSpecConflict,
            MaterialCard, unit_normalizer.
"""

from datetime import datetime, timezone

from loguru import logger
from sqlalchemy.orm import Session

from app.models import MaterialCard, CommoditySpecSchema, MaterialSpecFacet, MaterialSpecConflict
from app.services.unit_normalizer import normalize_value

# Source priority: lower number = higher priority
_SOURCE_PRIORITY: dict[str, int] = {
    "digikey_api": 1,
    "nexar_api": 1,
    "mouser_api": 1,
    "newegg_scrape": 2,
    "octopart_scrape": 2,
    "haiku_extraction": 3,
    "vendor_freetext": 4,
}

_DEFAULT_PRIORITY = 5


def _get_priority(source: str) -> int:
    return _SOURCE_PRIORITY.get(source, _DEFAULT_PRIORITY)


def record_spec(
    db: Session,
    card_id: int,
    spec_key: str,
    value: str | int | float | bool,
    *,
    source: str,
    confidence: float,
    unit: str | None = None,
) -> None:
    """Record a structured spec value for a material card.

    Handles normalization, validation, conflict resolution, and facet sync.
    Does not commit — caller manages the transaction.
    """
    card = db.get(MaterialCard, card_id)
    if card is None:
        logger.warning("record_spec: card_id={} not found, skipping", card_id)
        return

    category = (card.category or "").lower().strip()
    if not category:
        logger.debug("record_spec: card {} has no category, skipping", card_id)
        return

    # Look up schema
    schema = (
        db.query(CommoditySpecSchema)
        .filter_by(commodity=category, spec_key=spec_key)
        .first()
    )
    if schema is None:
        logger.debug(
            "record_spec: no schema for commodity={} spec_key={}, skipping",
            category,
            spec_key,
        )
        return

    # Validate enum
    if schema.data_type == "enum" and schema.enum_values:
        str_value = str(value)
        if str_value not in schema.enum_values:
            logger.debug(
                "record_spec: {} not in enum_values for {}.{}, skipping",
                str_value,
                category,
                spec_key,
            )
            return

    # Normalize unit for numeric types
    canonical_value = value
    canonical_unit = schema.canonical_unit
    if schema.data_type == "numeric" and unit and canonical_unit:
        canonical_value = normalize_value(value, unit, canonical_unit)

    # Build the spec entry
    now_iso = datetime.now(timezone.utc).isoformat()
    new_entry = {
        "value": canonical_value if schema.data_type == "numeric" else value,
        "source": source,
        "confidence": confidence,
        "updated_at": now_iso,
    }

    # For display/JSONB, store original value + unit for readability
    if schema.data_type == "numeric" and unit:
        new_entry["original_value"] = value
        new_entry["original_unit"] = unit

    # Conflict resolution
    specs = dict(card.specs_structured or {})
    existing = specs.get(spec_key)

    if existing and existing.get("source") != source:
        # Different source — check priority
        existing_priority = _get_priority(existing["source"])
        incoming_priority = _get_priority(source)
        existing_conf = existing.get("confidence", 0)
        incoming_conf = confidence

        # Determine resolution
        if incoming_conf >= 0.95 and existing_conf < 0.80:
            resolution = "overwrote"  # High confidence override
        elif abs(existing_conf - incoming_conf) <= 0.1 and existing_priority == incoming_priority:
            resolution = "flagged"  # Close confidence, same priority
        elif incoming_priority < existing_priority:
            resolution = "overwrote"  # Higher priority source
        elif incoming_priority == existing_priority and incoming_conf > existing_conf:
            resolution = "overwrote"  # Equal priority, higher confidence
        else:
            resolution = "kept_existing"

        # Log the conflict
        conflict = MaterialSpecConflict(
            material_card_id=card_id,
            spec_key=spec_key,
            existing_value=str(existing.get("value", "")),
            existing_source=existing.get("source", ""),
            existing_confidence=existing_conf,
            incoming_value=str(value),
            incoming_source=source,
            incoming_confidence=confidence,
            resolution=resolution,
            resolved_by="auto",
        )
        db.add(conflict)

        if resolution == "kept_existing" or resolution == "flagged":
            db.flush()
            return

    # Write to JSONB (source of truth)
    if schema.data_type == "boolean":
        new_entry["value"] = bool(value)
    specs[spec_key] = new_entry
    card.specs_structured = specs

    # Upsert facet row
    facet = (
        db.query(MaterialSpecFacet)
        .filter_by(material_card_id=card_id, spec_key=spec_key)
        .first()
    )
    if facet is None:
        facet = MaterialSpecFacet(
            material_card_id=card_id,
            category=category,
            spec_key=spec_key,
        )
        db.add(facet)

    if schema.data_type == "numeric":
        facet.value_numeric = canonical_value
        facet.value_text = None
        facet.value_unit = canonical_unit
    elif schema.data_type == "boolean":
        facet.value_text = "true" if value else "false"
        facet.value_numeric = None
        facet.value_unit = None
    else:  # enum / string
        facet.value_text = str(value)
        facet.value_numeric = None
        facet.value_unit = None

    db.flush()
    logger.debug(
        "record_spec: card={} {}.{}={} (source={}, conf={})",
        card_id,
        category,
        spec_key,
        value,
        source,
        confidence,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_spec_write_service.py -v`
Expected: All 11 tests PASS

- [ ] **Step 5: Commit**

```bash
git add app/services/spec_write_service.py tests/test_spec_write_service.py
git commit -m "feat: add spec write service with normalization, validation, and conflict resolution"
```

---

### Task 5: Seed Commodity Spec Schemas

**Files:**
- Create: `app/services/seed_commodity_schemas.py`
- Create: `tests/test_seed_commodity_schemas.py`

- [ ] **Step 1: Write failing test**

```python
"""tests/test_seed_commodity_schemas.py -- Tests for commodity schema seeding.

Covers: app/services/seed_commodity_schemas.py
Depends on: conftest.py (db_session), CommoditySpecSchema model
"""

from sqlalchemy.orm import Session

from app.models import CommoditySpecSchema
from app.services.seed_commodity_schemas import seed_schemas, COMMODITY_SCHEMAS
from tests.conftest import engine  # noqa: F401


def test_seed_creates_rows(db_session: Session):
    seed_schemas(db_session)
    count = db_session.query(CommoditySpecSchema).count()
    expected = sum(len(specs) for specs in COMMODITY_SCHEMAS.values())
    assert count == expected
    assert count > 50  # Sanity: 15 commodities × ~4 specs each


def test_seed_idempotent(db_session: Session):
    seed_schemas(db_session)
    count1 = db_session.query(CommoditySpecSchema).count()
    seed_schemas(db_session)  # Run again
    count2 = db_session.query(CommoditySpecSchema).count()
    assert count1 == count2


def test_dram_has_expected_specs(db_session: Session):
    seed_schemas(db_session)
    dram_specs = (
        db_session.query(CommoditySpecSchema)
        .filter_by(commodity="dram")
        .order_by(CommoditySpecSchema.sort_order)
        .all()
    )
    keys = [s.spec_key for s in dram_specs]
    assert "ddr_type" in keys
    assert "capacity_gb" in keys
    assert "ecc" in keys
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_seed_commodity_schemas.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write implementation**

```python
"""Seed commodity spec schemas for top 15 commodities.

What: Populates commodity_spec_schemas with spec definitions per commodity.
Called by: startup.py seed routine, or manually via CLI.
Depends on: CommoditySpecSchema model.
"""

from loguru import logger
from sqlalchemy.orm import Session

from app.models import CommoditySpecSchema


# Each commodity maps to a list of spec definitions.
# data_type: "enum", "numeric", "boolean"
COMMODITY_SCHEMAS: dict[str, list[dict]] = {
    "dram": [
        {"spec_key": "ddr_type", "display_name": "DDR Type", "data_type": "enum", "enum_values": ["DDR3", "DDR4", "DDR5", "DDR5X", "LPDDR4", "LPDDR5"], "is_primary": True},
        {"spec_key": "capacity_gb", "display_name": "Capacity (GB)", "data_type": "numeric", "unit": "GB", "canonical_unit": "GB", "is_primary": True},
        {"spec_key": "speed_mhz", "display_name": "Speed", "data_type": "enum", "enum_values": ["2133", "2400", "2666", "3200", "3600", "4800", "5200", "5600", "6000", "6400", "7200", "8000"]},
        {"spec_key": "ecc", "display_name": "ECC", "data_type": "boolean"},
        {"spec_key": "form_factor", "display_name": "Form Factor", "data_type": "enum", "enum_values": ["DIMM", "SO-DIMM", "UDIMM", "RDIMM", "LRDIMM"]},
    ],
    "capacitors": [
        {"spec_key": "capacitance", "display_name": "Capacitance", "data_type": "numeric", "unit": "pF", "canonical_unit": "pF", "is_primary": True},
        {"spec_key": "voltage_rating", "display_name": "Voltage Rating", "data_type": "numeric", "unit": "V", "canonical_unit": "V", "is_primary": True},
        {"spec_key": "dielectric", "display_name": "Dielectric", "data_type": "enum", "enum_values": ["X7R", "X5R", "C0G", "Y5V", "NP0"]},
        {"spec_key": "tolerance", "display_name": "Tolerance", "data_type": "enum", "enum_values": ["±1%", "±5%", "±10%", "±20%"]},
        {"spec_key": "package", "display_name": "Package", "data_type": "enum", "enum_values": ["0402", "0603", "0805", "1206", "1210", "through-hole"]},
    ],
    "resistors": [
        {"spec_key": "resistance", "display_name": "Resistance", "data_type": "numeric", "unit": "ohms", "canonical_unit": "ohms", "is_primary": True},
        {"spec_key": "power_rating", "display_name": "Power Rating", "data_type": "numeric", "unit": "W", "canonical_unit": "W"},
        {"spec_key": "tolerance", "display_name": "Tolerance", "data_type": "enum", "enum_values": ["0.1%", "1%", "5%"]},
        {"spec_key": "package", "display_name": "Package", "data_type": "enum", "enum_values": ["0402", "0603", "0805", "1206", "through-hole"]},
    ],
    "hdd": [
        {"spec_key": "capacity_gb", "display_name": "Capacity (GB)", "data_type": "numeric", "unit": "GB", "canonical_unit": "GB", "is_primary": True},
        {"spec_key": "rpm", "display_name": "RPM", "data_type": "enum", "enum_values": ["5400", "7200", "10000", "15000"]},
        {"spec_key": "form_factor", "display_name": "Form Factor", "data_type": "enum", "enum_values": ["2.5\"", "3.5\""]},
        {"spec_key": "interface", "display_name": "Interface", "data_type": "enum", "enum_values": ["SATA", "SAS", "NVMe"]},
    ],
    "ssd": [
        {"spec_key": "capacity_gb", "display_name": "Capacity (GB)", "data_type": "numeric", "unit": "GB", "canonical_unit": "GB", "is_primary": True},
        {"spec_key": "form_factor", "display_name": "Form Factor", "data_type": "enum", "enum_values": ["2.5\"", "M.2", "U.2", "mSATA"]},
        {"spec_key": "interface", "display_name": "Interface", "data_type": "enum", "enum_values": ["SATA", "NVMe", "SAS"]},
        {"spec_key": "read_speed_mbps", "display_name": "Read Speed (MB/s)", "data_type": "numeric", "unit": "MB/s", "canonical_unit": "MB/s"},
    ],
    "connectors": [
        {"spec_key": "pin_count", "display_name": "Pin Count", "data_type": "numeric", "unit": "pins", "canonical_unit": "pins", "is_primary": True},
        {"spec_key": "pitch_mm", "display_name": "Pitch (mm)", "data_type": "numeric", "unit": "mm", "canonical_unit": "mm"},
        {"spec_key": "mounting", "display_name": "Mounting", "data_type": "enum", "enum_values": ["through-hole", "SMD", "press-fit"]},
        {"spec_key": "gender", "display_name": "Gender", "data_type": "enum", "enum_values": ["male", "female", "genderless"]},
    ],
    "motherboards": [
        {"spec_key": "socket", "display_name": "Socket", "data_type": "enum", "enum_values": ["LGA1700", "AM5", "LGA4677", "LGA1151", "LGA2066", "SP3"], "is_primary": True},
        {"spec_key": "form_factor", "display_name": "Form Factor", "data_type": "enum", "enum_values": ["ATX", "mATX", "EATX", "Mini-ITX"], "is_primary": True},
        {"spec_key": "chipset", "display_name": "Chipset", "data_type": "enum", "enum_values": []},
        {"spec_key": "ram_slots", "display_name": "RAM Slots", "data_type": "numeric", "unit": "slots", "canonical_unit": "slots"},
    ],
    "microprocessors": [
        {"spec_key": "socket", "display_name": "Socket", "data_type": "enum", "enum_values": ["LGA1700", "AM5", "LGA4677", "LGA1151", "SP3"], "is_primary": True},
        {"spec_key": "core_count", "display_name": "Core Count", "data_type": "numeric", "unit": "cores", "canonical_unit": "cores", "is_primary": True},
        {"spec_key": "clock_speed_ghz", "display_name": "Clock Speed (GHz)", "data_type": "numeric", "unit": "GHz", "canonical_unit": "MHz"},
        {"spec_key": "tdp_watts", "display_name": "TDP (W)", "data_type": "numeric", "unit": "W", "canonical_unit": "W"},
    ],
    "power_supplies": [
        {"spec_key": "wattage", "display_name": "Wattage", "data_type": "numeric", "unit": "W", "canonical_unit": "W", "is_primary": True},
        {"spec_key": "form_factor", "display_name": "Form Factor", "data_type": "enum", "enum_values": ["ATX", "SFX", "1U server", "2U server", "redundant"]},
        {"spec_key": "efficiency", "display_name": "Efficiency", "data_type": "enum", "enum_values": ["80+ Bronze", "80+ Silver", "80+ Gold", "80+ Platinum", "80+ Titanium"]},
    ],
    "gpu": [
        {"spec_key": "memory_gb", "display_name": "Memory (GB)", "data_type": "numeric", "unit": "GB", "canonical_unit": "GB", "is_primary": True},
        {"spec_key": "memory_type", "display_name": "Memory Type", "data_type": "enum", "enum_values": ["GDDR5", "GDDR6", "GDDR6X", "HBM2", "HBM3"], "is_primary": True},
        {"spec_key": "interface", "display_name": "Interface", "data_type": "enum", "enum_values": ["PCIe 3.0", "PCIe 4.0", "PCIe 5.0"]},
    ],
    "inductors": [
        {"spec_key": "inductance", "display_name": "Inductance", "data_type": "numeric", "unit": "nH", "canonical_unit": "nH", "is_primary": True},
        {"spec_key": "current_rating", "display_name": "Current Rating", "data_type": "numeric", "unit": "A", "canonical_unit": "A"},
        {"spec_key": "package", "display_name": "Package", "data_type": "enum", "enum_values": []},
    ],
    "diodes": [
        {"spec_key": "type", "display_name": "Type", "data_type": "enum", "enum_values": ["rectifier", "zener", "Schottky", "TVS"], "is_primary": True},
        {"spec_key": "voltage", "display_name": "Voltage", "data_type": "numeric", "unit": "V", "canonical_unit": "V"},
        {"spec_key": "current", "display_name": "Current", "data_type": "numeric", "unit": "A", "canonical_unit": "A"},
        {"spec_key": "package", "display_name": "Package", "data_type": "enum", "enum_values": []},
    ],
    "mosfets": [
        {"spec_key": "type", "display_name": "Type", "data_type": "enum", "enum_values": ["N-channel", "P-channel"], "is_primary": True},
        {"spec_key": "vds", "display_name": "Vds", "data_type": "numeric", "unit": "V", "canonical_unit": "V"},
        {"spec_key": "rds_on", "display_name": "Rds(on)", "data_type": "numeric", "unit": "mOhm", "canonical_unit": "mOhm"},
        {"spec_key": "id_max", "display_name": "Id Max", "data_type": "numeric", "unit": "A", "canonical_unit": "A"},
        {"spec_key": "package", "display_name": "Package", "data_type": "enum", "enum_values": []},
    ],
    "microcontrollers": [
        {"spec_key": "core", "display_name": "Core", "data_type": "enum", "enum_values": ["ARM Cortex-M0", "ARM Cortex-M3", "ARM Cortex-M4", "ARM Cortex-M7", "RISC-V", "AVR", "PIC"], "is_primary": True},
        {"spec_key": "flash_kb", "display_name": "Flash (KB)", "data_type": "numeric", "unit": "KB", "canonical_unit": "KB"},
        {"spec_key": "ram_kb", "display_name": "RAM (KB)", "data_type": "numeric", "unit": "KB", "canonical_unit": "KB"},
        {"spec_key": "clock_mhz", "display_name": "Clock (MHz)", "data_type": "numeric", "unit": "MHz", "canonical_unit": "MHz"},
        {"spec_key": "package", "display_name": "Package", "data_type": "enum", "enum_values": []},
    ],
    "network_cards": [
        {"spec_key": "speed", "display_name": "Speed", "data_type": "enum", "enum_values": ["1GbE", "10GbE", "25GbE", "40GbE", "100GbE"], "is_primary": True},
        {"spec_key": "ports", "display_name": "Ports", "data_type": "numeric", "unit": "ports", "canonical_unit": "ports"},
        {"spec_key": "interface", "display_name": "Interface", "data_type": "enum", "enum_values": ["PCIe", "OCP", "LOM"]},
        {"spec_key": "controller", "display_name": "Controller", "data_type": "enum", "enum_values": ["Intel", "Broadcom", "Mellanox"]},
    ],
}

# Parent groups for UI display (not stored in DB)
COMMODITY_GROUPS: dict[str, list[str]] = {
    "Passives": ["capacitors", "resistors", "inductors"],
    "Semiconductors — Discrete": ["diodes", "mosfets"],
    "Processors & Programmable": ["microcontrollers", "microprocessors"],
    "Memory & Storage": ["dram", "ssd", "hdd"],
    "Connectors & Electromechanical": ["connectors"],
    "Power & Energy": ["power_supplies"],
    "Optoelectronics & Display": ["gpu"],
    "IT / Server Hardware": ["motherboards", "network_cards"],
}


def seed_schemas(db: Session) -> int:
    """Seed commodity_spec_schemas table. Idempotent — skips existing rows.

    Returns count of rows inserted.
    """
    inserted = 0
    for commodity, specs in COMMODITY_SCHEMAS.items():
        for i, spec in enumerate(specs):
            exists = (
                db.query(CommoditySpecSchema)
                .filter_by(commodity=commodity, spec_key=spec["spec_key"])
                .first()
            )
            if exists:
                continue

            row = CommoditySpecSchema(
                commodity=commodity,
                spec_key=spec["spec_key"],
                display_name=spec["display_name"],
                data_type=spec["data_type"],
                unit=spec.get("unit"),
                canonical_unit=spec.get("canonical_unit"),
                enum_values=spec.get("enum_values"),
                sort_order=i,
                is_filterable=True,
                is_primary=spec.get("is_primary", False),
            )
            db.add(row)
            inserted += 1

    db.flush()
    logger.info("seed_commodity_schemas: inserted {} rows", inserted)
    return inserted
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_seed_commodity_schemas.py -v`
Expected: All 3 tests PASS

- [ ] **Step 5: Wire seed into startup.py**

Add to `app/startup.py` in the existing seed section:

```python
from app.services.seed_commodity_schemas import seed_schemas

# Inside the startup function, after existing seed calls:
seed_schemas(db)
```

- [ ] **Step 6: Commit**

```bash
git add app/services/seed_commodity_schemas.py tests/test_seed_commodity_schemas.py app/startup.py
git commit -m "feat: add commodity spec schema seed data for 15 commodities"
```

---

### Task 6: Add Faceted Search Tables to Test conftest.py

**Files:**
- Modify: `tests/conftest.py`

Note: The new tables use `JSONB` (already mapped to `JSON` for SQLite in conftest) and standard column types. The partial index `ix_msf_key_numeric` uses `postgresql_where` which SQLite doesn't support — it needs to be excluded from SQLite table creation.

- [ ] **Step 1: Verify new tables are auto-created by conftest**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_spec_write_service.py::test_record_spec_creates_facet -v`

If it fails due to the partial index, add `commodity_spec_schemas`, `material_spec_facets`, and `material_spec_conflicts` to conftest's table creation logic, handling the partial index exclusion.

- [ ] **Step 2: If needed, update conftest to handle partial index**

The `postgresql_where` clause on `ix_msf_key_numeric` will fail on SQLite. If this is the case, add the index exclusion in conftest's table creation. Check how other PostgreSQL-specific features are handled (TSVECTOR → TEXT mapping already exists).

- [ ] **Step 3: Run full test suite**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v`
Expected: All tests PASS (existing + new)

- [ ] **Step 4: Commit (if conftest changes needed)**

```bash
git add tests/conftest.py
git commit -m "fix: handle PostgreSQL partial index in SQLite test conftest"
```

---

### Task 7: Full Suite Verification

- [ ] **Step 1: Run full test suite**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v
```
Expected: All tests PASS

- [ ] **Step 2: Test migration in Docker**

```bash
docker compose exec app alembic upgrade head
```
Expected: Migration applies cleanly

- [ ] **Step 3: Verify seed data in Docker**

```bash
docker compose exec app python -c "
from app.database import SessionLocal
from app.models import CommoditySpecSchema
db = SessionLocal()
count = db.query(CommoditySpecSchema).count()
print(f'Seeded {count} commodity spec schemas')
db.close()
"
```
Expected: `Seeded XX commodity spec schemas` (60+)

- [ ] **Step 4: Final commit and push**

```bash
git push origin main
```
