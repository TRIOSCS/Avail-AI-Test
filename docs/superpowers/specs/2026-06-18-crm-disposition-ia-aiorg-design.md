# CRM Cockpit Refinements — Disposition, Left-Panel IA, AI Organization

**Status:** Approved 2026-06-18 (brainstormed + 4 decisions confirmed by user). Folds into the relationship-cadence cockpit redesign ([[2026-06-17-crm-redesign-design.md]]). Grounded in two code-mapping sweeps (disposition; IA+AI-org).

**Goal:** Give salespeople control over account/contact lifecycle, make the left panel navigate company→site, and let AI keep company names + site groupings clean — all by reusing existing machinery.

## Locked decisions
1. **Disposition permissions:** owner-or-admin (the account owner disposes of their own; admins act on any). Mirrors `release_prospect`'s `is_admin` override.
2. **Multi-site accordion expand:** right panel shows a **company-header-only** partial (header + cadence + commercial rollups, no per-site tabs). The redundant in-panel Sites tab is retired (its add/edit/delete site actions move into the new site-detail view).
3. **Per-site tasks:** defer a real task model; show **"Open requisitions at this site"** (`Requisition.customer_site_id == site_id`). Contacts + notes are genuinely site-scoped now.
4. **AI-org depth:** full — fix the broken review queue + per-account banner + suggest-only name chip **and** add the durable `Company.normalized_name` (pg_trgm) + `alternate_names` foundation (mirror VendorCard).

## Three independent increments (separate PRs, build + deploy in order)

### Increment 1 — Disposition (account + contact lifecycle)
**Model (migration A):**
- `Company.disposition` `String(20)` indexed; `CompanyDisposition` StrEnum `{active, bucket}` in `app/constants.py` beside `ProspectAccountStatus`. NULL ⇒ active (mirror tier's NULL⇒standard).
- `Company.disposition_reason` `String` nullable; `disposition_set_by` (FK users SET NULL), `disposition_set_at` (UTCDateTime) — parity with prospect dismiss audit. Reason is **optional**.
- `SiteContact.is_priority` and `SiteContact.is_archived` — `Boolean NOT NULL server_default 'false'` (mirror `do_not_contact` exactly). NOT `is_active` (would vanish), NOT `contact_status` (machine-managed).

**Behavior:**
- **Send to Prospecting:** new `prospect_claim.send_company_to_prospecting(company_id, user_id, db, is_admin)` mirroring `release_prospect` (FOR UPDATE lock, clear `Company.account_owner_id` + set `ownership_cleared_at=now`, find-or-create `ProspectAccount(status=SUGGESTED)` by `Company.domain` via the `add_prospect_manually` dedupe pattern; commit; rollback-on-error). **No-domain fallback:** ownership-clear only (skip pool row). **Perms:** owner-or-admin; admin force-clears another owner's account (mirror release's `is_admin`).
- **BUCKET suppression:** add NULL-safe `or_(Company.disposition != 'bucket', Company.disposition.is_(None))` to the **shared** `crm_service._needs_call_filter` ONLY (preserves count==list invariant for the "N need a call" chip + click-through list), and to `cdm_company_query` base UNLESS a "Bucket" facet is explicitly selected (so it stays findable/un-bucketable). Suppression at the QUERY layer — never in `materialize_all_clocks`.
- **Contact sort:** change `crm_service.company_contact_rows` order_by to `is_archived.asc(), is_priority.desc(), is_primary.desc(), full_name`. Keep the `is_active.is_(True)` filter (archived rows stay, just sort to bottom). Guard legacy `contact is None` rows.

**Routes/UI (clone setter-route pattern):**
- `POST /v2/partials/customers/{company_id}/disposition` — clone `set_company_tier` (`_VALID_DISPOSITIONS` frozenset, 404, validate→400, write + audit fields, `invalidate_prefix('company_list','companies_typeahead')`, re-render disposition chip). Owner-or-admin gate. Reversible (set back to active).
- `POST /v2/partials/customers/{company_id}/send-to-prospecting` — calls the service; returns CRM partial + `showToast`.
- `POST /v2/partials/customers/{company_id}/contacts/{contact_id}/priority` and `.../archive` — clone `set_contact_dnc` incl. the IDOR-safe `SiteContact JOIN CustomerSite WHERE company_id==company_id` filter. New `_priority_toggle.html` / `_archive_toggle.html` mirror `_dnc_toggle.html`.

### Increment 2 — Left-panel IA (company → sites) + notes FK fix
- **Left row branch (`_account_list.html`):** reference `c.site_count` (already on every row, trigger-maintained — tests assert behavior not the count). `site_count <= 1` → today's exact behavior (`hx-get /v2/partials/customers/{id}` → `#cdm-detail`). `site_count > 1` → row becomes accordion header (`x-data="{expanded:false}"`, chevron, `x-show`/`x-transition` copied from `site_card.html`; NO global window listeners — Alpine-leak rule). Name click toggles expand AND hx-gets the company-header partial; site children lazy-load via a partial declared ABOVE the `{company_id}` catch-all.
- **Company-header partial:** `GET /v2/partials/customers/{company_id}/header` — detail.html header+cadence+commercial strip WITHOUT the tab strip.
- **Site-detail route:** `GET /v2/partials/customers/{company_id}/sites/{site_id}` (specific shape — won't shadow the `{company_id}` catch-all). Validate `CustomerSite.id==site_id AND company_id==company_id AND is_active` → 404. Renders site fields + per-site clocks + a mini tab strip: **Contacts** (reuse `company_contact_rows(db, company_id, sites=[this_site])`), **Notes** (site-scoped after the FK fix), **Open requisitions at this site** (`Requisition.customer_site_id==site_id`).
- **Notes FK fix (root-cause):** `add_site_contact_note` must populate `ActivityLog.company_id + customer_site_id + site_contact_id`; `get_site_contact_notes` reads by `site_contact_id` (not `contact_email`). Keep `ActivityType.CONTACT_NOTE` literal. Dual-read/backfill so existing email-scoped notes don't vanish.
- **Selection highlight:** add `selectedSiteId` to the `#cdm-workspace` x-data; site links set both `selectedId` + `selectedSiteId`.
- **Retire** the in-panel Sites tab for navigation (move add/edit/delete-site actions into the site-detail actions row) once parity is confirmed.

### Increment 3 — AI organization (durable foundation + surfaces)
**Model (migration B):** `Company.normalized_name` (`String`, pg_trgm GIN index) + `Company.alternate_names` (JSON) — mirror VendorCard's `normalized_name`/`alternate_names`/`_record_alternate_name`. Backfill `normalized_name` from existing names. On `merge_companies`, append the loser's name to `keep.alternate_names` (so re-import doesn't recreate the dupe). Convert `find_company_dedup_candidates` to DB `similarity()` (removes the 500-row O(n²) cap). Unify the two divergent duplicate paths (create-time exact/containment vs scan-time rapidfuzz) behind one scorer.
**Surfaces (reuse engine as-is — do not reimplement merge):**
- **Fix the broken review queue:** `settings/data_ops.html` Company-Duplicates loop reads FLAT `pair.name_a/id_a/sightings_a` but candidates are NESTED `pair.company_a.{id,name}/company_b/score/auto_keep_id` → renders blank + emits empty merge ids (dead today). Rewrite against the nested shape, default direction from `auto_keep_id`, wire to `POST /v2/partials/admin/company-merge`.
- **AI verdict chips:** extend `find_company_dedup_candidates(with_ai_verdict=True)` to attach `_ask_claude_merge`'s `{same_entity, confidence}` on the ambiguous band; honor the **different-owner skip** rule.
- **Per-account banner:** lazy `hx-get` panel in `detail.html` (model on the AI Insights block) → top dup match + Merge button reusing `/merge-form`→`/merge-preview`→`POST .../merge`.
- **Name suggestion chip (suggest-only):** surface `normalize_company_input` output as "Suggested name: … Apply?" on the header — never a silent rewrite (replaces the current fail-open silent rewrite at create).
- **Site-grouping suggestion:** in the site roster, surface within-account site dupes (UPPER(site_name) collision) + "this site may belong to company X".
- **Policy locked:** passive review queue; keep nightly auto-merge ≥98 + 92-97 Claude-gated; never merge different-owner accounts.

## Cross-cutting constraints (honor verbatim — from the mappings)
- count==list invariant: bucket exclusion goes in the SHARED `_needs_call_filter` only.
- `is_active` overload traps: NEVER use `is_active=False` for bucket (account) or archive (contact).
- `merge_companies` reassigns a FIXED 6-table FK set in silent try/except — any NEW `company_id`-bearing table must be added to it.
- Route shadowing: new static partial segments declared ABOVE `GET /v2/partials/customers/{company_id}` (htmx_views.py:5130).
- `@cached_endpoint` `company_list`/`companies_typeahead` — `invalidate_prefix` on disposition/name writes.
- Migrations: claim numbers in `MIGRATION_NUMBERS_IN_FLIGHT.txt`; `server_default` string literals; verify NULL-safe SQL on live Postgres (SQLite masks it); single `alembic heads`.
- Tailwind: verify new badge classes in built CSS post-deploy; prefer existing utilities.
- Legacy contacts have `contact is None` — guard all new `contact.is_*` access behind the real-SiteContact branch.

## Sequencing
Increment 1 (Disposition) → 2 (IA) → 3 (AI-org). Independent PRs; each merged + deployed before the next. Two migrations (A in inc 1, B in inc 3), numbered at build time to minimize collision with concurrent churn.
