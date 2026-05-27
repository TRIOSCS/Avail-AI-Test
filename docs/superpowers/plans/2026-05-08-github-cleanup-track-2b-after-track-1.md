# Track 2B — After-Track-1 Policy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Halt and surface IMMEDIATELY if any prerequisite check (§PREREQUISITES) fails — Track 2B must NOT land while Track 1 is in flight, otherwise it will block the 11-PR cascade and orphan stacked PRs.

**Goal:** Lock in the durable repo-policy posture for `TRIOSCS/Avail-AI-Test` after Track 1 finishes: turn on `deleteBranchOnMerge`, restrict to squash + rebase merges, enforce branch protection on `main` with the corrected job-name contexts (`test`, `security`), publish issue templates, and harden `deploy.sh` so the first post-protection deploy still succeeds. This plan executes the §2B subsections of the GitHub Cleanup design.

**Architecture:** Three small PRs land in series. PR-A (Task 1) is a config-only PR — repo settings via `gh repo edit` + branch protection via `gh api -X PUT`. PR-B (Task 2) adds three `.github/ISSUE_TEMPLATE/*.md` files. PR-C (Task 3) is the deploy.sh hardening — a one-line edit (`git pull --rebase origin main` immediately before `git push origin main`). PR-C has a state-dependent landing path: if PR #93 is still open at execution time, the edit is committed onto #93's branch; if #93 is closed/merged-without-the-fix, the edit lands on a fresh branch instead. After each task, an explicit verification command must produce the expected output before the task is marked done. The trial deploy (`./deploy.sh --no-commit`) is the load-bearing real-world check — protection is verified working only when an actual push succeeds under linear-history.

**Tech Stack:** GitHub CLI (`gh`) · GitHub REST API · Bash · Markdown · Git.

**Spec:** `docs/superpowers/specs/2026-05-08-github-cleanup-design.md` (§2B.1 – §2B.4; §2B.5 CODEOWNERS is DROPPED per spec §11/M6).

---

## PREREQUISITES — Track 1 must be 100% complete before any task here runs

Track 2B encodes the design's hard build-order rule (spec §architecture, "Key dependency rule"): premature application orphans the stacked #108/#109 PR, deadlocks the 11-PR cascade behind required-checks, and creates a destructive `deleteBranchOnMerge` × open-worktree interaction. **Verify ALL of the following before opening Task 1.** If any check fails, STOP and resume Track 1.

- [ ] **P1: Zero open PRs**
  ```bash
  gh pr list --repo TRIOSCS/Avail-AI-Test --state open --limit 1 --json number --jq 'length == 0'
  ```
  Expected: `true`.

- [ ] **P2: `main` CI is green on the latest commit**
  ```bash
  gh run list --repo TRIOSCS/Avail-AI-Test --branch main --limit 2 \
    --json conclusion --jq 'all(.[]; .conclusion == "success")'
  ```
  Expected: `true`.

- [ ] **P3: No orphan worktree directories**
  ```bash
  comm -23 \
    <(ls /root/availai/.claude/worktrees/ 2>/dev/null | sort) \
    <(cd /root/availai && git worktree list --porcelain | awk '/^worktree/ {print $2}' | sed 's|.*/||' | sort)
  ```
  Expected: empty output.

- [ ] **P4: All Track 1.3 cascade-merged head branches deleted from origin**
  ```bash
  gh api repos/TRIOSCS/Avail-AI-Test/branches \
    --jq '.[].name' | grep -E '^(fix/|chore/|docs/|feat/|dependabot/)' || echo "no surviving feature branches"
  ```
  Expected: `no surviving feature branches` (or only branches the operator has explicitly chosen to retain — see spec §1.4).

- [ ] **P5: PR #93 state captured for Task 3 routing**
  ```bash
  gh pr view 93 --repo TRIOSCS/Avail-AI-Test --json state,headRefName,mergeable
  ```
  Record `state` (`OPEN` / `MERGED` / `CLOSED`) and `headRefName`. This decides Task 3's branch (see Task 3 Step 0).

If P1–P4 all pass, proceed.

---

## File Structure

| Path | Action | Purpose |
|---|---|---|
| (no file — `gh repo edit` API call) | API | Repo merge settings: delete-branch-on-merge ON, merge-commit OFF, squash + rebase ON |
| (no file — `gh api -X PUT branches/main/protection`) | API | Branch protection on `main` with required `test` + `security` contexts and linear history |
| `.github/ISSUE_TEMPLATE/bug.md` | Create | Standard bug-report template |
| `.github/ISSUE_TEMPLATE/feature.md` | Create | Standard feature-request template |
| `.github/ISSUE_TEMPLATE/security.md` | Create | Security-issue template (private-disclosure friendly) |
| `deploy.sh` | Modify | Prepend `git pull --rebase origin main` before `git push origin main` so linear-history protection accepts the push |

CODEOWNERS is intentionally NOT created — spec §2B.5 drops it (M6: decorative without `require_code_owner_reviews`, single-user repo).

---

## Execution Order

Three tasks, each its own PR, sequential.

- **Task 1 — Config PR (PR-A).** Repo settings + branch protection in one PR. Verification of branch-protection contexts is the most load-bearing check in this whole plan (silent-failure §11 P0).
- **Task 2 — Issue templates (PR-B).** Three Markdown files. Mechanical.
- **Task 3 — deploy.sh hardening (PR-C).** State-dependent: rides into PR #93 if open, else fresh branch. Includes mandatory trial deploy under live branch protection.

Each task ends with a commit and a verification block. Land Task 1 first — if its branch protection is misconfigured, Task 3's trial deploy will surface the problem cleanly rather than under a real outage.

---

## Task 1: Apply repo settings + branch protection (config PR-A)

**Spec:** §2B.1, §2B.2.

**Files:** none — entirely `gh` CLI calls. Open one PR for traceability/audit; the PR body documents the commands run. Use a tiny no-op file (e.g. add a comment line to a config doc) ONLY if the repo requires non-empty PRs; otherwise document the changes in a tracking issue and skip the PR. Default path below uses an issue, since the changes are server-side.

- [ ] **Step 1: Open a tracking issue (audit trail)**

  ```bash
  gh issue create --repo TRIOSCS/Avail-AI-Test \
    --title "Track 2B: apply repo settings + branch protection on main" \
    --body "Tracks application of spec §2B.1 (repo settings) and §2B.2 (branch protection). See docs/superpowers/specs/2026-05-08-github-cleanup-design.md and docs/superpowers/plans/2026-05-08-github-cleanup-track-2b-after-track-1.md. This issue closes when Step 6 verification passes."
  ```

  Record the issue number returned (referenced as `$ISSUE_NUM` below).

- [ ] **Step 2: Apply repo merge settings (§2B.1)**

  ```bash
  gh repo edit TRIOSCS/Avail-AI-Test \
    --delete-branch-on-merge \
    --enable-merge-commit=false \
    --enable-squash-merge=true \
    --enable-rebase-merge=true
  ```

  Expected: command exits 0 with no error. No stdout on success is normal.

- [ ] **Step 3: Verify repo merge settings landed**

  ```bash
  gh repo view TRIOSCS/Avail-AI-Test \
    --json mergeCommitAllowed,squashMergeAllowed,rebaseMergeAllowed,deleteBranchOnMerge \
    --jq '.mergeCommitAllowed == false
          and .squashMergeAllowed == true
          and .rebaseMergeAllowed == true
          and .deleteBranchOnMerge == true'
  ```

  Expected: `true`. If `false`, re-run Step 2 and re-verify; do not proceed to Step 4.

- [ ] **Step 4: Apply branch protection on `main` (§2B.2)**

  Use the EXACT JSON below — every key is justified in spec §2B.2. **DO NOT substitute workflow names (`CI — Tests`, `Security Scanning`) for the contexts.** The contexts MUST be the GitHub-reported check-run job names `test` and `security`, otherwise the protection silently never matches and is a no-op (spec §11 P0).

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

  Expected: command exits 0. Response prints the applied protection JSON.

- [ ] **Step 5: CRITICAL verification — confirm contexts are job names and `strict` is false (spec §10 #4 + §11 P0)**

  This is the silent-failure-prevention check the parallel review caught. If this returns `false`, the protection is misconfigured and Task 3's trial deploy will block. Re-apply Step 4 with the correct JSON before proceeding.

  ```bash
  gh api repos/TRIOSCS/Avail-AI-Test/branches/main/protection \
    --jq '.required_status_checks.contexts == ["test","security"] and .required_status_checks.strict == false'
  ```

  Expected: `true`.

- [ ] **Step 6: Full §10 #4 protection verification (linear history + admin enforcement)**

  ```bash
  gh api repos/TRIOSCS/Avail-AI-Test/branches/main/protection \
    --jq '.required_status_checks.contexts == ["test","security"]
          and .required_status_checks.strict == false
          and .required_linear_history.enabled == true
          and .enforce_admins.enabled == false
          and .allow_force_pushes.enabled == false
          and .allow_deletions.enabled == false'
  ```

  Expected: `true`.

- [ ] **Step 7: Cross-check actual GitHub check-run names match the protection contexts**

  This catches the case where a CI workflow rename has shifted the job names without updating protection. Pull the latest check-run names from the most recent commit on `main`:

  ```bash
  gh api repos/TRIOSCS/Avail-AI-Test/commits/main/check-runs \
    --jq '[.check_runs[].name] | sort | unique'
  ```

  Expected: includes both `"test"` and `"security"`. If GitHub reports different names (e.g. `test (3.11)`), STOP — a follow-up plan is needed to either rename the job in `.github/workflows/` or update the protection contexts. Do not paper over this; mismatched contexts are exactly the silent-failure §11 prevented.

- [ ] **Step 8: Close the tracking issue with verification evidence**

  ```bash
  gh issue close $ISSUE_NUM --repo TRIOSCS/Avail-AI-Test \
    --comment "Applied. Verified via §10 #4 protection check + §10 #5 repo-settings check + check-run name cross-check (Steps 5/6/7)."
  ```

**Task 1 done when:** Steps 3, 5, 6, 7 all return `true` / matching output. Issue closed.

---

## Task 2: Publish issue templates (PR-B)

**Spec:** §2B.3.

**Files:**
- Create: `.github/ISSUE_TEMPLATE/bug.md`
- Create: `.github/ISSUE_TEMPLATE/feature.md`
- Create: `.github/ISSUE_TEMPLATE/security.md`

- [ ] **Step 1: Cut a fresh branch from current `main`**

  ```bash
  cd /root/availai
  git fetch origin
  git checkout -b chore/issue-templates origin/main
  ```

- [ ] **Step 2: Create `.github/ISSUE_TEMPLATE/bug.md`**

  ```markdown
  ---
  name: Bug report
  about: Report a defect in AvailAI
  title: "[bug] "
  labels: ["type:bug"]
  ---

  ## What happened

  <!-- Describe the actual behavior -->

  ## What you expected

  <!-- Describe the expected behavior -->

  ## Reproduction steps

  1.
  2.
  3.

  ## Environment

  - Branch / commit:
  - Browser / OS (if frontend):
  - Affected route or service:

  ## Logs / screenshots

  <!-- Paste relevant log lines, request_id, stack trace. NO secrets. -->

  ## Suspected blast radius

  <!-- Single user? Specific tenant? main branch? Helps prioritize. -->
  ```

- [ ] **Step 3: Create `.github/ISSUE_TEMPLATE/feature.md`**

  ```markdown
  ---
  name: Feature request
  about: Propose a new capability or enhancement
  title: "[feature] "
  labels: ["type:feature"]
  ---

  ## Problem

  <!-- The user-visible pain or workflow gap this solves -->

  ## Proposed solution

  <!-- 1-3 paragraphs. UI sketches in plain text are fine. -->

  ## Alternatives considered

  <!-- Other shapes you weighed and why this one wins -->

  ## Out of scope

  <!-- What this explicitly does NOT cover, to keep the PR small -->

  ## Acceptance criteria

  - [ ]
  - [ ]
  - [ ]
  ```

- [ ] **Step 4: Create `.github/ISSUE_TEMPLATE/security.md`**

  ```markdown
  ---
  name: Security issue
  about: Report a security defect (use private disclosure for sensitive issues)
  title: "[security] "
  labels: ["type:security", "priority:p0"]
  ---

  > **If this issue exposes credentials, customer data, or an active exploit, do NOT file it here.**
  > Email the maintainer directly and request a private channel.

  ## Severity assessment

  - [ ] P0 — exploitable, credentials/data at risk, or `main` compromised
  - [ ] P1 — exploitable but contained
  - [ ] P2 — defense-in-depth gap, not directly exploitable

  ## What's vulnerable

  <!-- Component, route, service, or dependency. NO exploit code. -->

  ## Reproduction (high level only)

  <!-- Enough for a maintainer to reproduce. Detailed exploit goes in private disclosure. -->

  ## Suggested mitigation

  <!-- Optional. Patch sketch, config change, dependency bump, etc. -->

  ## Disclosure status

  - [ ] Private disclosure already sent
  - [ ] Filing publicly because issue is low-severity / informational
  ```

- [ ] **Step 5: Stage, commit, push**

  ```bash
  cd /root/availai
  git add .github/ISSUE_TEMPLATE/bug.md \
          .github/ISSUE_TEMPLATE/feature.md \
          .github/ISSUE_TEMPLATE/security.md
  git commit -m "chore: add bug/feature/security issue templates (Track 2B §2B.3)"
  git push -u origin chore/issue-templates
  ```

- [ ] **Step 6: Open PR-B**

  ```bash
  gh pr create --repo TRIOSCS/Avail-AI-Test \
    --base main --head chore/issue-templates \
    --title "chore: add bug/feature/security issue templates" \
    --body "Implements spec §2B.3 (Track 2B). Three standard templates with type-label defaults. CODEOWNERS is intentionally NOT added (spec §2B.5 dropped — M6 silent-failure: decorative without require_code_owner_reviews on a single-user repo)."
  ```

- [ ] **Step 7: Wait for CI green and verify protection accepts the merge**

  ```bash
  gh pr checks $(gh pr view --repo TRIOSCS/Avail-AI-Test --json number --jq .number) \
    --repo TRIOSCS/Avail-AI-Test --watch
  ```

  Expected: `test` and `security` check-runs both pass.

- [ ] **Step 8: Squash-merge PR-B**

  ```bash
  PR_NUM=$(gh pr view --repo TRIOSCS/Avail-AI-Test --json number --jq .number)
  gh pr merge $PR_NUM --repo TRIOSCS/Avail-AI-Test --squash --delete-branch
  ```

  `--delete-branch` exercises the new `deleteBranchOnMerge` setting from Task 1.

- [ ] **Step 9: Verify the three files exist on `main`**

  ```bash
  for f in bug.md feature.md security.md; do
    gh api "repos/TRIOSCS/Avail-AI-Test/contents/.github/ISSUE_TEMPLATE/$f" \
      --jq '.path' || echo "MISSING: $f"
  done
  ```

  Expected: prints all three paths, no `MISSING` lines.

- [ ] **Step 10: Verify branch was actually deleted on origin**

  ```bash
  gh api repos/TRIOSCS/Avail-AI-Test/branches/chore/issue-templates 2>&1 | grep -q "Branch not found"
  ```

  Expected: exit 0 (i.e. branch is gone — `deleteBranchOnMerge` is functioning).

**Task 2 done when:** Steps 9 and 10 both pass.

---

## Task 3: Harden deploy.sh under linear-history protection (PR-C)

**Spec:** §2B.4.

**Files:**
- Modify: `deploy.sh`

The current `deploy.sh:20` does a bare `git push origin main`. Branch protection from Task 1 (`required_linear_history: true` + `allow_force_pushes: false`) rejects any non-fast-forward push, so any local divergence kills the deploy. Fix: prepend `git pull --rebase origin main` so the local push tip is always a fast-forward of remote `main`.

### Step 0: Decide branch routing based on PR #93's state (KEY DECISION)

The spec (§12 + §2B.4) prefers riding this fix into PR #93's existing scope. Determine #93's state from the PREREQUISITES P5 capture:

- **If PR #93 is OPEN:** open the deploy.sh edit AS A NEW COMMIT on #93's `headRefName` branch, rebased onto then-current `main`. Skip Step 1 below; jump to Step 1-A.
- **If PR #93 is MERGED-WITHOUT-THE-FIX or CLOSED:** the fix lands on a fresh branch as PR-C (this Track 2B's config-PR companion). Continue with Step 1 below.

Re-confirm state at execution time — it may have changed since prereq capture:

```bash
gh pr view 93 --repo TRIOSCS/Avail-AI-Test --json state,headRefName,mergeable
```

- [ ] **Step 1: (FRESH BRANCH path) Cut `fix/deploy-sh-rebase-before-push` from current `main`**

  Skip this step if #93 is OPEN — use Step 1-A instead.

  ```bash
  cd /root/availai
  git fetch origin
  git checkout -b fix/deploy-sh-rebase-before-push origin/main
  ```

- [ ] **Step 1-A: (RIDE-INTO-#93 path) Check out #93's branch and rebase onto current `main`**

  Skip this step if #93 is closed/merged — use Step 1 instead.

  ```bash
  cd /root/availai
  git fetch origin
  PR93_BRANCH=$(gh pr view 93 --repo TRIOSCS/Avail-AI-Test --json headRefName --jq .headRefName)
  git checkout "$PR93_BRANCH"
  git pull --rebase origin "$PR93_BRANCH"
  git rebase origin/main
  ```

  If rebase produces conflicts: STOP, surface them, do not auto-resolve. Conflicts on a foreign PR are not in this plan's scope.

- [ ] **Step 2: Inspect the current push line in `deploy.sh`**

  ```bash
  grep -n "git push origin main" /root/availai/deploy.sh
  ```

  Expected: a single hit at line 20 (the `if NO_COMMIT == false` block). If multiple hits or different line number, read the file and adapt the edit below — DO NOT blind-edit.

- [ ] **Step 3: Apply the diff to `deploy.sh`**

  Exact transformation — find the unique three-line sequence and add one line:

  **BEFORE** (deploy.sh lines 18–20):
  ```bash
          git add -A
          git commit -m "${1:-deploy}"
          git push origin main
  ```

  **AFTER** (deploy.sh lines 18–21):
  ```bash
          git add -A
          git commit -m "${1:-deploy}"
          git pull --rebase origin main
          git push origin main
  ```

  The `git pull --rebase origin main` line goes IMMEDIATELY before `git push origin main`, INSIDE the `if [ "$NO_COMMIT" = false ]` block, at the same 8-space indentation level as the surrounding lines. `set -euo pipefail` (deploy.sh:4) ensures a rebase failure aborts the whole deploy, which is the desired behavior — never push into a divergent state silently.

- [ ] **Step 4: Verify the edit landed correctly**

  ```bash
  grep -n -B 1 -A 1 "git push origin main" /root/availai/deploy.sh
  ```

  Expected: shows `git pull --rebase origin main` on the line immediately before `git push origin main`. Both lines indented identically.

- [ ] **Step 5: Lint check (optional but cheap)**

  ```bash
  bash -n /root/availai/deploy.sh
  ```

  Expected: no output (script parses).

- [ ] **Step 6: Stage, commit, push**

  Commit message differs by routing path:

  - **FRESH BRANCH path (Step 1):**
    ```bash
    cd /root/availai
    git add deploy.sh
    git commit -m "fix(deploy): rebase before push so linear-history protection accepts the deploy (Track 2B §2B.4)"
    git push -u origin fix/deploy-sh-rebase-before-push
    ```

  - **RIDE-INTO-#93 path (Step 1-A):**
    ```bash
    cd /root/availai
    git add deploy.sh
    git commit -m "fix(deploy): rebase before push for linear-history protection (rides into #93 per spec §2B.4)"
    git push --force-with-lease origin "$PR93_BRANCH"
    ```
    `--force-with-lease` is allowed against feature branches per the Track 0.6 hookify rule fix (only `--force` against `main` is blocked).

- [ ] **Step 7: Open PR-C (only on FRESH BRANCH path)**

  Skip if riding into #93 — that PR already exists.

  ```bash
  gh pr create --repo TRIOSCS/Avail-AI-Test \
    --base main --head fix/deploy-sh-rebase-before-push \
    --title "fix(deploy): rebase before push under linear-history protection" \
    --body "Implements spec §2B.4 (Track 2B). Branch protection enabled in Task 1 (required_linear_history: true) rejects non-fast-forward pushes, so deploy.sh now \`git pull --rebase origin main\` before \`git push\`. Verified manually via trial deploy in Step 9."
  ```

- [ ] **Step 8: Wait for CI green and merge (FRESH BRANCH path) — or wait for #93 to merge (RIDE-INTO-#93 path)**

  ```bash
  PR_NUM=$(gh pr view --repo TRIOSCS/Avail-AI-Test --json number --jq .number)
  gh pr checks $PR_NUM --repo TRIOSCS/Avail-AI-Test --watch
  gh pr merge $PR_NUM --repo TRIOSCS/Avail-AI-Test --squash --delete-branch
  ```

- [ ] **Step 9: TRIAL DEPLOY — the load-bearing verification (spec §10 #10)**

  This is the only real-world test that branch protection AND deploy.sh hardening agree. Run a no-commit deploy that exercises `git pull --rebase` and `git push origin main` under live linear-history protection:

  ```bash
  cd /root/availai
  git checkout main
  git pull --rebase origin main
  ./deploy.sh --no-commit
  ```

  Wait for the script to reach Step 4 ("App is healthy!"). If it does:
  - Push succeeded under protection → §2B.4 fix is sufficient.
  - Mark Task 3 done.

  **If `git push origin main` is rejected by GitHub (e.g. `protected branch hook declined`):**
  - The deploy.sh fix alone is NOT sufficient. Likely cause: a required check transitioned from green to pending mid-deploy, or check-run names drifted (re-run Task 1 Step 7).
  - DO NOT add a `bypass` actor without operator sign-off — single-user staging policy still requires intentional protection edits, not silent overrides.
  - DO NOT disable protection. Surface the rejection logs and STOP.
  - Recovery options to present to the operator (in order of preference):
    1. Re-verify §10 #4 protection contexts via Task 1 Step 5 — they may have drifted.
    2. Add the deploy.sh runner as a `bypass_pull_request_allowances` actor on the protection rule (operator decision).
    3. Move deploys behind a dedicated PR + auto-merge flow instead of direct `git push origin main` (architectural change, separate plan).

- [ ] **Step 10: Final post-deploy verification**

  ```bash
  gh run list --repo TRIOSCS/Avail-AI-Test --branch main --limit 1 \
    --json conclusion,headSha --jq '.[0]'
  ```

  Expected: `conclusion == "success"` on the new SHA. Confirms branch protection accepted the push AND the post-push CI passed.

**Task 3 done when:** Step 9 trial deploy succeeds AND Step 10 confirms green CI on the new tip.

---

## Self-review — spec §-numbers mapped to tasks

| Spec § | Subject | Task | Steps |
|---|---|---|---|
| §2B.1 | Repo merge settings (`gh repo edit`) | Task 1 | Steps 2, 3 |
| §2B.2 | Branch protection on `main` | Task 1 | Steps 4, 5 (CRITICAL job-name verification), 6, 7 |
| §2B.3 | Issue templates | Task 2 | Steps 2, 3, 4, 9 |
| §2B.4 | deploy.sh `git pull --rebase` | Task 3 | Steps 0 (#93 routing), 1/1-A, 3, 4 |
| §2B.5 | CODEOWNERS — DROPPED | (none) | Documented in File Structure + Task 2 PR body |
| §10 #4 | Branch-protection verification | Task 1 | Step 6 |
| §10 #5 | Repo-settings verification | Task 1 | Step 3 |
| §10 #10 | deploy.sh trial deploy | Task 3 | Step 9 |
| §11 P0 | Job-name vs workflow-name silent failure | Task 1 | Step 5 (must return `true`) + Step 7 (cross-check) |
| §11 C1 | `strict: false` (avoids cascade deadlock) | Task 1 | Step 4 JSON, Step 5 verification |
| §11 C2 | `deleteBranchOnMerge` × open-worktree interaction | PREREQUISITES | P3 (no orphans) |
| §11 C6 | deploy.sh blocked by linear-history | Task 3 | Step 3 (the fix) + Step 9 (real-world proof) |
| §11 H2 | `deleteBranchOnMerge` orphans stacked PRs | PREREQUISITES | P1 (zero open PRs ⇒ no stacks) |
| §11 M6 | CODEOWNERS decorative without enforcement | Task 2 PR body | Documented as deliberate non-action |
| §12 | PR #93 routing decision | Task 3 | Step 0 (KEY DECISION) + PREREQUISITES P5 |

All §2B.1–§2B.4 subsections covered; §2B.5 explicitly documented as dropped. Every spec §11 silent-failure that touches Track 2B has an explicit verification step. Acceptance: spec §10 checks #4, #5, and #10 all pass.
