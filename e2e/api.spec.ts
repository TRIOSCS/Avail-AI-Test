// API endpoint tests for AvailAI core CRUD operations.
// Runs AUTHED (storageState from e2e/auth.setup.ts, seeded admin on an EMPTY
// DB) — list endpoints assert the real wrapper shapes; invalid writes are
// invalid-payload probes only (no test may CREATE rows: the suite shares one
// serial in-memory DB and other projects assert empty states).
// Called by: npx playwright test --project=api
// Depends on: app/main.py, app/routers/materials.py, app/routers/sources.py

import { test, expect } from '@playwright/test';

test.describe('Materials API', () => {
  test('GET /api/materials returns the materials wrapper', async ({ request }) => {
    const res = await request.get('/api/materials');
    expect(res.status()).toBe(200);
    // Real wrapper shape: {materials: [], total, limit, offset} — NOT a bare
    // top-level array (routers/materials.py).
    const body = await res.json();
    expect(Array.isArray(body.materials)).toBe(true);
    expect(typeof body.total).toBe('number');
  });

  test('GET /api/materials with query params', async ({ request }) => {
    const res = await request.get('/api/materials?limit=5&offset=0');
    expect(res.status()).toBe(200);
    const body = await res.json();
    expect(Array.isArray(body.materials)).toBe(true);
  });

  test('GET /api/materials/by-mpn/TEST123 returns material or 404', async ({ request }) => {
    const res = await request.get('/api/materials/by-mpn/TEST123');
    // Empty DB → 404 today; 200 stays valid for a data-seeded round two.
    expect([200, 404]).toContain(res.status());
  });

  test('GET /api/materials/999999 returns 404 for non-existent', async ({ request }) => {
    const res = await request.get('/api/materials/999999');
    expect([404, 422]).toContain(res.status());
  });

  test('PUT /api/materials/999999 rejects invalid update', async ({ request }) => {
    const res = await request.put('/api/materials/999999', {
      data: { notes: 'test' },
    });
    expect([404, 422]).toContain(res.status());
  });

  test('POST /api/materials/merge rejects empty body', async ({ request }) => {
    const res = await request.post('/api/materials/merge', { data: {} });
    expect([400, 422]).toContain(res.status());
  });
});

test.describe('Sources API', () => {
  test('GET /api/sources returns the sources wrapper', async ({ request }) => {
    const res = await request.get('/api/sources');
    expect(res.status()).toBe(200);
    // Real wrapper shape: {sources: []} (routers/sources.py,
    // schemas/responses.py) — NOT a bare top-level array.
    const body = await res.json();
    expect(Array.isArray(body.sources)).toBe(true);
  });
});
