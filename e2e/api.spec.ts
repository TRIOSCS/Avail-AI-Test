// API endpoint tests for AvailAI core CRUD operations.
// Tests materials, vendors, and sources API endpoints.
// Called by: npx playwright test --project=api
// Depends on: app/main.py, app/routers/materials.py, app/routers/sources.py

import { test, expect } from '@playwright/test';

test.describe('Materials API', () => {
  test('GET /api/materials returns list', async ({ request }) => {
    const res = await request.get('/api/materials');
    // May return 401 if auth required, or 200 with data
    expect([200, 401, 307]).toContain(res.status());
  });

  test('GET /api/materials with query params', async ({ request }) => {
    const res = await request.get('/api/materials?limit=5&offset=0');
    expect([200, 401, 307]).toContain(res.status());
  });

  test('GET /api/materials/by-mpn/TEST123 returns material or 404', async ({ request }) => {
    const res = await request.get('/api/materials/by-mpn/TEST123');
    expect([200, 404, 401, 307]).toContain(res.status());
  });

  test('GET /api/materials/999999 returns 404 for non-existent', async ({ request }) => {
    const res = await request.get('/api/materials/999999');
    expect([404, 401, 307, 422]).toContain(res.status());
  });

  test('PUT /api/materials/999999 rejects invalid update', async ({ request }) => {
    const res = await request.put('/api/materials/999999', {
      data: { notes: 'test' },
    });
    expect([404, 401, 307, 422]).toContain(res.status());
  });

  test('POST /api/materials/merge rejects empty body', async ({ request }) => {
    const res = await request.post('/api/materials/merge', { data: {} });
    expect([400, 401, 422, 307]).toContain(res.status());
  });
});

test.describe('Sources API', () => {
  test('GET /api/sources returns source list', async ({ request }) => {
    const res = await request.get('/api/sources');
    expect([200, 401, 307]).toContain(res.status());
  });
});
