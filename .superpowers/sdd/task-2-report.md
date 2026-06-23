# Task 2 Report — Shared Attachment Service + Constants

**Date:** 2026-06-23
**Branch:** feat/attachments-unified
**Status:** DONE — 16/16 tests green, pre-commit clean

---

## Files changed

### `app/constants.py` (modified)
Added two constants at module level (after the header docstring, before the StrEnums):

```python
MAX_ATTACHMENT_BYTES: int = 10 * 1024 * 1024  # 10 MB

ALLOWED_ATTACHMENT_EXTENSIONS: frozenset[str] = frozenset({
    ".pdf", ".xlsx", ".xls", ".csv", ".doc", ".docx",
    ".png", ".jpg", ".jpeg", ".txt", ".zip",
})
```

`ALLOWED_ATTACHMENT_EXTENSIONS` is verbatim from `crm/offers.py:ALLOWED_OFFER_EXTENSIONS` (Task 3 will swap offers.py to import the shared constant).

---

### `app/services/attachment_service.py` (new)

Module-level imports: `os`, `BytesIO`, `fastapi.HTTPException/UploadFile`, `fastapi.responses.{RedirectResponse,StreamingResponse}`, `loguru.logger`, `sqlalchemy.orm.Session`, `..config.settings`, `..constants.{ALLOWED_ATTACHMENT_EXTENSIONS,MAX_ATTACHMENT_BYTES}`. All heavy imports (`graph_app_auth`, `http_client`, `scheduler`, `datasheet_library`) are lazy (inside functions) to avoid circular imports and to enable precise mocking at source.

#### Private helpers

**`_safe_name(name: str) -> str`**
Sanitizes a filename for Graph path segments: replaces `/` and `\` with `_`.

**`_validate(file: UploadFile, content: bytes) -> None`**
- Raises `HTTPException(400, "File too large (max 10 MB)")` if `len(content) > MAX_ATTACHMENT_BYTES`.
- Raises `HTTPException(400, "File type '...' not allowed. Accepted: ...")` if `os.path.splitext(file.filename)[1].lower()` not in `ALLOWED_ATTACHMENT_EXTENSIONS`.

**`async def _store(content, *, content_type, file_name, entity_label, entity_id, user, db) -> tuple[str|None, str|None, str|None]`**

Returns `(item_id, drive_id, web_url)`.

Backend-selection logic:

```
if settings.datasheet_library_drive_id:          # company-library branch
    token = await get_app_graph_token()           # lazy import from graph_app_auth
    PUT /drives/{drive_id}/root:/Attachments/{entity_label}/{entity_id}/{safe_name}:/content
    on failure → HTTPException(502, "Couldn't save to the company library")
    returns (item_id, drive_id, web_url)          # drive_id is non-NULL
else:                                             # OneDrive fallback branch
    token = await get_valid_token(user, db)       # lazy import from scheduler
    if not token → HTTPException(401, "Connect your Microsoft account to attach files")
    PUT /me/drive/root:/AvailAI/{entity_label}/{entity_id}/{safe_name}:/content
    401/403/other → appropriate HTTPException
    returns (item_id, None, web_url)              # drive_id IS NULL
```

Critical: a configured library PUT failure raises 502 immediately — no fallback to OneDrive. Fallback is a config decision, not an error-handling decision.

#### Public API

**`async def store_and_attach(db, *, model, fk_field, entity_label, entity_id, file, user) -> object`**

Signature matches the plan verbatim. Sequence: read file bytes → `_validate` → `_store` → build `model(**{fk_field: entity_id}, file_name=safe, library_item_id=..., library_drive_id=..., library_web_url=..., content_type=..., size_bytes=..., uploaded_by_id=user.id)` → `db.add` → `db.commit` → `db.refresh` → return.

**`def serialize(a) -> dict`**

Returns `{id, file_name, web_url (=library_web_url), content_type, size_bytes, uploaded_by (name or None), created_at (ISO), kind ("library" if library_drive_id else "onedrive")}`.

**`async def open_attachment(att, user) -> StreamingResponse | RedirectResponse`**

- Library row (`att.library_drive_id` set): lazy-imports `fetch_datasheet_bytes` from `app.services.datasheet_library`; returns `StreamingResponse(BytesIO(data), ...)` with `Content-Disposition: inline`; raises `HTTPException(404)` if bytes are None.
- OneDrive row: returns `RedirectResponse(att.library_web_url)`; raises `HTTPException(404)` if no URL.

**`async def remove_attachment(db, att, user) -> dict`**

Best-effort cloud delete per kind, then `db.delete` regardless:

- Library row: lazy-import `get_app_graph_token`, DELETE `/drives/{drive_id}/items/{item_id}`; on any failure (exception or non-200/204 status) → `warning` key added to result.
- OneDrive row: lazy-import `get_valid_token`, DELETE `/me/drive/items/{item_id}`; same warning logic.
- Always deletes the DB row. Returns `{"ok": True}` on clean success, `{"ok": True, "warning": "..."}` on cloud failure.

---

## Test file: `tests/test_attachment_service.py`

16 tests covering all spec-required paths:

| Test | What it checks |
|---|---|
| `test_store_library_path` | drive_id set → row has non-NULL library_drive_id |
| `test_store_library_calls_library_not_onedrive` | only library PUT called; OneDrive token never requested |
| `test_store_onedrive_path` | drive_id empty → row has NULL library_drive_id |
| `test_store_onedrive_calls_onedrive_not_library` | only OneDrive PUT called |
| `test_library_put_failure_raises_502` | configured PUT 500 → 502 raised; OneDrive not called |
| `test_no_drive_id_and_no_user_token_raises_401` | unset + no token → 401 with "Connect your Microsoft account" |
| `test_oversize_raises_400` | content > 10 MB → 400 |
| `test_bad_extension_raises_400` | .exe → 400 with allowed list |
| `test_serialize_kind_library` | library_drive_id set → kind="library" |
| `test_serialize_kind_onedrive` | library_drive_id NULL → kind="onedrive" |
| `test_open_attachment_library_streams` | StreamingResponse for library row |
| `test_open_attachment_onedrive_redirects` | RedirectResponse for OneDrive row |
| `test_open_attachment_library_404_when_bytes_none` | 404 when fetch returns None |
| `test_remove_attachment_warning_on_cloud_delete_failure` | warning + DB deleted when library DELETE fails |
| `test_remove_attachment_onedrive_warning_on_failure` | warning + DB deleted when OneDrive DELETE fails |
| `test_remove_attachment_clean_no_warning` | clean dict when cloud DELETE succeeds |

### What was mocked

Per CLAUDE.md "mock lazy imports at the source module":
- `app.services.graph_app_auth.get_app_graph_token` (AsyncMock)
- `app.scheduler.get_valid_token` (AsyncMock)
- `app.http_client.http` (MagicMock with `.put` / `.delete` = `AsyncMock(return_value=MagicMock(...))`)
- `app.services.datasheet_library.fetch_datasheet_bytes` (AsyncMock)
- `app.services.attachment_service.settings` (MagicMock with `datasheet_library_drive_id` set per test)
- `app.services.attachment_service._store` (patched as async mock in `store_and_attach` integration tests)

Key: all `AsyncMock` return values use explicit `MagicMock(status_code=..., json=...)` for the HTTP response to avoid the AsyncMock coroutine-attribute issue where `.json()` returns a coroutine when the mock parent is async.

### Test command

```
cd /root/availai/.claude/worktrees/attachments-unified && \
  TESTING=1 PYTHONPATH=/root/availai/.claude/worktrees/attachments-unified \
  pytest tests/test_attachment_service.py -v --override-ini="addopts="
```

Result: **16 passed** in 1.95s.

### pre-commit

```
pre-commit run --files app/constants.py app/services/attachment_service.py tests/test_attachment_service.py
```

Run twice (docformatter rewraps on first pass). Both runs clean. mypy: 0 errors in 441 source files.
