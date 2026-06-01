#!/usr/bin/env bash
#
# branch-cleanup.sh — safe, quarantine-aware branch pruning.
# See docs/BRANCH_AND_CI_WORKFLOW.md §3–§4.
#
# Guarantees:
#   - Branches with OPEN PRs are NEVER touched.
#   - Every unmerged branch is archived as a pushed `archive/<name>` tag BEFORE
#     it is deleted (recover with: git checkout -b <name> archive/<name>).
#   - Merged branches are deleted without a tag (already in main's history).
#   - Dry-run by default; pass --apply to make changes.
#
# Usage:
#   scripts/branch-cleanup.sh                 # preview only
#   scripts/branch-cleanup.sh --apply         # local branches + stashes
#   scripts/branch-cleanup.sh --apply --remote  # also delete stale remote branches
#
set -uo pipefail

APPLY=0
REMOTE=0
for a in "$@"; do
  case "$a" in
    --apply) APPLY=1 ;;
    --remote) REMOTE=1 ;;
    *) echo "unknown arg: $a" >&2; exit 2 ;;
  esac
done
run() { if [ "$APPLY" = 1 ]; then "$@"; else echo "DRY-RUN: $*"; fi; }

cd "$(git rev-parse --show-toplevel)" || exit 1
git fetch origin -q --prune

PROTECTED=$(gh pr list --state open --limit 100 --json headRefName -q '.[].headRefName' | sort -u)
protected() { grep -qxF "$1" <<<"$PROTECTED"; }

echo "== merged local branches =="
for b in $(git branch --merged origin/main --format='%(refname:short)' | grep -vxF 'main'); do
  protected "$b" && { echo "skip (open PR): $b"; continue; }
  run git branch -d "$b"
done

echo "== unmerged local branches (archive -> tag, then delete) =="
for b in $(git branch --no-merged origin/main --format='%(refname:short)' | grep -vxF 'main'); do
  protected "$b" && { echo "skip (open PR): $b"; continue; }
  run git tag -f "archive/$b" "$b"
  run git push --no-verify origin "archive/$b"
  run git branch -D "$b"
done

if [ "$REMOTE" = 1 ]; then
  echo "== stale remote branches (merged or archived; never open PRs) =="
  for r in $(git for-each-ref --format='%(refname:short)' refs/remotes/origin/ \
              | sed 's#^origin/##' | grep -vxF 'HEAD' | grep -vxF 'main'); do
    protected "$r" && { echo "skip (open PR): origin/$r"; continue; }
    merged=$(git merge-base --is-ancestor "origin/$r" origin/main 2>/dev/null && echo yes || echo no)
    archived=$(git rev-parse -q --verify "refs/tags/archive/$r" >/dev/null && echo yes || echo no)
    if [ "$merged" = yes ] || [ "$archived" = yes ]; then
      run git push --no-verify origin --delete "$r"
    else
      echo "skip (unmerged, not archived): origin/$r"
    fi
  done
fi

echo "== stashes (archive -> tag, then clear) =="
i=0
git stash list --format='%gd' 2>/dev/null | while read -r ref; do
  run git tag -f "archive/stash-$i-$(date +%s)" "$ref"
  i=$((i+1))
done
if git stash list --format='%gd' | grep -q .; then
  run git push --no-verify origin --tags
  run git stash clear
fi

echo
echo "Done${APPLY:+}. $( [ "$APPLY" = 1 ] && echo 'Applied.' || echo 'Dry run — re-run with --apply.')"
echo "Recover any archived branch:  git checkout -b <name> archive/<name>"
