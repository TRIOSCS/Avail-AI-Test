# Track 2A — Land-Now Policy Implementation Plan

> **For agentic workers:** Two tiny, fully independent repo changes from `2026-05-08-github-cleanup-design.md` §2A. Each artifact ships as its OWN PR. Both can land any time, in parallel with Track 1 — they have NO merge-blocking effect on the cascade. Do not combine them. Do not wait for Track 1. §2A.3 (`deleteBranchOnMerge`) and §2A.4 (Dependabot github-actions ecosystem) are explicitly REMOVED in the spec — do not implement them here.

## Goal

Land the two policy artifacts that are safe to ship at any time:
1. `.github/PULL_REQUEST_TEMPLATE.md` populating the GitHub PR description box with Summary / Test plan / Risk & rollback / Linked issues sections (spec §2A.1).
2. Issue label taxonomy (`type:*`, `priority:*`, `scope:*`) applied to the repo via `gh label create --force`, then back-applied to existing issues #88 and #89 (spec §2A.2).

Success = template renders for the next PR opened, `gh label list` returns all 13 names, and `gh issue view 88/89` shows the assigned labels.

## Architecture

- **PR template** is a single Markdown file at `.github/PULL_REQUEST_TEMPLATE.md`. GitHub auto-injects its body into every new PR description. Single-file PR. No code, no tests.
- **Labels** are repo-level configuration, NOT files. Applied via `gh label create --force` (idempotent — re-run safely). The same script back-applies labels to #88 and #89 via `gh issue edit`. The script is run-once-and-discard; nothing to commit. To keep the work auditable, the script is committed under `scripts/github/apply-labels.sh` so it can be re-run if the taxonomy ever drifts.

## Tech Stack

- GitHub PR template (Markdown)
- `gh` CLI (label create, issue edit)
- Bash (idempotent re-runnable script)
- Repo: `TRIOSCS/Avail-AI-Test`

## File Structure

| Path | Purpose |
|---|---|
| `.github/PULL_REQUEST_TEMPLATE.md` | PR description scaffold (spec §2A.1) |
| `scripts/github/apply-labels.sh` | Idempotent script: creates 13 labels + applies to #88, #89 (spec §2A.2) |

## PR plan

Two independent PRs. Either may land first. Both target `main`.

- **PR-A** — title: `chore(github): add PR template (spec §2A.1)` — Step 1.
- **PR-B** — title: `chore(github): apply issue label taxonomy (spec §2A.2)` — Step 2.

---

## Step 1 — PR template (PR-A)

### 1.1 Create the PR template file

Create `.github/PULL_REQUEST_TEMPLATE.md` with this EXACT content (copied verbatim from spec §2A.1):

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

### 1.2 Verify the file path GitHub looks for

GitHub auto-discovers any of: `.github/PULL_REQUEST_TEMPLATE.md`, `.github/pull_request_template.md`, `docs/PULL_REQUEST_TEMPLATE.md`, or repo-root `PULL_REQUEST_TEMPLATE.md`. We use `.github/PULL_REQUEST_TEMPLATE.md` for clarity. Confirm the file is at exactly that path with `git ls-files .github/PULL_REQUEST_TEMPLATE.md`.

### 1.3 Open PR-A

Branch: `chore/pr-template`. Push and open with:

```bash
gh pr create \
  --title "chore(github): add PR template (spec §2A.1)" \
  --body "$(cat <<'EOF'
## Summary

Adds `.github/PULL_REQUEST_TEMPLATE.md` per `docs/superpowers/specs/2026-05-08-github-cleanup-design.md` §2A.1. From now on every new PR opens with Summary / Test plan / Risk & rollback / Linked issues sections pre-filled.

## Test plan

- [ ] After merge, open a throwaway draft PR and confirm the template body appears
- [ ] Pre-commit clean (Markdown only — no code change)

## Risk & rollback

Zero blast radius — template only affects new PR description scaffolding. Revert by deleting the file.

## Linked issues

N/A (Track 2A.1 of GitHub cleanup design)
EOF
)"
```

### 1.4 Self-verify before requesting review

```bash
test -f .github/PULL_REQUEST_TEMPLATE.md && \
  grep -q "## Summary" .github/PULL_REQUEST_TEMPLATE.md && \
  grep -q "## Test plan" .github/PULL_REQUEST_TEMPLATE.md && \
  grep -q "## Risk & rollback" .github/PULL_REQUEST_TEMPLATE.md && \
  grep -q "## Linked issues" .github/PULL_REQUEST_TEMPLATE.md && \
  echo OK
```

Expect `OK`. After merge, open a throwaway draft PR (`gh pr create --draft --title test --body ""` then immediately `gh pr close --delete-branch`) and visually confirm the template body rendered into the description.

---

## Step 2 — Label taxonomy (PR-B)

### 2.1 Create `scripts/github/apply-labels.sh`

The script is idempotent — `--force` updates an existing label in place rather than failing. Re-running is safe.

File: `scripts/github/apply-labels.sh`. Mark executable (`chmod +x`).

```bash
#!/usr/bin/env bash
# apply-labels.sh — Idempotent label taxonomy for TRIOSCS/Avail-AI-Test
# Implements docs/superpowers/specs/2026-05-08-github-cleanup-design.md §2A.2.
# Re-run safely: `gh label create --force` updates existing labels in place.
# Called by: humans (one-shot) when taxonomy drifts. Depends on: gh CLI.
set -euo pipefail

# type:*  — what kind of change
gh label create "type:bug"      --description "Bug fix"                --color "d73a4a" --force
gh label create "type:feature"  --description "New feature"            --color "0e8a16" --force
gh label create "type:chore"    --description "Maintenance / cleanup"  --color "c2e0c6" --force
gh label create "type:security" --description "Security-related"       --color "b60205" --force
gh label create "type:docs"     --description "Documentation only"     --color "0075ca" --force

# priority:*  — urgency tier
gh label create "priority:p0" --description "Drop everything"      --color "b60205" --force
gh label create "priority:p1" --description "Important, this week" --color "d93f0b" --force
gh label create "priority:p2" --description "Nice to have"         --color "fbca04" --force

# scope:*  — area of the codebase
gh label create "scope:ci"       --description "CI / GitHub Actions"     --color "ededed" --force
gh label create "scope:db"       --description "Database / migrations"   --color "5319e7" --force
gh label create "scope:frontend" --description "HTMX / Alpine / Jinja2"  --color "1d76db" --force
gh label create "scope:backend"  --description "FastAPI / services"      --color "006b75" --force
gh label create "scope:infra"    --description "Docker / deploy / hosts" --color "bfdadc" --force

# Back-apply to existing issues per spec §2A.2.
gh issue edit 88 --add-label "type:chore,scope:frontend"
gh issue edit 89 --add-label "type:chore,scope:frontend"

echo "Labels applied. Verify with: gh label list"
```

### 2.2 Run the script against the repo

This is the one-shot apply. Run from repo root:

```bash
bash scripts/github/apply-labels.sh
```

Expect 13 lines of `gh label create` output (one per label) + 2 lines from `gh issue edit` + the trailing `Labels applied.` message. Non-zero exit = halt and surface.

### 2.3 Verify

```bash
gh label list --json name --jq '
  map(.name)
  | contains(["type:bug","type:feature","type:chore","type:security","type:docs",
              "priority:p0","priority:p1","priority:p2",
              "scope:ci","scope:db","scope:frontend","scope:backend","scope:infra"])
'
```

Expect `true`. Then:

```bash
gh issue view 88 --json labels --jq '[.labels[].name] | contains(["type:chore","scope:frontend"])'
gh issue view 89 --json labels --jq '[.labels[].name] | contains(["type:chore","scope:frontend"])'
```

Both expect `true`.

### 2.4 Open PR-B

Branch: `chore/issue-label-taxonomy`. The PR commits ONLY `scripts/github/apply-labels.sh` (the side-effect against the repo has already been applied in 2.2 — labels live on the GitHub side, not in the repo). Open with:

```bash
gh pr create \
  --title "chore(github): apply issue label taxonomy (spec §2A.2)" \
  --body "$(cat <<'EOF'
## Summary

Adds `scripts/github/apply-labels.sh` per `docs/superpowers/specs/2026-05-08-github-cleanup-design.md` §2A.2. Script is idempotent (`gh label create --force`); already executed once to seed the 13 labels and back-apply `type:chore,scope:frontend` to issues #88 and #89. Committed so the taxonomy is reproducible if it ever drifts.

## Test plan

- [ ] `bash scripts/github/apply-labels.sh` runs cleanly on a second invocation (no errors, exit 0)
- [ ] `gh label list` shows all 13 `type:* / priority:* / scope:*` labels
- [ ] `gh issue view 88` and `gh issue view 89` show `type:chore` + `scope:frontend`

## Risk & rollback

Zero code blast radius — script-only commit. The label side-effect is reversible by `gh label delete <name>`.

## Linked issues

N/A (Track 2A.2 of GitHub cleanup design)
EOF
)"
```

---

## Self-review — spec coverage

| Spec section | Status | Where in this plan |
|---|---|---|
| §2A.1 PR template — exact body | Implemented | Step 1.1 (verbatim copy) |
| §2A.1 path `.github/PULL_REQUEST_TEMPLATE.md` | Implemented | Step 1.1 / 1.2 |
| §2A.2 13 labels (`type:*` x5, `priority:*` x3, `scope:*` x5) | Implemented | Step 2.1 |
| §2A.2 idempotent via `--force` | Implemented | Step 2.1 (every line uses `--force`) |
| §2A.2 issue #88 → `type:chore scope:frontend` | Implemented | Step 2.1 (`gh issue edit 88`) |
| §2A.2 issue #89 → `type:chore scope:frontend` | Implemented | Step 2.1 (`gh issue edit 89`) |
| §2A.3 `deleteBranchOnMerge` — REMOVED, moved to Track 2B | Out of scope | Not implemented (per instructions) |
| §2A.4 Dependabot github-actions ecosystem — REMOVED (already exists) | Out of scope | Not implemented (per instructions) |
| Independence from Track 1 | Honored | Plan has no Prerequisites section; both PRs land any time |
| Each artifact = its own PR | Honored | PR-A (template) and PR-B (label script) are separate |

Done when both PRs are merged AND the §10 success-criteria spot-checks for §2A pass:

```bash
test -f .github/PULL_REQUEST_TEMPLATE.md \
  && gh label list --json name --jq 'map(.name) | contains(["type:bug","priority:p0","scope:ci"])'
```
