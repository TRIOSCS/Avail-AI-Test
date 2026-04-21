/**
 * requisitions2-resize.spec.ts — E2E tests for resizable split, columns, and tooltips.
 * Called by: npx playwright test e2e/requisitions2-resize.spec.ts
 * Depends on: running app server with test auth bypass (TESTING=1)
 */
import { test, expect, Page } from '@playwright/test';

const REQS_URL = '/requisitions2';

async function clearLayout(page: Page) {
  await page.goto(REQS_URL);
  await page.evaluate(() => {
    localStorage.removeItem('avail_split_rq2');
    localStorage.removeItem('avail_table_cols_rq2-list');
    localStorage.removeItem('avail_table_cols_rq2-parts');
  });
}

test.describe('Requisitions split divider', () => {
  test.beforeEach(async ({ page }) => {
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

test.describe('Requisitions left-list columns', () => {
  test.beforeEach(async ({ page }) => {
    await clearLayout(page);
    await page.reload();
  });

  test('Name column resize persists to localStorage and survives reload', async ({ page }) => {
    await page.goto(REQS_URL);

    const nameHeader = page.locator('#rq2-table th').filter({ hasText: 'Name' });
    await expect(nameHeader).toBeVisible({ timeout: 10000 });

    const handle = nameHeader.locator('.col-resize-handle');
    await expect(handle).toBeVisible();

    const hBox = await handle.boundingBox();
    if (!hBox) throw new Error('handle not visible');

    // Drag 80px right from the handle center
    await page.mouse.move(hBox.x + hBox.width / 2, hBox.y + hBox.height / 2);
    await page.mouse.down();
    await page.mouse.move(hBox.x + hBox.width / 2 + 80, hBox.y + hBox.height / 2, { steps: 10 });
    await page.mouse.up();

    const saved = await page.evaluate(() =>
      JSON.parse(localStorage.getItem('avail_table_cols_rq2-list') || '{}')
    );
    expect(saved.name).toBeGreaterThan(200);

    const savedName = saved.name;
    await page.reload();
    const col = page.locator('#rq2-table colgroup col').nth(1); // 0=select, 1=name
    const style = await col.getAttribute('style');
    expect(style).toContain(`width:${savedName}px`);
  });

  test('columns survive HTMX swap (sort reorder)', async ({ page }) => {
    await page.goto(REQS_URL);
    await page.evaluate(() => {
      localStorage.setItem(
        'avail_table_cols_rq2-list',
        JSON.stringify({ name: 260, status: 110, customer: 160, select: 36, count: 60 })
      );
    });
    await page.reload();

    const nameLink = page.locator('#rq2-table thead a', { hasText: 'Name' });
    if ((await nameLink.count()) > 0) {
      await nameLink.first().click();
      await page.waitForTimeout(400);
      const col = page.locator('#rq2-table colgroup col').nth(1);
      const style = await col.getAttribute('style');
      expect(style).toContain('width:260px');
    }
  });
});
