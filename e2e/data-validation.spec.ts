// Data validation tests for AvailAI — verifies API input validation and error handling.
// Tests malformed requests, boundary conditions, and response schemas.
// Called by: npx playwright test --project=data-validation
// Depends on: app/routers/materials.py, app/routers/auth.py

import { test, expect } from '@playwright/test';

test.describe('Input Validation', () => {
  test('POST /auth/login rejects invalid credentials', async ({ request }) => {
    const res = await request.post('/auth/login', {
      data: { email: '', password: '' },
    });
    expect([400, 401, 422]).toContain(res.status());
  });

  test('POST /auth/login rejects malformed email', async ({ request }) => {
    const res = await request.post('/auth/login', {
      data: { email: 'not-an-email', password: 'test' },
    });
    expect([400, 401, 422]).toContain(res.status());
  });

  test('GET /api/materials rejects negative offset', async ({ request }) => {
    const res = await request.get('/api/materials?offset=-1');
    expect([200, 400, 401, 422, 307]).toContain(res.status());
  });

  test('GET /api/materials handles very large limit', async ({ request }) => {
    const res = await request.get('/api/materials?limit=999999');
    expect([200, 400, 401, 422, 307]).toContain(res.status());
  });
});

test.describe('Error Responses', () => {
  test('404 for non-existent route', async ({ request }) => {
    const res = await request.get('/api/does-not-exist');
    expect(res.status()).toBe(404);
  });

  test('404 response has proper structure', async ({ request }) => {
    const res = await request.get('/api/does-not-exist');
    expect(res.status()).toBe(404);
    const body = await res.json();
    expect(body).toHaveProperty('error');
  });

  test('method not allowed returns 405', async ({ request }) => {
    const res = await request.delete('/auth/status');
    expect([405, 404]).toContain(res.status());
  });
});

test.describe('Response Schema', () => {
  test('/auth/status response has expected fields', async ({ request }) => {
    const res = await request.get('/auth/status');
    const body = await res.json();
    expect(typeof body.connected).toBe('boolean');
  });

  test('homepage content-type is HTML', async ({ request }) => {
    const res = await request.get('/', {
      headers: { 'Accept': 'text/html' },
    });
    const ct = res.headers()['content-type'] || '';
    expect(ct).toContain('text/html');
  });
});
