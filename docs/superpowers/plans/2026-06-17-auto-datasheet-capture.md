# Auto-Datasheet Capture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** On part search and RFQ-add, find a part's datasheet, verify it matches the MPN, store a permanent copy in OneDrive, and attach it to the `MaterialCard` — surviving vendor takedown of EOL datasheets.

**Architecture:** A fire-and-forget background job (`capture_datasheet`) triggered from the search/RFQ endpoints via `safe_background_task`. The job: gate (dedup + 30-day negative cache) → find candidate URL (connector `datasheet_url` first, Claude `web_search` fallback) → download PDF (guards) → verify (trusted source accepted; web hit must contain the MPN in its text) → upload our copy to OneDrive via Graph (delegated user token) → write a `MaterialCardDatasheet` row + stamp the card. UI shows the stored datasheet (link to our copy) with fetching/none-found states on the dossier specs section.

**Tech Stack:** FastAPI, SQLAlchemy 2.0 (sync sessions), Alembic, httpx (`app.http_client.http`), Microsoft Graph (delegated token), Anthropic `claude_json` web_search, `pypdf` (new dep), Jinja/HTMX.

## Global Constraints

- All datetime columns use `Column(UTCDateTime, ...)` (`from ..database import UTCDateTime`), never `DateTime`. Python-side default: `default=lambda: datetime.now(timezone.utc)`.
- New ORM models MUST be imported in `app/models/__init__.py` with `# noqa: F401`.
- Alembic numbered migration: claim the next free number (**108**) in `MIGRATION_NUMBERS_IN_FLIGHT.txt` (append-only) before merge; `down_revision = "107_is_scratch_requisitions"`; revision id ≤ 32 chars; use `if_not_exists=True` on create_table/create_index.
- Python deps: edit `requirements.in` (NOT `requirements.txt`); recompile the lock with `pip-compile` (see Task 3). CI has a lockfile-sync gate.
- OneDrive auth is **delegated** (`get_valid_token(user, db)` → user's `/me/drive`). The background job must load the `User` in its own session and fetch the token there. No M365 token → graceful skip (stamp `datasheet_searched_at`).
- `MaterialCard.datasheet_url` (String(1000)) is the EXISTING external link (kept as provenance). The stored copy is the NEW `MaterialCardDatasheet` record — do not conflate.
- Background jobs (`safe_background_task` coros) MUST open their own `SessionLocal()`; never accept a request-scoped `Session`.
- AI/web calls must be skipped under `TESTING` (env `TESTING=1`) and when no Anthropic key is configured (mirror `AIWebSearchConnector` gating).
- Tailwind: use only existing `brand-*` and standard neutral classes.
- After implementation, update the relevant `docs/APP_MAP_*.md`.

---

### Task 1: Data model — `MaterialCardDatasheet` + `MaterialCard` stamp columns

**Files:**
- Modify: `app/models/intelligence.py` (add columns to `MaterialCard` ~line 54-138; add new model after `MaterialCard`)
- Modify: `app/models/__init__.py` (export the new model)
- Test: `tests/test_material_datasheet_model.py`

**Interfaces:**
- Produces: `MaterialCardDatasheet` ORM model (`__tablename__ = "material_card_datasheets"`); `MaterialCard.datasheet_captured_at`, `MaterialCard.datasheet_searched_at` (UTCDateTime, nullable); `MaterialCard.datasheets` relationship (list, ordered newest-first).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_material_datasheet_model.py
import os
os.environ["TESTING"] = "1"
from datetime import datetime, timezone
from app.models.intelligence import MaterialCard, MaterialCardDatasheet


def test_datasheet_row_links_to_card(db_session):
    card = MaterialCard(normalized_mpn="lm317t", display_mpn="LM317T")
    db_session.add(card)
    db_session.flush()
    ds = MaterialCardDatasheet(
        material_card_id=card.id,
        file_name="LM317T-datasheet.pdf",
        onedrive_item_id="01ABC",
        onedrive_url="https://onedrive/x",
        content_type="application/pdf",
        size_bytes=12345,
        source="connector",
        original_url="https://ti.com/lm317t.pdf",
        verified=True,
        captured_at=datetime.now(timezone.utc),
    )
    db_session.add(ds)
    db_session.commit()
    db_session.refresh(card)
    assert card.datasheets[0].file_name == "LM317T-datasheet.pdf"
    assert card.datasheets[0].verified is True


def test_card_has_datasheet_stamp_columns(db_session):
    card = MaterialCard(normalized_mpn="ne555", display_mpn="NE555")
    card.datasheet_searched_at = datetime.now(timezone.utc)
    db_session.add(card)
    db_session.commit()
    assert card.datasheet_searched_at is not None
    assert card.datasheet_captured_at is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_material_datasheet_model.py -q`
Expected: FAIL — `ImportError: cannot import name 'MaterialCardDatasheet'`.

- [ ] **Step 3: Add the stamp columns to `MaterialCard`**

In `app/models/intelligence.py`, inside the `MaterialCard` class near the other enrichment columns (e.g. just after `datasheet_url = Column(String(1000))` at line 54), add:

```python
    # Auto-datasheet capture: stamps drive the dossier UI + 30-day negative cache.
    datasheet_captured_at = Column(UTCDateTime, nullable=True)
    datasheet_searched_at = Column(UTCDateTime, nullable=True)
```

Confirm `UTCDateTime` is already imported in this file (it is — used by `created_at`). If not: `from ..database import UTCDateTime`.

- [ ] **Step 4: Add the `MaterialCardDatasheet` model**

In `app/models/intelligence.py`, after the `MaterialCard` class definition, add (mirrors `OfferAttachment` in `app/models/offers.py:153`):

```python
class MaterialCardDatasheet(Base):
    """A permanent datasheet copy stored in OneDrive, attached to a MaterialCard.

    Unlike MaterialCard.datasheet_url (an external link that rots when vendors pull
    EOL datasheets), this is our own copy: download → verify → store in OneDrive.
    """

    __tablename__ = "material_card_datasheets"

    id = Column(Integer, primary_key=True)
    material_card_id = Column(
        Integer, ForeignKey("material_cards.id", ondelete="CASCADE"), nullable=False, index=True
    )
    file_name = Column(String(500), nullable=False)
    onedrive_item_id = Column(String(500))
    onedrive_url = Column(Text)
    content_type = Column(String(100))
    size_bytes = Column(Integer)
    source = Column(String(50))  # "connector" | "web"
    original_url = Column(Text)  # where the copy came from (provenance/audit)
    verified = Column(Boolean, nullable=False, default=False)
    uploaded_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    captured_at = Column(UTCDateTime, default=lambda: datetime.now(timezone.utc))

    material_card = relationship("MaterialCard", back_populates="datasheets")
    uploaded_by = relationship("User", foreign_keys=[uploaded_by_id])
```

Add the back-reference inside `MaterialCard` (near other relationships, or after the new stamp columns):

```python
    datasheets = relationship(
        "MaterialCardDatasheet",
        back_populates="material_card",
        cascade="all, delete-orphan",
        order_by="desc(MaterialCardDatasheet.captured_at)",
    )
```

Ensure imports at the top of `intelligence.py` include `Boolean`, `ForeignKey`, `Text`, `relationship` (add any missing to the existing `from sqlalchemy import ...` / `from sqlalchemy.orm import ...` lines).

- [ ] **Step 5: Export the model**

In `app/models/__init__.py`, add `MaterialCardDatasheet` to the existing `from .intelligence import (...)` block (with the other MaterialCard exports), keeping the `# noqa: F401` style.

- [ ] **Step 6: Run test to verify it passes**

Run: `python3 -m pytest tests/test_material_datasheet_model.py -q`
Expected: PASS (2 passed).

- [ ] **Step 7: Commit**

```bash
git add app/models/intelligence.py app/models/__init__.py tests/test_material_datasheet_model.py
git commit -m "feat(datasheet): MaterialCardDatasheet model + card stamp columns"
```

---

### Task 2: Alembic migration (claim 108)

**Files:**
- Create: `alembic/versions/108_material_card_datasheets.py`
- Modify: `MIGRATION_NUMBERS_IN_FLIGHT.txt` (append claim line)
- Test: `tests/test_migration_numbers_in_flight.py` (existing guard — must still pass)

**Interfaces:**
- Consumes: model from Task 1.
- Produces: `material_card_datasheets` table; `datasheet_captured_at` + `datasheet_searched_at` columns on `material_cards`.

- [ ] **Step 1: Claim the number**

Append to `MIGRATION_NUMBERS_IN_FLIGHT.txt` (follow the file's existing line format):

```
108  feat/auto-datasheet-capture  material_card_datasheets + card datasheet stamps  (chains onto 107_is_scratch_requisitions)
```

- [ ] **Step 2: Write the migration**

Create `alembic/versions/108_material_card_datasheets.py`:

```python
"""material_card_datasheets table + datasheet stamp columns on material_cards."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision = "108_material_card_datasheets"
down_revision = "107_is_scratch_requisitions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("material_cards", sa.Column("datasheet_captured_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("material_cards", sa.Column("datasheet_searched_at", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        "material_card_datasheets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("material_card_id", sa.Integer(), nullable=False),
        sa.Column("file_name", sa.String(length=500), nullable=False),
        sa.Column("onedrive_item_id", sa.String(length=500), nullable=True),
        sa.Column("onedrive_url", sa.Text(), nullable=True),
        sa.Column("content_type", sa.String(length=100), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("source", sa.String(length=50), nullable=True),
        sa.Column("original_url", sa.Text(), nullable=True),
        sa.Column("verified", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("uploaded_by_id", sa.Integer(), nullable=True),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["material_card_id"], ["material_cards.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["uploaded_by_id"], ["users.id"], ondelete="SET NULL"),
        if_not_exists=True,
    )
    op.create_index(
        "ix_material_card_datasheets_material_card_id",
        "material_card_datasheets",
        ["material_card_id"],
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index("ix_material_card_datasheets_material_card_id", table_name="material_card_datasheets")
    op.drop_table("material_card_datasheets")
    op.drop_column("material_cards", "datasheet_searched_at")
    op.drop_column("material_cards", "datasheet_captured_at")
```

- [ ] **Step 3: Verify migration applies on SQLite (test harness) and the in-flight guard passes**

Run: `python3 -m pytest tests/test_migration_numbers_in_flight.py -q`
Expected: PASS.

Run (apply head against a scratch SQLite DB — the project's migration harness):
`python3 -m pytest tests/migration_harness.py -q` (if a harness test exists) OR confirm models match by running Task 1's tests again.
Expected: PASS / no schema errors.

- [ ] **Step 4: Commit**

```bash
git add alembic/versions/108_material_card_datasheets.py MIGRATION_NUMBERS_IN_FLIGHT.txt
git commit -m "feat(datasheet): migration 108 — material_card_datasheets + stamps"
```

---

### Task 3: Add `pypdf` dependency

**Files:**
- Modify: `requirements.in` (add `pypdf`)
- Modify: `requirements.txt` (regenerated — do NOT hand-edit)

**Interfaces:**
- Produces: `pypdf` importable for Task 5's PDF text extraction.

- [ ] **Step 1: Add to the source file**

Append `pypdf` to `requirements.in` (alphabetical position among the runtime deps).

- [ ] **Step 2: Recompile the lock**

Run: `pip-compile requirements.in` (regenerates `requirements.txt`; this is the project's pip-tools workflow — see `.github/workflows/dependabot-lockfile-sync.yml`). If `pip-compile` is unavailable, `python3 -m piptools compile requirements.in`.
Expected: `requirements.txt` now pins `pypdf==<version>` (and any deps).

- [ ] **Step 3: Install + verify import**

Run: `pip install pypdf && python3 -c "import pypdf; print(pypdf.__version__)"`
Expected: prints a version, no error.

- [ ] **Step 4: Commit**

```bash
git add requirements.in requirements.txt
git commit -m "build(datasheet): add pypdf for datasheet PDF text verification"
```

---

### Task 4: OneDrive upload helper (service)

**Files:**
- Create: `app/services/onedrive_files.py`
- Test: `tests/test_onedrive_files.py`

**Interfaces:**
- Consumes: `app.http_client.http`, `app.utils.token_manager.get_valid_token`.
- Produces: `async def upload_bytes_to_onedrive(user, db, folder_path: str, file_name: str, content: bytes, content_type: str) -> dict | None` returning `{"onedrive_item_id": str, "onedrive_url": str, "size_bytes": int}` or `None` on failure (no token / non-2xx).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_onedrive_files.py
import os
os.environ["TESTING"] = "1"
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from app.services.onedrive_files import upload_bytes_to_onedrive


async def test_upload_returns_metadata_on_success(db_session):
    user = MagicMock()
    resp = MagicMock(status_code=201)
    resp.json.return_value = {"id": "01ITEM", "webUrl": "https://od/x"}
    with (
        patch("app.services.onedrive_files.get_valid_token", AsyncMock(return_value="tok")),
        patch("app.services.onedrive_files.http") as http,
    ):
        http.put = AsyncMock(return_value=resp)
        out = await upload_bytes_to_onedrive(user, db_session, "AvailAI/Datasheets/1", "x.pdf", b"%PDF-1.4", "application/pdf")
    assert out == {"onedrive_item_id": "01ITEM", "onedrive_url": "https://od/x", "size_bytes": 8}


async def test_upload_returns_none_without_token(db_session):
    with patch("app.services.onedrive_files.get_valid_token", AsyncMock(return_value=None)):
        out = await upload_bytes_to_onedrive(MagicMock(), db_session, "f", "x.pdf", b"x", "application/pdf")
    assert out is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_onedrive_files.py -q`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement the helper**

Create `app/services/onedrive_files.py` (mirrors `app/routers/requisitions/attachments.py:85-135`):

```python
"""onedrive_files.py — reusable OneDrive (Graph) byte upload for background jobs.

Extracted from the requisition/offer attachment upload pattern so non-request code
(e.g. the datasheet capture job) can store a file in the user's OneDrive. Delegated
token via get_valid_token; uploads to /me/drive/root:/<folder_path>/<file_name>:/content.
"""

from __future__ import annotations

from loguru import logger

from ..http_client import http
from ..utils.token_manager import get_valid_token


async def upload_bytes_to_onedrive(
    user, db, folder_path: str, file_name: str, content: bytes, content_type: str
) -> dict | None:
    """Upload bytes to OneDrive; return {onedrive_item_id, onedrive_url, size_bytes} or None."""
    token = await get_valid_token(user, db)
    if not token:
        logger.warning("onedrive upload skipped — no Graph token for user")
        return None
    safe_name = (file_name or "file").replace("/", "_").replace("\\", "_")
    drive_path = f"/me/drive/root:/{folder_path}/{safe_name}:/content"
    try:
        resp = await http.put(
            f"https://graph.microsoft.com/v1.0{drive_path}",
            content=content,
            headers={"Authorization": f"Bearer {token}", "Content-Type": content_type or "application/octet-stream"},
            timeout=60,
        )
    except Exception:
        logger.warning("onedrive upload errored path={}", drive_path, exc_info=True)
        return None
    if resp.status_code not in (200, 201):
        logger.warning("onedrive upload failed {} {}", resp.status_code, resp.text[:200])
        return None
    result = resp.json()
    return {
        "onedrive_item_id": result.get("id"),
        "onedrive_url": result.get("webUrl"),
        "size_bytes": len(content),
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_onedrive_files.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add app/services/onedrive_files.py tests/test_onedrive_files.py
git commit -m "feat(datasheet): reusable OneDrive byte-upload helper"
```

---

### Task 5: Datasheet primitives — download + verify

**Files:**
- Create: `app/services/datasheet_capture.py` (primitives now; orchestrator added in Task 6)
- Test: `tests/test_datasheet_primitives.py`

**Interfaces:**
- Consumes: `app.http_client.http_redirect`, `pypdf`, `app.utils.normalization.normalize_mpn_key`.
- Produces:
  - `async def download_pdf(url: str) -> bytes | None` — GET with redirects, 60s timeout; returns bytes only if it looks like a PDF (`%PDF` magic) and size ≤ 25 MB, else None.
  - `def pdf_contains_mpn(pdf_bytes: bytes, mpn: str) -> bool` — extract text (first 20 pages), strip to alphanumeric-lowercase, return True if `normalize_mpn_key(mpn)` (len ≥ 4) is a substring.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_datasheet_primitives.py
import os
os.environ["TESTING"] = "1"
from app.services.datasheet_capture import pdf_contains_mpn


def _pdf_with_text(text: str) -> bytes:
    # Minimal real PDF via pypdf so extraction has something to read.
    import io
    from pypdf import PdfWriter
    # pypdf can't easily add text; use a tiny hand-built PDF instead.
    return (
        b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]/Contents 4 0 R"
        b"/Resources<</Font<</F1 5 0 R>>>>>>endobj\n"
        b"4 0 obj<</Length 60>>stream\nBT /F1 12 Tf 10 100 Td (" + text.encode() + b") Tj ET\nendstream endobj\n"
        b"5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n"
        b"trailer<</Root 1 0 R>>\n%%EOF"
    )


def test_pdf_contains_mpn_true():
    assert pdf_contains_mpn(_pdf_with_text("Part 17P9905 Hard Drive"), "17P9905") is True


def test_pdf_contains_mpn_false_for_wrong_part():
    assert pdf_contains_mpn(_pdf_with_text("Part 1300940294 component"), "17P9905") is False


def test_pdf_contains_mpn_handles_unparseable_bytes():
    assert pdf_contains_mpn(b"not a pdf", "17P9905") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_datasheet_primitives.py -q`
Expected: FAIL — module/function not found.

- [ ] **Step 3: Implement the primitives**

Create `app/services/datasheet_capture.py`:

```python
"""datasheet_capture.py — find/download/verify/store a part's datasheet.

Primitives here; the capture orchestrator (capture_datasheet) is added in Task 6.
"""

from __future__ import annotations

import io
import re

from loguru import logger

from ..http_client import http_redirect
from ..utils.normalization import normalize_mpn_key

MAX_DATASHEET_BYTES = 25 * 1024 * 1024
_MAX_VERIFY_PAGES = 20
_NONALNUM = re.compile(r"[^a-z0-9]")


async def download_pdf(url: str) -> bytes | None:
    """GET a URL (following redirects); return bytes iff it is a PDF within the size cap."""
    if not url:
        return None
    try:
        resp = await http_redirect.get(url, timeout=60)
    except Exception:
        logger.warning("datasheet download errored url={}", url, exc_info=True)
        return None
    if resp.status_code != 200:
        return None
    content = resp.content
    if not content or len(content) > MAX_DATASHEET_BYTES:
        return None
    ctype = (resp.headers.get("content-type") or "").lower()
    if not (content[:5] == b"%PDF-" or "application/pdf" in ctype):
        return None
    return content


def pdf_contains_mpn(pdf_bytes: bytes, mpn: str) -> bool:
    """True if the MPN (normalized key, len>=4) appears in the PDF's extracted text."""
    key = normalize_mpn_key(mpn)
    if len(key) < 4:
        return False
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(pdf_bytes))
        text_parts = []
        for page in reader.pages[:_MAX_VERIFY_PAGES]:
            text_parts.append(page.extract_text() or "")
        text_key = _NONALNUM.sub("", "".join(text_parts).lower())
    except Exception:
        logger.warning("datasheet pdf parse failed", exc_info=True)
        return False
    return key in text_key
```

(`http_redirect` exists in `app/http_client.py` alongside `http`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_datasheet_primitives.py -q`
Expected: PASS (3 passed). If the tiny PDF doesn't extract text under the installed pypdf, replace `_pdf_with_text` in the test with a reportlab-free fixture committed under `tests/fixtures/` — but pypdf extracts the `(text) Tj` operator above.

- [ ] **Step 5: Commit**

```bash
git add app/services/datasheet_capture.py tests/test_datasheet_primitives.py
git commit -m "feat(datasheet): PDF download + MPN-in-PDF verification primitives"
```

---

### Task 6: Finder + capture orchestrator

**Files:**
- Modify: `app/services/datasheet_capture.py` (add finder + `capture_datasheet`)
- Test: `tests/test_datasheet_capture.py`

**Interfaces:**
- Consumes: Task 4 `upload_bytes_to_onedrive`, Task 5 primitives, `app.database.SessionLocal`, `app.search_service.resolve_material_card`, `app.utils.claude_client.claude_json`, `app.services.credential_service.get_credential_cached`, `app.models` (`MaterialCard`, `MaterialCardDatasheet`, `User`).
- Produces:
  - `async def find_datasheet_url(card, mpn: str) -> tuple[str, str] | None` — returns `(url, source)` where source ∈ {"connector","web"}; connector = `card.datasheet_url`; web = Claude `web_search` (skipped under TESTING / no key).
  - `async def capture_datasheet(mpn: str, user_id: int) -> None` — the fire-and-forget job (opens own session).
  - `CAPTURE_COOLDOWN_DAYS = 30`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_datasheet_capture.py
import os
os.environ["TESTING"] = "1"
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from app.models.intelligence import MaterialCard, MaterialCardDatasheet
from app.services import datasheet_capture as dc


@pytest.fixture(autouse=True)
def _session(db_session):
    with patch("app.services.datasheet_capture.SessionLocal", lambda: db_session):
        yield db_session


async def test_capture_stores_verified_connector_datasheet(_session):
    card = MaterialCard(normalized_mpn="17p9905", display_mpn="17P9905", datasheet_url="https://ti/17P9905.pdf")
    _session.add(card); _session.commit()
    user = MagicMock(id=1)
    with (
        patch("app.services.datasheet_capture._load_user", return_value=user),
        patch("app.services.datasheet_capture.download_pdf", AsyncMock(return_value=b"%PDF-1.4 data")),
        patch("app.services.datasheet_capture.upload_bytes_to_onedrive",
              AsyncMock(return_value={"onedrive_item_id": "01", "onedrive_url": "https://od/x", "size_bytes": 12})),
    ):
        await dc.capture_datasheet("17P9905", 1)
    _session.refresh(card)
    assert len(card.datasheets) == 1
    assert card.datasheets[0].source == "connector"
    assert card.datasheets[0].verified is True
    assert card.datasheet_captured_at is not None


async def test_capture_skips_within_cooldown(_session):
    card = MaterialCard(normalized_mpn="ne555", display_mpn="NE555",
                        datasheet_searched_at=datetime.now(timezone.utc) - timedelta(days=5))
    _session.add(card); _session.commit()
    with patch("app.services.datasheet_capture.find_datasheet_url", AsyncMock()) as find:
        await dc.capture_datasheet("NE555", 1)
        find.assert_not_called()


async def test_capture_web_hit_rejected_when_mpn_absent(_session):
    card = MaterialCard(normalized_mpn="17p9905", display_mpn="17P9905")  # no connector url
    _session.add(card); _session.commit()
    user = MagicMock(id=1)
    with (
        patch("app.services.datasheet_capture._load_user", return_value=user),
        patch("app.services.datasheet_capture.find_datasheet_url", AsyncMock(return_value=("https://x/wrong.pdf", "web"))),
        patch("app.services.datasheet_capture.download_pdf", AsyncMock(return_value=b"%PDF wrong")),
        patch("app.services.datasheet_capture.pdf_contains_mpn", return_value=False),
        patch("app.services.datasheet_capture.upload_bytes_to_onedrive", AsyncMock()) as up,
    ):
        await dc.capture_datasheet("17P9905", 1)
        up.assert_not_called()
    _session.refresh(card)
    assert card.datasheets == []
    assert card.datasheet_searched_at is not None  # negative cache stamped
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_datasheet_capture.py -q`
Expected: FAIL — `find_datasheet_url` / `capture_datasheet` / `_load_user` not defined.

- [ ] **Step 3: Implement finder + orchestrator**

Append to `app/services/datasheet_capture.py`:

```python
import os
from datetime import datetime, timezone, timedelta

from ..database import SessionLocal
from .onedrive_files import upload_bytes_to_onedrive

CAPTURE_COOLDOWN_DAYS = 30
_ONEDRIVE_FOLDER = "AvailAI/Datasheets"


def _load_user(db, user_id: int):
    from ..models import User

    return db.query(User).filter(User.id == user_id).first()


async def find_datasheet_url(card, mpn: str) -> tuple[str, str] | None:
    """Connector datasheet_url first (trusted); else Claude web_search (untrusted)."""
    if card is not None and card.datasheet_url:
        return (card.datasheet_url, "connector")

    if os.environ.get("TESTING"):
        return None
    from .credential_service import get_credential_cached

    if not get_credential_cached("anthropic_ai", "ANTHROPIC_API_KEY"):
        return None
    from ..utils.claude_client import claude_json

    mfr = (getattr(card, "manufacturer", "") or "") if card else ""
    prompt = (
        f"Find the official manufacturer datasheet PDF for part number '{mpn}'"
        f"{(' by ' + mfr) if mfr else ''}. Return JSON {{\"datasheet_url\": \"<direct PDF url>\"}} "
        f"or {{\"datasheet_url\": null}} if none found. The URL must point directly at a PDF."
    )
    try:
        out = await claude_json(
            prompt,
            model_tier="smart",
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 6}],
            timeout=60,
            cost_bucket="datasheet_capture",
        )
    except Exception:
        logger.warning("datasheet web_search failed mpn={}", mpn, exc_info=True)
        return None
    url = (out or {}).get("datasheet_url") if isinstance(out, dict) else None
    return (url, "web") if url else None


async def capture_datasheet(mpn: str, user_id: int) -> None:
    """Fire-and-forget: find → verify → store a datasheet copy on the MPN's card.

    Opens its own session (request session is gone by the time this runs).
    """
    from ..models import MaterialCard, MaterialCardDatasheet

    db = SessionLocal()
    try:
        key = normalize_mpn_key(mpn)
        if not key:
            return
        card = (
            db.query(MaterialCard)
            .filter(MaterialCard.normalized_mpn == key, MaterialCard.deleted_at.is_(None))
            .first()
        )
        # Gate: already stored, or within negative-cache cooldown.
        if card is not None:
            if card.datasheets:
                return
            if card.datasheet_searched_at:
                age = datetime.now(timezone.utc) - _as_utc(card.datasheet_searched_at)
                if age < timedelta(days=CAPTURE_COOLDOWN_DAYS):
                    return

        found = await find_datasheet_url(card, mpn)
        if not found:
            _stamp_searched(db, card)
            return
        url, source = found

        pdf = await download_pdf(url)
        if not pdf:
            _stamp_searched(db, card)
            return

        if source == "web" and not pdf_contains_mpn(pdf, mpn):
            _stamp_searched(db, card)  # wrong file — do not store
            return

        # Ensure a card exists to attach to (approved cardless rule: verified hit only).
        if card is None:
            from ..search_service import resolve_material_card

            card = resolve_material_card(mpn, db)
            if card is None:
                return

        user = _load_user(db, user_id)
        if user is None:
            _stamp_searched(db, card)
            return
        meta = await upload_bytes_to_onedrive(
            user, db, f"{_ONEDRIVE_FOLDER}/{card.id}", f"{card.display_mpn}-datasheet.pdf", pdf, "application/pdf"
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
                content_type="application/pdf",
                size_bytes=meta["size_bytes"],
                source=source,
                original_url=url,
                verified=True,
                uploaded_by_id=user.id,
                captured_at=datetime.now(timezone.utc),
            )
        )
        card.datasheet_captured_at = datetime.now(timezone.utc)
        db.commit()
        logger.info("datasheet captured mpn={} source={}", mpn, source)
    except Exception:
        logger.exception("capture_datasheet failed mpn={}", mpn)
        db.rollback()
    finally:
        db.close()


def _as_utc(dt):
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _stamp_searched(db, card) -> None:
    if card is None:
        return  # cardless miss — no place to negative-cache (re-hunts next trigger)
    card.datasheet_searched_at = datetime.now(timezone.utc)
    db.commit()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_datasheet_capture.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add app/services/datasheet_capture.py tests/test_datasheet_capture.py
git commit -m "feat(datasheet): finder (connector→web) + capture orchestrator"
```

---

### Task 7: Wire the triggers (search + RFQ)

**Files:**
- Modify: `app/routers/part_dossier.py` (dossier_hero ~line 70-92; quick_source `_start_quick_source` ~line 252-264)
- Modify: `app/routers/requisitions/requirements.py` (`add_requirements` ~line 368, after each Requirement gets its MPN)
- Test: `tests/test_part_dossier_router.py` (extend), `tests/test_datasheet_triggers.py`

**Interfaces:**
- Consumes: `app.services.datasheet_capture.capture_datasheet`, `app.utils.async_helpers.safe_background_task`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_datasheet_triggers.py
import os
os.environ["TESTING"] = "1"
from unittest.mock import AsyncMock, patch
from app.models.intelligence import MaterialCard


def test_search_enqueues_capture(client, db_session):
    db_session.add(MaterialCard(normalized_mpn="lm317t", display_mpn="LM317T")); db_session.commit()
    with patch("app.routers.part_dossier.safe_background_task", AsyncMock()) as bg:
        resp = client.get("/v2/partials/search/dossier/hero", params={"mpn": "LM317T"})
    assert resp.status_code == 200
    assert bg.called  # capture_datasheet enqueued
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_datasheet_triggers.py -q`
Expected: FAIL — `safe_background_task` not referenced in part_dossier (AttributeError on patch target) or `bg.called` False.

- [ ] **Step 3: Hook the search trigger (`dossier_hero`)**

In `app/routers/part_dossier.py`, add near the top-level imports:

```python
from ..utils.async_helpers import safe_background_task
```

At the END of `dossier_hero` (just before `return template_response(...)`), enqueue:

```python
    # Auto-datasheet capture (background, never blocks the dossier render).
    from ..services.datasheet_capture import capture_datasheet

    await safe_background_task(capture_datasheet(display_mpn, user.id), task_name="datasheet_capture")
```

- [ ] **Step 4: Hook the RFQ triggers**

In `app/routers/part_dossier.py`, inside `_start_quick_source` (or its async callers `quick_source_rfq`/`quick_source_offer`, which are `async`), after the requirement/card is created and committed, enqueue capture for `mpn`. Since `_start_quick_source` is sync, enqueue in the async route handlers instead — in both `quick_source_rfq` and `quick_source_offer`, after `_redirect_to_req(...)` is computed but before returning, add:

```python
    if mpn.strip():
        from ..services.datasheet_capture import capture_datasheet

        await safe_background_task(capture_datasheet(mpn.strip().upper(), user.id), task_name="datasheet_capture")
```

In `app/routers/requisitions/requirements.py` `add_requirements`, after the per-requirement loop commits (each Requirement has its `primary_mpn`), enqueue once per distinct MPN:

```python
    from ...services.datasheet_capture import capture_datasheet
    from ...utils.async_helpers import safe_background_task

    for _mpn in {r.primary_mpn for r in created if r.primary_mpn}:
        await safe_background_task(capture_datasheet(_mpn, user.id), task_name="datasheet_capture")
```

(Use the actual list/var name holding created requirements — confirm in the loop; if the endpoint already has a `BackgroundTasks` param, `safe_background_task` is still preferred for session-safety per Global Constraints.)

- [ ] **Step 5: Run tests**

Run: `python3 -m pytest tests/test_datasheet_triggers.py tests/test_part_dossier_router.py -q`
Expected: PASS (existing dossier tests still green + new trigger test passes).

- [ ] **Step 6: Commit**

```bash
git add app/routers/part_dossier.py app/routers/requisitions/requirements.py tests/test_datasheet_triggers.py
git commit -m "feat(datasheet): enqueue capture on search + RFQ-add"
```

---

### Task 8: Dossier UI — stored datasheet + status + poll

**Files:**
- Modify: `app/templates/htmx/partials/search/dossier_specs.html` (datasheet section ~line 63-77)
- Modify: `app/routers/part_dossier.py` (add `datasheet-status` poll endpoint; ensure `dossier_specs` ctx has the card with `datasheets` — it already passes `card`)
- Test: `tests/test_part_dossier_router.py` (extend)

**Interfaces:**
- Consumes: `card.datasheets`, `card.datasheet_captured_at`, `card.datasheet_searched_at`.
- Produces: `GET /v2/partials/search/dossier/datasheet-status?mpn=` → renders the datasheet block; returns HTTP 286 once captured or once a search has been recorded (stops polling).

- [ ] **Step 1: Write the failing test**

```python
def test_specs_shows_stored_datasheet(client, db_session):
    from datetime import datetime, timezone
    from app.models.intelligence import MaterialCard, MaterialCardDatasheet
    card = MaterialCard(normalized_mpn="lm317t", display_mpn="LM317T",
                        datasheet_captured_at=datetime.now(timezone.utc))
    db_session.add(card); db_session.flush()
    db_session.add(MaterialCardDatasheet(material_card_id=card.id, file_name="LM317T-datasheet.pdf",
                   onedrive_url="https://od/x", source="connector", verified=True,
                   captured_at=datetime.now(timezone.utc)))
    db_session.commit()
    resp = client.get("/v2/partials/search/dossier/specs", params={"mpn": "LM317T"})
    assert resp.status_code == 200
    assert "https://od/x" in resp.text
    assert "Datasheet (saved" in resp.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_part_dossier_router.py::test_specs_shows_stored_datasheet -q`
Expected: FAIL — "Datasheet (saved" not in output.

- [ ] **Step 3: Update the template**

In `app/templates/htmx/partials/search/dossier_specs.html`, replace the datasheet pill block (lines 63-77) so it prefers our stored copy and shows status. Keep the manufacturer pill:

```html
{# Datasheet (our stored copy preferred) + manufacturer pills. #}
{% set saved = card.datasheets[0] if card and card.datasheets else None %}
<div class="mt-4 flex flex-wrap items-center gap-2">
  {% if saved %}
  <a href="{{ saved.onedrive_url }}" target="_blank" rel="noopener noreferrer"
     class="inline-flex items-center gap-1.5 rounded-lg border border-brand-300 px-3 py-1.5 text-xs font-medium text-brand-700 hover:bg-brand-50">
    <svg class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"/></svg>
    Datasheet (saved {{ saved.captured_at.strftime('%b %d, %Y') if saved.captured_at else '' }})
  </a>
  {% elif card and card.datasheet_searched_at and not card.datasheet_captured_at %}
  <span class="inline-flex items-center gap-1.5 text-xs text-gray-400">No datasheet found (will retry)</span>
  {% else %}
  <span class="inline-flex items-center gap-1.5 text-xs text-gray-400"
        hx-get="/v2/partials/search/dossier/datasheet-status?mpn={{ mpn|urlencode }}"
        hx-trigger="every 15s" hx-target="this" hx-swap="outerHTML">
    <svg class="h-3.5 w-3.5 animate-spin text-brand-400" viewBox="0 0 24 24" fill="none"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"></path></svg>
    Fetching datasheet…
  </span>
  {% endif %}
  {% if card and card.datasheet_url and not saved %}
  <a href="{{ card.datasheet_url }}" target="_blank" rel="noopener noreferrer" class="text-[11px] text-gray-400 hover:text-brand-600 underline">vendor link</a>
  {% endif %}
  {% if card and card.manufacturer %}
  <span class="inline-flex items-center rounded-lg border border-brand-200 px-3 py-1.5 text-xs font-medium text-gray-600">{{ card.manufacturer }}</span>
  {% endif %}
</div>
```

Note: this block must render even when `card.datasheet_url` is empty (so the fetching/none states show), so it is no longer guarded by `{% if card.datasheet_url or card.manufacturer %}`. Wrap it with `{% if card %}` instead.

- [ ] **Step 4: Add the poll-status endpoint**

In `app/routers/part_dossier.py`, add (mirrors the enrich-status 286 pattern):

```python
@router.get("/v2/partials/search/dossier/datasheet-status", response_class=HTMLResponse)
async def dossier_datasheet_status(
    request: Request,
    mpn: str = Query(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Poll target for the 'fetching datasheet…' chip. Returns the datasheet block;
    stops polling (HTTP 286) once a copy is stored or a search has been recorded."""
    card = _resolve_card(db, normalize_mpn_key(mpn))
    ctx = _ctx(request, user)
    ctx.update({"mpn": mpn.strip().upper(), "card": card})
    resp = template_response("htmx/partials/search/dossier_datasheet_block.html", ctx)
    if card is not None and (card.datasheet_captured_at or card.datasheet_searched_at):
        resp.status_code = 286
    return resp
```

Extract the datasheet `<div>` from Step 3 into a new partial `app/templates/htmx/partials/search/dossier_datasheet_block.html` and `{% include %}` it from `dossier_specs.html`, so both the specs render and the poll endpoint reuse the same markup.

- [ ] **Step 5: Run tests**

Run: `python3 -m pytest tests/test_part_dossier_router.py -q`
Expected: PASS (new + existing).

- [ ] **Step 6: Commit**

```bash
git add app/templates/htmx/partials/search/dossier_specs.html app/templates/htmx/partials/search/dossier_datasheet_block.html app/routers/part_dossier.py tests/test_part_dossier_router.py
git commit -m "feat(datasheet): dossier UI — stored copy, fetching + none-found states"
```

---

### Task 9: Docs + full-suite gate

**Files:**
- Modify: `docs/APP_MAP_INTERACTIONS.md` (dossier section — add the datasheet-capture flow)

- [ ] **Step 1: Document the flow**

Add a short subsection under the Part Dossier flow describing: triggers (search + RFQ), the capture pipeline (connector→web, MPN-in-PDF verify, OneDrive copy, 30-day negative cache), the `MaterialCardDatasheet` table, and the UI states.

- [ ] **Step 2: Run the full suite + pre-commit**

Run: `TESTING=1 python3 -m pytest tests/ -q -n auto`
Expected: all pass.
Run: `pre-commit run --files <all changed files>`
Expected: ruff/format/docformatter/mypy pass.

- [ ] **Step 3: Commit**

```bash
git add docs/APP_MAP_INTERACTIONS.md
git commit -m "docs(datasheet): APP_MAP — auto-datasheet capture flow"
```

---

## Self-Review

**Spec coverage:** triggers (search Task 7 / RFQ Task 7); connector→web finder (Task 6); MPN-in-PDF verify (Task 5/6); OneDrive storage (Task 4/6); `material_card_datasheets` + stamps (Task 1/2); 30-day negative cache (Task 6 gate); cardless-MPN rule (Task 6 `resolve_material_card` on verified hit only); UI three states (Task 8); migration (Task 2); pypdf dep (Task 3); docs (Task 9). All spec sections map to a task.

**Placeholder scan:** every code step has real code; the one soft spot is Task 7 Step 4's "actual list var name holding created requirements" — the implementer confirms the variable in `add_requirements`' loop (the research notes requirements are built in the loop at requirements.py:402-420). No TBD/TODO left.

**Type consistency:** `upload_bytes_to_onedrive` returns `{onedrive_item_id, onedrive_url, size_bytes}` — consumed verbatim in Task 6. `find_datasheet_url` returns `(url, source)` — destructured in Task 6. `capture_datasheet(mpn, user_id)` signature matches all trigger call sites in Task 7. `MaterialCardDatasheet` columns in Task 1 match the row construction in Task 6 and the migration in Task 2.

## Known edge (documented, YAGNI)

A cardless MPN whose hunt finds nothing cannot be negative-cached (no card to stamp), so re-searching it re-hunts. Acceptable: pre-go-live the catalog is empty; post-go-live real parts have cards (SFDC import) and RFQ'd parts always resolve a card. A future MPN-keyed negative-cache table can be added if cardless re-hunt cost ever matters.
