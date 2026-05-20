# GitHub Cleanup & Optimization — Design

**Date:** 2026-05-08
**Status:** APPROVED — ready for writing-plans
**Repo:** TRIOSCS/Avail-AI-Test
**Context:** Single-user staging (app.availai.net), NOT multi-tenant prod — risk tolerance is higher than enterprise defaults; merge protections sized accordingly.

---

## Problem statement

The repo has accumulated structural drift that compounds friction on every change:

- **13 open PRs**, 8+ blocked behind a chicken-and-egg Alembic CI failure on `main`
- **2 PRs CONFLICTING** vs. main (#96, #103) — would fail any cascade rebase as-is
- **9 scattered worktrees** across `/tmp`, `/root/availai-*`, `.claude/worktrees/` — no convention
- **7 orphan worktree dirs** (`.claude/worktrees/agent-*`) not in `git worktree list`
- **`main` CI is RED** as of 2026-05-08 on `6890dcf5` — both `CI — Tests` and `Security Scanning` failing
- **No branch protection on main** (404 — fully open)
- **`deleteBranchOnMerge: false`** — branches accumulate after merge
- **No PR template, no issue templates, no issue labels**
- **CLAUDE.md drift**: 3 fix-now items + 4 NEW drift items vs. reality (pre-commit list, ANTHROPIC_MODEL 3-way mismatch, phantom env keys, missing real env keys)
- Repeated friction in this and prior sessions: pre-commit auto-fixes force re-stage, force-push hookify rule blocks worktree work, no orchestration for stacked-PR or cascade-merge flows

This design eliminates that drift in four parallelizable tracks, with corrections from a parallel review pass that caught **multiple silent-failure bugs in the original draft** (documented in §11).

---

## Goals & non-goals

### Goals (binary success criteria — see §10)
1. 0 open PRs (each merged or closed with rationale)
2. 0 orphan worktree dirs
3. `main` CI green and stays green
4. Branch protection on `main` with required `test` + `security` checks (NOT workflow names — see §6)
5. `deleteBranchOnMerge: true`, `allow_merge_commit: false`
6. PR template, issue templates, issue label taxonomy in place
7. `/cascade`, `/recommit`, `/smoke-gate`, `/worktree-prune` skills exist locally
8. `main-is-RED` pre-push warn hook active
9. CLAUDE.md drift items resolved
10. `deploy.sh` works under linear-history protection (verified by trial deploy)

### Non-goals (explicit, prevent scope creep)
- mypy 2080 errors / ESLint 9 problems / `test_sprint8_proactive` failure — separate code-quality initiative
- Multi-contributor concerns: CODEOWNERS enforcement, required reviews, signed commits — not relevant to single-user staging
- `/root/availai-health-fix` decision — flagged in §7, deferred to operator (this design does not delete it)
- Renaming the workflow from `CI — Tests` (em-dash) to ASCII — not load-bearing once we use job names for protection

---

## Architecture

Four tracks. Track 0 builds tools; Track 1 is the critical path; Tracks 2A/2B handle policy; Track 3 ships final automations.

```
Track 0 ──→ Track 1.1 ──→ Track 1.2 ──→ Track 1.3 ──→ Track 1.4 ──→ Track 2B ──→ Track 3
              (stabilize)   (stack)      (cascade)     (worktrees)   (protection)  (auto)

Track 2A ────────────────────────────────────────────→ (anytime; lands now)
```

**Key dependency rule:** Track 2B (`deleteBranchOnMerge` + branch protection) **must not land before Track 1 completes**. Reasoning:
- `deleteBranchOnMerge` while #108/#109 stack is open → orphans the dependent PR (silent-failure H2)
- `deleteBranchOnMerge` while worktrees still exist → destructive interaction (architecture C2)
- Branch protection with required checks while cascade PRs are red → indefinite block

---

## Track 0 — Pre-flight tooling

Local-only files in `.claude/skills/` and `.claude/hooks/`. No PRs. Run before Track 1 so Track 1 uses the tools.

### 0.1 `/cascade` skill — `.claude/skills/cascade/SKILL.md`
Toposort PRs by `baseRefName`, identify merge-ready leaves (state CLEAN + green), merge each, auto-rebase descendants, halt on conflict with explicit "next action" message. Snapshots unresolved review comments per `gh api repos/.../pulls/<n>/comments --jq '.[] | select(.in_reply_to_id == null)'` BEFORE rebase to prevent comment loss (silent-failure H7).

Idempotent: tracks last-merged PR in `.claude/state/cascade.json`; restart resumes from the next leaf.

### 0.2 `/recommit` skill — `.claude/skills/recommit/SKILL.md`
Replaces the originally-proposed auto-restage hook (silent-failure H5: hook would silently scoop up `git add -p` partial-stage hunks user didn't intend to commit).

Implementation: `git commit "$@" || (git add -u && git commit --no-edit)`. Explicit user invocation; no hidden auto-restage.

### 0.3 `/smoke-gate` skill — `.claude/skills/smoke-gate/SKILL.md`
Spin up ephemeral postgres (`docker run -d --rm -p 5433:5432 postgres:16`), run `alembic upgrade head` → `alembic downgrade base` → `alembic upgrade head` → step-down/step-up chain → teardown. Catches the exact failure mode that gave us the #108/#109 chicken-and-egg.

### 0.4 `/worktree-prune` skill — `.claude/skills/worktree-prune/SKILL.md`
Detect orphans: `comm -23 <(ls .claude/worktrees/) <(git worktree list --porcelain | awk '/^worktree/ {print $2}' | sed 's|.*/||')`. Cross-check `gh pr view <branch>` for state CLOSED/MERGED. Interactive confirm + `git worktree remove --force` + `rm -rf`.

### 0.5 `main-is-RED` pre-push warn hook
PreToolUse on Bash, matcher `git push.*` (excluding `--force` cases handled separately):
```bash
c=$(gh run list --branch main --limit 1 --json conclusion -q '.[0].conclusion' 2>/dev/null)
[ "$c" = "failure" ] && echo "WARN: main CI is currently RED — your PR's mergeStateStatus will show UNSTABLE due to main, not your PR" >&2
exit 0
```
Non-blocking. Solves the confusion we hit repeatedly this session.

### 0.6 Fix `warn-destructive-git` hookify rule
Current pattern blocks ALL `git push --force` (architecture C8 / silent-failure H6: rationalizing whitelist by path is band-aid; root cause is target-ref).

Replace with a target-ref-based pattern (single-line regex):

```
BLOCK pattern: git\s+push\b[^\n]*--force\b[^\n]*\b(main|master)\b
              git\s+push\b[^\n]*--force-with-lease[^\n]*\b(main|master)\b
ALLOW:        all other --force / --force-with-lease usages (i.e. to feature branches)
```

The two BLOCK alternatives are matched separately so `--force-with-lease` still flags when targeting `main`. Anchor `\b(main|master)\b` matches both `origin main` (space-separated) and `origin/main`.

This keeps the safety rail for the truly destructive case (force-push to main) while removing friction for legitimate worktree work.

---

## Track 1 — Critical path (sequential)

### 1.1 Stabilize main
- Pull failing run logs: `gh run view 25532626888 --log-failed`
- Diagnose root cause on `6890dcf5`
- Fix as direct-to-main hotfix (the ONLY direct-to-main exception in this design — branch protection isn't on yet)
- Verify with `/smoke-gate` if migration-related

### 1.2 Resolve stacked PRs (#109 → #108 → main)
PR #109 base = `fix/ci-unblock-alembic-and-audit` (PR #108's branch). PR #109 is currently fully green (test + security passing on `46ffc979`). PR #108 is red because it lacks #109's CI fixes.

Steps:
1. `gh pr merge 109 --squash` — squash-merges #109 INTO #108's branch
2. Wait for #108's CI to re-run on the new commits (now contains #109's fixes)
3. Verify #108 fully green
4. `gh pr merge 108 --squash` — squash-merges #108 → main
5. Verify main CI green
6. Ship CLAUDE.md fix-now mini-PR (small, direct PR — see §13)

### 1.3 Cascade-merge 11 PRs

**Pool:** #92, #93, #94, #96, #97, #100, #101, #102, #103, #106, #107

**Pre-flight (mandatory before /cascade runs):**
- Resolve conflicts on **#96** (`fix/ci-unblock-test-assertions`, mergeable=CONFLICTING) in its worktree
- Resolve conflicts on **#103** (`docs/claude-md-rebuild`, mergeable=CONFLICTING)
- Confirm no other PR has drifted to CONFLICTING since this audit

**Order** (deps → infra → features):
1. `#100` — dependabot/pip/minor-and-patch-ac6984c910 (FAST-TRACK: skip full agent review; verify CI passes + no major-version bumps + merge — architecture C7)
2. `#92` — fix/env-hygiene (4 lines, 1 file)
3. `#93` — fix/deploy-sh-harden (35 lines, 1 file) — **see §8.2 for required edit before merge**
4. `#94` — docs/pre-rollout-checklist (336 lines, 1 file, docs-only)
5. `#96` — fix/ci-unblock-test-assertions (post-conflict-resolution)
6. `#97` — fix/eslint-browser-globals (2 lines, 1 file)
7. `#101` — chore/great-purge-tracked-artifacts (2747 deletions, 361 files — biggest deletion PR; full review)
8. `#102` — fix/search-bg-session (867 lines, 8 files; full review)
9. `#103` — docs/claude-md-rebuild (post-conflict-resolution; coordinate with §13 fix-now PR — likely supersedes some items)
10. `#106` — fix/connector-hard-errors (13 lines, 2 files)
11. `#107` — chore/gradient-vestiges-and-docfmt (1347 lines, 32 files — biggest feature PR; full review)

**Per-PR procedure** (executed by `/cascade`):
1. Snapshot unresolved review comments via `gh api`
2. Rebase onto current `main`
3. Run pre-commit + tests + lint locally in worktree
4. Push (force-with-lease)
5. Re-post unresolved comments to new SHAs
6. Dispatch full agent review suite (skip for #100)
7. Address findings as fresh commits
8. Wait for green CI on the latest SHA
9. `gh pr merge --squash`
10. Move to next PR; if conflict, halt and surface

### 1.4 Worktree + remote-branch consolidation (uses `/worktree-prune`)

**Local worktrees:**
- `rm -rf` the 7 orphan dirs: `agent-{a034b395, a263dee1, a37647f7, a47a352b, a4cf0049, a6911920, aff7e329}`
- `git worktree remove` each `.claude/worktrees/<name>` whose branch was merged in 1.3
- `git worktree remove /tmp/pr109-rebase` (transient artifact from this session)
- Decide on `/root/availai-health-fix` (separate clone, branch `[origin/main: behind 4]`) — operator decision per §12

**Remote branches** (CRITICAL — Track 2B's `deleteBranchOnMerge` is NOT on during cascade, so 1.3's merged branches still exist on origin):
```bash
# Delete each cascade-merged branch from origin
gh pr list --state merged --base main --search "merged:>=2026-05-08" \
  --json number,headRefName --jq '.[].headRefName' | \
  while read branch; do
    [ -n "$branch" ] && gh api -X DELETE \
      "repos/TRIOSCS/Avail-AI-Test/git/refs/heads/$branch" 2>/dev/null || true
  done

# Local prune to reflect deletions
git fetch --prune origin
```

**Local branch cleanup** (only branches confirmed merged):
```bash
# Use git cherry to verify merge (silent-failure H4: -D after squash skips merge check)
for b in $(git branch -vv | grep ': gone\]' | awk '{print $1}'); do
  if [ -z "$(git cherry main "$b")" ]; then
    git branch -D "$b"
  else
    echo "SKIP $b: has un-squashed commits"
  fi
done
```

**Final state:** only `/root/availai` itself + any actively-used worktree under `.claude/worktrees/`. `git worktree list` and `ls .claude/worktrees/` agree.

---

## Track 2A — Land-now policy (parallel with Track 1)

These have no merge-blocking effect and can land any time. Each is its own small PR.

### 2A.1 PR template
Create `.github/PULL_REQUEST_TEMPLATE.md`:
```markdown
## Summary

<!-- 1-3 sentences on what changed and why -->

## Test plan

- [ ] Tests added/updated
- [ ] Pre-commit + ruff + mypy clean
- [ ] APP_MAP doc(s) in `docs/` updated if architecture changed

## Risk & rollback

<!-- Blast radius. How do we revert if this breaks main/staging? -->

## Linked issues

<!-- Closes #N (or N/A) -->
```

### 2A.2 Issue label taxonomy
Apply via `gh label create` (idempotent if `--force`):
- `type:bug`, `type:feature`, `type:chore`, `type:security`, `type:docs`
- `priority:p0`, `priority:p1`, `priority:p2`
- `scope:ci`, `scope:db`, `scope:frontend`, `scope:backend`, `scope:infra`

Then label existing #88 (`type:chore scope:frontend`), #89 (`type:chore scope:frontend`). Issues #83, #84 will close when their respective PRs merge.

### 2A.3 ~~`deleteBranchOnMerge: true`~~ → MOVED to Track 2B
Reason: H2 (orphans stacked #108/#109 if applied prematurely) + C2 (destructive worktree interaction).

### 2A.4 ~~Dependabot github-actions ecosystem~~ → REMOVED
Already exists at `.github/dependabot.yml:17-23`. Architecture C5: implementing as written would create duplicate keys → silent dependabot parse failure.

---

## Track 2B — Land-after-Track-1 policy

Order: Track 1.4 done → 2B starts. All in one configuration PR/script.

### 2B.1 Repo settings
```bash
gh repo edit TRIOSCS/Avail-AI-Test \
  --delete-branch-on-merge \
  --enable-merge-commit=false \
  --enable-squash-merge=true \
  --enable-rebase-merge=true
```

### 2B.2 Branch protection on `main`

**CRITICAL:** required check `contexts` are JOB names (`test`, `security`), NOT workflow names (`CI — Tests`, `Security Scanning`). Verified via `gh api repos/.../commits/main/check-runs --jq '.check_runs[].name'`. Using workflow names would silently never match anything → P0 silent-failure (parallel review caught this).

```bash
gh api -X PUT repos/TRIOSCS/Avail-AI-Test/branches/main/protection \
  -H "Accept: application/vnd.github+json" \
  --input - <<'JSON'
{
  "required_status_checks": {
    "strict": false,
    "contexts": ["test", "security"]
  },
  "enforce_admins": false,
  "required_pull_request_reviews": null,
  "required_signatures": false,
  "required_linear_history": true,
  "required_conversation_resolution": false,
  "allow_force_pushes": false,
  "allow_deletions": false,
  "block_creations": false,
  "restrictions": null
}
JSON
```

**Each setting justified:**
- `strict: false` — single-user, no concurrent-contributor races; `strict: true` would deadlock the cascade at ~2 hours sequential CI cycles (architecture C1)
- `contexts: ["test", "security"]` — actual GitHub-reported check-run names (P0 silent-failure prevention)
- `enforce_admins: false` — preserves emergency hotfix path (Track 1.1's pattern); single-user staging context
- `required_linear_history: true` — paired with `allow_merge_commit: false` from §2B.1, otherwise UI silently fails
- `required_pull_request_reviews: null` — single-user
- `allow_force_pushes: false` + `allow_deletions: false` — prevent main from being clobbered

### 2B.3 Issue templates
Create `.github/ISSUE_TEMPLATE/{bug.md,feature.md,security.md}` with standard fields.

### 2B.4 deploy.sh hardening
deploy.sh currently does `git push origin main` directly (line 21). Linear-history protection will reject non-fast-forward pushes. Two options, **pick exactly one**:

**Chosen: prepend `git pull --rebase origin main` before push** in deploy.sh. Rationale: PR #93 already proposes deploy.sh hardening; this addition rides into #93's scope. If #93 is closed/redirected during cascade, this fix lands as part of Track 2B's config PR instead.

Verify post-Track-2B with a trial deploy.

### 2B.5 ~~CODEOWNERS~~ → DROPPED
Without `require_code_owner_reviews: true`, CODEOWNERS is decorative (silent-failure M6). Single-user repo — drop. Revisit if multi-contributor.

---

## Track 3 — Final automations (after Track 1 done)

### 3.1 Auto-prune worktree on merge hook
`.claude/hooks/auto-prune-worktree.sh` triggered PostToolUse on Bash matching `gh pr merge.*` or `git merge.*` on a feature branch:
- Parse merged branch from output
- If `.claude/worktrees/<slug>` exists where slug derives from branch → `git worktree remove`
- Idempotent (no-op if already removed)

### 3.2 Schedule existing `claude-md-improver`
Use `claude-md-management:claude-md-improver` skill (already exists). Schedule via `/loop` or `/schedule` skill on a weekly cadence. **No custom subagent** — upstream covers it (architecture critique caught the duplicate).

### 3.3 ~~/worktree-switch skill~~ → SUBSUMED
Pruning was the real pain (7 orphans found). Navigation/copy-uncommitted is niche. Build only if proven needed post-Track-1.

### 3.4 ~~claude-md-auditor subagent~~ → DROPPED
Duplicate of upstream `claude-md-improver`. Use 3.2 instead.

---

## Failure modes & recovery

| Failure | Detection | Recovery |
|---|---|---|
| Cascade rebase introduces new conflict | `/cascade` halts on `git rebase` non-zero exit | Resolve in worktree → push → `/cascade` resumes from `state/cascade.json` |
| Cascade PR's CI fails on its own merits (not main-RED inheritance) | CI report shows failures unrelated to alembic/main-state | Investigate; fix in PR or close with rationale |
| `/cascade` dies mid-flight | State file shows incomplete | Idempotent restart from last green merge |
| Branch protection blocks deploy.sh push | First post-Track-2B deploy fails | §2B.4's `git pull --rebase` lands BEFORE protection (build-order rule) |
| Pre-rebase comment loss (H7) | New SHAs hide outdated comments | `/cascade` snapshots unresolved comments and re-posts after rebase |
| Stacked PR merge wrong order (silent-failure H2) | #108 merges before #109 → #109 orphaned | `/cascade` enforces leaf-first; 1.2 is hand-executed in spec order |
| `git worktree remove` fails because branch checked out | Non-zero exit | Halt; commit/stash work; retry |
| `gh repo edit --delete-branch-on-merge` fires while open stacked PR exists | Dependent PR auto-retargets to main with confused diff | Track 2B is gated on Track 1 done — by that point no stacked PRs exist |

---

## Success criteria — verifiable commands

```bash
# 1. Zero open PRs
gh pr list --state open --limit 1 --json number | jq 'length == 0'

# 2. Zero orphan worktree dirs
[ -z "$(comm -23 <(ls .claude/worktrees/ 2>/dev/null | sort) <(git worktree list --porcelain | awk '/^worktree/ {print $2}' | sed 's|.*/||' | sort))" ]

# 3. main CI green
gh run list --branch main --limit 2 --json conclusion --jq 'all(.[]; .conclusion == "success")'

# 4. Branch protection correctly applied
gh api repos/TRIOSCS/Avail-AI-Test/branches/main/protection \
  --jq '.required_status_checks.contexts == ["test","security"]
        and .required_status_checks.strict == false
        and .required_linear_history.enabled == true
        and .enforce_admins.enabled == false'

# 5. Repo merge settings
gh repo view TRIOSCS/Avail-AI-Test \
  --json mergeCommitAllowed,squashMergeAllowed,deleteBranchOnMerge \
  --jq '.mergeCommitAllowed == false and .squashMergeAllowed == true and .deleteBranchOnMerge == true'

# 6. Templates + labels exist
test -f .github/PULL_REQUEST_TEMPLATE.md \
  && test -f .github/ISSUE_TEMPLATE/bug.md \
  && gh label list --json name --jq 'map(.name) | contains(["type:bug","priority:p0","scope:ci"])'

# 7. Skills exist locally
test -d .claude/skills/cascade \
  && test -d .claude/skills/recommit \
  && test -d .claude/skills/smoke-gate \
  && test -d .claude/skills/worktree-prune

# 8. main-is-RED hook installed (either inline in settings.local.json OR as a script)
grep -q "main is currently RED" .claude/settings.local.json \
  || test -x .claude/hooks/main-is-red-warn.sh

# 9. CLAUDE.md drift fixed (spot-checks)
grep -q "claude-sonnet-4-6" CLAUDE.md \
  && grep -q "AVAIL_OPP_TABLE_V2" CLAUDE.md \
  && ! grep -q "MICROSOFT_GRAPH_ENDPOINT" CLAUDE.md

# 10. deploy.sh works under protection — VERIFY MANUALLY (trial deploy)
```

A track is "done" only when its block of these checks pass.

---

## Anti-scope

The following items came up in research and are **deliberately excluded**:

- **mypy 2080 errors / 144 files**: pre-existing tech debt; would be a multi-week initiative on its own
- **ESLint 9 problems**: same
- **`test_sprint8_proactive::test_badge_with_matches` failure**: unrelated, single test, separate fix
- **Renaming workflow `name:` from `CI — Tests` to ASCII**: not load-bearing once we use job names for protection (em-dash trap noted in §11)
- **Multi-contributor enforcement** (signed commits, required reviews, CODEOWNERS w/ enforcement): single-user staging
- **`/root/availai-health-fix` directory**: separate clone, not a worktree, branch is `[origin/main: behind 4]`. Likely abandoned but not provably so. Operator decides; this design does not delete it.
- **Auto-merge for dependabot PRs**: post-Track-2B nice-to-have, not in scope
- **Renaming branch convention**: 9 worktrees use various prefixes (fix/, chore/, docs/, feat/); not standardizing in this pass

---

## §11 — Bugs the parallel review caught in the original draft

This section preserves the silent-failure findings so writing-plans (and future sessions) understand WHY the design looks the way it does.

| ID | Severity | Bug | Fix in this design |
|---|---|---|---|
| C1 / M5 | P0 | `strict: true` + 11-PR cascade = 2hr deadlock | §2B uses `strict: false` |
| C3 | P0 | Required `Security Scanning` would block every PR (pre-existing pip/npm audit failures) | Drop as required; informational only via workflow |
| C5 | P0 | Dependabot github-actions ecosystem already exists; "add" would duplicate-key parse-fail | §2A.4 removed |
| C2 | P1 | `deleteBranchOnMerge` before worktree cleanup = destructive interaction | §2B.1 (was Track 2A) |
| C4 / H5 | P1 | Auto-restage hook bypasses `git add -p` user intent | Replaced by §0.2 `/recommit` skill |
| C6 | P1 | deploy.sh `git push origin main` blocked by linear-history protection | §2B.4 prepends `git pull --rebase` |
| C7 | P2 | Full agent review for dependabot #100 is overkill | §1.3 fast-tracks #100 |
| C8 / H6 | P1 | Force-push whitelist by path is band-aid | §0.6 ref-name based matching |
| H1 | P0 | Cascade reviews dispatched AFTER merge → tombstone | §1.3 reorders: review → fix → CI green → merge |
| H2 | P0 | `deleteBranchOnMerge` orphans stacked #108/#109 | §1.2 resolves stack BEFORE §2B fires |
| H3 | P0 | Workflow `pull_request: branches: [main]` doesn't fire on stacked PRs | Already fixed in PR #109's branch; lands with #108 merge |
| H4 | P1 | `git branch -D` after squash silently drops un-squashed commits | §1.4 explicit `git cherry` check before `-D`; remote branches deleted via `gh api` (deleteBranchOnMerge isn't on during cascade) |
| H7 | P1 | Rebase silently buries unresolved review comments | §0.1 `/cascade` snapshots before rebase |
| P0 (protection) | P0 | Required checks `CI — Tests` / `Security Scanning` are workflow names, not check-run names — would silently never match | §2B.2 uses `test` + `security` (job names) |

---

## §12 — Open operator decisions (informational, not blocking)

These are flagged for operator awareness; the design does not decide them:

- **`/root/availai-health-fix`**: separate clone with branch `[origin/main: behind 4]`. Probably stale; operator decides delete vs. keep.
- **PR #103 vs. CLAUDE.md fix-now mini-PR**: #103 (docs/claude-md-rebuild) may incorporate or supersede the §8.1 mini-PR. Operator chooses: ship mini-PR first then rebase #103 on top, OR ship #103 first and skip mini-PR if it covers the 7 fix-now items.
- **PR #93 vs. §2B.4 deploy.sh edit**: prefer riding the fix into #93's existing scope; if #93 closes for any reason, the fix lands in Track 2B's config PR.

---

## §13 — CLAUDE.md fix-now mini-PR scope

(Referenced from Track 1.2.) Scope = 3 fix-now + 4 NEW drift items, all edits to `/root/availai/CLAUDE.md` only:

1. Line 440: pre-commit hook list update (`ruff, ruff-format, mypy, docformatter, detect-private-key, check-yaml, check-added-large-files, check-merge-conflict, end-of-file-fixer, trailing-whitespace; pre-push hook runs full --all-files sweep automatically`)
2. Line 640: `ANTHROPIC_MODEL=claude-3-5-sonnet-20241022` → `ANTHROPIC_MODEL=claude-sonnet-4-6` (matches `app/config.py`)
3. Configuration block (lines 627–668):
   - REMOVE phantom keys: `MICROSOFT_GRAPH_ENDPOINT`, `SMTP_FROM`, `ACTIVITY_TRACKING_ENABLED`, `CONTACTS_SYNC_ENABLED`
   - REPLACE `AZURE_REDIRECT_URI` with `APP_URL`
   - ADD: `AVAIL_OPP_TABLE_V2`, `ENCRYPTION_SALT`, `EXPLORIUM_API_KEY`
   - ADD one-line pointer to `.env.example` for long-tail keys (`EIGHT_BY_EIGHT_*`, `NC_*`, `BACKUP_*`, `DO_SPACES_*`)
4. Worktree CLAUDE.md drift in 6 worktree copies + `/root/availai-health-fix/CLAUDE.md`: handled by deleting the worktrees in §1.4, not by editing each copy
5. Separate `.env.example:9` fix to `claude-sonnet-4-6` is a one-liner that can ride this PR

---

## §14 — Implementation handoff

This design is the input to `superpowers:writing-plans`. The plan should generate task lists for Tracks 0, 1, 2A, 2B, 3 in build order, with explicit reference to this spec's §-numbered subsections.

Track 0 tasks should produce concrete `.claude/skills/<name>/SKILL.md` and `.claude/hooks/<name>.sh` files. Track 1 tasks reference `/cascade`, `/recommit`, `/smoke-gate`, `/worktree-prune` invocations. Track 2A tasks each open small PRs. Track 2B is one configuration PR + the trial-deploy verification. Track 3 tasks update `.claude/settings.local.json` and add `.claude/hooks/auto-prune-worktree.sh`.

Acceptance for "design fully implemented" = all 10 success-criteria checks in §10 pass.
