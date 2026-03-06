# Site Agent Test Runner — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** One-command launcher (`./scripts/test-site.sh`) that dispatches 15 parallel Claude Code subagents to browser-test all 17 areas of app.availai.net using Playwright, filing trouble tickets for any issues found.

**Architecture:** A bash dispatcher script launches `claude -p` processes (non-interactive Claude Code) in parallel, each given a focused test prompt for 1-2 areas. Each agent uses the Playwright MCP plugin to control a real headless Chromium browser. Agents authenticate to the API via `x-agent-key` header for ticket filing, and navigate the site via a session cookie obtained by hitting `/auth/login` with password auth. A run-history log tracks results across runs for diffing.

**Tech Stack:** Bash (dispatcher), Claude Code CLI (`claude -p`), Playwright MCP plugin, existing trouble ticket API (`POST /api/trouble-tickets`, `GET /api/trouble-tickets/similar`)

---

### Task 1: Agent Login Endpoint

The agents need a session cookie to browse the site. Password login already exists at `POST /auth/login`, but agents need a programmatic way to get a cookie without a browser form. Add an agent-session endpoint that returns a session cookie given the `x-agent-key`.

**Files:**
- Modify: `app/routers/auth.py`
- Test: `tests/test_auth.py`

**Step 1: Write the failing test**

In `tests/test_auth.py`, add:

```python
class TestAgentSession:
    def test_agent_session_returns_cookie(self, client, db_session):
        """Agent API key returns a valid session cookie."""
        from app.models import User
        # Create agent user
        agent = User(email="agent@availai.local", name="Agent", role="admin", azure_id="agent-bot", is_active=True)
        db_session.add(agent)
        db_session.commit()

        resp = client.post("/auth/agent-session", headers={"x-agent-key": "test-agent-key"})
        assert resp.status_code == 200
        assert "session" in resp.cookies

    def test_agent_session_rejects_bad_key(self, client):
        resp = client.post("/auth/agent-session", headers={"x-agent-key": "wrong"})
        assert resp.status_code == 401

    def test_agent_session_rejects_missing_key(self, client):
        resp = client.post("/auth/agent-session")
        assert resp.status_code == 401
```

**Step 2: Run test to verify it fails**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_auth.py::TestAgentSession -v`
Expected: FAIL with 404 (route doesn't exist)

**Step 3: Write minimal implementation**

In `app/routers/auth.py`, add after the existing routes:

```python
@router.post("/auth/agent-session")
async def agent_session(request: Request, db: Session = Depends(get_db)):
    """Create a session for the agent service user. Auth via x-agent-key header."""
    from .config import settings  # adjust import path as needed

    agent_key = request.headers.get("x-agent-key")
    if not agent_key or not settings.agent_api_key or agent_key != settings.agent_api_key:
        raise HTTPException(401, "Invalid agent key")

    agent_user = db.query(User).filter_by(email="agent@availai.local").first()
    if not agent_user:
        raise HTTPException(401, "Agent user not found")

    request.session["user_id"] = agent_user.id
    return {"ok": True, "user": agent_user.email}
```

Note: adjust imports to match existing patterns in auth.py (e.g. `from ..config import settings` vs `from .config`).

**Step 4: Run test to verify it passes**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_auth.py::TestAgentSession -v`
Expected: PASS

**Step 5: Commit**

```bash
git add app/routers/auth.py tests/test_auth.py
git commit -m "feat: agent session endpoint for headless browser auth"
```

---

### Task 2: Agent Prompt Files

Convert the existing `test_prompts.py` prompts into standalone text files that `claude -p` can consume. Enrich them with workflow steps (not just "click buttons" — actual feature testing), screenshot instructions, and ticket-filing instructions.

**Files:**
- Create: `scripts/agent-prompts/_base.md` (shared instructions all agents get)
- Create: `scripts/agent-prompts/search.md`
- Create: `scripts/agent-prompts/requisitions.md`
- Create: `scripts/agent-prompts/rfq.md`
- Create: `scripts/agent-prompts/crm_companies.md`
- Create: `scripts/agent-prompts/crm_contacts.md`
- Create: `scripts/agent-prompts/crm_quotes.md`
- Create: `scripts/agent-prompts/prospecting.md`
- Create: `scripts/agent-prompts/vendors.md`
- Create: `scripts/agent-prompts/tagging.md`
- Create: `scripts/agent-prompts/tickets.md`
- Create: `scripts/agent-prompts/admin_api_health.md`
- Create: `scripts/agent-prompts/admin_settings.md`
- Create: `scripts/agent-prompts/notifications.md`
- Create: `scripts/agent-prompts/auth.md`
- Create: `scripts/agent-prompts/upload.md`
- Create: `scripts/agent-prompts/pipeline.md`
- Create: `scripts/agent-prompts/activity.md`

**Step 1: Create the base prompt**

`scripts/agent-prompts/_base.md`:

```markdown
# Site Test Agent Instructions

You are testing the AvailAI application at {{BASE_URL}}.

## Authentication
1. First, navigate to the site using Playwright
2. The dispatcher has already set your session cookie — you are logged in

## When you find an issue
1. Take a screenshot with Playwright
2. Check the browser console for errors
3. File a trouble ticket by calling this curl command from Bash:

curl -s -X POST {{BASE_URL}}/api/trouble-tickets \
  -H "Content-Type: application/json" \
  -H "x-agent-key: {{AGENT_KEY}}" \
  -d '{
    "source": "agent",
    "tested_area": "{{AREA}}",
    "title": "SHORT DESCRIPTION OF THE ISSUE",
    "description": "DETAILED DESCRIPTION WITH STEPS TO REPRODUCE",
    "current_page": "THE URL WHERE YOU SAW THE ISSUE",
    "console_errors": "ANY CONSOLE ERRORS",
    "current_view": "{{AREA}}"
  }'

## Before filing a ticket
Check for duplicates first:

curl -s "{{BASE_URL}}/api/trouble-tickets/similar?title=YOUR+TITLE" \
  -H "x-agent-key: {{AGENT_KEY}}"

If similar ticket exists (similarity > 0.7), skip filing.

## When everything works
If the area passes all tests with no issues, report back with "PASS: {{AREA}} — all tests passed" and exit.

## Rules
- Use Playwright MCP to navigate and interact with the real site
- Take screenshots before and after each major action
- Do NOT click delete/remove/logout/destroy buttons
- Timeout: finish within 3 minutes per area
- If a page doesn't load within 15 seconds, file a ticket and move on
```

**Step 2: Create one area prompt per area**

Each file follows this pattern (example for `search.md`):

```markdown
# Test Area: Search & Sourcing

Navigate to: {{BASE_URL}}/#rfqs

## Workflow Tests

### Test 1: Basic Part Search
1. Find the search input field
2. Type "LM358" and click Search (or press Enter)
3. Wait for results to load (look for a results table or list)
4. VERIFY: Results appear from multiple sources
5. VERIFY: Each result shows vendor name, MPN, quantity, price
6. Take a screenshot of the results

### Test 2: Empty Search Handling
1. Clear the search field
2. Type "ZZZNOTAREALMPO999" and search
3. VERIFY: A "no results" or empty state message appears (not an error/crash)

### Test 3: Result Interaction
1. Search for "LM358" again
2. Click on the first result row to expand details
3. VERIFY: Detail view shows price, quantity, vendor, source badge
4. Take a screenshot

### Test 4: Search with Special Characters
1. Search for "LM358-N/NOPB"
2. VERIFY: No crash, results or "no results" message appears

## What Correct Looks Like
- Results load within 10 seconds with a progress indicator
- Each result row shows vendor, MPN, quantity, price, and source
- No JavaScript errors in the console
- Empty/invalid search shows a message, not a crash
```

Create similar files for all 17 areas, pulling content from the existing `AREA_PROMPTS` in `test_prompts.py` but enriched with specific workflow steps.

**Step 3: Verify files exist**

```bash
ls scripts/agent-prompts/*.md | wc -l
# Expected: 18 (17 areas + 1 base)
```

**Step 4: Commit**

```bash
git add scripts/agent-prompts/
git commit -m "feat: agent test prompt files for all 17 areas"
```

---

### Task 3: Dispatcher Script

The main script that orchestrates everything.

**Files:**
- Create: `scripts/test-site.sh`

**Step 1: Write the dispatcher**

`scripts/test-site.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

# ── Config ──────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
PROMPTS_DIR="$SCRIPT_DIR/agent-prompts"
RESULTS_DIR="$SCRIPT_DIR/test-results"
HISTORY_FILE="$SCRIPT_DIR/test-history.jsonl"
MAX_PARALLEL=15
TIMEOUT_SECS=300  # 5 min per agent

BASE_URL="${BASE_URL:-https://app.availai.net}"
AGENT_KEY="${AGENT_KEY:-$(grep AGENT_API_KEY "$PROJECT_DIR/.env" 2>/dev/null | cut -d= -f2)}"

# All 17 areas
ALL_AREAS=(search requisitions rfq crm_companies crm_contacts crm_quotes prospecting vendors tagging tickets admin_api_health admin_settings notifications auth upload pipeline activity)

# ── Parse args ──────────────────────────────────────────────────────
if [ $# -gt 0 ]; then
    if [ "$1" = "--help" ] || [ "$1" = "-h" ]; then
        echo "Usage: $0 [area1 area2 ...] [--after-deploy]"
        echo "       $0                    # test all 17 areas"
        echo "       $0 search crm_companies  # test specific areas"
        echo ""
        echo "Areas: ${ALL_AREAS[*]}"
        exit 0
    fi
    AREAS=("$@")
else
    AREAS=("${ALL_AREAS[@]}")
fi

# ── Pre-flight ──────────────────────────────────────────────────────
echo "╔══════════════════════════════════════════════════════╗"
echo "║  AvailAI Site Agent Test Runner                      ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""
echo "  Target:  $BASE_URL"
echo "  Areas:   ${#AREAS[@]}"
echo "  Parallel: $MAX_PARALLEL"
echo ""

# Health check
echo -n "  Health check... "
if curl -sf "$BASE_URL/health" > /dev/null 2>&1; then
    echo "✓ OK"
else
    echo "✗ FAILED — is the app running?"
    exit 1
fi

# Get agent session cookie
echo -n "  Agent auth... "
COOKIE_JAR=$(mktemp)
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
    -c "$COOKIE_JAR" \
    -X POST "$BASE_URL/auth/agent-session" \
    -H "x-agent-key: $AGENT_KEY")
if [ "$HTTP_CODE" = "200" ]; then
    SESSION_COOKIE=$(grep session "$COOKIE_JAR" | awk '{print $NF}')
    echo "✓ OK"
else
    echo "✗ FAILED (HTTP $HTTP_CODE) — check AGENT_API_KEY"
    rm -f "$COOKIE_JAR"
    exit 1
fi
rm -f "$COOKIE_JAR"

# Prepare results dir
RUN_ID=$(date +%Y%m%d_%H%M%S)
RUN_DIR="$RESULTS_DIR/$RUN_ID"
mkdir -p "$RUN_DIR"

echo ""
echo "  Run ID: $RUN_ID"
echo "  Results: $RUN_DIR"
echo ""
echo "─────────────────────────────────────────────────────────"

# ── Build agent prompts ─────────────────────────────────────────────
BASE_PROMPT=$(cat "$PROMPTS_DIR/_base.md" \
    | sed "s|{{BASE_URL}}|$BASE_URL|g" \
    | sed "s|{{AGENT_KEY}}|$AGENT_KEY|g")

# ── Launch agents ───────────────────────────────────────────────────
PIDS=()
declare -A PID_AREA

for area in "${AREAS[@]}"; do
    AREA_FILE="$PROMPTS_DIR/${area}.md"
    if [ ! -f "$AREA_FILE" ]; then
        echo "  ⚠ No prompt file for '$area' — skipping"
        continue
    fi

    AREA_PROMPT=$(cat "$AREA_FILE" | sed "s|{{BASE_URL}}|$BASE_URL|g" | sed "s|{{AREA}}|$area|g")
    FULL_PROMPT="$BASE_PROMPT

---

$AREA_PROMPT"
    FULL_PROMPT=$(echo "$FULL_PROMPT" | sed "s|{{AREA}}|$area|g")

    # Write prompt to file for claude -p
    PROMPT_FILE="$RUN_DIR/${area}_prompt.md"
    echo "$FULL_PROMPT" > "$PROMPT_FILE"

    # Launch claude in background
    echo "  ▶ Launching agent: $area"
    timeout "$TIMEOUT_SECS" claude -p "$(cat "$PROMPT_FILE")" \
        --allowedTools "mcp__plugin_playwright_playwright__*,Bash" \
        > "$RUN_DIR/${area}_output.txt" 2>&1 &
    PID=$!
    PIDS+=($PID)
    PID_AREA[$PID]=$area

    # Throttle: wait if at max parallel
    while [ $(jobs -r | wc -l) -ge $MAX_PARALLEL ]; do
        sleep 1
    done
done

# ── Wait and collect results ────────────────────────────────────────
echo ""
echo "  ⏳ Waiting for ${#PIDS[@]} agents to complete..."
echo ""

PASS=0
FAIL=0
ERROR=0
RESULTS=()

for pid in "${PIDS[@]}"; do
    area="${PID_AREA[$pid]}"
    if wait "$pid" 2>/dev/null; then
        EXIT_CODE=0
    else
        EXIT_CODE=$?
    fi

    OUTPUT="$RUN_DIR/${area}_output.txt"

    if [ $EXIT_CODE -eq 124 ]; then
        STATUS="TIMEOUT"
        ((ERROR++))
    elif grep -q "PASS: $area" "$OUTPUT" 2>/dev/null; then
        STATUS="PASS"
        ((PASS++))
    elif grep -q "trouble-ticket" "$OUTPUT" 2>/dev/null || grep -q "filed ticket" "$OUTPUT" 2>/dev/null; then
        STATUS="FAIL"
        ((FAIL++))
    elif [ $EXIT_CODE -ne 0 ]; then
        STATUS="ERROR"
        ((ERROR++))
    else
        STATUS="PASS"
        ((PASS++))
    fi

    RESULTS+=("$area:$STATUS")

    case $STATUS in
        PASS)    echo "  ✓ $area" ;;
        FAIL)    echo "  ✗ $area — issues found (see $OUTPUT)" ;;
        TIMEOUT) echo "  ⏰ $area — timed out after ${TIMEOUT_SECS}s" ;;
        ERROR)   echo "  ⚠ $area — agent error (exit $EXIT_CODE)" ;;
    esac
done

# ── Summary ─────────────────────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════════════════════"
echo "  RESULTS: $PASS pass, $FAIL fail, $ERROR error"
echo "  Output:  $RUN_DIR/"
echo "═══════════════════════════════════════════════════════"

# ── Append to history ───────────────────────────────────────────────
RESULTS_JSON=$(printf '%s\n' "${RESULTS[@]}" | jq -R -s 'split("\n") | map(select(. != "")) | map(split(":") | {area: .[0], status: .[1]})')
echo "{\"run_id\": \"$RUN_ID\", \"timestamp\": \"$(date -Iseconds)\", \"areas_tested\": ${#AREAS[@]}, \"pass\": $PASS, \"fail\": $FAIL, \"error\": $ERROR, \"results\": $RESULTS_JSON}" >> "$HISTORY_FILE"

# ── Diff from last run ──────────────────────────────────────────────
PREV_RUN=$(tail -2 "$HISTORY_FILE" 2>/dev/null | head -1)
if [ -n "$PREV_RUN" ] && [ "$(echo "$PREV_RUN" | jq -r '.run_id')" != "$RUN_ID" ]; then
    echo ""
    echo "  Changes from last run:"
    # Compare current vs previous results
    for r in "${RESULTS[@]}"; do
        area=$(echo "$r" | cut -d: -f1)
        status=$(echo "$r" | cut -d: -f2)
        prev_status=$(echo "$PREV_RUN" | jq -r ".results[] | select(.area == \"$area\") | .status" 2>/dev/null)
        if [ -n "$prev_status" ] && [ "$prev_status" != "$status" ]; then
            echo "    $area: $prev_status → $status"
        fi
    done
fi

exit $FAIL  # non-zero if any failures
```

**Step 2: Make executable**

```bash
chmod +x scripts/test-site.sh
```

**Step 3: Smoke test (dry run)**

```bash
./scripts/test-site.sh --help
```

Expected: prints usage without error.

**Step 4: Commit**

```bash
git add scripts/test-site.sh
git commit -m "feat: site agent test runner dispatcher (15 parallel agents)"
```

---

### Task 4: End-to-End Test Run

Not a code task — actually run the system and verify it works.

**Step 1: Ensure agent user exists**

```bash
docker compose exec app python -c "
from app.database import SessionLocal
from app.models import User
db = SessionLocal()
u = db.query(User).filter_by(email='agent@availai.local').first()
if not u:
    u = User(email='agent@availai.local', name='Test Agent', role='admin', azure_id='agent-bot', is_active=True)
    db.add(u)
    db.commit()
    print('Created agent user')
else:
    print(f'Agent user exists: id={u.id}, role={u.role}')
db.close()
"
```

**Step 2: Run with a single easy area first**

```bash
./scripts/test-site.sh auth
```

Verify: agent launches, navigates the site, reports PASS or files a ticket.

**Step 3: Run with 3 areas**

```bash
./scripts/test-site.sh search crm_companies vendors
```

Verify: 3 agents run in parallel, results appear.

**Step 4: Full 17-area run**

```bash
./scripts/test-site.sh
```

Verify: 15 agents launch, remaining 2 queue, all complete within 5 minutes.

**Step 5: Check tickets were filed**

```bash
curl -s https://app.availai.net/api/trouble-tickets?source=agent \
  -H "x-agent-key: $(grep AGENT_API_KEY .env | cut -d= -f2)" | python3 -m json.tool | head -30
```

**Step 6: Check run history**

```bash
cat scripts/test-history.jsonl | jq .
```

**Step 7: Commit any prompt tweaks**

```bash
git add scripts/
git commit -m "feat: site agent runner — verified end-to-end with 17 areas"
```

---

### Task 5: Post-Deploy Hook (Optional)

Wire the test runner to auto-run after `docker compose up -d --build`.

**Files:**
- Create: `scripts/post-deploy.sh`

**Step 1: Write the hook**

```bash
#!/usr/bin/env bash
# Post-deploy: rebuild, wait for health, then run site agents
set -euo pipefail

cd /root/availai

echo "=== Rebuilding ==="
docker compose up -d --build

echo "=== Waiting for health (up to 60s) ==="
for i in $(seq 1 12); do
    sleep 5
    if curl -sf http://localhost:8000/health > /dev/null 2>&1; then
        echo "Health check passed after $((i*5))s"
        echo ""
        echo "=== Running site agents ==="
        ./scripts/test-site.sh
        exit $?
    fi
    echo "  Attempt $i/12..."
done

echo "ERROR: Health check failed after 60s"
exit 1
```

**Step 2: Make executable and commit**

```bash
chmod +x scripts/post-deploy.sh
git add scripts/post-deploy.sh
git commit -m "feat: post-deploy hook — rebuild + health check + site agents"
```

---

## Summary

| Task | What | Files |
|------|------|-------|
| 1 | Agent login endpoint | `auth.py`, `test_auth.py` |
| 2 | 18 prompt files | `scripts/agent-prompts/*.md` |
| 3 | Dispatcher script | `scripts/test-site.sh` |
| 4 | End-to-end verification | Run + tweak |
| 5 | Post-deploy hook (optional) | `scripts/post-deploy.sh` |
