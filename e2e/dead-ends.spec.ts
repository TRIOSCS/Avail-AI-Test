/**
 * dead-ends.spec.ts — Dead-End Detector for AvailAI.
 *
 * Runs AUTHED (storageState from e2e/auth.setup.ts, seeded admin on an EMPTY
 * DB). Hits every major HTMX partial endpoint and verifies:
 * 1. Live routes return a hard 200 with actual HTML content (not blank)
 * 2. Retired routes and missing-parent lookups return a clean 404
 * 3. Invalid POSTs return their observed validation/contract status, never 5xx
 *
 * NO test here may CREATE rows: the suite shares one serial in-memory DB and
 * other projects assert empty states. All POSTs are invalid-payload probes.
 * Statuses below were pinned from an observed authed run against the seeded
 * launcher (scripts/e2e_server.py), never guessed.
 *
 * Called by: npx playwright test --project=dead-ends
 * Depends on: running app server in TESTING=1 mode (scripts/e2e_server.py)
 */

import { test, expect } from '@playwright/test';

// Live list partials — every one must render for the seeded admin. The
// request fixture follows redirects, so the two renamed/relocated partials
// (/v2/partials/companies → /v2/partials/customers, /v2/partials/buy-plans →
// /v2/partials/approvals?tab=buy-plans) land on their live 200 targets.
const LIST_PARTIALS = [
  '/v2/partials/requisitions',
  '/v2/partials/vendors',
  '/v2/partials/companies',
  '/v2/partials/buy-plans',
  '/v2/partials/materials',
  '/v2/partials/materials/workspace',
  '/v2/partials/prospecting',
  '/v2/partials/proactive',
  '/v2/partials/follow-ups',
  '/v2/partials/settings',
  '/v2/partials/settings/connectors',
  '/v2/partials/dashboard',
  '/v2/partials/search',
  '/v2/partials/offers/review-queue',
];

// Retired partial routes — authed they 404 (observed). Asserting 404 documents
// the retirement honestly instead of repointing the detector; the /v2/quotes
// FULL page below stays live via its redirect.
const RETIRED_PARTIALS = [
  '/v2/partials/quotes',
  '/v2/partials/strategic',
  '/v2/partials/excess',
];

// Full pages that must render the authed app shell (redirects followed).
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
    test(`${url} renders authed with real content`, async ({ request }) => {
      const res = await request.get(url, {
        headers: { 'HX-Request': 'true' },
      });

      // Authed, a live partial has exactly one honest status.
      expect(res.status(), `${url} must render for the seeded admin`).toBe(200);

      const html = await res.text();
      expect(html.trim().length, `${url} returned empty response`).toBeGreaterThan(10);
      expect(html).not.toMatch(/^(Internal Server Error|Not Found)$/);
    });
  }

  for (const url of RETIRED_PARTIALS) {
    test(`${url} is a retired route (404)`, async ({ request }) => {
      const res = await request.get(url, {
        headers: { 'HX-Request': 'true' },
      });
      // retired route — documents the retirement (observed authed 404); if
      // this route comes back to life, move it up into LIST_PARTIALS.
      expect(res.status(), `${url} is expected to be retired`).toBe(404);
    });
  }
});

test.describe('Dead-End Detector — Full Pages', () => {
  for (const url of FULL_PAGES) {
    test(`${url} renders the authed app shell`, async ({ request }) => {
      const res = await request.get(url, {
        headers: { 'Accept': 'text/html' },
      });

      expect(res.status(), `${url} must render for the seeded admin`).toBe(200);

      const html = await res.text();
      expect(html.trim().length, `${url} returned empty page`).toBeGreaterThan(100);
    });
  }
});

test.describe('Dead-End Detector — Form Endpoints Accept POST', () => {
  // Invalid-payload probes ONLY (empty body) — must return the pinned
  // validation/contract status, never a crash and never a created row.
  const POST_ENDPOINTS: Array<{ url: string; expected: number; why: string }> = [
    // Empty form → FastAPI form validation error.
    { url: '/v2/partials/requisitions/create', expected: 422, why: 'form validation' },
    // Observed authed 405: the companies create partial no longer accepts
    // POST (flagged for owner visibility in the conversion PR).
    { url: '/v2/partials/companies/create', expected: 405, why: 'route no longer accepts POST' },
  ];

  for (const { url, expected, why } of POST_ENDPOINTS) {
    test(`POST ${url} returns ${expected} on empty submission`, async ({ request }) => {
      const res = await request.post(url, {
        headers: { 'HX-Request': 'true', 'Content-Type': 'application/x-www-form-urlencoded' },
        data: '',
      });
      expect(res.status(), `POST ${url} expected ${expected} (${why})`).toBe(expected);
    });
  }
});

test.describe('Dead-End Detector — Unified Attachments (Task 5)', () => {
  // Authed + empty DB, every parent id 1 is missing, so the routes 404 by
  // contract (attachments_extra.py, requisitions/attachments.py,
  // crm/offers.py). round-2 (owner-gated): positive attachment coverage —
  // 200 + panel HTML — needs a seeded parent row.
  const ATTACHMENT_LIST_ENDPOINTS = [
    '/api/requisitions/1/attachments',
    '/api/requirements/1/attachments',
    '/api/offers/1/attachments',
    '/api/companies/1/attachments',
    '/api/contacts/1/attachments',
    '/api/material-cards/1/attachments',
  ];

  for (const url of ATTACHMENT_LIST_ENDPOINTS) {
    test(`${url} 404s on the missing parent`, { tag: '@needs-data' }, async ({ request }) => {
      const res = await request.get(url, { headers: { 'HX-Request': 'true' } });
      // round-2: becomes toBe(200) + content assert once a parent is seeded.
      expect(res.status(), `${url} must 404 on a missing parent`).toBe(404);
    });
  }

  // The detail surfaces hosting the panel — same missing-parent contract.
  const ATTACHMENT_SURFACES = [
    '/v2/partials/customers/1/tab/files',
    '/v2/partials/materials/1/tab/files',
    '/v2/partials/contacts/1/files-modal',
  ];

  for (const url of ATTACHMENT_SURFACES) {
    test(`${url} 404s on the missing parent`, { tag: '@needs-data' }, async ({ request }) => {
      const res = await request.get(url, { headers: { 'HX-Request': 'true' } });
      // round-2: becomes toBe(200) + panel-surface assert with seeded data.
      expect(res.status(), `${url} must 404 on a missing parent`).toBe(404);
    });
  }
});

test.describe('Dead-End Detector — 404 Handling', () => {
  test('non-existent requisition returns 404, not crash', async ({ request }) => {
    const res = await request.get('/v2/partials/requisitions/999999', {
      headers: { 'HX-Request': 'true' },
    });
    expect(res.status()).toBe(404);
  });

  test('non-existent vendor returns 404, not crash', async ({ request }) => {
    const res = await request.get('/v2/partials/vendors/999999', {
      headers: { 'HX-Request': 'true' },
    });
    expect(res.status()).toBe(404);
  });

  test('non-existent company returns 404, not crash', async ({ request }) => {
    // 301-redirects to /v2/partials/customers/999999 (followed) → 404 there.
    const res = await request.get('/v2/partials/companies/999999', {
      headers: { 'HX-Request': 'true' },
    });
    expect(res.status()).toBe(404);
  });

  test('non-existent quote returns 404, not crash', async ({ request }) => {
    const res = await request.get('/v2/partials/quotes/999999', {
      headers: { 'HX-Request': 'true' },
    });
    expect(res.status()).toBe(404);
  });
});
