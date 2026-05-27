# Track 3 — Final Automations Implementation Plan

> **For agentic workers:** Two automation upgrades from `2026-05-08-github-cleanup-design.md` §3.1 and §3.2. Run AFTER Track 1 completes (cascade merged, worktrees pruned). §3.3 (`/worktree-switch`) and §3.4 (`claude-md-auditor` subagent) are explicitly DROPPED in the spec — do NOT implement them. The auto-prune hook lands as a local edit to `.claude/settings.local.json` plus a new script — there is NO PR for hook changes (settings.local.json is gitignored / per-machine). The schedule entry is created via the `/schedule` skill against the user's harness, NOT committed to the repo.

## Goal

1. **§3.1** — When `gh pr merge` or `git merge` runs and the merged feature branch has a corresponding `.claude/worktrees/<slug>` directory, automatically run `git worktree remove --force "<slug>"`. Idempotent (no-op on miss / already-pruned).
2. **§3.2** — Schedule the existing `claude-md-management:claude-md-improver` skill to run weekly (Monday 09:00) via the `/schedule` skill so CLAUDE.md drift never reaches the level it did this session.

Success = (a) merging any feature PR via `gh pr merge` removes its worktree without manual cleanup; (b) `/schedule list` shows the weekly `claude-md-improver` routine.

## Architecture

- **§3.1 hook** is a local Bash script invoked by Claude Code's `PostToolUse` event for `Bash` tool calls. The hook reads JSON from stdin (Claude Code hook protocol) — see `.claude/hooks/status-string-guard.sh` and `.claude/hooks/skill-instructions-hook.sh` for existing examples in this repo. The script greps the tool's command + output for the merged branch name, derives the worktree slug as the final path component of the branch (verified convention: `fix/env-hygiene` → `env-hygiene`, `docs/pre-rollout-checklist` → `pre-rollout-checklist`), and removes the worktree if present. No-op if slug isn't a worktree, the directory was already removed, or the parsed branch is `main`/`master`.
- **§3.2 schedule** is a routine in the user's Claude Code harness, not a repo artifact. It uses the `/schedule` skill (NOT `/loop` — `/loop` runs in-session at human pace; `/schedule` runs via cron and persists across sessions). The cron expression `0 9 * * 1` fires every Monday at 09:00 local. The action invokes `claude-md-management:claude-md-improver`, which already exists in the user's plugin set and audits + updates CLAUDE.md files.

## Tech Stack

- Bash (POSIX-portable script, reads JSON from stdin via `jq`)
- Claude Code hooks (`PostToolUse` Bash matcher in `.claude/settings.local.json`)
- `git worktree remove --force` (idempotent on already-removed dirs because we guard with `[ -d ]`)
- `/schedule` skill (cron-style routine manager, separate from `/loop` which is in-session)
- Existing skill: `claude-md-management:claude-md-improver`

## PREREQUISITES

- **Track 1 complete.** Specifically:
  - 1.2 (stacked #108/#109 merged)
  - 1.3 (cascade pool of 11 PRs merged or closed)
  - 1.4 (orphan worktree dirs `rm -rf`'d, merged-branch worktrees `git worktree remove`'d)
- After 1.4, `git worktree list` and `ls .claude/worktrees/` agree. The auto-prune hook is meant to keep that invariant going forward — installing it before Track 1.4 would just race the manual cleanup.
- **Track 2B is optional but typical** — once `deleteBranchOnMerge: true` is on (spec §2B.1), the merged branch is gone from origin AND we've cleaned up the local worktree, closing the loop end-to-end. The hook is correct either way.

## File Structure

| Path | Action | Purpose |
|---|---|---|
| `.claude/hooks/auto-prune-worktree.sh` | Create | Reads merge-tool stdin, removes matching `.claude/worktrees/<slug>` (spec §3.1) |
| `.claude/settings.local.json` | Modify | Append PostToolUse Bash matcher entry pointing at the new script (spec §3.1 wiring) |

No repo-tracked file changes — `.claude/settings.local.json` is per-machine local config, not committed. The `/schedule` routine (§3.2) is harness state, also not in the repo. **There is no PR for this Track.**

---

## Task 1 — Build the auto-prune hook script

### 1.1 Verify worktree slug convention

Run once to confirm the convention before coding:

```bash
git -C /root/availai worktree list | awk '/\.claude\/worktrees\// {print $1, $NF}'
```

Expected output (current state) maps each worktree dir to its branch — every dir name equals the LAST `/`-segment of the branch:

```
.claude/worktrees/ci-unblock              [fix/ci-unblock-test-assertions]
.claude/worktrees/deploy-sh-harden        [fix/deploy-sh-harden]
.claude/worktrees/env-hygiene             [fix/env-hygiene]
.claude/worktrees/eslint-browser-globals  [fix/eslint-browser-globals]
.claude/worktrees/migration-001           [fix/ci-unblock-alembic-and-audit]
.claude/worktrees/pre-rollout-checklist   [docs/pre-rollout-checklist]
.claude/worktrees/spec-cleanup            [docs/github-cleanup-spec]
```

NOTE the `migration-001`/`ci-unblock` cases prove the slug is NOT always `basename(branch)` — operators sometimes pick a custom shortname. The hook therefore canonicalizes by trying BOTH `basename` and the literal full branch (with `/` → `-`), and falls back to listing `git worktree list --porcelain` to find a worktree whose branch line equals the merged ref. This avoids false misses on hand-named worktrees.

### 1.2 Create the script

Path: `/root/availai/.claude/hooks/auto-prune-worktree.sh`. Mark executable (`chmod +x` after writing).

```bash
#!/usr/bin/env bash
# auto-prune-worktree.sh — PostToolUse hook: remove .claude/worktrees/<slug>
# after `gh pr merge` or `git merge` of a feature branch.
# Implements docs/superpowers/specs/2026-05-08-github-cleanup-design.md §3.1.
# Called by: Claude Code PostToolUse hook (Bash matcher) wired in
#   .claude/settings.local.json. Depends on: jq, git, gh (no network calls).
# Idempotent: any failure path falls through to exit 0 — never blocks the
# parent tool call, never re-removes an already-gone worktree.
set -u

# Hook protocol: JSON from stdin with .tool_input.command and .tool_response.output.
input="$(cat)"
[ -z "$input" ] && exit 0

cmd="$(printf '%s' "$input" | jq -r '.tool_input.command // empty' 2>/dev/null)"
out="$(printf '%s' "$input" | jq -r '.tool_response.output // empty' 2>/dev/null)"
[ -z "$cmd" ] && exit 0

# Only act on merge-style invocations.
case "$cmd" in
  *"gh pr merge"*|*"git merge"*) ;;
  *) exit 0 ;;
esac

# Try to extract the merged branch name from the combined cmd+output blob.
# `gh pr merge` prints lines like:
#   ✓ Squashed and merged pull request #92 (fix/env-hygiene)
# `git merge --ff-only fix/env-hygiene` echoes the ref as an arg.
blob="$cmd"$'\n'"$out"
branch=""

# Pattern 1: gh pr merge output — branch in parens after PR #N
branch="$(printf '%s' "$blob" | grep -oE 'pull request #[0-9]+ \([^)]+\)' \
  | head -n1 | sed -E 's/.*\(([^)]+)\).*/\1/')"

# Pattern 2: explicit branch arg on `git merge` / `gh pr merge <branch>`
if [ -z "$branch" ]; then
  branch="$(printf '%s' "$cmd" \
    | grep -oE '(git merge( --ff-only| --no-ff| --squash)?| gh pr merge)( -[A-Za-z]+)*( [A-Za-z0-9._/-]+)' \
    | awk '{print $NF}' | tail -n1)"
fi

# Refuse to touch main/master, empty, or anything non-feature-looking.
case "$branch" in
  ""|main|master|HEAD|origin/*) exit 0 ;;
esac

# Locate repo root from the hook's CWD; bail quietly if not in a git repo.
repo_root="$(git rev-parse --show-toplevel 2>/dev/null)" || exit 0
worktrees_dir="$repo_root/.claude/worktrees"
[ -d "$worktrees_dir" ] || exit 0

# Resolve slug: prefer `git worktree list` mapping (handles hand-named dirs
# like .claude/worktrees/migration-001 → fix/ci-unblock-alembic-and-audit),
# fall back to basename(branch), then full-branch-with-slash-replaced.
slug=""
while IFS= read -r line; do
  case "$line" in
    "worktree "*) wt_path="${line#worktree }" ;;
    "branch refs/heads/$branch")
      case "$wt_path" in
        "$worktrees_dir"/*) slug="${wt_path#$worktrees_dir/}" ;;
      esac
      ;;
  esac
done < <(git -C "$repo_root" worktree list --porcelain)

[ -z "$slug" ] && slug="${branch##*/}"
target="$worktrees_dir/$slug"

# Idempotent: bail if directory already gone.
[ -d "$target" ] || exit 0

# Use --force so dirty trees don't block cleanup; merge implies the work shipped.
git -C "$repo_root" worktree remove --force "$target" 2>/dev/null || rm -rf "$target"
echo "auto-prune-worktree: removed $target (branch $branch)" >&2
exit 0
```

### 1.3 Mark executable

```bash
chmod +x /root/availai/.claude/hooks/auto-prune-worktree.sh
```

### 1.4 Test the hook in isolation (no merge required)

Simulate the hook protocol JSON via stdin and a fake worktree:

```bash
# Create a sandbox worktree the hook can prune.
git -C /root/availai worktree add /root/availai/.claude/worktrees/_hook-smoke -b chore/_hook-smoke

# Feed simulated `gh pr merge` JSON to the hook.
printf '%s' '{
  "tool_input": {"command": "gh pr merge 999 --squash"},
  "tool_response": {"output": "✓ Squashed and merged pull request #999 (chore/_hook-smoke)\n"}
}' | /root/availai/.claude/hooks/auto-prune-worktree.sh

# Verify removal.
test ! -d /root/availai/.claude/worktrees/_hook-smoke && echo "PRUNED OK"
git -C /root/availai branch -D chore/_hook-smoke 2>/dev/null
```

Expect `PRUNED OK` and a stderr line `auto-prune-worktree: removed ... (branch chore/_hook-smoke)`.

Then run a no-op smoke (already-removed dir): re-feed the same JSON; expect exit 0 and no stderr line.

Then a guard-rail smoke: feed `"command": "git merge main"` (branch = `main`); expect exit 0 with no action. Confirm by re-creating `_hook-smoke` and re-feeding — it should NOT be removed for `main`.

---

## Task 2 — Wire the hook into `.claude/settings.local.json`

### 2.1 Read the current file structure

The file already has a `hooks` object with `PreToolUse` and `PostToolUse` arrays. Each array entry is `{matcher, hooks: [{type, command, statusMessage}]}`. Confirm shape with:

```bash
jq '.hooks | keys' /root/availai/.claude/settings.local.json
jq '.hooks.PostToolUse' /root/availai/.claude/settings.local.json
```

Expected: `["PostToolUse","PreToolUse"]`, and `PostToolUse` is an array containing the existing Ruff-format and related-tests entries.

### 2.2 Append the new hook entry

Add ONE entry to `.hooks.PostToolUse`. Use `jq` so the file stays valid JSON:

```bash
tmp=$(mktemp)
jq '.hooks.PostToolUse += [{
  "matcher": "Bash",
  "hooks": [{
    "type": "command",
    "command": ".claude/hooks/auto-prune-worktree.sh",
    "statusMessage": "Auto-prune merged worktree"
  }]
}]' /root/availai/.claude/settings.local.json > "$tmp" && mv "$tmp" /root/availai/.claude/settings.local.json
```

`matcher: "Bash"` (NOT a regex like `gh pr merge.*`) — Claude Code hook matchers run against the tool NAME, and the script does its own command-string filtering inside. This is the same pattern used by all existing entries in the file.

### 2.3 Verify JSON validity

```bash
jq -e . /root/availai/.claude/settings.local.json >/dev/null && echo "JSON OK"
jq -e '.hooks.PostToolUse | map(.hooks[0].command) | index(".claude/hooks/auto-prune-worktree.sh")' \
  /root/availai/.claude/settings.local.json
```

Expect `JSON OK` and a non-null index (the position of the new entry — typically `2` since two PostToolUse Bash entries already exist).

### 2.4 End-to-end smoke

Open a fresh Claude Code session in `/root/availai`. From within Claude, run a Bash tool call like `gh pr merge --squash <some-test-pr>` (or replay 1.4's sandbox worktree + a no-op `git merge --ff-only chore/_hook-smoke main`). Observe the hook fires (transcript shows status message `Auto-prune merged worktree`) and the worktree is gone.

If smoke fails: hook errors in Claude Code never block the parent tool call, but check the harness log at `~/.claude/logs/` for `auto-prune-worktree:` lines and stderr from `jq` / `git`.

---

## Task 3 — Schedule `claude-md-improver` weekly

### 3.1 Confirm the right scheduler skill

The user's harness exposes both `/loop` (in-session recurring at human pace) and `/schedule` (cron-style remote routines that survive session end). For a weekly CLAUDE.md audit we want **`/schedule`** — `/loop` would die the moment the session closes. The `/schedule` skill description: *"Create, update, list, or run scheduled remote agents (routines) that execute on a cron schedule."*

### 3.2 Create the routine

Invoke the skill with this prompt verbatim (the harness parses cron + action from natural language):

```
/schedule create
  name: claude-md-weekly-audit
  cron: 0 9 * * 1
  action: Invoke the claude-md-management:claude-md-improver skill against /root/availai. Audit and improve all CLAUDE.md files. Apply fixes directly; open a PR titled "chore(docs): weekly CLAUDE.md drift sweep" if any file changed. Skip silently if no drift found.
```

Cron expression `0 9 * * 1` = Monday at 09:00 (server-local time). Weekly cadence per spec §3.2. The action body explicitly references the existing skill name `claude-md-management:claude-md-improver` so the routine doesn't reinvent the auditor (spec §3.4 explicitly DROPS a custom subagent).

### 3.3 Verify

```
/schedule list
```

Expect output containing a routine named `claude-md-weekly-audit` with cron `0 9 * * 1`. Then optionally:

```
/schedule run claude-md-weekly-audit
```

…to fire it once on demand. Confirm it invokes the `claude-md-improver` skill (transcript should show that skill's first-line banner) and either reports "no drift" or opens the described PR.

### 3.4 No file artifact

`/schedule` routines live in the harness's state, NOT in the repo. There is nothing to commit for §3.2.

---

## Self-review — spec coverage

| Spec section | Status | Where in this plan |
|---|---|---|
| §3.1 hook script at `.claude/hooks/auto-prune-worktree.sh` | Implemented | Task 1.2 |
| §3.1 PostToolUse Bash matcher | Implemented | Task 2.2 (matcher: "Bash"; command-string filter inside script) |
| §3.1 parses merged branch from `gh pr merge` output | Implemented | Task 1.2 (Pattern 1: `pull request #N (branch)`) |
| §3.1 parses merged branch from `git merge ...` arg | Implemented | Task 1.2 (Pattern 2: tail arg of cmd) |
| §3.1 maps branch → worktree slug | Implemented | Task 1.2 (worktree-list lookup; basename fallback) |
| §3.1 `git worktree remove --force` | Implemented | Task 1.2 (with `rm -rf` fallback) |
| §3.1 idempotent (no-op if dir absent) | Implemented | Task 1.2 (`[ -d "$target" ] \|\| exit 0`) |
| §3.1 idempotent (no-op on `main`/`master`) | Implemented | Task 1.2 (case branch refusal) |
| §3.1 test simulating stdin | Implemented | Task 1.4 |
| §3.2 use existing `claude-md-management:claude-md-improver` skill | Implemented | Task 3.2 (action references skill by full name) |
| §3.2 schedule via `/loop` or `/schedule` skill | Implemented | Task 3.1–3.2 (chose `/schedule` per skill description; rationale in 3.1) |
| §3.2 NO custom subagent | Honored | Plan only schedules existing skill; no new subagent file |
| §3.2 weekly cadence | Implemented | Task 3.2 (cron `0 9 * * 1`) |
| §3.3 `/worktree-switch` skill — DROPPED | Out of scope | Not implemented (per spec) |
| §3.4 `claude-md-auditor` subagent — DROPPED | Out of scope | Not implemented (per spec) |
| Track 1 prerequisite | Honored | PREREQUISITES section at top |
| Track 2B optional/typical | Noted | PREREQUISITES section at top |

Done when:

```bash
# 1. Hook script present and executable
test -x /root/availai/.claude/hooks/auto-prune-worktree.sh

# 2. Hook wired into settings.local.json
jq -e '.hooks.PostToolUse | map(.hooks[0].command) | any(. == ".claude/hooks/auto-prune-worktree.sh")' \
  /root/availai/.claude/settings.local.json

# 3. Smoke test (Task 1.4) passed: PRUNED OK + no-op replay + main guard
# 4. /schedule list shows claude-md-weekly-audit (Task 3.3)
```

…and a real `gh pr merge` of any future cascade-style feature PR removes its `.claude/worktrees/<slug>` automatically.

---

## Task 4: Final §10 Success-Criteria Verification

Closeout for the entire GitHub cleanup initiative. Track 3 is the last track to land, so verifying spec §10's full 10-item success-criteria list belongs here. Each command below is copied verbatim from spec §10 — run each and confirm the expected output before declaring the cleanup done.

**Files:**
- None (verification only)

- [ ] **Step 4.1: Run all 10 success criteria from spec §10**

Run from `/root/availai`:

```bash
cd /root/availai

# 1. Zero open PRs
gh pr list --state open --limit 1 --json number | jq 'length == 0'
# Expected: true

# 2. Zero orphan worktree dirs
[ -z "$(comm -23 <(ls .claude/worktrees/ 2>/dev/null | sort) <(git worktree list --porcelain | awk '/^worktree/ {print $2}' | sed 's|.*/||' | sort))" ] && echo "ZERO ORPHANS" || echo "ORPHANS PRESENT"
# Expected: ZERO ORPHANS

# 3. main CI green (last 2 runs)
gh run list --branch main --limit 2 --json conclusion --jq 'all(.[]; .conclusion == "success")'
# Expected: true

# 4. Branch protection correctly applied (job names not workflow names; strict false; linear history)
gh api repos/TRIOSCS/Avail-AI-Test/branches/main/protection \
  --jq '.required_status_checks.contexts == ["test","security"]
        and .required_status_checks.strict == false
        and .required_linear_history.enabled == true
        and .enforce_admins.enabled == false'
# Expected: true

# 5. Repo merge settings
gh repo view TRIOSCS/Avail-AI-Test \
  --json mergeCommitAllowed,squashMergeAllowed,deleteBranchOnMerge \
  --jq '.mergeCommitAllowed == false and .squashMergeAllowed == true and .deleteBranchOnMerge == true'
# Expected: true

# 6. Templates + labels exist
test -f .github/PULL_REQUEST_TEMPLATE.md \
  && test -f .github/ISSUE_TEMPLATE/bug.md \
  && gh label list --json name --jq 'map(.name) | contains(["type:bug","priority:p0","scope:ci"])' \
  && echo "TEMPLATES+LABELS PASS"
# Expected: TEMPLATES+LABELS PASS

# 7. Skills exist locally
test -d .claude/skills/cascade \
  && test -d .claude/skills/recommit \
  && test -d .claude/skills/smoke-gate \
  && test -d .claude/skills/worktree-prune \
  && echo "SKILLS PASS"
# Expected: SKILLS PASS

# 8. main-is-RED hook installed
(grep -q "main is currently RED" .claude/settings.local.json \
  || test -x .claude/hooks/main-is-red-warn.sh) \
  && echo "RED-HOOK PASS"
# Expected: RED-HOOK PASS

# 9. CLAUDE.md drift fixed (spot-checks)
grep -q "claude-sonnet-4-6" CLAUDE.md \
  && grep -q "AVAIL_OPP_TABLE_V2" CLAUDE.md \
  && ! grep -q "MICROSOFT_GRAPH_ENDPOINT" CLAUDE.md \
  && echo "CLAUDE.MD-DRIFT PASS"
# Expected: CLAUDE.MD-DRIFT PASS

# 10. deploy.sh works under protection — VERIFIED MANUALLY in Track 2B Task 3 Step 9 (trial deploy)
echo "Item 10: confirmed by Track 2B trial deploy"
```

If any check fails, halt the closeout and trace back to the owning track:
- #1, #2 → Track 1.4
- #3 → Track 1.1 / Track 1.3
- #4, #5 → Track 2B Task 1
- #6 → Track 2A (template + labels) + Track 2B Task 2 (issue templates)
- #7 → Track 0 Tasks 1–4
- #8 → Track 0 Task 5
- #9 → Track 1.2 (§13 mini-PR)
- #10 → Track 2B Task 3

- [ ] **Step 4.2: Save closeout memory + update MEMORY.md index**

```bash
cat > /root/.claude/projects/-root/memory/project_github_cleanup_2026_05_08.md <<'EOF'
---
name: GitHub Cleanup 2026-05-08 Complete
description: Umbrella GitHub cleanup landed; success criteria verified; tracks 0/1/2A/2B/3 done
type: project
---

GitHub cleanup initiative completed 2026-05-08.

**Why:** 13 open PRs cascade-blocked behind alembic CI; main RED; no branch protection; 7 orphan worktrees; CLAUDE.md drift.

**How to apply:** Treat the umbrella spec at `docs/superpowers/specs/2026-05-08-github-cleanup-design.md` as the canonical record. Don't re-invent /cascade, /smoke-gate, /worktree-prune, /recommit — they live in `.claude/skills/`. Don't disable warn-destructive-git or the main-is-RED hook without good reason.

Success criteria all verified per spec §10 on completion.
EOF

echo "- [project_github_cleanup_2026_05_08.md](project_github_cleanup_2026_05_08.md) — 2026-05-08: full GitHub cleanup completed; all 10 success criteria verified" \
  >> /root/.claude/projects/-root/memory/MEMORY.md
```

**Cleanup done.**
