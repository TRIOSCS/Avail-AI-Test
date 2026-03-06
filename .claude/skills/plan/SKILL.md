---
name: plan
description: Create a design doc + implementation plan in docs/plans/ following project conventions
disable-model-invocation: true
---

Ask the user what feature they want to build if not provided as an argument.

Create two files in `docs/plans/` using today's date:

## 1. Design doc: `docs/plans/YYYY-MM-DD-{slug}-design.md`

Include sections:
- **Problem Statement** — what problem this solves
- **Proposed Approach** — high-level technical approach
- **Data Model Changes** — new tables, columns, migrations needed
- **API Changes** — new endpoints, modified responses, request schemas
- **Frontend Changes** — UI modifications, new views, JS changes
- **Risks and Alternatives** — what could go wrong, other approaches considered

## 2. Implementation plan: `docs/plans/YYYY-MM-DD-{slug}-plan.md`

Include:
- Numbered task list (aim for 5-10 tasks)
- Each task: description, files to create/modify, dependencies on other tasks
- Every implementation task must have corresponding test coverage
- Final task is always: run full test suite + coverage check + deploy

Use the slug format: lowercase, hyphens, no special characters (e.g., `contact-enrichment-ui`).
