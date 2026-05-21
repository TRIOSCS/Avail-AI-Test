# Track 1 — Critical Path Implementation Plan

> **For agentic workers:** This plan is the sequential critical path of the GitHub Cleanup design (spec: `/root/availai/.claude/worktrees/spec-cleanup/docs/superpowers/specs/2026-05-08-github-cleanup-design.md`, §1.1–§1.4 + §13). Execute tasks IN ORDER — earlier tasks unblock later ones. Each task is bite-sized (2–5 min). Section §-numbers in tasks reference the spec, not this plan. Stop and surface immediately on any unexpected output, conflict, or red CI; do not improvise around the spec.

## Goal

Drive the repo from its current red, blocked, drift-laden state to: `main` green, all 13 open PRs resolved, no orphan worktrees or stale remote branches, CLAUDE.md drift fixed. This unblocks Track 2B (branch protection + `deleteBranchOnMerge`), which the spec explicitly forbids landing until Track 1 is done.

Specifically:
- §1.1 Stabilize `main` (red CI on `6890dcf5`, direct-to-main hotfix allowed pre-protection).
- §1.2 Resolve the #109 → #108 → main stack and ship the §13 CLAUDE.md fix-now mini-PR.
- §1.3 Cascade-merge 11 PRs via the `/cascade` skill (deps → infra → features), with pre-flight conflict resolution on #96 and #103, fast-track on dependabot #100.
- §1.4 Worktree + remote-branch consolidation (orphan dirs, merged worktrees, merged remote branches, safe local prune).

## Architecture

Track 1 mostly mutates **repo state on GitHub** via `gh` and **local git state** via `git`/`git worktree`. The only files modified inside this track are:
1. The hotfix file(s) for §1.1 (unknown until diagnosis).
2. Conflict-resolution edits in #96's and #103's worktrees.
3. `/root/availai/CLAUDE.md` (and a one-liner in `/root/availai/.env.example`) for the §13 fix-now mini-PR.

The work is **sequential by design** (per spec §1 dependency arrow: 1.1 → 1.2 → 1.3 → 1.4). Do not parallelize across the four subsections — each guards the next from a known silent-failure mode (stack orphaning H2, comment loss H7, branch-deletion-during-cascade H4, etc.).

## Tech Stack

- `gh` CLI (PR/run/api operations) — repo `TRIOSCS/Avail-AI-Test`
- `git` + `git worktree` — local repo at `/root/availai`, this plan-execution worktree at `/root/availai/.claude/worktrees/spec-cleanup`
- Track 0 skills (PREREQUISITE, see below): `/cascade`, `/recommit`, `/smoke-gate`, `/worktree-prune`
- Local pre-commit, pytest, ruff, mypy (per CLAUDE.md "Before pushing big PRs" rule)

## PREREQUISITES

**Track 0 MUST be complete before starting Track 1.** Per spec §0 and the dependency arrow in §Architecture, Track 1 invokes the Track 0 skills directly.

Verify these exist before beginning:

```bash
test -d /root/availai/.claude/skills/cascade        || echo "MISSING: /cascade — Track 0.1 incomplete"
test -d /root/availai/.claude/skills/recommit       || echo "MISSING: /recommit — Track 0.2 incomplete"
test -d /root/availai/.claude/skills/smoke-gate     || echo "MISSING: /smoke-gate — Track 0.3 incomplete"
test -d /root/availai/.claude/skills/worktree-prune || echo "MISSING: /worktree-prune — Track 0.4 incomplete"
```

If any are missing, halt and complete Track 0 first. The §1.3 cascade absolutely requires `/cascade` (manual cascade is forbidden — H1, H4, H7 silent-failure modes are specifically what `/cascade` exists to prevent).

Also verify branch protection is OFF on `main` (the §1.1 direct-to-main hotfix depends on this):

```bash
gh api repos/TRIOSCS/Avail-AI-Test/branches/main/protection 2>&1 | grep -q '"message": "Branch not protected"' \
  && echo "OK: main is unprotected (Track 2B not yet applied)" \
  || echo "STOP: main protection appears active — investigate before §1.1 hotfix"
```

## File Structure

This track mutates repo state more than files. Files actually edited in Track 1:

| Path | Section | Notes |
|---|---|---|
| (TBD by §1.1 diagnosis) | §1.1 | Hotfix target unknown until `gh run view 25532626888 --log-failed` is read. Likely candidates: `requirements.txt`, an `alembic/versions/*.py`, an `app/**/*.py` test/source, or a CI workflow. |
| Files in #96's worktree | §1.3 pre-flight | Whatever conflicts vs. main on `fix/ci-unblock-test-assertions`. |
| Files in #103's worktree | §1.3 pre-flight | Whatever conflicts vs. main on `docs/claude-md-rebuild`. |
| `/root/availai/CLAUDE.md` | §1.2 step 6 (per §13) | 7 fix-now items: pre-commit list (line 440), `ANTHROPIC_MODEL` value (line 640), config block (lines 627–668: remove 4 phantom keys, replace `AZURE_REDIRECT_URI`→`APP_URL`, add 3 real keys, add `.env.example` pointer). |
| `/root/availai/.env.example` | §1.2 step 6 (per §13.5) | One-line `claude-sonnet-4-6` fix on line 9, rides the same mini-PR. |

Worktree-CLAUDE.md drift across 6 worktree copies + `/root/availai-health-fix/CLAUDE.md` is **not** edited per file — those copies vanish when their worktrees are removed in §1.4 (per spec §13 item 4).

---

## Tasks

### §1.1 — Stabilize main

#### Task 1.1.1 — Capture current main CI state

Confirm we are diagnosing the right run, and pin the failing SHA in writing for the worklog.

```bash
cd /root/availai
git fetch origin
git rev-parse origin/main                                 # should be 6890dcf5... (or document the new HEAD if main moved)
gh run list --branch main --limit 5 \
  --json databaseId,headSha,name,conclusion,createdAt \
  --jq '.[] | "\(.databaseId)\t\(.headSha[0:8])\t\(.name)\t\(.conclusion)\t\(.createdAt)"'
```

Expected: at least one row for run id `25532626888` showing `failure`. If main has moved past `6890dcf5`, re-identify the most recent `failure` run on `main` and use **its** id throughout §1.1 (treat `25532626888` in subsequent commands as a placeholder for "the failing main run").

#### Task 1.1.2 — Diagnose root cause from failing run logs

Pull the failing logs and read carefully.

```bash
gh run view 25532626888 --log-failed | tee /tmp/main-fail.log
```

Expected output structure to look for:
- Failing job name (e.g., `test`, `security`)
- Failing step (e.g., `Run pytest`, `pip-audit`, `npm audit`)
- The actual error trace — typically a Python traceback (test name + assertion / exception), an alembic error, a dependency-resolution error, or an audit CVE.

Write down (mentally or in scratch buffer) the **single root cause** before moving to fix. Per CLAUDE.md "No band-aids": you fix the cause, not the symptom.

#### Task 1.1.3 — Apply the §1.1 hotfix (direct to main)

Open-ended by necessity — the fix shape depends on §1.1.2. Reference shapes:

- **If dependency drift / pip-audit CVE:** pin or bump the offending version in `requirements.txt` (or `pyproject.toml`). Verify locally: `pip install -r requirements.txt && pip-audit` (or whichever tool the workflow uses).
- **If npm audit:** `npm audit fix` (or targeted `npm install <pkg>@<safe>`); commit `package-lock.json`.
- **If test regression:** fix the underlying code OR update the assertion (only if the assertion is genuinely stale — never tweak a test to mask a real bug). Re-run locally: `TESTING=1 PYTHONPATH=/root/availai pytest tests/<file>::<test> -v`.
- **If migration / alembic issue:** invoke `/smoke-gate` to verify `upgrade head → downgrade base → upgrade head` chain, then ship the migration fix.
- **If workflow YAML issue:** edit `.github/workflows/<file>.yml`; confirm syntax with `gh workflow view`.

Commit directly on `main` (per spec §1.1 — this is the **only** direct-to-main exception in the whole design):

```bash
cd /root/availai
git checkout main
git pull --ff-only origin main
git add <fixed-files>
git commit -m "fix(ci): stabilize main — <one-line cause>"
git push origin main
```

If `git push` is blocked by the `warn-destructive-git` hookify rule, that means Track 0.6 was not yet applied — pause and complete Track 0.6 (the rule should only block force-pushes to `main`, not regular pushes).

#### Task 1.1.4 — Verify main CI returns green

Wait for the new run, then verify.

```bash
sleep 15  # let GH register the push
gh run watch --exit-status $(gh run list --branch main --limit 1 --json databaseId --jq '.[0].databaseId')
gh run list --branch main --limit 2 --json conclusion --jq '.[0].conclusion'
```

Expected: `success`. If `failure`, return to §1.1.2 with the new run id and iterate. Do not proceed to §1.2 with red main.

---

### §1.2 — Resolve stacked PRs (#109 → #108 → main) and ship §13 CLAUDE.md mini-PR

#### Task 1.2.1 — Verify the stack invariant before merging

```bash
gh pr view 109 --json baseRefName,headRefName,mergeable,mergeStateStatus,commits \
  --jq '{base: .baseRefName, head: .headRefName, mergeable, state: .mergeStateStatus, latest_sha: .commits[-1].oid[0:8]}'
gh pr view 108 --json baseRefName,headRefName,mergeable,mergeStateStatus \
  --jq '{base: .baseRefName, head: .headRefName, mergeable, state: .mergeStateStatus}'
```

Expected:
- #109 `base` = `fix/ci-unblock-alembic-and-audit` (NOT `main`), `mergeable` = `MERGEABLE`, latest commit on green CI per spec §1.2 (sha prefix `46ffc97` per spec, but accept whatever HEAD is on the branch).
- #108 `base` = `main`, `head` = `fix/ci-unblock-alembic-and-audit`.

If #109's base is `main` instead of `fix/ci-unblock-alembic-and-audit`, halt — the stack has been broken and the spec's §1.2 procedure no longer applies; surface for operator decision.

#### Task 1.2.2 — Squash-merge #109 INTO #108's branch

Per spec §1.2 step 1 — this merges into the parent PR's branch, not main, because #109's base IS #108's branch.

```bash
gh pr merge 109 --squash --delete-branch=false
```

Note: explicit `--delete-branch=false` because `deleteBranchOnMerge` is OFF at the repo level right now anyway, but being explicit avoids a future-proofing surprise. After this command, #109 is `MERGED` and `fix/ci-unblock-alembic-and-audit` (the #108 branch) has gained #109's squashed commit.

Verify:

```bash
gh pr view 109 --json state --jq .state                   # MERGED
gh pr view 108 --json commits --jq '.commits[-1].oid[0:8]'  # should be a NEW sha vs. before
```

#### Task 1.2.3 — Wait for #108's CI to re-run and turn green

The merge in §1.2.2 pushed a new commit to `fix/ci-unblock-alembic-and-audit`, so a fresh CI run kicks off automatically.

```bash
sleep 15
gh run watch --exit-status $(gh run list --branch fix/ci-unblock-alembic-and-audit --limit 1 --json databaseId --jq '.[0].databaseId')
gh pr view 108 --json mergeStateStatus,statusCheckRollup --jq '{state: .mergeStateStatus, checks: [.statusCheckRollup[] | {name, conclusion}]}'
```

Expected: every check `SUCCESS`, mergeStateStatus `CLEAN`. If any check fails, halt — the assumption that #109's fixes covered #108 has broken; investigate the failure before continuing.

#### Task 1.2.4 — Squash-merge #108 into main

Per spec §1.2 step 4.

```bash
gh pr merge 108 --squash --delete-branch=false
```

Verify:

```bash
gh pr view 108 --json state --jq .state                   # MERGED
git -C /root/availai fetch origin
git -C /root/availai log origin/main -1 --oneline         # should be the squash-merge commit
```

#### Task 1.2.5 — Verify main CI green post-stack-resolution

```bash
sleep 15
gh run watch --exit-status $(gh run list --branch main --limit 1 --json databaseId --jq '.[0].databaseId')
gh run list --branch main --limit 1 --json conclusion --jq '.[0].conclusion'   # success
```

If failure: halt — the cascade in §1.3 cannot start with a red main (every cascade PR's `mergeStateStatus` would inherit UNSTABLE, which is the exact root cause of the original 8-PR pile-up).

#### Task 1.2.6 — Branch + worktree for the §13 fix-now mini-PR

Per spec §13 — small, direct PR scope = 7 CLAUDE.md edits + 1 `.env.example` line.

```bash
cd /root/availai
git checkout main && git pull --ff-only origin main
git checkout -b chore/claudemd-fix-now
# Stay in /root/availai for this PR — it's a tiny doc edit; no worktree needed.
```

#### Task 1.2.7 — Apply §13 item 1 — pre-commit hook list (CLAUDE.md line ~440)

Open `/root/availai/CLAUDE.md`, find the existing line:

```
Pre-commit hooks: ruff, ruff-format, mypy, docformatter, detect-private-key
```

Replace with the spec §13.1 verbatim list:

```
Pre-commit hooks: ruff, ruff-format, mypy, docformatter, detect-private-key, check-yaml, check-added-large-files, check-merge-conflict, end-of-file-fixer, trailing-whitespace; pre-push hook runs full --all-files sweep automatically
```

#### Task 1.2.8 — Apply §13 item 2 — `ANTHROPIC_MODEL` value (CLAUDE.md line ~640)

Find:

```
ANTHROPIC_MODEL=claude-3-5-sonnet-20241022
```

Replace with:

```
ANTHROPIC_MODEL=claude-sonnet-4-6
```

(Spec §13.2: matches `app/config.py`.)

#### Task 1.2.9 — Apply §13 item 3 — Configuration block surgery (CLAUDE.md lines 627–668)

Three sub-edits in the `## Configuration` block:

a. **Remove phantom keys** (these are not in `app/config.py`):
   - `MICROSOFT_GRAPH_ENDPOINT=...`
   - `SMTP_FROM=...`
   - `ACTIVITY_TRACKING_ENABLED=...`
   - `CONTACTS_SYNC_ENABLED=...`
   Delete the lines (and any now-empty surrounding block / fenced section header that goes with them).

b. **Replace** `AZURE_REDIRECT_URI=https://app.yourdomain.com/auth/callback` with `APP_URL=https://app.yourdomain.com` (spec §13.3 — `app/config.py` uses `APP_URL`, not `AZURE_REDIRECT_URI`).

c. **Add** these three keys to the appropriate subsection (Feature Flags or a new "Other" subsection — match the existing markdown style of fenced bash blocks with `KEY=value`):
   - `AVAIL_OPP_TABLE_V2=true`
   - `ENCRYPTION_SALT=<random>`
   - `EXPLORIUM_API_KEY=...`

d. **Add** a single pointer line at the bottom of the Configuration section (before any horizontal rule):

```
> See `.env.example` for long-tail keys (`EIGHT_BY_EIGHT_*`, `NC_*`, `BACKUP_*`, `DO_SPACES_*`).
```

#### Task 1.2.10 — Apply §13 item 5 — `.env.example` line 9

Open `/root/availai/.env.example`. Find line 9 (`ANTHROPIC_MODEL=claude-3-5-sonnet-20241022` per spec §13.5) and update to:

```
ANTHROPIC_MODEL=claude-sonnet-4-6
```

#### Task 1.2.11 — Lint, commit, and push the mini-PR

```bash
cd /root/availai
pre-commit run --all-files                 # CLAUDE.md spec rule: full sweep before push
git add CLAUDE.md .env.example
git commit -m "docs(claude-md): fix-now drift items per design §13

- Pre-commit hook list updated to match actual config
- ANTHROPIC_MODEL aligned to claude-sonnet-4-6 (CLAUDE.md + .env.example)
- Configuration block: remove 4 phantom keys, replace AZURE_REDIRECT_URI→APP_URL,
  add AVAIL_OPP_TABLE_V2 / ENCRYPTION_SALT / EXPLORIUM_API_KEY,
  add .env.example pointer for long-tail keys

Closes the 7 fix-now items from spec §13."
git push -u origin chore/claudemd-fix-now
```

#### Task 1.2.12 — Open the mini-PR and merge once green

```bash
gh pr create --base main --head chore/claudemd-fix-now \
  --title "docs(claude-md): fix-now drift items (§13)" \
  --body "$(cat <<'EOF'
## Summary
Fixes the 7 fix-now CLAUDE.md drift items called out in `2026-05-08-github-cleanup-design.md` §13.

## Test plan
- [x] Pre-commit + ruff clean
- [x] All edits sourced from spec §13 verbatim
- [x] APP_MAP docs unaffected (no architecture change)

## Risk & rollback
Doc-only PR. Revert with `git revert` if any wording is off.
EOF
)"

# Wait for CI then merge
sleep 15
gh run watch --exit-status $(gh run list --branch chore/claudemd-fix-now --limit 1 --json databaseId --jq '.[0].databaseId')
gh pr merge --squash --delete-branch=false $(gh pr list --head chore/claudemd-fix-now --json number --jq '.[0].number')
```

Note on coordination with #103 (per spec §12): #103 (`docs/claude-md-rebuild`) may incorporate or supersede some of these items. That's resolved during the cascade in §1.3 task 1.3.13 (#103 conflict resolution) — at that point this mini-PR is already in main and #103 rebases onto it.

---

### §1.3 — Cascade-merge 11 PRs (`/cascade` skill)

The cascade pool, in spec §1.3 order:

| # | PR | Branch | Notes |
|---|---|---|---|
| 1 | #100 | `dependabot/pip/minor-and-patch-ac6984c910` | Fast-track: skip full agent review; verify CI + no major bumps |
| 2 | #92  | `fix/env-hygiene` | 4 lines, 1 file |
| 3 | #93  | `fix/deploy-sh-harden` | 35 lines, 1 file. **§8.2 edit required before merge** (pre-pend `git pull --rebase origin main` per design §2B.4) |
| 4 | #94  | `docs/pre-rollout-checklist` | 336 lines, docs-only |
| 5 | #96  | `fix/ci-unblock-test-assertions` | **POST conflict resolution (§1.3.5 below)** |
| 6 | #97  | `fix/eslint-browser-globals` | 2 lines, 1 file |
| 7 | #101 | `chore/great-purge-tracked-artifacts` | 2747 deletions, 361 files — biggest deletion PR; full review extra-careful |
| 8 | #102 | `fix/search-bg-session` | 867 lines, 8 files; full review |
| 9 | #103 | `docs/claude-md-rebuild` | **POST conflict resolution AND post §13 mini-PR (§1.3.13)** |
| 10 | #106 | `fix/connector-hard-errors` | 13 lines, 2 files |
| 11 | #107 | `chore/gradient-vestiges-and-docfmt` | 1347 lines, 32 files — biggest feature PR; full review extra-careful |

#### Task 1.3.1 — Cascade procedure template (READ ONCE, REFERENCED BY EVERY 1.3.X TASK BELOW)

This task documents the per-PR procedure that `/cascade` executes (spec §1.3 "Per-PR procedure"). **Do not run this task on its own** — every per-PR task below says "run /cascade procedure on PR #N" and refers back here.

The 10 steps `/cascade` executes for each PR:

1. **Snapshot unresolved review comments** via `gh api repos/TRIOSCS/Avail-AI-Test/pulls/<N>/comments --jq '.[] | select(.in_reply_to_id == null)'` → write to `.claude/state/cascade/<N>-comments.json` (prevents H7 silent loss on rebase).
2. **Rebase onto current `main`** in the PR's worktree (or check it out if no worktree exists). Halt on conflict and surface to user with explicit "next action" message.
3. **Run pre-commit + tests + lint locally**: `pre-commit run --all-files && TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v && ruff check app/`. Halt on failure.
4. **Push** with `git push --force-with-lease origin <branch>` (allowed because force-push is to feature branch, not `main` — Track 0.6 ref-name rule covers this).
5. **Re-post unresolved comments** to the new SHAs from the snapshot in step 1.
6. **Dispatch full agent review suite** (per CLAUDE.md "Run ALL pr-review-toolkit agents on every PR"): comment-analyzer, pr-test-analyzer, type-design-analyzer, silent-failure-hunter, code-simplifier, code-reviewer, plus feature-dev:code-reviewer. **Skip this step for #100 only** (dependabot fast-track per spec §1.3 / architecture C7).
7. **Address review findings as fresh commits** (never amend — CLAUDE.md commit safety rule); push.
8. **Wait for green CI** on the latest SHA: `gh run watch --exit-status $(gh run list --branch <branch> --limit 1 --json databaseId --jq '.[0].databaseId')`.
9. **`gh pr merge <N> --squash --delete-branch=false`**. Explicit `--delete-branch=false` because Track 2B's `deleteBranchOnMerge` is not yet on; remote branches are cleaned up in bulk in §1.4 (silent-failure H4 protection).
10. **Update cascade state**: `/cascade` writes the merged PR number to `.claude/state/cascade.json`. If anything halts mid-flight, restart resumes from the next leaf.

Success criteria for **each** per-PR run: PR shows `state=MERGED`, branch's last CI run is `success`, `main` CI is `success` after the merge, no comments lost (snapshot file matches re-posted set).

#### Task 1.3.2 — Pre-flight: resolve conflicts on #96

Per spec §1.3 pre-flight bullet 1.

```bash
cd /root/availai
git fetch origin
gh pr view 96 --json mergeable,headRefName --jq '{mergeable, branch: .headRefName}'
# Expected: mergeable = CONFLICTING, branch = fix/ci-unblock-test-assertions

# Get into the PR's worktree if it exists, else check out the branch into a new worktree
git worktree list | grep -q "fix/ci-unblock-test-assertions" \
  || git worktree add /root/availai/.claude/worktrees/pr96 fix/ci-unblock-test-assertions

cd /root/availai/.claude/worktrees/pr96
git pull origin fix/ci-unblock-test-assertions
git rebase origin/main
# Resolve conflicts file-by-file. For each conflicted file:
#   - Open it
#   - Resolve <<<<<<< / ======= / >>>>>>> markers
#   - git add <file>
# Then: git rebase --continue (repeat until done)

# Verify the rebase landed cleanly
git status                                      # clean
pre-commit run --all-files                      # green
TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v   # green

git push --force-with-lease origin fix/ci-unblock-test-assertions
gh pr view 96 --json mergeable --jq .mergeable  # MERGEABLE
```

If conflicts cannot be resolved cleanly (semantic conflict that requires test/code changes): halt and surface — do not improvise; this is a signal that #96 needs operator review before cascading.

#### Task 1.3.3 — Pre-flight: resolve conflicts on #103

Same procedure as 1.3.2, branch `docs/claude-md-rebuild`. Per spec §12 + §13 coordination:

```bash
cd /root/availai
git worktree list | grep -q "docs/claude-md-rebuild" \
  || git worktree add /root/availai/.claude/worktrees/pr103 docs/claude-md-rebuild

cd /root/availai/.claude/worktrees/pr103
git pull origin docs/claude-md-rebuild
git rebase origin/main                          # main now contains the §13 mini-PR from §1.2.12
```

When resolving CLAUDE.md conflicts, the rule per spec §12: **prefer the §13 mini-PR's already-merged content** for any of the 7 fix-now items #103 also touches (mini-PR went first; #103 rebases on top). For other CLAUDE.md content #103 introduces (the broader rebuild), keep #103's version.

```bash
git status                                      # clean
pre-commit run --all-files                      # green
git push --force-with-lease origin docs/claude-md-rebuild
gh pr view 103 --json mergeable --jq .mergeable # MERGEABLE
```

#### Task 1.3.4 — Confirm no other PR has drifted to CONFLICTING

Per spec §1.3 pre-flight bullet 3.

```bash
gh pr list --state open --json number,mergeable \
  --jq '.[] | select(.mergeable == "CONFLICTING") | .number'
```

Expected output: empty (or only #96 / #103 if their force-pushes haven't propagated yet — re-run after 30s).
If a different PR appears as CONFLICTING: halt and resolve it via the same template as 1.3.2 before starting the cascade.

#### Task 1.3.5 — Initialize `/cascade` state

```bash
mkdir -p /root/availai/.claude/state/cascade
test -f /root/availai/.claude/state/cascade.json && cat /root/availai/.claude/state/cascade.json || echo '{"merged":[]}' > /root/availai/.claude/state/cascade.json
```

If a stale `cascade.json` from a prior run exists, decide whether to resume or reset. For a fresh run: overwrite with `{"merged":[]}`.

#### Task 1.3.6 — Run /cascade procedure on PR #100

PR-specific notes: **dependabot — FAST-TRACK; skip step 6 of the template (full agent review).** Per spec §1.3 / architecture C7: verify CI passes + no major-version bumps + merge.

Pre-merge sanity check before invoking `/cascade`:

```bash
gh pr view 100 --json title,body --jq '"\(.title)\n\(.body)"' | grep -iE 'major|breaking' \
  && echo "STOP: dependabot bump appears to include majors — promote to full review" \
  || echo "OK: minor/patch only, fast-track approved"
```

Then invoke `/cascade` for #100 with the `--fast-track` flag (skill argument that disables step 6). Verify success criteria from 1.3.1: #100 MERGED, branch CI success, main CI success.

#### Task 1.3.7 — Run /cascade procedure on PR #92

PR-specific notes: 4 lines, 1 file (`fix/env-hygiene`). Standard procedure — full review applies but it should be quick given size.

Invoke `/cascade` on #92. Verify success criteria from 1.3.1.

#### Task 1.3.8 — Run /cascade procedure on PR #93

PR-specific notes: 35 lines, 1 file (`fix/deploy-sh-harden`). **Mandatory edit before merge per spec §8.2 / §2B.4:** prepend `git pull --rebase origin main` before `deploy.sh`'s existing `git push origin main` (line 21). This rides into #93's existing scope so Track 2B's linear-history protection doesn't reject the next deploy.

In the cascade template's step 7 (address findings as fresh commits), explicitly add this edit if it isn't already in #93:

```bash
cd <pr93-worktree>
grep -n "git push origin main" deploy.sh         # find current line
# Edit deploy.sh to add `git pull --rebase origin main` immediately before it
git add deploy.sh
git commit -m "fix(deploy): pull --rebase before push (compat with linear-history protection)"
git push origin fix/deploy-sh-harden
```

Then continue cascade template from step 8. Verify success criteria from 1.3.1.

#### Task 1.3.9 — Run /cascade procedure on PR #94

PR-specific notes: 336 lines, 1 file, **docs-only** (`docs/pre-rollout-checklist`). Standard procedure; agent review should be quick (no code/security risk).

Invoke `/cascade` on #94. Verify success criteria from 1.3.1.

#### Task 1.3.10 — Run /cascade procedure on PR #96

PR-specific notes: **post-conflict-resolution from task 1.3.2.** Standard procedure from here.

Re-verify mergeable before invoking:

```bash
gh pr view 96 --json mergeable --jq .mergeable   # must be MERGEABLE
```

Invoke `/cascade` on #96. Verify success criteria from 1.3.1.

#### Task 1.3.11 — Run /cascade procedure on PR #97

PR-specific notes: 2 lines, 1 file (`fix/eslint-browser-globals`). Trivial — full review still runs but is quick.

Invoke `/cascade` on #97. Verify success criteria from 1.3.1.

#### Task 1.3.12 — Run /cascade procedure on PR #101

PR-specific notes: **2747 deletions, 361 files — biggest deletion PR (`chore/great-purge-tracked-artifacts`).** Full review **extra-careful** — confirm the deletions don't include load-bearing artifacts (e.g., a tracked migration, a tracked vendored dependency that's not in `requirements.txt`, a tracked build output the deploy depends on). Spend the time on step 6's full agent review — silent-failure-hunter and code-reviewer specifically.

Pre-merge sanity check inside the template's step 3:

```bash
cd <pr101-worktree>
git diff --stat origin/main...HEAD | tail -1     # confirm deletion stats
# Scan deleted files for risky patterns:
git diff --name-only --diff-filter=D origin/main...HEAD | grep -E '\.(py|yml|yaml|sh|html|jinja2)$' | head -50
# Each "real code" file in this list deserves a quick "why deleted?" justification.
```

Invoke `/cascade` on #101. Verify success criteria from 1.3.1.

#### Task 1.3.13 — Run /cascade procedure on PR #102

PR-specific notes: 867 lines, 8 files (`fix/search-bg-session`); full review. Likely touches search service / DB session lifecycle (per branch name) — spec calls this one out for full review specifically, so silent-failure-hunter + type-design-analyzer findings should be addressed thoroughly.

Invoke `/cascade` on #102. Verify success criteria from 1.3.1.

#### Task 1.3.14 — Run /cascade procedure on PR #103

PR-specific notes: **post-conflict-resolution from task 1.3.3 AND post §13 mini-PR from §1.2.12.** During cascade rebase, the §13 fix-now items are already in main; #103 must layer on top — see task 1.3.3's note about preferring main's content for the 7 fix-now items.

Re-verify mergeable before invoking:

```bash
gh pr view 103 --json mergeable --jq .mergeable  # must be MERGEABLE
```

Invoke `/cascade` on #103. Verify success criteria from 1.3.1.

#### Task 1.3.15 — Run /cascade procedure on PR #106

PR-specific notes: 13 lines, 2 files (`fix/connector-hard-errors`). Small, but per project memory this is part of the connector hard-errors split-out work — full review applies; scrutinize the error-handling semantics.

Invoke `/cascade` on #106. Verify success criteria from 1.3.1.

#### Task 1.3.16 — Run /cascade procedure on PR #107

PR-specific notes: **1347 lines, 32 files — biggest feature PR (`chore/gradient-vestiges-and-docfmt`).** Full review **extra-careful** — gradient cleanup touches CSS/templates and docfmt touches docs across many files. Per project memory, this PR was previously CI-blocked behind the main red; with main now green it should be unblocked. Code-simplifier and code-reviewer are the agents that matter most here.

Invoke `/cascade` on #107. Verify success criteria from 1.3.1.

#### Task 1.3.17 — Confirm 0 open PRs after cascade

```bash
gh pr list --state open --json number --jq 'length'
```

Expected: `0`. If non-zero: list which PRs remain and surface — `/cascade` may have halted on a PR not yet listed in this plan, or new PRs may have been opened during the cascade window.

```bash
gh pr list --state open --json number,title,headRefName
```

#### Task 1.3.18 — Confirm main CI is green after final cascade merge

```bash
gh run watch --exit-status $(gh run list --branch main --limit 1 --json databaseId --jq '.[0].databaseId')
gh run list --branch main --limit 3 --json conclusion --jq 'all(.[]; .conclusion == "success")'
```

Expected: `true`. Halt and surface if not.

---

### §1.4 — Worktree + remote-branch consolidation

#### Task 1.4.1 — Inventory worktrees and remote branches

Establish baseline before mutation.

```bash
cd /root/availai
git worktree list                                                          | tee /tmp/wt-before.txt
ls /root/availai/.claude/worktrees/                                        | tee /tmp/wt-dirs-before.txt
git branch -vv                                                             | tee /tmp/local-branches-before.txt
gh api repos/TRIOSCS/Avail-AI-Test/branches --paginate --jq '.[].name'     | tee /tmp/remote-branches-before.txt
```

Save these so the §1.4 success-criteria checks have something to diff against.

#### Task 1.4.2 — Remove the 7 orphan `agent-*` directories

Per spec §1.4 — these dirs are not in `git worktree list` (orphaned by previous failed worktree-add or manual rm).

```bash
for slug in a034b395 a263dee1 a37647f7 a47a352b a4cf0049 a6911920 aff7e329; do
  d="/root/availai/.claude/worktrees/agent-${slug}"
  if [ -d "$d" ]; then
    # Sanity: confirm it's not in `git worktree list`
    if git worktree list --porcelain | grep -q "$d"; then
      echo "REFUSE: $d is a real worktree, not an orphan — investigate"
    else
      rm -rf "$d" && echo "removed $d"
    fi
  else
    echo "absent (already cleaned): $d"
  fi
done
```

Use `/worktree-prune` (Track 0.4) for the interactive variant if preferred — but the explicit list above matches the spec verbatim and is safer as a one-shot.

#### Task 1.4.3 — Remove worktrees whose branches were merged in §1.3

Iterate `git worktree list` and, for each worktree whose branch shows up in the cascade-merged set, run `git worktree remove`. Use `/worktree-prune` skill which automates this:

```bash
/worktree-prune
```

(The skill cross-references `gh pr view <branch>` for state CLOSED/MERGED, prompts to confirm, then runs `git worktree remove --force <path>` + `rm -rf <path>`.)

If running manually instead:

```bash
cd /root/availai
git worktree list --porcelain | awk '/^worktree/ {wt=$2} /^branch/ {print wt "\t" $2}' \
  | while IFS=$'\t' read -r wt branch; do
      branch="${branch#refs/heads/}"
      [ "$branch" = "main" ] && continue
      [ "$wt" = "/root/availai" ] && continue
      state=$(gh pr list --head "$branch" --state all --json state --jq '.[0].state // ""')
      if [ "$state" = "MERGED" ] || [ "$state" = "CLOSED" ]; then
        echo "removing worktree $wt (branch $branch state $state)"
        git worktree remove --force "$wt" && rm -rf "$wt"
      fi
    done
```

#### Task 1.4.4 — Remove the transient `/tmp/pr109-rebase` worktree

Per spec §1.4 — session artifact from #109 rebase work.

```bash
if git worktree list | grep -q "/tmp/pr109-rebase"; then
  git worktree remove --force /tmp/pr109-rebase
fi
rm -rf /tmp/pr109-rebase 2>/dev/null || true
```

#### Task 1.4.5 — Surface `/root/availai-health-fix` for operator decision

Per spec §1.4 + §12 — design does **not** delete this. Operator decides.

```bash
if [ -d /root/availai-health-fix ]; then
  cd /root/availai-health-fix
  echo "=== /root/availai-health-fix ==="
  git status -sb 2>/dev/null
  git log -1 --oneline 2>/dev/null
  cd /root/availai
fi
```

Print the output. Do NOT delete. Surface to user with: "operator decision per spec §12 — keep or delete?"

#### Task 1.4.6 — Delete merged remote branches via `gh api`

**CRITICAL** per spec §1.4: `deleteBranchOnMerge` is NOT on during the cascade (Track 2B is gated behind Track 1), so §1.3's merged branches still exist on origin and must be deleted explicitly.

Use the spec's exact bash block (verbatim from spec §1.4):

```bash
gh pr list --state merged --base main --search "merged:>=2026-05-08" \
  --json number,headRefName --jq '.[].headRefName' | \
  while read branch; do
    [ -n "$branch" ] && gh api -X DELETE \
      "repos/TRIOSCS/Avail-AI-Test/git/refs/heads/$branch" 2>/dev/null || true
  done
```

Note: `2>/dev/null || true` swallows already-deleted (404) errors silently; that's intentional per the spec's idempotency goal. If you want a louder version for verification:

```bash
gh pr list --state merged --base main --search "merged:>=2026-05-08" \
  --json number,headRefName --jq '.[] | "\(.number)\t\(.headRefName)"' | \
  while IFS=$'\t' read num branch; do
    [ -z "$branch" ] && continue
    if gh api -X DELETE "repos/TRIOSCS/Avail-AI-Test/git/refs/heads/$branch" 2>&1 \
       | grep -qE '(204|Not Found)'; then
      echo "deleted (or already gone): #$num $branch"
    else
      echo "FAILED: #$num $branch"
    fi
  done
```

#### Task 1.4.7 — Local prune to reflect remote deletions

```bash
cd /root/availai
git fetch --prune origin
git branch -vv | grep ': gone\]'    # list local branches whose remote is gone
```

#### Task 1.4.8 — Safe local-branch cleanup with `git cherry` check

**CRITICAL** per spec §1.4 / silent-failure H4: `git branch -D` after squash-merge silently drops un-squashed commits. Verify with `git cherry main <branch>` first — empty output means all commits are squashed in main; non-empty means there's un-squashed work.

Use the spec's exact bash block (verbatim from spec §1.4):

```bash
for b in $(git branch -vv | grep ': gone\]' | awk '{print $1}'); do
  if [ -z "$(git cherry main "$b")" ]; then
    git branch -D "$b"
  else
    echo "SKIP $b: has un-squashed commits"
  fi
done
```

For any branch printed as `SKIP`: surface to user — these branches have commits that did not make it into main via squash. Do NOT force-delete them; investigate per branch.

#### Task 1.4.9 — Final-state verification

Per spec §1.4: "only `/root/availai` itself + any actively-used worktree under `.claude/worktrees/`. `git worktree list` and `ls .claude/worktrees/` agree."

```bash
cd /root/availai
echo "=== git worktree list ==="
git worktree list
echo
echo "=== ls .claude/worktrees/ ==="
ls /root/availai/.claude/worktrees/ 2>/dev/null
echo
echo "=== orphan check (expected: empty) ==="
comm -23 \
  <(ls /root/availai/.claude/worktrees/ 2>/dev/null | sort) \
  <(git worktree list --porcelain | awk '/^worktree/ {print $2}' | sed 's|.*/||' | sort)
```

Expected:
- `git worktree list` shows `/root/availai` + at most a small number of in-use worktrees (e.g., this plan-execution `spec-cleanup` worktree if still active).
- `ls .claude/worktrees/` matches the worktree list (no orphans).
- `comm -23` output is empty.

If any check fails: re-run §1.4.2 / §1.4.3 / §1.4.6 / §1.4.7 / §1.4.8 as needed.

---

## Self-review — spec coverage map

Mapping each spec §-number to the task(s) that implement it:

| Spec § | Coverage | Task(s) |
|---|---|---|
| §1.1 (stabilize main — pull failing logs) | yes | 1.1.1, 1.1.2 |
| §1.1 (diagnose + fix as direct-to-main hotfix) | yes | 1.1.3 |
| §1.1 (verify with /smoke-gate if migration) | yes | 1.1.3 (referenced as fix-shape option) |
| §1.1 (verify main green) | yes | 1.1.4 |
| §1.2 step 1 (`gh pr merge 109 --squash`) | yes | 1.2.2 |
| §1.2 step 2 (wait for #108 CI) | yes | 1.2.3 |
| §1.2 step 3 (verify #108 fully green) | yes | 1.2.3 |
| §1.2 step 4 (`gh pr merge 108 --squash`) | yes | 1.2.4 |
| §1.2 step 5 (verify main CI green) | yes | 1.2.5 |
| §1.2 step 6 (ship §13 mini-PR) | yes | 1.2.6 → 1.2.12 |
| §13 item 1 (pre-commit list, line 440) | yes | 1.2.7 |
| §13 item 2 (`ANTHROPIC_MODEL`, line 640) | yes | 1.2.8 |
| §13 item 3 (config block lines 627–668: 4 removes, 1 replace, 3 adds, 1 pointer) | yes | 1.2.9 (sub-edits a/b/c/d) |
| §13 item 4 (worktree CLAUDE.md drift handled by worktree deletion) | yes | 1.4.3 (no per-file edits — confirms spec) |
| §13 item 5 (`.env.example:9` one-liner rides the PR) | yes | 1.2.10 |
| §1.3 pre-flight: resolve #96 conflicts | yes | 1.3.2 |
| §1.3 pre-flight: resolve #103 conflicts | yes | 1.3.3 |
| §1.3 pre-flight: confirm no other CONFLICTING | yes | 1.3.4 |
| §1.3 cascade order #100 → #92 → #93 → #94 → #96 → #97 → #101 → #102 → #103 → #106 → #107 | yes | 1.3.6 → 1.3.16 (in order) |
| §1.3 per-PR procedure (10 steps) | yes | 1.3.1 (template task; referenced by all per-PR tasks) |
| §1.3 #100 fast-track (skip full review per C7) | yes | 1.3.6 |
| §1.3 #93 deploy.sh edit per §2B.4 | yes | 1.3.8 |
| §1.3 #103 coordinates with §13 mini-PR | yes | 1.3.3 + 1.3.14 |
| §1.3 final state: 0 open PRs, main green | yes | 1.3.17, 1.3.18 |
| §1.4 rm -rf 7 orphan agent-* dirs | yes | 1.4.2 |
| §1.4 git worktree remove merged worktrees | yes | 1.4.3 (via `/worktree-prune` or manual) |
| §1.4 remove `/tmp/pr109-rebase` | yes | 1.4.4 |
| §1.4 surface `/root/availai-health-fix` (operator decision per §12) | yes | 1.4.5 |
| §1.4 `gh api DELETE` merged remote branches (deleteBranchOnMerge not on yet) | yes | 1.4.6 |
| §1.4 `git fetch --prune origin` | yes | 1.4.7 |
| §1.4 `git cherry` check before `git branch -D` (H4 prevention) | yes | 1.4.8 |
| §1.4 final-state agreement check | yes | 1.4.9 |
| §0 prerequisite gating | yes | PREREQUISITES section |

All spec §1.1–§1.4 + §13 items are covered. Items deferred to other tracks (§2B branch protection, §2A templates/labels, §3 automations) are explicitly out of scope for this plan.
