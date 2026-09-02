// Accessibility tests for AvailAI — runs axe-core against key pages.
// Checks WCAG 2.1 AA compliance on rendered HTML.
// Runs AUTHED (storageState from e2e/auth.setup.ts): `/` follows its 302 into
// the real app shell (/v2/requisitions), so the scans cover the product UI,
// not the login page. Verified zero critical/serious violations authed before
// this conversion (2026-09-02 local run: 0 violations, 23 passes).
//
// GUARDRAILS for future scans here:
// - NEVER target /v2/sightings or any SSE-bearing page (they hold an
//   EventSource open with 15s server pings — `networkidle` never resolves).
// - NEVER use waitForLoadState('networkidle') — the authed shell polls badge
//   endpoints; use 'domcontentloaded' + an explicit element wait instead.
//
// Called by: npx playwright test --project=accessibility
// Depends on: @axe-core/playwright, app pages

import { test, expect } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';

// Helper: run axe on a page and return violations
async function checkAccessibility(page: any, url: string, disabledRules: string[] = []) {
  const response = await page.goto(url);
  expect(response?.ok()).toBeTruthy();

  const results = await new AxeBuilder({ page })
    .withTags(['wcag2a', 'wcag2aa', 'wcag21aa'])
    .disableRules([
      'color-contrast',  // Tailwind brand colors may trigger this — audit separately
      ...disabledRules,
    ])
    .analyze();

  return results;
}

test.describe('Accessibility — WCAG 2.1 AA', () => {
  test('authed shell has no critical violations', async ({ page }) => {
    const results = await checkAccessibility(page, '/');
    const critical = results.violations.filter(
      (v: any) => v.impact === 'critical' || v.impact === 'serious'
    );
    if (critical.length > 0) {
      console.log('Critical a11y violations:', JSON.stringify(critical, null, 2));
    }
    expect(critical).toHaveLength(0);
  });

  test('authed shell is accessible after full render', async ({ page }) => {
    // `/` 302s into /v2/requisitions for the seeded admin. domcontentloaded +
    // an explicit shell-element wait (NOT networkidle — see header guardrail).
    await page.goto('/', { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('#main-content');
    // Authed tripwire: the login page is ALSO axe-clean, so without this the
    // scans would pass silently if the project regressed to anonymous. The
    // bottom-nav module labels only render in the authed shell.
    await expect(page.locator('body')).toContainText('Sales Hub');
    const results = await new AxeBuilder({ page })
      .withTags(['wcag2a', 'wcag2aa', 'wcag21aa'])
      .disableRules(['color-contrast'])
      .analyze();
    const critical = results.violations.filter(
      (v: any) => v.impact === 'critical' || v.impact === 'serious'
    );
    if (critical.length > 0) {
      console.log('Authed shell a11y violations:', JSON.stringify(critical, null, 2));
    }
    expect(critical).toHaveLength(0);
  });
});

test.describe('Accessibility — Summary Report', () => {
  test('authed homepage full audit', async ({ page }) => {
    const results = await checkAccessibility(page, '/');

    console.log(`\n=== Accessibility Report: / (authed) ===`);
    console.log(`Violations: ${results.violations.length}`);
    console.log(`Passes: ${results.passes.length}`);
    console.log(`Incomplete: ${results.incomplete.length}`);

    for (const v of results.violations) {
      console.log(`  [${v.impact}] ${v.id}: ${v.description} (${v.nodes.length} elements)`);
    }

    // Fail only on critical/serious — warn on moderate/minor
    const serious = results.violations.filter(
      (v: any) => v.impact === 'critical' || v.impact === 'serious'
    );
    expect(serious).toHaveLength(0);
  });
});
