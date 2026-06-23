# Plan — Unified File Attachments (feat/attachments-unified)

Spec: `docs/superpowers/specs/2026-06-23-file-attachments-unified.md` (read it first).
Worktree off `main` @ f57302dd. Migration **126** (claimed, chains `125_enrichment_provenance`).
TDD per task. Each task ends green (`TESTING=1 PYTHONPATH=<worktree> pytest <files>`).

## Global constraints (bind every task)

- **Storage discriminator:** `library_drive_id IS NULL` ⇒ user-OneDrive fallback row;
  non-NULL ⇒ company-library row. No extra enum column.
- **Backend choice is config, not error-handling:** pick company-library iff
  `settings.datasheet_library_drive_id` is truthy, else OneDrive. On a *configured* PUT failure,
  raise 502 — **never silently fall back to OneDrive on error.**
- **Honest errors only** (silent-failure rubric): unset-drive + no user token → 401
  "Connect your Microsoft account to attach files"; oversize → 400 "File too large (max 10 MB)";
  bad type → 400 with allowed list. Never a generic 500 that conflates not-saved with saved.
- **Existing route URLs stay unchanged** (`/api/requisitions/{id}/attachments`,
  `/api/offers/{id}/attachments`, etc.) — back-compat.
- 10 MB cap + `ALLOWED_ATTACHMENT_EXTENSIONS` (superset of current `ALLOWED_OFFER_EXTENSIONS`)
  apply to ALL five entities.
- JSON errors use `{"error": …}` (tests assert `["error"]`). `db.get(Model, id)`. Loguru.
  Alpine attr quoting per CLAUDE.md. New files get a header comment.
- After code change, update the relevant `docs/APP_MAP_*.md` (Task 6).

---

## Task 1 — Models + migration 126 (foundation)

**Models** (`app/models/`):
- `crm.py`: add `CompanyAttachment` (`company_attachments`, FK `companies.id` CASCADE) and
  `SiteContactAttachment` (`site_contact_attachments`, FK `site_contacts.id` CASCADE), unified
  schema (spec table). Add `attachments` relationship + `back_populates` on `Company` and
  `SiteContact`, `cascade="all, delete-orphan"`.
- `intelligence.py`: add `MaterialCardAttachment` (`material_card_attachments`, FK
  `material_cards.id` CASCADE); `attachments` relationship on `MaterialCard` (leave `datasheets`
  untouched).
- `offers.py` `OfferAttachment` + `sourcing.py` `RequisitionAttachment`/`RequirementAttachment`:
  rename `onedrive_item_id`→`library_item_id`, `onedrive_url`→`library_web_url`; add
  `library_drive_id = Column(String(200))`.
- Export the 3 new models from `app/models/__init__.py`.

**Migration** `alembic/versions/126_unified_attachments.py` (revision `126_unified_attachments`,
down_revision `125_enrichment_provenance`):
- upgrade: `op.alter_column` ×6 (rename on the 3 existing tables); `op.add_column` ×3
  (`library_drive_id`); `op.create_table` ×3 (with the FK + a `ix_*_{entity}` index on the FK).
- downgrade: drop the 3 tables, drop `library_drive_id` ×3, rename columns back to `onedrive_*`.
- Verify `alembic heads` = single head after.

**Tests** (`tests/test_unified_attachment_models.py`, NEW): each new model instantiates + FK
back-ref works; `library_drive_id` nullable; existing models expose `library_item_id`/
`library_web_url`. (Migration up/down is exercised by the suite's schema build.)

**Acceptance:** models import; `alembic upgrade head` then `downgrade -1` then `upgrade head`
clean; single head; new+renamed columns present; targeted tests green.

---

## Task 2 — Shared attachment service + constants

**Constants** (`app/constants.py`): add `MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024` and
`ALLOWED_ATTACHMENT_EXTENSIONS: frozenset[str]` (take the current `ALLOWED_OFFER_EXTENSIONS`
set from `crm/offers.py:38` as the superset). `crm/offers.py` imports the shared one (Task 3).

**Service** `app/services/attachment_service.py` (NEW). Async, model-agnostic:
- `_validate(file, content) -> None` — size + extension; raises `HTTPException` with `{"error":…}`
  shape via the app's existing error convention.
- `async def _store(content, *, content_type, file_name, entity_label, entity_id, user, db)
   -> tuple[item_id, drive_id|None, web_url]` — company-library branch
  (`datasheet_library.upload_datasheet_to_library`-style PUT, app token, returns drive_id set) vs
  OneDrive branch (`scheduler.get_valid_token`, PUT
  `/me/drive/root:/AvailAI/{entity_label}/{entity_id}/{safe}:/content`, drive_id=None). Raise
  401/502 per the honest-error rules. `safe` = name with `/`,`\`→`_`.
- `async def store_and_attach(db, *, model, fk_field, entity_label, entity_id, file, user)
   -> object` — validate → `_store` → build `model(**{fk_field: entity_id}, file_name=…,
   library_item_id=…, library_drive_id=…, library_web_url=…, content_type=…, size_bytes=…,
   uploaded_by_id=user.id)` → commit → return.
- `def serialize(a) -> dict` — `{id, file_name, web_url: a.library_web_url, content_type,
   size_bytes, uploaded_by: a.uploaded_by.name if a.uploaded_by else None, created_at iso,
   kind: "library" if a.library_drive_id else "onedrive"}`.
- `async def open_attachment(att, user) -> Response` — library → `fetch_datasheet_bytes` →
  `StreamingResponse(BytesIO, media_type=content_type, headers Content-Disposition)`; onedrive →
  `RedirectResponse(att.library_web_url)`. 404 if bytes None.
- `async def remove_attachment(db, att, user) -> dict` — best-effort cloud delete (app-token
  DELETE `/drives/{drive_id}/items/{item_id}` for library; user-token DELETE
  `/me/drive/items/{item_id}` for onedrive), then `db.delete`; on cloud-delete failure keep DB
  delete + return `{"ok": True, "warning": …}` (mirror existing requisition delete semantics).

**Tests** (`tests/test_attachment_service.py`, NEW, mock `http`/graph + `get_valid_token` +
`datasheet_library`): drive-id-set → stores `library_drive_id`; drive-id-empty → NULL + OneDrive
PUT called; configured PUT 500 → service raises 502 (and did NOT call OneDrive); unset+no-token →
401; oversize → 400; bad ext → 400; `serialize` kind both ways; `open_attachment` streams vs
redirects; delete warning path.

**Acceptance:** service unit tests green; no router wiring yet.

---

## Task 3 — Route existing attachments through the service + unified serve route

- `app/routers/requisitions/attachments.py` + `app/routers/crm/offers.py`: replace the inline
  PUT/serialize/delete bodies in the requisition/requirement/offer upload+link+delete endpoints
  with calls to `attachment_service` (`store_and_attach`, `serialize`, `remove_attachment`).
  Keep the **route paths and JSON response shapes** stable. `crm/offers.py` imports
  `ALLOWED_ATTACHMENT_EXTENSIONS`/`MAX_ATTACHMENT_BYTES` from constants (delete its local copies).
- `offer_card.html` references to `onedrive_url` → `library_web_url` (Task 5 replaces the block,
  but keep it rendering until then).
- **New unified serve route** in `app/routers/attachments_extra.py` (NEW):
  `GET /api/attachments/{kind}/{att_id}/content`, `kind ∈
  {requisition,requirement,offer,company,contact,material}` → `db.get` the row in the matching
  table (a `kind→model` map), `require_user`, then `await open_attachment`. 404 unknown kind/row.
- Register `attachments_extra` router in `app/main.py`.

**Tests:** update `tests/test_attachments.py`, `test_routers_attachments.py`,
`test_attachments_router_coverage.py`, `test_attachments_coverage2.py`, `test_routers_crm.py`
to `library_*` columns + the service. New `tests/test_attachments_serve.py`: serve route streams
for a library row (mocked) and redirects for an onedrive row; 404 unknown kind.

**Acceptance:** all existing + new attachment tests green; existing API URLs unchanged.

---

## Task 4 — Company / Contact / MaterialCard endpoints

In `app/routers/attachments_extra.py` add list+upload+delete for the 3 new entities:
- `GET/POST /api/companies/{company_id}/attachments`,
  `DELETE /api/company-attachments/{att_id}`
- `GET/POST /api/contacts/{contact_id}/attachments`,
  `DELETE /api/contact-attachments/{att_id}`
- `GET/POST /api/material-cards/{card_id}/attachments`,
  `DELETE /api/material-card-attachments/{att_id}`
- Each upload → `store_and_attach(model=…, fk_field=…, entity_label="Companies"|"Contacts"|
  "Materials", entity_id=…)`. List returns `[serialize(a) …]` ordered `created_at desc`.
- **Auth/ownership** (`require_user` +): Company → owner-or-shared (match how company detail
  gates today — reuse the company-access helper used by `company_detail_partial`); Contact →
  resolve `SiteContact → CustomerSite → Company`, same company access; MaterialCard → shared
  catalog, `require_user` is enough (note in code comment). Return 404 (not 403) when the parent
  isn't visible, to avoid existence leaks — match the existing requisition pattern.

**Tests** (`tests/test_attachments_extra.py`, NEW): upload creates a row per entity (mocked
storage); list returns it; delete removes it; ownership 404 for a company the user can't see;
material-card upload works for any logged-in user.

**Acceptance:** new endpoints green; routers registered.

---

## Task 5 — Shared UI component + wire all 5 surfaces (frontend-design)

- `app/templates/htmx/partials/shared/_attachments.html` (NEW): Jinja macro
  `attachments_panel(kind, entity_id, items)` + register `attachmentsPanel(cfg)` Alpine factory in
  `app/static/htmx_app.js`. Drag-drop + picker, multipart upload to the list URL, re-render list on
  success, row (icon/name→`…/content`/size/uploader/date/delete-with-confirm), invitational empty
  state, `{"error":…}`→toast. Single-quoted Alpine attrs; explicit `hx-target`.
- A small list-partial endpoint per kind (or reuse the list JSON + client render). Prefer a
  server-rendered list partial `shared/_attachment_list.html` returned by the `GET …/attachments`
  endpoints when `HX-Request` present, JSON otherwise — keep it HTMX-native.
- **Wire:**
  1. Company **Files** tab — add `files` to the `tabs` loop in `customers/detail.html`; new
     `customers/tabs/files_tab.html`; branch in the company `…/tab/{tab_id}` view.
  2. MaterialCard **Files** tab — `materials/detail.html` (distinct from Datasheets).
  3. SiteContact — paperclip+count on the contact card (`_contact_macros.html`) → expand/modal
     hosting the panel; keep the calm-card scan.
  4. Requirement — collapsible "Files (N)" in the requisition Parts tab row.
  5. Offer — replace the bespoke attachment block in `shared/offer_card.html` with the macro.
- Run **frontend-design** for the panel (one signature component, restrained). Tailwind classes
  used must build (deploy gate). No `text-gray-500`/`text-[10px]` (static-analysis ratchet).

**Tests:** `e2e/dead-ends.spec.ts` (or the existing partial sweep) covers the 5 panels render +
the list endpoints return non-empty/auth-redirect. A Jinja render test that the macro emits the
upload form for each kind.

**Acceptance:** all 5 surfaces show the identical panel; `npm run build` clean; suite green.

---

## Task 6 — Docs + final sweep

- Update `docs/APP_MAP_DATABASE.md` (3 new tables + the renames), `docs/APP_MAP_INTERACTIONS.md`
  (attachment-service storage abstraction + fallback), and note the feature in the architecture
  map if it lists routers.
- `pre-commit run --all-files` twice if docformatter rewraps. Full suite green.

---

## Build order

T1 → T2 → T3 → T4 → T5 → T6 (sequential; T3/T4 both depend on T2 but touch overlapping router
files, so do not parallelize). Final whole-branch review (most-capable model) before finishing.
