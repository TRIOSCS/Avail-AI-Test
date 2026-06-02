#!/bin/bash
# Stop-gate: when Claude finishes a turn, run the EXACT same pre-commit gate CI
# runs (.github/workflows/ci.yml) against the files this branch changed vs
# origin/main — ruff, ruff-format, docformatter, the fixer hooks, and mypy (in
# pre-commit's own isolated venv, so it matches CI precisely and never invents
# failures CI would pass). "Claude says done" == "CI is green".
#
# Wired as a Stop hook with asyncRewake:true: on a real failure it exits 2, which
# wakes the model with the output so issues get fixed before the turn ends.
# Auto-fixers (docformatter/ruff --fix/whitespace) modify files and report
# failure on first pass; we re-run once so they settle silently and only a
# genuinely unfixable problem blocks. A loop-guard blocks at most LOOP_MAX times
# on the same failure, then steps aside (exit 0) so nothing can trap the session.
set -o pipefail

# --- Locate repo root (portable: env -> git -> script location) -------------
PROJECT_DIR="${CLAUDE_PROJECT_DIR:-}"
[ -z "$PROJECT_DIR" ] && PROJECT_DIR="$(git rev-parse --show-toplevel 2>/dev/null)"
[ -z "$PROJECT_DIR" ] && PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$PROJECT_DIR" || exit 0

STATE="${TMPDIR:-/tmp}/claude-stopgate-$(printf '%s' "$PROJECT_DIR" | cksum | cut -d' ' -f1)"
LOOP_MAX=2

base=$(git merge-base origin/main HEAD 2>/dev/null || git rev-parse HEAD 2>/dev/null) || exit 0

# --- Files changed vs base (committed + unstaged) + new untracked, still present
mapfile -t files < <(
  { git diff --name-only "$base"; git ls-files --others --exclude-standard; } \
    | sort -u \
    | grep -E '\.(py|html|j2|jinja|css|js|yaml|yml)$' \
    | while IFS= read -r f; do [ -f "$f" ] && echo "$f"; done
)
[ "${#files[@]}" -eq 0 ] && { rm -f "$STATE"; exit 0; }

# --- Run CI's pre-commit gate; re-run once so auto-fixers settle -------------
run_gate() { pre-commit run --files "${files[@]}" --hook-stage pre-commit 2>&1; }
out=$(run_gate); status=$?
if [ "$status" -ne 0 ]; then
  out=$(run_gate); status=$?   # let fixers that modified files settle
fi

# --- Loop guard: block at most LOOP_MAX times on the same failure ------------
if [ "$status" -eq 0 ]; then
  rm -f "$STATE"
  exit 0
fi

# Fingerprint ignores volatile lines (durations, file counts) so identical
# failures match across runs.
fp=$(printf '%s' "$out" | grep -vE 'duration:|checked [0-9]+ source file' | cksum | cut -d' ' -f1)
prev_fp=""; count=0
[ -f "$STATE" ] && { prev_fp=$(sed -n 1p "$STATE"); count=$(sed -n 2p "$STATE"); }
if [ "$fp" = "$prev_fp" ]; then count=$((count + 1)); else count=1; fi
printf '%s\n%s\n' "$fp" "$count" > "$STATE"

if [ "$count" -gt "$LOOP_MAX" ]; then
  rm -f "$STATE"
  echo "Stop-gate: the same failure persisted across $LOOP_MAX fix attempts —"
  echo "stepping aside (likely pre-existing or needs your call). Please review:"
  echo "$out" | tail -60
  exit 0
fi

echo "Stop-gate FAILED — pre-commit (CI-equivalent) found issues in changed files. Fix before completing:"
echo "$out" | tail -60
exit 2
