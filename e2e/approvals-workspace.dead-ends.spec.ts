/**
 * approvals-workspace.dead-ends.spec.ts — Dead-End Detector for the Approvals
 * Workspace (4-tab split-view console).
 *
 * Runs AUTHED (storageState from e2e/auth.setup.ts, seeded admin on an EMPTY
 * DB). Hits every workspace endpoint a button/link/form in the approvals
 * partials can reach — shell, tab bodies, lists (live/closed/filtered), panes,
 * sent-check, exports, notes/attachments/method POSTs — and verifies none is a
 * dead end:
 * 1. Every GET surface renders a hard 200 with real HTML (the four tabs render
 *    unconditionally from the static tab list, empty DB included)
 * 2. Missing ids and unknown tabs return a clean 404
 * 3. Invalid POSTs return their observed validation/contract status, never 5xx
 *
 * NO test here may CREATE rows: the suite shares one serial in-memory DB and
 * other projects assert empty states. All POSTs are invalid-payload probes.
 * Statuses pinned from an observed authed run against the seeded launcher.
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
    test(`${url} renders authed with real content`, async ({ request }) => {
      const res = await request.get(url, { headers: { 'HX-Request': 'true' } });
      // The 4-tab chrome renders unconditionally on an empty DB
      // (approvals_hub.py static tab list) — authed, only 200 is honest.
      expect(res.status(), `${url} must render for the seeded admin`).toBe(200);
      const html = await res.text();
      expect(html.trim().length, `${url} returned empty response`).toBeGreaterThan(10);
      expect(html).not.toMatch(/^(Internal Server Error|Not Found)$/);
    });
  }

  test('/v2/approvals full page renders the authed app shell', async ({ request }) => {
    const res = await request.get('/v2/approvals', { headers: { Accept: 'text/html' } });
    expect(res.status(), '/v2/approvals must render for the seeded admin').toBe(200);
    const html = await res.text();
    expect(html.trim().length, '/v2/approvals returned empty page').toBeGreaterThan(100);
  });
});

test.describe('Dead-End Detector — Approvals Workspace 404 handling', () => {
  // Every pane URL a list row / kanban card can dispatch, with an id that
  // cannot exist — must be a clean 404, never a crash.
  const MISSING_ID_URLS = [
    '/v2/partials/approvals/plan/999999/pane',
    '/v2/partials/approvals/po/999999/pane',
    '/v2/partials/approvals/prepayments/999999/pane',
    '/v2/partials/approvals/po/999999/sent-check',
  ];

  for (const url of MISSING_ID_URLS) {
    test(`${url} returns 404, not crash`, async ({ request }) => {
      const res = await request.get(url, { headers: { 'HX-Request': 'true' } });
      expect(res.status(), `${url} must 404 on a missing id`).toBe(404);
    });
  }

  test('unknown tab body, list, and export return 404, not crash', async ({ request }) => {
    for (const url of ['/v2/partials/approvals/nope', '/v2/partials/approvals/nope/list', '/v2/partials/approvals/nope/export']) {
      const res = await request.get(url, { headers: { 'HX-Request': 'true' } });
      expect(res.status(), `${url} must reject an unknown tab with 404`).toBe(404);
    }
  });
});

test.describe('Dead-End Detector — Approvals Workspace form endpoints accept POST', () => {
  // Invalid-payload probes ONLY (empty body / missing ids) — each pinned to
  // its observed authed status: validation 400s, the prepayment-method 403
  // (approval-state guard), and 404s for rows that cannot exist on empty DB.
  const POST_ENDPOINTS: Array<{ url: string; expected: number; why: string }> = [
    { url: '/v2/partials/approvals/notes', expected: 400, why: 'empty form fails validation' },
    { url: '/v2/partials/approvals/attachments', expected: 400, why: 'empty form fails validation' },
    { url: '/v2/partials/approvals/prepayments/1/method', expected: 403, why: 'approval-state guard rejects' },
    { url: '/v2/partials/approvals/plan/1/qp-sales', expected: 404, why: 'plan 1 missing on empty DB' },
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

  test('DELETE attachment returns 404 on a missing id', async ({ request }) => {
    const res = await request.delete('/v2/partials/approvals/attachments/999999', {
      headers: { 'HX-Request': 'true' },
    });
    expect(res.status(), 'attachment delete must 404 on a missing id').toBe(404);
  });

  test('CSV export endpoints respond 200 for every tab', async ({ request }) => {
    for (const tab of TABS) {
      const res = await request.get(`/v2/partials/approvals/${tab}/export`, {
        headers: { 'HX-Request': 'true' },
      });
      expect(res.status(), `${tab} export must serve CSV for the seeded admin`).toBe(200);
    }
  });
});
