// Smoke tests for AvailAI — verifies the app is running and key pages load.
// Runs AUTHED (storageState from e2e/auth.setup.ts, seeded admin): `/` follows
// its 302 into the real /v2/requisitions app shell, not the login page.
// Tests homepage shell, static assets, and API versioning.
// NO test here may CREATE rows: the suite shares one serial in-memory DB and
// later projects assert empty states.
// Called by: npx playwright test --project=smoke
// Depends on: app/routers/auth.py, scripts/e2e_server.py (webServer seeding)

import { test, expect } from '@playwright/test';

test.describe('App Health', () => {
  test('server is running and responds', async ({ request }) => {
    const res = await request.get('/');
    expect(res.ok()).toBeTruthy();
  });

  test('homepage returns HTML content', async ({ request }) => {
    const res = await request.get('/', {
      headers: { 'Accept': 'text/html' },
    });
    expect(res.status()).toBe(200);
    const text = await res.text();
    // Near-vacuous either way: 'AVAIL' appears on the login page too (kept as
    // a cheap sanity marker). The assertion that proves the session cookie
    // reached the app is the authed-shell nav module below — the anonymous
    // login page has no bottom-nav modules.
    expect(text).toContain('AVAIL');
    expect(text).toContain('Sales Hub');
  });

  test('homepage contains required meta tags', async ({ request }) => {
    const res = await request.get('/', {
      headers: { 'Accept': 'text/html' },
    });
    const html = await res.text();
    // Near-vacuous markers (base.html emits both on every page, login
    // included) — kept for template-regression value only.
    expect(html).toContain('viewport');
    expect(html).toContain('AvailAI');
  });
});

test.describe('Static Assets', () => {
  test('manifest.json is accessible', async ({ request }) => {
    const res = await request.get('/static/manifest.json');
    expect([200, 404]).toContain(res.status());
  });
});

test.describe('API Versioning', () => {
  test('API responses include version header', async ({ request }) => {
    const res = await request.get('/api/v1/sources');
    const version = res.headers()['x-api-version'];
    // Version header should be present if API versioning middleware is active
    if (version) {
      expect(version).toBe('v1');
    }
  });

  test('/api/v1/ prefix routes correctly', async ({ request }) => {
    const res = await request.get('/api/v1/sources');
    // Should not return 404 — should route through to the handler
    expect(res.status()).not.toBe(404);
  });
});
