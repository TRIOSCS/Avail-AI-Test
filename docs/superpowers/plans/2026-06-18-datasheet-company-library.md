# Datasheet Company Library Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Store captured datasheets in a company-wide SharePoint document library written via an app-only Graph token (independent of any user), and serve them through an in-app streaming download.

**Architecture:** Add an app-only Graph token service (client-credentials), a datasheet-library upload/download helper, swap `capture_datasheet`'s storage from per-user OneDrive to the library, add an in-app `StreamingResponse` download endpoint, and bundle two hardening fixes. Everything degrades gracefully (skips storage) until `DATASHEET_LIBRARY_DRIVE_ID` is configured.

**Tech Stack:** FastAPI, SQLAlchemy 2.0, Alembic, httpx (`app.http_client.http`/`http_redirect`), Microsoft Graph (app-only client-credentials), pydantic-settings.

## Global Constraints

- App-only Graph token = client-credentials against `https://login.microsoftonline.com/{azure_tenant_id}/oauth2/v2.0/token`, body `client_id/client_secret/grant_type=client_credentials/scope=https://graph.microsoft.com/.default`. This is SEPARATE from the delegated `token_manager.get_valid_token` (unchanged).
- Config via pydantic `Settings` (`app/config.py`): `datasheet_library_drive_id: str = ""`, `datasheet_library_subpath: str = "Datasheets"` (env `DATASHEET_LIBRARY_DRIVE_ID`, `DATASHEET_LIBRARY_SUBPATH`).
- When `settings.datasheet_library_drive_id` is empty OR no app token → storage is SKIPPED gracefully (log + return None); capture stamps `datasheet_searched_at` (cooldown) and never raises.
- `MaterialCardDatasheet.onedrive_item_id`/`onedrive_url` keys are reused (now point at the library); add `library_drive_id`.
- The existing per-user `onedrive_files.upload_bytes_to_onedrive` (req/offer attachments) is UNCHANGED — datasheets stop using it.
- Datetime cols use `UTCDateTime`. New models imported in `app/models/__init__.py` with `# noqa: F401` (MaterialCardDatasheet already exported — only a column is added).
- Alembic: claim the next free number in `MIGRATION_NUMBERS_IN_FLIGHT.txt`; chain `down_revision` onto the CURRENT head (confirm via `alembic heads` at build — currently `115_subscription_health`, next free number `116`); revision id ≤ 32 chars.
- AI/web + network calls already gated under TESTING in `capture_datasheet`/finder — unchanged.
- After implementation update `docs/APP_MAP_INTERACTIONS.md`.

---

### Task 1: App-only Graph token service + config

**Files:**
- Modify: `app/config.py` (add two settings near `azure_tenant_id`, ~line 68)
- Create: `app/services/graph_app_auth.py`
- Test: `tests/test_graph_app_auth.py`

**Interfaces:**
- Produces: `async def get_app_graph_token() -> str | None` (cached app-only Graph token; `None` if azure creds unset or token call fails). `settings.datasheet_library_drive_id`, `settings.datasheet_library_subpath`.

- [ ] **Step 1: Add config**

In `app/config.py`, after `azure_tenant_id: str = ""` (line 68) add:
```python
    # Company-wide SharePoint datasheet library (app-only Graph). Empty = storage skipped.
    datasheet_library_drive_id: str = ""
    datasheet_library_subpath: str = "Datasheets"
```

- [ ] **Step 2: Write the failing test**

```python
# tests/test_graph_app_auth.py
import os
os.environ["TESTING"] = "1"
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from app.services import graph_app_auth as gaa


@pytest.fixture(autouse=True)
def _reset_cache():
    gaa._TOKEN_CACHE.clear()
    yield
    gaa._TOKEN_CACHE.clear()


async def test_returns_none_without_creds():
    with patch.object(gaa.settings, "azure_client_id", ""), patch.object(gaa.settings, "azure_tenant_id", ""):
        assert await gaa.get_app_graph_token() is None


async def test_acquires_and_caches_token():
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"access_token": "APPTOK", "expires_in": 3600}
    with (
        patch.object(gaa.settings, "azure_client_id", "cid"),
        patch.object(gaa.settings, "azure_client_secret", "sec"),
        patch.object(gaa.settings, "azure_tenant_id", "tid"),
        patch("app.services.graph_app_auth.http") as http,
    ):
        http.post = AsyncMock(return_value=resp)
        t1 = await gaa.get_app_graph_token()
        t2 = await gaa.get_app_graph_token()
    assert t1 == "APPTOK" and t2 == "APPTOK"
    assert http.post.call_count == 1  # second call served from cache
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python3 -m pytest tests/test_graph_app_auth.py -q`
Expected: FAIL — module not found.

- [ ] **Step 4: Implement**

Create `app/services/graph_app_auth.py`:
```python
"""graph_app_auth.py — app-only (client-credentials) Microsoft Graph token.

For org-wide, user-independent Graph writes (the datasheet company library). Distinct
from the delegated per-user token in utils.token_manager. Requires the Azure app to hold
the Sites.Selected application permission (admin-consented), scoped to the library's site.
"""

from __future__ import annotations

import time

from loguru import logger

from ..config import settings
from ..http_client import http

# {"token": str, "expires_at": float}
_TOKEN_CACHE: dict[str, object] = {}


async def get_app_graph_token() -> str | None:
    """Return a cached app-only Graph token, or None if unavailable."""
    now = time.monotonic()
    cached = _TOKEN_CACHE.get("token")
    if cached and now < float(_TOKEN_CACHE.get("expires_at", 0)) - 300:
        return str(cached)
    if not (settings.azure_client_id and settings.azure_client_secret and settings.azure_tenant_id):
        return None
    url = f"https://login.microsoftonline.com/{settings.azure_tenant_id}/oauth2/v2.0/token"
    try:
        r = await http.post(
            url,
            data={
                "client_id": settings.azure_client_id,
                "client_secret": settings.azure_client_secret,
                "grant_type": "client_credentials",
                "scope": "https://graph.microsoft.com/.default",
            },
            timeout=15,
        )
    except Exception:
        logger.warning("app-only Graph token request errored", exc_info=True)
        return None
    if r.status_code != 200:
        logger.warning("app-only Graph token failed: {} {}", r.status_code, r.text[:200])
        return None
    body = r.json()
    token = body.get("access_token")
    if not token:
        return None
    _TOKEN_CACHE["token"] = token
    _TOKEN_CACHE["expires_at"] = now + int(body.get("expires_in", 3600))
    return token
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python3 -m pytest tests/test_graph_app_auth.py -q`
Expected: PASS (2 passed).

- [ ] **Step 6: Commit**

```bash
git add app/config.py app/services/graph_app_auth.py tests/test_graph_app_auth.py
git commit -m "feat(datasheet): app-only Graph token service + library config"
```

---

### Task 2: Datasheet library upload/download helper

**Files:**
- Create: `app/services/datasheet_library.py`
- Test: `tests/test_datasheet_library.py`

**Interfaces:**
- Consumes: Task 1 `get_app_graph_token`, `settings`, `app.http_client.http`/`http_redirect`.
- Produces:
  - `async def upload_datasheet_to_library(file_name: str, content: bytes, content_type: str, *, manufacturer: str = "") -> dict | None` → `{"onedrive_item_id","onedrive_url","size_bytes","library_drive_id"}` or `None`.
  - `async def fetch_datasheet_bytes(drive_id: str, item_id: str) -> bytes | None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_datasheet_library.py
import os
os.environ["TESTING"] = "1"
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from app.services import datasheet_library as dl


async def test_upload_returns_none_when_unconfigured():
    with patch.object(dl.settings, "datasheet_library_drive_id", ""):
        assert await dl.upload_datasheet_to_library("x.pdf", b"%PDF", "application/pdf") is None


async def test_upload_returns_metadata_on_success():
    resp = MagicMock(status_code=201)
    resp.json.return_value = {"id": "ITM", "webUrl": "https://sp/x"}
    with (
        patch.object(dl.settings, "datasheet_library_drive_id", "DRV"),
        patch.object(dl.settings, "datasheet_library_subpath", "Datasheets"),
        patch("app.services.datasheet_library.get_app_graph_token", AsyncMock(return_value="T")),
        patch("app.services.datasheet_library.http") as http,
    ):
        http.put = AsyncMock(return_value=resp)
        out = await dl.upload_datasheet_to_library("LM317-datasheet.pdf", b"%PDF-1.4", "application/pdf", manufacturer="TI")
    assert out == {"onedrive_item_id": "ITM", "onedrive_url": "https://sp/x", "size_bytes": 8, "library_drive_id": "DRV"}
    # path used the manufacturer folder under the configured subpath
    called_url = http.put.call_args[0][0]
    assert "/drives/DRV/root:/Datasheets/TI/LM317-datasheet.pdf:/content" in called_url


async def test_upload_none_on_non_2xx():
    resp = MagicMock(status_code=500); resp.text = "err"
    with (
        patch.object(dl.settings, "datasheet_library_drive_id", "DRV"),
        patch("app.services.datasheet_library.get_app_graph_token", AsyncMock(return_value="T")),
        patch("app.services.datasheet_library.http") as http,
    ):
        http.put = AsyncMock(return_value=resp)
        assert await dl.upload_datasheet_to_library("x.pdf", b"x", "application/pdf") is None


async def test_upload_none_on_exception():
    with (
        patch.object(dl.settings, "datasheet_library_drive_id", "DRV"),
        patch("app.services.datasheet_library.get_app_graph_token", AsyncMock(return_value="T")),
        patch("app.services.datasheet_library.http") as http,
    ):
        http.put = AsyncMock(side_effect=RuntimeError("boom"))
        assert await dl.upload_datasheet_to_library("x.pdf", b"x", "application/pdf") is None


async def test_fetch_bytes_ok():
    resp = MagicMock(status_code=200, content=b"%PDF-bytes")
    with (
        patch("app.services.datasheet_library.get_app_graph_token", AsyncMock(return_value="T")),
        patch("app.services.datasheet_library.http_redirect") as httpr,
    ):
        httpr.get = AsyncMock(return_value=resp)
        assert await dl.fetch_datasheet_bytes("DRV", "ITM") == b"%PDF-bytes"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_datasheet_library.py -q`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

Create `app/services/datasheet_library.py`:
```python
"""datasheet_library.py — store/fetch datasheets in the company SharePoint library.

App-only Graph (get_app_graph_token) against the configured library drive
(settings.datasheet_library_drive_id). Unconfigured or no-token => returns None so the
caller skips storage gracefully. Separate from onedrive_files (per-user req/offer attachments).
"""

from __future__ import annotations

import re

from loguru import logger

from ..config import settings
from ..http_client import http, http_redirect
from .graph_app_auth import get_app_graph_token

_GRAPH = "https://graph.microsoft.com/v1.0"


def _sanitize(part: str) -> str:
    return re.sub(r"[\\/]+", "_", (part or "").strip()) or "_unknown"


async def upload_datasheet_to_library(
    file_name: str, content: bytes, content_type: str, *, manufacturer: str = ""
) -> dict | None:
    """PUT the bytes into the company library; return metadata dict or None."""
    drive_id = settings.datasheet_library_drive_id
    if not drive_id:
        logger.info("datasheet library not configured — skipping storage")
        return None
    token = await get_app_graph_token()
    if not token:
        logger.warning("no app Graph token — skipping datasheet storage")
        return None
    folder = f"{settings.datasheet_library_subpath}/{_sanitize(manufacturer)}"
    safe_name = _sanitize(file_name)
    url = f"{_GRAPH}/drives/{drive_id}/root:/{folder}/{safe_name}:/content"
    try:
        r = await http.put(
            url,
            content=content,
            headers={"Authorization": f"Bearer {token}", "Content-Type": content_type or "application/octet-stream"},
            timeout=60,
        )
    except Exception:
        logger.warning("datasheet library upload errored url={}", url, exc_info=True)
        return None
    if r.status_code not in (200, 201):
        logger.warning("datasheet library upload failed {} {}", r.status_code, r.text[:200])
        return None
    body = r.json()
    return {
        "onedrive_item_id": body.get("id"),
        "onedrive_url": body.get("webUrl"),
        "size_bytes": len(content),
        "library_drive_id": drive_id,
    }


async def fetch_datasheet_bytes(drive_id: str, item_id: str) -> bytes | None:
    """GET the item content from the library (app-only). Returns bytes or None."""
    if not (drive_id and item_id):
        return None
    token = await get_app_graph_token()
    if not token:
        return None
    url = f"{_GRAPH}/drives/{drive_id}/items/{item_id}/content"
    try:
        r = await http_redirect.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=60)
    except Exception:
        logger.warning("datasheet library fetch errored item={}", item_id, exc_info=True)
        return None
    if r.status_code != 200 or not r.content:
        return None
    return r.content
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_datasheet_library.py -q`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add app/services/datasheet_library.py tests/test_datasheet_library.py
git commit -m "feat(datasheet): company-library upload/fetch helper (app-only Graph)"
```

---

### Task 3: `library_drive_id` column + migration

**Files:**
- Modify: `app/models/intelligence.py` (MaterialCardDatasheet, after `onedrive_url` ~line 212)
- Create: `alembic/versions/<NNN>_datasheet_library_drive_id.py`
- Modify: `MIGRATION_NUMBERS_IN_FLIGHT.txt`
- Test: `tests/test_material_datasheet_model.py` (extend)

**Interfaces:**
- Produces: `MaterialCardDatasheet.library_drive_id` (String(200), nullable).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_material_datasheet_model.py`:
```python
def test_datasheet_has_library_drive_id(db_session):
    from app.models.intelligence import MaterialCard, MaterialCardDatasheet
    card = MaterialCard(normalized_mpn="lm317x", display_mpn="LM317X")
    db_session.add(card); db_session.flush()
    ds = MaterialCardDatasheet(material_card_id=card.id, file_name="x.pdf", library_drive_id="DRV123")
    db_session.add(ds); db_session.commit()
    db_session.refresh(card)
    assert card.datasheets[0].library_drive_id == "DRV123"
```

- [ ] **Step 2: Run it (fails: no such column)**

Run: `python3 -m pytest tests/test_material_datasheet_model.py::test_datasheet_has_library_drive_id -q`
Expected: FAIL — `library_drive_id` is not a column.

- [ ] **Step 3: Add the column**

In `app/models/intelligence.py`, in `MaterialCardDatasheet` after `onedrive_url = Column(Text)` (~line 212):
```python
    library_drive_id = Column(String(200))  # Graph drive id of the company library this copy lives in
```

- [ ] **Step 4: Run it (passes on SQLite create_all)**

Run: `python3 -m pytest tests/test_material_datasheet_model.py::test_datasheet_has_library_drive_id -q`
Expected: PASS.

- [ ] **Step 5: Write the migration**

Confirm head: `alembic heads` (expected `115_subscription_health`; if different, use the actual head + next free number). Claim the number in `MIGRATION_NUMBERS_IN_FLIGHT.txt` (append):
```
116 feat/datasheet-company-library material_card_datasheets.library_drive_id; chains onto 115_subscription_health
```
Create `alembic/versions/116_datasheet_library_drive_id.py`:
```python
"""material_card_datasheets.library_drive_id — Graph drive id of the company library."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision = "116_datasheet_library_drive_id"
down_revision = "115_subscription_health"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("material_card_datasheets", sa.Column("library_drive_id", sa.String(length=200), nullable=True))


def downgrade() -> None:
    op.drop_column("material_card_datasheets", "library_drive_id")
```

- [ ] **Step 6: Verify single head + guard**

Run: `alembic heads` → exactly `116_datasheet_library_drive_id (head)`.
Run: `python3 -m pytest tests/test_migration_numbers_in_flight.py tests/test_migration_chain.py -q` → PASS.

- [ ] **Step 7: Commit**

```bash
git add app/models/intelligence.py alembic/versions/116_datasheet_library_drive_id.py MIGRATION_NUMBERS_IN_FLIGHT.txt tests/test_material_datasheet_model.py
git commit -m "feat(datasheet): material_card_datasheets.library_drive_id + migration"
```

---

### Task 4: Swap capture storage to the library + SSRF hardening

**Files:**
- Modify: `app/services/datasheet_capture.py` (storage block ~line 216-236; `_is_safe_url`/`download_pdf` ~line 29-66; `_ONEDRIVE_FOLDER` line 122)
- Test: `tests/test_datasheet_capture.py` (extend), `tests/test_datasheet_primitives.py` (unchanged should still pass)

**Interfaces:**
- Consumes: Task 2 `upload_datasheet_to_library`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_datasheet_capture.py`:
```python
async def test_capture_stores_to_company_library(_session):
    from app.models.intelligence import MaterialCard
    card = MaterialCard(normalized_mpn="17p9905", display_mpn="17P9905",
                        datasheet_url="https://ti/17P9905.pdf", manufacturer="IBM")
    _session.add(card); _session.commit()
    with (
        patch("app.services.datasheet_capture.download_pdf", AsyncMock(return_value=b"%PDF-1.4 data")),
        patch("app.services.datasheet_capture.upload_datasheet_to_library",
              AsyncMock(return_value={"onedrive_item_id": "ITM", "onedrive_url": "https://sp/x",
                                      "size_bytes": 12, "library_drive_id": "DRV"})),
        patch("app.services.datasheet_capture._load_user", return_value=None),
    ):
        await dc.capture_datasheet("17P9905", 0)
    card = _session.query(MaterialCard).filter_by(normalized_mpn="17p9905").first()
    assert len(card.datasheets) == 1
    d = card.datasheets[0]
    assert d.library_drive_id == "DRV" and d.onedrive_item_id == "ITM" and d.verified is True
    assert d.uploaded_by_id is None  # unattended-capable
    assert card.datasheet_captured_at is not None


async def test_capture_skips_when_library_unconfigured(_session):
    from app.models.intelligence import MaterialCard
    card = MaterialCard(normalized_mpn="ne555y", display_mpn="NE555Y", datasheet_url="https://ti/ne555.pdf")
    _session.add(card); _session.commit()
    with (
        patch("app.services.datasheet_capture.download_pdf", AsyncMock(return_value=b"%PDF data")),
        patch("app.services.datasheet_capture.upload_datasheet_to_library", AsyncMock(return_value=None)),
        patch("app.services.datasheet_capture._load_user", return_value=None),
    ):
        await dc.capture_datasheet("NE555Y", 0)
    card = _session.query(MaterialCard).filter_by(normalized_mpn="ne555y").first()
    assert card.datasheets == [] and card.datasheet_searched_at is not None
```

- [ ] **Step 2: Run it (fails)**

Run: `python3 -m pytest tests/test_datasheet_capture.py -q -k "company_library or library_unconfigured"`
Expected: FAIL — `upload_datasheet_to_library` not referenced in `datasheet_capture`.

- [ ] **Step 3: Implement the swap**

In `app/services/datasheet_capture.py`:
1. Replace the import at line 21 `from .onedrive_files import upload_bytes_to_onedrive` with:
```python
from .datasheet_library import upload_datasheet_to_library
```
2. Remove the now-unused `_ONEDRIVE_FOLDER` (line 122).
3. Replace the storage block (the `meta = await upload_bytes_to_onedrive(...)` call through the `MaterialCardDatasheet(...)` row, ~lines 210-236) with:
```python
        user = _load_user(db, user_id)  # optional attribution; storage no longer needs a user token
        meta = await upload_datasheet_to_library(
            f"{card.display_mpn}-datasheet.pdf", pdf, "application/pdf", manufacturer=card.manufacturer or ""
        )
        if not meta:
            _stamp_searched(db, card)
            return
        db.add(
            MaterialCardDatasheet(
                material_card_id=card.id,
                file_name=f"{card.display_mpn}-datasheet.pdf",
                onedrive_item_id=meta["onedrive_item_id"],
                onedrive_url=meta["onedrive_url"],
                library_drive_id=meta["library_drive_id"],
                content_type="application/pdf",
                size_bytes=meta["size_bytes"],
                source=source,
                original_url=url,
                verified=True,
                uploaded_by_id=user.id if user is not None else None,
                captured_at=now,
            )
        )
        card.datasheet_captured_at = now
        db.commit()
```
(Keep the existing `now = datetime.now(timezone.utc)` local that precedes this block; if it was inside the removed region, add `now = datetime.now(timezone.utc)` at the top of the success path. The prior `if user is None: _stamp_searched; return` guard that required a token is REMOVED — storage no longer needs a user.)

4. SSRF hardening — change `download_pdf` to call `_is_safe_url` off the event loop. Where `download_pdf` does `if not _is_safe_url(current):`, replace with:
```python
            import asyncio

            if not await asyncio.to_thread(_is_safe_url, current):
```
(`_is_safe_url` stays sync.)

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_datasheet_capture.py tests/test_datasheet_primitives.py -q`
Expected: PASS (new + existing; the old per-user-token tests that asserted `uploaded_by_id == user_id` may need updating to the library flow — update them to assert the library path / `uploaded_by_id` as attribution only).

- [ ] **Step 5: Commit**

```bash
git add app/services/datasheet_capture.py tests/test_datasheet_capture.py
git commit -m "feat(datasheet): capture stores to company library (app-only) + getaddrinfo off event loop"
```

---

### Task 5: In-app streaming download endpoint + UI link

**Files:**
- Modify: `app/routers/part_dossier.py` (add endpoint; import StreamingResponse + datasheet_library + MaterialCardDatasheet)
- Modify: `app/templates/htmx/partials/search/dossier_datasheet_block.html` (saved-state link → download endpoint, ~line 10)
- Test: `tests/test_part_dossier_router.py` (extend)

**Interfaces:**
- Consumes: Task 2 `fetch_datasheet_bytes`; `MaterialCardDatasheet`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_part_dossier_router.py`:
```python
def test_datasheet_download_streams_pdf(client, db_session):
    from unittest.mock import AsyncMock, patch
    from app.models.intelligence import MaterialCard, MaterialCardDatasheet
    card = MaterialCard(normalized_mpn="lm317z", display_mpn="LM317Z")
    db_session.add(card); db_session.flush()
    ds = MaterialCardDatasheet(material_card_id=card.id, file_name="LM317Z-datasheet.pdf",
                               onedrive_item_id="ITM", library_drive_id="DRV", content_type="application/pdf")
    db_session.add(ds); db_session.commit()
    with patch("app.routers.part_dossier.fetch_datasheet_bytes", AsyncMock(return_value=b"%PDF-1.4 hello")):
        resp = client.get(f"/v2/partials/search/dossier/datasheet/{ds.id}/download")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/pdf")
    assert resp.content == b"%PDF-1.4 hello"


def test_datasheet_download_404_missing(client):
    resp = client.get("/v2/partials/search/dossier/datasheet/99999999/download")
    assert resp.status_code == 404
```

- [ ] **Step 2: Run it (fails)**

Run: `python3 -m pytest tests/test_part_dossier_router.py -q -k "datasheet_download"`
Expected: FAIL — route 404 for the valid id (endpoint missing).

- [ ] **Step 3: Implement the endpoint**

In `app/routers/part_dossier.py`, add imports near the top:
```python
from fastapi.responses import HTMLResponse, StreamingResponse  # extend existing import
from ..models.intelligence import MaterialCardDatasheet
from ..services.datasheet_library import fetch_datasheet_bytes
```
Add the endpoint (near `dossier_datasheet_status`):
```python
@router.get("/v2/partials/search/dossier/datasheet/{datasheet_id:int}/download")
async def dossier_datasheet_download(
    datasheet_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Stream our stored datasheet copy from the company library (app-only fetch)."""
    row = db.query(MaterialCardDatasheet).filter(MaterialCardDatasheet.id == datasheet_id).first()
    if row is None or not row.onedrive_item_id:
        raise HTTPException(404, "Datasheet not found")
    data = await fetch_datasheet_bytes(row.library_drive_id, row.onedrive_item_id)
    if data is None:
        raise HTTPException(502, "Datasheet temporarily unavailable")
    return StreamingResponse(
        iter([data]),
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{row.file_name}"'},
    )
```
(Confirm `HTTPException` is imported in the file; it is used elsewhere there.)

- [ ] **Step 4: Update the UI link**

In `dossier_datasheet_block.html`, change the saved-state link (line 10) from
`href="{{ saved.onedrive_url }}" target="_blank"` to:
```html
  <a href="/v2/partials/search/dossier/datasheet/{{ saved.id }}/download" target="_blank" rel="noopener noreferrer"
```
(Keep the rest of the anchor — icon + "Datasheet (saved …)" label — unchanged.)

- [ ] **Step 5: Run tests**

Run: `python3 -m pytest tests/test_part_dossier_router.py -q`
Expected: PASS (new download tests + existing dossier tests; the stored-datasheet render test now expects the `/download` href — update its assertion from `onedrive_url` to the download path).

- [ ] **Step 6: Commit**

```bash
git add app/routers/part_dossier.py app/templates/htmx/partials/search/dossier_datasheet_block.html tests/test_part_dossier_router.py
git commit -m "feat(datasheet): in-app streaming download from company library"
```

---

### Task 6: Docs + full-suite/pre-commit gate

**Files:**
- Modify: `docs/APP_MAP_INTERACTIONS.md` (datasheet flow: company-library storage + app-only auth + download endpoint + config)

- [ ] **Step 1: Update the doc**

In the datasheet subsection, replace the OneDrive-storage description with: app-only Graph (`graph_app_auth`) → company library (`datasheet_library`, configured `DATASHEET_LIBRARY_DRIVE_ID`, graceful skip when unset) → `material_card_datasheets` (+`library_drive_id`); in-app `/dossier/datasheet/{id}/download` streams it; one-time Azure `Sites.Selected` setup.

- [ ] **Step 2: Full suite + pre-commit**

Run: `TESTING=1 python3 -m pytest tests/ -q -n auto` → all green (pre-existing flaky-on-overloaded-box tests excepted — confirm in isolation if any fail).
Run: `pre-commit run --files $(git diff --name-only $(git merge-base origin/main HEAD)..HEAD)` → ruff/format/docformatter/mypy pass.

- [ ] **Step 3: Commit**

```bash
git add docs/APP_MAP_INTERACTIONS.md
git commit -m "docs(datasheet): APP_MAP — company-library storage + app-only auth"
```

---

## Self-Review

**Spec coverage:** app-only auth (Task 1); company library helper (Task 2); `library_drive_id` + migration (Task 3); capture swap to library + `uploaded_by_id` optional + SSRF `getaddrinfo`→`to_thread` (Task 4); in-app streaming download + UI link (Task 5); graceful-skip-when-unconfigured (Tasks 2/4); upload non-2xx/exception tests (Task 2); docs (Task 6). All spec sections mapped.

**Placeholder scan:** every step has real code; migration number `116`/head `115_subscription_health` are "confirm at build" (main may have moved) — the only deferred value, by protocol, not a placeholder.

**Type consistency:** `upload_datasheet_to_library` returns the 4-key dict consumed verbatim in Task 4; `fetch_datasheet_bytes(drive_id, item_id)` consumed in Task 5; `get_app_graph_token() -> str|None` consumed in Task 2; `MaterialCardDatasheet.library_drive_id` defined Task 3, written Task 4, read Task 5.

## Known follow-ups (post-IT-config)
- Live-verify the real library upload + in-app download once `DATASHEET_LIBRARY_DRIVE_ID` is set (the SharePoint drive id from IT) + the `Sites.Selected` grant is applied.
