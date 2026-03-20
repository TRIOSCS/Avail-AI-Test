/**
 * dead-ends.spec.ts — Dead-End Detector for AvailAI.
 *
 * Hits every major HTMX partial endpoint and verifies:
 * 1. Returns 200 (not 500, not empty)
 * 2. Response contains actual HTML content (not blank)
 * 3. No bare error text without styling
 *
 * Called by: npx playwright test --project=dead-ends
 * Depends on: running app server in TESTING=1 mode
 */

import { test, expect } from '@playwright/test';

// All list partials that should render without any path params
const LIST_PARTIALS = [
  '/v2/partials/requisitions',
  '/v2/partials/vendors',
  '/v2/partials/companies',
  '/v2/partials/quotes',
  '/v2/partials/buy-plans',
  '/v2/partials/materials',
  '/v2/partials/materials/workspace',
  '/v2/partials/prospecting',
  '/v2/partials/proactive',
  '/v2/partials/strategic',
  '/v2/partials/follow-ups',
  '/v2/partials/excess',
  '/v2/partials/settings',
  '/v2/partials/dashboard',
  '/v2/partials/search',
  '/v2/partials/offers/review-queue',
];

// Full pages that should render the app shell
const FULL_PAGES = [
  '/v2',
  '/v2/requisitions',
  '/v2/vendors',
  '/v2/companies',
  '/v2/quotes',
  '/v2/buy-plans',
  '/v2/materials',
  '/v2/search',
  '/v2/prospecting',
  '/v2/settings',
];

test.describe('Dead-End Detector — List Partials', () => {
  for (const url of LIST_PARTIALS) {
    test(`${url} returns non-empty HTML or auth redirect`, async ({ request }) => {
      const res = await request.get(url, {
        headers: { 'HX-Request': 'true' },
      });

      // Should not be a server error — 200, 401, 307 are all acceptable
      // (401/307 = auth required, which is correct behavior for unauthenticated requests)
      expect(res.status(), `${url} crashed with ${res.status()}`).toBeLessThan(500);

      // If we got a successful response, verify it has content
      if (res.status() === 200) {
        const html = await res.text();
        expect(html.trim().length, `${url} returned empty response`).toBeGreaterThan(10);
        expect(html).not.toMatch(/^(Internal Server Error|Not Found)$/);
      }
    });
  }
});

test.describe('Dead-End Detector — Full Pages', () => {
  for (const url of FULL_PAGES) {
    test(`${url} loads without server error`, async ({ request }) => {
      const res = await request.get(url, {
        headers: { 'Accept': 'text/html' },
      });

      // Should not crash — 200 or auth redirect (401/307) are acceptable
      expect(res.status(), `${url} crashed with ${res.status()}`).toBeLessThan(500);

      if (res.status() === 200) {
        const html = await res.text();
        // Should contain either the app shell or the login page — both are valid
        expect(html.trim().length, `${url} returned empty page`).toBeGreaterThan(100);
      }
    });
  }
});

test.describe('Dead-End Detector — Form Endpoints Accept POST', () => {
  // These POST endpoints should return non-500 even with minimal/empty data
  // (they should return validation errors or auth errors, not crashes)
  const POST_ENDPOINTS = [
    '/v2/partials/requisitions/create',
    '/v2/partials/companies/create',
  ];

  for (const url of POST_ENDPOINTS) {
    test(`POST ${url} doesn't crash on empty submission`, async ({ request }) => {
      const res = await request.post(url, {
        headers: { 'HX-Request': 'true', 'Content-Type': 'application/x-www-form-urlencoded' },
        data: '',
      });

      // Should return validation error (4xx), auth redirect (401/307), or success (2xx) — NOT a crash (5xx)
      expect(res.status(), `POST ${url} crashed with ${res.status()}`).toBeLessThan(500);
    });
  }
});

test.describe('Dead-End Detector — 404 Handling', () => {
  test('non-existent requisition returns error, not crash', async ({ request }) => {
    const res = await request.get('/v2/partials/requisitions/999999', {
      headers: { 'HX-Request': 'true' },
    });
    expect(res.status()).toBeLessThan(500);
  });

  test('non-existent vendor returns error, not crash', async ({ request }) => {
    const res = await request.get('/v2/partials/vendors/999999', {
      headers: { 'HX-Request': 'true' },
    });
    expect(res.status()).toBeLessThan(500);
  });

  test('non-existent company returns error, not crash', async ({ request }) => {
    const res = await request.get('/v2/partials/companies/999999', {
      headers: { 'HX-Request': 'true' },
    });
    expect(res.status()).toBeLessThan(500);
  });

  test('non-existent quote returns error, not crash', async ({ request }) => {
    const res = await request.get('/v2/partials/quotes/999999', {
      headers: { 'HX-Request': 'true' },
    });
    expect(res.status()).toBeLessThan(500);
  });
});
