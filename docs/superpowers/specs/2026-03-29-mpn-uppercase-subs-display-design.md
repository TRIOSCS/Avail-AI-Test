# MPN Auto-Capitalization + Substitutes Display — Design Spec

**Date:** 2026-03-29
**Status:** Approved (revised after 4-agent review)

## Problem

1. **MPNs stored in mixed case** — part numbers like `ne5559`, `st9500420as`, `SL9bt` exist in the database. The inline edit path does not capitalize. Users expect all part numbers to be uppercase.
2. **Substitutes render as raw dict text** — newer subs stored as `[{"mpn": "...", "manufacturer": "..."}]` display as Python dict repr in the table. Hard to scan, unprofessional.
3. **JSON API create/PATCH stores string subs** — bypasses `parse_substitute_mpns()`, creating recurring format inconsistency even after backfill.

## Solution

1. **Model-level auto-capitalization** on all MPN-like fields + backfill migration
2. **Jinja2 filter + amber chips** for clean, scannable substitute display
3. **Fix JSON API paths** to store dict-format subs consistently

---

## Feature 1: MPN Auto-Capitalization

### Scope

Fields to capitalize: `primary_mpn`, `customer_pn`, `oem_pn` on `Requirement` model. Also substitute MPNs inside the JSON `substitutes` column.

### Model Validator

Add `@validates` to `Requirement` in `app/models/sourcing.py`, following existing `_key` convention:

```python
@validates("primary_mpn", "customer_pn", "oem_pn")
def _uppercase_mpn_fields(self, _key, value):
    return value.upper().strip() if value else value
```

This catches every ORM write path: form POST, inline PUT, API, imports, background jobs.

### MPN Display Cell

Add CSS `uppercase` class to the MPN display cell in `req_row.html` as belt-and-suspenders:

```html
<td data-col-key="mpn" class="... uppercase">{{ r.primary_mpn or "—" }}</td>
```

### Substitute MPN Capitalization

The HTMX form paths already use `parse_substitute_mpns()` which calls `normalize_mpn()` (uppercases). The JSON API paths bypass this — see Feature 3 below.

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

**Pure SQL for JSON substitutes column** (handles both string and dict formats, single pass):
```sql
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
  AND substitutes::text != '[]';
```

**Downgrade:** No-op with comment (capitalization is non-destructive).

---

## Feature 2: Substitutes Display

### Jinja2 Filter: `sub_mpns`

Register in `app/template_env.py`. Delegates to existing `normalize_mpn()` for consistency:

```python
from app.utils.normalization import normalize_mpn

def _sub_mpns_filter(subs):
    """Extract clean uppercase MPN strings from substitutes (handles both string and dict formats)."""
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

### Template: `req_row.html` Substitutes Cell

Replace the current buggy badge (lines 40-46) with amber chips:

```html
<td data-col-key="substitutes" class="px-4 py-2.5 text-sm" x-show="!editing" x-cloak>
  {% set mpns = r.substitutes|sub_mpns %}
  {% if mpns %}
    <div class="flex flex-wrap gap-1">
      {% for mpn in mpns %}
      <span class="px-1.5 py-0.5 text-[10px] font-mono font-medium rounded
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
- Amber chips — distinct from gray text in surrounding columns, catches the eye when scanning. Rectangular `rounded` (not `rounded-full`) follows project convention: rectangles = data labels, pills = status indicators
- `flex-wrap gap-1` — handles multiple subs gracefully, wraps within the cell
- `text-[10px]` — matches all other inline table badges in the codebase
- Each MPN is its own chip — no need to hover for details
- Already-uppercase from filter via `normalize_mpn()` — consistent regardless of DB format

---

## Feature 3: Fix JSON API Substitute Format

### Problem

The JSON API create path (`POST /api/requisitions/{id}/requirements`) at `requirements.py:377-401` stores substitutes as plain strings `["MPN1", "MPN2"]` instead of dicts `[{"mpn": "MPN1", "manufacturer": ""}]`. The PATCH path (`PATCH` at line 715-722) has the same issue. This means new requirements created via the API will have inconsistent sub format even after backfill.

### Fix

In `app/routers/requisitions/requirements.py`:

**Batch create** (lines 377-383): Wrap the dedup loop to store dicts:
```python
deduped_subs = [{"mpn": ns, "manufacturer": ""} for ns in unique_normalized_subs]
```

**PATCH** (lines 715-722): Same pattern — store as dict, not string.

---

## Files Modified

| File | Change |
|------|--------|
| `app/models/sourcing.py` | Add `@validates` for MPN uppercase |
| `alembic/versions/[new].py` | Data migration: backfill uppercase + fix subs |
| `app/template_env.py` | Add `sub_mpns` filter |
| `app/templates/htmx/partials/requisitions/tabs/req_row.html` | Replace subs badge with amber chips + add `uppercase` to MPN cell |
| `app/routers/requisitions/requirements.py` | Fix JSON API create/PATCH to store dict subs |
| `tests/test_models.py` | Tests for `@validates` behavior |
| `tests/test_template_filters.py` (new) | Tests for `sub_mpns` filter |

## Files NOT Modified

- No `normalization.py` changes (already uppercases in `normalize_mpn()`)
- No Pydantic schema changes (not blocking; schema cleanup is separate scope)
- No Alpine.js or CSS changes
- No changes to `parts/list.html` or `parts/header.html` (they already handle dict subs correctly)
