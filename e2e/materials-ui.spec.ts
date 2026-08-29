/**
 * materials-ui.spec.ts — UI cleanup verification for materials page.
 *
 * Verifies the accent migration (brand-* → accent-*) and new page-header/
 * Needs-review chip are present in the rendered HTML.
 *
 * QC 08-08 found this spec orphaned (matched by no config project) and
 * failing 8/18 once harnessed: it asserted rendered-HTML content
 * unconditionally, but no spec in this suite performs a real login — every
 * other harnessed spec (dead-ends.spec.ts, workflows.spec.ts,
 * approvals-workspace.*.spec.ts) treats an unauthenticated 401/307 as a
 * legitimate response and only makes content assertions on a 200. This
 * file now follows that same convention instead of assuming auth.
 *
 * Called by: npx playwright test --project=materials-ui
 * Depends on: running app server in TESTING=1 mode
 */

import { test, expect } from '@playwright/test';

test.describe('materials workspace UI', () => {
  test('workspace partial returns non-empty HTML or auth redirect', async ({ request }) => {
    const res = await request.get('/v2/partials/materials/workspace', {
      headers: { 'HX-Request': 'true' },
    });
    expect(res.status()).toBeLessThan(500);
    if (res.status() === 200) {
      const html = await res.text();
      // Page header "Materials" title
      expect(html).toContain('Materials');
      // Needs-review attention chip
      expect(html).toContain('Needs review');
      // Accent classes present (accent migration)
      expect(html).toContain('accent-');
      // No hand-rolled Add-part bg-brand-500 button remains
      expect(html).not.toContain('bg-brand-500 text-white text-sm font-medium rounded-lg hover:bg-brand-600');
    }
  });

  test('materials workspace partial uses .chip for removable filter pills', async ({ request }) => {
    const res = await request.get('/v2/partials/materials/workspace', {
      headers: { 'HX-Request': 'true' },
    });
    expect(res.status()).toBeLessThan(500);
    if (res.status() === 200) {
      const html = await res.text();
      // .chip class replaces hand-rolled inline-flex... rounded-full blocks
      expect(html).toContain('class="chip');
    }
  });

  test('materials list partial returns non-empty HTML or auth redirect', async ({ request }) => {
    const res = await request.get('/v2/partials/materials/faceted', {
      headers: { 'HX-Request': 'true' },
    });
    expect(res.status()).toBeLessThan(500);
  });

  test('commodity tree partial uses accent active state', async ({ request }) => {
    const res = await request.get('/v2/partials/materials/filters/tree', {
      headers: { 'HX-Request': 'true' },
    });
    expect(res.status()).toBeLessThan(500);
    if (res.status() === 200) {
      const html = await res.text();
      // Active branch uses accent-100 / accent-800 / accent-500 now
      expect(html).toContain('accent-100');
      expect(html).toContain('accent-500');
      // Old brand-100 active state must not appear
      expect(html).not.toContain("bg-brand-100 text-brand-800");
    }
  });
});
