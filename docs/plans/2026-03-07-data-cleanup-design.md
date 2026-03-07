# Data Cleanup Design

**Date**: 2026-03-07
**Scope**: Dedup SiteContacts, normalize phones, extract phones from site_name, add forward guards

## Problem

- 94 duplicate SiteContacts (same email on same site, no unique constraint)
- 55 unformatted phone numbers across multiple tables (validator falls back to raw input)
- 97 CustomerSite records with phone numbers embedded in `site_name`

## Solution

### Migration 048: Data Cleanup

**Schema change:**
- Add `contact_phone_2` (String 100, nullable) to `customer_sites`

**Data fixes (Python in migration):**

1. **SiteContact dedup**: Group by `(customer_site_id, lower(email))`. For each group with >1 record, merge non-null fields into the richest record (most populated fields), delete the rest. Preserve `is_primary=True` record when present.

2. **Phone normalization**: Run `format_phone_e164()` on all phone columns:
   - `companies.phone`
   - `customer_sites.contact_phone`
   - `site_contacts.phone`
   - `vendor_contacts.phone`
   - `vendor_contacts.phone_mobile`

3. **Site name phone extraction**: Regex scan `customer_sites.site_name` for phone patterns (10+ digit sequences, parenthesized area codes, +CC prefixes). Extract to `contact_phone` if empty, else `contact_phone_2`. Strip extracted phone from `site_name`.

### Forward Guards (schema/router layer)

1. **SiteContact dedup guard**: `SiteContactCreate` validator checks for existing contact with same email on same site before insert. Return existing record instead of creating duplicate.

2. **Site name phone guard**: `CustomerSiteCreate` validator extracts phone patterns from `site_name` field, moves them to `contact_phone`/`contact_phone_2`, saves clean name.

3. **E.164 enforcement**: Remove `or v` fallback from phone validators — reject unparseable phones with validation error instead of silently storing raw input.

## Models Affected

- `CustomerSite` — add `contact_phone_2` column
- `SiteContact` — dedup only, no schema change
- `Company`, `VendorContact` — data fix only

## Key Files

- `alembic/versions/048_data_cleanup.py` — migration
- `app/schemas/crm.py` — guard validators
- `app/routers/crm.py` — dedup check on SiteContact create
- `app/utils/phone_utils.py` — existing E.164 formatter (no changes needed)

## Testing

- Dedup merge logic: 2+ contacts same email/site, verify richest record kept
- Phone normalization: raw → E.164 across all tables
- Site name extraction: "Main Office (415) 555-1234" → site_name="Main Office", contact_phone="+14155551234"
- Guards: attempt duplicate SiteContact → blocked, phone in site_name → auto-extracted, bad phone → validation error
