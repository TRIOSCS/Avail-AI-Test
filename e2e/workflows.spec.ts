/**
 * workflows.spec.ts — Multi-step workflow tests for AvailAI.
 *
 * Runs AUTHED (storageState from e2e/auth.setup.ts, seeded admin on an EMPTY
 * DB), except the 'Legacy settings routes' describe, which pins an empty
 * storageState because its 401-before-redirect assertions are only meaningful
 * anonymous (authed, those routes 302 to /connectors).
 *
 * NO test here may CREATE rows: the suite shares one serial in-memory DB and
 * other projects assert empty states. Statuses pinned from an observed authed
 * run against the seeded launcher, never guessed.
 *
 * Called by: npx playwright test --project=workflows
 * Depends on: running app server in TESTING=1 mode (scripts/e2e_server.py)
 */

import { test, expect } from '@playwright/test';

test.describe('Navigation Workflows', () => {
  test('sidebar navigation loads correct partials', async ({ request }) => {
    for (const url of ['/v2/partials/requisitions', '/v2/partials/vendors', '/v2/partials/companies']) {
      const res = await request.get(url, {
        headers: { 'HX-Request': 'true' },
      });
      expect(res.status(), `${url} must render for the seeded admin`).toBe(200);
      const html = await res.text();
      expect(html.length, `${url} empty`).toBeGreaterThan(50);
    }
  });

  test('materials workspace loads with filters', async ({ request }) => {
    const res = await request.get('/v2/partials/materials/workspace', {
      headers: { 'HX-Request': 'true' },
    });
    expect(res.status()).toBe(200);
    const html = await res.text();
    expect(html).toContain('materialsFilter');
  });

  test('materials faceted search with commodity filter', async ({ request }) => {
    const res = await request.get('/v2/partials/materials/faceted?commodity=capacitors', {
      headers: { 'HX-Request': 'true' },
    });
    expect(res.status()).toBe(200);
  });

  test('search form renders and accepts queries', async ({ request }) => {
    // Load search form
    let res = await request.get('/v2/partials/search', {
      headers: { 'HX-Request': 'true' },
    });
    expect(res.status()).toBe(200);

    // Submit search
    res = await request.get('/v2/partials/search/global?q=LM317T', {
      headers: { 'HX-Request': 'true' },
    });
    expect(res.status()).toBe(200);
  });
});

test.describe('Form Submission Workflows', () => {
  test('create requisition form renders', async ({ request }) => {
    const res = await request.get('/v2/partials/requisitions/create-form', {
      headers: { 'HX-Request': 'true' },
    });
    expect(res.status()).toBe(200);
    const html = await res.text();
    expect(html).toContain('name');
  });

  test('create company form renders', async ({ request }) => {
    const res = await request.get('/v2/partials/companies/create-form', {
      headers: { 'HX-Request': 'true' },
    });
    expect(res.status()).toBe(200);
  });
});

test.describe('Settings & Admin', () => {
  test('settings page loads all sections', async ({ request }) => {
    // 'sources' and 'api-keys' redirect to /connectors (covered below,
    // anonymous); the three real sections must render for the seeded admin.
    const sections = ['system', 'profile', 'data-ops'];
    for (const section of sections) {
      const res = await request.get(`/v2/partials/settings/${section}`, {
        headers: { 'HX-Request': 'true' },
      });
      expect(res.status(), `Settings ${section} must render for the seeded admin`).toBe(200);
    }
  });

  test('/connectors renders authed with the true empty state', { tag: '@needs-data' }, async ({ request }) => {
    const res = await request.get('/v2/partials/settings/connectors', {
      headers: { 'HX-Request': 'true' },
    });
    expect(res.status(), '/connectors must render for the seeded admin').toBe(200);
    const html = await res.text();
    expect(html.trim().length, '/connectors returned empty response').toBeGreaterThan(10);
    // Empty DB truth: _build_connector_groups drops empty groups and
    // seed_api_sources() is TESTING-gated, so zero groups render — the page
    // shows its empty state instead.
    expect(html, '/connectors must show the no-connectors empty state').toContain('No connectors yet.');
    // round-2 (owner-gated): with ApiSource rows seeded, assert the group
    // headings instead — distinctive ones only ('Browser Workers',
    // 'Part Sourcing', 'Enrichment', 'Communications', 'Manual'); bare 'AI'
    // is an unfalsifiable substring, never assert it.
  });

  test('API health check renders for the admin', async ({ request }) => {
    const res = await request.get('/v2/partials/admin/api-health', {
      headers: { 'HX-Request': 'true' },
    });
    expect(res.status()).toBe(200);
  });
});

// These two assertions are only meaningful WITHOUT a session: auth runs BEFORE
// the legacy-route redirect (2026-07 security hardening), so anonymous → 401.
// Authed they 302 to /connectors (observed) — the authenticated mapping is
// covered by tests/test_connectors_settings.py::test_old_routes_redirect.
test.describe('Legacy settings routes (anonymous by design)', () => {
  test.use({ storageState: { cookies: [], origins: [] } });

  test('/sources requires auth before redirecting', async ({ request }) => {
    const res = await request.get('/v2/partials/settings/sources', {
      maxRedirects: 0,
      headers: { 'HX-Request': 'true' },
    });
    expect(res.status()).toBe(401);
  });

  test('/api-keys requires auth before redirecting', async ({ request }) => {
    const res = await request.get('/v2/partials/settings/api-keys', {
      maxRedirects: 0,
      headers: { 'HX-Request': 'true' },
    });
    expect(res.status()).toBe(401);
  });
});

test.describe('Dashboard', () => {
  test('dashboard loads', async ({ request }) => {
    const res = await request.get('/v2/partials/dashboard', {
      headers: { 'HX-Request': 'true' },
    });
    expect(res.status()).toBe(200);
  });
});
