# Datasheet Company Library — Design Spec

**Date:** 2026-06-18
**Status:** Approved (brainstorm) — pending spec review → implementation plan
**Builds on:** the auto-datasheet-capture feature (PR #352) — see
`2026-06-17-auto-datasheet-capture-design.md`.

## Problem

Auto-datasheet capture currently stores each datasheet in the **triggering user's
personal OneDrive** (`/me/drive`, delegated token). A datasheet is an **org-wide asset**
that should accumulate into a single durable, shareable library — not be scattered across
individuals' personal drives (orphaned if a user leaves / token lapses) and not require a
logged-in user for an unattended background capture.

## Goal

Store captured datasheets in one **company-wide SharePoint document library**, written by
the capture job **as the app itself** (app-only Graph token) so storage is independent of
any user. Serve them back through an **in-app streaming download** (clean PDF, no
SharePoint web-viewer/login). The library grows over time as a browsable repository.

## Non-Goals (YAGNI)

- Migrating the (currently zero) personal-OneDrive copies — none exist yet (feature is
  live but pre-go-live; no real captures). Switch the target forward; nothing to move.
- Per-user access control on the library (it's a shared company asset; app-only).
- A SharePoint browse UI inside the app (the dossier link + the library itself suffice).

## Architecture

### 1. App-only Graph auth (new) — `app/services/graph_app_auth.py`
- `async def get_app_graph_token() -> str | None`: client-credentials grant against
  `https://login.microsoftonline.com/{azure_tenant_id}/oauth2/v2.0/token` with
  `client_id=azure_client_id`, `client_secret=azure_client_secret`,
  `grant_type=client_credentials`, `scope=https://graph.microsoft.com/.default`.
  In-memory cache with expiry (refresh ~5 min before `expires_in`). Returns `None` if
  azure creds are unset or the token call fails (callers degrade gracefully). Uses the
  shared `app.http_client.http`.
- This is **app-only**, distinct from the existing delegated `token_manager.get_valid_token`
  (which stays for OneDrive/Mail/etc.). Requires the Azure app registration to hold the
  **`Sites.Selected`** application permission with admin consent, scoped to the datasheet
  library's site (least-privilege — the app can write ONLY that library).

### 2. Company library target (config)
- New settings (env): `datasheet_library_drive_id: str = ""` — the Graph **drive id** of
  the SharePoint "Datasheets" document library. (Primary addressing; the admin obtains it
  once.) Optional `datasheet_library_subpath: str = "Datasheets"` root folder name.
- When `datasheet_library_drive_id` is unset → the capture job **skips storage** (logs a
  warning, stamps `datasheet_searched_at` like the no-token path today). The feature is
  inert-but-safe until configured. This mirrors how the app gracefully handles missing
  connector credentials.

### 3. Library upload/download helper (new) — `app/services/datasheet_library.py`
Replaces the per-user OneDrive path for datasheets (the generic
`onedrive_files.upload_bytes_to_onedrive` stays for req/offer attachments — unchanged):
- `async def upload_datasheet_to_library(file_name, content, content_type, *, manufacturer="") -> dict | None`
  — get app token; if no token or no `datasheet_library_drive_id` → `None`. Folder:
  `{subpath}/{manufacturer-or-_unknown}/{safe_file_name}`. `PUT /drives/{drive_id}/root:/{path}:/content`.
  Returns `{"onedrive_item_id", "onedrive_url", "size_bytes", "library_drive_id"}` (keys
  kept compatible with the existing `MaterialCardDatasheet` columns) or `None`.
- `async def fetch_datasheet_bytes(drive_id, item_id) -> bytes | None` — app token;
  `GET /drives/{drive_id}/items/{item_id}/content`; returns bytes or `None`.
- Filename sanitization (`/`,`\` → `_`); manufacturer folder sanitized likewise.

### 4. Capture orchestrator change — `app/services/datasheet_capture.py`
- `capture_datasheet` no longer needs the user for storage. Storage path becomes
  `upload_datasheet_to_library(file_name, pdf, "application/pdf", manufacturer=card.manufacturer)`.
  The `user_id` parameter is **retained** (still used to attribute who triggered the
  capture, written to `MaterialCardDatasheet.uploaded_by_id` when available — nullable;
  unattended captures write `NULL`). No delegated token needed for storage.
- The `MaterialCardDatasheet` row records `library_drive_id` (new column) + the returned
  `onedrive_item_id`/`onedrive_url`.
- Trigger call sites are unchanged (still pass `user_id`); they no longer need a valid
  M365 token for capture to store.

### 5. In-app download endpoint (new) — `app/routers/part_dossier.py`
- `GET /v2/partials/search/dossier/datasheet/{datasheet_id}/download` (auth: `require_user`)
  → load the `MaterialCardDatasheet`; `fetch_datasheet_bytes(row.library_drive_id, row.onedrive_item_id)`;
  return `StreamingResponse(application/pdf, headers Content-Disposition: inline; filename="<file_name>")`.
  404 if the row/library item is missing; 502 on a Graph fetch failure. Works for any
  authenticated app user (the app holds the token), no SharePoint login.
- `dossier_datasheet_block.html`: the saved-state link points at this endpoint (replacing
  the direct `onedrive_url` webUrl link).

### 6. Schema change
- One migration: `ALTER TABLE material_card_datasheets ADD COLUMN library_drive_id varchar(200) NULL`.
  (Claim the next free number via `MIGRATION_NUMBERS_IN_FLIGHT.txt`; chain onto the current
  head. Revision id ≤ 32 chars; `if_not_exists` where applicable.)
- `MaterialCardDatasheet.library_drive_id` (String(200), nullable) + ORM field.
- No back-migration of data (zero existing rows).

### 7. Hardening (bundled)
- Keep `_is_safe_url` a sync helper, but call it from `download_pdf` via
  `await asyncio.to_thread(_is_safe_url, url)` so its blocking `socket.getaddrinfo` runs off
  the event loop (per-hop, before each fetch).
- Add `upload_*` tests for the non-2xx and exception-during-PUT paths (now on the library
  upload helper).

## Error Handling
- No app token / no library configured → skip storage, stamp `datasheet_searched_at`
  (cooldown), never raise. Capture remains best-effort and never breaks search/RFQ.
- Library upload non-2xx / exception → `None` → skip + stamp (cooldown retry).
- Download endpoint: missing row → 404; Graph fetch failure → 502; never leak the app token.

## Testing
- **app auth:** token acquired + cached (mock token endpoint); `None` when azure creds unset.
- **library upload:** correct drive/path, returns the 4-key dict on 201; `None` when
  drive_id unset / non-2xx / exception (the two hardening cases).
- **capture:** stores via the library helper (mock) with `library_drive_id` recorded +
  `uploaded_by_id` from the triggering user (or NULL); skips + stamps when the library is
  unconfigured; web-hit MPN verification unchanged.
- **download endpoint:** streams bytes with `application/pdf` (mock `fetch_datasheet_bytes`);
  404 on missing row.
- **SSRF hardening:** `getaddrinfo` wrapped (existing SSRF tests still pass; the
  blocks-unsafe / per-hop tests unchanged).
- **migration:** column present, single head, in-flight guard passes.
- **Live-verify** after go-live config: drive a real capture and confirm the copy lands in
  the company library + the in-app download streams it.

## Setup (operator / Azure-admin — one time, flagged)
1. Create a **"Datasheets" document library** in a chosen SharePoint site.
2. Grant the Azure app **`Sites.Selected`** *write* on that site (admin consent).
3. Obtain the library's Graph **drive id**; set `DATASHEET_LIBRARY_DRIVE_ID` in env.
Until done, capture runs but skips storage (graceful). This is operator-owned (like API
keys); see [[project_broker_apis_golive_2026_06_17]] for the credentials-are-user-owned pattern.

## Dependencies / Notes
- Reuses `azure_client_id/secret/tenant_id` (already in config) for the app-only token.
- Update the relevant `docs/APP_MAP_*.md` after implementation.
- Coexists with the existing delegated OneDrive attachment path (req/offer) — that is
  unchanged; only datasheets move to the company library.
