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
