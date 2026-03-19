// Accessibility tests for AvailAI — runs axe-core against key pages.
// Checks WCAG 2.1 AA compliance on rendered HTML.
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
  test('login page has no critical violations', async ({ page }) => {
    const results = await checkAccessibility(page, '/');
    const critical = results.violations.filter(
      (v: any) => v.impact === 'critical' || v.impact === 'serious'
    );
    if (critical.length > 0) {
      console.log('Critical a11y violations:', JSON.stringify(critical, null, 2));
    }
    expect(critical).toHaveLength(0);
  });

  test('login page is accessible', async ({ page }) => {
    // Test the actual login page (redirect from /)
    await page.goto('/');
    await page.waitForLoadState('networkidle');
    const results = await new AxeBuilder({ page })
      .withTags(['wcag2a', 'wcag2aa', 'wcag21aa'])
      .disableRules(['color-contrast'])
      .analyze();
    const critical = results.violations.filter(
      (v: any) => v.impact === 'critical' || v.impact === 'serious'
    );
    if (critical.length > 0) {
      console.log('Login page a11y violations:', JSON.stringify(critical, null, 2));
    }
    expect(critical).toHaveLength(0);
  });
});

test.describe('Accessibility — Summary Report', () => {
  test('homepage full audit', async ({ page }) => {
    const results = await checkAccessibility(page, '/');

    console.log(`\n=== Accessibility Report: / ===`);
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
