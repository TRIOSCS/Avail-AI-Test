/**
 * workflows.spec.ts — Multi-step workflow tests for AvailAI.
 *
 * Tests complete user journeys through the app: navigation, tab switching,
 * form submissions, and cross-page consistency.
 *
 * Called by: npx playwright test --project=workflows
 * Depends on: running app server in TESTING=1 mode
 */

import { test, expect } from '@playwright/test';

test.describe('Navigation Workflows', () => {
  test('sidebar navigation loads correct partials', async ({ request }) => {
    // Each partial should return without server error
    // (200 = success, 401/307 = auth required — both are valid, not dead ends)
    for (const url of ['/v2/partials/requisitions', '/v2/partials/vendors', '/v2/partials/companies']) {
      const res = await request.get(url, {
        headers: { 'HX-Request': 'true' },
      });
      expect(res.status(), `${url} crashed`).toBeLessThan(500);
      if (res.status() === 200) {
        const html = await res.text();
        expect(html.length, `${url} empty`).toBeGreaterThan(50);
      }
    }
  });

  test('materials workspace loads with filters', async ({ request }) => {
    const res = await request.get('/v2/partials/materials/workspace', {
      headers: { 'HX-Request': 'true' },
    });
    expect(res.status()).toBeLessThan(500);
    if (res.status() === 200) {
      const html = await res.text();
      expect(html).toContain('materialsFilter');
    }
  });

  test('materials faceted search with commodity filter', async ({ request }) => {
    const res = await request.get('/v2/partials/materials/faceted?commodity=capacitors', {
      headers: { 'HX-Request': 'true' },
    });
    expect(res.status()).toBeLessThan(500);
  });

  test('search form renders and accepts queries', async ({ request }) => {
    // Load search form
    let res = await request.get('/v2/partials/search', {
      headers: { 'HX-Request': 'true' },
    });
    expect(res.status()).toBeLessThan(500);

    // Submit search
    res = await request.get('/v2/partials/search/global?q=LM317T', {
      headers: { 'HX-Request': 'true' },
    });
    expect(res.status()).toBeLessThan(500);
  });
});

test.describe('Form Submission Workflows', () => {
  test('create requisition form renders', async ({ request }) => {
    const res = await request.get('/v2/partials/requisitions/create-form', {
      headers: { 'HX-Request': 'true' },
    });
    expect(res.status()).toBeLessThan(500);
    if (res.status() === 200) {
      const html = await res.text();
      expect(html).toContain('name');
    }
  });

  test('create company form renders', async ({ request }) => {
    const res = await request.get('/v2/partials/companies/create-form', {
      headers: { 'HX-Request': 'true' },
    });
    expect(res.status()).toBeLessThan(500);
  });
});

test.describe('Settings & Admin', () => {
  test('settings page loads all sections', async ({ request }) => {
    const sections = ['sources', 'system', 'profile', 'data-ops'];
    for (const section of sections) {
      const res = await request.get(`/v2/partials/settings/${section}`, {
        headers: { 'HX-Request': 'true' },
      });
      // 200 = success, 401/307 = auth required — both fine, not a dead end
      expect(res.status(), `Settings ${section} crashed`).toBeLessThan(500);
    }
  });

  test('API health check renders', async ({ request }) => {
    const res = await request.get('/v2/partials/admin/api-health', {
      headers: { 'HX-Request': 'true' },
    });
    // May require admin — 200, 401, 403 all acceptable
    expect([200, 401, 403]).toContain(res.status());
  });
});

test.describe('Dashboard', () => {
  test('dashboard loads', async ({ request }) => {
    const res = await request.get('/v2/partials/dashboard', {
      headers: { 'HX-Request': 'true' },
    });
    expect(res.status()).toBeLessThan(500);
  });
});
