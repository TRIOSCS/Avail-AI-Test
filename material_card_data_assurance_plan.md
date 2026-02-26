# Material Card Data Assurance & Redundancy Plan

**Priority**: CRITICAL — incorrect linkage directly causes lost deals
**Scope**: `material_cards`, `material_vendor_history`, and all FK linkages from `requirements`, `sightings`, `offers`
**Date**: 2026-02-25

---

## Executive Summary

The material card system is the central hub matching **customer demand** (requirements) to **vendor supply** (sightings/offers). A broken or incorrect linkage means:

- A vendor with stock doesn't appear in search results → **deal lost**
- Two variants treated as same part → **wrong part quoted** → liability
- Historical vendor data orphaned → **pricing intelligence degraded**
- Duplicate cards → **fragmented data** → incomplete picture for buyers

This plan addresses **7 identified vulnerabilities** across 4 layers of defense.

---

## Identified Vulnerabilities

### V1: Race Condition in `resolve_material_card()` (MEDIUM)
**File**: `search_service.py:714-728`
**Issue**: Two concurrent searches for the same MPN can both read "no card exists" and both attempt INSERT. PostgreSQL UNIQUE constraint catches the second, but the entire `_upsert_material_card()` call fails silently (line 183-185: caught, logged, rollback). The sightings from that search are never linked to the material card.
**Impact**: Sightings saved without `material_card_id` → invisible in material-card-based queries.

### V2: Vendor Name Case Sensitivity in History (MEDIUM)
**File**: `search_service.py:765`
**Issue**: `MaterialVendorHistory` keys vendor records by raw `s.vendor_name` (exact case). "ARROW" ≠ "Arrow" ≠ "arrow" creates 3 separate history records for the same vendor.
**Impact**: Inflated vendor counts, fragmented pricing history, confusion in vendor analysis.

### V3: No Orphan Detection / Self-Healing (HIGH)
**Issue**: If `material_card_id` goes NULL on a requirement/sighting/offer (card deleted, linkage failed, race condition), there is no mechanism to detect or repair it. These records become invisible to material-card-based queries.
**Impact**: Vendor supply data effectively lost until someone manually notices.

### V4: No Data Integrity Audit (HIGH)
**Issue**: No periodic check that records with the same normalized MPN all point to the same material card. If normalization logic changes or a bug introduces a mismatch, it could go undetected indefinitely.
**Impact**: Silent data corruption — the worst kind.

### V5: SET NULL on Delete Cascade (LOW-MEDIUM)
**File**: `sourcing.py:74,116`, `offers.py:33`
**Issue**: `ondelete="SET NULL"` means deleting a material card silently unlinks all associated records. While this prevents cascading deletes, it creates orphans with no audit trail.
**Impact**: Accidental card deletion silently degrades data completeness.

### V6: No Monitoring or Alerting (HIGH)
**Issue**: Material card upsert failures are logged (`log.error`) but there's no alerting, no metric tracking, no dashboard visibility. A systematic failure (e.g., DB constraint issue) could persist for hours.
**Impact**: Problems discovered only when a user notices something missing.

### V7: No Backup Linkage Data (MEDIUM)
**Issue**: The `material_card_id` FK is the only link between records and their material card. If this field is corrupted or lost, rebuilding requires re-normalizing every MPN and re-matching — which works (the backfill script proved this) but takes time and risks the live system.
**Impact**: Recovery is possible but slow and stressful during an incident.

---

## Defense Layer 1: Harden the Write Path

### 1A: Add Retry-on-Conflict to `resolve_material_card()` ★ CRITICAL

Replace the current non-defensive find-or-create with a PostgreSQL-native upsert pattern that handles race conditions atomically.

**Implementation** (`search_service.py`):

```python
from sqlalchemy.dialects.postgresql import insert as pg_insert

def resolve_material_card(mpn: str, db: Session) -> MaterialCard | None:
    norm = normalize_mpn_key(mpn)
    if not norm:
        return None

    # Attempt find first (fast path, no write)
    card = db.query(MaterialCard).filter_by(normalized_mpn=norm).first()
    if card:
        return card

    # Atomic upsert: INSERT ... ON CONFLICT DO NOTHING + re-SELECT
    display = normalize_mpn(mpn) or mpn.strip()
    stmt = pg_insert(MaterialCard).values(
        normalized_mpn=norm,
        display_mpn=display,
        search_count=0,
    ).on_conflict_do_nothing(index_elements=["normalized_mpn"])
    db.execute(stmt)
    db.flush()

    # Re-fetch (guaranteed to exist now)
    card = db.query(MaterialCard).filter_by(normalized_mpn=norm).first()
    return card
```

**Why**: Eliminates V1 entirely. The `ON CONFLICT DO NOTHING` + re-SELECT pattern is the PostgreSQL-standard way to handle concurrent find-or-create. No exceptions, no silent failures.

**Test compatibility**: For SQLite tests, detect dialect and fall back to try/except on IntegrityError.

### 1B: Normalize Vendor Names in History Keying ★ IMPORTANT

**Implementation** (`search_service.py:_upsert_material_card`):

```python
from app.vendor_utils import normalize_vendor_name

# When building the existing_vh lookup:
existing_vh = {
    normalize_vendor_name(vh.vendor_name): vh
    for vh in db.query(MaterialVendorHistory)
    .filter_by(material_card_id=card.id)
    .all()
}

# When looking up in the loop:
for s in pn_sightings:
    if not s.vendor_name:
        continue
    vn_key = normalize_vendor_name(s.vendor_name)
    vh = existing_vh.get(vn_key)
    ...
```

**Also requires**: A one-time migration to merge existing duplicate vendor history records (same card + same normalized vendor name → keep the one with highest `times_seen`, merge counts).

### 1C: Store `normalized_mpn` Redundantly on Linked Records ★ IMPORTANT

Add a computed/stored `normalized_mpn` column to `requirements`, `sightings`, and `offers`. This serves as a backup linkage key that can be used for integrity checks and re-linking.

**Migration**:
```python
# Already exists on requirements (sourcing.py:75)
# Need to add to sightings and offers, populated from mpn_matched
op.add_column("sightings", sa.Column("normalized_mpn", sa.String(255), index=True))
op.add_column("offers", sa.Column("normalized_mpn", sa.String(255), index=True))
```

**Backfill**: Compute from `mpn_matched` on sightings, `part_number` on offers.

**Why**: If `material_card_id` is ever lost, re-linking is a trivial JOIN on `normalized_mpn` rather than re-parsing raw data.

---

## Defense Layer 2: Continuous Integrity Monitoring

### 2A: Scheduled Integrity Check Job ★ CRITICAL

A background task that runs every **6 hours** (configurable) and checks:

**Check 1 — Orphaned Records** (V3):
```sql
-- Requirements with MPN but no material card
SELECT COUNT(*) FROM requirements
WHERE primary_mpn IS NOT NULL
  AND material_card_id IS NULL;

-- Sightings with MPN but no material card
SELECT COUNT(*) FROM sightings
WHERE mpn_matched IS NOT NULL
  AND material_card_id IS NULL;

-- Offers with part number but no material card
SELECT COUNT(*) FROM offers
WHERE part_number IS NOT NULL
  AND material_card_id IS NULL;
```

**Check 2 — Cross-Record Consistency** (V4):
```sql
-- Records with same normalized MPN pointing to different cards
SELECT normalized_mpn, COUNT(DISTINCT material_card_id) as card_count
FROM (
    SELECT normalize_mpn_key(primary_mpn) as normalized_mpn, material_card_id
    FROM requirements WHERE material_card_id IS NOT NULL
    UNION ALL
    SELECT normalize_mpn_key(mpn_matched), material_card_id
    FROM sightings WHERE material_card_id IS NOT NULL
) sub
GROUP BY normalized_mpn
HAVING COUNT(DISTINCT material_card_id) > 1;
```

**Check 3 — Duplicate Material Cards**:
```sql
-- Cards with same normalized_mpn (should be impossible with UNIQUE, but defense in depth)
SELECT normalized_mpn, COUNT(*)
FROM material_cards
GROUP BY normalized_mpn
HAVING COUNT(*) > 1;
```

**Check 4 — Dangling FKs**:
```sql
-- Records pointing to non-existent material cards
SELECT COUNT(*) FROM requirements r
LEFT JOIN material_cards mc ON r.material_card_id = mc.id
WHERE r.material_card_id IS NOT NULL AND mc.id IS NULL;
```

**Implementation location**: `app/services/integrity_service.py` (new file)
**Scheduler integration**: Add to `app/scheduler.py` alongside existing periodic tasks

### 2B: Self-Healing Re-Linker ★ CRITICAL

When Check 1 (orphaned records) finds unlinked records, automatically re-link them:

```python
async def heal_orphaned_records(db: Session) -> dict:
    """Re-link records that have an MPN but no material_card_id."""
    healed = {"requirements": 0, "sightings": 0, "offers": 0}

    # Requirements
    orphans = db.query(Requirement).filter(
        Requirement.primary_mpn.isnot(None),
        Requirement.material_card_id.is_(None),
    ).all()
    for r in orphans:
        card = resolve_material_card(r.primary_mpn, db)
        if card:
            r.material_card_id = card.id
            healed["requirements"] += 1

    # Sightings
    orphans = db.query(Sighting).filter(
        Sighting.mpn_matched.isnot(None),
        Sighting.material_card_id.is_(None),
    ).all()
    for s in orphans:
        card = resolve_material_card(s.mpn_matched, db)
        if card:
            s.material_card_id = card.id
            healed["sightings"] += 1

    # Offers (similar pattern)
    ...

    db.commit()
    return healed
```

**Trigger**: Runs after integrity check if orphans > 0. Also runs at app startup (after existing backfills).

### 2C: Integrity Health Endpoint ★ IMPORTANT

```
GET /api/admin/integrity
```

Returns:
```json
{
    "status": "healthy",  // or "degraded" or "critical"
    "last_check": "2026-02-25T14:00:00Z",
    "checks": {
        "orphaned_requirements": 0,
        "orphaned_sightings": 0,
        "orphaned_offers": 0,
        "cross_record_mismatches": 0,
        "duplicate_cards": 0,
        "dangling_fks": 0,
        "vendor_history_duplicates": 0
    },
    "last_heal": {
        "timestamp": "2026-02-25T14:00:05Z",
        "requirements_healed": 0,
        "sightings_healed": 0,
        "offers_healed": 0
    },
    "material_cards_total": 756000,
    "linkage_coverage": {
        "requirements": "100.0%",
        "sightings": "100.0%",
        "offers": "100.0%"
    }
}
```

**Severity levels**:
- `healthy`: All checks pass, 100% linkage
- `degraded`: <50 orphaned records OR vendor history duplicates detected → auto-heal running
- `critical`: >50 orphaned records OR cross-record mismatches OR dangling FKs → alert immediately

---

## Defense Layer 3: Audit Trail & Observability

### 3A: Material Card Audit Log ★ IMPORTANT

New table `material_card_audit`:

```python
class MaterialCardAudit(Base):
    __tablename__ = "material_card_audit"

    id = Column(Integer, primary_key=True)
    material_card_id = Column(Integer, index=True)  # No FK — survives card deletion
    action = Column(String(50), nullable=False)      # created, linked, unlinked, deleted, merged
    entity_type = Column(String(50))                 # requirement, sighting, offer
    entity_id = Column(Integer)
    old_card_id = Column(Integer)                    # For re-links / merges
    new_card_id = Column(Integer)
    normalized_mpn = Column(String(255), index=True)
    details = Column(JSON)                           # Additional context
    created_at = Column(DateTime, default=utcnow)
    created_by = Column(String(255))                 # system, user email, scheduler
```

**Logged events**:
- Card created (by resolve_material_card)
- Record linked to card (requirement/sighting/offer gets material_card_id set)
- Record unlinked (material_card_id set to NULL)
- Card deleted (before SET NULL cascade fires)
- Orphan healed (by self-healer)
- Cards merged (manual dedup operation)

**Why**: When something goes wrong, this tells you exactly when and how the data changed. Without this, debugging a linkage issue requires correlating app logs, DB state, and guesswork.

### 3B: Structured Metrics ★ IMPORTANT

Add counters (logged to structured log, queryable):

| Metric | Type | Description |
|--------|------|-------------|
| `material_card.created` | Counter | New cards created |
| `material_card.resolved` | Counter | Existing cards found |
| `material_card.upsert_failed` | Counter | Upsert exceptions (currently silent) |
| `material_card.race_condition` | Counter | ON CONFLICT triggered (after 1A fix) |
| `material_card.orphan_detected` | Gauge | Current orphan count per entity type |
| `material_card.orphan_healed` | Counter | Records re-linked by self-healer |
| `material_card.integrity_check` | Counter | Integrity checks run, with pass/fail label |
| `material_card.linkage_pct` | Gauge | % of records linked, per entity type |

**Implementation**: Use loguru structured logging with `extra=` dict. These can be aggregated by any log analysis tool.

### 3C: Alert on Critical Events

Log lines with specific patterns that can be matched by log monitoring:

```python
# In integrity check:
if orphan_count > 50:
    log.critical("INTEGRITY_ALERT: {count} orphaned {entity_type} records detected",
                 count=orphan_count, entity_type=entity_type)

# In resolve_material_card (after 1A fix):
if conflict_triggered:
    log.warning("MATERIAL_CARD_RACE: concurrent create for mpn={mpn}, resolved via ON CONFLICT",
                mpn=norm)

# In _upsert_material_card failure path:
log.error("MATERIAL_CARD_UPSERT_FAIL: mpn={mpn} error={error}", mpn=pn, error=str(e))
```

---

## Defense Layer 4: Recovery & Disaster Response

### 4A: Full Re-Link Script ★ HAVE READY (don't deploy unless needed)

An enhanced version of the existing `scripts/backfill_material_card_ids.py` that:

1. Reads ALL requirements, sightings, offers
2. Recomputes `normalized_mpn` from raw MPN fields
3. Resolves material cards (find-or-create)
4. Sets `material_card_id` on every record
5. Reports before/after linkage stats
6. Runs in batches of 1000 with progress logging
7. **Dry-run mode** by default — shows what would change without writing

**This already exists** and was used successfully in Phase 1 (100% linkage achieved). Keep it updated and tested.

### 4B: Card Merge Tool ★ IMPORTANT

For when duplicate cards are discovered (different `normalized_mpn` values that should be the same, or manual dedup):

```
POST /api/admin/materials/merge
{
    "source_card_id": 123,    // card to merge FROM (will be deleted)
    "target_card_id": 456     // card to merge INTO (will be kept)
}
```

**Algorithm**:
1. Re-point all requirements, sightings, offers from source → target
2. Merge vendor histories (combine counts, keep earliest first_seen, latest last_seen)
3. Log to audit table
4. Delete source card
5. Return summary of what changed

### 4C: Protect Against Accidental Card Deletion ★ IMPORTANT

Add a soft-delete mechanism:

```python
# On MaterialCard model:
deleted_at = Column(DateTime, nullable=True)  # NULL = active, non-NULL = soft-deleted

# In queries, filter by default:
db.query(MaterialCard).filter(MaterialCard.deleted_at.is_(None))
```

**Why**: Prevents V5 entirely. A "deleted" card can be restored. The SET NULL cascade never fires because the card still exists in the DB.

**Hard delete**: Only via admin endpoint, requires confirmation, logged to audit.

---

## Implementation Priority & Phases

### Phase 1: Stop the Bleeding (Week 1)
| Item | Effort | Impact |
|------|--------|--------|
| **1A**: Atomic upsert (ON CONFLICT) | 2 hours | Eliminates race condition (V1) |
| **2A**: Integrity check job (checks only) | 3 hours | Detects V3, V4 immediately |
| **2B**: Self-healing re-linker | 2 hours | Auto-fixes orphans (V3) |
| **3C**: Alert logging patterns | 1 hour | Makes problems visible (V6) |

### Phase 2: Build Confidence (Week 2)
| Item | Effort | Impact |
|------|--------|--------|
| **1B**: Vendor name normalization in history | 3 hours | Fixes V2, cleans vendor data |
| **2C**: Integrity health endpoint | 2 hours | Dashboard visibility |
| **3B**: Structured metrics | 2 hours | Trend tracking |
| **4B**: Card merge tool | 3 hours | Manual dedup capability |

### Phase 3: Full Protection (Week 3)
| Item | Effort | Impact |
|------|--------|--------|
| **1C**: Redundant `normalized_mpn` on all tables | 3 hours | Backup linkage key (V7) |
| **3A**: Audit log table | 3 hours | Full change history |
| **4C**: Soft-delete for material cards | 2 hours | Prevents V5 |
| **4A**: Update re-link script for new columns | 1 hour | Disaster recovery readiness |

---

## Ongoing Operations

### Daily
- Integrity check runs automatically every 6 hours
- Self-healer runs if orphans detected
- Review `MATERIAL_CARD_UPSERT_FAIL` log entries (should be zero after 1A)

### Weekly
- Check `/api/admin/integrity` endpoint for any degraded status
- Review material card growth rate (ensure not abnormal)
- Spot-check 5 random material cards: verify linked records make sense

### Monthly
- Run full re-link script in **dry-run mode** — compare output to current state
- Review vendor history dedup — check for new case-sensitivity issues
- Archive audit log entries older than 6 months

### After Any Normalization Logic Change
- **MANDATORY**: Run full re-link in dry-run mode first
- Review every MPN that would change cards
- Apply change + re-link in a maintenance window

---

## Validation Criteria

Before declaring this plan fully implemented, verify:

- [ ] `resolve_material_card()` uses atomic upsert — test with 10 concurrent requests for same MPN
- [ ] Integrity check correctly detects: orphaned records, cross-record mismatches, dangling FKs
- [ ] Self-healer successfully re-links orphaned records within one check cycle
- [ ] Health endpoint returns accurate counts (verified against direct DB queries)
- [ ] Vendor history merges correctly on case-variant names
- [ ] Audit log captures: create, link, unlink, merge, heal events
- [ ] Card merge tool correctly re-points all FKs and merges vendor history
- [ ] Soft-delete prevents SET NULL cascade
- [ ] Full re-link dry-run matches current state (0 changes needed)
- [ ] All metrics visible in structured logs

---

## Risk After Implementation

| Vulnerability | Before | After |
|---------------|--------|-------|
| V1: Race condition | Sightings silently unlinked | Eliminated (atomic upsert) |
| V2: Vendor name case | Duplicate history records | Normalized keying + dedup migration |
| V3: Orphan records | Undetected indefinitely | Detected in <6h, auto-healed |
| V4: Cross-record mismatch | Undetected indefinitely | Detected in <6h, alerted |
| V5: Accidental card delete | Silent data loss | Soft-delete prevents loss |
| V6: No monitoring | Problems found by users | Automated checks + alerts + dashboard |
| V7: No backup linkage | Slow recovery from corruption | Redundant key enables fast re-link |
