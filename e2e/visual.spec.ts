// Visual regression tests for AvailAI — captures screenshots of key pages.
// Uses Playwright's built-in toHaveScreenshot() for pixel comparison.
// First run: use `npm run test:visual:update` to create baseline screenshots.
// Called by: npx playwright test --project=visual
// Depends on: running app server

import { test, expect } from '@playwright/test';

test.describe('Visual Regression — Key Pages', () => {
  test('login page visual', async ({ page }) => {
    await page.goto('/');
    await page.waitForLoadState('networkidle');
    await expect(page).toHaveScreenshot('login-page.png', {
      maxDiffPixelRatio: 0.05,
      fullPage: true,
    });
  });
});
