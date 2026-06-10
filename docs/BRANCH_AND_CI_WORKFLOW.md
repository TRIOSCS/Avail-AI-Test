# Branch & CI Workflow

The canonical rules for branches, the formatting gate, and keeping the repo
clean. Applies to humans and agents. The goal: **no stale-branch drift, nothing
deleted without a recoverable mark, a small and current branch set.**

---

## 1. The formatting gate is changed-files only

Both the local pre-push hook (`changed-files-on-push` in `.pre-commit-config.yaml`)
and CI (`.github/workflows/ci.yml`) run pre-commit against **only the files a
branch changed** since its `merge-base` with `origin/main`:

```bash
base=$(git merge-base origin/main HEAD)
pre-commit run --from-ref "$base" --to-ref HEAD
```

Consequences — follow these:

- **Never bundle "unrelated" drift.** If a reviewer sees reformatting of files
  the PR didn't intend to touch, that is a mistake to remove, not required scope.
  (This reverses the old `all-files` policy — see the superseded note in memory.)
- **A stale branch failing the gate means it predates this system** — rebase it
  onto `origin/main`; do not reformat the world to satisfy it.
- **Never `--no-verify` to dodge a real failure.** The gate now only flags files
  you actually changed, so a failure is yours to fix.
- Hooks are version-pinned (docformatter, ruff, mypy in `.pre-commit-config.yaml`).
  Bump them deliberately in their own PR, never incidentally.

## 2. Branch naming & lifecycle

**Naming:** `<type>/<short-kebab-desc>` where `<type>` ∈
`feat | fix | chore | docs | refactor | test`. Stacked work uses a numbered
suffix (`feat/spec-resolver-1-migration`, `-2-models`, …) and merges bottom-up.

**Lifecycle — keep branches short-lived:**

1. Branch off **current** `origin/main` (`git fetch && git switch -c <name> origin/main`).
2. Open a PR early; keep it focused and small.
3. If `main` moves under you, **merge `origin/main` into the branch** before merging
   (normal maintenance, not a drift band-aid). Do NOT rebase a pushed branch — the
   resulting force-push is hook-blocked; see CLAUDE.md "Git Discipline" for the
   append-only recovery recipe if a branch was already rebased.
4. Merge, then **delete the branch promptly** (local + remote).

Do not let branches accumulate. A branch with no open PR and no active work is a
cleanup candidate.

## 3. Quarantine before delete — nothing is lost

Unmerged work and stashes are **archived as tags before deletion**, never just
dropped:

```bash
git tag archive/<name> <branch-or-stash-ref>
git push --no-verify origin archive/<name>   # tags carry no code to lint
git branch -D <name>                         # or: git stash drop
```

Recover anytime:

```bash
git checkout -b <name> archive/<name>
```

`archive/*` tags are the permanent, pushed record. Merged branches don't need a
tag (they're already in `main`'s history) — just delete them.

## 4. Routine cleanup

Run the maintained tool to prune stale branches safely. It **never touches
branches with open PRs**, archives every unmerged branch as an `archive/*` tag
before deleting, and is **dry-run by default**:

```bash
scripts/branch-cleanup.sh              # preview (dry run)
scripts/branch-cleanup.sh --apply      # delete stale LOCAL branches + clear stashes
scripts/branch-cleanup.sh --apply --remote   # also delete stale REMOTE branches
```

Do this whenever the local branch list grows past the active set, and after a
batch of PRs merges.

For **worktrees**, use the companion guard (same dry-run-by-default contract). It
removes a worktree only when it is BOTH clean and merged into `origin/main`, and
**HOLDS** any worktree with uncommitted work or unmerged commits — so a name
collision or a still-active session can never lose WIP to automated cleanup:

```bash
scripts/worktree-guard.sh              # report SAFE vs HELD (dry run)
scripts/worktree-guard.sh --apply      # remove only the SAFE (clean + merged) ones
```

## 5. Keep the workspace clean

- **Working tree:** commit or quarantine untracked files; `git status` should be
  clean between tasks. Scratch/debug artifacts go in `/root/quarantine/` (outside
  the repo), never committed.
- **Worktrees:** remove agent/eval worktrees when done (`git worktree remove`);
  `git worktree list` should normally show only the main checkout.
- **Stashes:** don't let them pile up — archive (§3) and clear.

## 6. Schema / datetime note

All datetime columns use `UTCDateTime` (`app/database.py`), which stores and
returns **tz-aware UTC** (symmetric bind+result, maps to `TIMESTAMPTZ`). Write
aware UTC (`datetime.now(timezone.utc)`); never strip tzinfo to "match" a column.
New datetime columns: just use `UTCDateTime` (no `timezone=` needed).
