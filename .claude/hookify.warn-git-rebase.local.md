---
name: warn-git-rebase
enabled: true
event: bash
pattern: git\s+rebase\b
action: warn
---

⚠️ **Rebasing a pushed branch is a dead end here**

The `warn-destructive-git` hook **blocks all force-pushes** (including `--force-with-lease`), so a rebased
branch that was already pushed **cannot be pushed again**. Two review-fleet agents burned an hour on this
on 2026-06-10.

**Prefer:** `git fetch origin && git merge origin/main` (append-only, plain push works).

**Already rebased?** Recovery recipe (no force-push needed):
1. `git branch desired-final` (snapshot the rebased tree)
2. `git checkout -B <branch> origin/<branch>` (back to the pushed tip)
3. `git merge origin/main` (resolve conflicts)
4. `git diff HEAD desired-final | git apply --index` then commit — tree now identical to the rebased state
5. plain `git push`

(Rebasing a branch that was never pushed is fine — proceed.)
