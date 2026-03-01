# Frontend Safety Nets — Design Doc

**Date**: 2026-03-01
**Status**: Approved
**Goal**: Stop recurring frontend crashes without restructuring the codebase.

## Problem

The frontend has crashed 11 times from the same class of bug: a JS function gets removed/renamed but its reference in `Object.assign(window, {...})` or an HTML `onclick` handler stays behind. The dangling reference throws `ReferenceError`, which crashes the entire ES module, killing all 504 exported functions. The whole app goes blank.

Three people edit these files (owner + AI assistants + a developer). There are no frontend tests or checks beyond Vite's default bundling.

## Solution: Three Layers of Defense

### Layer 1 — Extended Vite Build Checks (vite.config.js)

Extend the existing `checkExportsPlugin()` with three additional validations:

**Check A — HTML onclick handlers**: Parse `index.html`, extract every function name from inline handlers (`onclick`, `oninput`, `onchange`, `onkeydown`), verify each exists as a defined or imported function in `app.js` or `crm.js`.

**Check B — crm.js import verification**: Parse the `import { ... } from 'app'` block in `crm.js`, verify every imported name has a matching `export` in `app.js`.

**Check C — getElementById consistency**: Extract every `getElementById("someId")` call from JS, verify a matching `id="someId"` exists in `index.html`. One-way: not all HTML IDs need JS refs, but all JS refs need HTML IDs.

All checks run at `buildStart()`. Build fails with clear error messages listing every mismatch.

### Layer 2 — Post-Build Smoke Test (scripts/smoke-test-bundles.mjs)

After Vite builds the bundles, load them in a jsdom environment and verify:

1. **No ReferenceErrors on load** — the exact crash class that keeps hitting production.
2. **All Object.assign exports are callable** — after module execution, verify every `window.functionName` is `typeof 'function'`.
3. **crm.js loads after app.js without errors** — validates the import chain end-to-end.

Runs as `npm run postbuild`. Catches runtime issues that static analysis misses (circular deps, initialization errors).

**Dependency**: `jsdom` added to devDependencies (build-stage only, not in production image).

### Layer 3 — Pre-Push Git Hook (.githooks/pre-push)

Shell script that runs `npm run build` (which triggers Layer 1 + Layer 2). If the build fails, the push is rejected.

Setup: `git config core.hooksPath .githooks` (one-time, documented in README).

## What This Does NOT Cover

- Logic bugs inside functions (wrong args, bad state)
- CSS/layout regressions
- Performance issues
- API contract changes

These are real risks but require different solutions (TypeScript, E2E tests, API schema validation). This design targets only the #1 recurring crash pattern.

## Files Changed

| File | Change |
|------|--------|
| `vite.config.js` | Extend `checkExportsPlugin()` with checks A, B, C |
| `scripts/smoke-test-bundles.mjs` | New — post-build runtime smoke test |
| `package.json` | Add `jsdom` devDep, add `postbuild` script |
| `.githooks/pre-push` | New — runs build before allowing push |
| `.dockerignore` | No change needed (scripts/ already excluded, vite.config.js is copied) |
| `Dockerfile` | No change needed (jsdom is devDep, not installed in production) |

## Rollback

All changes are additive checks. If any check produces false positives, it can be disabled by removing the relevant section from `checkExportsPlugin()` or deleting the `postbuild` script. No production code is modified.
