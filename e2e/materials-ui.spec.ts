/**
 * materials-ui.spec.ts — UI cleanup verification for materials page.
 *
 * Verifies the accent migration (brand-* → accent-*) and page-header chrome
 * in the rendered HTML. Runs AUTHED (storageState from e2e/auth.setup.ts,
 * seeded admin on an EMPTY DB), so content assertions are live — the old
 * if-200 guards (from the era when no spec logged in) are gone. The commodity
 * tree accent test needs seeded material_cards and is a round-two skip.
 *
 * NO test here may CREATE rows: the suite shares one serial in-memory DB and
 * other projects assert empty states.
 *
 * Called by: npx playwright test --project=materials-ui
 * Depends on: running app server in TESTING=1 mode (scripts/e2e_server.py)
 */

import { test, expect } from '@playwright/test';

test.describe('materials workspace UI', () => {
  test('workspace partial renders authed with page chrome', async ({ request }) => {
    const res = await request.get('/v2/partials/materials/workspace', {
      headers: { 'HX-Request': 'true' },
    });
    expect(res.status()).toBe(200);
    const html = await res.text();
    // Page header "Materials" title
    expect(html).toContain('Materials');
    // Workspace root marker (replaces the stale 'Needs review' chip assert —
    // that literal no longer exists anywhere under app/).
    expect(html).toContain('id="materials-workspace"');
    // Accent classes present (accent migration)
    expect(html).toContain('accent-');
    // No hand-rolled Add-part bg-brand-500 button remains
    expect(html).not.toContain('bg-brand-500 text-white text-sm font-medium rounded-lg hover:bg-brand-600');
  });

  test('materials workspace partial uses .chip for removable filter pills', async ({ request }) => {
    const res = await request.get('/v2/partials/materials/workspace', {
      headers: { 'HX-Request': 'true' },
    });
    expect(res.status()).toBe(200);
    const html = await res.text();
    // .chip class replaces hand-rolled inline-flex... rounded-full blocks
    // (removable_chip is emitted unconditionally in the template source).
    expect(html).toContain('class="chip');
  });

  test('materials faceted list renders authed', async ({ request }) => {
    const res = await request.get('/v2/partials/materials/faceted', {
      headers: { 'HX-Request': 'true' },
    });
    expect(res.status()).toBe(200);
    const html = await res.text();
    expect(html.trim().length, 'faceted list returned empty response').toBeGreaterThan(10);
  });

  test('commodity tree partial uses accent active state', { tag: '@needs-data' }, async ({ request }) => {
    test.skip(true, 'round-2 (owner-gated): needs seeded content data');
    // The tree renders branches only when count > 0 (tree.html count-gate);
    // with zero material_cards the asserted accent classes cannot appear.
    // round-2: with seeded material_cards, assert accent-100/accent-500 and
    // the absence of the old bg-brand-100 active state.
    const res = await request.get('/v2/partials/materials/filters/tree', {
      headers: { 'HX-Request': 'true' },
    });
    expect(res.status()).toBe(200);
    const html = await res.text();
    expect(html).toContain('accent-100');
    expect(html).toContain('accent-500');
    expect(html).not.toContain('bg-brand-100 text-brand-800');
  });
});
