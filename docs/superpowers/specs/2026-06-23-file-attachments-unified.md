# Spec — Unified File Attachments across 5 entities

**Date:** 2026-06-23
**Branch:** `feat/attachments-unified` (worktree off `main` @ f57302dd)
**Migration:** 126 (chains onto `125_enrichment_provenance`)

## Goal

Let users attach arbitrary files (PDFs, images, office docs) to **five** entities with one
consistent experience: **Offer, Requirement, Requisition, Company (CRM account),
SiteContact (CRM contact), MaterialCard (part card).** Today Offer/Requirement/Requisition
have attachment *backends* (Offer has UI; Requirement/Requisition's endpoints are dead — no
UI). Company/SiteContact/MaterialCard have nothing. We unify all of them onto one shared
service + one shared UI component.

> Note: "five entities" the user named = Offer, Requirement, Account(=Company),
> Contact(=SiteContact), MaterialCard. Requisition is folded in because it already shares the
> attachment table family and the requirement parts live under it.

## Locked decisions (from the user)

1. **Storage = SharePoint company library** (app-token, the `datasheet_library` pattern),
   **with automatic user-OneDrive fallback** while `DATASHEET_LIBRARY_DRIVE_ID` is unset
   (it is currently unset on the live app — IT pending). So: works + live-verifiable today on
   user OneDrive; auto-upgrades to the shared company drive the instant IT sets the ID, with
   **no redeploy and no code change**.
2. **Unify all 5** — restandardize the existing Offer UI and surface the dead
   Requirement/Requisition backend onto the single shared component, so the attach experience
   is byte-identical in every place.

## Architecture

### Storage abstraction (the heart of the feature)

A single function chooses backend **per upload** based on config, and each row records which
backend it landed on via the `library_drive_id`-NULL discriminator:

- `settings.datasheet_library_drive_id` **set** → upload to the **company library** (app-token,
  `app/services/datasheet_library.py: upload_datasheet_to_library`-style PUT). Row stores
  `library_drive_id = <drive id>`, `library_item_id`, `library_web_url`.
- `settings.datasheet_library_drive_id` **empty** → fall back to the **uploader's OneDrive**
  (user token via `scheduler.get_valid_token`, PUT to
  `/me/drive/root:/AvailAI/{Entity}/{entity_id}/{name}:/content`). Row stores
  `library_drive_id = NULL`, `library_item_id = <item id>`, `library_web_url = <webUrl>`.

**Discriminator:** `library_drive_id IS NULL` ⇒ OneDrive-fallback row; non-NULL ⇒ company-library row.
No extra enum column — the presence of a drive id is the source of truth, and it also future-proofs
mixed-era rows (old fallback rows stay readable after IT enables the drive).

**Serve/download:**
- company-library row → in-app byte stream via `datasheet_library.fetch_datasheet_bytes(drive_id, item_id)`
  (app token), `StreamingResponse` (mirrors `part_dossier.py:358`).
- OneDrive-fallback row → redirect to `library_web_url` (the existing behavior; the single
  user can open it in their OneDrive). Affordance label is identical ("Download").

**Honest failure (no silent fallbacks):**
- company library configured but Graph PUT fails → 502 "Couldn't save to the company library",
  logged with status+body. **Do NOT silently fall back to OneDrive on error** — fallback is a
  *config* decision (drive-id unset), never an *error*-handling decision.
- drive-id unset AND user has no Microsoft token → 401 "Connect your Microsoft account to
  attach files" (clear, actionable). Never a generic 500.
- oversize → 400 "File too large (max 10 MB)". Disallowed type → 400 with the allowed list.

### Data model

**Unified column shape** (all six attachment tables identical):

| column | type | notes |
|---|---|---|
| `id` | Integer PK | |
| `{entity}_id` | Integer FK CASCADE, indexed, NOT NULL | the owning entity |
| `file_name` | String(500) NOT NULL | sanitized (`/`,`\` → `_`) |
| `library_item_id` | String(500) | Graph driveItem id (company lib OR OneDrive) |
| `library_drive_id` | String(200) NULL | company-lib drive id; **NULL ⇒ OneDrive fallback** |
| `library_web_url` | Text | webUrl |
| `thumbnail_url` | Text | |
| `content_type` | String(100) | |
| `size_bytes` | Integer | |
| `uploaded_by_id` | Integer FK users SET NULL | |
| `created_at` | UTCDateTime | default now(UTC) |

**Existing 3 tables** (`requisition_attachments`, `requirement_attachments`, `offer_attachments`):
- RENAME `onedrive_item_id` → `library_item_id`, `onedrive_url` → `library_web_url`
  (precedent: migration `121_datasheet_lib_col_rename` did exactly this for datasheets).
- ADD `library_drive_id String(200) NULL`.

**New 3 tables** (`company_attachments`, `site_contact_attachments`, `material_card_attachments`):
- create with the full unified schema. FK + back-ref relationship on `Company` (`crm.py:14`),
  `SiteContact` (`crm.py:209`), `MaterialCard` (`intelligence.py:26`) named `attachments`.

**Migration 126** (`126_unified_attachments`, down_revision `125_enrichment_provenance`):
upgrade = 6 `alter_column` renames + 3 `add_column` + 3 `create_table` (+ indexes); downgrade =
exact reverse. MaterialCard's existing `datasheets` relationship is untouched — user attachments
are a separate concept from system-captured datasheets.

### Shared service — `app/services/attachment_service.py` (NEW)

Pure, model-agnostic. Signature sketch (final types in the plan):
- `async def store_and_attach(db, *, model, fk_field, entity_label, entity_id, file, user) -> Attachment`
  — validates (size + MIME), picks backend, PUTs, persists a row, returns it.
- `async def remove_attachment(db, att, user) -> dict` — best-effort cloud delete (app-token for
  company-lib rows, user-token for OneDrive rows), then DB delete; warns (never 500s) if cloud
  delete fails but DB row removed.
- `def serialize(a) -> dict` — `{id, file_name, web_url, content_type, size_bytes, uploaded_by, created_at, kind}`
  where `kind ∈ {"library","onedrive"}` from the discriminator.
- `async def open_attachment(att, user) -> StreamingResponse | RedirectResponse` — serve per kind.
- Validation constants moved to `app/constants.py`:
  `MAX_ATTACHMENT_BYTES = 10*1024*1024`, `ALLOWED_ATTACHMENT_EXTENSIONS` (superset of the current
  `ALLOWED_OFFER_EXTENSIONS`), applied to **all** entities (today only Offer validates type).

### Routers

- **Existing** (`requisitions/attachments.py`, `crm/offers.py`): rewrite the upload/link/delete
  bodies to call the shared service; **keep the existing route URLs unchanged** (back-compat).
  Add the in-app serve route.
- **New** (`app/routers/attachments_extra.py` or per-entity): for company / contact / material —
  `POST/GET/DELETE` list+upload+delete + `GET …/content` serve. Auth: `require_user` +
  resource-ownership (company: owner-or-shared; contact: via its site→company; material card:
  shared catalog, any buyer). Register in `app/main.py`.
- **Unified serve route:** `GET /api/attachments/{kind}/{att_id}/content`,
  `kind ∈ {requisition,requirement,offer,company,contact,material}` → resolves the row in the
  matching table and serves per the storage kind.

### Shared UI — `app/templates/htmx/partials/shared/_attachments.html` (NEW)

One Jinja macro `attachments_panel(kind, entity_id, items)` + an Alpine factory
`attachmentsPanel(cfg)` registered in `htmx_app.js`:
- File picker + drag-drop zone; multipart upload via `fetch` (or `htmx.ajax`) to the list URL;
  optimistic spinner; on success re-render the list partial.
- Row: type icon, filename (links to `…/content`), size (human), uploader, relative date, delete
  (kebab or trailing icon, confirm).
- Empty state: invitational ("Drop a file or browse — datasheets, POs, drawings, photos").
- Honest error surface: server `{"error": …}` → toast (reuse the corrected `body.error` reader).
- Alpine attribute quoting per CLAUDE.md (single-quoted attrs / `tojson`), `hx-target` explicit.

**Surface placement:**
1. **Company detail** — new **Files** tab. Add `files` to the `tabs` loop in
   `customers/detail.html:~210`; new tab partial `customers/tabs/files_tab.html`; new router
   branch in the company `…/tab/{tab_id}` view (`htmx_views.py`, the `company_tab_partial`).
2. **MaterialCard detail** — new **Files** tab in `materials/detail.html` (alongside Specs/FRU/…),
   kept distinct from the auto **Datasheets**.
3. **SiteContact** — paperclip + count on the scannable contact card (`_contact_macros.html`);
   click → expand/modal hosting the shared panel (respect the calm-card design just shipped).
4. **Requirement** — collapsible "Files (N)" section in the requisition **Parts** tab row.
5. **Offer** — replace the bespoke block in `shared/offer_card.html` with the shared panel.

## Testing

- **Service** (`tests/test_attachment_service.py`, NEW): company-lib path (drive-id set, mocked
  app-token PUT) stores `library_drive_id`; OneDrive fallback (drive-id empty, mocked user PUT)
  stores NULL drive-id; PUT-failure → 502 (no silent fallback); no-token+unset → 401; oversize →
  400; bad-extension → 400; serialize `kind`; delete best-effort warning path.
- **Migration** (`tests/test_migrations` style or model tests): 3 new tables exist with FK
  CASCADE; renamed columns present; downgrade restores `onedrive_*`.
- **Routers**: existing attachment tests updated to `library_*` columns and the shared service;
  new company/contact/material upload/list/delete/serve + ownership 403s; serve streams for
  library rows and redirects for OneDrive rows.
- **e2e/dead-ends**: the new list/serve partials return non-empty HTML or auth-redirect; the 5
  detail surfaces render the panel.
- Full suite green; `pre-commit run --all-files`.

## Out of scope

- Virus scanning (note as a follow-up; Graph/SharePoint does some).
- Versioning / multiple revisions of the same file.
- Bulk download / zip.
- Migrating the existing (near-empty) attachment rows' bytes between drives — fallback rows stay
  readable; no data move needed.

## Deploy & verify

- Build + merge to main; deploy folds in the already-landed CRM QA fixes (f57302dd).
- The feature is **live + verifiable today** on user-OneDrive fallback.
- When IT delivers `DATASHEET_LIBRARY_DRIVE_ID`, set the env var and restart — new uploads route
  to the shared company library automatically. Schedule a follow-up to live-verify that path then.
