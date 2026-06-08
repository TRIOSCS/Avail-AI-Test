#!/usr/bin/env bash
#
# worktree-guard.sh — safe pruning of git worktrees (companion to branch-cleanup.sh).
#
# Why this exists: with multiple Claude sessions sharing one checkout, feature work
# is isolated in worktrees under .claude/worktrees/ (and friends). An automated
# cleanup once *nearly* deleted a worktree holding uncommitted WIP because its branch
# name collided with an already-merged one (`materials-filter-rework` vs the merged
# `materials-filter-tree`). This guard makes that impossible: a worktree is only
# eligible for removal when it is BOTH clean (no uncommitted tracked changes) AND its
# branch is fully merged into origin/main. Anything else is HELD and reported — never
# auto-removed. `git branch -d/-D` can't delete a worktree-attached branch anyway, so
# branch-cleanup.sh skips these; this script is the piece that handles the worktrees.
#
# Guarantees:
#   - A worktree with uncommitted TRACKED changes is NEVER removed.
#   - A worktree whose branch has commits not in origin/main is NEVER removed.
#   - The primary checkout is never touched.
#   - Dry-run by default; pass --apply to actually remove the SAFE ones.
#   - HOLD worktrees print the exact reason; forcing one is a deliberate MANUAL act
#     (`git worktree remove --force <path>`), intentionally not automated here.
#
# Usage:
#   scripts/worktree-guard.sh            # report only (default)
#   scripts/worktree-guard.sh --apply    # remove worktrees that are clean AND merged
#
set -uo pipefail

APPLY=0
for a in "$@"; do
  case "$a" in
    --apply) APPLY=1 ;;
    -h | --help) sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "unknown arg: $a" >&2; exit 2 ;;
  esac
done
run() { if [ "$APPLY" = 1 ]; then "$@"; else echo "  DRY-RUN: $*"; fi; }

cd "$(git rev-parse --show-toplevel)" || exit 1
git fetch origin -q --prune || true

# Branches with open PRs are off-limits (mirrors branch-cleanup.sh).
PROTECTED=$(gh pr list --state open --limit 100 --json headRefName -q '.[].headRefName' 2>/dev/null | sort -u || true)
protected() { [ -n "$PROTECTED" ] && grep -qxF "$1" <<<"$PROTECTED"; }

# Parse `git worktree list --porcelain` into "path<TAB>branch" lines, skipping the
# first (primary) worktree, which git always lists first.
mapfile -t WORKTREES < <(
  git worktree list --porcelain | awk '
    /^worktree /  { path=substr($0,10) }
    /^branch /    { sub(/^branch refs\/heads\//,""); print path "\t" $0 }
    /^detached$/  { print path "\t(detached)" }
  ' | tail -n +2
)

if [ "${#WORKTREES[@]}" -eq 0 ]; then
  echo "No linked worktrees. Nothing to do."
  exit 0
fi

held=0 safe=0
for line in "${WORKTREES[@]}"; do
  path=${line%%$'\t'*}
  branch=${line#*$'\t'}
  [ -d "$path" ] || { echo "stale worktree entry (path gone): $path — run 'git worktree prune'"; continue; }

  reasons=()
  # 1) uncommitted TRACKED changes (untracked files like node_modules don't block)
  if [ -n "$(git -C "$path" status --porcelain --untracked-files=no)" ]; then
    reasons+=("uncommitted tracked changes")
  fi
  # 2) commits not in origin/main (squash-merges land here too — held to be safe)
  if [ "$branch" != "(detached)" ] && [ -n "$(git -C "$path" log --oneline origin/main..HEAD 2>/dev/null)" ]; then
    n=$(git -C "$path" rev-list --count origin/main..HEAD 2>/dev/null || echo "?")
    reasons+=("$n commit(s) not in origin/main")
  fi
  # 3) open PR on the branch
  if [ "$branch" != "(detached)" ] && protected "$branch"; then
    reasons+=("open PR")
  fi

  if [ "${#reasons[@]}" -gt 0 ]; then
    held=$((held + 1))
    printf 'HOLD  %s [%s]\n      reason: %s\n' "$path" "$branch" "$(IFS='; '; echo "${reasons[*]}")"
    echo "      to remove anyway (loses uncommitted work): git worktree remove --force \"$path\""
  else
    safe=$((safe + 1))
    echo "SAFE  $path [$branch] — clean and merged into origin/main"
    run git worktree remove "$path"
    [ "$branch" != "(detached)" ] && run git branch -d "$branch"
  fi
done

echo "---"
echo "SAFE: $safe   HELD: $held   ($([ "$APPLY" = 1 ] && echo 'applied' || echo 'dry-run; pass --apply to remove SAFE ones'))"
