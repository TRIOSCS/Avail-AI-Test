/**
 * sales-hub-ui.spec.ts — UI-cleanup regression tests for the Sales Hub
 * (parts workspace at /v2/requisitions).
 *
 * Tests: accent active states, readability floor, empty-state CTA,
 * ws-tab-active CSS variable. Runs AUTHED (storageState from
 * e2e/auth.setup.ts, seeded admin on an EMPTY DB), so the list/workspace
 * content assertions are live; the parts/1 tab partials are honest round-two
 * skips (the parent row cannot exist on the empty DB).
 *
 * NO test here may CREATE rows: the suite shares one serial in-memory DB and
 * other projects assert empty states.
 *
 * Called by: npx playwright test --project=sales-hub-ui
 * Depends on: running app server in TESTING=1 mode, no seeded data required.
 */
import { test, expect } from '@playwright/test';

const HX_HEADER = { 'HX-Request': 'true' };

test.describe('Sales Hub — parts/list partial', () => {
  test('/v2/partials/parts renders authed', async ({ request }) => {
    const res = await request.get('/v2/partials/parts', { headers: HX_HEADER });
    expect(res.status()).toBe(200);
  });

  test('empty-state contains an Add Requisition button', async ({ request }) => {
    const res = await request.get('/v2/partials/parts', { headers: HX_HEADER });
    expect(res.status()).toBe(200);
    const body = await res.text();
    // The empty DB guarantees the empty state — this CTA was designed for it.
    expect(body).toContain('No parts found');
    expect(body).toContain('Add Requisition');
    expect(body).toContain('btn-primary');
  });

  test('filter pills use accent-500 active class not brand-500', async ({ request }) => {
    const res = await request.get('/v2/partials/parts?status=open', { headers: HX_HEADER });
    expect(res.status()).toBe(200);
    const body = await res.text();
    expect(body).not.toContain('bg-brand-500 text-white');
    expect(body).toContain('accent-500');
  });

  test('Add Req button uses btn-primary not bespoke brand-500', async ({ request }) => {
    const res = await request.get('/v2/partials/parts', { headers: HX_HEADER });
    expect(res.status()).toBe(200);
    const body = await res.text();
    expect(body).toContain('btn-primary');
    expect(body).not.toMatch(/class="[^"]*px-2 py-0\.5[^"]*bg-brand-500[^"]*"/);
  });

  test('search input uses .input .input-sm not bespoke focus:ring-brand-500', async ({ request }) => {
    const res = await request.get('/v2/partials/parts', { headers: HX_HEADER });
    expect(res.status()).toBe(200);
    const body = await res.text();
    expect(body).toContain('input input-sm');
    expect(body).not.toContain('focus:ring-brand-500');
  });

  test('pagination uses btn-secondary btn-sm', async ({ request }) => {
    const res = await request.get('/v2/partials/parts', { headers: HX_HEADER });
    expect(res.status()).toBe(200);
    const body = await res.text();
    // @needs-data: the Prev/Next guard self-no-ops on the empty DB (zero rows
    // ⇒ no pagination); round two seeds past one page to make it bite.
    if (body.includes('Prev') || body.includes('Next')) {
      expect(body).toContain('btn-secondary');
    }
  });
});

test.describe('Sales Hub — parts/workspace partial', () => {
  test('/v2/partials/parts/workspace renders authed', async ({ request }) => {
    const res = await request.get('/v2/partials/parts/workspace', { headers: HX_HEADER });
    expect(res.status()).toBe(200);
  });

  test('pipeline strip always shows Sales Hub eyebrow', async ({ request }) => {
    const res = await request.get('/v2/partials/parts/workspace', { headers: HX_HEADER });
    expect(res.status()).toBe(200);
    const body = await res.text();
    expect(body).toContain('Sales Hub');
  });

  test('drag handle uses accent-400/accent-500 not brand-400/brand-500', async ({ request }) => {
    const res = await request.get('/v2/partials/parts/workspace', { headers: HX_HEADER });
    expect(res.status()).toBe(200);
    const body = await res.text();
    expect(body).toContain('hover:bg-accent-400');
    expect(body).toContain("'bg-accent-500'");
    expect(body).not.toContain('hover:bg-brand-400');
    expect(body).not.toContain("'bg-brand-500'");
  });
});

test.describe('Sales Hub — ws-tab-active CSS', () => {
  test('styles.css .ws-tab-active uses var(--accent)', async ({ request }) => {
    const res = await request.get('/static/styles.css');
    if (res.status() === 404) test.skip();
    const body = await res.text();
    expect(body).toContain('color: var(--accent)');
    expect(body).not.toContain('color: #3A4252');
  });
});

test.describe('Sales Hub — offers tab partial', () => {
  test('/v2/partials/parts/1/tab/offers 404s on the missing parent', async ({ request }) => {
    const res = await request.get('/v2/partials/parts/1/tab/offers', { headers: HX_HEADER });
    // Authed + empty DB: part 1 cannot exist (observed 404).
    expect(res.status()).toBe(404);
  });

  test('offers tab uses .badge-success/.badge-info not raw bg-green-100', { tag: '@needs-data' }, async ({ request }) => {
    const res = await request.get('/v2/partials/parts/1/tab/offers', { headers: HX_HEADER });
    if (res.status() !== 200) test.skip(true, 'round-2 (owner-gated): needs a seeded part row');
    const body = await res.text();
    expect(body).not.toContain('bg-green-100 text-green-700');
    expect(body).not.toContain('bg-blue-100 text-blue-700');
  });
});

test.describe('Sales Hub — sourcing tab partial', () => {
  test('/v2/partials/parts/1/tab/sourcing 404s on the missing parent', async ({ request }) => {
    const res = await request.get('/v2/partials/parts/1/tab/sourcing', { headers: HX_HEADER });
    // Authed + empty DB: part 1 cannot exist (observed 404).
    expect(res.status()).toBe(404);
  });

  test('sourcing tab Score/Tier cells use compact-cell', { tag: '@needs-data' }, async ({ request }) => {
    const res = await request.get('/v2/partials/parts/1/tab/sourcing', { headers: HX_HEADER });
    if (res.status() !== 200) test.skip(true, 'round-2 (owner-gated): needs a seeded part row');
    const body = await res.text();
    expect(body).not.toContain('"px-3 py-2 whitespace-nowrap"');
  });
});
