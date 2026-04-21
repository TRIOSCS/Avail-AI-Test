/**
 * requisitions2-resize.spec.ts — E2E tests for resizable split, columns, and tooltips.
 * Called by: npx playwright test e2e/requisitions2-resize.spec.ts
 * Depends on: running app server with TESTING=1 (seeds test@availai.local / testpass)
 */
import { test, expect, Page } from '@playwright/test';

const REQS_URL = '/requisitions2';

/** Log in using the test user seeded by startup._bootstrap_test_db (TESTING=1). */
async function loginAsTestUser(page: Page) {
  const res = await page.request.post('/auth/login', {
    form: { email: 'test@availai.local', password: 'testpass' },
  });
  if (res.status() !== 200) {
    const body = await res.text();
    throw new Error(`Login failed (${res.status()}): ${body}`);
  }
}

async function clearLayout(page: Page) {
  await page.evaluate(() => {
    localStorage.removeItem('avail_split_rq2');
    localStorage.removeItem('avail_table_cols_rq2-list');
    localStorage.removeItem('avail_table_cols_rq2-parts');
  });
}

test.describe('Requisitions split divider', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsTestUser(page);
    await page.goto(REQS_URL);
    await clearLayout(page);
    await page.reload();
  });

  test('split divider is draggable and position persists after reload', async ({ page }) => {
    await page.goto(REQS_URL);

    const divider = page.locator('[role="separator"][aria-label="Resize panels"]');
    await expect(divider).toBeVisible();

    const box = await divider.boundingBox();
    if (!box) throw new Error('divider not visible');

    // Drag divider 150px to the right from center
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await page.mouse.down();
    await page.mouse.move(box.x + box.width / 2 + 150, box.y + box.height / 2, { steps: 10 });
    await page.mouse.up();

    const savedPct = await page.evaluate(() => localStorage.getItem('avail_split_rq2'));
    expect(savedPct).not.toBeNull();
    const pct = Number(savedPct);
    expect(Number.isFinite(pct)).toBe(true);
    expect(pct).toBeGreaterThan(40);
    expect(pct).toBeLessThanOrEqual(70);

    // Reload; assert the saved width is applied
    await page.reload();
    const leftPanel = page.locator('#split-rq2 > div').first();
    const style = await leftPanel.getAttribute('style');
    expect(style).toContain(`width: ${pct}%`);
  });
});
