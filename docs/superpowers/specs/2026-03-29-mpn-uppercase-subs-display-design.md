# MPN Auto-Capitalization + Substitutes Display — Design Spec

**Date:** 2026-03-29
**Status:** Approved

## Problem

1. **MPNs stored in mixed case** — part numbers like `ne5559`, `st9500420as`, `SL9bt` exist in the database. The inline edit path does not capitalize. Users expect all part numbers to be uppercase.
2. **Substitutes render as raw dict text** — newer subs stored as `[{"mpn": "...", "manufacturer": "..."}]` display as Python dict repr in the table. Hard to scan, unprofessional.

## Solution

1. **Model-level auto-capitalization** on all MPN-like fields + backfill migration
2. **Jinja2 filter + amber chips** for clean, scannable substitute display

---

## Feature 1: MPN Auto-Capitalization

### Scope

Fields to capitalize: `primary_mpn`, `customer_pn`, `oem_pn` on `Requirement` model. Also substitute MPNs inside the JSON `substitutes` column.

### Model Validator

Add `@validates` to `Requirement` in `app/models/sourcing.py`:

```python
@validates("primary_mpn", "customer_pn", "oem_pn")
def _uppercase_mpn_fields(self, key, value):
    return value.upper().strip() if value else value
```

This catches every write path: form POST, inline PUT, API, imports, background jobs. No router changes needed.

### Substitute MPN Capitalization

In `app/utils/normalization.py`, the existing `parse_substitute_mpns()` function already normalizes substitute MPNs via `normalize_mpn()` which calls `.upper()`. Verify this covers all paths. If any substitute-saving path bypasses `parse_substitute_mpns()`, add `.upper()` there too.

### Backfill Migration

Alembic data-only migration (no schema changes):

**SQL for string columns:**
```sql
UPDATE requirements SET primary_mpn = UPPER(TRIM(primary_mpn))
  WHERE primary_mpn IS NOT NULL AND primary_mpn != UPPER(TRIM(primary_mpn));
UPDATE requirements SET customer_pn = UPPER(TRIM(customer_pn))
  WHERE customer_pn IS NOT NULL AND customer_pn != UPPER(TRIM(customer_pn));
UPDATE requirements SET oem_pn = UPPER(TRIM(oem_pn))
  WHERE oem_pn IS NOT NULL AND oem_pn != UPPER(TRIM(oem_pn));
```

**Python for JSON substitutes column:**
Load all requirements with non-empty substitutes, uppercase each MPN (handling both string and dict formats), save back. Run in batches of 500.

```python
from sqlalchemy import text

# Fetch rows with substitutes
rows = conn.execute(text("SELECT id, substitutes FROM requirements WHERE substitutes IS NOT NULL AND substitutes != '[]'"))
for row in rows:
    subs = json.loads(row.substitutes) if isinstance(row.substitutes, str) else row.substitutes
    updated = []
    for s in subs:
        if isinstance(s, str):
            updated.append(s.upper().strip())
        elif isinstance(s, dict) and "mpn" in s:
            s["mpn"] = s["mpn"].upper().strip() if s["mpn"] else s["mpn"]
            updated.append(s)
    conn.execute(text("UPDATE requirements SET substitutes = :subs WHERE id = :id"),
                 {"subs": json.dumps(updated), "id": row.id})
```

**Downgrade:** No-op (capitalization is non-destructive, no rollback needed).

---

## Feature 2: Substitutes Display

### Jinja2 Filter: `sub_mpns`

Register in `app/template_env.py`:

```python
def _sub_mpns_filter(subs):
    """Extract clean uppercase MPN strings from substitutes (handles both string and dict formats)."""
    if not subs:
        return []
    result = []
    for s in subs:
        if isinstance(s, str):
            mpn = s.strip().upper()
        elif isinstance(s, dict):
            mpn = (s.get("mpn") or "").strip().upper()
        else:
            continue
        if mpn:
            result.append(mpn)
    return result

templates.env.filters["sub_mpns"] = _sub_mpns_filter
```

### Template: `req_row.html` Substitutes Cell

Replace the current buggy badge (lines 40-46) with amber chips:

```html
<td data-col-key="substitutes" class="px-4 py-2.5 text-sm" x-show="!editing" x-cloak>
  {% set mpns = r.substitutes|sub_mpns %}
  {% if mpns %}
    <div class="flex flex-wrap gap-1">
      {% for mpn in mpns %}
      <span class="px-1.5 py-0.5 text-[11px] font-mono font-medium rounded
                   bg-amber-50 text-amber-700 border border-amber-200">{{ mpn }}</span>
      {% endfor %}
    </div>
  {% else %}
    <span class="text-gray-400">—</span>
  {% endif %}
</td>
```

**Design rationale:**
- `font-mono` — part numbers are identifiers, monospace aids readability
- Amber chips — distinct from gray text in surrounding columns, catches the eye when scanning
- `flex-wrap gap-1` — handles multiple subs gracefully, wraps within the cell
- `text-[11px]` — slightly larger than old 10px badge, readable at a glance
- Each MPN is its own chip — no need to hover for details
- Already-uppercase from filter — consistent regardless of DB format

---

## Files Modified

| File | Change |
|------|--------|
| `app/models/sourcing.py` | Add `@validates` for MPN uppercase |
| `alembic/versions/[new].py` | Data migration: backfill uppercase + fix subs |
| `app/template_env.py` | Add `sub_mpns` filter |
| `app/templates/htmx/partials/requisitions/tabs/req_row.html` | Replace subs badge with amber chips |
| `tests/test_manual_search.py` or new test file | Tests for validator + filter |

## Files NOT Modified

- No router changes (model validator handles all paths)
- No `normalization.py` changes (already uppercases in `normalize_mpn()`)
- No schema changes (data-only migration)
- No Alpine.js or CSS changes
