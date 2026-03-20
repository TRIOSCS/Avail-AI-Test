# Requirement & Offer Fields + Column Picker

## Problem

Multiple fields exist on the Requirement and Offer models but are not exposed in the UI add/edit forms. Users also need 3 new columns and column visibility controls on table views.

## Design

### New Model Columns (1 Alembic migration)

- `Requirement.customer_pn` — String(255), nullable. Customer's internal part number.
- `Requirement.need_by_date` — Date, nullable. When customer needs the parts.
- `Offer.spq` — Integer, nullable. Standard Pack Quantity (vendor's minimum shipping unit).
- `User.requirements_column_prefs` — JSON, nullable. Stores array of visible column keys for requirements table.
- `User.offers_column_prefs` — JSON, nullable. Stores array of visible column keys for offers table.

### Requirement Add Form (`app/templates/htmx/partials/requisitions/tabs/parts.html`)

Expand from 5 fields to 11 + notes, using existing `grid grid-cols-2 md:grid-cols-4 gap-3` layout:

- **Row 1**: MPN\* (required), Qty (number, default 1), Brand (text), Target Price (number, step 0.0001)
- **Row 2**: Customer PN (text), Need-by Date (date input), Condition (select: New, New Surplus, ETN, Refurbished, Used, Pulls, As-Is), Packaging (text)
- **Row 3**: Date Codes (text), Firmware (text), Hardware Codes (text), Substitutes (text, comma-sep)
- **Full-width**: Notes (textarea, 2 rows)

Styling matches existing exactly:

- Labels: `block text-xs text-gray-500 mb-1`
- Inputs: `w-full px-2 py-1.5 text-sm border border-gray-300 rounded focus:ring-brand-500 focus:border-brand-500`
- Submit: `px-4 py-1.5 text-sm font-medium text-white bg-brand-500 rounded hover:bg-brand-600`

### Requirement Edit Form (inline in `req_row.html`)

Add the same fields to the inline edit form that appears on double-click. Follow existing pattern of input fields within table cells.

### Offer Add Form (`app/templates/htmx/partials/requisitions/add_offer_form.html`)

Expand from 9 fields to 17 + notes, using same grid layout:

- **Row 1**: Vendor Name\* (required), MPN\* (required), Qty Available (number), Unit Price (number, step 0.0001)
- **Row 2**: Manufacturer (text), Lead Time (text), Date Code (text), Condition (select: same options as requirement)
- **Row 3**: MOQ (number), SPQ (number), Packaging (text), Firmware (text)
- **Row 4**: Hardware Code (text), Warranty (text), Country of Origin (text), Valid Until (date input)
- **Linked Requirement** (select, existing)
- **Full-width**: Notes (textarea, 2 rows)

### Offer Edit Form (`app/templates/htmx/partials/requisitions/edit_offer_form.html`)

Add the same new/missing fields. Follow existing edit form pattern.

### Schema Updates

- `RequirementCreate` (`app/schemas/requisitions.py`): Add `customer_pn: str | None = None`, `need_by_date: date | None = None`
- `RequirementUpdate` (`app/schemas/requisitions.py`): Add same fields
- `RequirementOut` (`app/schemas/requisitions.py`): Add same fields
- `OfferCreate` (`app/schemas/crm.py`): Add `spq: int | None = Field(default=None, ge=1)`
- `OfferUpdate` (`app/schemas/crm.py`): Add same field
- `OfferOut` (`app/schemas/crm.py`): Add same field

### Column Picker on Table Views

Reuse existing `app/templates/htmx/partials/shared/column_picker.html` component. This component already provides:

- Gear icon button that opens a checkbox dropdown
- localStorage persistence with `avail_cols_<pickerId>` key
- Reset-to-defaults button
- Dynamic column visibility via `data-col-key` attributes
- Prevents hiding all columns

Wire up for two tables:

1. **Requirements table** (inside requisition detail, `parts.html`):
   - pickerId: `"requirements"`
   - POST endpoint: `/v2/partials/requisitions/{id}/column-prefs` saves to `user.requirements_column_prefs`
   - Default visible: MPN, Brand, Qty, Target Price, Customer PN, Need-by Date, Status, Sightings
   - Toggleable: Condition, Date Codes, Firmware, Hardware Codes, Packaging, Notes, Substitutes

2. **Offers table** (inside requisition detail, offers tab):
   - pickerId: `"offers"`
   - POST endpoint: `/v2/partials/requisitions/{id}/offers-column-prefs` saves to `user.offers_column_prefs`
   - Default visible: Vendor, MPN, Qty, Price, Condition, Date Code, Lead Time, Status
   - Toggleable: Manufacturer, MOQ, SPQ, Packaging, Firmware, Hardware Code, Warranty, Country of Origin, Valid Until, Notes

Follow existing pattern from parts list:

- Define `_ALL_REQ_COLUMNS` and `_DEFAULT_REQ_COLUMNS` constants in router
- Define `_ALL_OFFER_COLUMNS` and `_DEFAULT_OFFER_COLUMNS` constants in router
- Validate column names against whitelist
- Save to user JSON column on POST
- Re-render table with updated visibility on response

### Table Row Templates

- `req_row.html`: Add `data-col-key` attributes to each `<td>` for column picker toggling. Add new cells for customer_pn, need_by_date, condition, date_codes, firmware, hardware_codes, packaging, notes.
- Offers tab table: Add `data-col-key` attributes. Add new cells for manufacturer, spq, packaging, firmware, hardware_code, warranty, country_of_origin, valid_until, notes.

### What We're NOT Building

- No separate `user_preferences` table — use JSON columns on existing User model
- No localStorage-to-server background sync — follow existing synchronous POST pattern
- No debouncing — not needed with synchronous saves
- No server-side column filtering — CSS hiding via `data-col-key` is sufficient for typical row counts

## Migration

Single Alembic migration adding 5 columns:

```python
# upgrade
op.add_column('requirements', sa.Column('customer_pn', sa.String(255)))
op.add_column('requirements', sa.Column('need_by_date', sa.Date()))
op.add_column('offers', sa.Column('spq', sa.Integer()))
op.add_column('users', sa.Column('requirements_column_prefs', sa.JSON()))
op.add_column('users', sa.Column('offers_column_prefs', sa.JSON()))

# downgrade
op.drop_column('users', 'offers_column_prefs')
op.drop_column('users', 'requirements_column_prefs')
op.drop_column('offers', 'spq')
op.drop_column('requirements', 'need_by_date')
op.drop_column('requirements', 'customer_pn')
```

## Testing

- Test migration up/down
- Test RequirementCreate/Update with new fields
- Test OfferCreate/Update with spq
- Test column prefs POST endpoints (follow `test_column_prefs.py` pattern)
- Test that default columns render correctly
- Test that toggling columns persists to user model

## Files to Modify

1. `app/models/sourcing.py` — add customer_pn, need_by_date to Requirement
2. `app/models/offers.py` — add spq to Offer
3. `app/models/auth.py` — add requirements_column_prefs, offers_column_prefs to User
4. `app/schemas/requisitions.py` — update RequirementCreate, RequirementUpdate, RequirementOut
5. `app/schemas/crm.py` — update OfferCreate, OfferUpdate, OfferOut
6. `alembic/versions/XXX_add_requirement_offer_fields.py` — migration
7. `app/templates/htmx/partials/requisitions/tabs/parts.html` — requirement add form + column picker
8. `app/templates/htmx/partials/requisitions/tabs/req_row.html` — requirement table row + inline edit
9. `app/templates/htmx/partials/requisitions/add_offer_form.html` — offer add form
10. `app/templates/htmx/partials/requisitions/edit_offer_form.html` — offer edit form
11. `app/templates/htmx/partials/requisitions/tabs/offers.html` — offers table + column picker (if exists)
12. `app/routers/htmx_views.py` — column prefs endpoints, column constants
13. `tests/test_column_prefs.py` — new column pref tests
14. `tests/test_requisitions.py` — requirement CRUD with new fields
15. `tests/test_offers.py` — offer CRUD with spq
