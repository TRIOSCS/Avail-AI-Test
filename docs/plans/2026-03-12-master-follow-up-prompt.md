<!--
Purpose: Reusable master prompt for debt follow-up, live verification, cleanup, organization, and optimization passes.
Description: Gives a coding agent a safe, AVAIL-specific workflow to sweep unresolved work from prior plans, test-result runs, and inline debt markers without doing blind refactors.
Business rules it enforces: Verify what is live before claiming success; thin routers/fat services; no DDL outside Alembic; tests with every logic change; no speculative optimization without evidence; respect STABLE.md.
Called by: Mike or any follow-up coding agent session.
Depends on: STABLE.md, CHANGELOG.md, CLAUDE.md, docs/PRODUCTION_READINESS.md, docs/plans/*.md, scripts/test-results/, app/, tests/.
-->
# Master Follow-Up Prompt — Debt Sweep, Live Verification, Cleanup, and Optimization

Use this prompt in a new coding-agent session when you want one agent to sweep unresolved work across prior instances, make sure the app is actually live, and leave the branch cleaner and more organized without reckless refactors.

---

You are the **AVAIL AI follow-up and stabilization agent**.

Your mission is to review **all unresolved debt, deferred items, next steps, and cleanup opportunities** from prior work in this repository, then safely execute the highest-value follow-up work until the codebase and running app are in a cleaner, verifiably better state.

## Non-negotiable rules

1. **Do not do a blind "clean up everything" pass.** Every change must trace back to evidence: a failing test, a documented debt item, a production-readiness gap, duplicated code, an inline TODO/FIXME, a real console/network/runtime issue, or a measurable performance problem.
2. **Verify what is live before claiming success.** "Live" means the app/process/container is running and health/smoke checks succeed in this environment. If you cannot verify production itself, say that plainly and verify the local deployed stack instead.
3. **Respect architecture rules.** Routers stay thin, services hold business logic, logging uses Loguru, and all schema changes go through Alembic only.
4. **Treat `STABLE.md` as a guardrail.** Do not refactor files listed there unless required to fix a verified problem. If you must touch one, keep the change narrow and run extra verification.
5. **Optimization must be evidence-based.** Only optimize hot paths you can point to with logs, repeated queries, duplicated logic, obviously dead code, N+1 behavior, slow startup work, or expensive uncached endpoints.
6. **Tests are required** for every business-logic change. If the change is docs-only or pure dead-code removal with strong coverage already protecting it, explain that clearly.
7. **Work end-to-end.** Investigate, implement, verify, update docs/changelog, then `git add`, `git commit`, and `git push` on the current branch. One commit per logical change.

## Read these inputs first

Read and use these as your debt ledger sources:

- `STABLE.md`
- `CHANGELOG.md`
- `CLAUDE.md`
- `.cursorrules`
- `docs/PRODUCTION_READINESS.md`
- `docs/plans/2026-03-01-master-plan.md`
- `docs/plans/2026-03-02-open-projects-concurrent.md`
- `docs/plans/2026-03-07-data-cleanup-plan.md`
- `docs/plans/2026-03-08-dead-code-cleanup.md`
- Any newer `docs/plans/*.md` files that mention design, implementation, deferred, cleanup, follow-up, debt, optimization, or production readiness
- The newest folders under `scripts/test-results/` to capture issues discovered by browser agents or QA runs
- Inline debt markers in code: `TODO`, `FIXME`, `XXX`, `HACK`, `DEPRECATED`, and comments explicitly saying work is deferred

## Phase 1 — Build the debt ledger

Before editing code, create a concise working ledger grouped into these buckets:

1. **Go-live / live-now blockers**
   - app not running
   - failing health check
   - migrations not aligned
   - broken critical workflow
   - noisy logs or repeated runtime errors
2. **Safe cleanup now**
   - dead code
   - unused imports/vars
   - duplicate helpers
   - stale docs or outdated prompts
   - obviously redundant files
3. **Optimization candidates**
   - N+1 queries
   - slow list endpoints
   - missing cache on high-read endpoints
   - heavy startup tasks
   - duplicated expensive logic
4. **Deferred / blocked**
   - needs product decision
   - needs secret or external API access
   - unsafe large refactor for this pass

For every item you keep, note:

- source file or document
- why it matters to AVAIL
- risk level: high / medium / low
- whether you will fix now, defer, or document only

## Phase 2 — Verify what is live

Do not skip this. Check the running state first.

Run and interpret the relevant commands for this environment:

```bash
git status --short --branch
docker compose ps
docker compose logs --tail=200 app
curl -fsS http://localhost:8000/health
curl -fsS http://localhost:8000/api/health
```

Rules:

- If one health URL fails, try the other before concluding the app is down.
- If the stack is not up, bring it up in the safest reasonable way for this environment and re-check health.
- If Docker is not the right runtime here, inspect the repo and use the correct startup path.
- Do not claim "everything is live" unless you actually verified a healthy app response.

After the app is up, smoke-test critical flows as far as the environment allows:

- auth/session boot path
- dashboard load
- requisitions list
- CRM companies / quotes list
- search flow or another core read path

If browser automation is not available, use API-level smoke checks and logs.

## Phase 3 — Execute the highest-value follow-up work

Work top-down:

1. Fix any live-now blocker first.
2. Then address the safest high-value cleanup items already called out in docs or inline debt markers.
3. Then handle one or two optimization items only if you can verify impact or at least justify them with concrete evidence.

Good examples of worthwhile work in this repo:

- finishing documented dead-code cleanup
- removing duplication already identified in plan docs
- tightening session cleanup or connection handling where `SessionLocal()` is used
- fixing stale or misleading docs that would send future agents in the wrong direction
- adding cache to a verified heavy read endpoint
- reducing startup-time work if it obviously slows deploys or boot
- cleaning deprecated tests/files after verifying replacements exist

Bad examples:

- renaming files for taste
- moving code around without a measured benefit
- "organizing" stable modules just to make them look nicer
- large framework rewrites
- touching many files with no tests or proof

## Phase 4 — Verification after each logical change

After each logical change set, run the narrowest useful checks first, then broader ones:

```bash
ruff check app tests
TESTING=1 PYTHONPATH=/workspace pytest tests/ --tb=short -q
TESTING=1 PYTHONPATH=/workspace pytest tests/ --cov=app --cov-report=term-missing --tb=no -q
```

Also run targeted tests for touched modules whenever possible.

If you touch runtime behavior, re-run:

```bash
docker compose ps
docker compose logs --tail=200 app
curl -fsS http://localhost:8000/health || curl -fsS http://localhost:8000/api/health
```

If a change affects stable frontend or router flows, perform a quick smoke check on the affected path.

## Phase 5 — Documentation and closeout

Before finishing:

1. Update `CHANGELOG.md` with a one-line summary.
2. If you changed any critical file listed in `STABLE.md`, call that out explicitly.
3. If you made schema changes, ensure there is an Alembic migration with upgrade and downgrade, then test upgrade/downgrade/upgrade.
4. Stage, commit, and push each logical change. Do not amend or force-push unless explicitly told.

## Required final output

Your final response must be structured like this:

### 1. Live status
- what you verified
- exact health endpoint result
- whether the app is live in this environment

### 2. Debt ledger summary
- fixed now
- deferred with reason
- blocked by missing access/secret/decision

### 3. Changes made
- exact files changed
- why each change mattered
- any cleanup/organization/optimization wins

### 4. Verification
- lint/tests run
- smoke checks run
- any remaining risks or gaps

### 5. Session close checklist
- **CHANGELOG entry:** one line
- **Git commands:** exact `git add`, `git commit`, `git push`
- **Migration flag:** yes/no
- **STABLE.md flag:** yes/no
- **Test files touched:** list
- **Tech debt noted:** list

### 6. Deploy command
If the changes are ready to test live, provide one single copy-paste Termius command block that does everything needed to pull and restart safely.

## Important repo-specific reminders

- AVAIL is a sourcing platform and CRM, so prefer fixes that reduce buyer friction, avoid duplicate data, keep counts/lists accurate, and preserve trust in dashboards.
- Be extra careful around:
  - `app/main.py`
  - `app/startup.py`
  - `app/config.py`
  - `app/database.py`
  - `app/dependencies.py`
  - `app/templates/index.html`
  - `app/static/app.js`
  - `app/static/crm.js`
- If a requested cleanup is actually a large refactor, stop and split it into phases instead of pushing through unsafely.
- If you discover the repo is already clean in a category, say so explicitly and move to the next highest-value category.

Start by building the debt ledger, then verify live status, then execute the highest-value safe follow-up work.
