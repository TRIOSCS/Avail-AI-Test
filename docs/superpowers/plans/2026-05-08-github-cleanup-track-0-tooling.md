# Track 0 — Pre-flight Tooling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. These artifacts are LOCAL `.claude/` config, not committed to the repo (`.claude/settings.local.json` and friends are .gitignored). Verification is by execution, not by CI.

**Goal:** Build the four local skills (`/cascade`, `/recommit`, `/smoke-gate`, `/worktree-prune`), add the `main-is-RED` pre-push warn hook, and fix the over-broad `warn-destructive-git` hookify rule — so that Track 1 (the cascade-merge critical path) has the tools it requires before it starts.

**Architecture:** Each skill is a directory `.claude/skills/<name>/` containing a `SKILL.md` (YAML frontmatter: `name`, `description`, `disable-model-invocation: true`) plus optional helper scripts under the same directory. The `/cascade` skill is the most complex and ships an actual bash implementation in `.claude/skills/cascade/cascade.sh` (toposort by `baseRefName`, leaf-first merge, idempotent state at `.claude/state/cascade.json`, pre-rebase comment snapshot via `gh api`). `/recommit` is a one-liner. `/smoke-gate` exercises a throwaway Postgres 16 container against the alembic upgrade/downgrade chain. `/worktree-prune` diffs `ls .claude/worktrees/` against `git worktree list --porcelain` and confirms each removal interactively. The `main-is-RED` hook is a non-blocking PreToolUse hook installed inline in `.claude/settings.local.json` matched on `Bash` tool calls whose `command` matches `git push.*`. The `warn-destructive-git` rule is rewritten so the regex matches the *target ref* (`main`/`master`), not the bare `--force` flag, allowing legitimate force-pushes to feature branches in worktree workflows.

**Tech Stack:** Bash 5 + `gh` CLI + `jq` + `git` + Docker (for `/smoke-gate`'s ephemeral Postgres) + Claude Code skill/hook framework.

**Spec:** `docs/superpowers/specs/2026-05-08-github-cleanup-design.md` §0.1 – §0.6.

**Intentional decision — NO COMMITS:** Every file in this plan lives under `.claude/` which is `.gitignored` for local-only config (`.claude/settings.local.json`, `hookify.*.local.md`, `skills/`, `hooks/`, `state/`). Do not run `git add` against any file produced here. If git accidentally tracks one of them, untrack with `git rm --cached <file>` and add it to `.gitignore`.

---

## File Inventory

| File | Action | Purpose |
|---|---|---|
| `.claude/skills/cascade/SKILL.md` | Create | Skill frontmatter + invocation instructions for `/cascade` |
| `.claude/skills/cascade/cascade.sh` | Create | Toposort + leaf-first merge + idempotent state machine (spec §0.1) |
| `.claude/skills/recommit/SKILL.md` | Create | One-line `git commit` wrapper that re-stages auto-fixed files (spec §0.2) |
| `.claude/skills/smoke-gate/SKILL.md` | Create | Skill frontmatter + invocation instructions for `/smoke-gate` |
| `.claude/skills/smoke-gate/smoke-gate.sh` | Create | Ephemeral Postgres + alembic upgrade/downgrade chain (spec §0.3) |
| `.claude/skills/worktree-prune/SKILL.md` | Create | Skill frontmatter + invocation instructions for `/worktree-prune` |
| `.claude/skills/worktree-prune/worktree-prune.sh` | Create | Orphan-detect + interactive prune (spec §0.4) |
| `.claude/state/.gitkeep` | Create | Reserves the state directory used by `/cascade` |
| `.claude/settings.local.json` | Modify | Append `main-is-RED` PreToolUse Bash warn hook (spec §0.5) |
| `.claude/hookify.warn-destructive-git.local.md` | Modify | Rewrite regex to match target-ref (`main`/`master`) instead of bare `--force` (spec §0.6) |

---

## Task 1: `/cascade` skill — directory + SKILL.md scaffold

**Files:**
- Create: `.claude/skills/cascade/SKILL.md`
- Create: `.claude/state/.gitkeep`

- [ ] **Step 1: Create the cascade skill directory and the state directory**

```bash
mkdir -p /root/availai/.claude/skills/cascade
mkdir -p /root/availai/.claude/state
touch /root/availai/.claude/state/.gitkeep
```

Expected: directories exist, no errors.

```bash
ls -la /root/availai/.claude/skills/cascade /root/availai/.claude/state
```

Expected output: both directories listed; `.gitkeep` present in `state/`.

- [ ] **Step 2: Write `.claude/skills/cascade/SKILL.md`**

This file is the user-facing entry point — frontmatter follows the format from `.claude/skills/deploy/SKILL.md` (verified). The body tells the model exactly what `cascade.sh` does and when to invoke it, per spec §0.1.

```markdown
---
name: cascade
description: Toposort open PRs, merge ready leaves, rebase descendants, halt on conflict. Idempotent.
disable-model-invocation: true
---

Run `/cascade` when you need to merge a stack of feature PRs in dependency order without a human babysitting each step. See `docs/superpowers/specs/2026-05-08-github-cleanup-design.md` §0.1 for the design rationale.

Steps:

1. Run `bash /root/availai/.claude/skills/cascade/cascade.sh`
2. The script will:
   - Read state from `.claude/state/cascade.json` if present (resume mode)
   - Toposort all OPEN PRs by `baseRefName` (PRs targeting `main` or whose base was already merged are leaves)
   - For each leaf: snapshot unresolved review comments via `gh api repos/.../pulls/<n>/comments --jq '.[] | select(.in_reply_to_id == null)'`, rebase onto current `main`, force-push with lease, wait for green CI, run `gh pr merge --squash`, then update `.claude/state/cascade.json`
   - Halt on the first conflict and print the exact `next action` (resolve in worktree, push, re-run `/cascade`)
3. Report which PRs merged, which halted, and the next action.

The state file shape is `{"merged": [<pr-number>...], "halted_at": <pr-number-or-null>, "halt_reason": "<string-or-null>"}`. Re-running `/cascade` re-reads this and resumes from the next leaf.

DO NOT use this for the stacked PR pair #108/#109 — that pair has a hand-executed procedure in spec §1.2 because the leaf (#109) merges into a non-`main` base. `/cascade` only handles PRs whose base is `main`.
```

- [ ] **Step 3: Verify the SKILL.md is well-formed**

```bash
head -5 /root/availai/.claude/skills/cascade/SKILL.md
```

Expected: opening `---`, `name: cascade`, `description:` line, `disable-model-invocation: true`, closing `---`.

- [ ] **Step 4: Local-only — do NOT commit**

These files are under `.claude/` which is gitignored. Skip any `git add` step. If `git status` shows them as untracked, leave them untracked.

```bash
cd /root/availai && git check-ignore -v .claude/skills/cascade/SKILL.md
```

Expected: prints the matching `.gitignore` rule (typically `.gitignore:.claude/`). If it does NOT print a match, STOP and confirm `.claude/` is in `.gitignore` before continuing.

---

## Task 2: `/cascade` skill — implementation script

**Files:**
- Create: `.claude/skills/cascade/cascade.sh`
- Test: manual smoke run with `--dry-run` flag against current open PRs

- [ ] **Step 1: Write `cascade.sh` with toposort + leaf-merge loop + state file**

```bash
cat > /root/availai/.claude/skills/cascade/cascade.sh <<'CASCADE_SH'
#!/usr/bin/env bash
# cascade.sh — toposort open PRs, merge leaves, rebase descendants, halt on conflict.
# Called by: /cascade skill (.claude/skills/cascade/SKILL.md)
# Depends on: gh CLI authenticated, jq, git, working tree clean on main.
# State: .claude/state/cascade.json
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
STATE_FILE="$REPO_ROOT/.claude/state/cascade.json"
DRY_RUN="${1:-}"

# Load or initialize state
if [ -f "$STATE_FILE" ]; then
  MERGED=$(jq -r '.merged | join(" ")' "$STATE_FILE")
else
  MERGED=""
  echo '{"merged": [], "halted_at": null, "halt_reason": null}' > "$STATE_FILE"
fi

# Snapshot all open PRs
PRS_JSON=$(gh pr list --state open --limit 100 \
  --json number,baseRefName,headRefName,mergeStateStatus,mergeable,title)

echo "$PRS_JSON" | jq -r '.[] | "PR #\(.number): \(.headRefName) → \(.baseRefName) [\(.mergeStateStatus)]"'

# Compute leaves: PRs whose baseRefName is "main" AND not already in MERGED
LEAVES=$(echo "$PRS_JSON" | jq -r --arg merged "$MERGED" '
  [.[] | select(.baseRefName == "main")
       | select((.number | tostring) as $n | ($merged | split(" ") | index($n)) | not)
       | select(.mergeable == "MERGEABLE" and (.mergeStateStatus == "CLEAN" or .mergeStateStatus == "UNSTABLE"))]
  | .[] | .number')

if [ -z "$LEAVES" ]; then
  echo "No mergeable leaves found. Either all merged, all blocked, or all conflicting."
  echo "Halt reason candidates:"
  echo "$PRS_JSON" | jq -r '.[] | select(.baseRefName == "main") | "  PR #\(.number): mergeable=\(.mergeable) state=\(.mergeStateStatus)"'
  exit 0
fi

for PR_NUMBER in $LEAVES; do
  echo ""
  echo "=== Processing PR #$PR_NUMBER ==="

  HEAD_BRANCH=$(echo "$PRS_JSON" | jq -r --arg n "$PR_NUMBER" '.[] | select(.number == ($n | tonumber)) | .headRefName')

  # Step 1: Snapshot unresolved review comments BEFORE rebase (spec §0.1, silent-failure H7)
  SNAPSHOT_FILE="$REPO_ROOT/.claude/state/cascade-pr${PR_NUMBER}-comments.json"
  echo "Snapshotting unresolved comments to $SNAPSHOT_FILE"
  gh api "repos/{owner}/{repo}/pulls/$PR_NUMBER/comments" \
    --jq '[.[] | select(.in_reply_to_id == null)]' > "$SNAPSHOT_FILE" 2>/dev/null || echo "[]" > "$SNAPSHOT_FILE"

  if [ "$DRY_RUN" = "--dry-run" ]; then
    echo "DRY RUN: would rebase $HEAD_BRANCH onto main, push --force-with-lease, wait for CI, merge"
    continue
  fi

  # Step 2: Rebase onto current main
  git fetch origin main
  if ! git checkout "$HEAD_BRANCH" 2>/dev/null; then
    git fetch origin "$HEAD_BRANCH:$HEAD_BRANCH"
    git checkout "$HEAD_BRANCH"
  fi

  if ! git rebase origin/main; then
    git rebase --abort || true
    jq --arg pr "$PR_NUMBER" --arg reason "rebase conflict" \
      '.halted_at = ($pr | tonumber) | .halt_reason = $reason' \
      "$STATE_FILE" > "$STATE_FILE.tmp" && mv "$STATE_FILE.tmp" "$STATE_FILE"
    echo ""
    echo "HALT: PR #$PR_NUMBER has rebase conflict on $HEAD_BRANCH."
    echo "Next action: cd into worktree for $HEAD_BRANCH, run 'git rebase origin/main', resolve, push --force-with-lease, then re-run /cascade."
    exit 1
  fi

  # Step 3: Push with lease
  git push --force-with-lease origin "$HEAD_BRANCH"

  # Step 4: Wait for CI green on the new SHA
  echo "Waiting for CI to complete on PR #$PR_NUMBER..."
  for i in $(seq 1 60); do
    sleep 30
    STATE=$(gh pr view "$PR_NUMBER" --json mergeStateStatus,mergeable,statusCheckRollup \
      --jq '.statusCheckRollup | map(.conclusion) | unique')
    case "$STATE" in
      *FAILURE*|*CANCELLED*)
        jq --arg pr "$PR_NUMBER" --arg reason "CI failed after rebase" \
          '.halted_at = ($pr | tonumber) | .halt_reason = $reason' \
          "$STATE_FILE" > "$STATE_FILE.tmp" && mv "$STATE_FILE.tmp" "$STATE_FILE"
        echo "HALT: PR #$PR_NUMBER CI failed. Investigate at gh pr view $PR_NUMBER --web"
        exit 1
        ;;
      *SUCCESS*)
        if ! echo "$STATE" | grep -qE 'PENDING|null'; then
          echo "CI green on PR #$PR_NUMBER"
          break
        fi
        ;;
    esac
    if [ "$i" = "60" ]; then
      echo "TIMEOUT: CI did not complete in 30 minutes for PR #$PR_NUMBER"
      exit 1
    fi
  done

  # Step 5: Re-post unresolved comments to new SHAs (spec §0.1)
  COUNT=$(jq 'length' "$SNAPSHOT_FILE")
  if [ "$COUNT" -gt 0 ]; then
    echo "Re-posting $COUNT snapshotted comments as PR review remarks"
    BODY=$(jq -r '"Pre-rebase unresolved review comments (auto-restored by /cascade):\n\n" + ([.[] | "- @" + .user.login + " (" + .path + ":" + (.line // .original_line | tostring) + "): " + .body] | join("\n"))' "$SNAPSHOT_FILE")
    gh pr comment "$PR_NUMBER" --body "$BODY" || echo "WARN: could not re-post comments"
  fi

  # Step 6: Squash-merge
  gh pr merge "$PR_NUMBER" --squash --delete-branch=false

  # Step 7: Update state
  jq --arg pr "$PR_NUMBER" '.merged += [($pr | tonumber)]' \
    "$STATE_FILE" > "$STATE_FILE.tmp" && mv "$STATE_FILE.tmp" "$STATE_FILE"
  echo "MERGED: PR #$PR_NUMBER"

  # Step 8: After each merge, refresh PRS_JSON so newly-eligible leaves (descendants whose base just merged) are picked up
  git checkout main
  git pull origin main
  PRS_JSON=$(gh pr list --state open --limit 100 \
    --json number,baseRefName,headRefName,mergeStateStatus,mergeable,title)
done

echo ""
echo "=== /cascade complete ==="
jq '.' "$STATE_FILE"
CASCADE_SH
chmod +x /root/availai/.claude/skills/cascade/cascade.sh
```

- [ ] **Step 2: Smoke-test the dry-run path**

```bash
cd /root/availai && bash .claude/skills/cascade/cascade.sh --dry-run
```

Expected: prints the open-PR list, identifies leaves (PRs whose `baseRefName` is `main`), prints `DRY RUN: would rebase ...` for each. Does NOT push, does NOT call `gh pr merge`. Exits 0.

If output is empty (no leaves), expected for current repo state if all PRs are CONFLICTING — see spec §1.3 pre-flight which says #96 and #103 must have conflicts resolved before `/cascade` runs.

- [ ] **Step 3: Verify the state file shape on first invocation**

```bash
cat /root/availai/.claude/state/cascade.json
```

Expected: `{"merged": [], "halted_at": null, "halt_reason": null}` (or similar — JSON with these three keys).

- [ ] **Step 4: Verify the snapshot file is created on first dry run**

```bash
ls /root/availai/.claude/state/cascade-pr*-comments.json 2>/dev/null | head -3
```

Expected: at least one snapshot file per leaf PR. Each is valid JSON (verify with `jq '.' <file>`).

- [ ] **Step 5: Local-only — do NOT commit**

`.claude/state/` is gitignored alongside `.claude/`. Verify with `git check-ignore -v .claude/state/cascade.json`. Expected: prints the gitignore rule.

---

## Task 3: `/recommit` skill

**Files:**
- Create: `.claude/skills/recommit/SKILL.md`

This is the entire skill — no helper script. Per spec §0.2, the implementation is a one-liner: `git commit "$@" || (git add -u && git commit --no-edit)`. The skill exists so the user has a single named entry point that is explicit about WHY auto-restage happens (pre-commit hooks reformat files, the first commit fails, we re-stage and retry).

- [ ] **Step 1: Create directory and write SKILL.md**

```bash
mkdir -p /root/availai/.claude/skills/recommit
```

```markdown
---
name: recommit
description: git commit with explicit re-stage on pre-commit auto-fix failure. No hidden state.
disable-model-invocation: true
---

Run `/recommit` instead of `git commit` whenever you have pre-commit hooks that auto-fix files (ruff, ruff-format, docformatter). The first `git commit` will fail because the hook modified files; the second invocation re-stages those files via `git add -u` and retries with the same message.

This is the EXPLICIT replacement for the originally-proposed auto-restage hook (spec §0.2, silent-failure H5: an auto-hook would silently scoop up `git add -p` partial-stage hunks the user did not intend to commit). `/recommit` is opt-in per commit.

Run this exact command, passing through any args the user provides (e.g. `-m "msg"`, `--amend`, file paths):

```bash
git commit "$@" || (git add -u && git commit --no-edit)
```

If both attempts fail, surface the full git output to the user — do NOT swallow the error.
```

Write that file:

```bash
cat > /root/availai/.claude/skills/recommit/SKILL.md <<'RECOMMIT_MD'
---
name: recommit
description: git commit with explicit re-stage on pre-commit auto-fix failure. No hidden state.
disable-model-invocation: true
---

Run `/recommit` instead of `git commit` whenever you have pre-commit hooks that auto-fix files (ruff, ruff-format, docformatter). The first `git commit` will fail because the hook modified files; the second invocation re-stages those files via `git add -u` and retries with the same message.

This is the EXPLICIT replacement for the originally-proposed auto-restage hook (spec §0.2, silent-failure H5: an auto-hook would silently scoop up `git add -p` partial-stage hunks the user did not intend to commit). `/recommit` is opt-in per commit.

Run this exact command, passing through any args the user provides (e.g. `-m "msg"`, `--amend`, file paths):

```bash
git commit "$@" || (git add -u && git commit --no-edit)
```

If both attempts fail, surface the full git output to the user — do NOT swallow the error.
RECOMMIT_MD
```

- [ ] **Step 2: Verify**

```bash
head -5 /root/availai/.claude/skills/recommit/SKILL.md
```

Expected: opening `---`, `name: recommit`, `description:`, `disable-model-invocation: true`, closing `---`.

- [ ] **Step 3: Local-only — do NOT commit**

Same as Task 1 Step 4 — `.claude/skills/` is gitignored.

---

## Task 4: `/smoke-gate` skill — SKILL.md scaffold

**Files:**
- Create: `.claude/skills/smoke-gate/SKILL.md`

- [ ] **Step 1: Create directory and write SKILL.md**

```bash
mkdir -p /root/availai/.claude/skills/smoke-gate
```

```bash
cat > /root/availai/.claude/skills/smoke-gate/SKILL.md <<'SMOKE_MD'
---
name: smoke-gate
description: Ephemeral Postgres + alembic upgrade/downgrade/upgrade chain. Catches alembic chicken-and-egg.
disable-model-invocation: true
---

Run `/smoke-gate` before merging any PR that touches `alembic/versions/`. See spec §0.3.

The script:
1. Spins up `postgres:16` on host port 5433 via `docker run -d --rm`
2. Waits for the DB to accept connections
3. Runs `alembic upgrade head` → `alembic downgrade base` → `alembic upgrade head`
4. Tears down the container even on failure (trap EXIT)

This is exactly the chain that exposes the failure mode behind PRs #108/#109 (downgrade missing reverse op causes `alembic downgrade base` to fail).

Run:

```bash
bash /root/availai/.claude/skills/smoke-gate/smoke-gate.sh
```

Exit code 0 means the chain passes; non-zero means a migration is broken — surface the alembic stderr to the user.
SMOKE_MD
```

- [ ] **Step 2: Verify SKILL.md present and well-formed**

```bash
head -5 /root/availai/.claude/skills/smoke-gate/SKILL.md
```

Expected: opening `---`, frontmatter with `name`, `description`, `disable-model-invocation: true`, closing `---`.

---

## Task 5: `/smoke-gate` skill — implementation script

**Files:**
- Create: `.claude/skills/smoke-gate/smoke-gate.sh`
- Test: live run against the current `alembic/` state

- [ ] **Step 1: Write `smoke-gate.sh` with strict cleanup trap**

```bash
cat > /root/availai/.claude/skills/smoke-gate/smoke-gate.sh <<'SMOKE_SH'
#!/usr/bin/env bash
# smoke-gate.sh — ephemeral Postgres + alembic upgrade/downgrade/upgrade chain.
# Called by: /smoke-gate skill (.claude/skills/smoke-gate/SKILL.md)
# Depends on: docker, alembic available on PATH, repo root with alembic.ini
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

CONTAINER_NAME="smoke-gate-pg-$$"
HOST_PORT=5433
DB_USER=smoke
DB_PASS=smoke
DB_NAME=smoke

cleanup() {
  echo "Cleaning up container $CONTAINER_NAME"
  docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

echo "Starting ephemeral Postgres 16 on host port $HOST_PORT..."
docker run -d --rm \
  --name "$CONTAINER_NAME" \
  -e POSTGRES_USER="$DB_USER" \
  -e POSTGRES_PASSWORD="$DB_PASS" \
  -e POSTGRES_DB="$DB_NAME" \
  -p "$HOST_PORT:5432" \
  postgres:16 >/dev/null

echo "Waiting for Postgres to accept connections..."
for i in $(seq 1 30); do
  if docker exec "$CONTAINER_NAME" pg_isready -U "$DB_USER" -d "$DB_NAME" >/dev/null 2>&1; then
    break
  fi
  sleep 1
  if [ "$i" = "30" ]; then
    echo "FAIL: Postgres did not become ready in 30s"
    exit 1
  fi
done

export DATABASE_URL="postgresql://${DB_USER}:${DB_PASS}@localhost:${HOST_PORT}/${DB_NAME}"
export TESTING=1
export PYTHONPATH="$REPO_ROOT"

echo ""
echo "=== Step 1: alembic upgrade head ==="
alembic upgrade head

echo ""
echo "=== Step 2: alembic downgrade base ==="
alembic downgrade base

echo ""
echo "=== Step 3: alembic upgrade head (round-trip) ==="
alembic upgrade head

echo ""
echo "=== /smoke-gate PASS ==="
SMOKE_SH
chmod +x /root/availai/.claude/skills/smoke-gate/smoke-gate.sh
```

- [ ] **Step 2: Live-test the script**

```bash
cd /root/availai && bash .claude/skills/smoke-gate/smoke-gate.sh
```

Expected output (last line): `=== /smoke-gate PASS ===`, exit code 0.

If it fails on `alembic downgrade base`, that is the legitimate finding the skill is designed to catch — record the failing revision and check whether it matches the spec's reference to PR #108/#109's chicken-and-egg.

- [ ] **Step 3: Verify cleanup happens on failure**

Manually break the chain to confirm the trap fires:

```bash
cd /root/availai && (bash .claude/skills/smoke-gate/smoke-gate.sh; echo "exit=$?") &
SMOKE_PID=$!
sleep 5
kill -INT $SMOKE_PID 2>/dev/null || true
wait $SMOKE_PID 2>/dev/null || true
docker ps --filter "name=smoke-gate-pg-" --format '{{.Names}}'
```

Expected: empty output from the last `docker ps` command — the trap removed the container even though we SIGINT'd the script.

- [ ] **Step 4: Local-only — do NOT commit**

Same as previous tasks.

---

## Task 6: `/worktree-prune` skill — SKILL.md scaffold

**Files:**
- Create: `.claude/skills/worktree-prune/SKILL.md`

- [ ] **Step 1: Create directory and write SKILL.md**

```bash
mkdir -p /root/availai/.claude/skills/worktree-prune
cat > /root/availai/.claude/skills/worktree-prune/SKILL.md <<'PRUNE_MD'
---
name: worktree-prune
description: Detect orphan .claude/worktrees/ dirs, cross-check PR state, interactive remove.
disable-model-invocation: true
---

Run `/worktree-prune` to find orphans in `.claude/worktrees/` (dirs that exist on disk but are NOT in `git worktree list`). Per spec §0.4.

The script:
1. Computes `comm -23 <(ls .claude/worktrees/) <(git worktree list --porcelain | awk '/^worktree/ {print $2}' | sed 's|.*/||')`
2. For each orphan, prints the dir + the associated branch state (from `gh pr view <branch>` if recoverable from `.git/HEAD` inside the dir)
3. Asks the user to confirm each removal (`y/n/q`)
4. Removes confirmed orphans via `git worktree remove --force <path>` if git knows about them, or plain `rm -rf <path>` if it does not

Run:

```bash
bash /root/availai/.claude/skills/worktree-prune/worktree-prune.sh
```

Re-run safely — script is idempotent (no-op on dirs already cleaned).
PRUNE_MD
```

- [ ] **Step 2: Verify**

```bash
head -5 /root/availai/.claude/skills/worktree-prune/SKILL.md
```

Expected: well-formed frontmatter.

---

## Task 7: `/worktree-prune` skill — implementation script

**Files:**
- Create: `.claude/skills/worktree-prune/worktree-prune.sh`
- Test: live run against the 7 known orphan dirs from spec §1.4

- [ ] **Step 1: Write `worktree-prune.sh`**

```bash
cat > /root/availai/.claude/skills/worktree-prune/worktree-prune.sh <<'PRUNE_SH'
#!/usr/bin/env bash
# worktree-prune.sh — detect orphan .claude/worktrees/ dirs and prompt for removal.
# Called by: /worktree-prune skill (.claude/skills/worktree-prune/SKILL.md)
# Depends on: git, gh CLI (optional; for branch state)
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

WORKTREE_DIR="$REPO_ROOT/.claude/worktrees"
if [ ! -d "$WORKTREE_DIR" ]; then
  echo "No .claude/worktrees/ directory present. Nothing to prune."
  exit 0
fi

# Set of directory names actually present on disk
ON_DISK=$(ls "$WORKTREE_DIR" 2>/dev/null | sort)

# Set of basenames git tracks
TRACKED=$(git worktree list --porcelain | awk '/^worktree/ {print $2}' | sed 's|.*/||' | sort)

# Orphans = on-disk minus tracked
ORPHANS=$(comm -23 <(echo "$ON_DISK") <(echo "$TRACKED"))

if [ -z "$ORPHANS" ]; then
  echo "No orphan worktree directories found."
  exit 0
fi

echo "Found orphan dirs in .claude/worktrees/:"
echo "$ORPHANS" | sed 's/^/  /'
echo ""

for dir in $ORPHANS; do
  full_path="$WORKTREE_DIR/$dir"
  echo "=== $full_path ==="

  # Try to discover the branch this dir was for
  if [ -f "$full_path/.git" ]; then
    head_file=$(grep '^gitdir:' "$full_path/.git" 2>/dev/null | awk '{print $2}')/HEAD
    branch=$(cat "$head_file" 2>/dev/null | sed 's|ref: refs/heads/||')
  else
    branch="(unknown)"
  fi

  echo "  branch: $branch"

  # Optional gh lookup
  if [ "$branch" != "(unknown)" ] && command -v gh >/dev/null 2>&1; then
    pr_state=$(gh pr list --head "$branch" --state all --json number,state --jq '.[0] | "PR #\(.number) \(.state)"' 2>/dev/null || echo "(no PR)")
    echo "  PR state: $pr_state"
  fi

  read -r -p "  Remove this orphan? [y/N/q] " ans
  case "$ans" in
    y|Y)
      if git worktree list --porcelain | grep -q "^worktree $full_path\$"; then
        git worktree remove --force "$full_path"
      else
        rm -rf "$full_path"
      fi
      echo "  removed."
      ;;
    q|Q)
      echo "  quitting."
      exit 0
      ;;
    *)
      echo "  skipped."
      ;;
  esac
done

# Final reconciliation
git worktree prune
echo ""
echo "Done. Remaining worktrees:"
git worktree list
PRUNE_SH
chmod +x /root/availai/.claude/skills/worktree-prune/worktree-prune.sh
```

- [ ] **Step 2: Live-test in dry-mode by piping `n` to all prompts**

```bash
cd /root/availai && yes n | bash .claude/skills/worktree-prune/worktree-prune.sh | head -40
```

Expected: lists the 7 known orphan dirs from spec §1.4 (`agent-a034b395`, `agent-a263dee1`, `agent-a37647f7`, `agent-a47a352b`, `agent-a4cf0049`, `agent-a6911920`, `agent-aff7e329`), prompts for each, and skips all (because we answered `n`). Final line: `Done. Remaining worktrees:` followed by `git worktree list` output. Exit code 0.

If the orphan list is different from the spec's 7 (e.g. some were already cleaned manually), record the actual list — do NOT mutate it. Track 1.4 will do the actual removal.

- [ ] **Step 3: Local-only — do NOT commit**

Same as previous tasks.

---

## Task 8: `main-is-RED` pre-push warn hook

**Files:**
- Modify: `.claude/settings.local.json` — append a new PreToolUse hook entry under the `Bash` matcher

- [ ] **Step 1: Read the current `settings.local.json` and identify the insertion point**

The current file has `permissions` and `hooks.PreToolUse` arrays. The new hook is a `PreToolUse` entry on `Bash` with command-pattern matching. Existing PreToolUse entries use `matcher: "Read|Edit|Write"` etc.; this new one uses `matcher: "Bash"` and inspects `$CLAUDE_TOOL_INPUT` to confirm the command starts with `git push`.

- [ ] **Step 2: Insert the hook block via Edit**

Add this new object to the `PreToolUse` array (alongside the existing three blocks). The hook is non-blocking — it always exits 0; it only writes a warn line to stderr (which Claude Code surfaces to the operator).

The exact command, written single-line for JSON:

```
case "$CLAUDE_TOOL_INPUT" in *git*push*) c=$(gh run list --branch main --limit 1 --json conclusion -q '.[0].conclusion' 2>/dev/null); [ "$c" = "failure" ] && echo "WARN: main CI is currently RED — your PR's mergeStateStatus will show UNSTABLE due to main, not your PR" >&2 ;; esac; exit 0
```

Append the following block to the `PreToolUse` array (becomes the fourth entry):

```json
{
  "matcher": "Bash",
  "hooks": [
    {
      "type": "command",
      "command": "case \"$CLAUDE_TOOL_INPUT\" in *git*push*) c=$(gh run list --branch main --limit 1 --json conclusion -q '.[0].conclusion' 2>/dev/null); [ \"$c\" = \"failure\" ] && echo \"WARN: main CI is currently RED — your PR's mergeStateStatus will show UNSTABLE due to main, not your PR\" >&2 ;; esac; exit 0",
      "statusMessage": "main-is-RED check"
    }
  ]
}
```

Use the Edit tool to insert this block immediately before the closing `]` of the `PreToolUse` array (after the existing third block ending with `"statusMessage": "Status string guard"`). Be careful to add a trailing comma to the previous block.

- [ ] **Step 3: Verify the JSON is still valid**

```bash
jq '.' /root/availai/.claude/settings.local.json > /dev/null && echo "JSON OK"
```

Expected: `JSON OK`. If `jq` errors, fix the trailing-comma / brace placement.

- [ ] **Step 4: Verify the hook fires by simulating `git push`**

```bash
CLAUDE_TOOL_INPUT="git push origin feature/test-branch" bash -c "$(jq -r '.hooks.PreToolUse | map(select(.matcher == "Bash")) | .[0].hooks[0].command' /root/availai/.claude/settings.local.json)"
echo "exit=$?"
```

Expected: `exit=0`. Stderr will contain the WARN line ONLY if `main` CI is currently red at the time of this command.

To force-test the warn path (regardless of real CI state), you can replace the `gh run list` call with `c=failure` for one shot:

```bash
CLAUDE_TOOL_INPUT="git push origin feature/test-branch" bash -c 'case "$CLAUDE_TOOL_INPUT" in *git*push*) c=failure; [ "$c" = "failure" ] && echo "WARN: main CI is currently RED — your PRs mergeStateStatus will show UNSTABLE due to main, not your PR" >&2 ;; esac; exit 0'
echo "exit=$?"
```

Expected: warn line on stderr, `exit=0`. Confirms the conditional branch logic.

- [ ] **Step 5: Local-only — do NOT commit**

`.claude/settings.local.json` is already gitignored per project convention. Verify with `git check-ignore -v .claude/settings.local.json`.

---

## Task 9: Fix `warn-destructive-git` hookify rule (target-ref matching)

**Files:**
- Modify: `.claude/hookify.warn-destructive-git.local.md`

Current pattern (verified by reading the file):

```
pattern: git\s+(push\s+--force|reset\s+--hard|checkout\s+\.|clean\s+-f|branch\s+-D)
```

Per spec §0.6, the change is to make `git push --force` block ONLY when targeting `main` or `master`, while still blocking the other unconditional destructive ops (`reset --hard`, `checkout .`, `clean -f`, `branch -D`). `--force-with-lease` to `main`/`master` must also be blocked.

- [ ] **Step 1: Replace the `pattern:` line with the target-ref version**

The new pattern uses an alternation across five sub-patterns. The first two cover the force-push variants targeting `main`/`master`; the remaining three are unchanged unconditional blocks.

```
pattern: (git\s+push\b[^\n]*--force\b[^\n]*\b(main|master)\b)|(git\s+push\b[^\n]*--force-with-lease[^\n]*\b(main|master)\b)|(git\s+reset\s+--hard)|(git\s+checkout\s+\.)|(git\s+clean\s+-f)|(git\s+branch\s+-D)
```

Use Edit to replace the existing `pattern:` line in `.claude/hookify.warn-destructive-git.local.md`:

- old: `pattern: git\s+(push\s+--force|reset\s+--hard|checkout\s+\.|clean\s+-f|branch\s+-D)`
- new: `pattern: (git\s+push\b[^\n]*--force\b[^\n]*\b(main|master)\b)|(git\s+push\b[^\n]*--force-with-lease[^\n]*\b(main|master)\b)|(git\s+reset\s+--hard)|(git\s+checkout\s+\.)|(git\s+clean\s+-f)|(git\s+branch\s+-D)`

- [ ] **Step 2: Update the message body to reflect the new semantics**

Replace the current message body so it correctly explains that force-push to feature branches is allowed:

- old:

```
**BLOCKED: Destructive git operation detected**

Force push, hard reset, and clean -f can cause irreversible data loss.
Ask the user for explicit confirmation before proceeding with destructive git operations.
```

- new:

```
**BLOCKED: Destructive git operation against a protected target**

This rule blocks:
- `git push --force` or `--force-with-lease` to `main`/`master`
- `git reset --hard`, `git checkout .`, `git clean -f`, `git branch -D` (unconditional)

Force-push to feature branches is allowed (legitimate worktree workflow).
Ask the user for explicit confirmation before proceeding.
```

- [ ] **Step 3: Verify the file's frontmatter is still well-formed**

```bash
head -8 /root/availai/.claude/hookify.warn-destructive-git.local.md
```

Expected: opening `---`, `name: warn-destructive-git`, `enabled: true`, `event: bash`, `pattern: ...` (the new long pattern on one line), `action: block`, closing `---`.

- [ ] **Step 4: Test the regex against the spec's intent — these MUST match (BLOCK)**

```bash
PATTERN='(git\s+push\b[^\n]*--force\b[^\n]*\b(main|master)\b)|(git\s+push\b[^\n]*--force-with-lease[^\n]*\b(main|master)\b)|(git\s+reset\s+--hard)|(git\s+checkout\s+\.)|(git\s+clean\s+-f)|(git\s+branch\s+-D)'
for cmd in \
  "git push --force origin main" \
  "git push origin main --force" \
  "git push --force-with-lease origin main" \
  "git push origin main --force-with-lease" \
  "git push --force origin master" \
  "git reset --hard HEAD~1" \
  "git checkout ." \
  "git clean -fd" \
  "git branch -D feature/foo"; do
    if echo "$cmd" | grep -Pq "$PATTERN"; then echo "BLOCK: $cmd"; else echo "MISS:  $cmd"; fi
done
```

Expected: every line prints `BLOCK: <cmd>`. Any `MISS:` line is a regression — fix the pattern.

- [ ] **Step 5: Test the regex against the ALLOW cases — these MUST NOT match**

```bash
PATTERN='(git\s+push\b[^\n]*--force\b[^\n]*\b(main|master)\b)|(git\s+push\b[^\n]*--force-with-lease[^\n]*\b(main|master)\b)|(git\s+reset\s+--hard)|(git\s+checkout\s+\.)|(git\s+clean\s+-f)|(git\s+branch\s+-D)'
for cmd in \
  "git push --force origin feature/cleanup" \
  "git push --force-with-lease origin chore/great-purge" \
  "git push --force origin docs/claude-md-rebuild" \
  "git push origin main" \
  "git push origin main --tags"; do
    if echo "$cmd" | grep -Pq "$PATTERN"; then echo "BLOCK: $cmd (REGRESSION)"; else echo "ALLOW: $cmd"; fi
done
```

Expected: every line prints `ALLOW: <cmd>`. Any `BLOCK: ... (REGRESSION)` is a false positive — fix the pattern.

- [ ] **Step 6: Local-only — do NOT commit**

`.claude/hookify.*.local.md` files are gitignored. Verify with `git check-ignore -v .claude/hookify.warn-destructive-git.local.md`.

---

## Task 10: End-to-end validation pass

**Files:** none modified — pure verification.

- [ ] **Step 1: Confirm all four skill directories + scripts exist and are executable**

```bash
test -f /root/availai/.claude/skills/cascade/SKILL.md \
  && test -x /root/availai/.claude/skills/cascade/cascade.sh \
  && test -f /root/availai/.claude/skills/recommit/SKILL.md \
  && test -f /root/availai/.claude/skills/smoke-gate/SKILL.md \
  && test -x /root/availai/.claude/skills/smoke-gate/smoke-gate.sh \
  && test -f /root/availai/.claude/skills/worktree-prune/SKILL.md \
  && test -x /root/availai/.claude/skills/worktree-prune/worktree-prune.sh \
  && echo "ALL TRACK 0 ARTIFACTS PRESENT"
```

Expected: `ALL TRACK 0 ARTIFACTS PRESENT`.

- [ ] **Step 2: Confirm settings.local.json contains the main-is-RED hook**

```bash
jq '.hooks.PreToolUse | map(select(.matcher == "Bash")) | length' /root/availai/.claude/settings.local.json
```

Expected: `1` (or higher if other Bash matchers exist; the new entry must be present).

```bash
jq -r '.hooks.PreToolUse | map(select(.matcher == "Bash")) | .[0].hooks[0].command' /root/availai/.claude/settings.local.json | grep -q "main is currently RED" && echo "HOOK INSTALLED"
```

Expected: `HOOK INSTALLED`.

- [ ] **Step 3: Confirm the warn-destructive-git pattern is the new target-ref version**

```bash
grep -Eq 'main\|master' /root/availai/.claude/hookify.warn-destructive-git.local.md && echo "PATTERN UPGRADED"
```

Expected: `PATTERN UPGRADED`.

- [ ] **Step 4: Confirm none of the Track 0 artifacts are tracked by git**

```bash
cd /root/availai && git status --porcelain .claude/skills/cascade .claude/skills/recommit .claude/skills/smoke-gate .claude/skills/worktree-prune .claude/state .claude/settings.local.json .claude/hookify.warn-destructive-git.local.md
```

Expected: empty output (all paths gitignored, nothing tracked, nothing to commit). If any path appears, verify `.gitignore` covers `.claude/`.

- [ ] **Step 5: Run the success-criteria check from spec §10 item 7**

```bash
test -d /root/availai/.claude/skills/cascade \
  && test -d /root/availai/.claude/skills/recommit \
  && test -d /root/availai/.claude/skills/smoke-gate \
  && test -d /root/availai/.claude/skills/worktree-prune \
  && echo "SPEC §10 ITEM 7 PASS"
```

Expected: `SPEC §10 ITEM 7 PASS`.

- [ ] **Step 6: Run the success-criteria check from spec §10 item 8**

```bash
grep -q "main is currently RED" /root/availai/.claude/settings.local.json \
  || test -x /root/availai/.claude/hooks/main-is-red-warn.sh \
  && echo "SPEC §10 ITEM 8 PASS"
```

Expected: `SPEC §10 ITEM 8 PASS`.

---

## Self-review against the spec

Each spec subsection mapped to the task that implements it:

| Spec § | Subject | Implementing task(s) |
|---|---|---|
| §0.1 | `/cascade` skill — toposort, leaf merge, idempotent state, comment snapshot | Task 1 (SKILL.md) + Task 2 (cascade.sh) |
| §0.2 | `/recommit` skill — explicit re-stage replacement for auto-restage hook | Task 3 |
| §0.3 | `/smoke-gate` skill — ephemeral Postgres + alembic chain | Task 4 (SKILL.md) + Task 5 (smoke-gate.sh) |
| §0.4 | `/worktree-prune` skill — orphan detect + interactive prune | Task 6 (SKILL.md) + Task 7 (worktree-prune.sh) |
| §0.5 | `main-is-RED` pre-push warn hook (PreToolUse Bash, non-blocking) | Task 8 |
| §0.6 | Fix `warn-destructive-git` hookify rule — target-ref matching | Task 9 |
| §10 item 7 (success criteria) | All four skill dirs exist | Task 10 Step 5 |
| §10 item 8 (success criteria) | `main-is-RED` hook installed | Task 10 Step 6 |

Silent-failure protections from §11 specifically addressed by Track 0:

- **H5 / C4 (auto-restage hook bypasses `git add -p`)** → Task 3 ships explicit `/recommit` skill instead of an auto-hook.
- **H6 / C8 (force-push whitelist by path was a band-aid)** → Task 9 rewrites pattern to match target-ref (`main`/`master`), which is the root-cause fix.
- **H7 (rebase silently buries unresolved review comments)** → Task 2 Step 1's `cascade.sh` snapshots unresolved comments to `.claude/state/cascade-pr<n>-comments.json` BEFORE rebase and re-posts them after force-push.

Build-order rule preserved: `/cascade`, `/recommit`, `/smoke-gate`, `/worktree-prune` all exist locally before Track 1 begins, so Track 1.1 (stabilize main) can use `/smoke-gate`, Track 1.3 (cascade-merge 11 PRs) can use `/cascade`, and Track 1.4 (worktree consolidation) can use `/worktree-prune`. The `main-is-RED` hook starts firing immediately, surfacing the `mergeStateStatus=UNSTABLE` confusion that Track 1 will encounter.
