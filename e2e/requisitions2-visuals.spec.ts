/**
 * requisitions2-visuals.spec.ts — E2E visual regressions for the
 * /requisitions2 merged v2 opportunity table.
 * Called by: npx playwright test e2e/requisitions2-visuals.spec.ts
 * Depends on: running app server with seeded requisitions (TESTING=1)
 */
import { test, expect, Page } from '@playwright/test';

const REQS_URL = '/requisitions2';

async function gotoFresh(page: Page) {
  await page.goto(REQS_URL);
  await page.waitForSelector('.rq2-row', { timeout: 8000 });
}

test.describe('Status cell', () => {
  test('each bucket renders its dot color class', async ({ page }) => {
    await gotoFresh(page);
    const dots = await page.locator('.opp-status-dot').all();
    expect(dots.length).toBeGreaterThan(0);
    for (const dot of dots) {
      const cls = await dot.getAttribute('class') || '';
      expect(cls).toMatch(/opp-status-dot--(open|sourcing|offered|quoted|neutral)/);
    }
  });

  test('time text uses correct class when hours_until_bid_due is set', async ({ page }) => {
    await gotoFresh(page);
    const times = page.locator('.opp-time');
    const count = await times.count();
    if (count > 0) {
      const cls = await times.first().getAttribute('class') || '';
      expect(cls).toMatch(/opp-time--(24h|72h|normal)/);
    }
  });
});

test.describe('Urgency accent on <tr>', () => {
  test('rows with class opp-row--urgent-24h are <tr> elements', async ({ page }) => {
    await gotoFresh(page);
    const urgent = page.locator('tr.opp-row--urgent-24h');
    const count = await urgent.count();
    if (count > 0) {
      const tag = await urgent.first().evaluate((el) => el.tagName);
      expect(tag).toBe('TR');
    }
  });
});

test.describe('Deal value', () => {
  test('tier class matches magnitude', async ({ page }) => {
    await gotoFresh(page);
    const deals = await page.locator('.opp-deal').all();
    for (const d of deals) {
      const cls = await d.getAttribute('class') || '';
      const raw = (await d.textContent() || '').trim();
      const digits = raw.replace(/[^0-9]/g, '');
      const n = parseInt(digits || '0', 10);
      if (!digits || raw === '—') {
        expect(cls).toContain('opp-deal--tier-tertiary');
        continue;
      }
      if (n >= 100000) expect(cls).toContain('opp-deal--tier-primary-500');
      else if (n >= 1000) expect(cls).toContain('opp-deal--tier-primary-400');
      else expect(cls).toContain('opp-deal--tier-tertiary');
    }
  });

  test('partial source renders ~ prefix, italic hook, and tooltip copy', async ({ page }) => {
    await gotoFresh(page);
    const partial = page.locator('.opp-deal--partial');
    const count = await partial.count();
    if (count > 0) {
      const first = partial.first();
      const text = (await first.textContent() || '').trim();
      expect(text.startsWith('~$')).toBe(true);
      const cls = await first.getAttribute('class') || '';
      expect(cls).toContain('opp-deal--computed');
      const title = await first.getAttribute('title') || '';
      expect(title).toMatch(/\d+ of \d+ parts priced/);
    }
  });
});

test.describe('Coverage meter', () => {
  test('renders 6 segments with role=img and aria-label', async ({ page }) => {
    await gotoFresh(page);
    const meter = page.locator('.opp-coverage').first();
    await expect(meter).toBeVisible();
    await expect(meter).toHaveAttribute('role', 'img');
    const aria = await meter.getAttribute('aria-label') || '';
    expect(aria).toMatch(/Coverage: \d+ of \d+ parts sourced/);
    const segs = await meter.locator('.opp-coverage-seg').count();
    expect(segs).toBe(6);
  });
});

test.describe('Chip overflow', () => {
  test('each chip row has at least one visible chip', async ({ page }) => {
    await gotoFresh(page);
    const rows = await page.locator('.opp-chip-row').all();
    expect(rows.length).toBeGreaterThan(0);
    for (const row of rows) {
      const visible = await row.locator(':scope > *:not([style*="display: none"])').count();
      expect(visible).toBeGreaterThan(0);
    }
  });

  test('narrowing Name column does not increase visible chip count', async ({ page }) => {
    await gotoFresh(page);
    const handle = page.locator('th.resizable .col-resize-handle').first();
    if ((await handle.count()) === 0) test.skip();
    const box = await handle.boundingBox();
    if (!box) test.skip();

    const firstRow = page.locator('.opp-chip-row').first();
    const before = await firstRow.locator(':scope > *:not([style*="display: none"])').count();

    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await page.mouse.down();
    await page.mouse.move(box.x - 80, box.y + box.height / 2, { steps: 10 });
    await page.mouse.up();
    await page.waitForTimeout(120);

    const after = await firstRow.locator(':scope > *:not([style*="display: none"])').count();
    expect(after).toBeLessThanOrEqual(before);
  });

  test('hovering +N reveals tooltip containing hidden chips', async ({ page }) => {
    await gotoFresh(page);
    const more = page.locator('.opp-chip-more:visible').first();
    if ((await more.count()) === 0) test.skip();

    await more.hover();
    const tip = page.locator('.truncate-tip.visible');
    await expect(tip).toBeVisible({ timeout: 2000 });
    const tipChips = await tip.locator('.opp-chip-row > *').count();
    expect(tipChips).toBeGreaterThan(0);
  });
});

test.describe('Hover action rail', () => {
  test('rail hidden at pageload', async ({ page }) => {
    await gotoFresh(page);
    const rails = await page.locator('.opp-action-rail').all();
    for (const rail of rails) {
      const opacity = await rail.evaluate((el) => getComputedStyle(el).opacity);
      expect(parseFloat(opacity)).toBeLessThan(0.2);
    }
  });

  test('mouse-hover reveals rail; leave hides', async ({ page }) => {
    await gotoFresh(page);
    const row = page.locator('.rq2-row').first();
    await row.hover();
    await page.waitForTimeout(150);
    const rail = row.locator('.opp-action-rail');
    const visibleOpacity = await rail.evaluate((el) => getComputedStyle(el).opacity);
    expect(parseFloat(visibleOpacity)).toBeGreaterThan(0.5);
    await page.mouse.move(0, 0);
    await page.waitForTimeout(150);
    const hiddenOpacity = await rail.evaluate((el) => getComputedStyle(el).opacity);
    expect(parseFloat(hiddenOpacity)).toBeLessThan(0.2);
  });

  test('clicking a rail button does not trigger row hx-get detail', async ({ page }) => {
    await gotoFresh(page);
    const row = page.locator('.rq2-row').first();
    await row.hover();
    const clone = row.locator('.opp-action-rail [aria-label^="Clone"]');
    if ((await clone.count()) === 0) test.skip();
    await clone.click();
    await page.waitForTimeout(300);
    const after = await page.locator('#rq2-detail').innerHTML();
    // Heuristic: detail pane stays empty-or-unchanged; no row-detail marker inserted.
    expect(after).not.toMatch(/data-rq2-detail-id/);
  });
});
