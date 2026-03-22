# Required Manufacturer for Part Entry

**Date:** 2026-03-22
**Status:** Approved

## Context

Material card enrichment was disabled because AI-only enrichment (guessing from MPNs) produces hallucinated data. To rebuild enrichment with 100% accuracy using real data sources (DigiKey, Mouser, Nexar), we need the manufacturer name. Without it, an MPN alone is ambiguous — the same part number can exist across manufacturers.

Substitutes are equal to primary parts from a sourcing perspective. Both require the same data quality.

## Scope

1. **Manufacturer lookup table** — canonical names + aliases, typeahead search
2. **Requirement model** — add required `manufacturer` column
3. **Substitutes JSON restructure** — from string array to array of objects with `mpn` + `manufacturer`
4. **UI: manufacturer typeahead** on all part entry forms (primary + subs)
5. **UI: structured sub input** — replace comma-separated text with per-sub rows
6. **Material card integration** — pass manufacturer to `resolve_material_card()`
7. **Validation** — manufacturer required on all entry paths

## Design

### 1. Manufacturer Lookup Table

**New model in `app/models/sourcing.py`:** (co-located with Requirement since it's directly referenced)

```python
class Manufacturer(Base):
    __tablename__ = "manufacturers"
    id = Column(Integer, primary_key=True)
    canonical_name = Column(String(255), nullable=False, unique=True, index=True)
    aliases = Column(JSON, default=list)  # ["TI", "Texas Inst", "T.I."]
    website = Column(String(500))  # optional, for future enrichment
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
```

**Seed data:** ~50-100 common electronic component manufacturers (TI, ADI, Microchip, STMicro, NXP, ON Semiconductor, Infineon, Renesas, Maxim/ADI, Vishay, Murata, TDK, Samsung, Hynix, Micron, Intel, AMD, Broadcom, Qualcomm, etc.). Each with common aliases.

**Typeahead endpoint:**

```
GET /v2/partials/manufacturers/search?q=ti
```

Returns HTML partial with matching manufacturers (searches canonical_name + aliases via ILIKE). Results show canonical name. If no match, show "Add new manufacturer" option that creates the record on the fly.

**Storage policy:** Manufacturer is stored as a **free-text `String(255)`** on Requirement, not as an FK to the `Manufacturer` table. The lookup table exists purely for typeahead/normalization assistance. This is intentionally denormalized — the `Manufacturer` table helps users enter consistent data but does not constrain it. Server-side validation checks that the manufacturer field is non-empty but does NOT require it to exist in the lookup table. Users can type a manufacturer name that isn't in the table (edge case for obscure manufacturers).

**Normalization:** When the user selects from the typeahead, the canonical name is stored. If they type freely, whatever they type is stored. Aliases are only for search/typeahead matching.

### 2. Requirement Model Changes

**Add column:**

```python
manufacturer = Column(String(255), nullable=False, server_default="")
```

**Alembic migration:**

```python
def upgrade():
    op.add_column("requirements", sa.Column("manufacturer", sa.String(255), nullable=True))
    # Best-effort backfill from existing brand column
    op.execute("UPDATE requirements SET manufacturer = COALESCE(brand, '') WHERE manufacturer IS NULL")
    op.alter_column("requirements", "manufacturer", nullable=False, server_default=sa.text("''"))
    op.create_index("ix_requirements_manufacturer", "requirements", ["manufacturer"])

def downgrade():
    op.drop_index("ix_requirements_manufacturer")
    op.drop_column("requirements", "manufacturer")
```

**Existing `brand` column** stays as-is (optional, for dual-label cases like IBM-branded Seagate drives).

### 3. Substitutes JSON Structure Change

**Before:**

```json
["LM338T", "SG3525"]
```

**After:**

```json
[
  {"mpn": "LM338T", "manufacturer": "Texas Instruments"},
  {"mpn": "SG3525", "manufacturer": "ON Semiconductor"}
]
```

**Migration:** Convert existing string arrays to object arrays:

```python
op.execute("""
    UPDATE requirements
    SET substitutes = (
        SELECT jsonb_agg(jsonb_build_object('mpn', elem, 'manufacturer', ''))
        FROM jsonb_array_elements_text(substitutes::jsonb) AS elem
    )
    WHERE substitutes IS NOT NULL
      AND jsonb_typeof(substitutes::jsonb) = 'array'
      AND jsonb_array_length(substitutes::jsonb) > 0
      AND jsonb_typeof(substitutes::jsonb -> 0) = 'string'
""")
```

Existing subs get `manufacturer: ""` — these are flagged as incomplete until the user fills them in.

**`substitutes_text` generated column:** Currently defined as `substitutes::text` which stringifies the entire JSON. After the restructure, the raw JSON text will contain `{"mpn": "LM338T", "manufacturer": "..."}` — MPN substrings are still present in the text, so ILIKE search (`WHERE substitutes_text ILIKE '%LM338T%'`) continues to work. However, the column now also contains manufacturer text and JSON syntax noise. The migration must **drop and recreate** the generated column to extract only MPNs:

```sql
ALTER TABLE requirements DROP COLUMN substitutes_text;
ALTER TABLE requirements ADD COLUMN substitutes_text TEXT
  GENERATED ALWAYS AS (
    (SELECT string_agg(elem->>'mpn', ', ')
     FROM jsonb_array_elements(COALESCE(substitutes, '[]'::jsonb)) AS elem)
  ) STORED;
```

This keeps the GIN trigram index functional and search results clean.

**`parse_substitute_mpns()` changes:**

Current signature: `parse_substitute_mpns(raw: str, primary_mpn: str) -> list[str]`

New signature: `parse_substitute_mpns(subs: list[dict], primary_mpn: str, *, limit: int = MAX_SUBSTITUTES) -> list[dict]`

- `subs`: list of `{"mpn": "...", "manufacturer": "..."}` dicts (pre-structured by caller)
- `primary_mpn`: used to exclude the primary from the sub list (same as before)
- Returns: `[{"mpn": "LM338T", "manufacturer": "TI"}, ...]` — normalized, deduped, limit-capped
- Each MPN normalized via `normalize_mpn()`, deduped via `normalize_mpn_key()`
- Manufacturer passed through as-is (no normalization on manufacturer)

**Callers** (htmx_views.py) zip the form arrays `sub_mpn[]` + `sub_manufacturer[]` into dicts before calling:

```python
subs_raw = [
    {"mpn": m.strip(), "manufacturer": mfr.strip()}
    for m, mfr in zip(form.getlist("sub_mpn"), form.getlist("sub_manufacturer"))
    if m.strip()
]
sub_list = parse_substitute_mpns(subs_raw, primary_mpn)
```

### 4. UI: Manufacturer Typeahead

**All part entry forms get a manufacturer field:**

- Import form (create requisition with indexed parts)
- Quick-add form (create requisition from text)
- Add requirement form (single part add to existing requisition)
- Update requirement form (inline edit)
- **Header inline edit** (click-to-edit on header — uses typeahead for manufacturer field)

**Typeahead behavior:**

- Text input with `hx-get="/v2/partials/manufacturers/search?q=..."` on `input` event (300ms debounce)
- Dropdown shows matching canonical names
- Clicking a result fills the input with the canonical name
- If no match: "Add [typed text] as new manufacturer" option
- Clicking "Add" creates a new Manufacturer record and fills the input
- Field is required — form submission blocked without it

**Styling:** Same compact input styling as MPN field. Positioned directly after MPN in all forms.

### 5. UI: Structured Sub Input

**Current:** Single text input, comma-separated MPNs: `"LM338T, SG3525"`

**New:** Multi-row structured input. Each row has:
- MPN text input (required)
- Manufacturer typeahead input (required)
- Remove button (x)
- "Add substitute" button below the rows

```html
<div class="space-y-1">
  <!-- Existing sub rows -->
  <div class="flex gap-2 items-center">
    <input name="sub_mpn[]" placeholder="MPN" required class="...">
    <input name="sub_manufacturer[]" placeholder="Manufacturer" required class="..."
           hx-get="/v2/partials/manufacturers/search" ...>
    <button type="button" @click="removeSub(idx)" class="text-gray-400 hover:text-red-500">×</button>
  </div>
  <!-- Add button -->
  <button type="button" @click="addSub()" class="text-xs text-brand-500">+ Add substitute</button>
</div>
```

Alpine.js manages the dynamic rows via an array of `{mpn, manufacturer}` objects.

**Header inline edit for subs:** The header's click-to-edit for substitutes currently uses a comma-separated text input. This changes to the same structured multi-row input described above. The `PATCH /v2/partials/parts/{id}/header` route receives `sub_mpn[]` + `sub_manufacturer[]` arrays and zips them into the new format.

### 6. Material Card Integration

**`resolve_material_card()` changes:**

Current: `resolve_material_card(mpn: str, db: Session) -> MaterialCard | None`

New: `resolve_material_card(mpn: str, db: Session, manufacturer: str = "") -> MaterialCard | None`

- If `manufacturer` is provided and the card exists without a manufacturer, update it
- If creating a new card, set the manufacturer
- MaterialCard already has a `manufacturer` column — no schema change needed

**Substitute material card loops** must pass manufacturer per-sub. After the JSON restructure, `sub_list` is `list[dict]`, so the existing loops change:

```python
# Before:
for sub_mpn in sub_list:
    resolve_material_card(sub_mpn, db)

# After:
for sub in sub_list:
    resolve_material_card(sub["mpn"], db, manufacturer=sub.get("manufacturer", ""))
```

This applies to all 3 HTMX paths (import-save, add_requirement, update_requirement) and the header save path.

### 7. Display Changes

**Part header (`header.html`):**
- Show manufacturer prominently: `LM317T · Texas Instruments` (before brand)
- Manufacturer is click-to-edit (typeahead) like other header fields

**Sub chips in left panel (`list.html`):**
- Chip shows MPN only (compact)
- Tooltip shows `"LM338T — Texas Instruments"` (manufacturer in tooltip)

**Sub chips in header:**
- Show as `MPN (Mfr)` format: `LM338T (TI)`

**REQ Detail sibling table:**
- Add "Mfr" column showing the manufacturer

### 8. Validation

All entry paths enforce manufacturer:

- **HTMX paths** (4 routes + header save in `htmx_views.py`): validate manufacturer is non-empty before creating/updating Requirement. Return 422 with user-friendly error.
- **API routes** (in `requisitions/requirements.py`): same validation via Pydantic schema `manufacturer: str` (required).
- **Subs**: each sub dict must have non-empty `mpn` and `manufacturer`.
- Parts with empty manufacturer from migration are allowed to exist but flagged in UI (subtle indicator prompting user to fill in).

## What This Does NOT Change

- `brand` field — stays optional, unchanged semantics
- Enrichment — stays disabled (this is the data prerequisite)
- MPN normalization — `normalize_mpn_key()`, `normalize_mpn()` unchanged
- Left panel columns — no new columns
- Offer matching — unchanged

## Files Changed

| File | Change |
|------|--------|
| `app/models/sourcing.py` | Add `Manufacturer` model + `manufacturer` column on `Requirement` |
| `alembic/versions/xxx_add_manufacturer.py` | Migration: add column, backfill, convert subs JSON, recreate `substitutes_text` generated column |
| `app/utils/normalization.py` | Update `parse_substitute_mpns()` to accept/return `list[dict]` |
| `app/search_service.py` | Update `resolve_material_card()` to accept `manufacturer` param |
| `app/routers/htmx_views.py` | Add manufacturer typeahead endpoint, update 4 entry paths + header save, validation, sub_card_map updates |
| `app/routers/requisitions/requirements.py` | Update API validation |
| `app/templates/htmx/partials/parts/list.html` | Sub chip tooltips with manufacturer |
| `app/templates/htmx/partials/parts/header.html` | Show manufacturer, structured sub edit, update sub chip format |
| `app/templates/htmx/partials/parts/tabs/req_details.html` | Add Mfr column to sibling table |
| `app/templates/htmx/partials/requisitions/tabs/parts.html` | Update add/edit forms with manufacturer fields |
| `app/templates/htmx/partials/manufacturers/search.html` | New typeahead results partial |
| `app/startup.py` | Seed manufacturer lookup data |

## Testing

- Manufacturer CRUD: create, search, add-on-the-fly
- Typeahead: returns matches on canonical name + aliases
- Requirement creation: manufacturer required, rejected without
- Requirement update: manufacturer editable
- Subs: structured input creates correct JSON objects with manufacturer
- Subs: each sub requires manufacturer
- Subs: parse_substitute_mpns accepts list[dict], returns normalized list[dict]
- Migration: existing data backfilled correctly
- Migration: existing subs converted to object format
- Migration: substitutes_text generated column recreated with MPN-only extraction
- resolve_material_card: sets manufacturer on new/existing cards
- resolve_material_card: sub loops pass manufacturer per-sub
- Display: header shows manufacturer, tooltips correct
- Header inline edit: structured sub input with manufacturer typeahead
