# Self-Heal Pipeline Completion — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix the three infrastructure gaps blocking the self-heal pipeline (auth, patch validation, bug triage) so it can run autonomously.

**Architecture:** Add agent session endpoint for browser auth, add patch validation at three layers (generator, executor, applier), then run the pipeline to self-heal the 5 simpler bugs while manually fixing the 13 complex ones.

**Tech Stack:** FastAPI, SQLAlchemy, pytest, asyncio, Playwright MCP

---

### Task 1: Add agent session endpoint

**Files:**
- Modify: `app/routers/auth.py:310` (append after last route)
- Test: `tests/test_routers_auth.py` (append new test class)

**Step 1: Write the failing test**

Add to `tests/test_routers_auth.py`:

```python
class TestAgentSession:
    def test_agent_session_valid_key(self, auth_client, db_session, monkeypatch):
        """POST /auth/agent-session with valid key sets session cookie."""
        from app.config import settings
        monkeypatch.setattr(settings, "agent_api_key", "test-agent-key-123")

        # Seed agent user
        agent_user = db_session.query(User).filter_by(email="agent@availai.local").first()
        if not agent_user:
            agent_user = User(
                email="agent@availai.local",
                name="Agent",
                role="admin",
                azure_id="agent-internal",
            )
            db_session.add(agent_user)
            db_session.commit()

        resp = auth_client.post(
            "/auth/agent-session",
            headers={"x-agent-key": "test-agent-key-123"},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert "session" in resp.headers.get("set-cookie", "").lower()

    def test_agent_session_invalid_key(self, auth_client):
        """POST /auth/agent-session with wrong key returns 401."""
        resp = auth_client.post(
            "/auth/agent-session",
            headers={"x-agent-key": "wrong-key"},
        )
        assert resp.status_code == 401

    def test_agent_session_missing_key(self, auth_client):
        """POST /auth/agent-session without key returns 401."""
        resp = auth_client.post("/auth/agent-session")
        assert resp.status_code == 401

    def test_agent_session_no_agent_user(self, auth_client, db_session, monkeypatch):
        """POST /auth/agent-session when agent user doesn't exist returns 500."""
        from app.config import settings
        monkeypatch.setattr(settings, "agent_api_key", "test-agent-key-123")

        # Ensure no agent user exists
        db_session.query(User).filter_by(email="agent@availai.local").delete()
        db_session.commit()

        resp = auth_client.post(
            "/auth/agent-session",
            headers={"x-agent-key": "test-agent-key-123"},
        )
        assert resp.status_code == 500
```

**Step 2: Run test to verify it fails**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_routers_auth.py::TestAgentSession -v`
Expected: FAIL — 404 (endpoint doesn't exist)

**Step 3: Write the endpoint**

Add to `app/routers/auth.py` after the `auth_status` function (after line 309):

```python
@router.post("/auth/agent-session")
async def agent_session(request: Request, db: Session = Depends(get_db)):
    """Create a session for headless agent testing.

    Validates x-agent-key header, sets session cookie for the agent user.
    Called by: scripts/test-site.sh before launching Playwright agents.
    """
    agent_key = request.headers.get("x-agent-key")
    if not agent_key or not settings.agent_api_key or agent_key != settings.agent_api_key:
        return JSONResponse({"error": "Invalid agent key"}, status_code=401)

    agent_user = db.query(User).filter_by(email="agent@availai.local").first()
    if not agent_user:
        logger.error("agent-session: agent@availai.local user not found")
        return JSONResponse({"error": "Agent user not configured"}, status_code=500)

    request.session["user_id"] = agent_user.id
    return JSONResponse({"ok": True, "user_id": agent_user.id, "email": agent_user.email})
```

**Step 4: Run test to verify it passes**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_routers_auth.py::TestAgentSession -v`
Expected: 4 PASS

**Step 5: Commit**

```bash
git add app/routers/auth.py tests/test_routers_auth.py
git commit -m "feat: add POST /auth/agent-session endpoint for headless browser auth"
```

---

### Task 2: Add patch validation to patch_generator.py

**Files:**
- Modify: `app/services/patch_generator.py:112-114` (add validation before return)
- Test: `tests/test_patch_generator.py` (new file)

**Step 1: Write the failing tests**

Create `tests/test_patch_generator.py`:

```python
"""Tests for patch_generator — validates search string matching.

Called by: pytest
Depends on: app.services.patch_generator
"""

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app.services.patch_generator import generate_patches, validate_patches


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TestValidatePatches:
    def test_valid_patches_pass(self, tmp_path):
        """Patches with exact search strings pass validation."""
        src = tmp_path / "app" / "routers" / "test.py"
        src.parent.mkdir(parents=True)
        src.write_text("def hello():\n    return 'world'\n")

        patches = [
            {
                "file": "app/routers/test.py",
                "search": "def hello():\n    return 'world'\n",
                "replace": "def hello():\n    return 'fixed'\n",
                "explanation": "Fix return value",
            }
        ]
        ok, errors = validate_patches(patches, source_dir=tmp_path)
        assert ok is True
        assert errors == []

    def test_search_string_not_found_fails(self, tmp_path):
        """Patches with non-matching search strings fail validation."""
        src = tmp_path / "app" / "routers" / "test.py"
        src.parent.mkdir(parents=True)
        src.write_text("def hello():\n    return 'world'\n")

        patches = [
            {
                "file": "app/routers/test.py",
                "search": "def goodbye():\n    return 'world'\n",
                "replace": "def goodbye():\n    return 'fixed'\n",
                "explanation": "Fix return value",
            }
        ]
        ok, errors = validate_patches(patches, source_dir=tmp_path)
        assert ok is False
        assert len(errors) == 1
        assert "not found" in errors[0].lower()

    def test_file_not_found_fails(self, tmp_path):
        """Patches targeting non-existent files fail validation."""
        patches = [
            {
                "file": "app/routers/nonexistent.py",
                "search": "anything",
                "replace": "something",
                "explanation": "Fix",
            }
        ]
        ok, errors = validate_patches(patches, source_dir=tmp_path)
        assert ok is False
        assert "not found" in errors[0].lower() or "not a file" in errors[0].lower()

    def test_empty_patches_pass(self, tmp_path):
        """Empty patch list passes validation."""
        ok, errors = validate_patches([], source_dir=tmp_path)
        assert ok is True

    def test_multiple_patches_all_must_pass(self, tmp_path):
        """If any patch fails, entire validation fails."""
        src = tmp_path / "app" / "test.py"
        src.parent.mkdir(parents=True)
        src.write_text("line1\nline2\n")

        patches = [
            {"file": "app/test.py", "search": "line1", "replace": "fixed1", "explanation": "ok"},
            {"file": "app/test.py", "search": "MISSING", "replace": "fixed2", "explanation": "bad"},
        ]
        ok, errors = validate_patches(patches, source_dir=tmp_path)
        assert ok is False
        assert len(errors) == 1  # only the failing one


class TestGeneratePatchesValidation:
    @patch("app.services.patch_generator.claude_structured", new_callable=AsyncMock)
    @patch("app.services.patch_generator.SOURCE_DIR")
    def test_invalid_patches_rejected(self, mock_dir, mock_claude, tmp_path):
        """generate_patches returns None when Claude produces non-matching search strings."""
        src = tmp_path / "app" / "test.py"
        src.parent.mkdir(parents=True)
        src.write_text("real content here\n")

        mock_dir.__truediv__ = tmp_path.__truediv__
        mock_dir.__str__ = lambda s: str(tmp_path)

        mock_claude.return_value = {
            "patches": [
                {
                    "file": "app/test.py",
                    "search": "WRONG CONTENT",
                    "replace": "fixed",
                    "explanation": "Bad match",
                }
            ],
            "summary": "Fix stuff",
        }

        result = _run(generate_patches(
            title="Test bug",
            diagnosis={"root_cause": "Bug", "fix_approach": "Fix it"},
            category="api",
            affected_files=["app/test.py"],
        ))
        assert result is None

    @patch("app.services.patch_generator.claude_structured", new_callable=AsyncMock)
    @patch("app.services.patch_generator.SOURCE_DIR")
    def test_valid_patches_returned(self, mock_dir, mock_claude, tmp_path):
        """generate_patches returns result when patches are valid."""
        src = tmp_path / "app" / "test.py"
        src.parent.mkdir(parents=True)
        src.write_text("buggy code\n")

        mock_dir.__truediv__ = tmp_path.__truediv__
        mock_dir.__str__ = lambda s: str(tmp_path)

        mock_claude.return_value = {
            "patches": [
                {
                    "file": "app/test.py",
                    "search": "buggy code",
                    "replace": "fixed code",
                    "explanation": "Fixed the bug",
                }
            ],
            "summary": "Fix stuff",
        }

        result = _run(generate_patches(
            title="Test bug",
            diagnosis={"root_cause": "Bug", "fix_approach": "Fix it"},
            category="api",
            affected_files=["app/test.py"],
        ))
        assert result is not None
        assert len(result["patches"]) == 1
```

**Step 2: Run tests to verify they fail**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_patch_generator.py -v`
Expected: FAIL — `validate_patches` not defined

**Step 3: Add validate_patches function and integrate into generate_patches**

Add `validate_patches` function and validation call to `app/services/patch_generator.py`:

```python
def validate_patches(patches: list[dict], source_dir: Path | None = None) -> tuple[bool, list[str]]:
    """Validate that all patch search strings exist in their target files.

    Returns (ok, errors) where ok=True means all patches are valid.
    """
    base = source_dir or SOURCE_DIR
    errors = []

    for i, patch in enumerate(patches):
        rel_path = patch.get("file", "")
        search = patch.get("search", "")

        if not rel_path:
            errors.append(f"Patch [{i}]: missing 'file' field")
            continue

        target = base / rel_path
        if not target.is_file():
            errors.append(f"Patch [{i}]: file not found: {rel_path}")
            continue

        try:
            content = target.read_text(encoding="utf-8")
        except Exception as exc:
            errors.append(f"Patch [{i}]: cannot read {rel_path}: {exc}")
            continue

        if search and search not in content:
            preview = search[:80].replace("\n", "\\n")
            errors.append(
                f"Patch [{i}]: search string not found in {rel_path}: '{preview}...'"
            )

    return (len(errors) == 0, errors)
```

Then modify the end of `generate_patches()` to validate before returning. Replace lines 112-114:

```python
    patches = result.get("patches", [])
    if patches:
        ok, errors = validate_patches(patches)
        if not ok:
            for err in errors:
                logger.warning("patch_generator: validation failed: {}", err)
            logger.warning("patch_generator: rejecting {} invalid patches for '{}'", len(patches), title)
            return None

    logger.info("patch_generator: generated {} patches for '{}'", len(patches), title)
    return result
```

**Step 4: Run tests to verify they pass**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_patch_generator.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add app/services/patch_generator.py tests/test_patch_generator.py
git commit -m "feat: add patch validation — reject patches with non-matching search strings"
```

---

### Task 3: Add pre-flight validation to apply_patches.py

**Files:**
- Modify: `scripts/apply_patches.py:67-101` (rewrite main() with pre-flight)

**Step 1: Write the test**

Create `tests/test_apply_patches.py`:

```python
"""Tests for scripts/apply_patches.py — patch application with pre-flight validation.

Called by: pytest
Depends on: scripts/apply_patches.py
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch as mock_patch

import pytest

# Import from scripts directory
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from apply_patches import apply_patch, validate_all_patches


class TestValidateAllPatches:
    def test_all_valid(self, tmp_path):
        """All patches with matching search strings pass pre-flight."""
        src = tmp_path / "test.py"
        src.write_text("old code\nmore code\n")

        patches = [
            {"file": "test.py", "search": "old code", "replace": "new code", "explanation": "fix"},
        ]
        with mock_patch("apply_patches.PROJ_DIR", tmp_path):
            ok = validate_all_patches(patches)
        assert ok is True

    def test_one_invalid_fails_all(self, tmp_path):
        """If any patch search string doesn't match, pre-flight rejects all."""
        src = tmp_path / "test.py"
        src.write_text("old code\n")

        patches = [
            {"file": "test.py", "search": "old code", "replace": "new code", "explanation": "ok"},
            {"file": "test.py", "search": "MISSING", "replace": "new", "explanation": "bad"},
        ]
        with mock_patch("apply_patches.PROJ_DIR", tmp_path):
            ok = validate_all_patches(patches)
        assert ok is False

    def test_missing_file_fails(self, tmp_path):
        """Patch targeting non-existent file fails pre-flight."""
        patches = [
            {"file": "nope.py", "search": "x", "replace": "y", "explanation": "fix"},
        ]
        with mock_patch("apply_patches.PROJ_DIR", tmp_path):
            ok = validate_all_patches(patches)
        assert ok is False

    def test_empty_patches_pass(self, tmp_path):
        """Empty patch list passes pre-flight."""
        with mock_patch("apply_patches.PROJ_DIR", tmp_path):
            ok = validate_all_patches([])
        assert ok is True


class TestApplyPatch:
    def test_successful_apply(self, tmp_path):
        """Patch with matching search string applies correctly."""
        src = tmp_path / "test.py"
        src.write_text("def foo():\n    return 1\n")

        patch = {"file": "test.py", "search": "return 1", "replace": "return 2", "explanation": "fix"}
        with mock_patch("apply_patches.PROJ_DIR", tmp_path):
            result = apply_patch(patch, 0)
        assert result is True
        assert "return 2" in src.read_text()

    def test_search_not_found(self, tmp_path):
        """Patch with non-matching search string fails."""
        src = tmp_path / "test.py"
        src.write_text("def foo():\n    return 1\n")

        patch = {"file": "test.py", "search": "MISSING", "replace": "new", "explanation": "fix"}
        with mock_patch("apply_patches.PROJ_DIR", tmp_path):
            result = apply_patch(patch, 0)
        assert result is False
        assert "return 1" in src.read_text()  # unchanged
```

**Step 2: Run tests to verify they fail**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_apply_patches.py -v`
Expected: FAIL — `validate_all_patches` not defined

**Step 3: Add validate_all_patches and pre-flight to apply_patches.py**

Add `validate_all_patches` function before `main()` in `scripts/apply_patches.py`:

```python
def validate_all_patches(patches: list[dict]) -> bool:
    """Pre-flight: verify all search strings exist before applying any patch.

    Returns True only if ALL patches would succeed.
    """
    if not patches:
        return True

    all_ok = True
    for i, patch in enumerate(patches):
        rel_path = patch.get("file", "")
        search = patch.get("search", "")

        if not rel_path or not search:
            print(f"  [{i}] PRE-FLIGHT FAIL  Missing 'file' or 'search'")
            all_ok = False
            continue

        target = PROJ_DIR / rel_path
        if not target.is_file():
            print(f"  [{i}] PRE-FLIGHT FAIL  File not found: {rel_path}")
            all_ok = False
            continue

        try:
            content = target.read_text(encoding="utf-8")
        except Exception as exc:
            print(f"  [{i}] PRE-FLIGHT FAIL  Cannot read {rel_path}: {exc}")
            all_ok = False
            continue

        if search not in content:
            preview = search[:80].replace("\n", "\\n")
            print(f"  [{i}] PRE-FLIGHT FAIL  Search string not found in {rel_path}")
            print(f"         Expected: '{preview}...'")
            all_ok = False

    return all_ok
```

Then update `main()` to call pre-flight before applying:

```python
def main() -> int:
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <fix-json-file>")
        return 2

    fix_path = Path(sys.argv[1])
    if not fix_path.is_file():
        print(f"ERR  Fix file not found: {fix_path}")
        return 2

    try:
        data = json.loads(fix_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(f"ERR  Cannot parse fix file: {exc}")
        return 2

    patches = data.get("patches", [])
    if not patches:
        print("WARN No patches in fix file")
        return 0

    ticket_id = data.get("ticket_id", "?")
    print(f"Applying {len(patches)} patch(es) for ticket #{ticket_id}")

    # Pre-flight: validate ALL patches before applying ANY
    print("Pre-flight validation...")
    if not validate_all_patches(patches):
        print("PRE-FLIGHT FAILED — aborting all patches (no files modified)")
        return 1

    all_ok = True
    for i, patch in enumerate(patches):
        if not apply_patch(patch, i):
            all_ok = False

    if all_ok:
        print(f"All {len(patches)} patch(es) applied successfully")
        return 0
    else:
        print("One or more patches failed")
        return 1
```

**Step 4: Run tests to verify they pass**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_apply_patches.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add scripts/apply_patches.py tests/test_apply_patches.py
git commit -m "feat: add pre-flight validation to apply_patches — all-or-nothing patch application"
```

---

### Task 4: Fix execution_service tests for current API

**Files:**
- Modify: `tests/test_execution_service.py:170-254` (update mock targets from `_run_fix` to `_generate_fix`)

The existing tests mock `_run_fix` which no longer exists — the current code uses `_generate_fix`. Also the success status changed from `awaiting_verification` to `fix_queued`.

**Step 1: Update the test mocks**

Replace `_run_fix` with `_generate_fix` and update expected statuses:

In `TestExecuteFixSuccess`:
- Change `@patch("app.services.execution_service._run_fix"` to `@patch("app.services.execution_service._generate_fix"`
- Change mock return from `{"success": True, "summary": "...", "branch": "...", "cost_usd": ...}` to `{"success": True, "patches": [{"file": "x", "search": "a", "replace": "b", "explanation": "c"}], "summary": "...", "cost_usd": ...}`
- Change expected status from `"awaiting_verification"` to `"fix_queued"`
- Patch `_write_fix_queue` to prevent actual file writes

In `TestExecuteFixFailure`:
- Change `@patch("app.services.execution_service._run_fix"` to `@patch("app.services.execution_service._generate_fix"`

**Step 2: Run tests to verify they pass**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_execution_service.py -v`
Expected: All PASS

**Step 3: Commit**

```bash
git add tests/test_execution_service.py
git commit -m "fix: update execution_service tests for current _generate_fix API"
```

---

### Task 5: Clear failed fix queue and re-run pipeline

**Files:**
- No code changes — operational task

**Step 1: Clear the failed queue**

```bash
mv /root/availai/fix_queue/failed/*.json /root/availai/fix_queue/failed/archive/ 2>/dev/null || true
mkdir -p /root/availai/fix_queue/failed/archive
```

**Step 2: Deploy the changes**

```bash
cd /root/availai
git push
docker compose up -d --build
```

**Step 3: Verify agent session works**

```bash
# Get agent API key from env
docker compose exec app python3 -c "from app.config import settings; print(settings.agent_api_key)"

# Test the endpoint
curl -X POST https://app.availai.net/auth/agent-session \
    -H "x-agent-key: <key>" -v
```

Expected: 200 OK with session cookie in Set-Cookie header

**Step 4: Run the test loop**

Either click "Find Trouble" in the UI or run:

```bash
cd /root/availai && bash scripts/test-site.sh
```

Expected: Agents authenticate, test all 17 areas. The 5 simpler bugs should generate validated patches that the watcher can apply.

**Step 5: Verify watcher processes fixes**

```bash
tail -f /var/log/avail/self_heal_watcher.log
ls fix_queue/  # Should see new JSON files
ls fix_queue/applied/  # Should see successfully applied files
```

---

### Task 6: Manual fix — pipeline scoring & test data

**Files:**
- Modify: `app/routers/pipeline.py` or `app/services/performance_service.py` — fix leaderboard sort
- Modify: Clean test data (11/12 quotes to same req)
- Modify: Fix inflated scores/pricing
- Modify: Fix needs-attention endpoint

**Step 1: Investigate specific issues from agent output**

Read: `scripts/test-results/20260306_083133/pipeline_output.txt`

Identify exact code locations for:
- Leaderboard `avail_rank` unsorted
- Inflated scores/pricing
- `needs-attention` always empty

**Step 2: Fix each issue with tests**

Write failing test → implement fix → verify → commit for each sub-issue.

**Step 3: Commit**

```bash
git commit -m "fix: pipeline scoring — sort leaderboard, fix inflated prices, populate needs-attention"
```

---

### Task 7: Manual fix — admin orphaned data cleanup

**Files:**
- Create: `alembic/versions/054_cleanup_orphaned_data.py` (data migration)

**Step 1: Investigate the orphans**

```sql
-- Run inside Docker:
-- Orphaned requirements (no parent requisition)
SELECT COUNT(*) FROM requirements WHERE requisition_id NOT IN (SELECT id FROM requisitions);

-- Orphaned sightings (no parent requirement or material card)
SELECT COUNT(*) FROM sightings WHERE requirement_id NOT IN (SELECT id FROM requirements);
```

**Step 2: Write Alembic data migration**

Create migration that deletes orphaned rows in a transaction with counts logged.

**Step 3: Test migration rollback**

```bash
docker compose exec app alembic upgrade head
docker compose exec app alembic downgrade -1
docker compose exec app alembic upgrade head
```

**Step 4: Commit**

```bash
git add alembic/versions/054_cleanup_orphaned_data.py
git commit -m "fix: clean up 39K orphaned requirements + 214K orphaned sightings"
```

---

### Task 8: Manual fix — vendor sort/filter

**Files:**
- Modify: `app/routers/vendors.py` — fix sort and tier filter
- Test: `tests/test_routers_vendors.py`

**Step 1: Investigate from agent output**

Read: `scripts/test-results/20260306_083133/vendors_output.txt`

Identify which sort fields and filter params are broken.

**Step 2: Fix with TDD**

Write failing tests for sort order and filter, then fix the endpoint logic.

**Step 3: Commit**

```bash
git commit -m "fix: vendor sort + tier filter — correct ORDER BY and WHERE clauses"
```

---

### Task 9: Manual fix — duplicate contacts + null ticket data

**Files:**
- Modify: `app/services/trouble_ticket_service.py` — backfill null risk_tier/category
- Database: Deduplicate 94 contact records

**Step 1: Backfill null ticket fields**

Write a startup backfill or one-time script:

```python
# In startup.py or as a one-time migration
tickets = db.query(TroubleTicket).filter(
    TroubleTicket.risk_tier.is_(None),
    TroubleTicket.category.is_(None),
).all()
for t in tickets:
    t.risk_tier = "low"
    t.category = "other"
db.commit()
```

**Step 2: Deduplicate contacts**

Investigate duplicates, merge or delete keeping the most complete record.

**Step 3: Commit**

```bash
git commit -m "fix: backfill 125 tickets with null risk_tier/category, deduplicate 94 contacts"
```

---

### Task 10: Run full suite + verify clean sweep

**Files:** None — verification only

**Step 1: Run full test suite**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v --tb=short 2>&1 | tail -20
```

Expected: All pass

**Step 2: Check coverage**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ --cov=app --cov-report=term-missing --tb=no -q 2>&1 | tail -10
```

**Step 3: Deploy and run Find Trouble**

```bash
docker compose up -d --build
# Wait for healthy, then click Find Trouble or run test-site.sh
```

Expected: Significantly fewer failures. Target: 0 new tickets for 2 consecutive rounds.

**Step 4: Commit and push**

```bash
git push
```
