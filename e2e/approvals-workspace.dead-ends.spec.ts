/**
 * approvals-workspace.dead-ends.spec.ts — Dead-End Detector for the Approvals
 * Workspace (4-tab split-view console).
 *
 * Hits every workspace endpoint a button/link/form in the approvals partials can
 * reach — shell, tab bodies, lists (live/closed/filtered), panes, sent-check,
 * exports, notes/attachments/method POSTs — and verifies none is a dead end:
 * 1. Never a 5xx (200, or 401/307 auth redirect, or a clean 4xx validation error)
 * 2. A 200 always carries real HTML content (not blank, not bare error text)
 *
 * Called by: npx playwright test --project=dead-ends
 * Depends on: running app server in TESTING=1 mode; app/routers/htmx/approvals_hub.py
 *             and the approvals partials under app/templates/htmx/partials/approvals/
 */

import { test, expect } from '@playwright/test';

const TABS = ['sales-orders', 'buy-plans', 'purchase-orders', 'prepayments'];

// Every GET surface the workspace UI links to (tab pills, lazy list, filter bar,
// Mine/All + Live/Closed toggles, the empty-search state, sent-check).
const WORKSPACE_GET_PARTIALS = [
  '/v2/partials/approvals',
  ...TABS.map((t) => `/v2/partials/approvals?tab=${t}`),
  ...TABS.map((t) => `/v2/partials/approvals/${t}`),
  ...TABS.map((t) => `/v2/partials/approvals/${t}/list`),
  ...TABS.map((t) => `/v2/partials/approvals/${t}/list?scope=mine`),
  ...TABS.map((t) => `/v2/partials/approvals/${t}/list?show_closed=true`),
  // Empty states must render guidance, never a blank panel: the no-match search.
  ...TABS.map((t) => `/v2/partials/approvals/${t}/list?q=ZZZ-NO-SUCH-THING`),
];

test.describe('Dead-End Detector — Approvals Workspace partials', () => {
  for (const url of WORKSPACE_GET_PARTIALS) {
    test(`${url} returns non-empty HTML or auth redirect`, async ({ request }) => {
      const res = await request.get(url, { headers: { 'HX-Request': 'true' } });
      expect(res.status(), `${url} crashed with ${res.status()}`).toBeLessThan(500);
      if (res.status() === 200) {
        const html = await res.text();
        expect(html.trim().length, `${url} returned empty response`).toBeGreaterThan(10);
        expect(html).not.toMatch(/^(Internal Server Error|Not Found)$/);
      }
    });
  }

  test('/v2/approvals full page loads without server error', async ({ request }) => {
    const res = await request.get('/v2/approvals', { headers: { Accept: 'text/html' } });
    expect(res.status(), '/v2/approvals crashed').toBeLessThan(500);
    if (res.status() === 200) {
      const html = await res.text();
      expect(html.trim().length, '/v2/approvals returned empty page').toBeGreaterThan(100);
    }
  });
});

test.describe('Dead-End Detector — Approvals Workspace 404 handling', () => {
  // Every pane URL a list row / kanban card can dispatch, with an id that
  // cannot exist — must be a clean 404 (or auth redirect), never a crash.
  const MISSING_ID_URLS = [
    '/v2/partials/approvals/plan/999999/pane',
    '/v2/partials/approvals/po/999999/pane',
    '/v2/partials/approvals/prepayments/999999/pane',
    '/v2/partials/approvals/po/999999/sent-check',
  ];

  for (const url of MISSING_ID_URLS) {
    test(`${url} returns error, not crash`, async ({ request }) => {
      const res = await request.get(url, { headers: { 'HX-Request': 'true' } });
      expect(res.status(), `${url} crashed with ${res.status()}`).toBeLessThan(500);
    });
  }

  test('unknown tab body and list return error, not crash', async ({ request }) => {
    for (const url of ['/v2/partials/approvals/nope', '/v2/partials/approvals/nope/list', '/v2/partials/approvals/nope/export']) {
      const res = await request.get(url, { headers: { 'HX-Request': 'true' } });
      expect(res.status(), `${url} crashed with ${res.status()}`).toBeLessThan(500);
      expect(res.status(), `${url} should not render an unknown tab`).not.toBe(200);
    }
  });
});

test.describe('Dead-End Detector — Approvals Workspace form endpoints accept POST', () => {
  // The pane/thread forms' POST targets should return validation or auth errors on
  // minimal/empty data — not crashes.
  const POST_ENDPOINTS = [
    '/v2/partials/approvals/notes',
    '/v2/partials/approvals/attachments',
    '/v2/partials/approvals/prepayments/1/method',
    '/v2/partials/approvals/plan/1/qp-sales',
  ];

  for (const url of POST_ENDPOINTS) {
    test(`POST ${url} doesn't crash on empty submission`, async ({ request }) => {
      const res = await request.post(url, {
        headers: { 'HX-Request': 'true', 'Content-Type': 'application/x-www-form-urlencoded' },
        data: '',
      });
      expect(res.status(), `POST ${url} crashed with ${res.status()}`).toBeLessThan(500);
    });
  }

  test('DELETE attachment doesn\'t crash on a missing id', async ({ request }) => {
    const res = await request.delete('/v2/partials/approvals/attachments/999999', {
      headers: { 'HX-Request': 'true' },
    });
    expect(res.status(), `attachment delete crashed with ${res.status()}`).toBeLessThan(500);
  });

  test('CSV export endpoints respond for every tab', async ({ request }) => {
    for (const tab of TABS) {
      const res = await request.get(`/v2/partials/approvals/${tab}/export`, {
        headers: { 'HX-Request': 'true' },
      });
      expect(res.status(), `${tab} export crashed with ${res.status()}`).toBeLessThan(500);
    }
  });
});
