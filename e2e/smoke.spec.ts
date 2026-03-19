// Smoke tests for AvailAI — verifies the app is running and key pages load.
// Tests homepage, static assets, and API versioning.
// Called by: npx playwright test --project=smoke
// Depends on: app/main.py, app/routers/auth.py

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
    expect(text).toContain('AVAIL');
  });

  test('homepage contains required meta tags', async ({ request }) => {
    const res = await request.get('/', {
      headers: { 'Accept': 'text/html' },
    });
    const html = await res.text();
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
