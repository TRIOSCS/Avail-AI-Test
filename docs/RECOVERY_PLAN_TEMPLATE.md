# Recovery Plan Template

## Phase 1 — Audit only
- map the current shell/layout structure
- identify the exact templates/components/CSS causing the compression/overlap
- compare against intended list → detail UX
- stop

## Phase 2 — Layout repair plan
- propose the smallest safe slices
- likely order:
  1. shell/content width fix
  2. results/list pane sizing fix
  3. detail pane rendering fix
  4. spacing/overflow cleanup
  5. sourcing-specific polish
- stop

## Phase 3+ — one slice at a time
For each slice:
- make smallest safe diff
- explain root cause
- explain fix
- run checks
- stop

## Important
Do not rewrite the entire page just because the layout is broken.
Repair the shell/content contract first.
