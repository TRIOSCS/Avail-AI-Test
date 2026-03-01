# Frontend Safety Nets — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add three layers of build-time checks that prevent the recurring frontend crash class (dangling function references) from reaching production.

**Architecture:** Extend the existing Vite `checkExportsPlugin()` with HTML handler and import validation. Add a post-build jsdom smoke test that loads the built bundles and verifies no ReferenceErrors. Add a git pre-push hook that runs the full build before allowing pushes.

**Tech Stack:** Vite 6 plugin API, jsdom (devDependency), git hooks (shell script).

---

### Task 1: Extend Vite Plugin — HTML Onclick Handler Check

**Files:**
- Modify: `vite.config.js:10-59` (extend checkExportsPlugin)

**Step 1: Add HTML handler extraction to the plugin**

Add a new function and call it from `buildStart()`. This parses `index.html`, extracts every function name from inline handlers (`onclick`, `oninput`, `onchange`, `onkeydown`), and verifies each exists in app.js or crm.js definitions.

The BUILTIN set should include DOM methods that appear in handlers but aren't our functions: `if`, `event`, `this`, `document`, `window`, `console`, `navigator`, `getElementById`, `querySelector`, `querySelectorAll`, `preventDefault`, `stopPropagation`, `focus`, `click`, `scrollBy`, `contains`, `match`, `catch`, `then`, `fetch`, `reload`, `writeText`.

Extract function call names from handler bodies (word followed by open paren), skip builtins, collect into a Set, then verify each exists in the combined definitions from both JS files.

**Step 2: Test the check catches a bad handler**

Temporarily add `onclick="fakeBrokenHandler()"` to index.html, run `npm run build`, verify it fails with a clear error. Remove the test line with `git checkout app/templates/index.html`.

**Step 3: Run clean build to verify no false positives**

```bash
npm run build
```

Expected: Build succeeds with no errors.

**Step 4: Commit**

```bash
git add vite.config.js
git commit -m "Add build-time check: HTML onclick handlers must reference defined functions"
```

---

### Task 2: Extend Vite Plugin — crm.js Import Verification

**Files:**
- Modify: `vite.config.js` (extend checkExportsPlugin, same buildStart)

**Step 1: Add import-vs-export cross-check**

Parse crm.js for `import { ... } from 'app'` blocks, extract imported names. Parse app.js for `export function` and `export const/let/var` declarations. Verify every crm.js import has a matching app.js export.

**Step 2: Test the check catches a bad import**

Temporarily add `fakeBrokenImport,` to the crm.js import block, run build, verify failure. Restore with `git checkout app/static/crm.js`.

**Step 3: Run clean build**

```bash
npm run build
```

Expected: Build succeeds.

**Step 4: Commit**

```bash
git add vite.config.js
git commit -m "Add build-time check: crm.js imports must match app.js exports"
```

---

### Task 3: Post-Build Smoke Test with jsdom

**Files:**
- Create: `scripts/smoke-test-bundles.mjs`
- Modify: `package.json` (add jsdom devDep, add postbuild script)

**Step 1: Install jsdom**

```bash
npm install --save-dev jsdom
```

**Step 2: Write the smoke test script**

Create `scripts/smoke-test-bundles.mjs` that:
1. Reads the Vite manifest to find the built bundle filenames
2. Creates a JSDOM instance with a minimal HTML document (including the `app-config` JSON script tag)
3. Stubs browser APIs jsdom doesn't provide: `matchMedia`, `IntersectionObserver`, `MutationObserver`, `fetch`, `localStorage`, `history.pushState/replaceState`, `navigator.clipboard`, `html2canvas`
4. Uses `vm.Script` (Node's VM module) to run each bundle in the jsdom window context — this avoids direct use of eval while still testing runtime execution
5. Catches any errors during execution
6. Extracts export names from the Object.assign block in the built bundle and verifies each is a function on window
7. Reports pass/fail with counts

**Step 3: Add postbuild script to package.json**

```json
"postbuild": "node scripts/smoke-test-bundles.mjs"
```

**Step 4: Run full build + smoke test**

```bash
npm run build
```

Expected: Vite build succeeds, then smoke test runs and prints `SMOKE TEST PASSED: bundles loaded, N exports verified.`

**Step 5: Commit**

```bash
git add scripts/smoke-test-bundles.mjs package.json package-lock.json
git commit -m "Add post-build smoke test: load bundles in jsdom, verify no ReferenceErrors"
```

---

### Task 4: Pre-Push Git Hook

**Files:**
- Create: `.githooks/pre-push`

**Step 1: Create the hook script**

Shell script that:
1. Checks if any JS/HTML/CSS files or vite.config.js or package.json changed in the commits being pushed
2. If yes, runs `npm run build` (triggers all checks)
3. If build fails, blocks the push with a clear error message
4. If no frontend files changed, skips the check

**Step 2: Make it executable and configure git**

```bash
chmod +x .githooks/pre-push
git config core.hooksPath .githooks
```

**Step 3: Test the hook**

```bash
git push --dry-run
```

**Step 4: Commit**

```bash
git add .githooks/pre-push
git commit -m "Add pre-push hook: block pushes when frontend build checks fail"
```

---

### Task 5: Final Integration Test + Deploy

**Step 1: Run the full build pipeline end-to-end**

```bash
npm run build
```

Expected: All checks pass, smoke test passes.

**Step 2: Rebuild Docker image**

```bash
docker compose up -d --build
```

Expected: Build succeeds (jsdom is devDep so not in production image). App starts healthy.

**Step 3: Verify app logs are clean**

```bash
docker compose logs app --tail=10
```

**Step 4: Final commit + push**

```bash
git push
```

Expected: Pre-push hook runs build, passes, push succeeds.
