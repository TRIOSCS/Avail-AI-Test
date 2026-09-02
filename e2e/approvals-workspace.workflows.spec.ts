/**
 * approvals-workspace.workflows.spec.ts — Approvals Workspace journey coverage.
 *
 * Walks the request-level shape of the 4-tab split-view console (shell → tab body →
 * work list → detail pane → decide/notes/export endpoints) exactly as the HTMX
 * front-end drives it. Runs AUTHED (storageState from e2e/auth.setup.ts, seeded
 * admin on an EMPTY DB): the workspace chrome renders unconditionally from the
 * static four-tab list, so every GET surface is a hard 200; missing ids are hard
 * 404s; invalid mutations pin their observed validation/guard statuses.
 *
 * NO test here may CREATE rows: the suite shares one serial in-memory DB and
 * other projects assert empty states. All POSTs are invalid-payload probes.
 * The authenticated in-browser journeys live in
 * tests/e2e/test_approvals_workspace_e2e.py (authed_page against the live app).
 *
 * Called by: npx playwright test --project=workflows
 * Depends on: running app server in TESTING=1 mode; app/routers/htmx/approvals_hub.py
 */

import { test, expect } from '@playwright/test';

const TABS = ['sales-orders', 'buy-plans', 'purchase-orders', 'prepayments'];
const LEGACY_TABS = ['buy-plan', 'po-approval', 'prepayment'];
const HX = { 'HX-Request': 'true' };

test.describe('Approvals Workspace — shell and tabs', () => {
  test('shell renders the four tab pills and the lazy body', async ({ request }) => {
    const res = await request.get('/v2/partials/approvals', { headers: HX });
    expect(res.status(), 'approvals shell must render for the seeded admin').toBe(200);
    const html = await res.text();
    for (const label of ['Sales Orders', 'Buy Plans', 'Purchase Orders', 'Prepayments']) {
      expect(html, `shell missing tab pill: ${label}`).toContain(label);
    }
    expect(html, 'shell missing the lazy tab body target').toContain('ap-hub-body');
  });

  test('shell accepts ?tab= deep links for every tab and legacy alias', async ({ request }) => {
    for (const tab of [...TABS, ...LEGACY_TABS]) {
      const res = await request.get(`/v2/partials/approvals?tab=${tab}`, { headers: HX });
      expect(res.status(), `shell ?tab=${tab} must render for the seeded admin`).toBe(200);
    }
  });

  test('each tab body renders the split view (list + pane)', async ({ request }) => {
    for (const tab of TABS) {
      const res = await request.get(`/v2/partials/approvals/${tab}`, { headers: HX });
      expect(res.status(), `tab body ${tab} must render for the seeded admin`).toBe(200);
      const html = await res.text();
      expect(html, `${tab} body missing the left list container`).toContain('aw-list');
      expect(html, `${tab} body missing the right pane container`).toContain('aw-pane');
    }
  });

  test('legacy tab keys alias onto the workspace tab bodies', async ({ request }) => {
    for (const tab of LEGACY_TABS) {
      const res = await request.get(`/v2/partials/approvals/${tab}`, { headers: HX });
      expect(res.status(), `legacy tab body ${tab} must render for the seeded admin`).toBe(200);
    }
  });

  test('an unknown tab is rejected, never rendered', async ({ request }) => {
    for (const url of ['/v2/partials/approvals/not-a-tab', '/v2/partials/approvals/not-a-tab/list']) {
      const res = await request.get(url, { headers: HX, maxRedirects: 0 });
      // Authed, an unknown tab has exactly one honest status; a 200 here
      // would be a routing bug.
      expect(res.status(), `${url} must reject an unknown tab with 404`).toBe(404);
    }
  });

  test('full page /v2/approvals renders the authed app shell', async ({ request }) => {
    const res = await request.get('/v2/approvals', { headers: { Accept: 'text/html' } });
    expect(res.status(), '/v2/approvals must render for the seeded admin').toBe(200);
  });
});

test.describe('Approvals Workspace — work lists', () => {
  test('every tab list renders with its filter bar', async ({ request }) => {
    for (const tab of TABS) {
      const res = await request.get(`/v2/partials/approvals/${tab}/list`, { headers: HX });
      expect(res.status(), `${tab} list must render for the seeded admin`).toBe(200);
      const html = await res.text();
      expect(html, `${tab} list missing the filter form`).toContain('aw-filters');
    }
  });

  test('list search / Mine scope / Closed filter params are accepted', async ({ request }) => {
    for (const tab of TABS) {
      const url = `/v2/partials/approvals/${tab}/list?q=ZZZ-NO-MATCH&scope=mine&show_closed=true`;
      const res = await request.get(url, { headers: HX });
      expect(res.status(), `${tab} filtered list must render for the seeded admin`).toBe(200);
    }
  });

  test('CSV export responds 200 for every tab (and legacy keys)', async ({ request }) => {
    for (const tab of [...TABS, ...LEGACY_TABS]) {
      const res = await request.get(`/v2/partials/approvals/${tab}/export?scope=all`, { headers: HX });
      expect(res.status(), `${tab} export must serve CSV for the seeded admin`).toBe(200);
    }
  });
});

test.describe('Approvals Workspace — detail panes', () => {
  test('panes reject missing ids cleanly (404, never 5xx)', async ({ request }) => {
    const urls = [
      '/v2/partials/approvals/plan/999999/pane',
      '/v2/partials/approvals/plan/999999/pane?lens=buy-plans',
      '/v2/partials/approvals/po/999999/pane',
      '/v2/partials/approvals/prepayments/999999/pane',
      '/v2/partials/approvals/po/999999/sent-check',
    ];
    for (const url of urls) {
      const res = await request.get(url, { headers: HX, maxRedirects: 0 });
      expect(res.status(), `${url} must 404 on a missing id`).toBe(404);
    }
  });
});

test.describe('Approvals Workspace — mutation endpoints validate, never crash', () => {
  const form = { 'HX-Request': 'true', 'Content-Type': 'application/x-www-form-urlencoded' };

  test('add-note rejects an empty/subject-less submission', async ({ request }) => {
    const res = await request.post('/v2/partials/approvals/notes', {
      headers: form,
      data: '',
      maxRedirects: 0,
    });
    // Authed, the exactly-one-subject guard rejects the empty form (observed).
    expect(res.status()).toBe(400);
  });

  test('attachment upload rejects an empty submission', async ({ request }) => {
    const res = await request.post('/v2/partials/approvals/attachments', {
      headers: form,
      data: '',
      maxRedirects: 0,
    });
    expect(res.status()).toBe(400);
  });

  test('attachment delete handles a missing id cleanly', async ({ request }) => {
    const res = await request.delete('/v2/partials/approvals/attachments/999999', {
      headers: HX,
      maxRedirects: 0,
    });
    expect(res.status()).toBe(404);
  });

  test('prepayment method adjust validates before writing', async ({ request }) => {
    const res = await request.post('/v2/partials/approvals/prepayments/999999/method', {
      headers: form,
      data: 'payment_method=wire',
      maxRedirects: 0,
    });
    // Observed authed 403: the approval-state guard rejects before the
    // missing-row lookup.
    expect(res.status()).toBe(403);
  });

  test('QP-sales save validates before writing', async ({ request }) => {
    const res = await request.post('/v2/partials/approvals/plan/999999/qp-sales', {
      headers: form,
      data: 'qp_sales_condition=NEW',
      maxRedirects: 0,
    });
    // Observed authed 404: plan 999999 cannot exist on the empty DB.
    expect(res.status()).toBe(404);
  });
});
